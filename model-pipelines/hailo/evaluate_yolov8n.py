from pathlib import Path
from importlib.metadata import version
import hashlib
import json
import platform
import sys
import numpy as np

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'reports/validation-v1'
EXPECTED = '23fe4fa0d4647f3b7a5da171d38b7702eb3fc17763beac869b5c595d9dabcaa6'
LABELS = {0: '人', 39: '瓶子', 41: '杯子', 67: '手機'}
SCORE = 0.35
IOU = 0.50

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b''):
            h.update(block)
    return h.hexdigest()

def save(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2,
                   allow_nan=False) + '\n',
        encoding='utf-8'
    )

def unpack(result, count):
    if isinstance(result, dict):
        if len(result) != 1:
            raise RuntimeError(
                f'預期只有一個後處理輸出，實際：{list(result)}')
        result = next(iter(result.values()))
    if isinstance(result, (list, tuple)) and len(result) == 1:
        result = result[0]
    array = np.asarray(result)
    if array.shape != (count, 80, 5, 100):
        raise RuntimeError(f'後處理輸出尺寸不同：{array.shape}')
    if not np.isfinite(array).all():
        raise RuntimeError('輸出包含非有限數值。')
    scores = array[:, :, 4, :]
    if scores.min() < -0.0001 or scores.max() > 1.0001:
        raise RuntimeError('信心分數超出 0～1，停止解讀輸出。')
    return array.astype(np.float32, copy=False)

def decode(frame):
    boxes = []
    for cls in LABELS:
        for row in frame[cls].T:
            if row[4] < SCORE:
                continue
            if row[:4].min() < -2 or row[:4].max() > 3:
                raise RuntimeError('偵測框座標不符合預期的正規化範圍。')
            y1, x1, y2, x2 = np.clip(row[:4], 0, 1).tolist()
            if y2 <= y1 or x2 <= x1:
                continue
            boxes.append({
                'class_id': cls,
                'name': LABELS[cls],
                'score': float(row[4]),
                'box': [y1, x1, y2, x2],
                'matched': False
            })
    return boxes

def overlap(a, b):
    y1, x1 = max(a[0], b[0]), max(a[1], b[1])
    y2, x2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, y2-y1) * max(0, x2-x1)
    union = (
        (a[2]-a[0])*(a[3]-a[1])
        + (b[2]-b[0])*(b[3]-b[1]) - inter
    )
    return inter / union if union > 0 else 0.0

def match_boxes(left, right):
    from scipy.optimize import linear_sum_assignment
    pairs = []
    for cls in LABELS:
        a = [i for i, box in enumerate(left)
             if box['class_id'] == cls]
        b = [i for i, box in enumerate(right)
             if box['class_id'] == cls]
        if not a or not b:
            continue
        matrix = np.array([
            [overlap(left[i]['box'], right[j]['box']) for j in b]
            for i in a
        ])
        reward = np.where(
            matrix >= IOU, min(len(a), len(b)) + 1 + matrix, 0)
        rr, cc = linear_sum_assignment(-reward)
        for r, c in zip(rr, cc):
            if matrix[r, c] >= IOU:
                i, j = a[r], b[c]
                left[i]['matched'] = right[j]['matched'] = True
                pairs.append({
                    'class_id': cls,
                    'iou': float(matrix[r, c]),
                    'score_delta': abs(
                        left[i]['score'] - right[j]['score'])
                })
    return pairs

