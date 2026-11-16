from pathlib import Path
import fcntl
import hashlib
import json
import math
import os
import re
import select
import shutil
import struct
import subprocess
import time
import numpy as np

ROOT = Path('/home/debian/pose_demo')
DT = Path('/sys/firmware/devicetree/base')
EXPECTED = 'fc4f835f0c17197cd957ee414deb2374de07e44a928d8de22ad05deb5c655a2e'
FW_NAME = 'c71-pose-selftest-' + EXPECTED[:12] + '.xe71'
OUTPUT_BYTES = 17 * 64 * 48
CREATE_EPT, DESTROY_EPT = 0x4028B501, 0xB502

def need(ok, message):
    if not ok:
        raise RuntimeError(message)

def read(path):
    return path.read_bytes().decode(
        errors='replace'
    ).strip('\x00 \t\r\n')

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def save(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(
        json.dumps(
            value, ensure_ascii=False, indent=2,
            allow_nan=False
        ) + '\n'
    )
    temporary.replace(path)

def write_once(path, value):
    data = (value + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CLOEXEC)
    try:
        need(
            os.write(fd, data) == len(data),
            '核心控制欄位寫入不完整。'
        )
    finally:
        os.close(fd)

def wait_for(check, description, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise TimeoutError('等候逾時：' + description)

def traces(core):
    folder = Path('/sys/kernel/debug/remoteproc') / core.name
    values = []
    for path in sorted(folder.glob('trace*')):
        values.append(read(path))
    return '\n'.join(values)

def diagnostics(core):
    if core is not None:
        print('\n=== C71 追蹤紀錄 ===')
        try:
            print(traces(core)[-10000:] or '目前沒有追蹤內容。')
            print('C71 狀態：', read(core / 'state'))
        except OSError as exc:
            print('追蹤讀取失敗：', exc)
    try:
        p = subprocess.run(
            ['dmesg', '--color=never'],
            capture_output=True, text=True,
            errors='replace', timeout=10
        )
        lines = [
            s for s in p.stdout.splitlines()
            if re.search(
                r'64800000|remoteproc|rpmsg|c71|sram|udma',
                s, re.I
            )
        ]
        print('\n=== 相關核心紀錄 ===\n' + '\n'.join(lines[-35:]))
    except OSError:
        pass

def related(path, controller):
    return controller in path.resolve().parents

def endpoints(controller, name):
    found = []
    for p in Path('/sys/class/rpmsg').glob('rpmsg*'):
        if not re.fullmatch(r'rpmsg\d+', p.name):
            continue
        if not related(p, controller):
            continue
        if (p / 'name').exists() and (p / 'dst').exists():
            if read(p / 'name') == name \
                    and int(read(p / 'dst'), 0) == 14:
                if (Path('/dev') / p.name).exists():
                    found.append(p)
    need(len(found) <= 1, '找到多個相同的 C71 通訊端點。')
    return found[0] if found else None

def open_endpoint(controller):
    def announced():
        for p in Path('/sys/bus/rpmsg/devices').glob('*'):
            if not related(p, controller):
                continue
            if (p / 'name').exists() and (p / 'dst').exists():
                if read(p / 'name') == 'rpmsg_chrdev' \
                        and int(read(p / 'dst'), 0) == 14:
                    return True
        return False

    wait_for(
        announced, 'C71 宣告端點 14 的 RPMsg 服務', 25
    )

    def available():
        default = endpoints(controller, 'rpmsg_chrdev')
        if default is not None:
            return ('default', default)
        controls = [
            p for p in Path('/sys/class/rpmsg').glob('rpmsg_ctrl*')
            if related(p, controller)
            and (Path('/dev') / p.name).exists()
        ]
        need(len(controls) <= 1, '找到多個 C71 控制裝置。')
        return ('control', controls[0]) if controls else None

    kind, path = wait_for(
        available, 'C71 的 RPMsg 字元裝置', 25
    )
    created = False
    if kind == 'control':
        name = 'pose-test-' + str(os.getpid())
        ctrl = os.open(
            Path('/dev') / path.name,
            os.O_RDWR | os.O_CLOEXEC
        )
        try:
            info = struct.pack(
                '=32sII', name.encode(), 0xFFFFFFFF, 14
            )
            fcntl.ioctl(ctrl, CREATE_EPT, info)
            path = wait_for(
                lambda: endpoints(controller, name),
                '本次通訊端點'
            )
            created = True
        finally:
            os.close(ctrl)

    device = Path('/dev') / path.name
    fd = os.open(
        device, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC
    )
    print('C71 訊息裝置：', device, flush=True)
    return fd, created

def exchange(fd, command, timeout=10, core=None):
    data = command.encode('ascii')
    end = time.monotonic() + timeout
    while True:
        try:
            need(
                os.write(fd, data) == len(data),
                '通訊封包寫入不完整。'
            )
            break
        except BlockingIOError:
            need(
                time.monotonic() < end,
                '通訊封包送出逾時。'
            )
            time.sleep(0.01)

    progress = time.monotonic() + 10
    while time.monotonic() < end:
        remaining = max(0, end - time.monotonic())
        if select.select([fd], [], [], min(1, remaining))[0]:
            try:
                reply = os.read(fd, 4096)
            except BlockingIOError:
                continue
            need(reply, 'C71 通訊裝置已關閉。')
            return reply
        if core is not None and time.monotonic() >= progress:
            stages = [
                s for s in traces(core).splitlines()
                if 'POSE stage=' in s
            ]
            print(
                '仍在等候推論回覆；'
                + (stages[-1] if stages else '尚無階段紀錄'),
                flush=True
            )
            progress = time.monotonic() + 10
    raise TimeoutError(
        command + ' 等候回覆逾時；未自動重送。'
    )

def fetch_output(fd, total):
    raw = bytearray()
    while len(raw) < total:
        offset = len(raw)
        reply = exchange(fd, 'POSE,OUT,' + str(offset))
        count = min(240, total - offset)
        need(
            len(reply) == 8 + count
            and reply[:4] == b'POD1'
            and struct.unpack_from('<I', reply, 4)[0] == offset,
            '熱圖讀回封包不完整或順序不符。'
        )
        raw.extend(reply[8:])
    return bytes(raw)

def compare(raw, scale, expected):
    actual = (
        np.frombuffer(raw, dtype=np.int8)
        .astype(np.float32)
        .reshape(17, 64, 48) / scale
    )
    difference = np.abs(actual - expected)
    denominator = float(
        np.linalg.norm(actual) * np.linalg.norm(expected)
    )
    cosine = (
        float(np.sum(actual * expected) / denominator)
        if denominator > 0 else None
    )
    step = 1.0 / scale
    close = bool(np.all(difference <= 2 * step + 1e-6))
    names = [
        '鼻子', '左眼', '右眼', '左耳', '右耳',
        '左肩', '右肩', '左肘', '右肘', '左腕', '右腕',
        '左髖', '右髖', '左膝', '右膝', '左踝', '右踝'
    ]
    joints = []
    print('\n=== 17 個關節熱圖峰值 ===')
    for k, name in enumerate(names):
        ry, rx = np.unravel_index(
            np.argmax(expected[k]), (64, 48)
        )
        ay, ax = np.unravel_index(
            np.argmax(actual[k]), (64, 48)
        )
        distance = math.hypot(
            int(ax) - int(rx), int(ay) - int(ry)
        )
        weak = float(expected[k, ry, rx]) < 0.20
        print(
            f'{name}：TI 模擬=({rx},{ry})，'
            f'C71=({ax},{ay})，差={distance:.2f} 格'
            + ('；參考訊號較弱' if weak else '')
        )
        joints.append({
            'name': name,
            'reference_xy': [int(rx), int(ry)],
            'c71_xy': [int(ax), int(ay)],
            'distance_cells': distance,
            'reference_weak': weak,
        })
    metrics = {
        'max_abs': float(difference.max()),
        'mean_abs': float(difference.mean()),
        'cosine': cosine,
        'quantization_step': step,
        'within_two_quantization_steps': close,
        'joints': joints,
    }
    return actual, metrics

def main():
    core = None
    fd = None
    created = False
    try:
        need(os.geteuid() == 0, '請加 sudo 執行。')
        need(
            'BeagleBone AI-64' in read(DT / 'model'),
            '請在 BeagleBone AI-64 執行。'
        )
        guard = (ROOT / '.c71-selftest.lock').open('a')
        fcntl.flock(
            guard, fcntl.LOCK_EX | fcntl.LOCK_NB
        )
        boot_id = read(
            Path('/proc/sys/kernel/random/boot_id')
        )
        ready = json.loads(
            (ROOT / 'c71-memory-ready.json').read_text()
        )
        need(
            ready.get('memory_ready') is True
            and ready['boot_id'] == boot_id,
            '缺少本次開機通過的記憶體驗證。'
        )
        ddr = DT / 'reserved-memory/c71-pose-memory@ac000000'
        need(
            (ddr / 'reg').read_bytes()
            == struct.pack(
                '>4I', 0, 0xAC000000, 0, 0x08000000
            )
            and (ddr / 'no-map').exists(),
            'DDR 保留設定不符。'
        )

        report = json.loads(Path(
            '/home/debian/c71-pose-selftest-build.json'
        ).read_text())
        firmware = Path(
            '/home/debian/c71-pose-selftest.xe71'
        )
        need(
            report.get('build_passed') is True
            and digest(firmware) == report['sha256'] == EXPECTED,
            '韌體不是最後一次通過建置的版本。'
        )

        bundle = Path(
            read(ROOT / 'last-staged.txt')
        ).resolve()
        need(
            bundle.parent == (ROOT / 'incoming').resolve(),
            '測試資料目錄不符。'
        )
        manifest = json.loads(
            (bundle / 'manifest.json').read_text()
        )
        for name, expected_hash in manifest['files'].items():
            need(
                Path(name).name == name
                and name not in ('.', '..'),
                '檔案清單路徑不符。'
            )
            need(
                digest(bundle / name) == expected_hash,
                '測試資料校驗失敗：' + name
            )
        for key, name in [
            ('network', 'network.bin'),
            ('io', 'io.bin'),
            ('test_input', 'test_input_rgb_chw_u8.bin'),
        ]:
            need(
                digest(bundle / name)
                == report['asset_sha256'][key],
                '韌體內建資料不符：' + name
            )

        expected = np.fromfile(
            bundle / 'expected_int8_output_f32.bin',
            dtype='<f4'
        )
        need(
            expected.size == OUTPUT_BYTES
            and np.isfinite(expected).all()
            and np.any(expected != 0),
            'TI 模擬輸出不完整。'
        )
        expected = expected.reshape(17, 64, 48)

        cores = []
        for p in Path('/sys/class/remoteproc').glob('remoteproc*'):
            name = read(p / 'name')
            if name == '64800000.dsp':
                cores.append(p)
            if name in ('4d80800000.dsp', '4d81800000.dsp'):
                need(
                    read(p / 'state') == 'offline',
                    'C66 正在運行，先停止本次測試。'
                )
        need(len(cores) == 1, '找不到唯一的 C71。')
        core = cores[0]
        controller = (core / 'device').resolve()
        need(
            controller.name == '64800000.dsp',
            'C71 裝置對應不符。'
        )
        print('目標核心：', core.name, read(core / 'name'))

        for module in ('rpmsg_char', 'rpmsg_ctrl'):
            subprocess.run(
                ['modprobe', module],
                capture_output=True, timeout=10
            )
        if not os.path.ismount('/sys/kernel/debug'):
            subprocess.run(
                [
                    'mount', '-t', 'debugfs', 'debugfs',
                    '/sys/kernel/debug'
                ],
                capture_output=True, timeout=10, check=True
            )

        target = Path('/lib/firmware') / FW_NAME
        if target.exists():
            need(
                digest(target) == EXPECTED,
                '已安裝的同名韌體內容不符。'
            )
        else:
            temporary = target.with_name(target.name + '.tmp')
            shutil.copyfile(firmware, temporary)
            os.chmod(temporary, 0o644)
            need(
                digest(temporary) == EXPECTED,
                '韌體安裝校驗失敗。'
            )
            temporary.replace(target)

        state = read(core / 'state')
        need(
            state in ('offline', 'running'),
            'C71 狀態異常：' + state
        )
        if state == 'offline':
            save(ROOT / 'c71-runtime-session.json', {
                'boot_id': boot_id,
                'core': core.name,
                'previous_firmware': read(core / 'firmware'),
                'firmware': FW_NAME,
                'sha256': EXPECTED,
            })
            write_once(core / 'firmware', FW_NAME)
            write_once(core / 'state', 'start')
            wait_for(
                lambda: read(core / 'state') == 'running',
                'C71 啟動'
            )
        else:
            need(
                read(core / 'firmware') == FW_NAME,
                'C71 正在執行其他韌體，未切換。'
            )

        print(
            'C71 狀態：', read(core / 'state'),
            flush=True
        )
        fd, created = open_endpoint(controller)
        hello = exchange(
            fd, 'POSE,INFO'
        ).rstrip(b'\0').decode('ascii')
        print('版本回覆：', hello, flush=True)
        need(
            hello.startswith('POSE,INFO,C71_TIDL_POSE_V1,'),
            '收到其他服務的回覆。'
        )
        fields = dict(
            p.split('=', 1)
            for p in hello.split(',') if '=' in p
        )
        need(
            fields.get('flow') == '0'
            and fields.get('model') == report['model_sha256'],
            'C71 模型或執行模式不符。'
        )
        need(
            fields.get('error') == '0',
            '前次 C71 推論已有錯誤，先貼回紀錄。'
        )

        print('開始執行內建的單張人物圖片……', flush=True)
        answer = exchange(
            fd, 'POSE,TEST', timeout=120, core=core
        ).rstrip(b'\0').decode('ascii')
        print('推論回覆：', answer, flush=True)

        parts = answer.split(',')
        need(
            len(parts) == 5
            and parts[:2] == ['POSE', 'DONE'],
            'C71 推論未完成：' + answer
        )
        total = int(parts[2])
        scale = float(parts[3])
        cycles = int(parts[4])
        need(
            total == OUTPUT_BYTES
            and math.isfinite(scale) and scale > 0,
            'C71 輸出尺寸或量化比例不符。'
        )

        raw = fetch_output(fd, total)
        actual, metrics = compare(raw, scale, expected)

        out = ROOT / 'selftest-results' / str(time.time_ns())
        out.mkdir(parents=True)
        (out / 'output-int8.bin').write_bytes(raw)
        actual.astype('<f4').tofile(out / 'output-fp32.bin')
        (out / 'c71-trace.txt').write_text(traces(core))

        result = {
            'hardware_inference_completed': True,
            'single_image_numeric_passed':
                metrics['within_two_quantization_steps'],
            'firmware_sha256': EXPECTED,
            'model_sha256': report['model_sha256'],
            'input_sha256': report['asset_sha256']['test_input'],
            'output_sha256': hashlib.sha256(raw).hexdigest(),
            'output_shape': [1, 17, 64, 48],
            'scale': scale,
            'cycles': cycles,
            'boot_id': boot_id,
            'metrics': metrics,
        }
        save(out / 'result.json', result)
        (ROOT / 'last-selftest.txt').write_text(str(out) + '\n')

        print('\n=== 硬體與 TI 主機模擬比較 ===')
        print('PASS：C71 推論完成，完整讀回 52224 個 INT8 數值。')
        print('最大絕對差：', metrics['max_abs'])
        print('平均絕對差：', metrics['mean_abs'])
        print('餘弦相似度：', metrics['cosine'])
        print('一個量化刻度：', metrics['quantization_step'])
        if metrics['within_two_quantization_steps']:
            print('PASS：本張輸出差異均在 2 個量化刻度內。')
        else:
            print('推論與傳輸完成；數值差異超過本次檢查門檻，請貼回結果。')
        print('紀錄位置：', out)

    except Exception:
        diagnostics(core)
        raise
    finally:
        if fd is not None:
            if created:
                try:
                    fcntl.ioctl(fd, DESTROY_EPT)
                except OSError as exc:
                    print('端點清理訊息：', exc)
            os.close(fd)
        if core is not None:
            try:
                print(
                    '結束時 C71 狀態：', read(core / 'state'),
                    '；本程式沒有停止核心。'
                )
            except OSError:
                print('C71 狀態暫時無法讀取；本程式沒有發出停止指令。')

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit('\n已停止主機端等候，C71 保持原狀。')
    except Exception as exc:
        raise SystemExit('\n停止：' + str(exc))
