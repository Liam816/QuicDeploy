"""Offline real-time source-grid/RTC test of the actual GPU worker; no hardware.

Camera content comes from recorded RGB views; timestamps/frame indices advance
in real wall time. This tests latency, cache upkeep and scheduling, not success.
"""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def main():
    import numpy as np
    from quicdeploy.config.resolve import resolve
    from quicdeploy.planning.scheduler import QueueScheduler, PrefixWindowExpired
    from quicdeploy.policy.worker import InferenceWorker
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--sample', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--duration', type=float, default=10)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    config = resolve(args.config)
    sampling = config['model']['temporal_sampling']
    count, stride, fps = sampling['history_frames'], sampling['history_stride'], sampling['source_fps']
    with np.load(args.sample, allow_pickle=False) as data:
        anchor = data['anchor'].copy()
        rgb = {name: data['image_'+name].copy() for name in config['model']['cameras']}
    store, comparisons = {}, []
    base = None
    session = 'offline-continuous'
    def images(index):
        if index not in store:
            store[index] = {name: np.roll(value, (index // stride) % 13, axis=1).copy()
                            for name, value in rgb.items()}
        return store[index]
    def observation(current):
        offsets = list(range(-(count-1)*stride, 1, stride))
        indices = [current if current + offset < 0 else current + offset for offset in offsets]
        return {'images': images(current), 'eef_state': anchor, 'timestamp': base+current/fps,
                'vision_history': {'session':session,'frame_index':current,'first_frame_index':0,
                                  'time_offsets':offsets,
                                  'samples':[{'frame_index':i,'images':images(i)} for i in indices]}}
    def context(step, request):
        return {'seed':0,'episode':0,'anchor_index':step,'prefix_absolute':request.prefix_u}
    worker = InferenceWorker(dict(config['model']))
    scheduler = QueueScheduler(config['model'],config['execution'],config['postprocess'])
    report = {'status':'running','hardware_connected':False,'physical_commands_sent':0,
              'input_scope':'recorded image content, real-time 30Hz source indices and original RTC scheduler',
              'sampling':sampling,'warmup_s':worker.startup_info['warmup_s'],'cases':[],
              'rtc_restarts':0,'vision_offers':0,'max_feed_call_ms':0,'max_tick_ms':0}
    def accept(reply, request, held=False):
        result = scheduler.accept(reply['raw'],request,time.monotonic(),held_verified=held)
        item = {'step':request.step,'model_ms':reply['inference_s']*1000,
                'roundtrip_ms':reply['roundtrip_s']*1000,'vision_cache':reply['vision_cache'],
                'expired_steps':result.expired_steps,'gap_s':result.gap_s,
                'has_rtc_prefix':request.prefix_u is not None}
        report['cases'].append(item)
        if request.step in (0, 1, 10):
            comparisons.append((request.step, reply['vision_cache']['frame_index'],
                                None if request.prefix_u is None else request.prefix_u.copy(),
                                reply['raw'].copy()))
        print(json.dumps(item),flush=True)
    try:
        base = time.monotonic()
        first = scheduler.prepare(0,base,anchor,requested_at=time.monotonic())
        worker.submit(observation(0),context(0,first))
        accept(worker.wait(30),first,held=True)
        pending, step = None, 1
        deadline = time.monotonic()+args.duration
        while time.monotonic()<deadline or pending is not None:
            tick = time.monotonic()
            current = int((tick-base)*fps)//stride*stride
            cursor = worker.vision_cursor(session)
            start = 0 if cursor is None else cursor+stride
            for index in range(start,current+1,stride):
                before = time.perf_counter()
                offered = worker.offer_vision({'session':session,'frame_index':index,
                                               'timestamp':base+index/fps,'images':images(index)})
                report['max_feed_call_ms'] = max(report['max_feed_call_ms'],(time.perf_counter()-before)*1000)
                if not offered: break
                report['vision_offers'] += 1
            if pending is not None:
                reply = worker.poll()
                if reply is not None:
                    accept(reply,pending)
                    pending = None
            now = time.monotonic()
            fresh = now-(base+current/fps)<=config['observation']['max_camera_age_ms']/1000
            if now<deadline and pending is None and scheduler.should_request(now) and fresh:
                try:
                    pending = scheduler.prepare(step,base+current/fps,anchor,requested_at=now)
                except PrefixWindowExpired:
                    report['rtc_restarts'] += 1
                    scheduler.restart_rtc()
                    pending = scheduler.prepare(step,base+current/fps,anchor,requested_at=now)
                worker.submit(observation(current),context(step,pending))
                step += 1
            # Exercise playback interpolation; no FK, IK or robot commands.
            scheduler.target(time.monotonic()+1/config['execution']['control_hz'])
            report['max_tick_ms'] = max(report['max_tick_ms'],(time.monotonic()-tick)*1000)
            time.sleep(max(0,tick+1/config['execution']['control_hz']-time.monotonic()))
        steady = report['cases'][1:]
        assert len(steady)>=5
        assert all(v['vision_cache']['new_frame_encodings']==0 for v in steady), steady
        assert all(v['has_rtc_prefix'] for v in steady)
        assert report['rtc_restarts']==0
        assert all(v['gap_s']<1e-6 for v in steady)
        rt = sorted(v['roundtrip_ms'] for v in steady)
        report['summary'] = {'requests':len(steady),
                             'model_ms_median':statistics.median(v['model_ms'] for v in steady),
                             'roundtrip_ms_median':statistics.median(rt),
                             'roundtrip_ms_p95':rt[min(len(rt)-1,int(len(rt)*.95))],
                             'roundtrip_ms_max':max(rt),
                             'action_vit_calls':sum(v['vision_cache']['new_frame_encodings'] for v in steady),
                             'background_frames':sum(v['vision_cache']['background_frames'] for v in steady)}
        assert report['summary']['model_ms_median']<150
        assert report['summary']['roundtrip_ms_p95']<400
        # Performance measurements are complete. Compare to uncached image
        # replay using the same distinct frames, noise and actual RTC prefixes.
        worker.close()
        from quicdeploy.policy.base import load_adapter
        reference = load_adapter(dict(config['model']))
        report['uncached_reference_comparisons'] = []
        for request_step, current, prefix, expected in comparisons:
            reference.reset()
            reference.session, reference.first_token_frame_index = session, 0
            actual = reference.infer(observation(current), {'seed':0,'episode':0,'anchor_index':request_step,
                                                           'prefix_absolute':prefix})
            error = float(np.max(np.abs(actual-expected)))
            assert np.array_equal(actual, expected), (request_step, error)
            report['uncached_reference_comparisons'].append({'step':request_step,'max_abs_error':error})
        report['status'] = 'passed'
    except BaseException as exc:
        report.update(status='failed',error=str(exc))
        raise
    finally:
        worker.close()
        (args.output/'continuous_validation.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k!='cases'},indent=2),flush=True)


if __name__=='__main__':
    import fcntl
    import os
    # Offline GPU profiling must not compete with an active arm controller.
    with open(f'/tmp/piper-vr-oneclick-{os.getuid()}.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        main()
