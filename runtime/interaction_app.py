from service_support import open_log, LOG_DIR, CAMERA, worker_stderr, sensor_signals, take_zero
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import deque
import base64
import json
import os
import select
import subprocess
import threading
import time
import cv2
import numpy as np
from interaction_fusion import Fusion, make_handler
ROOT = Path('/home/debian/interaction_demo')
FUSION = Fusion(ROOT)
BOOT = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
LOCK = threading.Lock()
VIEW = {'valid': False, 'error': '影像初始化中'}
LABELS = {0: '人', 39: '瓶子', 41: '杯子', 67: '手機'}
BOX_LABELS = {0: 'person', 39: 'bottle', 41: 'cup', 67: 'phone'}

def sensor_status():
    try:
        value = json.loads((ROOT / 'status.json').read_text())
        now = time.monotonic_ns()
        age = (now - value['host_monotonic_ns']) / 1000000000.0
        fresh = value['boot_id'] == BOOT and 0 <= age <= 2
        presence = value['presence']
        value['usable'] = fresh and value['phase'] == 'running' and presence.get('valid', False)
        if not value['usable']:
            value['presence'] = {'valid': False, 'present': None}
        temp = value['temperature']
        if not fresh or not temp.get('valid') or (not 0 <= now - temp.get('host_received_ns', 0) <= 2000000000):
            value['temperature'] = {'valid': False}
        value['age_ms'] = round(age * 1000)
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'usable': False, 'phase': 'unavailable', 'error': str(exc), 'presence': {'valid': False, 'present': None}, 'temperature': {'valid': False}}

class Hailo:

    def __init__(self, log):
        model = ROOT.parent / '/home/debian/interaction_demo/models/yolov8n_custom_hailo8l-317500776eaf.hef'
        self.process = subprocess.Popen([str(ROOT / 'hailo_worker'), str(model)], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, bufsize=0)
        self.pending = b''
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)

    def receive(self, deadline):
        fd = self.process.stdout.fileno()
        while b'\n' not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError('Hailo 回覆逾時，請查看 hailo-worker.log')
            chunk = os.read(fd, 65536)
            if not chunk:
                raise RuntimeError('Hailo 工作程式已退出，請查看 hailo-worker.log')
            self.pending += chunk
            if len(self.pending) > 1000000:
                raise RuntimeError('Hailo 回覆長度異常')
        line, self.pending = self.pending.split(b'\n', 1)
        return json.loads(line)

    def infer(self, rgb):
        deadline = time.monotonic() + 15
        data = memoryview(rgb).cast('B')
        fd = self.process.stdin.fileno()
        while len(data):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                raise TimeoutError('送往 Hailo 的影像逾時')
            count = os.write(fd, data)
            data = data[count:]
        return self.receive(deadline)

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=7)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()

class Camera:

    def __init__(self):
        self.cap = cv2.VideoCapture(str(CAMERA), cv2.CAP_V4L2)
        if not self.cap.isOpened():
            raise RuntimeError('無法開啟 /dev/video0')
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'YUYV'))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self.cap.set(cv2.CAP_PROP_FPS, 15)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.condition = threading.Condition()
        self.stopped = False
        self.latest = None
        self.serial = 0
        self.thread = threading.Thread(target=self.capture, daemon=True)
        self.thread.start()

    def capture(self):
        while not self.stopped:
            ok, frame = self.cap.read()
            if not ok:
                break
            with self.condition:
                self.serial += 1
                self.latest = (self.serial, time.monotonic_ns(), frame)
                self.condition.notify_all()

    def next(self, previous):
        with self.condition:
            if not self.condition.wait_for(lambda: self.latest is not None and self.latest[0] != previous, timeout=3):
                raise TimeoutError('攝影機三秒沒有新影像')
            serial, stamp, frame = self.latest
            return (serial, stamp, frame.copy())

    def close(self):
        self.stopped = True
        self.thread.join(timeout=1)
        self.cap.release()
PAGE = '<!doctype html><html lang="zh-Hant"><meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>桌面互動感測台</title><style>\nbody{font:18px sans-serif;background:#101827;color:#edf5ff;margin:24px;max-width:1100px}\nimg{width:100%;max-width:800px;background:#000;border-radius:12px}\n.cards{display:flex;gap:14px;flex-wrap:wrap;margin:18px 0}.card{background:#223147;padding:18px;border-radius:12px;min-width:210px}\nsmall{color:#a9bfd8}#notice{color:#ffc875}h1{font-size:28px}strong{font-size:25px}\n</style><h1>桌面互動感測台</h1>\n<small>Hailo-8L 物件偵測 · R5F 物品有無 · 紅外線表面測溫</small>\n<div class="cards"><div class="card">秤台狀態<br><strong id="load">等待資料</strong></div>\n<div class="card">視野內表面溫度<br><strong id="temp">等待資料</strong></div>\n<div class="card">影像處理速度<br><strong id="speed">等待資料</strong></div></div>\n<p id="notice">正在連線……</p><img id="video" alt="即時攝影機影像">\n<p id="objects"></p><p id="event"></p><small>框線：person＝人、bottle＝瓶子、cup＝杯子、phone＝手機。溫度屬於紅外線感測器的視野範圍。</small>\n<script>\nlet lastFrame=-1;\nconst put=(id,value)=>document.getElementById(id).textContent=value;\nasync function refresh(){try{\nconst r=await fetch(\'/state\',{cache:\'no-store\',signal:AbortSignal.timeout(3000)});\nif(!r.ok)throw Error(\'連線失敗\'); const d=await r.json(),v=d.vision,s=d.sensors,t=s.temperature;\nput(\'load\',s.usable?(s.presence.present?\'有物品\':\'空秤\'):\'暫無有效資料\');\nput(\'temp\',t.valid?t.object_c.toFixed(1)+\' °C\':\'暫無有效資料\');\nput(\'speed\',v.valid?v.fps.toFixed(1)+\' 張／秒\':\'暫無有效影像\');\nput(\'notice\',v.valid?(s.usable?\'影像與感測資料持續更新\':\'影像正常；感測器尚未就緒或資料逾時\'):(v.error||\'影像逾時\'));\nif(v.valid&&v.image&&v.frame!==lastFrame){document.getElementById(\'video\').src=\'data:image/jpeg;base64,\'+v.image;lastFrame=v.frame;}\nput(\'objects\',v.valid?\'偵測：\'+(v.detections.map(x=>x.label+\' \'+Math.round(x.score*100)+\'%\').join(\'、\')||\'未偵測到人、瓶子、杯子或手機\'):\'\');\nput(\'event\',s.usable&&s.last_event?\'最近一次秤台變化：\'+(s.last_event.kind===\'load_on\'?\'放上物品\':\'移走物品\'):\'\');\n}catch(e){put(\'notice\',\'連線已中斷，請查看板子終端機\');put(\'load\',\'暫無有效資料\');put(\'temp\',\'暫無有效資料\');put(\'speed\',\'暫無有效影像\');}\nsetTimeout(refresh,50);}refresh();\n</script></html>'.encode()

