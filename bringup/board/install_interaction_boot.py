SUPPORT = r'''from pathlib import Path
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
'''

from pathlib import Path
import ast
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile

HOME_DIR = Path('/home/debian')
APP = HOME_DIR / 'interaction_demo'
UNIT_NAMES = (
    'bbai64-interaction-sensors.service',
    'bbai64-interaction-vision.service',
    'bbai64-interaction.target'
)
EXPECTED_SOURCES = {
    'interaction_sensors.py':
        '407386bba6c342cd5eff6e6588cdcb2d8b0552fbcf9aba209d595e9119741b96',
    'interaction_sensor_io.py':
        '3c91785c3a4c48c5e000fabdb34449a508f2070de7acbeb65a0b6aaa06d206f4',
}

def need(condition, message):
    if not condition:
        raise RuntimeError(message)

def sha(data):
    return hashlib.sha256(data).hexdigest()

def expression(code):
    return ast.parse(code, mode='eval').body

class ServiceVersion(ast.NodeTransformer):
    def __init__(self, name):
        self.name, self.counts = name, {}

    def changed(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1

    def visit_FunctionDef(self, node):
        if self.name == 'interaction_sensors.py' and node.name == 'obtain_zero':
            loop = node.body[0]
            need(
                isinstance(loop, ast.While)
                and 'input(' in ast.unparse(loop.body[0])
                and ast.unparse(loop.body[1].test) == "answer == 'q'",
                '歸零函式結構不同。'
            )
            loop.body[:2] = ast.parse(
                "print('自動建立空秤基準，請保持秤台清空、手離開。', flush=True)"
            ).body
            self.changed('zero')
        node = self.generic_visit(node)
        if self.name == 'interaction_sensors.py' and node.name == 'main':
            node.body.insert(0, ast.Expr(expression('sensor_signals()')))
            self.changed('signals')
        return node

    def visit_If(self, node):
        if self.name == 'interaction_sensors.py' and 'sys.stdin' in ast.unparse(node.test):
            commands = [
                n for n in node.body
                if isinstance(n, ast.If)
                and ast.unparse(n.test) == "command == 'z'"
            ]
            need(len(commands) == 1, '重新歸零指令結構不同。')
            self.changed('keyboard')
            return ast.If(expression('take_zero()'), commands[0].body, [])
        return self.generic_visit(node)

    def visit_Assign(self, node):
        target = ast.unparse(node.targets[0])
        if self.name in ('interaction_app.py', 'interaction_pose_app.py') and target == 'ROOT':
            node.value = expression("Path('/home/debian/interaction_demo')")
            self.changed('root')
        if self.name in ('interaction_sensor_io.py', 'interaction_fusion.py') and target == 'self.log':
            key = 'sensors' if self.name == 'interaction_sensor_io.py' else 'events'
            node.value = expression(f'open_log({key!r})')
            self.changed('log')
        if self.name == 'interaction_fusion.py' and target == 'self.log_path':
            node.value = expression("LOG_DIR / 'events.jsonl'")
            self.changed('logpath')
        return self.generic_visit(node)

    def visit_Call(self, node):
        if self.name == 'interaction_app.py' and ast.unparse(node.func) == 'cv2.VideoCapture':
            need(
                len(node.args) == 2 and ast.unparse(node.args[0]) == '0',
                '攝影機開啟方式不同。'
            )
            node.args[0] = expression('str(CAMERA)')
            self.changed('camera')
        return self.generic_visit(node)

    def visit_With(self, node):
        if self.name == 'interaction_pose_app.py':
            for item in node.items:
                target = ast.unparse(item.optional_vars) if item.optional_vars else ''
                if target in ('errors', 'records'):
                    item.context_expr = expression(
                        'worker_stderr()' if target == 'errors'
                        else "open_log('vision-pose')"
                    )
                    self.changed(target)
        return self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            if self.name == 'interaction_sensors.py':
                node.value = node.value.replace(
                    '空秤取樣約 7 秒', '正在暖機及取樣建立零點'
                )
                node.value = node.value.replace(
                    '輸入 z 重新歸零', '使用服務指令重新歸零'
                )
                if '輸入 z 再按 Enter' in node.value:
                    node.value = '感測服務已開始；使用服務指令可重新歸零。'
            if '按 Ctrl+C 結束此視窗' in node.value:
                node.value = '開機展示服務已啟動。'
        return node

def convert(name, source):
    expected = {
        'interaction_sensors.py': {'zero':1, 'signals':1, 'keyboard':1},
        'interaction_sensor_io.py': {'log':1},
        'interaction_fusion.py': {'log':1, 'logpath':1},
        'interaction_app.py': {'root':1, 'camera':1},
        'interaction_pose_app.py': {'root':1, 'errors':1, 'records':1},
    }
    transform = ServiceVersion(name)
    tree = transform.visit(ast.parse(source))
    need(
        transform.counts == expected.get(name, {}),
        f'{name} 程式結構不符：{transform.counts}'
    )
    tree.body.insert(0, ast.parse(
        'from service_support import open_log, LOG_DIR, CAMERA, '
        'worker_stderr, sensor_signals, take_zero'
    ).body[0])
    result = ast.unparse(ast.fix_missing_locations(tree)) + '\n'
    compile(result, name, 'exec')
    if name == 'interaction_sensors.py':
        need(
            'sys.stdin' not in result and 'input(' not in result,
            '仍有鍵盤輸入等待。'
        )
    return result

def units(work):
    common = f'''RequiresMountsFor=/home/debian/interaction_demo
PartOf=bbai64-interaction.target

[Service]
Type=exec
User=root
WorkingDirectory={work}
Environment=PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
StandardInput=null
StandardOutput=journal
StandardError=journal
KillSignal=SIGINT
KillMode=mixed
TimeoutStopSec=30
SuccessExitStatus=130
'''
    sensor = (
        '# BBAI64 interaction autostart v1\n'
        '[Unit]\n'
        'Description=BBAI64 R5F presence and temperature\n'
        + common + f'''ExecStartPre=/usr/bin/python3 {work}/service_support.py devices sensors
ExecStart=/usr/bin/python3 -u {work}/interaction_sensors.py /home/debian/hx711-mcu2_0.elf
ExecStartPost=/usr/bin/python3 {work}/service_support.py ready
ExecReload=/usr/bin/python3 {work}/service_support.py zero $MAINPID
TimeoutStartSec=210
Restart=no
'''
    )
    video = (
        '# BBAI64 interaction autostart v1\n'
        '[Unit]\n'
        'Description=BBAI64 Hailo and C71 interaction web\n'
        'After=network.target bbai64-interaction-sensors.service\n'
        'Wants=bbai64-interaction-sensors.service\n'
        'StartLimitIntervalSec=180\n'
        'StartLimitBurst=3\n'
        + common + f'''ExecStartPre=/usr/bin/python3 {work}/service_support.py devices vision
ExecStart=/usr/bin/python3 -u {work}/interaction_actions.py
TimeoutStartSec=90
Restart=on-failure
RestartSec=5
'''
    )
    target = '''# BBAI64 interaction autostart v1
[Unit]
Description=BBAI64 complete interaction demonstration
Wants=bbai64-interaction-sensors.service bbai64-interaction-vision.service
After=bbai64-interaction-sensors.service bbai64-interaction-vision.service

[Install]
WantedBy=multi-user.target
'''
    return dict(zip(UNIT_NAMES, (sensor, video, target)))

def main():
    need(os.geteuid() == 0, '請在板子使用 sudo 執行。')
    need(
        'BeagleBone AI-64' in Path('/proc/device-tree/model').read_text(),
        '板子型號不符。'
    )
    guard = (APP / '.autostart-install.lock').open('a')
    fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)

    pointer = APP / 'service-current'
    need(
        not pointer.exists() or pointer.is_symlink(),
        'service-current 已被一般目錄使用。'
    )
    if pointer.is_symlink():
        need(
            pointer.resolve().parent == APP
            and pointer.resolve().name.startswith('service-v1.'),
            '既有服務連結用途不同。'
        )

    names = (
        'interaction_app.py', 'interaction_fusion.py',
        'interaction_pose_app.py', 'interaction_pose_stable.py',
        'interaction_actions.py', 'c71_pose_client.py'
    )
    originals = {name: (APP / name).read_bytes() for name in names}
    for name, expected in EXPECTED_SOURCES.items():
        originals[name] = (HOME_DIR / name).read_bytes()
        need(sha(originals[name]) == expected, '感測程式已變更：' + name)

    firmware_checks = {
        HOME_DIR / 'hx711-mcu2_0.elf':
            '764a9ca96bd0b31790aa30d05d4e817b7619d8a6f6573eaf9db4adab361380f5',
        HOME_DIR / 'c71-pose-v2.xe71':
            '090670a08ad59b341da7219cd6e47c7a6d26e5fe4eae0a110782f28b4ed80399',
        APP / 'models/yolov8n_custom_hailo8l-317500776eaf.hef':
            '317500776eaf552016a08e6171e050a24f6c48c0d22fbe585c17b467a8999674',
    }
    for path, expected in firmware_checks.items():
        need(
            sha(path.read_bytes()) == expected,
            '模型或韌體版本不同：' + str(path)
        )

    need((HOME_DIR / 'c71_pose_selftest.py').is_file(), '缺少 C71 支援程式。')
    need((HOME_DIR / 'c71-pose-v2-build.json').is_file(), '缺少 C71 建置紀錄。')
    need(os.access(APP / 'hailo_worker', os.X_OK), 'Hailo 執行程式缺少或不能執行。')

    unit_root = Path('/etc/systemd/system')
    previous = {}
    for name in UNIT_NAMES:
        probe = subprocess.run(
            ['systemctl', 'is-active', name],
            capture_output=True, text=True
        )
        need(
            probe.stdout.strip() not in
            ('active', 'activating', 'deactivating', 'reloading'),
            '服務正在使用，請先貼回狀態：' + name
        )
        path = unit_root / name
        need(not path.is_symlink(), '服務路徑是既有連結：' + name)
        previous[name] = path.read_bytes() if path.exists() else None
        need(
            previous[name] is None
            or previous[name].startswith(b'# BBAI64 interaction autostart v1\n'),
            '同名服務已有其他用途：' + name
        )

    prepared = {
        name: convert(name, raw.decode())
        for name, raw in originals.items()
    }
    compile(SUPPORT, 'service_support.py', 'exec')

    work = Path(tempfile.mkdtemp(prefix='service-v1.', dir=APP))
    work.chmod(0o755)
    for folder in ('original', 'units', 'previous-units'):
        (work / folder).mkdir()

    for name, raw in originals.items():
        (work / 'original' / name).write_bytes(raw)
        (work / name).write_text(prepared[name], encoding='utf-8')
    (work / 'service_support.py').write_text(SUPPORT, encoding='utf-8')

    config = units(work)
    for name, source in config.items():
        (work / 'units' / name).write_text(source)
        if previous[name] is not None:
            (work / 'previous-units' / name).write_bytes(previous[name])

    subprocess.run(
        ['systemd-analyze', 'verify']
        + [str(work / 'units' / name) for name in UNIT_NAMES],
        check=True
    )

    manifest = dict(
        directory=str(work),
        originals={k:sha(v) for k,v in originals.items()},
        generated={
            p.name:sha(p.read_bytes())
            for p in work.glob('*.py')
        },
        units=list(UNIT_NAMES)
    )
    (work / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n'
    )

    was_enabled = subprocess.run(
        ['systemctl', 'is-enabled', UNIT_NAMES[2]],
        capture_output=True
    ).returncode == 0

    try:
        for name in UNIT_NAMES:
            path = unit_root / name
            temporary = path.with_name(name + '.installing')
            temporary.write_text(config[name])
            temporary.chmod(0o644)
            temporary.replace(path)
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'enable', UNIT_NAMES[2]], check=True)
    except BaseException:
        if not was_enabled:
            subprocess.run(
                ['systemctl', 'disable', UNIT_NAMES[2]],
                capture_output=True
            )
        for name, raw in previous.items():
            path = unit_root / name
            if raw is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(raw)
        subprocess.run(['systemctl', 'daemon-reload'], check=False)
        raise

    temporary = APP / '.service-current.installing'
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(work.name)
    temporary.replace(pointer)

    print('\nPASS：服務版與開機設定已安裝，下次開機生效。')
    print('服務版：', work)
    print('原始程式保留；目前兩個執行中的程式未停止。')
    print('請清空秤台，再執行 sudo reboot。')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit('安裝停止：' + str(exc))
