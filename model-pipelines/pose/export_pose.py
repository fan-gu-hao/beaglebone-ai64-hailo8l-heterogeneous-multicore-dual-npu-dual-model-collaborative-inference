from pathlib import Path
from importlib.metadata import version
from collections import Counter
import hashlib
import json
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
REPO = ROOT / 'sources/mmpose'
EXPECTED = 'd1e58e7b3b932552fd97e9182fff7d81994a44753962ac568a2fd087e2aad4cd'
COMMIT = '0d9a00c1d64b68999e4b2a4ee5ed0d5ac2c3e296'

if platform.machine() != 'x86_64' or sys.version_info[:2] != (3, 10):
    raise SystemExit('請使用筆電的新姿態模型 Python 3.10 環境。')

versions = {name: version(name) for name in
            ('torch', 'torchvision', 'mmcv-full', 'mmpose',
             'numpy', 'onnx', 'onnxruntime')}
print('使用的套件：', json.dumps(versions, ensure_ascii=False), flush=True)

if versions['mmpose'] != '0.29.0' or versions['mmcv-full'] != '1.6.2':
    raise SystemExit('MMPose 或 MMCV 版本不符。')

commit = subprocess.check_output(
    ['git', '-C', str(REPO), 'rev-parse', 'HEAD'], text=True).strip()
if commit != COMMIT:
    raise SystemExit('官方原始碼版本與上一步不同。')

checkpoint = ROOT / 'models/mobilenetv2_coco_256x192-d1e58e7b_20200727.pth'
if hashlib.sha256(checkpoint.read_bytes()).hexdigest() != EXPECTED:
    raise SystemExit('原始權重的 SHA256 不符。')

import numpy as np
import onnx
import onnxruntime as ort
import torch
from mmcv import Config
from mmcv.runner import load_checkpoint
from mmpose.models import build_posenet

torch.set_num_threads(4)
torch.manual_seed(20261115)

config = REPO / (
    'configs/body/2d_kpt_sview_rgb_img/topdown_heatmap/'
    'coco/mobilenetv2_coco_256x192.py'
)
cfg = Config.fromfile(str(config))
cfg.model.pretrained = None
cfg.model.test_cfg.flip_test = False

model = build_posenet(cfg.model)
load_checkpoint(model, str(checkpoint), map_location='cpu', strict=True)
model.cpu().float().eval()
model.forward = model.forward_dummy

print('PASS：官方模型結構與原始權重完整對應。', flush=True)

mean = np.array([0.485, 0.456, 0.406], np.float32)[None, :, None, None]
std = np.array([0.229, 0.224, 0.225], np.float32)[None, :, None, None]
rng = np.random.default_rng(20261115)

cases = [
    ('中灰影像', np.full((1, 3, 256, 192), 128, dtype=np.uint8)),
    ('隨機影像一', rng.integers(0, 256, (1, 3, 256, 192), dtype=np.uint8)),
    ('隨機影像二', rng.integers(0, 256, (1, 3, 256, 192), dtype=np.uint8)),
]
cases = [
    (name, np.ascontiguousarray((x.astype(np.float32) / 255 - mean) / std))
    for name, x in cases
]

output = ROOT / 'models/mobilenetv2_pose_256x192.fp32.onnx'
temporary = output.with_name(output.stem + '.partial.onnx')

print('\n開始匯出 ONNX……', flush=True)
with torch.no_grad():
    torch.onnx.export(
        model, torch.from_numpy(cases[0][1]), str(temporary),
        export_params=True, opset_version=11,
        do_constant_folding=True, keep_initializers_as_inputs=False,
        input_names=['image'], output_names=['heatmaps']
    )

graph = onnx.load(str(temporary))
onnx.checker.check_model(graph)

def shape(value):
    return [d.dim_value for d in value.type.tensor_type.shape.dim]

if len(graph.graph.input) != 1 or len(graph.graph.output) != 1:
    raise SystemExit('模型的輸入或輸出數量不符。')

input_shape = shape(graph.graph.input[0])
output_shape = shape(graph.graph.output[0])

if input_shape != [1, 3, 256, 192] or output_shape != [1, 17, 64, 48]:
    raise SystemExit(f'模型尺寸不符：{input_shape} → {output_shape}')

for value in (graph.graph.input[0], graph.graph.output[0]):
    if value.type.tensor_type.elem_type != onnx.TensorProto.FLOAT:
        raise SystemExit('模型輸入輸出不是 FP32。')

options = ort.SessionOptions()
options.intra_op_num_threads = 4
options.inter_op_num_threads = 1
session = ort.InferenceSession(
    str(temporary), sess_options=options,
    providers=['CPUExecutionProvider']
)

results = []
for name, sample in cases:
    with torch.no_grad():
        reference = model(torch.from_numpy(sample)).numpy()

    actual = session.run(['heatmaps'], {'image': sample})[0]
    error = np.abs(reference - actual)
    passed = (
        np.isfinite(reference).all()
        and np.isfinite(actual).all()
        and np.allclose(reference, actual, rtol=1e-4, atol=1e-5)
    )
    print(
        f'{name}：最大差={error.max():.8g}，平均差={error.mean():.8g}，'
        f'{"PASS" if passed else "FAIL"}',
        flush=True
    )
    if not passed:
        raise SystemExit('匯出前後數值比對未通過，停止。')

    results.append({
        'name': name,
        'max_abs_error': float(error.max()),
        'mean_abs_error': float(error.mean())
    })

temporary.replace(output)
digest = hashlib.sha256(output.read_bytes()).hexdigest()
operators = dict(sorted(Counter(n.op_type for n in graph.graph.node).items()))

report = {
    'stage': 'onnx_fp32',
    'checkpoint_sha256': EXPECTED,
    'mmpose_commit': commit,
    'versions': versions,
    'onnx_path': str(output),
    'onnx_sha256': digest,
    'opset': 11,
    'input_shape': input_shape,
    'output_shape': output_shape,
    'preprocessing': {
        'location': 'outside_model',
        'color': 'RGB',
        'layout': 'NCHW',
        'divide_by': 255,
        'mean': [0.485, 0.456, 0.406],
        'std': [0.229, 0.224, 0.225]
    },
    'operators': operators,
    'numerical_tests_synthetic_inputs': results,
    'quantized': False,
    'ti_hardware_verified': False
}
(ROOT / 'reports').mkdir(exist_ok=True)
(ROOT / 'reports/export-report.json').write_text(
    json.dumps(report, ensure_ascii=False, indent=2) + '\n',
    encoding='utf-8'
)

print('\n=== 姿態模型匯出完成 ===')
print('輸入：', input_shape, 'RGB，FP32')
print('輸出：', output_shape, '17 個關節熱圖，FP32')
print('運算子：', operators)
print('ONNX：', output)
print('SHA256：', digest)
print('PASS：原始權重 → ONNX，以及三組數值一致性檢查完成。')
print('這次尚未量化；也尚未在 TI MMA 上執行。')
