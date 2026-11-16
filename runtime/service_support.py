from pathlib import Path
from contextlib import nullcontext
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = Path('/home/debian/interaction_demo')
LOG_DIR = ROOT / 'service-logs'
CAMERA = Path('/dev/v4l/by-id/usb-046d_C270_HD_WEBCAM_200901010001-video-index0')
MAX_LOG_BYTES = 8 * 1024 * 1024
ZERO_PENDING = False

def request_zero(signum, frame):
    global ZERO_PENDING
    ZERO_PENDING = True

def take_zero():
    global ZERO_PENDING
    value, ZERO_PENDING = ZERO_PENDING, False
    return value

def sensor_signals():
    signal.signal(signal.SIGUSR1, request_zero)

class RollingText:
    def __init__(self, name):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOG_DIR / (name + '.jsonl')
        self.lock = threading.RLock()
        self.stream = self.path.open('ab')

    def write(self, text):
        raw = text.encode('utf-8')
        if len(raw) > MAX_LOG_BYTES:
            raise RuntimeError('單筆紀錄超過檔案大小上限。')
        with self.lock:
            if self.stream.tell() + len(raw) > MAX_LOG_BYTES:
                self.stream.close()
                for index in range(3, 0, -1):
                    old = Path(str(self.path) + '.' + str(index-1)) if index > 1 else self.path
                    if old.exists():
                        old.replace(Path(str(self.path) + '.' + str(index)))
                self.stream = self.path.open('ab')
            self.stream.write(raw)
            self.stream.flush()
        return len(text)

    def close(self):
        with self.lock:
            self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

def open_log(name):
    return RollingText(name)

def worker_stderr():
    return nullcontext(sys.stderr)

def read(path):
    return path.read_text().strip('\x00 \t\r\n')

def core(name):
    found = [p for p in Path('/sys/class/remoteproc').glob('remoteproc*')
             if read(p / 'name') == name]
    if len(found) != 1:
        raise RuntimeError('尚未找到核心：' + name)
    return found[0]

def wait(check, timeout):
    end = time.monotonic() + timeout
    last = '資料尚未就緒'
    while time.monotonic() < end:
        try:
            if check():
                return
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            last = str(exc)
        time.sleep(.25)
    raise RuntimeError('啟動等候逾時：' + last)

def devices(role):
    if role == 'sensors':
        subprocess.run(['modprobe', 'i2c-dev'], check=True, timeout=15)
        from interaction_sensor_io import mlx_bus
        def check():
            core('5c00000.r5f')
            return bool(list(Path('/dev').glob('gpiochip*'))) and Path(f'/dev/i2c-{mlx_bus()}').exists()
    else:
        def check():
            core('64800000.dsp')
            return CAMERA.exists() and Path('/dev/hailo0').exists()
    wait(check, 60)
    print('裝置已就緒：' + role, flush=True)

def sensor_ready():
    value = json.loads((ROOT / 'status.json').read_text())
    return (value.get('boot_id') == read(Path('/proc/sys/kernel/random/boot_id'))
            and value.get('phase') == 'running'
            and value.get('presence', {}).get('valid') is True
            and 0 <= time.monotonic_ns() - value.get('host_monotonic_ns', 0) <= 2_000_000_000)

def rezero(pid):
    if pid <= 1:
        raise RuntimeError('感測服務程序編號無效。')
    requested = time.monotonic_ns()
    os.kill(pid, signal.SIGUSR1)
    started = False
    def finished():
        nonlocal started
        value = json.loads((ROOT / 'status.json').read_text())
        if value.get('phase') == 'zeroing' and value.get('host_monotonic_ns', 0) >= requested:
            started = True
        return started and sensor_ready()
    wait(finished, 90)
    print('重新歸零完成，R5F 持續運行。', flush=True)

def status():
    good = True
    for unit in ('bbai64-interaction-sensors.service', 'bbai64-interaction-vision.service'):
        result = subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True, timeout=10)
        print(unit, '：', result.stdout.strip())
        good = good and result.returncode == 0
    for name in ('5c00000.r5f', '64800000.dsp'):
        target = core(name)
        state = read(target / 'state')
        print(name, '：', state)
        good = good and state == 'running'
    previous = None
    for _ in range(2):
        with urllib.request.urlopen('http://127.0.0.1:8080/state', timeout=5) as response:
            value = json.load(response)
        vision, sensors = value['vision'], value['sensors']
        good = good and bool(vision.get('valid') and sensors.get('usable'))
        if previous is not None:
            good = good and vision.get('frame') != previous
        previous = vision.get('frame')
        time.sleep(.5)
    print('秤台：', sensors.get('presence'))
    print('溫度：', sensors.get('temperature'))
    print('影像：', round(vision.get('fps', 0), 1), '張／秒')
    pose = vision.get('pose', {})
    print('姿態：', str(pose.get('visible')) + '/17' if pose.get('valid') else pose.get('message'))
    good = good and bool(sensors.get('temperature', {}).get('valid')) and not pose.get('error')
    print('網頁：http://192.168.1.253:8080')
    if not good:
        raise RuntimeError('仍有服務或資料未就緒，請保留以上輸出。')
    print('PASS：服務、兩顆遠端核心、感測資料與更新中的影像均正常。')

if __name__ == '__main__':
    try:
        if sys.argv[1] == 'devices':
            devices(sys.argv[2])
        elif sys.argv[1] == 'ready':
            wait(sensor_ready, 120)
            print('自動歸零完成，感測資料已就緒。', flush=True)
        elif sys.argv[1] == 'status':
            status()
        elif sys.argv[1] == 'zero':
            rezero(int(sys.argv[2]))
        else:
            raise RuntimeError('未知的服務指令。')
    except Exception as exc:
        raise SystemExit('停止：' + str(exc))
