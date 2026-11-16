from service_support import open_log, LOG_DIR, CAMERA, worker_stderr, sensor_signals, take_zero
import math
import sys
import time
import numpy as np
import cv2
sys.dont_write_bytecode = True
import interaction_pose_app as app
MIN_CUTOFF = 0.8
SPEED_BETA = 4.0
POINT_ON, POINT_OFF = (0.3, 0.2)
HOLD_SECONDS = 0.2
PERSON_ON, PERSON_KEEP = (0.5, 0.35)
TRACK_TIMEOUT = 0.5

class OneEuro:

    def __init__(self):
        self.value = None
        self.velocity = np.zeros(2)
        self.stamp = None

    def update(self, value, stamp):
        value = np.asarray(value, dtype=np.float64)
        if self.value is None or stamp <= self.stamp or stamp - self.stamp > 0.5:
            self.value = value.copy()
            self.velocity[:] = 0
        else:
            dt = stamp - self.stamp
            derivative = (value - self.value) / dt
            a = 1 / (1 + 1 / (2 * math.pi * 1.0 * dt))
            self.velocity += a * (derivative - self.velocity)
            cutoff = MIN_CUTOFF + SPEED_BETA * np.abs(self.velocity)
            a = 1 / (1 + 1 / (2 * math.pi * cutoff * dt))
            self.value += a * (value - self.value)
        self.stamp = stamp
        return self.value.copy()

def iou(a, b):
    a, b = (np.asarray(a), np.asarray(b))
    size = np.maximum(0, np.minimum(a[2:], b[2:]) - np.maximum(a[:2], b[:2]))
    intersection = float(np.prod(size))
    area = float(np.prod(a[2:] - a[:2]) + np.prod(b[2:] - b[:2]))
    return intersection / max(area - intersection, 1.0)

class PersonBox(list):

    def __init__(self, values, identity):
        super().__init__((float(x) for x in values))
        self.identity = identity

