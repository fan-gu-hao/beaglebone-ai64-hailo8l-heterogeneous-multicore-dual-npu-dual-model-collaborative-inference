from service_support import open_log, LOG_DIR, CAMERA, worker_stderr, sensor_signals, take_zero
from pathlib import Path
from collections import deque
from http.server import ThreadingHTTPServer
import base64
import json
import os
import sys
import threading
import time
sys.dont_write_bytecode = True
import cv2
import numpy as np
import interaction_app as base
import interaction_fusion as fusion_ui
from c71_pose_client import PoseClient
ROOT = Path('/home/debian/interaction_demo')
LOCK = threading.Lock()
VIEW = {'valid': False, 'error': '影像與姿態初始化中'}
POINT_SCORE = 0.25
EDGES = ((0, 1), (0, 2), (1, 3), (2, 4), (5, 6), (5, 7), (7, 9), (6, 8), (8, 10), (5, 11), (6, 12), (11, 12), (11, 13), (13, 15), (12, 14), (14, 16))

def install_page():
    page = fusion_ui.PAGE.decode('utf-8')
    changes = [('Hailo-8L 物件偵測 · R5F 物品有無 · 紅外線表面測溫', 'Hailo-8L 物件偵測 · C71／MMA 人體姿態 · R5F 物品有無 · 紅外線測溫'), ('<strong id="speed">等待資料</strong></div></div>', '<strong id="speed">等待資料</strong></div><div class="card">C71 人體姿態<strong id="pose">等待人物</strong></div></div><p id="pose-detail"></p>'), ('黃色區域用於比對物品框底部的小黃點；一次操作一個物品。', '青色為姿態骨架；目前處理畫面中最大的人物。黃色區域用於比對物品框底部的小黃點；一次操作一個物品。'), ("put('fusion',f.message);", "put('fusion',f.message);\nconst p=v.pose||{};\nput('pose',!v.valid?'暫無有效影像':p.valid?p.visible+'/17 個關節':(p.message||'等待人物'));\nput('pose-detail',v.valid&&p.valid?\n  '傳圖 '+p.upload_ms.toFixed(1)+' 毫秒｜推論請求往返 '+p.run_rpc_ms.toFixed(1)+\n  ' 毫秒｜骨架與影像來自同一張圖片':(v.valid?(p.error||'請讓頭、肩膀、手肘及手腕進入畫面。'):''));"), ("}catch(e){put('notice',", "}catch(e){put('pose','暫無有效資料');put('pose-detail','');put('notice',")]
    for old, new in changes:
        if page.count(old) != 1:
            raise RuntimeError('既有網頁版本不同，停止套用：' + old[:45])
        page = page.replace(old, new, 1)
    fusion_ui.PAGE = page.encode('utf-8')

