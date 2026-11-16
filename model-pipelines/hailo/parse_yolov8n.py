from pathlib import Path
from importlib.metadata import version
import hashlib
import json
import platform

ROOT = Path(__file__).resolve().parent
if platform.machine() != "x86_64":
    raise SystemExit("請在 Ubuntu 筆電執行。")
if version("hailo-dataflow-compiler") != "3.33.1":
    raise SystemExit("請使用先前建立的 DFC 3.33.1 環境。")

for folder in ("artifacts", "reports", "model_config"):
    (ROOT / folder).mkdir(parents=True, exist_ok=True)

def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

model = ROOT / "models/yolov8n.onnx"
export = json.loads((ROOT / "reports/export-report.json").read_text())
expected_hash = "32151df2e9e6b919d140eab881d96aa980fb8d0bf39b8f740c9c9367e3ae185d"
actual_hash = sha256(model)
if actual_hash != expected_hash or actual_hash != export["onnx_sha256"]:
    raise SystemExit("ONNX 與剛才驗證通過的檔案不同，停止。")
if export.get("passed") is not True:
    raise SystemExit("尚未通過 ONNX 匯出檢查。")

end_nodes = [
    f"/model.22/{branch}.{scale}/{branch}.{scale}.2/Conv"
    for scale in range(3) for branch in ("cv2", "cv3")
]
if export["end_nodes"] != end_nodes:
    raise SystemExit("匯出報告中的六個輸出節點不符。")

print("來源 ONNX：", model, flush=True)
print("SHA256：", actual_hash, flush=True)
print("編譯目標：Hailo-8L", flush=True)

from hailo_sdk_client import ClientRunner
runner = ClientRunner(hw_arch="hailo8l")
runner.translate_onnx_model(
    str(model), "yolov8n",
    start_node_names=["images"],
    end_node_names=end_nodes,
)

har = ROOT / "artifacts/yolov8n_hailo8l.parsed.har"
runner.save_har(str(har))
hn = runner.get_hn()
if isinstance(hn, (str, bytes)):
    hn = json.loads(hn)
save_json(ROOT / "reports/yolov8n_parsed_hn.json", hn)

layers = hn["layers"]
inputs = {n: v for n, v in layers.items() if v.get("type") == "input_layer"}
outputs = {n: v for n, v in layers.items() if v.get("type") == "output_layer"}
print("\n=== Hailo 輸入與輸出 ===", flush=True)
for name, layer in {**inputs, **outputs}.items():
    print(name, "尺寸=", layer.get("output_shapes"),
          "來源=", layer.get("input"), flush=True)
if len(inputs) != 1 or len(outputs) != 6:
    raise SystemExit("HAR 已保存，但輸入或輸出數量不符，請貼回輸出。")

direct_sources = {n for v in outputs.values() for n in v.get("input", [])}
mapping, decoders, class_layers = [], [], []

print("\n=== 六個偵測分支對應 ===", flush=True)
for scale in range(3):
    stride = 8 * (2 ** scale)
    spatial = 640 // stride
    decoder = {"name": f"yolov8n/bbox_decoder{stride}", "stride": stride}

    for branch, role, channels, key in (
        ("cv2", "框位置分布", 64, "reg_layer"),
        ("cv3", "類別分數", 80, "cls_layer"),
    ):
        original = f"/model.22/{branch}.{scale}/{branch}.{scale}.2/Conv"
        matches = [
            (name, layer) for name, layer in layers.items()
            if layer.get("type") == "conv"
            and original in layer.get("original_names", [])
        ]
        if len(matches) != 1:
            raise SystemExit(f"無法唯一對應分支：{original}")
        name, layer = matches[0]
        shapes = layer.get("output_shapes", [])
        if not shapes or list(shapes[0][-3:]) != [spatial, spatial, channels]:
            raise SystemExit(f"分支尺寸不符：{name} → {shapes}")
        if name not in direct_sources:
            raise SystemExit(f"分支沒有直接連到模型輸出：{name}")

        decoder[key] = name
        if key == "cls_layer":
            class_layers.append(name)
        mapping.append({
            "onnx_node": original, "hailo_layer": name,
            "role": role, "stride": stride, "shape": shapes[0],
        })
        print(f"步距={stride:2d}｜{role}｜{name}｜{shapes[0]}", flush=True)
    decoders.append(decoder)

nms = {
    "nms_scores_th": 0.2, "nms_iou_th": 0.7,
    "image_dims": [640, 640], "max_proposals_per_class": 100,
    "classes": 80, "regression_length": 16,
    "background_removal": False, "bbox_decoders": decoders,
}
nms_path = ROOT / "model_config/yolov8n_nms.json"
save_json(nms_path, nms)

script_lines = [
    "normalization1 = normalization([0.0, 0.0, 0.0], [255.0, 255.0, 255.0])"
]
for name in class_layers:
    short_name = name.rsplit("/", 1)[-1]
    script_lines.append(f"change_output_activation({short_name}, sigmoid)")
script_lines.extend([
    f"nms_postprocess({json.dumps(str(nms_path))}, meta_arch=yolov8, engine=cpu)",
    "allocator_param(width_splitter_defuse=disabled)",
])
script_path = ROOT / "model_config/yolov8n.alls"
script_path.write_text("\n".join(script_lines) + "\n")

if not har.is_file() or har.stat().st_size == 0:
    raise SystemExit("沒有產生有效大小的 HAR 檔案。")
report = {
    "passed": True, "stage": "parsed", "hw_arch": "hailo8l",
    "dfc_version": version("hailo-dataflow-compiler"),
    "source_onnx_sha256": actual_hash,
    "har_path": str(har), "har_sha256": sha256(har),
    "head_mapping": mapping,
    "model_script_path": str(script_path),
    "model_script_sha256": sha256(script_path),
    "nms_config_sha256": sha256(nms_path),
    "model_script_applied": False, "quantized": False,
}
save_json(ROOT / "reports/parse-report.json", report)

print("\n=== 解析完成 ===", flush=True)
print("HAR：", har, flush=True)
print("大小：", round(har.stat().st_size / 1024**2, 2), "MiB", flush=True)
print("HAR SHA256：", report["har_sha256"], flush=True)
print("量化階段使用的設定：", script_path, flush=True)
print("PASS：ONNX → HAR 解析與六分支對應完成。", flush=True)
print("後處理設定已建立；尚未套用、量化或編譯 HEF。", flush=True)