class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == '/':
            body, kind = (PAGE, 'text/html; charset=utf-8')
        elif self.path == '/state':
            with LOCK:
                view = dict(VIEW)
            if time.monotonic_ns() - view.get('host_received_ns', 0) > 2000000000:
                view.update(valid=False, error='影像資料逾時')
            body = json.dumps({'vision': view, 'sensors': sensor_status()}, ensure_ascii=False, allow_nan=False).encode()
            kind = 'application/json; charset=utf-8'
        else:
            self.send_error(404)
            return
        try:
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass
Handler = make_handler(LOCK, lambda: VIEW, sensor_status, FUSION)

def main():
    global VIEW
    camera = worker = server = web_thread = None
    cv2.setNumThreads(2)
    with (ROOT / 'hailo-worker.log').open('w') as errors, (ROOT / ('vision-' + str(time.time_ns()) + '.jsonl')).open('x', encoding='utf-8', buffering=1) as records:
        try:
            server = ThreadingHTTPServer(('0.0.0.0', 8080), Handler)
            worker = Hailo(errors)
            ready = worker.receive(time.monotonic() + 30)
            if not ready.get('ready'):
                raise RuntimeError('Hailo 沒有回報就緒')
            print('Hailo 模型已就緒。')
            camera = Camera()
            web_thread = threading.Thread(target=server.serve_forever, daemon=True)
            web_thread.start()
            print('展示網址：http://192.168.1.253:8080')
            print('本視窗按 Ctrl+C 結束影像；感測器視窗繼續運作。')
            serial, last_log = (0, 0.0)
            times = deque(maxlen=30)
            while True:
                serial, stamp, frame = camera.next(serial)
                h, w = frame.shape[:2]
                scale = min(640 / w, 640 / h)
                nw, nh = (round(w * scale), round(h * scale))
                left, top = ((640 - nw) // 2, (640 - nh) // 2)
                padded = np.full((640, 640, 3), 114, dtype=np.uint8)
                padded[top:top + nh, left:left + nw] = cv2.resize(frame, (nw, nh))
                result = worker.infer(cv2.cvtColor(padded, cv2.COLOR_BGR2RGB))
                detections = []
                for detection in result['detections']:
                    ymin, xmin, ymax, xmax = detection['box']
                    x1, x2 = [int(np.clip((x * 640 - left) / scale, 0, w - 1)) for x in (xmin, xmax)]
                    y1, y2 = [int(np.clip((y * 640 - top) / scale, 0, h - 1)) for y in (ymin, ymax)]
                    if x2 <= x1 or y2 <= y1:
                        continue
                    cls, score = (detection['class_id'], detection['score'])
                    detections.append(dict(class_id=cls, label=LABELS[cls], score=score, box=[x1, y1, x2, y2]))
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (80, 230, 100), 2)
                    if cls in (39, 41, 67):
                        cv2.circle(frame, ((x1 + x2) // 2, y2), 4, (0, 220, 255), -1)
                    cv2.putText(frame, f'{BOX_LABELS[cls]} {score:.2f}', (x1, max(18, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (80, 230, 100), 2)
                ok, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if not ok:
                    raise RuntimeError('影像壓縮失敗')
                now = time.monotonic()
                times.append(now)
                fps = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 else 0.0
                sensors = sensor_status()
                fusion = FUSION.update(detections, w, h, stamp, serial, sensors)
                view = dict(valid=True, frame=serial, host_received_ns=stamp, fps=fps, infer_ms=result['infer_ms'], detections=detections, fusion=fusion)
                with LOCK:
                    VIEW = dict(view, image=base64.b64encode(jpeg).decode('ascii'))
                if now - last_log >= 1:
                    records.write(json.dumps(dict(host_unix_ns=time.time_ns(), vision=view, sensors=sensors), ensure_ascii=False) + '\n')
                    print(f'影像={serial}｜處理={fps:.1f} 張/秒｜偵測框={len(detections)}', flush=True)
                    last_log = now
        finally:
            if server is not None:
                if web_thread is not None:
                    server.shutdown()
                server.server_close()
            if camera is not None:
                camera.close()
            if worker is not None:
                worker.close()
if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n影像展示已停止。')
    except Exception as exc:
        print('展示停止：', exc)
        log = ROOT / 'hailo-worker.log'
        if log.exists():
            print(log.read_text(errors='replace')[-3000:])
        raise SystemExit(1)
    finally:
        FUSION.close()
