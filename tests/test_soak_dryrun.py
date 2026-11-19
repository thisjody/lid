"""The soak and load drivers (tests/soak/soak.py) run end to end against a fake box and a fake clock.

A clean fake run must PASS. A fake told to drop a job, leak a box, fail a decoy, fail to prove a chaos, lose an audit,
lose a worker outside a reboot, or go unreadable at the end must FAIL. Jobs take different times, so the poll sees jobs
pending in every combination and some finish only in the drain. Nothing touches a real box; time only moves on sleep().
"""
import json
import re
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from tests.soak import soak  # noqa: E402   (a package path, so mutation testing maps the tests)

T0 = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.t = T0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeBox:
    def __init__(self, clock, drop=(), fail=(), leak=(), bad_decoy=False, unproven=(), lose_audit=(), gone=(),
                 final_unreadable=False, refuse_over=None, preflight_fail=(), mem_used=1000, c5_ran=False):
        self.c = clock
        self.drop, self.fail, self.leak, self.unproven = set(drop), set(fail), set(leak), set(unproven)
        self.lose_audit, self.gone, self.bad_decoy, self.final_unreadable = set(lose_audit), set(gone), bad_decoy, final_unreadable
        self.refuse_over, self.preflight_fail, self.mem_used, self.c5_ran = refuse_over, set(preflight_fail), mem_used, c5_ran
        self.jobs, self.n, self.chaos_done, self.max_live = {}, 0, [], 0
        self.lock = threading.Lock()

    def _dur(self, n):
        return 400 + (n * 137) % 900

    def launch(self, kind, jid, a, shards=None):
        with self.lock:
            return self._launch(kind, jid)

    def _launch(self, kind, jid):
        self.n += 1
        live = sum(1 for j in self.jobs.values() if self.c.time() < j['end'])
        refused = self.refuse_over is not None and kind == 'claude' and live >= self.refuse_over
        pre = self.n in self.preflight_fail
        self.jobs[jid] = {'n': self.n, 'kind': kind, 'start': self.c.time(),
                          'end': self.c.time() + (5 if (refused or pre) else self._dur(self.n)), 'refused': refused, 'pre': pre}
        return 0

    def status(self, prefix):
        if self.final_unreadable and self.c.time() > T0 + 3 * 3600 - 600:
            return None
        jobs, live = {}, []
        for jid, j in self.jobs.items():
            if not jid.startswith(prefix):
                continue
            n, now = j['n'], self.c.time()
            done = n not in self.drop and now >= j['end']
            v = None
            if done:
                v = 'FAIL' if (n in self.fail or n in self.gone or j['refused'] or j['pre']) else 'PASS'
            cred = None
            if j['kind'] == 'claude' and done and not (j['refused'] or j['pre']):
                cred = None if n in self.lose_audit else {'readable': True, 'swapped': 2, 'hosts': ['api.anthropic.com']}
            stages = {'preflight': '00:00:00'} if (j['refused'] or j['pre']) else {'box': '00:00:00', 'setup': '00:00:12', 'boxes': '00:00:00', 'probe': '00:00:30'}
            jobs[jid] = {'done': ('== done ' + v) if done else None, 'done_at': j['end'] if done else None, 'verdict': v,
                         'never_forgive': False, 'worker_gone': n in self.gone, 'stages': stages, 'cred': cred,
                         'cause': '6 boxes already live' if j['refused'] else ('lockdown not in place' if j['pre'] else None)}
            if (not done and not (j['refused'] or j['pre'])) or n in self.leak:
                live.append({'id': f'sbx_{n}', 'job': jid})
        self.max_live = max(self.max_live, len(live))
        return {'jobs': jobs, 'live': live, 'host': {'load1': 1.0, 'mem_used_mb': self.mem_used, 'mem_total_mb': 30000}}

    def decoy(self):
        return 'FAIL' if self.bad_decoy else 'PASS'

    def chaos(self, cid, kind, jid=None, a=None):
        self.chaos_done.append(cid)
        at = self.c.time()
        out = {'ok': cid not in self.unproven, 'proof': 'fake', 'at': at, 'back': at + (300 if kind == 'reboot' else 0)}
        if kind == 'drop-iap':
            out['rc'] = -9   # the launch was killed: the box got the job only if c5_ran
            if self.c5_ran:
                with self.lock:
                    self._launch('claude', jid)
        return out

    def repo_state(self):
        return {'sha': 'f' * 40, 'dirty': False, 'deployed_ok': True}


