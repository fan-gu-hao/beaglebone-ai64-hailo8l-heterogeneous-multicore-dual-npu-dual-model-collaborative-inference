from pathlib import Path
from importlib.metadata import version
import hashlib
import json
import platform
import sys
import time

ROOT = Path(__file__).resolve().parent
if platform.machine() != 'x86_64' or version('hailo-dataflow-compiler') != '3.33.1':
    raise SystemExit('請在 Ubuntu 筆電使用 dfc-python 執行。')
if len(sys.argv) != 2 or sys.argv[1] not in ('check', 'quantize'):
    raise SystemExit('參數必須是 check 或 quantize。')

import numpy as np
import tensorflow as tf
from hailo_sdk_client import ClientRunner, InferenceContext

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b''):
            h.update(block)
    return h.hexdigest()

def save(path, obj):
    path.write_text(json.dumps(
        obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n')

parsed = ROOT / 'artifacts/yolov8n_hailo8l.parsed.har'
onnx_path = ROOT / 'models/yolov8n.onnx'
base_script = ROOT / 'model_config/yolov8n.alls'
nms_config = ROOT / 'model_config/yolov8n_nms.json'
parse_info = json.loads((ROOT / 'reports/parse-report.json').read_text())
prep_path = ROOT / 'reports/calibration-report.json'
prep = json.loads(prep_path.read_text())
if parse_info['hw_arch'] != 'hailo8l' or not prep.get('passed'):
    raise SystemExit('模型目標或圖片前處理紀錄不符。')

for path, key in [
    (parsed, 'har_sha256'),
    (onnx_path, 'source_onnx_sha256'),
    (base_script, 'model_script_sha256'),
    (nms_config, 'nms_config_sha256')
]:
    if digest(path) != parse_info[key]:
        raise SystemExit(f'檔案與先前成功紀錄不同：{path}')

entry = prep['sets']['calibration']
calib_path = Path(entry['path'])
print('核對校正陣列完整性……', flush=True)
if digest(calib_path) != entry['sha256']:
    raise SystemExit('校正陣列校驗失敗。')
calib = np.load(calib_path, mmap_mode='r', allow_pickle=False)
if calib.shape != (1024, 640, 640, 3) or calib.dtype != np.float32:
    raise SystemExit('校正陣列尺寸或型別不同。')

gpus = tf.config.list_physical_devices('GPU')
if not gpus:
    raise SystemExit('TensorFlow 未偵測到 GPU，請貼回輸出。')
tf.config.experimental.enable_tensor_float_32_execution(False)
print('GPU：', gpus, flush=True)

identity = {
    'parsed_har_sha256': parse_info['har_sha256'],
    'onnx_sha256': parse_info['source_onnx_sha256'],
    'preparation_report_sha256': digest(prep_path)
}
check_path = ROOT / 'reports/native-check.json'

if sys.argv[1] == 'check':
    import onnx
    import onnxruntime as ort

    graph = onnx.load(str(onnx_path))
    nodes = {n.name: n for n in graph.graph.node}
    heads = []
    graph.graph.ClearField('output')

    for scale, stride in enumerate((8, 16, 32)):
        for branch, channels in ((2, 64), (3, 80)):
            name = (
                f'/model.22/cv{branch}.{scale}/'
                f'cv{branch}.{scale}.2/Conv'
            )
            h = 640 // stride
            graph.graph.output.append(
                onnx.helper.make_tensor_value_info(
                    nodes[name].output[0],
                    onnx.TensorProto.FLOAT,
                    [1, channels, h, h]
                )
            )
            heads.append((name, (h, h, channels)))

    onnx.checker.check_model(graph)
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 4
    session = ort.InferenceSession(
        graph.SerializeToString(),
        sess_options=opts,
        providers=['CPUExecutionProvider']
    )

    indices = prep['native_check_indices']
    x = np.asarray(calib[indices], dtype=np.float32) / 255.0
    reference = [[] for _ in heads]
    for image in x:
        values = session.run(None, {
            'images': np.ascontiguousarray(
                image.transpose(2, 0, 1)[None])
        })
        for collection, value in zip(reference, values):
            collection.append(value.transpose(0, 2, 3, 1))

    print('開始比對四張實拍照片的 ONNX 與原生 HAR 輸出……', flush=True)
    runner = ClientRunner(har=str(parsed), hw_arch='hailo8l')
    with runner.infer_context(InferenceContext.SDK_NATIVE) as ctx:
        result = runner.infer(ctx, x, batch_size=1)

    values = list(result.values()) if isinstance(result, dict) else result
    if not isinstance(values, (list, tuple)) or len(values) != 6:
        raise RuntimeError(f'HAR 輸出結構與預期不同：{type(result)}')

    by_shape = {}
    for value in values:
        value = np.asarray(value)
        if value.ndim != 4 or value.shape[0] != len(indices):
            raise RuntimeError(f'HAR 輸出尺寸不同：{value.shape}')
        shape = tuple(value.shape[1:])
        if shape in by_shape:
            raise RuntimeError(f'HAR 輸出尺寸重複：{shape}')
        by_shape[shape] = value

    comparisons = []
    for (name, shape), ref in zip(heads, reference):
        expected = np.concatenate(ref, axis=0)
        actual = by_shape[shape]
        if not np.isfinite(actual).all():
            raise RuntimeError(f'HAR 輸出包含非有限數值：{name}')
        error = np.abs(expected - actual)
        passed = bool(np.allclose(
            expected, actual, rtol=0.002, atol=0.002))
        comparisons.append({
            'node': name,
            'max_abs_error': float(error.max()),
            'mean_abs_error': float(error.mean()),
            'passed': passed
        })
        print(
            f"{name}：最大差={error.max():.6g}，"
            f"{'PASS' if passed else 'FAIL'}",
            flush=True
        )

    ok = all(r['passed'] for r in comparisons)
    save(check_path, {
        **identity,
        'passed': ok,
        'comparisons': comparisons,
        'rtol': 0.002,
        'atol': 0.002,
        'images': [entry['images'][i] for i in indices]
    })
    if not ok:
        raise SystemExit('解析數值比對未通過，停止在量化之前。')
    print(
        '\nPASS：ONNX 與原生 HAR 的六個分支數值比對通過。',
        flush=True
    )

else:
    checked = json.loads(check_path.read_text())
    if (not checked.get('passed')
            or any(checked.get(k) != v for k, v in identity.items())):
        raise SystemExit('請先完成同一模型與資料批次的 check。')

    script = base_script.read_text().rstrip() + '''
model_optimization_flavor(optimization_level=2, compression_level=0)
model_optimization_config(calibration, batch_size=1, calibset_size=1024)
post_quantization_optimization(finetune, policy=enabled, dataset_size=1024, epochs=4, batch_size=1)
model_optimization_config(checker_cfg, batch_size=1)
'''
    script_path = ROOT / 'model_config/yolov8n_quantize.alls'
    script_path.write_text(script)

    runner = ClientRunner(har=str(parsed), hw_arch='hailo8l')
    runner.load_model_script(script)
    start = time.monotonic()

    print('套用正規化與偵測後處理設定……', flush=True)
    runner.optimize_full_precision(calib_data=calib)
    runner.save_har(str(
        ROOT / 'artifacts/yolov8n_hailo8l.fp.har'))

    print('開始量化與量化感知微調，每批 1 張……', flush=True)
    runner.optimize(calib)

    output = ROOT / 'artifacts/yolov8n_hailo8l.quantized.har'
    temporary = output.with_name(
        'yolov8n_hailo8l.quantized.partial.har')
    runner.save_har(str(temporary))
    temporary.replace(output)

    save(ROOT / 'reports/quantize-report.json', {
        **identity,
        'passed': True,
        'stage': 'quantized',
        'hw_arch': 'hailo8l',
        'dfc_version': version('hailo-dataflow-compiler'),
        'model_script_sha256': digest(script_path),
        'calibration_sha256': entry['sha256'],
        'calibration_count': 1024,
        'batch_size': 1,
        'optimization_level': 2,
        'compression_level': 0,
        'finetune_epochs': 4,
        'har_path': str(output),
        'har_sha256': digest(output),
        'elapsed_seconds': round(time.monotonic() - start, 1),
        'accuracy_validated': False
    })
    print('\nPASS：量化 HAR 已建立。', flush=True)
    print('檔案：', output, flush=True)
    print('SHA256：', digest(output), flush=True)
    print('接下來使用另外保留的 128 張圖片檢查量化影響。', flush=True)
