from pathlib import Path
from importlib.metadata import version
import hashlib
import json
import platform
import time

ROOT = Path(__file__).resolve().parent
EXPECTED = '23fe4fa0d4647f3b7a5da171d38b7702eb3fc17763beac869b5c595d9dabcaa6'

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b''):
            h.update(block)
    return h.hexdigest()

if platform.machine() != 'x86_64':
    raise SystemExit('請在 Ubuntu 筆電執行。')
if version('hailo-dataflow-compiler') != '3.33.1':
    raise SystemExit('請使用現有的 DFC 3.33.1 環境。')

source = ROOT / 'artifacts/yolov8n_hailo8l.quantized.har'
info = json.loads((ROOT / 'reports/quantize-report.json').read_text())

if digest(source) != EXPECTED or info['har_sha256'] != EXPECTED:
    raise SystemExit('量化 HAR 與剛才測試的版本不同。')
if info.get('hw_arch') != 'hailo8l':
    raise SystemExit('量化紀錄的硬體目標不是 Hailo-8L。')

from hailo_sdk_client import ClientRunner

hef_path = ROOT / 'artifacts/yolov8n_custom_hailo8l.hef'
compiled_har = ROOT / 'artifacts/yolov8n_custom_hailo8l_compiled_model.har'

print('來源：', source, flush=True)
print('目標：Hailo-8L', flush=True)
print('開始編譯，請保持終端機開啟……', flush=True)
started = time.monotonic()

runner = ClientRunner(har=str(source), hw_arch='hailo8l')
hef = runner.compile()

if not isinstance(hef, (bytes, bytearray, memoryview)) or not len(hef):
    raise RuntimeError('編譯器未回傳有效的 HEF 二進位內容。')

temporary = hef_path.with_suffix('.partial.hef')
temporary.write_bytes(hef)
temporary.replace(hef_path)
print('HEF 已寫入：', hef_path, flush=True)

runner.save_har(str(compiled_har))

report = {
    'stage': 'compiled',
    'hw_arch': 'hailo8l',
    'dfc_version': version('hailo-dataflow-compiler'),
    'source_quantized_har_sha256': EXPECTED,
    'source_onnx_sha256': info.get('onnx_sha256'),
    'hef_path': str(hef_path),
    'hef_sha256': digest(hef_path),
    'hef_bytes': hef_path.stat().st_size,
    'compiled_har_path': str(compiled_har),
    'compiled_har_sha256': digest(compiled_har),
    'elapsed_seconds': round(time.monotonic() - started, 1),
    'hardware_verified': False,
}

report_path = ROOT / 'reports/compile-report.json'
report_path.write_text(
    json.dumps(report, ensure_ascii=False, indent=2) + '\n',
    encoding='utf-8'
)

print('\nPASS：HEF 編譯完成；接下來進行板端驗證。', flush=True)
print('檔案：', hef_path)
print('大小：', round(report['hef_bytes'] / 1024**2, 2), 'MiB')
print('SHA256：', report['hef_sha256'])
print('耗時：', report['elapsed_seconds'], '秒')
