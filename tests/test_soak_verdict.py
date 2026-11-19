"""The soak verdict (tests/soak/verdict.py), a pure function over the run's records and the criteria written before it.
Every planted defect must FAIL; only the clean record set passes."""
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from tests.soak import verdict as V  # noqa: E402   (a package path, so mutation testing maps the tests)

S = 1_000_000.0
H = 3600
CRIT = {'hours': 3, 'chaos': ['c1', 'c2', 'c3', 'c4', 'c5'], 'decoy_every_s': H}


def cred_ok():
    return {'readable': True, 'swapped': 3, 'hosts': ['api.anthropic.com']}


def clean():
    jobs = []
    for n in range(10):
        t = S + 600 + n * 900
        kind = 'claude' if n % 2 else 'fan'
        jobs.append({'id': f'j{n}', 'kind': kind, 'launched': t, 'launch_rc': 0, 'done_at': t + 700, 'verdict': 'PASS',
                     'never_forgive': False, 'worker_gone': False, 'cred': cred_ok() if kind == 'claude' else None})
    for n, (t, kind) in enumerate(((S + 9000, 'fan'), (S + 9100, 'claude'), (S + 8000, 'claude')), start=10):   # recovery after c4, c5
        jobs.append({'id': f'j{n}', 'kind': kind, 'launched': t, 'launch_rc': 0, 'done_at': t + 700, 'verdict': 'PASS',
                     'never_forgive': False, 'worker_gone': False, 'cred': cred_ok() if kind == 'claude' else None})
    chaos = [{'id': c, 'kind': k, 'at': S + o, 'until': S + o + w, 'ok': True, 'proof': 'x'}
             for c, k, o, w in (('c1', 'kill-discobox-server', 1200, 300), ('c2', 'restart-thv-github', 3000, 180),
                                ('c3', 'kill-agentgateway', 4800, 120), ('c4', 'reboot', 6600, 900), ('c5', 'drop-iap', 8400, 120))]
    leaks = [{'at': S + 60 * k, 'live': [], 'pending': []} for k in range(0, 180, 5)]
    decoys = [{'at': S + k * H, 'result': 'PASS'} for k in range(4)]
    return {'jobs': jobs, 'chaos': chaos, 'leaks': leaks, 'decoys': decoys, 'window': {'start': S, 'end': S + 3 * H + 30}}


def fails(rec, crit=CRIT):
    v, f = V.verdict(rec, crit)
    assert (v == 'PASS') == (not f)
    return f


def test_clean_passes():
    assert fails(clean()) == []


def test_missing_done_fails():
    r = clean(); r['jobs'][3].update(done_at=None, verdict=None)
    assert any('never finished' in f for f in fails(r))


def test_fail_outside_chaos_fails():
    r = clean(); r['jobs'][3]['verdict'] = 'FAIL'   # [S+3300, S+4000] overlaps no chaos window
    assert any('no chaos that affects it' in f for f in fails(r))


def test_fail_inside_chaos_passes():
    r = clean(); j = r['jobs'][0]; j.update(launched=S + 1000, done_at=S + 1250, verdict='FAIL', cred=None)   # j2 still shows fan recovery
    assert fails(r) == []


def test_fail_inside_unproven_chaos_fails():
    r = clean(); j = r['jobs'][1]; j.update(launched=S + 1000, done_at=S + 1250, verdict='FAIL', cred=None)
    r['chaos'][0]['ok'] = False
    f = fails(r)
    assert any('no chaos that affects it' in x for x in f) and any('c1' in x and 'not proven' in x for x in f)


def test_leak_or_violation_never_forgiven():
    r = clean(); j = r['jobs'][1]; j.update(launched=S + 1000, done_at=S + 1250, verdict='FAIL', never_forgive=True)
    assert any('LEAK or VIOLATION' in f for f in fails(r))


def test_never_forgive_fails_a_pass_too():
    r = clean(); r['jobs'][2]['never_forgive'] = True
    assert any('LEAK or VIOLATION' in f for f in fails(r))


def test_worker_gone_forgiven_only_by_a_reboot():
    r = clean(); j = r['jobs'][7]; j.update(launched=S + 6000, done_at=S + 6800, verdict='FAIL', worker_gone=True, cred=None)
    assert fails(r) == []
    r = clean(); j = r['jobs'][1]; j.update(launched=S + 1000, done_at=S + 1250, verdict='FAIL', worker_gone=True, cred=None)
    assert any('worker died with no reboot' in f for f in fails(r))


def test_launch_failure_outside_chaos_fails():
    r = clean(); r['jobs'][0].update(launch_rc=3, done_at=None, verdict=None)
    assert any('launch failed' in f for f in fails(r))