def run(tmp_path, monkeypatch, mode='soak', chaos_override=None, **box):
    c = Clock()
    fb = FakeBox(c, **box)
    monkeypatch.setattr(soak, 'time', c)
    for name in ('launch', 'status', 'decoy', 'chaos', 'repo_state'):
        monkeypatch.setattr(soak, name, getattr(fb, name))
    if chaos_override:   # the fake box's chaos, with the override's job/missed fields
        def chaos(cid, kind, jid=None, a=None):
            extra = {k: v for k, v in chaos_override(cid, kind, jid, a).items() if k in ('job', 'missed', 'job_ran')}
            ran = [extra['job']] if extra.get('job_ran') else []
            for m in (extra.get('missed') or []) + ran:   # launches that reached the (fake) box and run
                with fb.lock:
                    fb._launch('claude', m)
            return {**fb.chaos(cid, kind, jid, a), **extra}
        monkeypatch.setattr(soak, 'chaos', chaos)
    a = type('A', (), {'out': str(tmp_path / mode), 'hours': 3.0, 'prompt': 'p', 'fan_repo': 'r', 'fan_cmd': 'c'})()
    rc = getattr(soak, mode)(a)
    v = json.load(open(tmp_path / mode / 'verdict.json'))
    return rc, v, fb


def test_clean_fake_soak_passes(tmp_path, monkeypatch):
    rc, v, fb = run(tmp_path, monkeypatch)
    assert (rc, v['verdict']) == (0, 'PASS'), v
    assert fb.n >= 20 and fb.chaos_done == ['c1', 'c2', 'c3', 'c4', 'c5']
    rec = json.load(open(tmp_path / 'soak' / 'records.json'))
    c5 = next(c['job'] for c in rec['chaos'] if c['id'] == 'c5')
    assert all(j['verdict'] == 'PASS' for j in rec['jobs'] if j['id'] != c5)   # the c5 launch was killed before it ran
    assert any(len(l['pending']) == 1 for l in rec['leaks']), 'the poll must see one job pending, not only none or two'


@pytest.mark.parametrize('box,needle', [
    ({'drop': {5}}, 'never finished'),
    ({'fail': {1}}, 'no chaos that affects it'),   # job 1 runs [0, 537 s], before any chaos
    ({'leak': {3}}, 'outlived its job'),
    ({'bad_decoy': True}, 'decoy'),
    ({'unproven': {'c4'}}, 'c4'),
    ({'lose_audit': {3}}, 'readable audit'),
    ({'gone': {4}}, 'worker died with no reboot'),
    ({'final_unreadable': True}, 'final leak check'),
])
def test_planted_defects_fail(tmp_path, monkeypatch, box, needle):
    rc, v, _ = run(tmp_path, monkeypatch, **box)
    assert rc == 1 and any(needle in f for f in v['fails']), v


def test_the_last_jobs_finish_in_the_drain_and_are_judged(tmp_path, monkeypatch):
    rc, v, fb = run(tmp_path, monkeypatch, lose_audit={fb_n for fb_n in range(1, 200)})
    rec = json.load(open(tmp_path / 'soak' / 'records.json'))
    last = max((j for j in rec['jobs'] if j['kind'] == 'claude'), key=lambda j: j['launched'])
    assert last['verdict'] is not None and any(last['id'] in f for f in v['fails']), 'a drained Claude job is still audited'


def test_changed_criterion_refuses(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch)
    for f in ('verdict_py_sha256', 'soak_py_sha256', 'box_status_py_sha256'):
        crit = tmp_path / 'soak' / 'criteria.json'
        d = json.load(open(crit)); good = d[f]; d[f] = '0' * 64; json.dump(d, open(crit, 'w'))
        assert soak.judge(type('A', (), {'out': str(tmp_path / 'soak')})()) == 2, f
        d[f] = good; json.dump(d, open(crit, 'w'))


