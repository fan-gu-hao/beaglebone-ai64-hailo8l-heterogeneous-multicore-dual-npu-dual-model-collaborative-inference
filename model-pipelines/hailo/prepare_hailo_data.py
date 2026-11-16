from pathlib import Path, PurePosixPath
from importlib.metadata import version
import hashlib
import json
import os
import platform
import random
import shutil

ROOT = Path(__file__).resolve().parent
SESSION = '20261114_173839_933684'
DATA = ROOT / 'datasets' / SESSION
if platform.machine() != 'x86_64' or version('ultralytics') != '8.3.161':
    raise SystemExit('請使用 Ubuntu 筆電的 venv-export 環境。')
os.environ['YOLO_CONFIG_DIR'] = str(ROOT / 'settings')
os.environ['YOLO_AUTOINSTALL'] = 'false'

import cv2
import numpy as np
from ultralytics.data.augment import LetterBox

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b''):
            h.update(block)
    return h.hexdigest()

manifest = json.loads((DATA / 'manifest.json').read_text())
if manifest.get('complete') is not True or manifest['session'] != SESSION:
    raise SystemExit('圖片收集紀錄不完整或批次不同。')
rows = manifest['images']
seen = set()
for row in rows:
    p = PurePosixPath(row['file'])
    if (len(p.parts) != 2 or p.parts[0] != row['split']
            or row['split'] not in ('calibration', 'validation')
            or '..' in p.parts or p.suffix != '.jpg' or str(p) in seen):
        raise SystemExit('圖片清單格式或檔名重複檢查失敗。')
    seen.add(str(p))
if len({row['frame_sequence'] for row in rows}) != len(rows):
    raise SystemExit('發現重複的攝影機影像序號，停止。')
groups = {s: [r for r in rows if r['split'] == s]
          for s in ('calibration', 'validation')}
if [len(groups[s]) for s in groups] != [1024, 128]:
    raise SystemExit('應有 1024 張校正圖片與 128 張驗證圖片。')
actual = {str(p.relative_to(DATA)) for split in groups
          for p in (DATA / split).glob('*.jpg')}
if actual != seen:
    raise SystemExit('實際圖片與清單不一致，請重新執行傳輸指令。')
if shutil.disk_usage(DATA).free < 7 * 1024**3:
    raise SystemExit('製作輸入陣列需要至少 7 GiB 可用磁碟空間。')

random.Random(20261115).shuffle(groups['calibration'])
prepared = DATA / 'prepared'
prepared.mkdir(exist_ok=True)
letterbox = LetterBox(new_shape=(640, 640), auto=False, stride=32)
report = {
    'session': SESSION,
    'source_manifest_sha256': digest(DATA / 'manifest.json'),
    'preprocessing': 'LetterBox 640x640, padding=114, RGB, NHWC, float32, 0..255',
    'ultralytics': version('ultralytics'),
    'sets': {}
}

for split, items in groups.items():
    label = '校正圖片' if split == 'calibration' else '驗證圖片'
    target = prepared / f'{split}_rgb640_float32.npy'
    partial = target.with_suffix('.partial')
    data = np.lib.format.open_memmap(
        partial, mode='w+', dtype=np.float32,
        shape=(len(items), 640, 640, 3))
    low, high = 255, 0
    for i, row in enumerate(items):
        raw = (DATA / row['file']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise SystemExit(f"圖片校驗失敗：{row['file']}")
        bgr = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8),
                          cv2.IMREAD_COLOR)
        if bgr is None or bgr.shape != (480, 640, 3):
            raise SystemExit(f"圖片無法讀取或尺寸不同：{row['file']}")
        rgb = cv2.cvtColor(letterbox(image=bgr), cv2.COLOR_BGR2RGB)
        if rgb.shape != (640, 640, 3):
            raise SystemExit('補邊後的圖片尺寸不符。')
        data[i] = rgb
        low = min(low, int(rgb.min()))
        high = max(high, int(rgb.max()))
        if (i + 1) % 128 == 0:
            print(f'{label}：{i + 1}/{len(items)} 張', flush=True)
    data.flush()
    del data
    partial.replace(target)
    report['sets'][split] = {
        'path': str(target),
        'sha256': digest(target),
        'shape': [len(items), 640, 640, 3],
        'dtype': 'float32',
        'range': [low, high],
        'images': [r['file'] for r in items]
    }
    print(f'{label}完成：範圍 {low}～{high}；{target}', flush=True)

report['native_check_indices'] = [
    next(i for i, r in enumerate(groups['calibration'])
         if r['stage'] == stage)
    for stage in (1, 2, 3, 4)
]
report['passed'] = True
(ROOT / 'reports/calibration-report.json').write_text(
    json.dumps(report, ensure_ascii=False, indent=2) + '\n')
print('\nPASS：1152 張圖片校驗及前處理完成。')
print('校正陣列：(1024, 640, 640, 3)，float32，數值保留 0～255。')
print('驗證陣列：(128, 640, 640, 3)，另行保留。')
