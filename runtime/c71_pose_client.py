from service_support import open_log, LOG_DIR, CAMERA, worker_stderr, sensor_signals, take_zero
from pathlib import Path
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import select
import shutil
import struct
import subprocess
import time
import zlib
import numpy as np
ROOT = Path('/home/debian/pose_demo')
DT = Path('/sys/firmware/devicetree/base')
PROTOCOL = 'C71_TIDL_POSE_V2'
INPUT_BYTES = 3 * 256 * 192
OUTPUT_BYTES = 17 * 64 * 48

def need(ok, message):
    if not ok:
        raise RuntimeError(message)

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def read(path):
    return path.read_bytes().decode(errors='replace').strip('\x00 \t\r\n')

def check_memory(core):
    ddr = DT / 'reserved-memory/c71-pose-memory@ac000000'
    sram = DT / 'bus@100000/sram@70000000'
    child = sram / 'c71-pose-sram@20000'
    need((ddr / 'reg').read_bytes() == struct.pack('>4I', 0, 2885681152, 0, 134217728) and (ddr / 'no-map').exists(), 'DDR 保留設定不符。')
    refs = (core / 'device/of_node/memory-region').read_bytes()
    need(len(refs) == 12 and refs[-4:] == (ddr / 'phandle').read_bytes(), 'C71 沒有連到新增的 DDR 保留區。')
    need((sram / 'reg').read_bytes() == struct.pack('>4I', 0, 1879048192, 0, 8323072) and (child / 'reg').read_bytes() == struct.pack('>2I', 131072, 8159232), '片上記憶體保留設定不符。')
    need(not any(((child / name).exists() for name in ('pool', 'export', 'protect-exec'))), '片上記憶體仍可被其他程式配置。')
    need(Path('/sys/bus/platform/devices/70000000.sram/driver').exists(), '片上記憶體驅動未綁定。')
    ram = [(int(m[1], 16), int(m[2], 16) + 1) for m in re.finditer('^\\s*([0-9a-f]+)-([0-9a-f]+) : System RAM\\s*$', Path('/proc/iomem').read_text(), re.M)]
    need(ram and any((a > 0 for a, b in ram)), '讀不到實際記憶體登記。')
    need(all((max(a, 2885681152) >= min(b, 3019898880) for a, b in ram)), '新增 DDR 仍屬於 Linux 一般記憶體。')

