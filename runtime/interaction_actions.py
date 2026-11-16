from service_support import open_log, LOG_DIR, CAMERA, worker_stderr, sensor_signals, take_zero
from collections import deque
import json
import math
import sys
import threading
import time
import types
import cv2
sys.dont_write_bytecode = True
import interaction_pose_stable as stable
app = stable.app

class HandEvidence:

    def __init__(self):
        self.lock = threading.Lock()
        self.context = None
        self.identity = None
        self.last_frame = -1
        self.last_stamp = 0
        self.history = deque(maxlen=150)
        self.counters = [0, 0]
        self.active = [False, False]
        self.since = [0, 0]

    @staticmethod
    def zone(roi):
        if roi is None:
            return None
        x1, y1, x2, y2 = roi
        return [max(0, x1 - 0.06), max(0, y1 - 0.18), min(1, x2 + 0.06), min(1, y2 + 0.04)]

    def observe(self, job, pose, config):
        stamp = job['stamp']
        now = time.monotonic_ns()
        if not 0 <= now - stamp <= 1000000000:
            return None
        with self.lock:
            if job['serial'] <= self.last_frame:
                return None
            self.last_frame = job['serial']
            sensor = job['sensors']
            context = (config['revision'], sensor.get('run_id'))
            identity = pose.get('person_id')
            switched = identity is not None and self.identity is not None and (identity != self.identity)
            if context != self.context or switched:
                self.history.clear()
                self.counters = [0, 0]
                self.active = [False, False]
                self.since = [0, 0]
            if not 0 < stamp - self.last_stamp <= 300000000:
                self.counters = [0, 0]
                self.active = [False, False]
                self.since = [0, 0]
            self.context, self.last_stamp = (context, stamp)
            if identity is not None:
                self.identity = identity
            while self.history and stamp - self.history[0]['stamp_ns'] > 5000000000:
                self.history.popleft()
            area = self.zone(config['roi'])
            result = dict(revision=config['revision'], zone=area, valid=False, near=False, wrists=[], message='手腕未確認')
            if area is None:
                self.history.clear()
                result['message'] = '請先設定秤台區域'
                return result
            if job['fusion'].get('revision') != config['revision']:
                self.history.clear()
                self.counters = [0, 0]
                self.active = [False, False]
                self.since = [0, 0]
                result['message'] = '區域設定同步中'
                return result
            h, w = job['image'].shape[:2]
            wrists, near_names = ([], [])
            for slot, (index, name) in enumerate(((9, '左手腕'), (10, '右手腕'))):
                fresh = pose.get('valid') and len(pose.get('drawn', [])) == 17 and pose['drawn'][index]
                if fresh:
                    x, y, score = pose['keypoints'][index]
                    fresh = all((math.isfinite(v) for v in (x, y, score))) and score >= 0.3 and (0 <= x < w) and (0 <= y < h)
                if not fresh:
                    self.counters[slot] = 0
                    self.active[slot] = False
                    self.since[slot] = 0
                    continue
                point = [x / w, y / h]
                margin = 0.025 if self.active[slot] else 0
                inside = area[0] - margin <= point[0] <= area[2] + margin and area[1] - margin <= point[1] <= area[3] + margin
                if inside:
                    if self.counters[slot] == 0:
                        self.since[slot] = stamp
                    self.counters[slot] += 1
                    self.active[slot] = self.counters[slot] >= 2 and stamp - self.since[slot] >= 80000000
                else:
                    self.counters[slot] = 0
                    self.active[slot] = False
                    self.since[slot] = 0
                near = self.active[slot]
                wrists.append(dict(name=name, x=x, y=y, score=score, near=near))
                if near:
                    near_names.append(name)
            result.update(valid=bool(wrists), near=bool(near_names), wrists=wrists)
            if near_names:
                result['message'] = '、'.join(near_names) + '接近秤台（影像推定）'
            elif wrists:
                result['message'] = '尚未確認手腕接近'
            if not sensor.get('usable'):
                self.history.clear()
            elif near_names:
                self.history.append(dict(stamp_ns=stamp, video_frame=job['serial'], person_id=identity, revision=config['revision'], sensor_run_id=sensor.get('run_id'), wrists=[p for p in wrists if p['near']]))
            return result

    def match(self, sensor, revision):
        stamp = sensor.get('host_monotonic_ns')
        if not isinstance(stamp, int) or stamp <= 0:
            return None
        with self.lock:
            candidates = [item for item in self.history if item['revision'] == revision and item['sensor_run_id'] == sensor.get('run_id') and (-800000000 <= item['stamp_ns'] - stamp <= 200000000)]
            if not candidates:
                return None
            item = min(candidates, key=lambda p: abs(p['stamp_ns'] - stamp))
            return dict(item, time_difference_ms=(item['stamp_ns'] - stamp) / 1000000.0)
