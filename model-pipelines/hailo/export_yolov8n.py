from pathlib import Path
from importlib.metadata import version
import hashlib
import json
import os
import platform
import urllib.request

ROOT = Path(__file__).resolve().parent
if platform.machine() != "x86_64":
    raise SystemExit("請在 Ubuntu 筆電執行。")

for folder in ("models", "reports", "settings"):
    (ROOT / folder).mkdir(parents=True, exist_ok=True)

os.environ["YOLO_CONFIG_DIR"] = str(ROOT / "settings")
os.environ["YOLO_AUTOINSTALL"] = "false"

import cv2
import numpy as np
import onnx
import onnxruntime as ort
import torch
from ultralytics import YOLO
from ultralytics.data.augment import LetterBox
from ultralytics.utils import ASSETS

expected_versions = {
    "torch": "2.5.1",
    "torchvision": "0.20.1",
    "ultralytics": "8.3.161",
    "numpy": "1.26.4",
    "onnx": "1.16.2",
    "onnxruntime": "1.20.1",
    "opencv-python": "4.11.0.86",
}
versions = {}
for package, expected in expected_versions.items():
    actual = version(package)
    versions[package] = actual
    if actual.split("+")[0] != expected:
        raise SystemExit(f"版本不符：{package}={actual}，預期 {expected}")

torch.set_num_threads(4)
torch.set_num_interop_threads(1)

def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

url = "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.pt"
expected_hash = "f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36"
weights = ROOT / "models/yolov8n.pt"

if not weights.is_file():
    print("取得原始 YOLOv8n 預訓練權重……", flush=True)
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise SystemExit("下載的原始權重雜湊值不符，停止。")
    temporary = weights.with_suffix(".pt.part")
    temporary.write_bytes(data)
    temporary.replace(weights)

if sha256(weights) != expected_hash:
    raise SystemExit("現有 yolov8n.pt 與指定原始權重不同，停止。")

print("原始權重 SHA256：", sha256(weights), flush=True)
model = YOLO(str(weights), task="detect")
wanted = {0: "person", 39: "bottle", 41: "cup", 67: "cell phone"}
if len(model.names) != 80 or any(model.names[i] != name for i, name in wanted.items()):
    raise SystemExit("模型類別與預期的 COCO 80 類不符。")

options = dict(
    format="onnx", imgsz=640, batch=1, opset=11,
    dynamic=False, simplify=False, nms=False,
    half=False, device="cpu",
)
print("\n開始由原始權重匯出 ONNX……", flush=True)
output = Path(model.export(**options)).resolve()
if not output.is_file():
    raise SystemExit("沒有產生 ONNX 檔案。")

graph = onnx.load(str(output))
onnx.checker.check_model(graph)

def shape(value):
    return [d.dim_value for d in value.type.tensor_type.shape.dim]

if len(graph.graph.input) != 1 or len(graph.graph.output) != 1:
    raise SystemExit("ONNX 輸入或輸出數量不符。")

input_shape = shape(graph.graph.input[0])
output_shape = shape(graph.graph.output[0])
if input_shape != [1, 3, 640, 640] or output_shape != [1, 84, 8400]:
    raise SystemExit(f"張量尺寸不符：{input_shape} → {output_shape}")

end_nodes = [
    f"/model.22/{branch}.{scale}/{branch}.{scale}.2/Conv"
    for scale in range(3)
    for branch in ("cv2", "cv3")
]
nodes = {node.name: node for node in graph.graph.node}
for name in end_nodes:
    if name not in nodes or nodes[name].op_type != "Conv":
        raise SystemExit(f"缺少預期的偵測輸出節點：{name}")

print("ONNX 輸入：", input_shape, flush=True)
print("ONNX 輸出：", output_shape, flush=True)
print("PASS：ONNX 結構與六個偵測輸出節點檢查通過。", flush=True)

reference = YOLO(str(weights), task="detect").model.float().cpu().eval()
reference.fuse(verbose=False)
session_options = ort.SessionOptions()
session_options.intra_op_num_threads = 4
session_options.inter_op_num_threads = 1
session = ort.InferenceSession(
    str(output), sess_options=session_options,
    providers=["CPUExecutionProvider"],
)
letterbox = LetterBox(new_shape=(640, 640), auto=False)
comparisons = {}

print("\n開始比對原始模型與 ONNX 的推論數值……", flush=True)
for name in ("bus.jpg", "zidane.jpg"):
    sample = Path(ASSETS) / name
    image = cv2.imread(str(sample))
    if image is None:
        raise SystemExit(f"讀不到比對圖片：{sample}")

    image = letterbox(image=image)
    tensor = np.ascontiguousarray(
        image[:, :, ::-1].transpose(2, 0, 1)[None],
        dtype=np.float32,
    ) / 255.0

    with torch.inference_mode():
        expected = reference(torch.from_numpy(tensor))
        if isinstance(expected, (tuple, list)):
            expected = expected[0]
        expected = expected.cpu().numpy()

    actual = session.run(None, {session.get_inputs()[0].name: tensor})[0]
    if actual.shape != expected.shape:
        raise SystemExit(f"{name}：兩個模型的輸出尺寸不一致。")
    if not np.isfinite(actual).all() or not np.isfinite(expected).all():
        raise SystemExit(f"{name}：輸出包含無效數值。")

    box_error = float(np.max(np.abs(actual[:, :4] - expected[:, :4])))
    score_error = float(np.max(np.abs(actual[:, 4:] - expected[:, 4:])))
    passed = bool(np.allclose(actual, expected, rtol=1e-4, atol=1e-3))
    comparisons[name] = {
        "image_sha256": sha256(sample),
        "max_box_error_pixels": box_error,
        "max_score_error": score_error,
        "passed": passed,
    }
    print(
        f"{name}：框座標最大差={box_error:.8f} 像素，"
        f"分數最大差={score_error:.8f}，"
        f"{'PASS' if passed else 'FAIL'}",
        flush=True,
    )
    if not passed:
        raise SystemExit("轉換前後的數值差異超過門檻，停止。")

report = {
    "passed": True,
    "source_url": url,
    "weights_sha256": sha256(weights),
    "onnx_sha256": sha256(output),
    "input_shape": input_shape,
    "output_shape": output_shape,
    "opset": 11,
    "export_options": options,
    "preprocessing": "RGB, letterbox 640x640, padding 114, NCHW float32 / 255",
    "end_nodes": end_nodes,
    "comparisons": comparisons,
    "tolerance": {"rtol": 1e-4, "atol": 1e-3},
    "versions": versions,
}
(ROOT / "reports/export-report.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2) + "\n"
)
(ROOT / "reports/hailo-end-nodes.json").write_text(
    json.dumps(end_nodes, indent=2) + "\n"
)

print("\n=== 匯出完成 ===", flush=True)
print("ONNX：", output, flush=True)
print("ONNX SHA256：", report["onnx_sha256"], flush=True)
print("PASS：原始權重 → ONNX 匯出與兩張圖片數值比對完成。", flush=True)
print("此檔案尚未量化，也尚未編譯成 HEF。", flush=True)