class PoseClient:

    def __init__(self):
        self.fd = None
        self.created = False
        self.guard = None
        self.core = None
        self.serial = time.monotonic_ns() & 4294967295
        need(os.geteuid() == 0, '請加 sudo 執行。')
        need('BeagleBone AI-64' in read(DT / 'model'), '請在 BeagleBone AI-64 執行。')
        spec = importlib.util.spec_from_file_location('pose_v1_support', '/home/debian/c71_pose_selftest.py')
        self.support = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.support)
        try:
            self.guard = (ROOT / '.c71-selftest.lock').open('a')
            fcntl.flock(self.guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.report = json.loads(Path('/home/debian/c71-pose-v2-build.json').read_text())
            firmware = Path('/home/debian/c71-pose-v2.xe71')
            need(self.report.get('build_passed') is True and self.report.get('protocol') == PROTOCOL and (sha(firmware) == self.report['sha256']), '第二版韌體或建置紀錄不符。')
            cores = []
            for p in Path('/sys/class/remoteproc').glob('remoteproc*'):
                name = read(p / 'name')
                if name == '64800000.dsp':
                    cores.append(p)
                if name in ('4d80800000.dsp', '4d81800000.dsp'):
                    need(read(p / 'state') == 'offline', 'C66 正在使用共享資源。')
            need(len(cores) == 1, '找不到唯一的 C71。')
            self.core = cores[0]
            check_memory(self.core)
            controller = (self.core / 'device').resolve()
            need(controller.name == '64800000.dsp', '核心裝置對應不符。')
            name = 'c71-pose-v2-' + self.report['sha256'][:12] + '.xe71'
            state = read(self.core / 'state')
            need(state in ('offline', 'running'), 'C71 狀態異常：' + state)
            if state == 'running':
                need(read(self.core / 'firmware') == name, 'C71 仍在執行其他版本；請先依步驟 sudo reboot。')
            for module in ('rpmsg_char', 'rpmsg_ctrl'):
                subprocess.run(['modprobe', module], capture_output=True, timeout=10)
            if not os.path.ismount('/sys/kernel/debug'):
                subprocess.run(['mount', '-t', 'debugfs', 'debugfs', '/sys/kernel/debug'], capture_output=True, timeout=10, check=True)
            installed = Path('/lib/firmware') / name
            if not installed.exists():
                temporary = installed.with_name(installed.name + '.tmp')
                shutil.copyfile(firmware, temporary)
                os.chmod(temporary, 420)
                need(sha(temporary) == self.report['sha256'], '安裝校驗失敗。')
                temporary.replace(installed)
            need(sha(installed) == self.report['sha256'], '同名已安裝韌體內容不符。')
            if state == 'offline':
                self.support.write_once(self.core / 'firmware', name)
                self.support.write_once(self.core / 'state', 'start')
                self.support.wait_for(lambda: read(self.core / 'state') == 'running', 'C71 啟動')
            self.fd, self.created = self.support.open_endpoint(controller)
            hello = self.rpc('POSE,INFO').rstrip(b'\x00').decode('ascii')
            print('版本回覆：', hello, flush=True)
            fields = dict((p.split('=', 1) for p in hello.split(',') if '=' in p))
            need(hello.startswith('POSE,INFO,' + PROTOCOL + ',') and fields.get('flow') == '0' and (fields.get('model') == self.report['model_sha256']) and (fields.get('error') == '0'), 'C71 服務、模型或狀態不符。')
        except Exception:
            if self.core is not None:
                self.support.diagnostics(self.core)
            self.close()
            raise

    def rpc(self, message, timeout=10):
        data = message.encode('ascii') if isinstance(message, str) else message
        need(0 < len(data) < 256, '封包長度不符。')
        end = time.monotonic() + timeout
        while True:
            try:
                need(os.write(self.fd, data) == len(data), '封包寫入不完整。')
                break
            except BlockingIOError:
                need(time.monotonic() < end, '封包送出逾時。')
                time.sleep(0.001)
        remaining = end - time.monotonic()
        if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
            raise TimeoutError('C71 回覆逾時；未自動重送。')
        reply = os.read(self.fd, 4096)
        need(reply, 'C71 通訊裝置已關閉。')
        need(not reply.startswith(b'POSE,ERROR'), reply.decode('ascii', errors='replace'))
        return reply

    def infer(self, data, full_output=False):
        need(isinstance(data, bytes) and len(data) == INPUT_BYTES, '輸入必須是 RGB、CHW、uint8，共 147456 位元組。')
        self.serial = self.serial + 1 & 4294967295 or 1
        frame = self.serial
        start = time.monotonic()
        answer = self.rpc(f'POSE,BEGIN,{frame},{len(data)},{zlib.crc32(data)}', 120)
        need(answer == f'POSE,READY,{frame},{len(data)}'.encode(), '接收初始化回覆不符。')
        ready = time.monotonic()
        deadline = ready + 30
        for offset in range(0, len(data), 240):
            chunk = data[offset:offset + 240]
            need(time.monotonic() < deadline, '整張圖片傳送超過 30 秒。')
            ack = self.rpc(b'PIN2' + struct.pack('<II', frame, offset) + chunk, min(3, max(0.001, deadline - time.monotonic())))
            need(ack == b'PIA2' + struct.pack('<II', frame, offset + len(chunk)), '圖片確認封包的編號或位置不符。')
        uploaded = time.monotonic()
        result = self.rpc(f'POSE,RUN,{frame}', 120).rstrip(b'\x00').decode().split(',')
        finished = time.monotonic()
        need(len(result) == 6 and result[:2] == ['POSE', 'DONE'] and (int(result[2]) == frame) and (int(result[3]) == OUTPUT_BYTES), '推論完成回覆不符。')
        scale, cycles = (float(result[4]), int(result[5]))
        need(math.isfinite(scale) and scale > 0 and (cycles >= 0), '推論數值格式不符。')
        packed = self.rpc(f'POSE,KEYS,{frame}')
        need(len(packed) == 224 and packed[:4] == b'PKP2', '關節封包長度不符。')
        key_id, key_cycles, key_scale = struct.unpack_from('<IQf', packed, 4)
        need(key_id == frame and key_cycles == cycles and math.isclose(key_scale, scale, rel_tol=1e-06), '關節封包不是本張圖片。')
        points = np.frombuffer(packed, dtype='<f4', offset=20).reshape(17, 3).copy()
        need(np.isfinite(points).all(), '關節座標含有無效數值。')
        elapsed = time.monotonic()
        answer = {'frame': frame, 'keypoints': points.tolist(), 'scale': scale, 'cycles': cycles, 'prepare_ms': (ready - start) * 1000, 'upload_ms': (uploaded - ready) * 1000, 'run_rpc_ms': (finished - uploaded) * 1000, 'total_ms': (elapsed - start) * 1000}
        if full_output:
            raw = bytearray()
            while len(raw) < OUTPUT_BYTES:
                offset = len(raw)
                block = self.rpc(f'POSE,OUT,{frame},{offset}')
                count = min(240, OUTPUT_BYTES - offset)
                need(len(block) == 12 + count and block[:12] == b'POD2' + struct.pack('<II', frame, offset), '熱圖讀回的圖片編號、位置或長度不符。')
                raw.extend(block[12:])
            answer['raw'] = bytes(raw)
        return answer

    def close(self):
        if self.fd is not None:
            try:
                if self.created:
                    fcntl.ioctl(self.fd, self.support.DESTROY_EPT)
            finally:
                os.close(self.fd)
                self.fd = None
        if self.guard is not None:
            self.guard.close()
            self.guard = None

def local_keys(raw, scale):
    maps = np.frombuffer(raw, dtype=np.int8).reshape(17, 64, 48)
    points = []
    for heat in maps:
        y, x = np.unravel_index(np.argmax(heat), heat.shape)
        peak = int(heat[y, x])
        px, py = (-1.0, -1.0)
        if peak > 0:
            px, py = (float(x * 4), float(y * 4))
            if 1 < x < 47 and 1 < y < 63:
                px += int(np.sign(int(heat[y, x + 1]) - int(heat[y, x - 1])))
                py += int(np.sign(int(heat[y + 1, x]) - int(heat[y - 1, x])))
        points.append([px, py, peak / scale])
    return np.asarray(points, dtype=np.float32)

def selftest():
    client = None
    try:
        client = PoseClient()
        bundle = Path(read(ROOT / 'last-staged.txt')).resolve()
        need(bundle.parent == (ROOT / 'incoming').resolve(), '測試資料位置不符。')
        manifest = json.loads((bundle / 'manifest.json').read_text())
        for name in ('test_input_rgb_chw_u8.bin', 'expected_int8_output_f32.bin'):
            need(sha(bundle / name) == manifest['files'][name], '測試資料校驗失敗：' + name)
        original = (bundle / 'test_input_rgb_chw_u8.bin').read_bytes()
        need(len(original) == INPUT_BYTES and hashlib.sha256(original).hexdigest() == client.report['asset_sha256']['test_input'], '參考圖片版本不符。')
        expected = np.fromfile(bundle / 'expected_int8_output_f32.bin', dtype='<f4').reshape(17, 64, 48)
        need(np.isfinite(expected).all() and np.any(expected), '參考輸出無效。')
        mirrored = np.frombuffer(original, dtype=np.uint8).reshape(3, 256, 192)[:, :, ::-1].copy().tobytes()
        need(mirrored != original, '參考圖片左右完全對稱，無法進行這項測試。')
        folder = ROOT / 'external-tests' / str(time.time_ns())
        folder.mkdir(parents=True)
        print('本次紀錄：', folder, flush=True)
        results, outputs = ([], [])
        cases = (('原圖', original), ('左右翻轉', mirrored), ('恢復原圖', original))
        for index, (title, data) in enumerate(cases):
            print('\n開始傳入圖片：' + title, flush=True)
            result = client.infer(data, full_output=True)
            raw = result.pop('raw')
            (folder / f'{index}-output-int8.bin').write_bytes(raw)
            actual = np.frombuffer(raw, dtype=np.int8).astype(np.float32).reshape(17, 64, 48) / result['scale']
            need(np.allclose(result['keypoints'], local_keys(raw, result['scale']), rtol=1e-06, atol=1e-06), 'C71 關節封包與完整熱圖不一致。')
            if index in (0, 2):
                difference = np.abs(actual - expected)
                result['reference_max_abs'] = float(difference.max())
                need(np.all(difference <= 2 / result['scale'] + 1e-06), '原圖輸出與 TI 參考差異超過門檻。')
                print('與 TI 參考最大差：', result['reference_max_abs'])
            result.update(title=title, input_sha256=hashlib.sha256(data).hexdigest())
            results.append(result)
            outputs.append(actual)
            print(f"傳圖={result['upload_ms']:.1f} 毫秒｜推論請求往返={result['run_rpc_ms']:.1f} 毫秒")
        change = float(np.max(np.abs(outputs[1] - outputs[0])))
        need(change > 1e-06, '翻轉圖片後輸出完全相同，尚不能確認外部輸入生效。')
        report = {'passed': True, 'protocol': PROTOCOL, 'boot_id': read(Path('/proc/sys/kernel/random/boot_id')), 'firmware_sha256': client.report['sha256'], 'model_sha256': client.report['model_sha256'], 'mirror_output_change': change, 'results': results}
        (folder / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        (ROOT / 'last-external-test.txt').write_text(str(folder) + '\n')
        print('\nPASS：外部傳圖、不同圖片推論、原圖重現與關節回傳全部通過。')
        print('紀錄位置：', folder)
    except Exception:
        if client is not None:
            client.support.diagnostics(client.core)
        raise
    finally:
        if client is not None:
            client.close()
            print('結束時 C71 狀態：', read(client.core / 'state'), '；保持運行。')
if __name__ == '__main__':
    try:
        selftest()
    except KeyboardInterrupt:
        raise SystemExit('\n停止等候；未停止 C71。')
    except Exception as exc:
        raise SystemExit('\n停止：' + str(exc))