def test_launch_failure_inside_chaos_passes():
    r = clean(); r['jobs'][0].update(launched=S + 1210, launch_rc=3, done_at=None, verdict=None)
    assert fails(r) == []


def test_claude_pass_needs_a_swapped_exchange():
    r = clean(); r['jobs'][1]['cred'] = {'readable': True, 'swapped': 0, 'hosts': []}
    assert any('no credential exchange' in f for f in fails(r))


def test_claude_pass_needs_a_readable_audit():
    for c in (None, {'readable': False, 'swapped': 0, 'hosts': []}):
        r = clean(); r['jobs'][1]['cred'] = c
        assert any('audit' in f for f in fails(r)), c


def test_credential_to_another_host_fails():
    r = clean(); r['jobs'][1]['cred'] = {'readable': True, 'swapped': 2, 'hosts': ['api.anthropic.com', 'evil.example']}
    assert any('evil.example' in f for f in fails(r))


def test_credential_to_another_host_fails_on_a_failed_job_too():
    r = clean(); j = r['jobs'][1]
    j.update(launched=S + 1000, done_at=S + 1250, verdict='FAIL', cred={'readable': True, 'swapped': 1, 'hosts': ['evil.example']})
    assert any('evil.example' in f for f in fails(r))


def test_missing_planned_chaos_fails():
    r = clean(); del r['chaos'][2]
    assert any('c3' in f for f in fails(r))


def test_short_run_fails():
    r = clean(); r['window']['end'] = S + 1 * H
    assert any('ran' in f and 'of 3' in f for f in fails(r))


def test_leak_seen_twice_fails():
    r = clean()
    r['leaks'][5]['live'] = [{'id': 'sbx_x', 'job': 'j0'}]
    r['leaks'][6]['live'] = [{'id': 'sbx_x', 'job': 'j0'}]
    assert any('sbx_x' in f for f in fails(r))


def test_unowned_box_seen_twice_fails():
    r = clean()
    r['leaks'][5]['live'] = [{'id': 'sbx_y', 'job': None}]
    r['leaks'][7]['live'] = [{'id': 'sbx_y', 'job': None}]
    assert any('sbx_y' in f for f in fails(r))


def test_a_box_seen_once_mid_run_passes():
    r = clean(); r['leaks'][5]['live'] = [{'id': 'sbx_x', 'job': 'j0'}]
    assert fails(r) == []


def test_a_pending_jobs_box_is_not_a_leak():
    r = clean()
    for k in (5, 6, 7):
        r['leaks'][k].update(live=[{'id': 'sbx_p', 'job': 'j3'}], pending=['j3'])
    assert fails(r) == []


def test_a_box_in_the_final_check_fails():
    r = clean(); r['leaks'][-1]['live'] = [{'id': 'sbx_z', 'job': 'j9'}]
    assert any('sbx_z' in f for f in fails(r))


def test_unreadable_listing_outside_chaos_fails():
    r = clean(); r['leaks'][3]['live'] = None
    assert any('unreadable' in f for f in fails(r))


def test_unreadable_listing_inside_chaos_passes():
    r = clean(); r['leaks'].append({'at': S + 6700, 'live': None, 'pending': []}); r['leaks'].sort(key=lambda l: l['at'])
    assert fails(r) == []


def test_final_listing_unreadable_fails():
    r = clean(); r['leaks'].append({'at': S + 3 * H + 20, 'live': None, 'pending': []})
    assert any('final' in f for f in fails(r))


def test_decoy_fail_fails():
    r = clean(); r['decoys'][1]['result'] = 'FAIL'
    assert any('decoy' in f for f in fails(r))


def test_decoys_bunched_together_fail():
    r = clean(); r['decoys'] = [{'at': S + k * 30, 'result': 'PASS'} for k in range(4)]
    assert any('decoy gap' in f for f in fails(r))


def test_no_jobs_fails():
    r = clean(); r['jobs'] = []
    assert any('no jobs' in f for f in fails(r))


def test_the_input_is_not_changed():
    r = clean(); r['jobs'][0]['verdict'] = 'FAIL'; before = copy.deepcopy(r)
    V.verdict(r, CRIT)
    assert r == before



def test_a_fail_whose_life_merely_overlaps_chaos_fails():
    r = clean(); j = r['jobs'][1]; j.update(launched=S + 1100, done_at=S + 2500, verdict='FAIL', cred=None)   # c1 is [1200, 1500]
    assert any('no chaos that affects it' in f for f in fails(r))


def test_c2_c3_c5_forgive_nothing():
    for c in (1, 2, 4):   # c2 restart-thv-github, c3 kill-agentgateway, c5 drop-iap
        ch = clean()['chaos'][c]
        r = clean(); j = r['jobs'][1]; j.update(launched=ch['at'] - 100, done_at=ch['at'] + 10, verdict='FAIL', cred=None)
        assert any('no chaos that affects it' in f for f in fails(r)), ch['id']


