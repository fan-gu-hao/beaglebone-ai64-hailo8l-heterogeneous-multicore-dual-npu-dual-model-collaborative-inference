"""Exercise production definitions without opening board devices or starting services."""
import ast
from collections import deque
import io
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def definitions(filename, names, namespace):
    tree = ast.parse((ROOT / 'runtime' / filename).read_text())
    nodes = [n for n in tree.body
             if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in names]
    assert {n.name for n in nodes} == set(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, 'exec'), namespace)
    return types.SimpleNamespace(**{name: namespace[name] for name in names})


class Clock:
    now = 100.0

    def monotonic_ns(self):
        return round(self.now * 1e9)

    def time_ns(self):
        return self.monotonic_ns()

    def monotonic(self):
        return self.now


def sensor(t, present=False, run='A', event=0):
    return {'usable': True, 'run_id': run, 'host_monotonic_ns': round(t * 1e9),
            'presence': {'valid': True, 'present': present},
            'temperature': {'valid': True, 'object_c': 25.5},
            'last_event': {'id': event, 'kind': 'load_on' if present else 'load_off'}}


class TemporalTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)
        self.logs = []
        def open_log(name):
            stream = (self.path / (name + '.jsonl')).open('w')
            self.logs.append(stream)
            return stream
        self.addCleanup(lambda: [s.close() for s in self.logs if not s.closed])
        env = dict(np=np, math=math, time=self.clock, deque=deque,
                   threading=threading, Path=Path, json=json, os=os,
                   LOG_DIR=self.path, open_log=open_log, NAMES={39:'瓶子',41:'杯子',67:'手機'})
        self.h = definitions('interaction_actions.py', ['HandEvidence'], dict(env)).HandEvidence()
        self.st = definitions('interaction_pose_stable.py',
            ['OneEuro', 'Joint', 'PersonBox', 'PersonSelector', 'iou'], dict(env,
            MIN_CUTOFF=.8, SPEED_BETA=4., POINT_ON=.30, POINT_OFF=.20,
            HOLD_SECONDS=.2, PERSON_ON=.5, PERSON_KEEP=.35, TRACK_TIMEOUT=.5))
        self.f = definitions('interaction_fusion.py',
            ['Fusion', 'checked_roi', 'overlap'], dict(env)).Fusion(self.path)
        self.addCleanup(self.f.close)
        self.roi = [.65, .8, .95, .98]
        self.f.set_roi(self.roi)
        self.config = {'revision': 1, 'roi': self.roi}

    def observe(self, t, frame, *, score=.8, fresh=True, identity=1,
                run='A', revision=1, x=.8, stamp=None):
        self.clock.now = t
        stamp = t if stamp is None else stamp
        points = [[0., 0., 0.] for _ in range(17)]
        points[9] = [x * 640, .75 * 480, score]
        drawn = [False] * 17
        drawn[9] = fresh
        pose = dict(valid=True, keypoints=points, drawn=drawn, person_id=identity)
        job = dict(stamp=round(stamp*1e9), serial=frame,
            image=np.zeros((480,640,3),np.uint8), sensors=sensor(stamp,run=run),
            fusion={'revision':revision})
        config = dict(self.config, revision=revision)
        return self.h.observe(job, pose, config)

    def update(self, t, frame, present=False, event=0, detections=None, run='A', usable=True):
        self.clock.now = t
        value = sensor(t, present, run, event)
        value['usable'] = usable
        return self.f.update(detections or [], 640, 480, round(t*1e9), frame, value)

    def test_wrist_requires_two_observations_and_elapsed_time(self):
        self.assertFalse(self.observe(100,1)['near'])
        self.assertFalse(self.observe(100.02,2)['near'])
        self.assertTrue(self.observe(100.1,3)['near'])
        self.assertEqual(self.h.match(sensor(100.2),1)['video_frame'],3)

    def test_held_and_low_confidence_points_are_not_evidence(self):
        for i in range(3):
            self.assertFalse(self.observe(100+i*.1,i+1,fresh=False)['near'])
        for i in range(3):
            self.assertFalse(self.observe(101+i*.1,i+4,score=.25)['near'])
        self.assertIsNone(self.h.match(sensor(101.2),1))

    def test_wrist_history_scoped_to_sensor_run_and_roi(self):
        self.observe(100,1); self.observe(100.1,2)
        self.assertIsNotNone(self.h.match(sensor(100.2),1))
        self.assertIsNone(self.h.match(sensor(100.2,run='B'),1))
        self.assertIsNone(self.h.match(sensor(100.2),2))
        self.assertIsNone(self.h.match(sensor(102),1))

    def test_person_switch_reacquires(self):
        self.observe(100,1); self.observe(100.1,2)
        self.assertFalse(self.observe(100.2,3,identity=2)['near'])
        self.assertIsNone(self.h.match(sensor(100.2),1))
        self.assertTrue(self.observe(100.3,4,identity=2)['near'])

    def test_old_or_duplicate_frame_is_ignored(self):
        self.observe(100,1)
        self.assertIsNone(self.observe(100.1,1))
        self.assertIsNone(self.observe(102,2,stamp=100))

    def test_alternating_inside_outside_does_not_accumulate(self):
        for i in range(8):
            self.assertFalse(self.observe(100+i*.1,i+1,x=.8 if i%2==0 else .1)['near'])

    def test_sensor_transition_is_authority_and_events_do_not_repeat(self):
        bottle = dict(class_id=39,score=.9,box=[450,100,530,445])
        self.update(100,1)
        for frame,t in enumerate((100.1,100.25,100.4),2):
            self.update(t,frame,True,1,[bottle])
        self.assertEqual(len(self.f.events),1)
        self.assertIn('瓶子',self.f.events[0]['message'])
        self.update(100.8,5,False,2)
        self.assertEqual(self.f.events[0]['kind'],'load_off')
        for i in range(5):self.update(100.9+i*.1,6+i,False,2)
        self.assertEqual(len(self.f.events),2)

    def test_visual_bottle_without_load_does_not_create_placement(self):
        bottle = dict(class_id=39,score=.99,box=[450,100,530,445])
        for i in range(6):self.update(100+i*.2,i+1,False,0,[bottle])
        self.assertEqual(len(self.f.events),0)

    def test_unknown_class_is_recorded_after_timeout(self):
        self.update(100,1)
        for i in range(8):self.update(100.1+i*.35,i+2,True,1)
        self.assertEqual(len(self.f.events),1)
        self.assertIn('類別未確認',self.f.events[0]['message'])

    def test_new_sensor_session_establishes_baseline(self):
        self.update(100,1)
        self.update(100.1,2,True,1,run='B')
        self.assertEqual(len(self.f.events),0)

    def test_invalid_data_does_not_create_event(self):
        self.update(100,1)
        result=self.update(100.1,2,True,1,usable=False)
        self.assertFalse(result['valid'])
        self.assertEqual(len(self.f.events),0)

    def test_filter_reduces_stationary_jitter(self):
        rng=np.random.default_rng(44)
        raw=np.array([.5,.4])+rng.normal(0,.008,(250,2))
        f=self.st.OneEuro()
        output=np.array([f.update(p,10+i*.1) for i,p in enumerate(raw)])
        ratio=np.std(output[20:],axis=0).mean()/np.std(raw[20:],axis=0).mean()
        self.assertLess(ratio,.75)

    def test_filter_tracks_moving_point_and_resets_after_gap(self):
        f=self.st.OneEuro()
        out=[f.update([.2+i*.04,.4],100+i*.1) for i in range(15)]
        self.assertLess(.2+14*.04-out[-1][0],.06)
        np.testing.assert_array_equal(f.update([.9,.1],105),[.9,.1])

    def test_joint_hold_expires(self):
        j=self.st.Joint()
        self.assertFalse(j.update(np.array([.4,.4]),.8,True,100,.8)[1])
        self.assertTrue(j.update(np.array([.4,.4]),.8,True,100.1,.8)[1])
        self.assertTrue(j.update(np.array([.4,.4]),.01,True,100.2,.8)[2])
        self.assertFalse(j.update(np.array([.4,.4]),.01,True,100.5,.8)[2])

    def test_joint_outlier_does_not_immediately_move_filter(self):
        j=self.st.Joint()
        for t in (100,100.1):j.update(np.array([.2,.2]),.9,True,t,.5)
        p,fresh,held=j.update(np.array([.9,.9]),.9,True,100.2,.5)
        self.assertFalse(fresh); self.assertTrue(held)
        np.testing.assert_allclose(p,[.2,.2])

    def test_joint_confidence_hysteresis(self):
        j=self.st.Joint()
        for t in (100,100.1):j.update(np.array([.2,.2]),.31,True,t,.5)
        self.assertTrue(j.update(np.array([.2,.2]),.21,True,100.2,.5)[1])
        self.assertFalse(j.update(np.array([.2,.2]),.19,True,100.3,.5)[1])

    def test_tracking_keeps_person_when_larger_person_appears(self):
        tracker=self.st.PersonSelector(lambda *args:([],None))
        frame=np.zeros((480,640,3),np.uint8)
        a=dict(class_id=0,score=.8,box=[.2,.2,.8,.6])
        b=dict(class_id=0,score=.9,box=[.1,.65,.95,1.])
        _,first=tracker({'detections':[a]},frame,(1,0,80))
        self.clock.now=100.1
        _,second=tracker({'detections':[a,b]},frame,(1,0,80))
        self.assertEqual(first.identity,second.identity)
        np.testing.assert_array_equal(first,second)


if __name__ == '__main__':
    unittest.main()
