from pathlib import Path
from importlib.metadata import version
import hashlib
import json
import os
import platform
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
HAILO = Path.home() / 'hailo-model-build/yolov8n-v1'
SOURCE = HAILO / 'datasets/20261114_173839_933684/prepared'
SPLITS = {'calibration': (1024, 64), 'validation': (128, 32)}
MODEL_SHA = 'dc2935b1ceaa6de68514d91ef5789e014c5087dfebba1cd6402cc50d42bbc635'
YOLO_SHA = 'f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36'

def need(ok, message):
    if not ok:
        raise RuntimeError(message)

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def save(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')
    temporary.replace(path)

def detect():
    need(version('ultralytics') == '8.3.161',
         '請使用既有 Hailo 的 venv-export。')
    os.environ['YOLO_CONFIG_DIR'] = str(HAILO / 'settings')
    os.environ['YOLO_AUTOINSTALL'] = 'false'

    import cv2
    import numpy as np
    import torch
    from ultralytics import YOLO

    weights = HAILO / 'models/yolov8n.pt'
    need(digest(weights) == YOLO_SHA, '人物偵測權重校驗不符。')
    torch.set_num_threads(4)
    detector = YOLO(str(weights))

    (ROOT / 'datasets').mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(
        prefix='pose-data.', dir=ROOT / 'datasets'))
    images = work / 'rgb640'
    images.mkdir()
    records, seen = [], set()
    sources = {}
    print('資料目錄：', work, flush=True)

    for split, (total, wanted) in SPLITS.items():
        title = '校正組' if split == 'calibration' else '驗證組'
        path = SOURCE / f'{split}_rgb640_float32.npy'
        data = np.load(path, mmap_mode='r', allow_pickle=False)
        need(data.shape == (total, 640, 640, 3)
             and data.dtype == np.float32,
             f'來源陣列規格不符：{path}，{data.shape}，{data.dtype}')

        sources[split] = str(path)
        count = 0
        order = np.random.default_rng(20261116).permutation(total)

        for scanned, index in enumerate(order, 1):
            frame = np.asarray(data[index])
            need(np.isfinite(frame).all()
                 and frame.min() >= 0 and frame.max() <= 255,
                 f'來源圖片數值異常：{title} 第 {index} 張')

            rgb = np.rint(frame).astype(np.uint8)
            frame_sha = hashlib.sha256(rgb.tobytes()).hexdigest()
            if frame_sha in seen:
                continue

            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            result = detector.predict(
                source=bgr, imgsz=640, conf=0.50, iou=0.70,
                classes=[0], device='cpu', verbose=False)[0]

            candidates = []
            for xyxy, score in zip(
                    result.boxes.xyxy.cpu().numpy(),
                    result.boxes.conf.cpu().numpy()):
                x1, y1, x2, y2 = np.clip(xyxy, 0, 640).tolist()
                if x2 - x1 >= 32 and y2 - y1 >= 64:
                    candidates.append((
                        (x2 - x1) * (y2 - y1),
                        [x1, y1, x2, y2], float(score)))

            if candidates:
                _, box, score = max(
                    candidates, key=lambda item: item[0])
                image = images / f'{split}_{count + 1:03d}.png'
                need(cv2.imwrite(str(image), bgr),
                     f'寫入失敗：{image}')
                records.append({
                    'split': split,
                    'source_index': int(index),
                    'source_frame_sha256': frame_sha,
                    'image': str(image),
                    'image_sha256': digest(image),
                    'bbox_xyxy': box,
                    'person_score': score,
                })
                seen.add(frame_sha)
                count += 1
                if count % 16 == 0:
                    print(
                        f'{title}：已選 {count}/{wanted} 張人物影像',
                        flush=True)
                if count == wanted:
                    break

            if scanned % 128 == 0:
                print(
                    f'{title}：已檢查 {scanned}/{total} 張',
                    flush=True)

        need(count == wanted,
             f'{title} 只有 {count} 張合適人物影像，'
             f'需要 {wanted} 張；停止。')

    detected = {
        'directory': str(work),
        'sources': sources,
        'coordinate_space': 'letterboxed RGB 640x640',
        'yolo_sha256': YOLO_SHA,
        'records': records,
    }
    save(work / 'detection.json', detected)
    save(ROOT / 'reports/pose-data-detected.json', detected)
    print('PASS：人物框選完成，接著使用姿態環境進行裁切。',
          flush=True)