def test_each_chaos_needs_a_pass_of_each_kind_after_it():
    r = clean(); r['jobs'] = [j for j in r['jobs'] if j['id'] != 'j10']
    assert any('c5' in f and 'fan' in f and 'after' in f for f in fails(r))


def test_the_c5_job_alone_is_excused_its_killed_launch():
    r = clean(); r['jobs'].append({'id': 'jc5', 'kind': 'claude', 'launched': S + 8400, 'launch_rc': -9, 'done_at': None, 'verdict': None})
    r['chaos'][4]['job'] = 'jc5'
    assert fails(r) == []
    r['chaos'][4]['job'] = 'other'
    assert any('jc5' in f for f in fails(r))


def test_the_c5_job_if_it_ran_must_pass():
    r = clean(); r['jobs'].append({'id': 'jc5', 'kind': 'fan', 'launched': S + 8400, 'launch_rc': -9, 'done_at': S + 9000,
                                   'verdict': 'FAIL', 'never_forgive': False, 'worker_gone': False, 'cred': None})
    r['chaos'][4]['job'] = 'jc5'
    assert any('jc5' in f for f in fails(r))


def test_a_reboot_forgives_a_worker_closed_soon_after_it():
    r = clean(); c4 = r['chaos'][3]
    j = r['jobs'][7]; j.update(launched=c4['at'] - 500, done_at=c4['until'] - 1, verdict='FAIL', worker_gone=True, cred=None)
    assert fails(r) == []
    j.update(done_at=c4['until'] + 1000)
    assert any('worker died with no reboot' in f for f in fails(r))


def test_the_soak_must_be_three_hours():
    r = clean(); r['window']['end'] = S + 2.5 * H + 30
    assert any('3 h' in f for f in fails(r, dict(CRIT, hours=2.5)))


def test_credential_host_with_a_port_is_the_lock_host():
    r = clean(); r['jobs'][1]['cred'] = {'readable': True, 'swapped': 2, 'hosts': ['api.anthropic.com:443']}
    assert fails(r) == []




def test_recovery_is_shown_between_each_chaos_and_the_next():
    r = clean(); r['jobs'] = [j for j in r['jobs'] if j['id'] != 'j12']   # the only claude launched in [c4 until, c5 at)
    assert any('c4' in f and 'claude' in f for f in fails(r))


def test_a_reboot_does_not_excuse_a_job_launched_long_before_it():
    r = clean(); c4 = r['chaos'][3]
    j = r['jobs'][3]; j.update(launched=c4['at'] - 3000, done_at=c4['at'] + 200, verdict='FAIL', worker_gone=True, cred=None)
    assert any('worker died with no reboot' in f for f in fails(r))


LOAD_CRIT = {'maxbox': 6, 'fan_cap': 12}


def load_clean():
    steps = [{'step': n, 'jobs': int(n), 'boxed': int(n), 'refused': 0, 'failed': 0, 'live_max': int(n)} for n in ('1', '2', '4', '6')]
    steps += [{'step': 'over-cap-7', 'jobs': 7, 'boxed': 6, 'refused': 1, 'failed': 0, 'live_max': 6},
              {'step': 'backlog-12', 'jobs': 12, 'boxed': 12, 'refused': 0, 'failed': 0, 'live_max': 6},
              {'step': 'fan-12', 'jobs': 1, 'boxed': 1, 'refused': 0, 'failed': 0, 'live_max': 12}]
    return {'load': steps, 'jobs': [], 'leaks': [{'at': 1, 'live': [], 'pending': []}]}


def test_load_clean_passes():
    assert V.load_verdict(load_clean(), LOAD_CRIT) == ('PASS', [])


def test_load_needs_every_step():
    r = load_clean(); r['load'] = r['load'][:1]
    v, f = V.load_verdict(r, LOAD_CRIT)
    assert v == 'FAIL' and any('missing' in x for x in f)


def test_load_over_cap_must_fill_the_cap():
    r = load_clean(); r['load'][4].update(boxed=3, refused=4, live_max=3)
    v, f = V.load_verdict(r, LOAD_CRIT)
    assert v == 'FAIL' and any('over-cap' in x for x in f)



def test_load_fan_step_over_the_box_ceiling_fails():
    r = load_clean()
    next(s for s in r['load'] if s['step'] == 'fan-12')['live_max'] = 13
    v, f = V.load_verdict(r, LOAD_CRIT)
    assert v == 'FAIL' and any('fan' in x and 'live' in x for x in f), f


def test_load_without_a_fan_cap_fails():
    v, f = V.load_verdict(load_clean(), {'maxbox': 6})
    assert v == 'FAIL' and any('fan_cap' in x for x in f), f