def make_input(frame):
    h, w = frame.shape[:2]
    scale = min(640 / w, 640 / h)
    nw, nh = (round(w * scale), round(h * scale))
    left, top = ((640 - nw) // 2, (640 - nh) // 2)
    padded = np.full((640, 640, 3), 114, np.uint8)
    padded[top:top + nh, left:left + nw] = cv2.resize(frame, (nw, nh))
    return (cv2.cvtColor(padded, cv2.COLOR_BGR2RGB), (scale, left, top))

def decode(result, frame, geometry):
    h, w = frame.shape[:2]
    scale, left, top = geometry
    detections, people = ([], [])
    for d in result['detections']:
        cls, score = (int(d['class_id']), float(d['score']))
        box = np.asarray(d['box'], dtype=np.float64)
        if cls not in base.LABELS or not np.isfinite(box).all() or (not np.isfinite(score)):
            continue
        ymin, xmin, ymax, xmax = np.clip(box * 640, 0, 640)
        x1, x2 = [int(np.clip((x - left) / scale, 0, w - 1)) for x in (xmin, xmax)]
        y1, y2 = [int(np.clip((y - top) / scale, 0, h - 1)) for y in (ymin, ymax)]
        if x2 <= x1 or y2 <= y1:
            continue
        detections.append(dict(class_id=cls, label=base.LABELS[cls], score=score, box=[x1, y1, x2, y2]))
        if cls == 0 and score >= 0.5 and (xmax - xmin >= 32) and (ymax - ymin >= 64):
            people.append(((xmax - xmin) * (ymax - ymin), [xmin, ymin, xmax, ymax]))
    return (detections, max(people, key=lambda p: p[0])[1] if people else None)

def crop_person(rgb, box):
    x1, y1, x2, y2 = box
    x, y, w, h = np.asarray([x1, y1, x2 - x1, y2 - y1], np.float32)
    center = np.asarray([x + w * 0.5, y + h * 0.5], np.float32)
    aspect = 192 / 256
    if w > aspect * h:
        h = w / aspect
    elif w < aspect * h:
        w = h * aspect
    crop_scale = np.asarray([w / 200, h / 200], np.float32) * 1.25
    width = (crop_scale * 200)[0]
    src = np.zeros((3, 2), np.float32)
    src[0] = center
    src[1] = center + np.asarray([0, -width * 0.5], np.float32)
    direction = src[0] - src[1]
    src[2] = src[1] + np.asarray([-direction[1], direction[0]], np.float32)
    dst = np.asarray([[96, 128], [96, 32], [0, 32]], np.float32)
    matrix = cv2.getAffineTransform(src, dst)
    patch = cv2.warpAffine(rgb, matrix, (192, 256), flags=cv2.INTER_LINEAR)
    data = np.ascontiguousarray(patch.transpose(2, 0, 1)).tobytes()
    if len(data) != 147456:
        raise RuntimeError('人物裁切尺寸不符。')
    return (data, cv2.invertAffineTransform(matrix))

def project_keys(answer, inverse, geometry, shape):
    points = np.asarray(answer['keypoints'], np.float64)
    if points.shape != (17, 3) or not np.isfinite(points).all():
        raise RuntimeError('C71 關節格式不符。')
    xy = np.column_stack((points[:, :2], np.ones(17))) @ inverse.T
    scale, left, top = geometry
    xy = (xy - np.asarray([left, top])) / scale
    h, w = shape[:2]
    valid = (points[:, 2] >= POINT_SCORE) & (points[:, 0] >= 0) & (points[:, 0] < 192) & (points[:, 1] >= 0) & (points[:, 1] < 256) & (xy[:, 0] >= 0) & (xy[:, 0] < w) & (xy[:, 1] >= 0) & (xy[:, 1] < h)
    return dict(valid=True, visible=int(valid.sum()), keypoints=np.column_stack((xy, points[:, 2])).tolist(), drawn=valid.tolist(), upload_ms=answer['upload_ms'], run_rpc_ms=answer['run_rpc_ms'], total_ms=answer['total_ms'], rpc_frame=answer['frame'])

class Publisher:

    def __init__(self, records):
        self.records = records
        self.times = deque(maxlen=30)
        self.last_frame = -1
        self.last_log = 0.0

    def publish(self, job, pose):
        global VIEW
        if time.monotonic_ns() - job['stamp'] > 1000000000:
            return False
        image = job['image'].copy()
        for d in job['detections']:
            x1, y1, x2, y2 = d['box']
            cls = d['class_id']
            cv2.rectangle(image, (x1, y1), (x2, y2), (80, 230, 100), 2)
            cv2.putText(image, f"{base.BOX_LABELS[cls]} {d['score']:.2f}", (x1, max(18, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (80, 230, 100), 2)
            if cls in (39, 41, 67):
                cv2.circle(image, ((x1 + x2) // 2, y2), 4, (0, 220, 255), -1)
        if pose.get('valid'):
            points, valid = (pose['keypoints'], pose['drawn'])
            coords = [(round(x), round(y)) for x, y, score in points]
            for a, b in EDGES:
                if valid[a] and valid[b]:
                    cv2.line(image, coords[a], coords[b], (255, 220, 40), 2, cv2.LINE_AA)
            for index, p in enumerate(coords):
                if valid[index]:
                    cv2.circle(image, p, 4, (255, 220, 40), -1, cv2.LINE_AA)
        ok, jpeg = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            raise RuntimeError('影像壓縮失敗。')
        encoded = base64.b64encode(jpeg).decode('ascii')
        with LOCK:
            if job['serial'] <= self.last_frame or time.monotonic_ns() - job['stamp'] > 1000000000:
                return False
            now = time.monotonic()
            self.times.append(now)
            fps = (len(self.times) - 1) / (self.times[-1] - self.times[0]) if len(self.times) > 1 else 0.0
            view = dict(valid=True, frame=job['serial'], host_received_ns=job['stamp'], fps=fps, infer_ms=job['infer_ms'], detections=job['detections'], fusion=job['fusion'], pose=pose)
            VIEW = dict(view, image=encoded)
            self.last_frame = job['serial']
            if now - self.last_log >= 1:
                self.records.write(json.dumps(dict(host_unix_ns=time.time_ns(), vision=view, sensors=job['sensors']), ensure_ascii=False, allow_nan=False) + '\n')
                detail = f"關節={pose['visible']}/17｜傳圖={pose['upload_ms']:.1f} 毫秒｜推論往返={pose['run_rpc_ms']:.1f} 毫秒" if pose.get('valid') else pose.get('message', '等待人物')
                print(f'影像={fps:.1f} 張／秒｜{detail}', flush=True)
                self.last_log = now
        return True

class PoseWorker:

    def __init__(self, client, publish):
        self.client, self.publish = (client, publish)
        self.condition = threading.Condition()
        self.stop = threading.Event()
        self.pending = None
        self.error = ''
        original_rpc = client.rpc

        def live_rpc(message, timeout=10):
            if self.stop.is_set():
                raise InterruptedError('姿態執行緒結束')
            return original_rpc(message, timeout=min(timeout, 3.0))
        client.rpc = live_rpc
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def submit(self, job):
        with self.condition:
            if self.error or self.stop.is_set():
                return False
            self.pending = job
            self.condition.notify()
            return True

    def clear(self):
        with self.condition:
            self.pending = None

    def run(self):
        try:
            while not self.stop.is_set():
                with self.condition:
                    self.condition.wait_for(lambda: self.stop.is_set() or self.pending is not None)
                    if self.stop.is_set():
                        break
                    job, self.pending = (self.pending, None)
                if time.monotonic_ns() - job['stamp'] > 500000000:
                    continue
                data, inverse = crop_person(job['rgb'], job['person'])
                answer = self.client.infer(data)
                pose = project_keys(answer, inverse, job['geometry'], job['image'].shape)
                if not self.stop.is_set():
                    self.publish(job, pose)
        except Exception as exc:
            if not self.stop.is_set():
                with self.condition:
                    self.error = str(exc)
                    self.pending = None
                print('C71 姿態停止：' + str(exc) + '；影像與感測顯示繼續。', flush=True)
        finally:
            self.client.close()

    def close(self):
        self.stop.set()
        with self.condition:
            self.pending = None
            self.condition.notify_all()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            print('姿態通訊尚在結束；C71 未停止。', flush=True)

def main():
    if os.geteuid() != 0:
        raise RuntimeError('請加 sudo，並在 BeagleBone AI-64 執行。')
    install_page()
    cv2.setNumThreads(2)
    camera = hailo = server = web = pose = client = None
    with worker_stderr() as errors, open_log('vision-pose') as records:
        try:
            handler = fusion_ui.make_handler(LOCK, lambda: VIEW, base.sensor_status, base.FUSION)
            server = ThreadingHTTPServer(('0.0.0.0', 8080), handler)
            client = PoseClient()
            print('C71 第二版已連接，沿用目前的模型服務。', flush=True)
            hailo = base.Hailo(errors)
            if not hailo.receive(time.monotonic() + 30).get('ready'):
                raise RuntimeError('Hailo 未回報就緒。')
            camera = base.Camera()
            publisher = Publisher(records)
            pose = PoseWorker(client, publisher.publish)
            web = threading.Thread(target=server.serve_forever, daemon=True)
            web.start()
            print('展示網址：http://192.168.1.253:8080', flush=True)
            print('開機展示服務已啟動。', flush=True)
            serial = 0
            while True:
                serial, stamp, frame = camera.next(serial)
                rgb, geometry = make_input(frame)
                result = hailo.infer(rgb)
                detections, person = decode(result, frame, geometry)
                sensors = base.sensor_status()
                h, w = frame.shape[:2]
                fusion = base.FUSION.update(detections, w, h, stamp, serial, sensors)
                job = dict(serial=serial, stamp=stamp, image=frame, rgb=rgb, geometry=geometry, detections=detections, person=person, infer_ms=result['infer_ms'], sensors=sensors, fusion=fusion)
                if person is not None and pose.submit(job):
                    continue
                pose.clear()
                publisher.publish(job, dict(valid=False, message='姿態通訊中斷' if pose.error else '等待清楚的人物', error=pose.error))
        finally:
            if pose is not None:
                pose.close()
            elif client is not None:
                client.close()
            if server is not None:
                if web is not None:
                    server.shutdown()
                server.server_close()
            if camera is not None:
                camera.close()
            if hailo is not None:
                hailo.close()
if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n展示已停止；C71 保持運行。')
    except Exception as exc:
        print('\n展示停止：', exc)
        raise SystemExit(1)
    finally:
        base.FUSION.close()
