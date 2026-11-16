from pathlib import Path
import hashlib
import json
import os
import re
import tempfile
import time
from check_tidl_pose_fp32 import (
    ROOT, TIDL, EXPECTED, config, run_tool, need)

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    previous = json.loads(
        (ROOT / 'reports/tidl-fp32-report.json').read_text())
    data = json.loads(
        (ROOT / 'reports/pose-data-report.json').read_text())
    model = ROOT / 'models/mobilenetv2_pose_256x192.fp32.onnx'

    need(previous.get('passed') is True,
         '請先完成 TI 浮點比對。')
    need(digest(model) == EXPECTED
         == previous['onnx_sha256'] == data['onnx_sha256'],
         '模型與先前通過檢查的版本不同。')

    calibration = data['splits']['calibration']
    raw = Path(calibration['raw'])
    need(calibration['count'] == 64
         and digest(raw) == calibration['sha256'],
         '校正資料數量或校驗值不符。')

    importer = TIDL / 'utils/tidlModelImport/out/tidl_model_import.out'
    simulator = TIDL / 'test/PC_dsp_test_dl_algo.out'
    compiler = TIDL / 'utils/perfsim/ti_cnnperfsim.out'
    device = TIDL / 'test/testvecs/config/import/device_config.cfg'

    for tool in (importer, simulator, compiler):
        need(tool.is_file() and os.access(tool, os.X_OK),
             f'找不到工具：{tool}')
    need(device.is_file(),
         f'找不到 TI 裝置編譯設定：{device}')

    work = Path(tempfile.mkdtemp(
        prefix='tidl-int8.', dir=ROOT / 'artifacts'))
    network = work / 'tidl_net_pose_int8.bin'
    io_prefix = work / 'tidl_io_pose_int8_'
    cfg = work / 'import.txt'

    config(cfg, {
        'modelType': 2,
        'inputNetFile': model,
        'outputNetFile': network,
        'outputParamsFile': io_prefix,
        'numParamBits': 8,
        'numFeatureBits': 8,
        'quantizationStyle': 2,
        'calibrationOption': 7,
        'numFrames': 64,
        'numFramesBiasCalibration': 64,
        'biasCalibrationIterations': 10,
        'executeNetworkCompiler': 1,
        'perfSimTool': compiler,
        'perfSimConfig': device,
        'tidlStatsTool': simulator,
        'inElementType': 0,
        'inDataFormat': 1,
        'inWidth': 192,
        'inHeight': 256,
        'inNumChannels': 3,
        'numBatches': 1,
        'inDataNorm': 1,
        'inMean': '123.675 116.28 103.53',
        'inScale': ' '.join(
            f'{1 / (255 * v):.12g}'
            for v in (.229, .224, .225)),
        'foldPreBnConv2D': 0,
        'inFileFormat': 1,
        'rawDataInElementType': 0,
        'inData': raw,
        'postProcType': 0,
    })

    print('本次結果目錄：', work, flush=True)
    print('8 位元量化；64 張校正資料；10 輪偏差校正；啟用硬體編譯。',
          flush=True)

    started = time.monotonic()
    log = work / 'build.log'
    run_tool(
        [str(importer), str(cfg)],
        TIDL / 'utils/tidlModelImport', log)

    text = log.read_text(errors='replace')
    need(
        'TIDL_E_DATAFLOW_INFO_NULL' not in text
        and 'This model can only be used on PC emulation' not in text,
        f'硬體編譯缺少資料流資訊，不能部署；請貼回紀錄：{log}')
    need(
        not re.search(r'\b[1-9]\d*\s+ERRORS?\b', text, re.I),
        f'TI 工具列出了錯誤；請貼回紀錄：{log}')
    need(network.is_file() and network.stat().st_size > 0,
         f'沒有產生網路檔：{network}')

    descriptors = sorted(work.glob(io_prefix.name + '*.bin'))
    need(len(descriptors) == 1
         and descriptors[0].stat().st_size > 0,
         f'輸入輸出描述檔不符：{descriptors}')

    report = {
        'stage': 'int8_build_completed_pending_validation',
        'onnx_sha256': EXPECTED,
        'network': str(network),
        'network_sha256': digest(network),
        'io_config': str(descriptors[0]),
        'io_sha256': digest(descriptors[0]),
        'build_directory': str(work),
        'dataset_directory': data['directory'],
        'calibration_sha256': calibration['sha256'],
        'config': str(cfg),
        'log': str(log),
        'elapsed_seconds': time.monotonic() - started,
        'quantized': True,
        'execute_network_compiler': True,
        'numerical_validation_passed': False,
        'ti_hardware_verified': False,
    }

    for path in (
            work / 'build-report.json',
            ROOT / 'reports/tidl-int8-build-report.json'):
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')

    print('\n=== TI 量化與編譯結果 ===', flush=True)
    print('網路與權重：', network, flush=True)
    print('輸入輸出描述：', descriptors[0], flush=True)
    print('網路 SHA256：', report['network_sha256'], flush=True)
    print(f'耗時：{report["elapsed_seconds"]:.1f} 秒', flush=True)
    print('PASS：8 位元量化與硬體編譯產物已建立。', flush=True)
    print('下一步：比較保留圖片的姿態輸出；這次尚未部署到板子。',
          flush=True)

if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        raise SystemExit(f'\n停止：{exc}')