def test_resume_keeps_polling_jobs_in_flight(tmp_path, monkeypatch):
    c = Clock()
    fb = FakeBox(c)
    monkeypatch.setattr(soak, 'time', c)
    for name in ('launch', 'status', 'decoy', 'chaos', 'repo_state'):
        monkeypatch.setattr(soak, name, getattr(fb, name))
    a = type('A', (), {'out': str(tmp_path / 'soak'), 'hours': 3.0, 'prompt': 'p', 'fan_repo': 'r', 'fan_cmd': 'c'})()

    class Crash(Exception):
        pass
    real_sleep, calls = c.sleep, {'n': 0}

    def crashing_sleep(s):
        calls['n'] += 1
        if calls['n'] == 100:
            raise Crash()
        real_sleep(s)
    c.sleep = crashing_sleep
    with pytest.raises(Crash):
        soak.soak(a)
    c.sleep = real_sleep
    rc = soak.soak(a)
    v = json.load(open(tmp_path / 'soak' / 'verdict.json'))
    assert (rc, v['verdict']) == (0, 'PASS'), v
    rec = json.load(open(tmp_path / 'soak' / 'records.json'))
    gaps = sorted(d['at'] for d in rec['decoys'])
    assert all(b - a > 600 for a, b in zip(gaps, gaps[1:])), 'decoys stay spaced across a resume'


# -- load ----------------------------------------------------------------------------------------------------

def test_clean_fake_load_passes_and_reports(tmp_path, monkeypatch):
    rc, v, fb = run(tmp_path, monkeypatch, mode='load', refuse_over=6)
    assert (rc, v['verdict']) == (0, 'PASS'), v
    s = json.load(open(tmp_path / 'load' / 'summary.json'))
    assert [st['step'] for st in s['steps']] == ['1', '2', '4', '6', 'over-cap-7', 'backlog-12', f'fan-{soak.FAN_CAP}']
    for st in s['steps']:
        assert {'create_p50_s', 'create_p99_s', 'create_max_s', 'wall_max_s', 'load1_max', 'mem_used_max_mb', 'failed', 'live_max'} <= set(st), st
    over = next(st for st in s['steps'] if st['step'] == 'over-cap-7')
    assert over['refused'] == 1 and over['live_max'] <= 6


def test_load_fails_if_the_cap_is_exceeded(tmp_path, monkeypatch):
    rc, v, _ = run(tmp_path, monkeypatch, mode='load', refuse_over=None)
    assert rc == 1 and any('over-cap' in f for f in v['fails']), v


def test_load_fails_on_a_failed_job(tmp_path, monkeypatch):
    rc, v, _ = run(tmp_path, monkeypatch, mode='load', refuse_over=6, fail={2})
    assert rc == 1 and any('FAIL' in f for f in v['fails']), v


def test_load_fails_on_a_leak(tmp_path, monkeypatch):
    rc, v, _ = run(tmp_path, monkeypatch, mode='load', refuse_over=6, leak={1})
    assert rc == 1 and any('live' in f for f in v['fails']), v


# -- the real box-facing helpers -------------------------------------------------------------------------------------

