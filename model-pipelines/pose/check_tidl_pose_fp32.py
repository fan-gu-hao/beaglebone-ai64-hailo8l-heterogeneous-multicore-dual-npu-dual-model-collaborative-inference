from pathlib import Path
import hashlib
import json
import os
import platform
import subprocess
import tempfile
import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).resolve().parent
BASE = Path.home() / 'ti/bbai64-bringup'
SDK = BASE / 'sdk-08.00.00.12/ti-processor-sdk-rtos-j721e-evm-08_00_00_12'
TIDL = SDK / 'tidl_j7_08_00_00_10/ti_dl'
EXPECTED = 'dc2935b1ceaa6de68514d91ef5789e014c5087dfebba1cd6402cc50d42bbc635'

def need(ok, message):
    if not ok:
        raise RuntimeError(message)

def run_tool(args, cwd, log):
    print('\n執行：', args[0], flush=True)
    print('設定：', args[1], flush=True)
    with log.open('w', encoding='utf-8') as stream:
        process = subprocess.Popen(
            args, cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors='replace')
        try:
            for line in process.stdout:
                print(line, end='', flush=True)
                stream.write(line)
            code = process.wait()
        except BaseException:
            process.terminate()
            process.wait()
            raise
    need(code == 0, f'TI 工具回傳 {code}；紀錄：{log}')

def config(path, items):
    lines = []
    for key, value in items.items():
        if isinstance(value, Path):
            need(not any(c.isspace() for c in str(value)),
                 f'TI 工具路徑目前不可含空白：{value}')
            value = '"' + str(value) + '"'
        lines.append(f'{key} = {value}')
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')

