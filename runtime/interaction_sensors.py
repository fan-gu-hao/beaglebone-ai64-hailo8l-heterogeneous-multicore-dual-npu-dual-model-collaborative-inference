from service_support import open_log, LOG_DIR, CAMERA, worker_stderr, sensor_signals, take_zero
from pathlib import Path
import errno
import fcntl
import hashlib
import os
import re
import select
import statistics
import struct
import subprocess
import sys
import time
import gpiod
EXPECTED = '764a9ca96bd0b31790aa30d05d4e817b7619d8a6f6573eaf9db4adab361380f5'
FW_NAME = 'hx711-mcu2_0-' + EXPECTED[:12] + '.elf'
RPMSG = Path('/sys/class/rpmsg')
CREATE_EPT = 1076409601
DESTROY_EPT = 46338

def sysfs_write_once(path, value):
    payload = value.encode('ascii')
    fd = os.open(str(path), os.O_WRONLY | os.O_CLOEXEC)
    try:
        count = os.write(fd, payload)
        if count != len(payload):
            raise OSError(errno.EIO, '核心控制指令沒有完整寫入')
    finally:
        os.close(fd)

def read(path):
    return path.read_bytes().decode(errors='replace').strip('\x00 \t\r\n')

def wait_for(check, description, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise RuntimeError('等候逾時：' + description)

def belongs(path, parent):
    return path.resolve().is_relative_to(parent.resolve())

def endpoint(parent, name):
    found = [p for p in RPMSG.glob('rpmsg*') if re.fullmatch('rpmsg\\d+', p.name) and belongs(p, parent) and (p / 'name').exists() and (read(p / 'name') == name) and (p / 'dst').exists() and (int(read(p / 'dst'), 0) == 14) and (Path('/dev') / p.name).exists()]
    if len(found) > 1:
        raise RuntimeError('找到多個相同的訊息端點。')
    return found[0] if found else None

def rpc(fd, command, timeout=4):
    payload = command.encode('ascii') + b'\x00'
    end = time.monotonic() + timeout
    while True:
        try:
            count = os.write(fd, payload)
            if count != len(payload):
                raise RuntimeError('訊息沒有完整送出。')
            break
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.ENOMEM, errno.EINTR):
                raise
            if time.monotonic() >= end:
                raise RuntimeError('傳送逾時：' + command)
            time.sleep(0.01)
    while time.monotonic() < end:
        ready = select.select([fd], [], [], max(0, end - time.monotonic()))[0]
        if not ready:
            break
        try:
            reply = os.read(fd, 256)
        except BlockingIOError:
            continue
        if not reply:
            raise RuntimeError('訊息端點已關閉。')
        return reply.rstrip(b'\x00\r\n').decode('ascii')
    raise RuntimeError('沒有收到 R5F 回覆：' + command)