HANDS = HandEvidence()

def emit_with_hand(self, kind, observation, sensor, frame_id, now):
    self.sequence += 1
    name = app.fusion_ui.NAMES[observation['class_id']] if observation else None
    if kind == 'load_on':
        message = '放上：' + name + '（影像推定）' if name else '放上物品（類別未確認）'
    else:
        message = '移走物品；先前辨識為' + name if name else '移走物品（類別未確認）'
    evidence = HANDS.match(sensor, self.revision)
    message += '；曾見手腕接近（影像推定）' if evidence else '；手腕線索未確認'
    source = sensor.get('last_event') or {}
    record = dict(schema=2, id=self.sequence, unix_ms=time.time_ns() // 1000000, host_monotonic_ns=now, kind=kind, message=message, observation=observation, video_frame=frame_id, roi=self.roi, sensor_run_id=sensor.get('run_id'), sensor_event=source if source.get('kind') == kind else None, sensor_observed_ns=sensor.get('host_monotonic_ns'), presence=sensor.get('presence'), temperature=sensor.get('temperature'), hand_evidence=evidence)
    self.log.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
    self.events.appendleft({k: record[k] for k in ('id', 'unix_ms', 'kind', 'message')})
    print('互動事件：', message, flush=True)

def draw_zone(image, zone):
    if zone is None:
        return
    h, w = image.shape[:2]
    x1, y1, x2, y2 = [round(v * s) for v, s in zip(zone, (w - 1, h - 1, w - 1, h - 1))]
    color = (220, 100, 220)
    for x in range(x1, x2, 14):
        for y in (y1, y2):
            cv2.line(image, (x, y), (min(x + 7, x2), y), color, 1)
    for y in range(y1, y2, 14):
        for x in (x1, x2):
            cv2.line(image, (x, y), (x, min(y + 7, y2)), color, 1)

class ActionPublisher(stable.StablePublisher):

    def publish(self, job, pose):
        config = app.base.FUSION.config()
        hand = HANDS.observe(job, pose, config)
        if hand is None:
            return False
        image = job['image'].copy()
        draw_zone(image, hand['zone'])
        for wrist in hand['wrists']:
            if wrist['near']:
                cv2.circle(image, (round(wrist['x']), round(wrist['y'])), 10, (220, 100, 220), 2, cv2.LINE_AA)
        return super().publish(dict(job, image=image), dict(pose, hand=hand))

def install():
    stable.install()
    app.base.FUSION.emit = types.MethodType(emit_with_hand, app.base.FUSION)
    app.Publisher = ActionPublisher
    original = app.install_page

    def page():
        original()
        source = app.fusion_ui.PAGE.decode('utf-8')
        edits = [('<p id="notice">', '<p id="hand" style="padding:14px;background:#223147;color:#e9b8ff">手腕未確認</p><p id="notice">'), ("put('fusion',f.message);", "put('fusion',f.message);\nconst hand=(v.pose||{}).hand;\nput('hand',!f.roi?'請先設定秤台區域':\n  v.valid&&hand&&hand.revision===f.revision?hand.message:'手腕資料未就緒');"), ("}catch(e){put('pose',", "}catch(e){put('hand','手腕資料未就緒');put('pose',"), ('紅外線溫度代表測溫模組視野內的表面。', '紫色虛線為手腕接近判斷區，僅代表影像位置接近。灰色暫留關節不參與接近判斷。紅外線溫度代表測溫模組視野內的表面。')]
        for old, new in edits:
            if source.count(old) != 1:
                raise RuntimeError('既有網頁版本不同：' + old)
            source = source.replace(old, new, 1)
        app.fusion_ui.PAGE = source.encode('utf-8')
    app.install_page = page
if __name__ == '__main__':
    try:
        install()
        print('啟用：物品存在、物件類別、手腕接近線索與表面測溫整合。', flush=True)
        app.main()
    except KeyboardInterrupt:
        print('\n展示已停止；C71 與感測器視窗繼續運行。')
    except Exception as exc:
        print('\n展示停止：', exc)
        raise SystemExit(1)
    finally:
        app.base.FUSION.close()