PAGE = r'''<!doctype html><html lang="zh-Hant"><meta charset="utf-8">
<title>量化前後對照</title><style>
body{font:18px sans-serif;background:#111c2b;color:#edf3fa;margin:24px}
button,select{font:inherit;padding:8px;margin:4px;max-width:100%}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
canvas{width:100%;max-width:640px;background:#222}
p{line-height:1.6}.note{color:#ffce80}
@media(max-width:800px){.grid{grid-template-columns:1fr}}
</style><h1>量化前後對照</h1>
<p>信心門檻 35%；同類別、交併比（IoU）至少 0.5 視為對應。<br>
綠色框：找到對應；橘色框：未找到對應。預設先顯示差異較大的照片。</p>
<button id="prev">上一張</button><button id="next">下一張</button>
<select id="pick"></select><p id="info" class="note"></p>
<div class="grid">
<section><h2>浮點模型</h2>
<canvas id="fp" width="640" height="640"></canvas></section>
<section><h2>量化模型</h2>
<canvas id="q" width="640" height="640"></canvas></section></div>
<p>這是相對浮點模型的一致性比較，請同時查看圖片中實際存在的物品。</p>
<script>
const rows=__DATA__, pick=document.getElementById('pick');
rows.forEach((r,i)=>{
  const o=document.createElement('option');o.value=i;
  o.textContent=`照片 ${r.index+1}｜浮點 ${r.fp.length} 框｜量化 ${r.quantized.length} 框｜未配對 ${r.unmatched} 框`;
  pick.appendChild(o);
});
function draw(id,img,boxes){
  const c=document.getElementById(id),ctx=c.getContext('2d');
  ctx.clearRect(0,0,640,640);ctx.drawImage(img,0,0,640,640);
  ctx.lineWidth=3;ctx.font='18px sans-serif';
  for(const b of boxes){
    const [y1,x1,y2,x2]=b.box.map(v=>v*640);
    ctx.strokeStyle=b.matched?'#39ed9a':'#ffad43';
    ctx.strokeRect(x1,y1,x2-x1,y2-y1);
    const text=b.name+' '+Math.round(b.score*100)+'%';
    const y=Math.max(22,y1);
    ctx.fillStyle='#000c';
    ctx.fillRect(x1,y-21,ctx.measureText(text).width+8,24);
    ctx.fillStyle=ctx.strokeStyle;ctx.fillText(text,x1+4,y-2);
  }
}
let generation=0;
function show(){
  const r=rows[Number(pick.value)], token=++generation;
  document.getElementById('info').textContent=r.file;
  const img=new Image();
  img.onload=()=>{
    if(token!==generation)return;
    draw('fp',img,r.fp);draw('q',img,r.quantized);
  };
  img.src=r.image;
}
pick.onchange=show;
document.getElementById('prev').onclick=()=>{
  pick.selectedIndex=(pick.selectedIndex+rows.length-1)%rows.length;
  show();
};
document.getElementById('next').onclick=()=>{
  pick.selectedIndex=(pick.selectedIndex+1)%rows.length;
  show();
};
show();
</script></html>'''

def make_report(fp, quantized, images, names, out, identity):
    from PIL import Image
    (out / 'images').mkdir(exist_ok=True)
    stats = {
        c: {'fp': 0, 'quantized': 0, 'matched': 0,
            'ious': [], 'score_deltas': []}
        for c in LABELS
    }
    frames = []
    same = empty = 0
    for i in range(len(images)):
        a, b = decode(fp[i]), decode(quantized[i])
        pairs = match_boxes(a, b)
        missing = len(a) - len(pairs)
        added = len(b) - len(pairs)
        same += int(missing == 0 and added == 0)
        empty += int(not a and not b)
        for cls in LABELS:
            s = stats[cls]
            s['fp'] += sum(d['class_id'] == cls for d in a)
            s['quantized'] += sum(d['class_id'] == cls for d in b)
            for p in pairs:
                if p['class_id'] == cls:
                    s['matched'] += 1
                    s['ious'].append(p['iou'])
                    s['score_deltas'].append(p['score_delta'])
        image_path = f'images/{i:03d}.jpg'
        Image.fromarray(
            np.clip(images[i], 0, 255).astype(np.uint8)
        ).save(out / image_path, quality=95)
        frames.append({
            'index': i, 'file': names[i], 'image': image_path,
            'fp': a, 'quantized': b,
            'unmatched': missing + added,
            'mean_iou': (
                float(np.mean([p['iou'] for p in pairs]))
                if pairs else None),
            'score_delta': (
                float(np.mean([p['score_delta'] for p in pairs]))
                if pairs else None)
        })

    table = []
    def percent(value):
        return '—' if value is None else f'{value*100:.1f}%'

    print('\n=== 四類物品的量化前後比較 ===')
    for cls, s in stats.items():
        retained = s['matched'] / s['fp'] if s['fp'] else None
        corresponding = (
            s['matched'] / s['quantized']
            if s['quantized'] else None)
        mean_iou = float(np.mean(s['ious'])) if s['ious'] else None
        row = {
            'class_id': cls, 'name': LABELS[cls],
            'fp': s['fp'], 'quantized': s['quantized'],
            'matched': s['matched'],
            'fp_box_retained_fraction': retained,
            'quantized_box_corresponding_fraction': corresponding,
            'mean_matched_iou': mean_iou,
            'mean_abs_score_delta': (
                float(np.mean(s['score_deltas']))
                if s['score_deltas'] else None)
        }
        table.append(row)
        iou_text = '無配對' if mean_iou is None else f'{mean_iou:.3f}'
        print(
            f"{LABELS[cls]}：浮點框={s['fp']}，"
            f"量化框={s['quantized']}，配對={s['matched']}，"
            f'保留率={percent(retained)}，'
            f'對應率={percent(corresponding)}，平均 IoU={iou_text}'
        )

    report = {
        **identity,
        'comparison_completed': True,
        'ground_truth_available': False,
        'confidence_threshold': SCORE,
        'match_iou_threshold': IOU,
        'validation_count': len(images),
        'per_class': table,
        'fully_matched_frames': same,
        'both_empty_frames': empty,
        'frames_with_any_detection': len(images)-empty,
        'fully_matched_nonempty_frames': same-empty
    }
    save(out / 'summary.json', report)
    save(out / 'detections.json', frames)
    ordered = sorted(
        frames,
        key=lambda r: (
            r['unmatched'],
            r['score_delta'] or 0,
            1-r['mean_iou'] if r['mean_iou'] is not None else 0,
            bool(r['fp'] or r['quantized'])
        ),
        reverse=True
    )
    payload = json.dumps(
        ordered, ensure_ascii=False, allow_nan=False
    ).replace('<', '\\u003c')
    (out / 'index.html').write_text(
        PAGE.replace('__DATA__', payload), encoding='utf-8')
    print(
        f'有偵測框的照片：{len(images)-empty}/{len(images)}；'
        f'其中全部配對：{same-empty} 張。')
    print(f'兩個模型都沒有上述四類偵測框：{empty} 張。')
    print('PASS：比較報告與對照頁已建立；結果尚待判讀。')
    print('對照頁：', out / 'index.html')

