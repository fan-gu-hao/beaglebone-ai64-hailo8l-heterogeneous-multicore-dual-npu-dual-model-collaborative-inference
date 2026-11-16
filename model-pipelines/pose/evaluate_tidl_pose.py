from pathlib import Path
import hashlib
import json
import shutil
import tempfile
import numpy as np
import cv2
from mmpose.core.evaluation import keypoints_from_heatmaps
from check_tidl_pose_fp32 import (
    ROOT, TIDL, EXPECTED, config, run_tool, need
)

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def save(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2,
                   allow_nan=False) + '\n',
        encoding='utf-8'
    )

def main():
    build_path = ROOT / 'reports/tidl-int8-build-report.json'
    need(build_path.is_file(),
         '量化尚未成功完成，請等目前的編譯程序結束。')

    build = json.loads(build_path.read_text())
    data = json.loads(
        (ROOT / 'reports/pose-data-report.json').read_text()
    )

    need(build['onnx_sha256'] == data['onnx_sha256'] == EXPECTED,
         '模型版本不符。')
    need(
        build['calibration_sha256']
        == data['splits']['calibration']['sha256'],
        '資料集與本次編譯使用的版本不同。'
    )

    net = Path(build['network'])
    io = Path(build['io_config'])
    need(
        digest(net) == build['network_sha256']
        and digest(io) == build['io_sha256'],
        '編譯產物校驗失敗。'
    )

    val = data['splits']['validation']
    raw = Path(val['raw'])
    need(
        val['count'] == 32 and digest(raw) == val['sha256'],
        '驗證資料校驗失敗。'
    )

    reference = np.load(
        data['reference_heatmaps'], allow_pickle=False
    )
    need(
        reference.shape == (32, 17, 64, 48)
        and np.isfinite(reference).all(),
        '浮點參考輸出不符。'
    )

    records = [
        r for r in data['records']
        if r['split'] == 'validation'
    ]
    need(len(records) == 32, '驗證圖片紀錄數量不符。')

    out = Path(tempfile.mkdtemp(
        prefix='pose-validation.', dir=ROOT / 'reports'
    ))
    output = out / 'heatmaps_int8_dequantized.bin'
    cfg = out / 'infer.txt'

    config(cfg, {
        'netBinFile': net,
        'ioConfigFile': io,
        'inFileFormat': 1,
        'rawDataInElementType': 0,
        'inData': raw,
        'outData': output,
        'numFrames': 32,
        'postProcType': 0,
        'writeOutput': 2,
        'writeTraceLevel': 0,
        'debugTraceLevel': 0,
    })

    print('開始執行 32 張量化模型主機模擬。', flush=True)
    run_tool(
        [str(TIDL / 'test/PC_dsp_test_dl_algo.out'),
         's:' + str(cfg)],
        TIDL / 'test',
        out / 'infer.log'
    )

    need(
        output.is_file()
        and output.stat().st_size == reference.size * 4,
        f'TI 輸出檔大小不符；紀錄：{out / "infer.log"}'
    )
    quantized = np.fromfile(
        output, dtype='<f4'
    ).reshape(reference.shape)
    need(
        np.isfinite(quantized).all(),
        '量化輸出包含非有限數值。'
    )
    np.save(out / 'heatmaps_int8_dequantized.npy', quantized)

    center = np.tile(
        np.array([[96., 128.]], np.float32), (32, 1)
    )
    scale = np.tile(
        np.array([[192 / 200, 256 / 200]], np.float32),
        (32, 1)
    )

    def decode(heatmaps):
        xy, score = keypoints_from_heatmaps(
            heatmaps, center, scale,
            post_process='default', kernel=11, use_udp=False
        )
        return xy, score[:, :, 0]

    fp_xy, fp_score = decode(reference)
    q_xy, q_score = decode(quantized)
    distance = np.linalg.norm(fp_xy - q_xy, axis=2)
    mask = fp_score >= 0.20

    groups = {
        '全部關節': list(range(17)),
        '肩肘腕': list(range(5, 11)),
        '手腕': [9, 10],
    }
    metrics = {}

    print('\n=== 量化前後的關節比較 ===', flush=True)
    print(
        '位移單位：192×256 人物裁切圖上的像素；'
        '這不是標註準確率。',
        flush=True
    )

    for name, ids in groups.items():
        selected = mask[:, ids]
        count = int(selected.sum())
        valid = selected & (q_score[:, ids] > 0)
        errors = distance[:, ids][valid]
        retained = int(
            (selected & (q_score[:, ids] >= 0.20)).sum()
        )
        within = int(
            (valid & (distance[:, ids] <= 8)).sum()
        )

        item = {
            'reference_count': count,
            'quantized_confident_count': retained,
            'within_8px_count': within,
            'mean_shift_px':
                float(errors.mean()) if errors.size else None,
            'p90_shift_px':
                float(np.percentile(errors, 90))
                if errors.size else None,
            'max_shift_px':
                float(errors.max()) if errors.size else None,
        }
        metrics[name] = item

        if count and errors.size:
            print(
                f'{name}：參考關節 {count} 個｜'
                f'量化保留 {retained}/{count}｜'
                f'平均位移 {item["mean_shift_px"]:.2f}｜'
                f'90% 位移 ≤ {item["p90_shift_px"]:.2f}｜'
                f'8 像素內 {within}/{count}',
                flush=True
            )
        else:
            print(
                f'{name}：參考關節 {count} 個，'
                '沒有足夠配對可計算位移。',
                flush=True
            )

    links = [
        (0,1), (0,2), (1,3), (2,4),
        (5,6), (5,7), (7,9), (6,8), (8,10),
        (5,11), (6,12), (11,12),
        (11,13), (13,15), (12,14), (14,16),
    ]

    def draw(image, xy, score, color):
        image = image.copy()
        visible = (
            (score >= .20)
            & (xy[:, 0] >= 0) & (xy[:, 0] < 192)
            & (xy[:, 1] >= 0) & (xy[:, 1] < 256)
        )
        pts = np.rint(xy).astype(int)

        for a, b in links:
            if visible[a] and visible[b]:
                cv2.line(
                    image, tuple(pts[a]), tuple(pts[b]),
                    color, 2
                )
        for j in range(17):
            if visible[j]:
                cv2.circle(
                    image, tuple(pts[j]), 3, color, -1
                )
        return image

    cards = []
    for i, record in enumerate(records):
        image = cv2.imread(record['crop'])
        need(
            image is not None
            and image.shape[:2] == (256, 192),
            '裁切圖片讀取失敗。'
        )

        pair = np.hstack([
            draw(image, fp_xy[i], fp_score[i], (0,220,0)),
            draw(image, q_xy[i], q_score[i], (0,180,255)),
        ])

        name = f'compare_{i + 1:03d}.jpg'
        need(
            cv2.imwrite(str(out / name), pair),
            '對照圖片寫入失敗。'
        )
        cards.append(
            f'<section><h3>第 {i+1} 張：'
            '左邊浮點，右邊量化</h3>'
            f'<img src="{name}" width="768"></section>'
        )

    page = out / 'index.html'
    page.write_text(
        '<!doctype html><meta charset="utf-8">'
        '<title>TI 姿態量化比較</title>'
        '<style>'
        'body{background:#172132;color:#eee;'
        'font:20px sans-serif;padding:24px}'
        'img{max-width:100%}section{margin:32px 0}'
        '</style>'
        '<h1>TI 姿態量化比較</h1>'
        '<p>綠色：浮點模型；橘色：量化模型。'
        '兩邊都只畫熱圖峰值至少 0.20 的關節。</p>'
        '<p>比較的是模型轉換前後的一致性，'
        '沒有使用人工關節標註。</p>'
        + ''.join(cards),
        encoding='utf-8'
    )

    report = {
        'network_sha256': build['network_sha256'],
        'inference_completed': True,
        'metrics': metrics,
        'preview': str(page),
        'directory': str(out),
        'quality_accepted': False,
        'ti_hardware_verified': False,
    }
    save(out / 'report.json', report)
    save(ROOT / 'reports/pose-validation-report.json', report)

    (ROOT / 'deployment').mkdir(exist_ok=True)
    bundle = Path(tempfile.mkdtemp(
        prefix='pose-mobilenetv2-',
        dir=ROOT / 'deployment'
    ))

    shutil.copy2(net, bundle / 'network.bin')
    shutil.copy2(io, bundle / 'io.bin')

    with raw.open('rb') as stream:
        (bundle / 'test_input_rgb_chw_u8.bin').write_bytes(
            stream.read(3 * 256 * 192)
        )

    quantized[0].astype('<f4').tofile(
        bundle / 'expected_int8_output_f32.bin'
    )
    reference[0].astype('<f4').tofile(
        bundle / 'reference_onnx_output_f32.bin'
    )

    save(bundle / 'preprocess.json', {
        'shape': [1,3,256,192],
        'color': 'RGB',
        'layout': 'NCHW',
        'dtype': 'uint8',
        'normalization_location': 'inside TIDL network',
        'normalization': data['normalization_in_tidl'],
        'crop_source': data['coordinate_space'],
        'padding': 1.25,
        'use_udp': False,
        'output_shape': [1,17,64,48],
        'post_process': 'default',
    })
    save(bundle / 'validation.json', report)
    shutil.copy2(build_path, bundle / 'build-report.json')

    files = {
        p.name: digest(p)
        for p in sorted(bundle.iterdir())
        if p.is_file()
    }
    save(bundle / 'manifest.json', {
        'target': 'J721E C71x/MMA',
        'tidl_version': '08_00_00_10',
        'onnx_sha256': EXPECTED,
        'network_sha256': build['network_sha256'],
        'files': files,
        'ti_hardware_verified': False,
    })
    save(ROOT / 'reports/pose-package.json', {
        'directory': str(bundle),
        'name': bundle.name,
    })

    print(
        '\nPASS：32 張推論、骨架對照頁與板端測試資料已建立。',
        flush=True
    )
    print('對照頁：', page, flush=True)
    print('待部署資料夾：', bundle, flush=True)
    print(
        '模型品質仍需依比較結果判讀；'
        '目前尚未在板子上執行。',
        flush=True
    )

if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        raise SystemExit(f'\n停止：{exc}')