def main():
    need(platform.machine() == 'x86_64', '請在 Ubuntu 筆電執行。')
    model = ROOT / 'models/mobilenetv2_pose_256x192.fp32.onnx'
    report = json.loads((ROOT / 'reports/export-report.json').read_text())
    actual = hashlib.sha256(model.read_bytes()).hexdigest()
    need(actual == EXPECTED == report['onnx_sha256'],
         '姿態 ONNX 校驗值不符。')

    importer = TIDL / 'utils/tidlModelImport/out/tidl_model_import.out'
    simulator = TIDL / 'test/PC_dsp_test_dl_algo.out'
    for tool in (importer, simulator):
        need(tool.is_file() and os.access(tool, os.X_OK),
             f'找不到可執行工具：{tool}')

    (ROOT / 'artifacts').mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(
        prefix='tidl-fp32.', dir=ROOT / 'artifacts'))
    print('本次結果目錄：', work, flush=True)
    print('模式：TI 浮點主機模擬；不進行量化、不操作板子。', flush=True)

    rng = np.random.default_rng(20261115)
    data = np.concatenate([
        np.full((1, 3, 256, 192), 128, dtype=np.uint8),
        rng.integers(0, 256, (1, 3, 256, 192), dtype=np.uint8),
        rng.integers(0, 256, (1, 3, 256, 192), dtype=np.uint8),
    ])
    raw = work / 'probe_rgb_chw_u8.bin'
    data.tofile(raw)

    mean = np.array(
        [0.485, 0.456, 0.406], np.float32)[None, :, None, None]
    std = np.array(
        [0.229, 0.224, 0.225], np.float32)[None, :, None, None]

    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(
        str(model), sess_options=options,
        providers=['CPUExecutionProvider'])

    reference = np.concatenate([
        session.run(
            ['heatmaps'],
            {'image': (sample.astype(np.float32) / 255 - mean) / std}
        )[0]
        for sample in np.split(data, 3)
    ])
    np.save(work / 'onnx_reference.npy', reference)
    del session

    network = work / 'tidl_net_pose_fp32.bin'
    io_prefix = work / 'tidl_io_pose_fp32_'
    import_cfg = work / 'import.txt'

    config(import_cfg, {
        'modelType': 2,
        'inputNetFile': model,
        'outputNetFile': network,
        'outputParamsFile': io_prefix,
        'numParamBits': 32,
        'executeNetworkCompiler': 0,
        'inElementType': 0,
        'inDataFormat': 1,
        'inWidth': 192,
        'inHeight': 256,
        'inNumChannels': 3,
        'numBatches': 1,
        'inDataNorm': 1,
        'inMean': '123.675 116.28 103.53',
        'inScale': ' '.join(
            f'{1 / (255 * v):.12g}' for v in (0.229, 0.224, 0.225)),
        'foldPreBnConv2D': 0,
        'inFileFormat': 1,
        'rawDataInElementType': 0,
        'inData': raw,
        'numFrames': 3,
        'postProcType': 0,
        'tidlStatsTool': simulator,
    })

    run_tool(
        [str(importer), str(import_cfg)],
        TIDL / 'utils/tidlModelImport', work / 'import.log')

    need(network.is_file() and network.stat().st_size > 0,
         f'TI 未產生網路檔，請貼回匯入紀錄：{work / "import.log"}')

    descriptors = sorted(work.glob(io_prefix.name + '*.bin'))
    need(len(descriptors) == 1,
         f'輸入輸出描述檔數量不符：{descriptors}；請貼回匯入紀錄。')

    output = work / 'tidl_heatmaps_fp32.bin'
    infer_cfg = work / 'infer.txt'

    config(infer_cfg, {
        'netBinFile': network,
        'ioConfigFile': descriptors[0],
        'inFileFormat': 1,
        'rawDataInElementType': 0,
        'inData': raw,
        'outData': output,
        'numFrames': 3,
        'postProcType': 0,
        'writeOutput': 2,
        'writeTraceLevel': 0,
        'debugTraceLevel': 0,
    })

    run_tool(
        [str(simulator), 's:' + str(infer_cfg), '--flowCtrl', '1'],
        TIDL / 'test', work / 'infer.log')

    need(output.is_file(),
         f'TI 沒有產生輸出檔；請貼回 {work / "infer.log"}。')
    need(output.stat().st_size == reference.size * 4,
         f'輸出大小不符：實際 {output.stat().st_size}，'
         f'預期 {reference.size * 4} 位元組。')

    tidl = np.fromfile(output, dtype='<f4').reshape(reference.shape)
    need(np.isfinite(tidl).all(), 'TI 輸出包含非有限數值。')
    np.save(work / 'tidl_heatmaps_fp32.npy', tidl)

    print('\n=== ONNX 與 TI 浮點輸出比較 ===', flush=True)
    tests = []
    for index, name in enumerate(
            ('中灰影像', '隨機影像一', '隨機影像二')):
        error = np.abs(tidl[index] - reference[index])
        passed = bool(np.allclose(
            tidl[index], reference[index], rtol=1e-3, atol=1e-4))
        tests.append({
            'name': name,
            'max_abs_error': float(error.max()),
            'mean_abs_error': float(error.mean()),
            'passed': passed,
        })
        print(
            f'{name}：最大差={error.max():.8g}，'
            f'平均差={error.mean():.8g}，'
            f'{"PASS" if passed else "FAIL"}',
            flush=True)

    summary = {
        'onnx_sha256': actual,
        'run_directory': str(work),
        'network': str(network),
        'io_config': str(descriptors[0]),
        'mode': 'host_fp32_reference',
        'input': 'RGB uint8 NCHW, normalization embedded in TIDL',
        'output_shape': list(tidl.shape),
        'tests': tests,
        'passed': all(t['passed'] for t in tests),
        'quantized': False,
        'ti_hardware_verified': False,
    }
    (work / 'result.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')

    need(summary['passed'],
         f'數值尚未對齊，停止後續轉換；結果保留在 {work}。')

    (ROOT / 'reports/tidl-fp32-report.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')

    print('\nPASS：TI 模型匯入與三組浮點數值比對完成。', flush=True)
    print('下一步：從既有實拍照片裁切人物，進行 TI 量化。', flush=True)

if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        raise SystemExit(f'\n停止：{exc}')