def crop():
    need(version('mmpose') == '0.29.0',
         '請使用姿態模型的 venv-export。')

    import cv2
    import numpy as np
    import onnxruntime as ort
    from mmpose.datasets.pipelines import (
        TopDownGetBboxCenterScale, TopDownAffine)

    detected = json.loads(
        (ROOT / 'reports/pose-data-detected.json').read_text())
    work = Path(detected['directory'])
    model = ROOT / 'models/mobilenetv2_pose_256x192.fp32.onnx'
    need(digest(model) == MODEL_SHA, '姿態模型校驗不符。')

    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(
        str(model), sess_options=options,
        providers=['CPUExecutionProvider'])

    to_center = TopDownGetBboxCenterScale(padding=1.25)
    affine = TopDownAffine(use_udp=False)
    mean = np.array(
        [.485, .456, .406], np.float32)[None, :, None, None]
    std = np.array(
        [.229, .224, .225], np.float32)[None, :, None, None]
    tiles, references, outputs = [], [], {}

    for split, (_, wanted) in SPLITS.items():
        title = '校正組' if split == 'calibration' else '驗證組'
        records = [
            r for r in detected['records'] if r['split'] == split]
        need(len(records) == wanted, f'{title} 圖片數量不符。')

        raw = work / f'{split}_rgb_chw_u8.bin'
        crops = work / f'{split}_crops'
        crops.mkdir(exist_ok=True)

        with raw.open('wb') as stream:
            for index, record in enumerate(records, 1):
                image = Path(record['image'])
                need(digest(image) == record['image_sha256'],
                     f'圖片校驗不符：{image}')
                bgr = cv2.imread(str(image))
                need(bgr is not None, f'無法讀取圖片：{image}')
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                x1, y1, x2, y2 = record['bbox_xyxy']

                sample = {
                    'img': rgb,
                    'bbox': np.array(
                        [x1, y1, x2 - x1, y2 - y1], np.float32),
                    'rotation': 0,
                    'joints_3d': np.zeros((17, 3), np.float32),
                    'joints_3d_visible': np.zeros((17, 3), np.float32),
                    'ann_info': {
                        'image_size': np.array([192, 256]),
                        'num_joints': 17,
                    },
                }
                sample = affine(to_center(sample))
                patch = sample['img']
                need(patch.shape == (256, 192, 3)
                     and patch.dtype == np.uint8,
                     '人物裁切尺寸或型別不符。')

                chw = np.ascontiguousarray(
                    patch.transpose(2, 0, 1))
                stream.write(chw.tobytes())
                record['center'] = sample['center'].tolist()
                record['scale'] = sample['scale'].tolist()

                crop_image = crops / f'{index:03d}.png'
                crop_bgr = cv2.cvtColor(
                    patch, cv2.COLOR_RGB2BGR)
                need(cv2.imwrite(str(crop_image), crop_bgr),
                     '裁切圖片寫入失敗。')
                record['crop'] = str(crop_image)

                tile = np.full((150, 96, 3), 240, np.uint8)
                tile[:128] = cv2.resize(crop_bgr, (96, 128))
                label = (
                    'CAL' if split == 'calibration' else 'VAL'
                ) + f' {index:03d}'
                cv2.putText(
                    tile, label, (3, 144),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.38, (0, 0, 0), 1, cv2.LINE_AA)
                tiles.append(tile)

                if split == 'validation':
                    normalized = (
                        chw[None].astype(np.float32) / 255 - mean
                    ) / std
                    heatmaps = session.run(
                        ['heatmaps'], {'image': normalized})[0]
                    need(heatmaps.shape == (1, 17, 64, 48)
                         and np.isfinite(heatmaps).all(),
                         '姿態參考輸出異常。')
                    references.append(heatmaps[0])

                if index % 16 == 0:
                    print(
                        f'{title}：已裁切 {index}/{wanted} 張',
                        flush=True)

        need(raw.stat().st_size == wanted * 3 * 256 * 192,
             '原始輸入檔大小不符。')
        outputs[split] = {
            'raw': str(raw),
            'sha256': digest(raw),
            'count': wanted,
        }

    reference_path = work / 'validation_onnx_heatmaps.npy'
    np.save(reference_path, np.stack(references))

    preview = ROOT / 'reports/pose-data-preview.jpg'
    grid = np.vstack([
        np.hstack(tiles[i:i + 8])
        for i in range(0, len(tiles), 8)
    ])
    need(cv2.imwrite(str(preview), grid), '預覽圖寫入失敗。')

    report = {
        **detected,
        'onnx_sha256': MODEL_SHA,
        'splits': outputs,
        'reference_heatmaps': str(reference_path),
        'preview': str(preview),
        'input_shape': [1, 3, 256, 192],
        'raw_input': 'RGB uint8 CHW, 0..255, not normalized',
        'normalization_in_tidl': {
            'mean': [123.675, 116.28, 103.53],
            'scale': [
                1 / (255 * v) for v in (.229, .224, .225)],
        },
        'padding': 1.25,
        'use_udp': False,
    }
    save(work / 'dataset.json', report)
    save(ROOT / 'reports/pose-data-report.json', report)

    print('\nPASS：64 張校正人物、32 張獨立驗證人物準備完成。',
          flush=True)
    print('TI 輸入：RGB、CHW、uint8，保留 0～255；正規化交給 TI。',
          flush=True)
    print('預覽：', preview, flush=True)

if __name__ == '__main__':
    try:
        need(platform.machine() == 'x86_64',
             '請在 Ubuntu 筆電執行。')
        need(len(sys.argv) == 2
             and sys.argv[1] in ('detect', 'crop'),
             '參數必須是 detect 或 crop。')
        {'detect': detect, 'crop': crop}[sys.argv[1]]()
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        raise SystemExit(f'\n停止：{exc}')