def test_lid_survives_a_timeout(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise subprocess.TimeoutExpired('x', 1)
    monkeypatch.setattr(soak.subprocess, 'run', boom)
    assert soak.lid('true', timeout=1) == (124, '')


def test_status_parses_or_is_none(monkeypatch):
    monkeypatch.setattr(soak.time, 'sleep', lambda sec: None)
    for out, expect in (('', None), ('noise\n{"jobs": {}, "live": []}\n', {'jobs': {}, 'live': []}), ('garbage\n', None)):
        monkeypatch.setattr(soak, '_lid_full', lambda cmd, timeout=600, o=out: (0, o, ''))
        assert soak.status('soak-1-') == expect
    monkeypatch.setattr(soak, '_lid_full', lambda cmd, timeout=600: (1, '{"jobs": {}, "live": []}\n', ''))
    assert soak.status('soak-1-') is None


def test_status_ships_box_status_isolated(monkeypatch):
    seen = {}

    def fake(cmd, timeout=600):
        seen['cmd'] = cmd
        return 0, '{"jobs": {}, "live": []}\n', ''
    monkeypatch.setattr(soak, '_lid_full', fake)
    soak.status('soak-1-')
    assert 'python3 -I -' in seen['cmd'] and 'soak-1-' in seen['cmd'] and 'base64 -d' in seen['cmd']


def test_chaos_commands_prove_themselves():
    src = open(soak.__file__).read()
    for word in ('KILLED', 'RESTARTED', 'boot_id', 'killpg'):
        assert word in src, word
    assert "pkill -f 'gcloud compute ssh'" not in src




def test_load_fails_when_jobs_die_before_a_box(tmp_path, monkeypatch):
    rc, v, _ = run(tmp_path, monkeypatch, mode='load', refuse_over=6, preflight_fail=set(range(1, 100)))
    assert rc == 1 and any('FAIL or unfinished' in f for f in v['fails']), v


def test_load_over_cap_must_refuse_at_least_one(tmp_path, monkeypatch):
    rc, v, _ = run(tmp_path, monkeypatch, mode='load', refuse_over=7)
    assert rc == 1 and any('over-cap' in f for f in v['fails']), v


def test_load_memory_headroom(tmp_path, monkeypatch):
    rc, v, _ = run(tmp_path, monkeypatch, mode='load', refuse_over=6, mem_used=29000)
    assert rc == 1 and any('memory' in f for f in v['fails']), v


def test_load_is_not_resumable(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, mode='load', refuse_over=6)
    a = type('A', (), {'out': str(tmp_path / 'load'), 'hours': 3.0, 'prompt': 'p', 'fan_repo': 'r', 'fan_cmd': 'c'})()
    assert soak.load(a) == 2


def test_a_dirty_tree_or_undeployed_code_refuses_to_start(tmp_path, monkeypatch):
    for state in ({'sha': 'x', 'dirty': True, 'deployed_ok': True}, {'sha': 'x', 'dirty': False, 'deployed_ok': False}):
        monkeypatch.setattr(soak, 'repo_state', lambda s=state: s)
        a = type('A', (), {'out': str(tmp_path / ('s' + str(state['dirty']))), 'hours': 3.0, 'prompt': 'p', 'fan_repo': 'r', 'fan_cmd': 'c'})()
        assert soak.soak(a) == 2 and soak.load(a) == 2


def test_the_criteria_record_the_repo_sha(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch)
    crit = json.load(open(tmp_path / 'soak' / 'criteria.json'))
    assert crit['repo_sha'] == 'f' * 40 and crit['hours'] >= 3


def test_the_c5_job_is_recorded_and_excused_only_itself(tmp_path, monkeypatch):
    rc, v, _ = run(tmp_path, monkeypatch)
    rec = json.load(open(tmp_path / 'soak' / 'records.json'))
    c5 = next(c for c in rec['chaos'] if c['id'] == 'c5')
    j = next(j for j in rec['jobs'] if j['id'] == c5['job'])
    assert j['launch_rc'] == -9 and rc == 0


def test_percentiles_are_nearest_rank():
    assert soak.pct([1, 2], 0.5) == 1 and soak.pct(list(range(1, 101)), 0.99) == 99 and soak.pct([5], 0.99) == 5
    assert soak.pct(list(range(1, 11)), 0.5) == 5 and soak.pct([], 0.5) is None




def test_a_c5_job_that_did_reach_the_box_is_in_flight_not_a_leak(tmp_path, monkeypatch):
    rc, v, _ = run(tmp_path, monkeypatch, c5_ran=True)
    assert (rc, v['verdict']) == (0, 'PASS'), v


def test_a_changed_chaos_plan_refuses(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch)
    crit = tmp_path / 'soak' / 'criteria.json'
    d = json.load(open(crit)); d['chaos_plan'][0][0] = 1; json.dump(d, open(crit, 'w'))
    assert soak.judge(type('A', (), {'out': str(tmp_path / 'soak')})()) == 2


def test_the_plan_leaves_room_for_recovery():
    ends = [off + w for off, _, _, w in soak.CHAOS_PLAN]
    starts = [off for off, _, _, _ in soak.CHAOS_PLAN][1:] + [3 * 3600 - soak.LAUNCH_CUTOFF_S]
    assert all(b - a >= 12 * 60 for a, b in zip(ends, starts)), list(zip(ends, starts))
    assert dict((k, w) for _, _, k, w in soak.CHAOS_PLAN)['kill-discobox-server'] >= 1200 and soak.IN_FLIGHT >= 3


# -- the live soak on 2d490ff: c5 never hit a launch (it slept a fixed 5 s; launches now finish sooner) ----------------

def _fake_launcher(tmp_path, script):
    f = tmp_path / 'fake_disco_job.sh'
    f.write_text('#!/bin/bash\n' + script)
    f.chmod(0o755)
    return str(f)


def _c5(tmp_path, monkeypatch, script, box=None):
    """box: {job name: 'mid' | 'worker'} -- how far that launch got on the box when it was killed (absent: never reached it)."""
    monkeypatch.setattr(soak, 'DISCO_JOB', _fake_launcher(tmp_path, script))
    box = box or {}

    def lid(cmd, timeout=600):
        m = re.search(r'W=~/scratch/([\w.-]+);', cmd)
        return (0, box.get(m.group(1), 'none') + '\n') if m else (0, '')
    monkeypatch.setattr(soak, 'lid', lid)
    a = type('A', (), {'prompt': 'p'})()
    return soak.chaos('c5', 'drop-iap', jid='soak-1-c5', a=a)


# The kill aims a moment after gcloud's own ssh child appears; where it landed is read from the box afterwards.
# A fake `ssh` is a link to sleep, so the process name the harness looks for is really `ssh`.
SSH_THEN_WAIT = ('d=$(dirname "$0"); ln -sf "$(command -v sleep)" "$d/ssh"; echo "$1" >> "$d/names"\n'
                 'exec -a "gcloud compute ssh fake" bash -c "\\"$d/ssh\\" 30"\n')


@pytest.fixture(autouse=True)
def _quick_kill(monkeypatch):
    monkeypatch.setattr(soak, 'C5_KILL_AFTER_S', (0.3, 0.3))


def test_c5_proven_when_the_kill_cut_the_launch_on_the_box(tmp_path, monkeypatch):
    c = _c5(tmp_path, monkeypatch, SSH_THEN_WAIT, box={'soak-1-c5-1': 'mid'})
    assert c['ok'] and c['rc'] != 0 and c['job'] == 'soak-1-c5-1' and not c['job_ran'] and c['missed'] == [], c
    assert 'reached the box' in c['proof'], c


def test_c5_kill_before_the_box_is_not_a_proof_and_retries(tmp_path, monkeypatch):
    c = _c5(tmp_path, monkeypatch, SSH_THEN_WAIT, box={'soak-1-c5-2': 'mid'})
    assert c['ok'] and c['job'] == 'soak-1-c5-2' and c['missed'] == [] and 'before the box' in c['proof'], c


def test_c5_kill_after_the_worker_started_is_a_real_job(tmp_path, monkeypatch):
    c = _c5(tmp_path, monkeypatch, SSH_THEN_WAIT, box={'soak-1-c5-1': 'worker'})
    assert c['ok'] and c['job'] == 'soak-1-c5-1' and c['job_ran'], c


def test_c5_does_not_kill_on_gcloud_alone(tmp_path, monkeypatch):
    # gcloud has started but no ssh child yet, and the launch then finishes: that is a launch that beat the kill
    c = _c5(tmp_path, monkeypatch, 'exec -a "gcloud compute ssh fake" sleep 1\n')
    assert not c['ok'] and len(c['missed']) == soak.C5_TRIES, c


def test_c5_retries_when_a_launch_finishes_before_the_kill(tmp_path, monkeypatch):
    # the first launch completes at once (it reached the box: a real job, recorded and judged); the second is killed
    script = ('d=$(dirname "$0"); n=$(( $(cat "$d/n" 2>/dev/null || echo 0) + 1 )); echo $n > "$d/n"\n'
              '[ "$n" = 1 ] && exit 0\n' + SSH_THEN_WAIT)
    c = _c5(tmp_path, monkeypatch, script, box={'soak-1-c5-2': 'mid'})
    assert c['ok'] and c['job'] == 'soak-1-c5-2' and c['missed'] == ['soak-1-c5-1'], c


def test_c5_that_never_hits_a_launch_is_not_proven(tmp_path, monkeypatch):
    c = _c5(tmp_path, monkeypatch, 'exit 0\n')
    assert not c['ok'] and len(c['missed']) == soak.C5_TRIES and 'never' in c['proof'], c


def test_c5_reports_launches_that_failed_on_their_own(tmp_path, monkeypatch):
    c = _c5(tmp_path, monkeypatch, 'exit 3\n')
    assert not c['ok'] and c['missed'] == [] and 'failed on its own' in c['proof'], c


def test_a_c5_job_whose_worker_started_is_judged_not_excused(tmp_path, monkeypatch):
    def chaos(cid, kind, jid=None, a=None):
        out = {'ok': True, 'proof': 'fake', 'at': 0, 'back': 0}
        if kind == 'drop-iap':
            out.update(rc=-9, job=f'{jid}-1', missed=[], job_ran=True)
        return out
    rc, v, _ = run(tmp_path, monkeypatch, chaos_override=chaos)
    rec = json.load(open(tmp_path / 'soak' / 'records.json'))
    c5 = next(c for c in rec['chaos'] if c['id'] == 'c5')
    j = next(j for j in rec['jobs'] if j['id'] == c5['job'])
    assert j['launch_rc'] == 0 and j['verdict'] == 'PASS' and rc == 0, (j, v)


def test_missed_c5_launches_are_recorded_as_real_jobs_and_judged(tmp_path, monkeypatch):
    def chaos(cid, kind, jid=None, a=None):
        out = {'ok': True, 'proof': 'fake', 'at': 0, 'back': 0}
        if kind == 'drop-iap':
            out.update(rc=-9, job=f'{jid}-2', missed=[f'{jid}-1'])
        return out
    rc, v, _ = run(tmp_path, monkeypatch, chaos_override=chaos)
    rec = json.load(open(tmp_path / 'soak' / 'records.json'))
    c5 = next(c for c in rec['chaos'] if c['id'] == 'c5')
    ids = {j['id']: j for j in rec['jobs']}
    assert c5['job'].endswith('c5-2') and ids[c5['job']]['launch_rc'] == -9
    missed = c5['job'][:-2] + '-1'
    assert missed in ids and ids[missed]['kind'] == 'claude' and ids[missed]['launch_rc'] == 0
    assert ids[missed]['verdict'] == 'PASS' and rc == 0, v   # judged end to end; the renamed c5 job is excused


def test_a_soak_interrupted_during_c5_refuses_to_resume(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch)
    p = tmp_path / 'soak' / 'records.json'
    rec = json.load(open(p))
    c5 = next(c for c in rec['chaos'] if c['id'] == 'c5')
    rec['chaos'] = [c for c in rec['chaos'] if c['id'] != 'c5']
    next(j for j in rec['jobs'] if j['id'] == c5['job'])['id'] = re.sub(r'-c5(-\d+)?$', '-c5', c5['job'])   # the pre-record, as saved
    json.dump(rec, open(p, 'w'))
    a = type('A', (), {'out': str(tmp_path / 'soak'), 'hours': 3.0, 'prompt': 'p', 'fan_repo': 'r', 'fan_cmd': 'c'})()
    assert soak.soak(a) == 2


def test_c5_retries_an_unreadable_box_query(tmp_path, monkeypatch):
    monkeypatch.setattr(soak, 'DISCO_JOB', _fake_launcher(tmp_path, SSH_THEN_WAIT))
    calls = {'n': 0}

    def lid(cmd, timeout=600):
        if 'W=~/scratch/' in cmd:
            calls['n'] += 1
            return (255, '') if calls['n'] == 1 else (0, 'worker\n')
        return (0, '')
    monkeypatch.setattr(soak, 'lid', lid)
    real_sleep = soak.time.sleep
    monkeypatch.setattr(soak.time, 'sleep', lambda sec: real_sleep(min(sec, 0.3)))   # the query's 10 s retry wait, shortened
    c = soak.chaos('c5', 'drop-iap', jid='soak-1-c5', a=type('A', (), {'prompt': 'p'})())
    assert c['ok'] and c['job'] == 'soak-1-c5-1' and c['job_ran'] and calls['n'] == 2, c


# -- soak #2 on 6ffb7d4: one status poll in 261 came back unreadable outside chaos, during the drain, with no reason kept --

def _status_with(monkeypatch, answers):
    calls = []

    def full(cmd, timeout=600):
        calls.append(cmd)
        return answers[min(len(calls), len(answers)) - 1]
    monkeypatch.setattr(soak, '_lid_full', full)
    monkeypatch.setattr(soak.time, 'sleep', lambda s: None)
    return soak.status('soak-1-'), calls


GOOD_STATUS = (0, 'noise\n{"jobs": {}, "live": []}\n', '')


def test_status_retries_one_failed_poll(monkeypatch):
    st, calls = _status_with(monkeypatch, [(255, '', 'ssh: connect to host: Connection timed out'), GOOD_STATUS])
    assert st == {'jobs': {}, 'live': []} and len(calls) == 2


def test_status_that_stays_unreadable_keeps_its_reason(monkeypatch):
    st, calls = _status_with(monkeypatch, [(255, '', 'ssh: connect to host: Connection timed out')] * 2)
    assert st is None and len(calls) == 2 and 'timed out' in soak.STATUS_WHY['why']


def test_an_unreadable_leak_check_records_why(tmp_path, monkeypatch):
    monkeypatch.setattr(soak, 'status', lambda prefix: (soak.STATUS_WHY.update(why='rc 255: tunnel'), None)[1])
    r = soak.Run(str(tmp_path))
    soak.poll(r, 'soak-1-')
    assert r.rec['leaks'][-1]['live'] is None and r.rec['leaks'][-1]['why'] == 'rc 255: tunnel'


def test_a_recovered_poll_leaves_a_trace_and_clears_the_reason(monkeypatch):
    st, calls = _status_with(monkeypatch, [(255, '', 'ssh: Connection reset'), GOOD_STATUS])
    assert st is not None and soak.STATUS_WHY['why'] is None and 'Connection reset' in soak.STATUS_WHY['retried']


def test_unparseable_json_is_retried_and_named(monkeypatch):
    st, calls = _status_with(monkeypatch, [(0, '{"jobs": \n', '')] * 2)
    assert st is None and len(calls) == 2 and 'unparseable' in soak.STATUS_WHY['why']


def test_a_sighting_is_stamped_when_its_listing_was_read(tmp_path, monkeypatch):
    """A readable poll is stamped with the time its listing was read (a retry can read it minutes after the poll began,
    and the leak span is measured between sightings); an unreadable one with the time its poll started."""
    t = {'now': 1000.0}
    monkeypatch.setattr(soak.time, 'time', lambda: t['now'])

    def slow_status(prefix):
        t['now'] += 300          # the first attempt timed out
        soak.STATUS_WHY.update(why=None, retried='rc 124: timed out', read_at=t['now'] + 10)
        t['now'] += 20
        return {'jobs': {}, 'live': []}
    monkeypatch.setattr(soak, 'status', slow_status)
    r = soak.Run(str(tmp_path))
    soak.poll(r, 'soak-1-')
    leak = r.rec['leaks'][-1]
    assert leak['at'] == 1310.0 and leak.get('retried') == 'rc 124: timed out' and 'why' not in leak

    def dead_status(prefix):
        t['now'] += 610
        soak.STATUS_WHY.update(why='rc 255: x', retried=None, read_at=None)
        return None
    monkeypatch.setattr(soak, 'status', dead_status)
    start = t['now']
    soak.poll(r, 'soak-1-')
    assert r.rec['leaks'][-1]['at'] == start and r.rec['leaks'][-1]['why'] == 'rc 255: x'


def test_status_records_the_read_time_of_the_attempt_that_succeeded(monkeypatch):
    t = {'now': 0.0}
    monkeypatch.setattr(soak.time, 'time', lambda: t['now'])
    calls = []

    def full(cmd, timeout=600):
        calls.append(cmd)
        t['now'] += 300
        return (124, '', 'timed out') if len(calls) == 1 else GOOD_STATUS
    monkeypatch.setattr(soak, '_lid_full', full)
    monkeypatch.setattr(soak.time, 'sleep', lambda sec: t.update(now=t['now'] + sec))
    assert soak.status('soak-1-') is not None and soak.STATUS_WHY['read_at'] == 610.0


def test_a_clean_poll_after_a_retried_one_carries_no_retry(monkeypatch):
    _status_with(monkeypatch, [(255, '', 'ssh: reset'), GOOD_STATUS])
    assert soak.STATUS_WHY['retried']
    _status_with(monkeypatch, [GOOD_STATUS])
    assert soak.STATUS_WHY['retried'] is None and soak.STATUS_WHY['why'] is None