class PersonSelector:

    def __init__(self, original_decode):
        self.decode = original_decode
        self.box = None
        self.seen = 0.0
        self.identity = 0

    def __call__(self, result, frame, geometry):
        detections, _ = self.decode(result, frame, geometry)
        now = time.monotonic()
        if now - self.seen > TRACK_TIMEOUT:
            self.box = None
        people = []
        for d in result['detections']:
            score = float(d['score'])
            if int(d['class_id']) != 0 or not np.isfinite(score) or score < PERSON_KEEP:
                continue
            coords = np.asarray(d['box'], dtype=np.float64)
            if coords.shape != (4,) or not np.isfinite(coords).all():
                continue
            y1, x1, y2, x2 = np.clip(coords * 640, 0, 640)
            scale, left, top = geometry
            h, w = frame.shape[:2]
            intersects = min(x2, left + w * scale) > max(x1, left) and min(y2, top + h * scale) > max(y1, top)
            if intersects and x2 - x1 >= 32 and (y2 - y1 >= 64):
                people.append(([x1, y1, x2, y2], score))
        chosen = None
        if self.box is not None:
            matches = [(iou(self.box, box), box) for box, score in people]
            if matches:
                overlap, box = max(matches, key=lambda item: item[0])
                if overlap >= 0.25:
                    chosen = box
        else:
            candidates = [box for box, score in people if score >= PERSON_ON]
            if candidates:
                chosen = max(candidates, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
                self.identity += 1
        if chosen is None:
            return (detections, None)
        self.box, self.seen = (chosen, now)
        return (detections, PersonBox(chosen, self.identity))

class Joint:

    def __init__(self):
        self.filter = OneEuro()
        self.active = False
        self.good = -1e+30
        self.raw = None
        self.candidate = None
        self.candidate_stamp = -1e+30

    def confirm(self, xy, stamp, radius):
        confirmed = self.candidate is not None and 0 < stamp - self.candidate_stamp <= 0.25 and (np.linalg.norm(xy - self.candidate) <= radius)
        self.candidate = xy.copy()
        self.candidate_stamp = stamp
        return confirmed

    def update(self, xy, score, inside, stamp, body_size):
        if stamp - self.good > HOLD_SECONDS + 1e-09:
            self.active = False
        threshold = POINT_OFF if self.active else POINT_ON
        accepted = inside and score >= threshold
        fresh = False
        if accepted:
            if not self.active:
                accepted = self.confirm(xy, stamp, max(0.04, 0.15 * body_size))
                if accepted:
                    self.filter = OneEuro()
            else:
                limit = max(0.08, 0.35 * body_size) * max(1, (stamp - self.good) / 0.1)
                if np.linalg.norm(xy - self.raw) > limit:
                    accepted = self.confirm(xy, stamp, max(0.04, 0.15 * body_size))
                else:
                    self.candidate = None
            if accepted:
                self.filter.update(xy, stamp)
                self.raw, self.good, self.active = (xy.copy(), stamp, True)
                self.candidate = None
                fresh = True
        else:
            self.candidate = None
        held = self.active and (not fresh) and (stamp - self.good <= HOLD_SECONDS + 1e-09)
        point = self.filter.value.copy() if fresh or held else xy.copy()
        return (point, fresh, held)

class PoseSmoother:

    def __init__(self):
        self.identity = None
        self.stamp = None
        self.shape = None
        self.joints = [Joint() for _ in range(17)]

    def update(self, pose, job):
        stamp = job['stamp'] / 1000000000.0
        identity = job['person'].identity
        shape = job['image'].shape[:2]
        if identity != self.identity or shape != self.shape or self.stamp is None or (not 0 < stamp - self.stamp <= 0.5):
            self.joints = [Joint() for _ in range(17)]
        self.identity, self.stamp, self.shape = (identity, stamp, shape)
        h, w = shape
        unit = float(max(h, w))
        box = job['person']
        body_size = np.hypot(box[2] - box[0], box[3] - box[1]) / job['geometry'][0] / unit
        raw = np.asarray(pose['keypoints'], dtype=np.float64)
        points, fresh, held = ([], [], [])
        for joint, (x, y, score) in zip(self.joints, raw):
            inside = 0 <= x < w and 0 <= y < h
            p, f, k = joint.update(np.array([x, y]) / unit, score, inside, stamp, body_size)
            points.append([float(p[0] * unit), float(p[1] * unit), float(score) if f else 0.0])
            fresh.append(bool(f))
            held.append(bool(k))
        return dict(pose, raw_keypoints=raw.tolist(), keypoints=points, drawn=fresh, held=held, visible=sum(fresh) + sum(held), fresh_count=sum(fresh), held_count=sum(held), person_id=identity, filter='one-euro-v1')

class StableWorker(app.PoseWorker):

    def __init__(self, client, publish):
        self.smoother = PoseSmoother()
        super().__init__(client, publish)

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
                data, inverse = app.crop_person(job['rgb'], job['person'])
                answer = self.client.infer(data)
                pose = app.project_keys(answer, inverse, job['geometry'], job['image'].shape)
                pose = self.smoother.update(pose, job)
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

class StablePublisher(app.Publisher):

    def publish(self, job, pose):
        if pose.get('valid') and any(pose.get('held', [])):
            image = job['image'].copy()
            coords = [(round(x), round(y)) for x, y, score in pose['keypoints']]
            held, fresh = (pose['held'], pose['drawn'])
            for a, b in app.EDGES:
                if (held[a] or fresh[a]) and (held[b] or fresh[b]) and (held[a] or held[b]):
                    cv2.line(image, coords[a], coords[b], (150, 150, 150), 2, cv2.LINE_AA)
            for i, p in enumerate(coords):
                if held[i]:
                    cv2.circle(image, p, 4, (150, 150, 150), -1, cv2.LINE_AA)
            job = dict(job, image=image)
        return super().publish(job, pose)

def install():
    original_page = app.install_page

    def page():
        original_page()
        source = app.fusion_ui.PAGE.decode('utf-8')
        edits = [('C71 人體姿態<strong', 'C71 人體姿態・穩定版<strong'), ('青色為姿態骨架；目前處理畫面中最大的人物。', '青色為時間平滑後的關節；灰色為最多 0.2 秒的暫留位置。'), ("' 毫秒｜骨架與影像來自同一張圖片'", "' 毫秒｜一歐元平滑｜灰色暫留 '+(p.held_count||0)+' 點'")]
        for old, new in edits:
            if source.count(old) != 1:
                raise RuntimeError('網頁版本不同，停止套用：' + old)
            source = source.replace(old, new, 1)
        app.fusion_ui.PAGE = source.encode('utf-8')
    app.install_page = page
    app.decode = PersonSelector(app.decode)
    app.PoseWorker = StableWorker
    app.Publisher = StablePublisher
if __name__ == '__main__':
    try:
        install()
        print('啟用：人物連續選擇、關節遲滯、一歐元平滑與短暫漏點處理。', flush=True)
        app.main()
    except KeyboardInterrupt:
        print('\n展示已停止；C71 保持運行。')
    except Exception as exc:
        print('\n展示停止：', exc)
        raise SystemExit(1)
    finally:
        app.base.FUSION.close()