def check_memory(core):
    node = (core / 'device/of_node').resolve()
    refs = (node / 'memory-region').read_bytes()
    handles = struct.unpack('>' + 'I' * (len(refs) // 4), refs)
    regions = set()
    root = Path('/sys/firmware/devicetree/base/reserved-memory')
    for child in root.iterdir():
        ph = child / 'phandle'
        reg = child / 'reg'
        if ph.is_file() and reg.is_file():
            if int.from_bytes(ph.read_bytes(), 'big') in handles:
                data = reg.read_bytes()
                if len(data) != 16:
                    raise RuntimeError('保留記憶體格式與先前不同。')
                address, size = struct.unpack('>QQ', data)
                regions.add((address, size))
    expected = {(2717908992, 1048576), (2718957568, 15728640)}
    if regions != expected:
        raise RuntimeError(f'保留記憶體與編譯設定不符：{regions}')
from interaction_sensor_io import SensorFusion

def main():
    sensor_signals()
    if os.geteuid() != 0:
        raise RuntimeError('請使用 sudo 執行。')
    if 'BeagleBone AI-64' not in read(Path('/proc/device-tree/model')):
        raise RuntimeError('這段必須在 BeagleBone AI-64 執行。')
    if not hasattr(gpiod, 'LINE_REQ_DIR_OUT'):
        raise RuntimeError('需要 Debian 12 的 libgpiod 1.x Python 介面。')
    if len(sys.argv) != 2:
        raise RuntimeError('請在指令最後指定韌體路徑。')
    image = Path(sys.argv[1]).read_bytes()
    if hashlib.sha256(image).hexdigest() != EXPECTED:
        raise RuntimeError('韌體 SHA256 與 Ubuntu 上的結果不同。')
    cores = [p for p in Path('/sys/class/remoteproc').glob('remoteproc*') if read(p / 'name') == '5c00000.r5f']
    if len(cores) != 1:
        raise RuntimeError('找不到唯一的 5c00000.r5f。')
    core = cores[0]
    if read(core / 'state') != 'offline':
        raise RuntimeError(f"{core.name} 目前為 {read(core / 'state')}，請貼回狀態。")
    check_memory(core)
    print('目標核心：', core.name, read(core / 'name'))
    installed = Path('/lib/firmware') / FW_NAME
    if installed.is_symlink():
        raise RuntimeError('專用韌體路徑是符號連結，停止。')
    if installed.exists():
        if hashlib.sha256(installed.read_bytes()).hexdigest() != EXPECTED:
            raise RuntimeError('專用韌體路徑已存在不同內容。')
    else:
        with installed.open('xb') as output:
            output.write(image)
        installed.chmod(420)
    for module, driver in (('rpmsg_char', 'rpmsg_chrdev'), ('rpmsg_ctrl', 'rpmsg_ctrl')):
        if not (Path('/sys/bus/rpmsg/drivers') / driver).exists():
            subprocess.run(['modprobe', module], check=True)
    chip = None
    lines = []
    clock = None
    control_fd = endpoint_fd = None
    created = False
    started = False
    old_firmware = read(core / 'firmware')
    fusion = None
    try:
        for path in sorted(Path('/dev').glob('gpiochip*')):
            candidate = gpiod.Chip(str(path))
            if candidate.label() == '600000.gpio':
                chip = candidate
                break
            candidate.close()
        if chip is None:
            raise RuntimeError('找不到主域 GPIO0 控制器。')
        data_line = chip.get_line(45)
        data_line.request(consumer='hx711-r5f-data', type=gpiod.LINE_REQ_DIR_IN)
        lines.append(data_line)
        clock_line = chip.get_line(47)
        clock_line.request(consumer='hx711-r5f-clock', type=gpiod.LINE_REQ_DIR_OUT, default_val=0)
        lines.append(clock_line)
        clock = clock_line
        print('GPIO 已保留：', chip.name(), '資料=45、時脈=47')
        sysfs_write_once(core / 'firmware', FW_NAME)
        started = True
        sysfs_write_once(core / 'state', 'start')
        wait_for(lambda: read(core / 'state') == 'running', 'R5F 啟動')
        print('R5F 狀態：running')
        wait_for(lambda: any((belongs(p, core) and (p / 'name').exists() and (read(p / 'name') == 'rpmsg_chrdev') for p in Path('/sys/bus/rpmsg/devices').glob('*'))), 'R5F 宣告 RPMsg 服務')

        def interface():
            ep = endpoint(core, 'rpmsg_chrdev')
            if ep:
                return ('endpoint', ep)
            controls = [p for p in RPMSG.glob('rpmsg_ctrl*') if belongs(p, core) and (Path('/dev') / p.name).exists()]
            if len(controls) == 1:
                return ('control', controls[0])
            return None
        kind, device = wait_for(interface, '這顆 R5F 的 RPMsg 介面')
        if kind == 'control':
            control_fd = os.open(str(Path('/dev') / device.name), os.O_RDWR)
            name = 'hx711-' + str(os.getpid())
            fcntl.ioctl(control_fd, CREATE_EPT, struct.pack('=32sII', name.encode(), 4294967295, 14))
            created = True
            device = wait_for(lambda: endpoint(device, name), '專用 RPMsg 端點')
        endpoint_fd = os.open(str(Path('/dev') / device.name), os.O_RDWR | os.O_NONBLOCK)
        print('訊息裝置：', '/dev/' + device.name)
        hello = rpc(endpoint_fd, 'HX,HELLO')
        print('版本回覆：', hello)
        if hello != 'HX,HELLO,HX711_R5F_V1,mcu2_0,P9.12,P9.15':
            raise RuntimeError('回覆不是預期的 HX711 韌體。')
        reply = rpc(endpoint_fd, 'HX,INIT')
        print('初始化回覆：', reply)
        if reply != 'HX,INIT,0':
            raise RuntimeError('HX711 初始化失敗：' + reply)
        previous = [0, -1]

        def sample():
            reply = rpc(endpoint_fd, 'HX,READ')
            parts = reply.split(',')
            if len(parts) != 7 or parts[:2] != ['HX', 'RAW']:
                raise RuntimeError('取樣失敗：' + reply)
            seq, raw, high, low, maximum = map(int, parts[2:])
            stamp = high << 32 | low
            if seq != previous[0] + 1 or stamp <= previous[1]:
                raise RuntimeError('取樣序號或時間戳異常：' + reply)
            if not -8388608 <= raw <= 8388607:
                raise RuntimeError('取樣值超出範圍：' + reply)
            if not 0 <= maximum < 45:
                raise RuntimeError('脈衝耗時異常：' + reply)
            previous[:] = [seq, stamp]
            return (seq, raw, stamp, maximum)
        from collections import deque
        ON_THRESHOLD = 5000
        OFF_THRESHOLD = 2500
        CONFIRM_COUNT = 3

        def raw_value():
            _, value, _, _ = sample()
            if value in (-8388608, 8388607):
                raise RuntimeError('感測讀值已達飽和，停止判斷。')
            return value

        def obtain_zero():
            while True:
                print('自動建立空秤基準，請保持秤台清空、手離開。', flush=True)
                print('正在暖機及取樣建立零點，期間請勿碰秤台或桌面。')
                for _ in range(20):
                    raw_value()
                    time.sleep(0.1)
                values = []
                for _ in range(50):
                    values.append(raw_value())
                    time.sleep(0.1)
                span = max(values) - min(values)
                if span > 2000:
                    print(f'空秤取樣變動較大：{span} 計數，請重新確認空秤。')
                    continue
                zero = statistics.median(values)
                print(f'空秤基準={zero:g}，取樣極差={span} 計數')
                return zero
        print('\n=== 物品存在偵測 ===')
        print('目前接線下，放上物品會使原始值增加。')
        print(f'有物品門檻：+{ON_THRESHOLD}；空秤門檻：+{OFF_THRESHOLD} 計數')
        print('使用最近 5 筆中位數，連續 3 次符合條件才切換。')
        fusion = SensorFusion(Path(sys.argv[1]).resolve().parent / 'interaction_demo')
        fusion.invalidate('zeroing')
        zero = obtain_zero()
        if zero is None:
            return
        window = deque(maxlen=5)
        present = False
        confirmations = 0
        last_report = float('-inf')
        print('感測服務已開始；使用服務指令可重新歸零。')
        while True:
            if take_zero():
                fusion.invalidate('zeroing')
                zero = obtain_zero()
                if zero is None:
                    return
                window.clear()
                present = False
                confirmations = 0
                last_report = float('-inf')
                continue
            window.append(raw_value())
            if len(window) == window.maxlen:
                delta = statistics.median(window) - zero
                condition = delta <= OFF_THRESHOLD if present else delta >= ON_THRESHOLD
                confirmations = confirmations + 1 if condition else 0
                changed = confirmations >= CONFIRM_COUNT
                if changed:
                    present = not present
                    confirmations = 0
                fusion.update(present, delta, previous[0], previous[1])
                now = time.monotonic()
                if changed or now - last_report >= 1.0:
                    label = '有物品' if present else '空秤'
                    note = '；基準負向偏移，確認清空後可使用服務指令重新歸零' if delta < -ON_THRESHOLD else ''
                    print(f'狀態={label}｜相對空秤差值={delta:+.0f} 計數{note}｜{fusion.describe()}')
                    last_report = now
            time.sleep(0.1)
    except BaseException:
        trace = Path('/sys/kernel/debug/remoteproc') / core.name / 'trace0'
        if trace.exists():
            print('\n=== R5F 追蹤紀錄 ===')
            print(read(trace)[-2500:])
        raise
    finally:
        if fusion is not None:
            try:
                fusion.close()
            except Exception as exc:
                print('感測紀錄關閉錯誤：', exc)
        if endpoint_fd is not None:
            if created:
                try:
                    fcntl.ioctl(endpoint_fd, DESTROY_EPT, 0)
                except OSError:
                    pass
            os.close(endpoint_fd)
        if control_fd is not None:
            os.close(control_fd)
        if started:
            try:
                if read(core / 'state') in ('running', 'crashed'):
                    sysfs_write_once(core / 'state', 'stop')
                wait_for(lambda: read(core / 'state') == 'offline', 'R5F 停止', 5)
                sysfs_write_once(core / 'firmware', old_firmware)
                print('R5F 狀態欄位：offline；已還原韌體選擇。')
            except Exception as exc:
                print('R5F 清理未完成：', exc)
        if clock is not None:
            try:
                clock.set_value(0)
            except OSError:
                pass
        for line in reversed(lines):
            line.release()
        if chip is not None:
            chip.close()
if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n已中止測試。')
        sys.exit(130)
    except Exception as exc:
        print('\n測試停止：', exc)
        try:
            result = subprocess.run(['dmesg', '--color=never'], capture_output=True, text=True, errors='replace', timeout=5)
            logs = [s for s in result.stdout.splitlines() if re.search('remoteproc|rpmsg|r5f|5c00000', s, re.I)]
            print('\n=== 相關核心紀錄 ===\n' + '\n'.join(logs[-25:]))
        except Exception:
            pass
        sys.exit(1)
