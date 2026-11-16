from service_support import open_log, LOG_DIR, CAMERA, worker_stderr, sensor_signals, take_zero
from pathlib import Path
from collections import deque
import json
import math
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler
NAMES = {39: '瓶子', 41: '杯子', 67: '手機'}

def checked_roi(value):
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError('區域需要四個座標')
    values = [float(x) for x in value]
    if not all((math.isfinite(x) and 0 <= x <= 1 for x in values)):
        raise ValueError('座標必須介於 0 與 1')
    x1, y1, x2, y2 = values
    if x2 - x1 < 0.03 or y2 - y1 < 0.03:
        raise ValueError('選取區域太小，請重新選取')
    return values

def overlap(a, b):
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1])
    return intersection / max(area - intersection, 1)

class Fusion:

    def __init__(self, folder):
        self.folder = Path(folder)
        self.lock = threading.Lock()
        self.roi = None
        self.revision = 0
        self.events = deque(maxlen=12)
        self.sequence = 0
        self.reset()
        config = self.folder / 'scale_roi.json'
        if config.exists():
            try:
                self.roi = checked_roi(json.loads(config.read_text())['roi'])
            except (OSError, ValueError, KeyError, TypeError):
                print('原區域設定無效，請在網頁重新框選。')
        self.log_path = LOG_DIR / 'events.jsonl'
        self.log = open_log('events')
        print('互動事件紀錄：', self.log_path)

    def reset(self):
        self.run = None
        self.previous = None
        self.last_step = 0
        self.last_event_id = 0
        self.pending = None
        self.active = None
        self.candidate = None
        self.hits = 0
        self.since = 0

    def config(self):
        with self.lock:
            return {'roi': self.roi, 'revision': self.revision}

    def set_roi(self, value):
        value = checked_roi(value)
        with self.lock:
            temporary = self.folder / 'scale_roi.json.tmp'
            temporary.write_text(json.dumps({'schema': 1, 'roi': value}) + '\n')
            os.replace(temporary, self.folder / 'scale_roi.json')
            self.roi = value
            self.revision += 1
            self.reset()
            return {'roi': self.roi, 'revision': self.revision}

    def emit(self, kind, observation, sensor, frame_id, now):
        self.sequence += 1
        name = NAMES[observation['class_id']] if observation else None
        if kind == 'load_on':
            message = '放上：' + name + '（影像推定）' if name else '放上物品（類別未確認）'
        else:
            message = '移走物品；先前辨識為' + name if name else '移走物品（類別未確認）'
        source = sensor.get('last_event') or {}
        record = {'schema': 1, 'id': self.sequence, 'unix_ms': time.time_ns() // 1000000, 'host_monotonic_ns': now, 'kind': kind, 'message': message, 'observation': observation, 'video_frame': frame_id, 'roi': self.roi, 'sensor_run_id': sensor.get('run_id'), 'sensor_event': source if source.get('kind') == kind else None, 'presence': sensor.get('presence'), 'temperature': sensor.get('temperature')}
        self.log.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
        self.events.appendleft({k: record[k] for k in ('id', 'unix_ms', 'kind', 'message')})
        print('互動事件：', message, flush=True)

    def update(self, detections, width, height, stamp, frame_id, sensor):
        now = time.monotonic_ns()
        with self.lock:
            presence = sensor.get('presence', {})
            current = presence.get('present')
            valid = sensor.get('usable', False) and isinstance(current, bool) and (0 <= now - stamp <= 1000000000) and (0 <= now - sensor.get('host_monotonic_ns', 0) <= 1000000000) and (abs(stamp - sensor.get('host_monotonic_ns', 0)) <= 1000000000)

            def result(message, usable):
                return {'message': message, 'valid': bool(usable), 'present': current, 'sensor_run_id': sensor.get('run_id'), 'revision': self.revision, 'host_monotonic_ns': now, 'events': list(self.events)}
            if not valid or self.roi is None:
                self.reset()
                return result('請先設定秤台區域' if self.roi is None else '資料不足，暫不判斷', False)
            event_id = (sensor.get('last_event') or {}).get('id', 0)
            continuous = self.run == sensor.get('run_id') and self.previous is not None and (now - self.last_step <= 1000000000) and (self.last_event_id <= event_id <= self.last_event_id + 1)
            if not continuous:
                self.reset()
                self.run = sensor.get('run_id')
                self.previous = current
            if current != self.previous:
                if self.pending is not None:
                    old_sensor, old_frame, _ = self.pending
                    self.emit('load_on', None, old_sensor, old_frame, now)
                    self.pending = None
                if current:
                    self.active = None
                    self.pending = (sensor, frame_id, now + 2000000000)
                else:
                    observation = self.active
                    if observation and now - observation['seen_ns'] > 2000000000:
                        observation = None
                    self.emit('load_off', observation, sensor, frame_id, now)
                    self.active = None
                self.candidate = None
                self.hits = 0
            self.previous = current
            self.last_event_id = event_id
            self.last_step = now
            x1, y1, x2, y2 = self.roi
            candidates = []
            for item in detections:
                if item['class_id'] not in NAMES or item['score'] < 0.45:
                    continue
                box = item['box']
                foot_x = (box[0] + box[2]) / (2 * width)
                foot_y = box[3] / height
                if x1 <= foot_x <= x2 and y1 <= foot_y <= y2:
                    candidates.append(item)
            candidate = candidates[0] if len(candidates) == 1 else None
            if len(candidates) > 1:
                self.active = None
            if candidate is not None:
                if self.candidate is not None and self.candidate['class_id'] == candidate['class_id'] and (overlap(self.candidate['box'], candidate['box']) >= 0.25):
                    self.hits += 1
                else:
                    self.hits = 1
                    self.since = now
                self.candidate = candidate
            else:
                self.candidate = None
                self.hits = 0
            stable = candidate is not None and self.hits >= 3 and (now - self.since >= 250000000)
            if current and stable:
                self.active = dict(candidate, seen_ns=now, frame=frame_id)
                if self.pending is not None:
                    old_sensor, _, _ = self.pending
                    self.emit('load_on', self.active, old_sensor, frame_id, now)
                    self.pending = None
            if self.pending is not None and now >= self.pending[2]:
                old_sensor, old_frame, _ = self.pending
                self.emit('load_on', None, old_sensor, old_frame, now)
                self.pending = None
            if not current:
                message = '空秤'
            elif stable:
                message = '秤台有' + NAMES[candidate['class_id']] + '（影像推定）'
            elif len(candidates) > 1:
                message = '有物品；區域內有多個候選，類別未確認'
            else:
                message = '有物品；等待影像確認或物品被遮擋'
            return result(message, True)

    def close(self):
        with self.lock:
            self.log.close()
PAGE = '<!doctype html><html lang="zh-Hant"><meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>桌面互動感測台</title><style>\nbody{font:17px sans-serif;background:#101827;color:#edf5ff;margin:24px;max-width:1100px}\nh1{font-size:28px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:18px 0}\n.card{background:#223147;padding:16px;border-radius:12px}strong{display:block;font-size:24px;margin-top:8px}\n.stage{position:relative;max-width:800px;line-height:0}.stage img{display:block;width:100%;border-radius:10px}\ncanvas{position:absolute;inset:0;width:100%;height:100%}button{font:inherit;padding:8px 14px;margin:8px 8px 8px 0;cursor:pointer}\n#fusion{padding:14px;background:#223147;border-left:4px solid #ffd166;max-width:772px}small{color:#a9bfd8}\n#notice,#tip{color:#ffd166}li{margin:8px 0}\n</style><h1>桌面互動感測台</h1>\n<small>Hailo-8L 物件偵測 · R5F 物品有無 · 紅外線表面測溫</small>\n<div class="cards"><div class="card">秤台狀態<strong id="load">等待資料</strong></div>\n<div class="card">視野內表面溫度<strong id="temp">等待資料</strong></div>\n<div class="card">影像處理速度<strong id="speed">等待資料</strong></div></div>\n<p id="notice">正在連線……</p><p id="fusion">正在初始化互動判斷</p>\n<button id="choose">設定秤台區域</button><button id="clear">清除區域</button>\n<p id="tip">先固定攝影機，再框選秤台上表面，邊緣保留少量空間。</p>\n<div class="stage"><img id="video" alt="即時影像"><canvas id="roi"></canvas></div>\n<p id="objects"></p><small>黃色區域用於比對物品框底部的小黃點；一次操作一個物品。紅外線溫度代表測溫模組視野內的表面。</small>\n<h2>最近事件</h2><ol id="events"><li>尚無新事件</li></ol>\n<script>\nconst put=(id,text)=>document.getElementById(id).textContent=text;\nconst video=document.getElementById(\'video\'),canvas=document.getElementById(\'roi\');\nlet lastFrame=-1,roi=null,selecting=false,first=null,hover=null,saving=false,eventKey=null;\nfunction draw(){\nif(!video.naturalWidth)return;\ncanvas.width=video.naturalWidth;canvas.height=video.naturalHeight;\nconst c=canvas.getContext(\'2d\');c.lineWidth=3;c.strokeStyle=\'#ffd166\';\nfunction box(r){c.strokeRect(r[0]*canvas.width,r[1]*canvas.height,(r[2]-r[0])*canvas.width,(r[3]-r[1])*canvas.height);}\nif(roi)box(roi);\nif(first){c.fillStyle=\'#39d7ff\';c.fillRect(first[0]*canvas.width-4,first[1]*canvas.height-4,8,8);\nif(hover){c.setLineDash([6,4]);box([Math.min(first[0],hover[0]),Math.min(first[1],hover[1]),Math.max(first[0],hover[0]),Math.max(first[1],hover[1])]);}}\n}\nvideo.onload=draw;\nfunction point(e){const r=canvas.getBoundingClientRect();return [Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))];}\ndocument.getElementById(\'choose\').onclick=()=>{selecting=true;first=hover=null;canvas.style.cursor=\'crosshair\';put(\'tip\',\'在影像上點選區域的第一個角落。\');draw();};\nasync function save(value){\nif(saving)return;saving=true;\ntry{const r=await fetch(\'/roi\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({roi:value}),signal:AbortSignal.timeout(3000)});\nconst d=await r.json();if(!r.ok)throw Error(d.error||\'儲存失敗\');\nroi=d.roi;selecting=false;first=hover=null;canvas.style.cursor=\'default\';\nput(\'tip\',roi?\'區域已儲存。物品放在秤台時，小黃點應落在黃色區域內。\':\'區域已清除，請重新設定。\');draw();\n}catch(e){put(\'tip\',\'設定失敗：\'+e.message);}finally{saving=false;}}\ndocument.getElementById(\'clear\').onclick=()=>save(null);\ncanvas.onclick=e=>{if(!selecting||saving)return;const p=point(e);\nif(!first){first=p;put(\'tip\',\'再點選對角，完成區域設定。\');draw();return;}\nsave([Math.min(first[0],p[0]),Math.min(first[1],p[1]),Math.max(first[0],p[0]),Math.max(first[1],p[1])]);};\ncanvas.onmousemove=e=>{if(selecting&&first){hover=point(e);draw();}};\nasync function refresh(){try{\nconst r=await fetch(\'/state\',{cache:\'no-store\',signal:AbortSignal.timeout(3000)});\nif(!r.ok)throw Error(\'連線失敗\');const d=await r.json(),v=d.vision,s=d.sensors,t=s.temperature,f=d.fusion;\nroi=f.roi;put(\'load\',s.usable?(s.presence.present?\'有物品\':\'空秤\'):\'暫無有效資料\');\nput(\'temp\',t.valid?t.object_c.toFixed(1)+\' °C\':\'暫無有效資料\');put(\'speed\',v.valid?v.fps.toFixed(1)+\' 張／秒\':\'暫無有效影像\');\nput(\'notice\',v.valid?(s.usable?\'影像與感測資料持續更新\':\'感測資料未就緒\'):\'影像資料未就緒\');put(\'fusion\',f.message);\nif(v.valid&&v.image&&v.frame!==lastFrame){video.src=\'data:image/jpeg;base64,\'+v.image;lastFrame=v.frame;}\nput(\'objects\',v.valid?\'偵測：\'+(v.detections.map(x=>x.label+\' \'+Math.round(x.score*100)+\'%\').join(\'、\')||\'目前沒有目標物品\'):\'\');\nconst key=JSON.stringify(f.events||[]);\nif(key!==eventKey){eventKey=key;const list=document.getElementById(\'events\');list.replaceChildren();\nfor(const e of (f.events||[])){const li=document.createElement(\'li\');li.textContent=new Date(e.unix_ms).toLocaleTimeString()+\'\u3000\'+e.message;list.appendChild(li);}\nif(!list.children.length){const li=document.createElement(\'li\');li.textContent=\'尚無新事件\';list.appendChild(li);}}\n}catch(e){put(\'notice\',\'連線中斷，請查看影像程式視窗\');put(\'fusion\',\'資料不足，暫不判斷\');put(\'load\',\'暫無有效資料\');put(\'temp\',\'暫無有效資料\');put(\'speed\',\'暫無有效影像\');}\nsetTimeout(refresh,50);}refresh();\n</script></html>'.encode()

def make_handler(lock, get_view, get_sensors, engine):

    class Handler(BaseHTTPRequestHandler):

        def reply(self, code, data, kind='application/json; charset=utf-8'):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            try:
                self.send_response(code)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path == '/':
                self.reply(200, PAGE, 'text/html; charset=utf-8')
                return
            if self.path != '/state':
                self.reply(404, {'error': '找不到頁面'})
                return
            with lock:
                view = dict(get_view())
            now = time.monotonic_ns()
            if now - view.get('host_received_ns', 0) > 2000000000:
                view['valid'] = False
            sensor = get_sensors()
            fusion = dict(view.pop('fusion', {}))
            config = engine.config()
            same_config = fusion.get('revision') == config['revision']
            fusion.update(config)
            if config['roi'] is None:
                fusion.update(valid=False, message='請先設定秤台區域')
            elif not (view.get('valid') and sensor.get('usable')):
                fusion.update(valid=False, message='資料不足，暫不判斷')
            elif not same_config or fusion.get('sensor_run_id') != sensor.get('run_id') or fusion.get('present') != sensor['presence'].get('present') or (now - fusion.get('host_monotonic_ns', 0) > 1000000000):
                fusion.update(valid=False, message='影像與感測資料同步中')
            self.reply(200, {'vision': view, 'sensors': sensor, 'fusion': fusion})

        def do_POST(self):
            if self.path != '/roi':
                self.reply(404, {'error': '找不到操作'})
                return
            try:
                if self.headers.get_content_type() != 'application/json':
                    raise ValueError('需要 JSON 格式')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096:
                    raise ValueError('設定資料長度不符')
                data = json.loads(self.rfile.read(length))
                self.reply(200, engine.set_roi(data['roi']))
            except (ValueError, TypeError, KeyError) as exc:
                self.reply(400, {'error': str(exc)})
            except OSError as exc:
                self.reply(500, {'error': str(exc)})

        def log_message(self, *args):
            pass
    return Handler