def main():
    if (platform.machine() != 'x86_64'
            or version('hailo-dataflow-compiler') != '3.33.1'):
        raise SystemExit('請在 Ubuntu 筆電使用 dfc-python 執行。')
    if (len(sys.argv) != 2
            or sys.argv[1] not in ('fp', 'quantized', 'report')):
        raise SystemExit('參數必須是 fp、quantized 或 report。')
    mode = sys.argv[1]
    OUT.mkdir(parents=True, exist_ok=True)

    prep_path = ROOT / 'reports/calibration-report.json'
    prep = json.loads(prep_path.read_text())
    info = json.loads(
        (ROOT / 'reports/quantize-report.json').read_text())
    if (info['har_sha256'] != EXPECTED
            or digest(prep_path) != info['preparation_report_sha256']):
        raise SystemExit('量化或前處理紀錄與這次的檔案不同。')

    models = {
        'fp': ROOT / 'artifacts/yolov8n_hailo8l.fp.har',
        'quantized': ROOT / 'artifacts/yolov8n_hailo8l.quantized.har'
    }
    model_hashes = {k: digest(p) for k, p in models.items()}
    if model_hashes['quantized'] != EXPECTED:
        raise SystemExit('量化 HAR 校驗失敗。')

    entry = prep['sets']['validation']
    data_path = Path(entry['path'])
    if digest(data_path) != entry['sha256']:
        raise SystemExit('驗證陣列校驗失敗。')
    data = np.load(data_path, mmap_mode='r', allow_pickle=False)
    if data.shape != (128, 640, 640, 3) or data.dtype != np.float32:
        raise SystemExit('驗證陣列尺寸或型別不同。')
    identity = {
        'model_sha256': model_hashes,
        'validation_sha256': entry['sha256']
    }

    if mode != 'report':
        import tensorflow as tf
        from hailo_sdk_client import ClientRunner, InferenceContext
        if not tf.config.list_physical_devices('GPU'):
            raise SystemExit('TensorFlow 未偵測到 GPU。')
        tf.config.experimental.enable_tensor_float_32_execution(False)
        context = (
            InferenceContext.SDK_FP_OPTIMIZED if mode == 'fp'
            else InferenceContext.SDK_QUANTIZED
        )
        print(
            '開始推論：',
            '浮點模型' if mode == 'fp' else '量化模型',
            '，128 張，每批 1 張。',
            flush=True
        )
        runner = ClientRunner(
            har=str(models[mode]), hw_arch='hailo8l')
        with runner.infer_context(context) as ctx:
            result = runner.infer(
                ctx, np.asarray(data), batch_size=1)
        result = unpack(result, len(data))
        target = OUT / f'{mode}_nms.npy'
        partial = OUT / f'{mode}_nms.partial.npy'
        np.save(partial, result, allow_pickle=False)
        partial.replace(target)
        save(OUT / f'{mode}.json', {
            **identity,
            'output_sha256': digest(target)
        })
        print(
            f'PASS：{mode} 完成，輸出尺寸={result.shape}。',
            flush=True
        )
    else:
        arrays = {}
        for key in models:
            meta = json.loads((OUT / f'{key}.json').read_text())
            path = OUT / f'{key}_nms.npy'
            if (any(meta.get(k) != v for k, v in identity.items())
                    or digest(path) != meta['output_sha256']):
                raise SystemExit(
                    f'推論結果與目前模型或資料不符：{key}')
            arrays[key] = unpack(
                np.load(path, allow_pickle=False), len(data))
        make_report(
            arrays['fp'], arrays['quantized'],
            data, entry['images'], OUT, identity
        )

if __name__ == '__main__':
    main()
