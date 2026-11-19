"""Tests written from the mutation run on 68f1d7f: each pins a boundary, a default or a path that a surviving
mutant showed was untested. Package imports, so mutation testing maps them."""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from scripts.lid import job_sweep as js  # noqa: E402
from tests.soak import box_status as B  # noqa: E402
from tests.soak import verdict as V  # noqa: E402

S = 1_000_000.0


# -- verdict: windows and boundaries ---------------------------------------------------------------------------------

def test_window_edges_are_inclusive():
    w = [{'id': 'c', 'at': 10, 'until': 20}]
    assert V._inside(20, 30, w) == ['c'] and V._inside(0, 10, w) == ['c'] and V._inside(21, 30, w) == [] and V._inside(0, 9, w) == []
    assert V._at(10, w) == ['c'] and V._at(20, w) == ['c'] and V._at(9, w) == [] and V._at(21, w) == []


def base(jobs, chaos, end=S + 4 * 3600):
    return {'jobs': jobs, 'chaos': chaos, 'leaks': [{'at': S, 'live': [], 'pending': []}],
            'decoys': [{'at': S + k * 3600, 'result': 'PASS'} for k in range(5)], 'window': {'start': S, 'end': end}}


def job(i, kind, launched, done, v='PASS', **kw):
    d = {'id': i, 'kind': kind, 'launched': launched, 'launch_rc': 0, 'done_at': done, 'verdict': v,
         'never_forgive': False, 'worker_gone': False,
         'cred': {'readable': True, 'swapped': 1, 'hosts': ['api.anthropic.com']} if kind == 'claude' else None}
    d.update(kw)
    return d


CR = {'hours': 4, 'chaos': ['c4'], 'decoy_every_s': 3600}
REBOOT = {'id': 'c4', 'kind': 'reboot', 'at': S + 5000, 'until': S + 6000, 'ok': True, 'proof': 'x'}
AFTER = [job('pa', 'claude', S + 6000, S + 6100), job('pb', 'fan', S + 6000, S + 6100)]


@pytest.mark.parametrize('launched,done,ok', [
    (S + 5000 - V.MAX_JOB_S, S + 5000, True),       # launched exactly one job lifetime before, closed exactly at the reboot
    (S + 5000 - V.MAX_JOB_S - 1, S + 5500, False),
    (S + 6000, S + 6000, True),                     # launched and closed at the window's last second
    (S + 4000, S + 6001, False),
    (S + 4000, S + 4999, False),
])
def test_reboot_excuse_edges(launched, done, ok):
    r = base([job('w', 'fan', launched, done, 'FAIL', worker_gone=True)] + AFTER, [REBOOT])
    v, f = V.verdict(r, CR)
    assert (v == 'PASS') == ok, f


def test_a_failed_job_without_a_done_time_is_judged_at_its_launch():
    c1 = {'id': 'c4', 'kind': 'kill-discobox-server', 'at': S + 100, 'until': S + 200, 'ok': True, 'proof': 'x'}
    r = base([job('x', 'fan', S + 150, None, 'FAIL'), job('pa', 'claude', S + 300, S + 400), job('pb', 'fan', S + 300, S + 400)], [c1])
    assert V.verdict(r, CR) == ('PASS', [])


def test_exact_messages():
    r = base([], [])
    assert V.verdict(r, {'hours': 4, 'chaos': [], 'decoy_every_s': 3600})[1] == ['no jobs ran', 'no chaos was injected']


def test_recovery_checks_continue_past_an_unproven_chaos():
    bad = {'id': 'c1', 'kind': 'kill-discobox-server', 'at': S + 100, 'until': S + 200, 'ok': False, 'proof': 'NOPID'}
    good = {'id': 'c2', 'kind': 'kill-discobox-server', 'at': S + 1000, 'until': S + 1100, 'ok': True, 'proof': 'x'}
    r = base([job('a', 'claude', S + 1200, S + 1300)], [bad, good])
    f = V.verdict(r, {'hours': 4, 'chaos': ['c1', 'c2'], 'decoy_every_s': 3600})[1]
    assert 'chaos c2: no fan job PASSed after it and before the next (recovery not shown)' in f


def test_recovery_with_no_window_end_is_open_ended():
    c = {'id': 'c1', 'kind': 'reboot', 'at': S + 100, 'until': S + 200, 'ok': True, 'proof': 'x'}
    r = base([job('a', 'claude', S + 300, S + 400), job('b', 'fan', S + 300, S + 400)], [c])
    r['window'] = {'start': S}
    assert not any('recovery' in x for x in V.verdict(r, {'hours': 4, 'chaos': ['c1'], 'decoy_every_s': 3600})[1])


def test_recovery_must_launch_before_the_next_chaos():
    c1 = {'id': 'c1', 'kind': 'reboot', 'at': S + 100, 'until': S + 200, 'ok': True, 'proof': 'x'}
    c2 = {'id': 'c2', 'kind': 'reboot', 'at': S + 1000, 'until': S + 1100, 'ok': True, 'proof': 'x'}
    jobs = [job('a', 'claude', S + 1000, S + 1050), job('b', 'fan', S + 1000, S + 1050),
            job('c', 'claude', S + 1200, S + 1300), job('d', 'fan', S + 1200, S + 1300)]
    f = V.verdict(base(jobs, [c1, c2]), {'hours': 4, 'chaos': ['c1', 'c2'], 'decoy_every_s': 3600})[1]
    assert any(x.startswith('chaos c1: no claude') for x in f) and any(x.startswith('chaos c1: no fan') for x in f)


@pytest.mark.parametrize('crit_hours,ran,expect_ran_fail', [(None, 600, False), (4, 4 * 3600 - V.WINDOW_SLACK_S, False), (4, 4 * 3600 - V.WINDOW_SLACK_S - 1, True)])
def test_hours_and_duration(crit_hours, ran, expect_ran_fail):
    r = base([job('a', 'claude', S + 10, S + 20)], [{'id': 'c1', 'kind': 'x', 'at': S, 'until': S + 1, 'ok': True, 'proof': 'x'}], end=S + ran)
    crit = {'chaos': [], 'decoy_every_s': None}
    if crit_hours:
        crit['hours'] = crit_hours
    f = V.verdict(r, crit)[1]
    assert any(x.startswith('ran ') for x in f) == expect_ran_fail, f
    assert any('the soak must run 3 h' in x for x in f) == (crit_hours is None)


def test_window_without_start_or_end():
    r = base([job('a', 'claude', S + 10, S + 20)], [{'id': 'c1', 'kind': 'x', 'at': S, 'until': S + 1, 'ok': True, 'proof': 'x'}])
    r['window'] = {'end': 4 * 3600 + 10}
    f = V.verdict(r, {'hours': 4, 'chaos': [], 'decoy_every_s': 3600})[1]
    assert not any(x.startswith('ran ') for x in f) and not any('decoy gap' in x for x in f)


def test_leak_span_and_unreadable_order():
    leaks = [{'at': 0, 'live': None, 'pending': []},
             {'at': 10, 'live': [{'id': 'a', 'job': 'j'}], 'pending': []},
             {'at': 40, 'live': [{'id': 'a', 'job': 'j'}], 'pending': []},
             {'at': 100, 'live': [], 'pending': []}]
    f = V._leaks(leaks, [{'id': 'c', 'at': 0, 'until': 5}])
    assert f == []    # an unreadable listing inside chaos, then a box seen twice 30 s apart: under the 60 s span
    leaks[2]['at'] = 10 + V.LEAK_MIN_SPAN_S
    assert V._leaks(leaks, [{'id': 'c', 'at': 0, 'until': 5}]) == ['leak: box a outlived its job (2 checks over 60 s)']


def test_decoy_gap_edge():
    w = {'start': 0, 'end': 3600 + V.DECOY_SLACK_S}
    assert V._decoys([], w, 3600) == []
    assert V._decoys([], {'start': 0, 'end': 3601 + V.DECOY_SLACK_S}, 3600) == [f'decoy gap {3601 + V.DECOY_SLACK_S} s exceeds {3600 + V.DECOY_SLACK_S} s']
    assert V._decoys([], {'end': 10 ** 9}, 3600) == [] and V._decoys([], {'start': 0}, 3600) == []


# -- load_verdict ------------------------------------------------------------------------------------------------------

def steps(**over):
    st = [{'step': n, 'jobs': int(n), 'boxed': int(n), 'refused': 0, 'failed': 0, 'live_max': int(n)} for n in ('1', '2', '4', '6')]
    st += [{'step': 'over-cap-7', 'jobs': 7, 'boxed': 6, 'refused': 1, 'failed': 0, 'live_max': 6},
           {'step': 'backlog-12', 'jobs': 12, 'boxed': 12, 'refused': 0, 'failed': 0, 'live_max': 6},
           {'step': 'fan-12', 'jobs': 1, 'boxed': 1, 'refused': 0, 'failed': 0, 'live_max': 12}]
    for s in st:
        s.update(over.get(s['step'], {}))
    return st


def lrec(st=None, jobs=None, final=()):
    return {'load': st if st is not None else steps(), 'jobs': jobs or [], 'leaks': [{'at': 1, 'live': list(final), 'pending': []}]}


FAN12 = {'fan_cap': 12}   # fan_cap is required (the pool's measured box ceiling); maxbox defaults to 6


def test_load_defaults_are_the_documented_caps():
    assert V.load_verdict(lrec(), FAN12) == ('PASS', [])
    assert any('no fan_cap' in f for f in V.load_verdict(lrec(), {})[1])


def test_load_custom_caps():
    st = steps()
    st[3].update(live_max=4); st[5].update(live_max=4)   # with a cap of 4, nothing holds more than 4 at once
    st[4].update(step='over-cap-5', jobs=5, boxed=4, live_max=4)
    st[6].update(step='fan-8', live_max=8)   # the fan step stays within its box ceiling
    assert V.load_verdict(lrec(st), {'maxbox': 4, 'fan_cap': 8}) == ('PASS', [])
    assert V.load_verdict(lrec(st), {})[0] == 'FAIL'


@pytest.mark.parametrize('over,expect', [
    ({'over-cap-7': {'boxed': 7, 'refused': 0, 'live_max': 7}},
     ['load over-cap step: 7 boxes and 7 live for 7 jobs (cap 6)', 'load over-cap step: nothing was refused (the cap of 6 was never exercised)',
      'load step over-cap-7: 7 boxes live at once (cap 6)']),
    ({'over-cap-7': {'failed': 1}}, ['load over-cap step: 1 refused, 1 failed of 7']),
    ({'over-cap-7': {'refused': 0, 'jobs': 6}}, ['load over-cap step: nothing was refused (the cap of 6 was never exercised)']),
    ({'4': {'failed': 2}}, ['load step 4: 2 job(s) FAIL or unfinished']),
    ({'6': {'live_max': 7}}, ['load step 6: 7 boxes live at once (cap 6)']),
    ({'fan-12': {'live_max': 12}}, []),
    ({'2': {'mem_used_max_mb': 900, 'mem_total_mb': 1000}}, []),
    ({'2': {'mem_used_max_mb': 901, 'mem_total_mb': 1000}}, ['load step 2: memory 901 of 1000 MB (over 90%)']),
])
def test_load_step_rules(over, expect):
    assert V.load_verdict(lrec(steps(**over)), FAN12)[1] == expect


def test_load_steps_missing_keys_use_safe_defaults():
    st = steps()
    for s in st:
        for k in ('failed', 'refused', 'live_max'):
            s.pop(k, None)
    st[4]['refused'] = 1
    assert V.load_verdict(lrec(st), FAN12) == ('PASS', [])


def test_load_job_rules():
    ok = {'readable': True, 'swapped': 1, 'hosts': ['api.anthropic.com']}
    jobs = [{'id': 'a', 'kind': 'claude', 'verdict': 'PASS', 'cred': ok},
            {'id': 'b', 'kind': 'claude', 'verdict': 'PASS', 'cred': None},
            {'id': 'c', 'kind': 'claude', 'verdict': 'PASS', 'cred': {'readable': True, 'swapped': 0, 'hosts': []}},
            {'id': 'd', 'kind': 'claude', 'verdict': 'FAIL', 'cred': None},
            {'id': 'e', 'kind': 'fan', 'verdict': 'PASS', 'cred': None},
            {'id': 'f', 'kind': 'claude', 'verdict': 'PASS', 'cred': {'readable': True, 'swapped': 1, 'hosts': ['evil:443']}},
            {'id': 'g', 'kind': 'fan', 'verdict': 'PASS', 'never_forgive': True}]
    assert V.load_verdict(lrec(jobs=jobs), FAN12)[1] == [
        'job b: PASS without a swapped exchange in a readable audit', 'job c: PASS without a swapped exchange in a readable audit',
        'job f: credential exchange to evil:443', 'job g: its log or audit records a LEAK or VIOLATION']


def test_load_final_check():
    assert V.load_verdict({'load': steps(), 'jobs': [], 'leaks': []}, FAN12)[1] == ['final leak check: listing unreadable']
    assert V.load_verdict({'load': steps(), 'jobs': [], 'leaks': [{'at': 1, 'live': None}]}, FAN12)[1] == ['final leak check: listing unreadable']
    assert V.load_verdict(lrec(final=[{'id': 'x', 'job': None}]), FAN12)[1] == ["final leak check: 1 box(es) still live: ['x']"]


# -- box_status --------------------------------------------------------------------------------------------------------

def mk(root, name, log_bytes, audit_txt=None):
    d = root / name
    (d / 'logs').mkdir(parents=True)
    (d / 'out').mkdir()
    (d / 'logs' / 'run.log').write_bytes(log_bytes)
    if audit_txt is not None:
        (d / 'out' / 'audit.txt').write_bytes(audit_txt)
    return d


def test_status_survives_non_utf8_logs(tmp_path):
    mk(tmp_path, 'soak-1-1', b'== agent 01:00:00\n\xff\xfe junk\n== done 01:00:05\n', audit_txt=b'\xff VIOLATION x\n')
    j = B.status(str(tmp_path), 'soak-1-', {'sandboxes': []})['jobs']['soak-1-1']
    assert j['verdict'] == 'PASS' and j['never_forgive'] is True


def test_owner_by_name_when_there_is_no_display_name(tmp_path):
    mk(tmp_path, 'soak-1-1', b'== agent 01:00:00\n')
    live = B.status(str(tmp_path), 'soak-1-', {'sandboxes': [{'id': 'a', 'name': 'soak-1-1-123456-abcdef-0', 'runtime': {'state': 'running'}},
                                                             {'id': 'b', 'displayName': None, 'name': 'soak-1-1-123456-abcdef-1', 'runtime': {'state': 'running'}},
                                                             {'id': 'c', 'runtime': {'state': 'running'}}]})['live']
    assert live == [{'id': 'a', 'job': 'soak-1-1'}, {'id': 'b', 'job': 'soak-1-1'}, {'id': 'c', 'job': None}]


def test_listing_passes_exact_args_and_needs_rc0(tmp_path):
    argf = tmp_path / 'args'
    ok = tmp_path / 'ok.sh'
    ok.write_text(f'#!/bin/sh\necho "$@" > {argf}\necho \'{{"sandboxes": []}}\'\n'); ok.chmod(0o755)
    assert B.listing(str(ok)) == {'sandboxes': []} and argf.read_text().strip() == '--auto-start-server=false ls -o json'
    bad = tmp_path / 'bad.sh'
    bad.write_text('#!/bin/sh\necho \'{"sandboxes": []}\'\nexit 3\n'); bad.chmod(0o755)
    assert B.listing(str(bad)) is None


def test_host_reads_the_given_proc(tmp_path):
    p = tmp_path / 'p'
    p.mkdir()
    (p / 'loadavg').write_text('0.25 0 0 1/1 1\n')
    (p / 'meminfo').write_text('MemTotal: 2048 kB\nMemAvailable: 1024 kB\n')
    assert B.host(str(p)) == {'load1': 0.25, 'mem_used_mb': 1, 'mem_total_mb': 2}


def test_box_status_main(tmp_path, monkeypatch, capsys):
    (tmp_path / 'scratch').mkdir()
    monkeypatch.setattr(B.os.path, 'expanduser', lambda p: str(tmp_path))
    ok = tmp_path / 'db.sh'
    ok.write_text('#!/bin/sh\necho \'{"sandboxes": []}\'\n'); ok.chmod(0o755)
    monkeypatch.setattr(B, 'host', lambda proc='/proc': {'load1': 1.0, 'mem_used_mb': 1, 'mem_total_mb': 2})
    assert B.main(['x', 'soak-1-', str(ok)]) == 0
    assert json.loads(capsys.readouterr().out) == {'jobs': {}, 'live': [], 'host': {'load1': 1.0, 'mem_used_mb': 1, 'mem_total_mb': 2}}


# -- job_sweep ---------------------------------------------------------------------------------------------------------

def test_processes_skips_unreadable_and_keeps_going(tmp_path):
    p = tmp_path / 'proc'
    for pid, raw in (('1', None), ('2', b'a\x00\xff\x00'), ('3', b'')):
        (p / pid).mkdir(parents=True)
        if raw is not None:
            (p / pid / 'cmdline').write_bytes(raw)
    assert js.processes(str(p)) == ['a \ufffd']


def test_orphans_reads_non_utf8_logs(tmp_path):
    d = tmp_path / 'j'
    (d / 'job').mkdir(parents=True)
    (d / 'logs').mkdir()
    (d / 'job' / 'worker.sh').write_text('')
    (d / 'logs' / 'run.log').write_bytes(b'\xff\n== done 01:00:00\n')
    assert js.orphans(str(tmp_path), [], 10 ** 10) == []


def test_boxes_of_name_fallback_and_skips_archived_first():
    lst = {'sandboxes': [{'id': 'z', 'name': 'j-123456-abcdef-1', 'runtime': {'state': 'archived'}},
                         {'id': 'a', 'name': 'j-123456-abcdef-1', 'runtime': {'state': 'running'}},
                         {'id': 'b', 'displayName': None, 'name': 'j-123456-abcdef-2', 'runtime': {'state': 'running'}}]}
    assert js.boxes_of('j', '/s/j', '', lst) == ['a', 'b']


class DB:
    def __init__(self, listing, stuck=(), fail_rm=()):
        self.listing, self.stuck, self.fail_rm = listing, set(stuck), set(fail_rm)
        self.removed, self.sleeps, self.audits = [], [], []

    def ls(self):
        d = json.loads(json.dumps(self.listing))
        d['sandboxes'] = [b for b in d['sandboxes'] if not (b['id'] in self.removed and b['id'] not in self.stuck)]
        return d

    def rm(self, i):
        if i in self.fail_rm:
            raise RuntimeError('')
        self.removed.append(i)

    def stop_agent(self, i):
        pass

    def audit(self, jobdir, i):
        self.audits.append((jobdir, i))


def mkjob(root, name, log, audit=False, conf='REPO=x\n'):
    d = root / name
    (d / 'job').mkdir(parents=True)
    (d / 'logs').mkdir()
    (d / 'out').mkdir()
    (d / 'job' / 'worker.sh').write_text('')
    (d / 'job' / 'job.conf').write_bytes(conf.encode() if isinstance(conf, str) else conf)
    if audit:
        (d / 'job' / 'audit.py').write_text('')
    (d / 'logs' / 'run.log').write_bytes(log)
    os.utime(d / 'logs' / 'run.log', (0, 0))
    return d


def test_a_box_gone_from_the_listing_is_archived_and_sleeps_are_5s(tmp_path):
    d = mkjob(tmp_path, 'j', b'== agent 01:00:00\n-- box sbx_a\n\xff\n', audit=True, conf=b'SHARDS=1\n\xff')
    db = DB({'sandboxes': [{'id': 'sbx_a', 'displayName': 'x', 'runtime': {'state': 'running'}}]})
    out = js.sweep(str(tmp_path), [], 10 ** 10, db, sleep=db.sleeps.append)
    assert out == {'j': {'boxes': ['sbx_a'], 'leaked': []}} and db.sleeps == [5] and db.audits == [(str(d), 'sbx_a')]
    tail = (d / 'logs' / 'run.log').read_text(errors='replace').splitlines()[-1]
    assert tail == '== done FAIL (worker gone)'


def test_the_default_close_time_is_utc_hms(tmp_path):
    import re
    d = mkjob(tmp_path, 'j', b'== agent 01:00:00\n')
    js.sweep(str(tmp_path), [], 10 ** 10, DB({'sandboxes': []}), sleep=lambda s: None)
    assert re.fullmatch(r'== done FAILED at agent \(worker gone\) \d\d:\d\d:\d\d', (d / 'logs' / 'run.log').read_text().splitlines()[-1])


def test_an_error_on_the_second_box_leaks_only_it_and_later(tmp_path):
    d = mkjob(tmp_path, 'j', b'== agent 01:00:00\n-- box sbx_a\n-- box sbx_b\n-- box sbx_c\n')
    db = DB({'sandboxes': [{'id': 'sbx_' + i, 'displayName': 'x', 'runtime': {'state': 'running'}} for i in 'abc']}, fail_rm={'sbx_b'})
    out = js.sweep(str(tmp_path), [], 10 ** 10, db, sleep=lambda s: None)
    assert out['j'] == {'boxes': ['sbx_a', 'sbx_b', 'sbx_c'], 'leaked': ['sbx_b', 'sbx_c'], 'error': 'RuntimeError'}
    assert '-- sweep: RuntimeError' in (d / 'logs' / 'run.log').read_text()


def test_listing_error_message():
    with pytest.raises(RuntimeError, match='the box listing has no sandboxes list'):
        js._listing(type('D', (), {'ls': lambda self: {}})())


def test_discobox_wrapper(tmp_path):
    log = tmp_path / 'calls'
    exe = tmp_path / 'discobox'
    exe.write_text(f'#!/bin/sh\necho "$@" >> {log}\ncase "$1" in ls) echo \'{{"sandboxes": []}}\';; shell) exit 0;; rm) exit 0;; esac\n')
    exe.chmod(0o755)
    db = js.Discobox(str(exe))
    assert db.ls() == {'sandboxes': []}
    db.rm('a')
    db.stop_agent('b')
    assert log.read_text().splitlines() == ['ls -o json', 'rm a', 'shell b -- sudo -n systemctl stop job-agent']
    bad = tmp_path / 'bad'
    bad.write_text('#!/bin/sh\necho oops >&2\nexit 1\n'); bad.chmod(0o755)
    with pytest.raises(RuntimeError, match='discobox ls failed: oops'):
        js.Discobox(str(bad)).ls()


def test_discobox_audit(tmp_path):
    jd = tmp_path / 'j'
    (jd / 'job').mkdir(parents=True)
    (jd / 'out').mkdir()
    (jd / 'job' / 'audit.py').write_text('import sys\nprint("audited", sys.argv[1], sys.argv[2].endswith("/out"))\nsys.exit(int(sys.argv[1] == "bad"))\n')
    db = js.Discobox('/nonexistent')
    db.audit(str(jd), 'a')
    assert (jd / 'out' / 'audit.txt').read_text() == 'box a (audited by job_sweep)\naudited a True\n'
    with pytest.raises(RuntimeError, match='audit failed'):
        db.audit(str(jd), 'bad')


def test_main_happy_and_leak_paths(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(js.os.path, 'expanduser', lambda p: str(tmp_path))
    assert js.main() == 0                                    # no scratch dir: nothing to do
    (tmp_path / 'scratch').mkdir()
    monkeypatch.setattr(js, 'processes', lambda proc='/proc': [])
    for result, rc, text in (({}, 0, ''),
                             ({'j': {'boxes': ['a'], 'leaked': []}}, 0, "job_sweep: closed j: boxes ['a'] leaked []\n"),
                             ({'j': {'boxes': ['a'], 'leaked': ['a']}}, 1, "job_sweep: closed j: boxes ['a'] leaked ['a']\n"),
                             ({'j': {'boxes': [], 'leaked': [], 'error': 'boom'}}, 1, 'job_sweep: closed j: boxes [] leaked [] error boom\n')):
        seen = {}
        monkeypatch.setattr(js, 'sweep', lambda sc, pr, now, db, r=result: (seen.update(sc=sc, exe=db.exe), r)[1])
        assert js.main() == rc
        assert capsys.readouterr().out == text
        assert seen == {'sc': str(tmp_path / 'scratch'), 'exe': str(tmp_path / '.local' / 'bin' / 'discobox')}


# -- the second mutation pass (8f54da9) ---------------------------------------------------------------------------------

def test_processes_reads_past_an_unreadable_first_entry(tmp_path):
    p = tmp_path / 'proc'
    (p / '1').mkdir(parents=True)                       # no cmdline: unreadable, and first in sorted order
    (p / '2').mkdir()
    (p / '2' / 'cmdline').write_bytes(b'bash\x00/h/scratch/j/job/worker.sh\x00')
    assert js.processes(str(p)) == ['bash /h/scratch/j/job/worker.sh']


def test_an_earlier_leak_survives_a_later_error(tmp_path):
    d = mkjob(tmp_path, 'j', b'== agent 01:00:00\n-- box sbx_a\n-- box sbx_b\n')
    db = DB({'sandboxes': [{'id': i, 'displayName': 'x', 'runtime': {'state': 'running'}} for i in ('sbx_a', 'sbx_b')]},
            stuck={'sbx_a'}, fail_rm={'sbx_b'})
    out = js.sweep(str(tmp_path), [], 10 ** 10, db, sleep=lambda s: None)
    assert out['j']['leaked'] == ['sbx_a', 'sbx_b']


def test_boxes_of_by_display_name_alone():
    lst = {'sandboxes': [{'id': 'sbx_d', 'displayName': 'j-010101-abcdef-0', 'runtime': {'state': 'running'}}]}
    assert js.boxes_of('j', '/s/j', '', lst) == ['sbx_d']


def test_owner_by_directory_picks_the_right_job(tmp_path):
    mk(tmp_path, 'soak-1-1', b'== agent 01:00:00\n')
    mk(tmp_path, 'soak-1-2', b'== agent 01:00:00\n')
    lst = {'sandboxes': [{'id': 'a', 'runtime': {'state': 'running'}, 'config': {'source': {'localDirectory': str(tmp_path / 'soak-1-2' / 'src')}}}]}
    assert B.status(str(tmp_path), 'soak-1-', lst)['live'] == [{'id': 'a', 'job': 'soak-1-2'}]


def test_box_status_main_default_paths(tmp_path, monkeypatch, capsys):
    (tmp_path / 'scratch').mkdir()
    mk(tmp_path / 'scratch', 'soak-9-1', b'== done 01:00:00\n')
    exe = tmp_path / '.local' / 'bin' / 'discobox'
    exe.parent.mkdir(parents=True)
    exe.write_text('#!/bin/sh\necho \'{"sandboxes": []}\'\n'); exe.chmod(0o755)
    monkeypatch.setattr(B.os.path, 'expanduser', lambda p: str(tmp_path) if p == '~' else p)
    monkeypatch.setattr(B, 'host', lambda proc='/proc': None)
    assert B.main(['x', 'soak-9-']) == 0
    out = json.loads(capsys.readouterr().out)
    assert list(out['jobs']) == ['soak-9-1'] and out['live'] == [] and out['host'] is None


def test_verdict_word_is_exactly_fail():
    assert V.verdict({'jobs': []}, {})[0] == 'FAIL' and V.load_verdict({}, {})[0] == 'FAIL'


def test_leak_span_uses_the_difference_not_the_sum():
    leaks = [{'at': 10 ** 6, 'live': [{'id': 'a', 'job': 'j'}], 'pending': []},
             {'at': 10 ** 6 + 30, 'live': [{'id': 'a', 'job': 'j'}], 'pending': []},
             {'at': 10 ** 6 + 100, 'live': [], 'pending': []}]
    assert V._leaks(leaks, []) == []



# a fan box is the job's only if its name is exactly <job>-HHMMSS-<6 hex>-<shard>; job `mut` never claims `mut-2`'s boxes.
def test_fan_box_ownership_is_exact():
    lst = {'sandboxes': [{'id': 'sbx_mine', 'displayName': 'mut-123456-abcdef-0', 'runtime': {'state': 'running'}},
                         {'id': 'sbx_other', 'displayName': 'mut-2-123456-abcdef-0', 'runtime': {'state': 'running'}},
                         {'id': 'sbx_odd', 'displayName': 'mut-x', 'runtime': {'state': 'running'}}]}
    assert js.boxes_of('mut', '/s/mut', '', lst) == ['sbx_mine']


def test_box_status_fan_ownership_is_exact(tmp_path):
    mk(tmp_path, 'soak-1-2', b'== run 01:00:00\n')
    mk(tmp_path, 'soak-1-2-x', b'== run 01:00:00\n')
    lst = {'sandboxes': [{'id': 'a', 'displayName': 'soak-1-2-x-123456-abcdef-0', 'runtime': {'state': 'running'}},
                         {'id': 'b', 'displayName': 'soak-1-2-123456-abcdef-1', 'runtime': {'state': 'running'}}]}
    assert B.status(str(tmp_path), 'soak-1-', lst)['live'] == [{'id': 'a', 'job': 'soak-1-2-x'}, {'id': 'b', 'job': 'soak-1-2'}]


# a live box owned by a job that already wrote == done is a leak: the sweep archives it and records LEAK in the job.
def test_sweep_archives_a_closed_jobs_live_box(tmp_path):
    d = mkjob(tmp_path, 'done1', b'== agent 01:00:00\n== done FAILED at box 01:00:05\n')
    db = DB({'sandboxes': [{'id': 'sbx_late', 'displayName': 'x', 'runtime': {'state': 'running'},
                            'config': {'source': {'localDirectory': str(d / 'src')}}}]})
    out = js.sweep(str(tmp_path), [], 10 ** 10, db, sleep=lambda s: None)
    assert out == {'done1': {'boxes': ['sbx_late'], 'leaked': [], 'after_done': True}} and db.removed == ['sbx_late']
    assert (d / 'logs' / 'run.log').read_text().splitlines()[-1] == '-- box sbx_late was still live after == done: LEAK (archived by job_sweep)'


def test_sweep_leaves_a_closed_job_without_boxes_alone(tmp_path):
    d = mkjob(tmp_path, 'done1', b'== done 01:00:05\n')
    assert js.sweep(str(tmp_path), [], 10 ** 10, DB({'sandboxes': []}), sleep=lambda s: None) == {}
    assert (d / 'logs' / 'run.log').read_text() == '== done 01:00:05\n'


# a refusal outside the over-cap step fails the load verdict.
def test_load_refusal_outside_over_cap_fails():
    assert V.load_verdict(lrec(steps(**{'2': {'refused': 1, 'boxed': 1}})), FAN12)[1] == ['load step 2: 1 job(s) refused by the cap']



def test_late_leak_check_uses_a_fresh_listing(tmp_path):
    """A healthy job that archives its box and writes == done while the sweep closes an orphan is not a leak."""
    o = mkjob(tmp_path, 'a-orphan', b'== agent 01:00:00\n-- box sbx_o\n')
    h = mkjob(tmp_path, 'b-healthy', b'== agent 01:00:00\n-- box sbx_h\n')
    os.utime(h / 'logs' / 'run.log', None)                    # healthy: just written, too young to be an orphan

    class Racy(DB):
        def rm(self, i):
            super().rm(i)
            if i == 'sbx_o':                                     # meanwhile the healthy job finishes cleanly
                self.removed.append('sbx_h')
                with open(h / 'logs' / 'run.log', 'a') as f:
                    f.write('== done 01:05:00\n')
    db = Racy({'sandboxes': [{'id': i, 'displayName': 'x', 'runtime': {'state': 'running'}} for i in ('sbx_o', 'sbx_h')]})
    import time
    out = js.sweep(str(tmp_path), [], time.time(), db, sleep=lambda s: None)   # real time: the healthy job is young
    assert list(out) == ['a-orphan'] and 'LEAK' not in (h / 'logs' / 'run.log').read_text()


def test_late_leak_errors_are_isolated_and_recorded(tmp_path):
    a = mkjob(tmp_path, 'a', b'== done 01:00:00\n-- box sbx_a\n')
    b = mkjob(tmp_path, 'b', b'== done 01:00:00\n-- box sbx_b\n')
    db = DB({'sandboxes': [{'id': i, 'displayName': 'x', 'runtime': {'state': 'running'}} for i in ('sbx_a', 'sbx_b')]}, fail_rm={'sbx_a'})
    out = js.sweep(str(tmp_path), [], 10 ** 10, db, sleep=lambda s: None)
    assert out['a']['error'] and out['a']['leaked'] == ['sbx_a'] and out['b'] == {'boxes': ['sbx_b'], 'leaked': [], 'after_done': True}
    assert '-- box sbx_a was still live after == done: LEAK (NOT removed)' in (a / 'logs' / 'run.log').read_text()


def test_late_claude_box_is_stopped_and_audited_first(tmp_path):
    d = mkjob(tmp_path, 'c', b'== done 01:00:00\n-- box sbx_c\n', audit=True)

    class Rec(DB):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.calls = []

        def stop_agent(self, i):
            self.calls.append(('stop', i))

        def audit(self, jobdir, i):
            self.calls.append(('audit', i))

        def rm(self, i):
            self.calls.append(('rm', i))
            super().rm(i)
    db = Rec({'sandboxes': [{'id': 'sbx_c', 'displayName': 'x', 'runtime': {'state': 'running'}}]})
    js.sweep(str(tmp_path), [], 10 ** 10, db, sleep=lambda s: None)
    assert db.calls[:3] == [('stop', 'sbx_c'), ('audit', 'sbx_c'), ('rm', 'sbx_c')]



def test_a_failing_late_listing_keeps_the_orphan_results(tmp_path, monkeypatch, capsys):
    o = mkjob(tmp_path, 'a', b'== agent 01:00:00\n')

    class Once(DB):
        n = 0

        def ls(self):
            Once.n += 1
            if Once.n > 1:
                raise RuntimeError('ls timed out')
            return super().ls()
    out = js.sweep(str(tmp_path), [], 10 ** 10, Once({'sandboxes': []}), sleep=lambda s: None)
    assert out['a'] == {'boxes': [], 'leaked': []} and out['_late'] == {'error': 'ls timed out'}
    monkeypatch.setattr(js.os.path, 'expanduser', lambda p: str(tmp_path / 'home'))
    (tmp_path / 'home' / 'scratch').mkdir(parents=True)
    monkeypatch.setattr(js, 'processes', lambda proc='/proc': [])
    monkeypatch.setattr(js, 'sweep', lambda *a, **k: out)
    assert js.main() == 1
    text = capsys.readouterr().out
    assert 'job_sweep: closed a: boxes [] leaked []' in text and 'job_sweep: late-leak check stopped: ls timed out' in text
    assert 'before closing anything' not in text


def test_a_violation_printed_by_the_audit_reaches_the_log(tmp_path):
    for late in (False, True):
        root = tmp_path / str(late)
        d = mkjob(root, 'c', (b'== done 01:00:00\n' if late else b'== agent 01:00:00\n') + b'-- box sbx_c\n', audit=True)

        class Viol(DB):
            def audit(self, jobdir, i):
                return 'box sbx_c\nVIOLATION connector traffic: 2 exchange(s) to mcp\nrequests 9\n'
        js.sweep(str(root), [], 10 ** 10, Viol({'sandboxes': [{'id': 'sbx_c', 'displayName': 'x', 'runtime': {'state': 'running'}}]}),
                 sleep=lambda s: None)
        assert 'VIOLATION in the audit of sbx_c (job_sweep): connector traffic: 2 exchange(s) to mcp' in (d / 'logs' / 'run.log').read_text(), late


def test_discobox_audit_returns_its_output(tmp_path):
    jd = tmp_path / 'j'
    (jd / 'job').mkdir(parents=True)
    (jd / 'out').mkdir()
    (jd / 'job' / 'audit.py').write_text('print("VIOLATION x")\n')
    assert js.Discobox('/nonexistent').audit(str(jd), 'a') == 'VIOLATION x\n'


def test_an_unreadable_done_log_does_not_stop_the_late_check(tmp_path):
    bad = mkjob(tmp_path, 'a', b'== done 01:00:00\n')
    (bad / 'logs' / 'run.log').chmod(0)
    good = mkjob(tmp_path, 'b', b'== done 01:00:00\n-- box sbx_b\n')
    try:
        out = js.sweep(str(tmp_path), [], 10 ** 10, DB({'sandboxes': [{'id': 'sbx_b', 'displayName': 'x', 'runtime': {'state': 'running'}}]}),
                       sleep=lambda s: None)
    finally:
        (bad / 'logs' / 'run.log').chmod(0o644)
    if os.geteuid() != 0:
        assert out['b'] == {'boxes': ['sbx_b'], 'leaked': [], 'after_done': True}



def test_an_orphan_log_that_cannot_be_written_is_the_jobs_error(tmp_path, monkeypatch):
    mkjob(tmp_path, 'a', b'== agent 01:00:00\n')
    b = mkjob(tmp_path, 'b', b'== agent 01:00:00\n')
    real_open = open

    def flaky(path, mode='r', *a, **k):
        if str(path).endswith(os.path.join('a', 'logs', 'run.log')) and 'a' in mode:
            raise OSError(28, 'No space left on device')
        return real_open(path, mode, *a, **k)
    monkeypatch.setattr('builtins.open', flaky)
    out = js.sweep(str(tmp_path), [], 10 ** 10, DB({'sandboxes': []}), sleep=lambda s: None)
    assert 'No space left' in out['a']['error'] and out['b'] == {'boxes': [], 'leaked': []}
    assert (b / 'logs' / 'run.log').read_text().endswith('(worker gone) ' + (b / 'logs' / 'run.log').read_text().rsplit(' ', 1)[-1])


def test_violations_are_reported_and_fail_the_run(tmp_path, monkeypatch, capsys):
    mkjob(tmp_path, 'c', b'== done 01:00:00\n-- box sbx_c\n', audit=True)

    class Viol(DB):
        def audit(self, jobdir, i):
            return 'VIOLATION connector traffic: 1 exchange(s)\n'
    out = js.sweep(str(tmp_path), [], 10 ** 10, Viol({'sandboxes': [{'id': 'sbx_c', 'displayName': 'x', 'runtime': {'state': 'running'}}]}),
                   sleep=lambda s: None)
    assert out['c']['violations'] == ['VIOLATION in the audit of sbx_c (job_sweep): connector traffic: 1 exchange(s)']
    monkeypatch.setattr(js.os.path, 'expanduser', lambda p: str(tmp_path / 'home'))
    (tmp_path / 'home' / 'scratch').mkdir(parents=True)
    monkeypatch.setattr(js, 'processes', lambda proc='/proc': [])
    monkeypatch.setattr(js, 'sweep', lambda *a, **k: {'c': {'boxes': [], 'leaked': [], 'violations': ['VIOLATION x']}})
    assert js.main() == 1 and 'VIOLATION x' in capsys.readouterr().out


def test_an_unreadable_log_is_reported_every_sweep(tmp_path):
    bad = mkjob(tmp_path, 'a', b'== agent 01:00:00\n')
    (bad / 'logs' / 'run.log').chmod(0)
    try:
        out = js.sweep(str(tmp_path), [], 10 ** 10, DB({'sandboxes': []}), sleep=lambda s: None)
    finally:
        (bad / 'logs' / 'run.log').chmod(0o644)
    if os.geteuid() != 0:
        assert out == {'a': {'boxes': [], 'leaked': [], 'error': 'run.log unreadable: not closed and not checked'}}



def test_an_unreadable_job_conf_is_that_jobs_error(tmp_path, monkeypatch):
    mkjob(tmp_path, 'a', b'== agent 01:00:00\n-- box sbx_a\n')
    b = mkjob(tmp_path, 'b', b'== agent 01:00:00\n')
    real_open = open

    def flaky(path, mode='r', *a, **k):
        if str(path).endswith(os.path.join('a', 'job', 'job.conf')):
            raise OSError(5, 'Input/output error')
        return real_open(path, mode, *a, **k)
    monkeypatch.setattr('builtins.open', flaky)
    db = DB({'sandboxes': [{'id': 'sbx_a', 'displayName': 'x', 'runtime': {'state': 'running'}}]})
    out = js.sweep(str(tmp_path), [], 10 ** 10, db, sleep=lambda s: None)
    assert out['a'] == {'boxes': [], 'leaked': [], 'error': 'job.conf unreadable: not closed'} and db.removed == []
    assert out['b'] == {'boxes': [], 'leaked': []} and '(worker gone)' in (b / 'logs' / 'run.log').read_text()


def test_main_says_not_closed_for_a_job_it_could_not_close(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(js.os.path, 'expanduser', lambda p: str(tmp_path))
    (tmp_path / 'scratch').mkdir()
    monkeypatch.setattr(js, 'processes', lambda proc='/proc': [])
    monkeypatch.setattr(js, 'sweep', lambda *a, **k: {'a': {'boxes': [], 'leaked': [], 'error': 'run.log unreadable: not closed and not checked'}})
    assert js.main() == 1
    assert capsys.readouterr().out == 'job_sweep: a: run.log unreadable: not closed and not checked\n'


# -- the mutation pass on 335caa8: job_sweep's new paths (tests only; the chmod-based tests skip as root) ---------------

def _deny(monkeypatch, *suffixes):
    real_open = open

    def guarded(path, mode='r', *a, **k):
        if any(str(path).endswith(s) for s in suffixes) and 'r' in mode:
            raise PermissionError(13, 'Permission denied')
        return real_open(path, mode, *a, **k)
    monkeypatch.setattr('builtins.open', guarded)


def test_unreadable_logs_reported_as_any_user(tmp_path, monkeypatch):
    mkjob(tmp_path, 'a-bad', b'== agent 01:00:00\n')
    mkjob(tmp_path, 'b-orphan', b'== agent 01:00:00\n')
    mkjob(tmp_path, 'c-done', b'== done 01:00:00\n-- box sbx_c\n')
    _deny(monkeypatch, os.path.join('a-bad', 'logs', 'run.log'))
    db = DB({'sandboxes': [{'id': 'sbx_c', 'displayName': 'x', 'runtime': {'state': 'running'}}]})
    out = js.sweep(str(tmp_path), [], 10 ** 10, db, sleep=lambda s: None)
    assert out['a-bad'] == {'boxes': [], 'leaked': [], 'error': 'run.log unreadable: not closed and not checked'}
    assert out['b-orphan'] == {'boxes': [], 'leaked': []}                     # the walk goes on past the unreadable one
    assert out['c-done'] == {'boxes': ['sbx_c'], 'leaked': [], 'after_done': True}


def test_non_job_dirs_do_not_end_either_walk(tmp_path):
    (tmp_path / 'a-plain').mkdir()
    (tmp_path / 'b-logonly' / 'logs').mkdir(parents=True)
    (tmp_path / 'b-logonly' / 'logs' / 'run.log').write_text('== done 01:00:00\n-- box sbx_x\n')
    mkjob(tmp_path, 'c-orphan', b'== agent 01:00:00\n')
    mkjob(tmp_path, 'd-done', b'== done 01:00:00\n-- box sbx_d\n')
    db = DB({'sandboxes': [{'id': i, 'displayName': 'x', 'runtime': {'state': 'running'}} for i in ('sbx_x', 'sbx_d')]})
    out = js.sweep(str(tmp_path), [], 10 ** 10, db, sleep=lambda s: None)
    assert sorted(out) == ['c-orphan', 'd-done'] and db.removed == ['sbx_d']   # a dir without job/worker.sh is never touched


def test_orphan_violation_in_result(tmp_path):
    mkjob(tmp_path, 'o', b'== agent 01:00:00\n-- box sbx_o\n', audit=True)

    class Viol(DB):
        def audit(self, jobdir, i):
            return 'VIOLATION metadata answered 200\n'
    out = js.sweep(str(tmp_path), [], 10 ** 10, Viol({'sandboxes': [{'id': 'sbx_o', 'displayName': 'x', 'runtime': {'state': 'running'}}]}),
                   sleep=lambda s: None)
    assert out['o']['violations'] == ['VIOLATION in the audit of sbx_o (job_sweep): metadata answered 200']


def test_late_ownership_by_fan_name_only(tmp_path):
    mkjob(tmp_path, 'f', b'== done PASS\n', conf='SHARDS=1\n')
    lst = {'sandboxes': [{'id': 'sbx_n', 'name': 'f-123456-abcdef-0', 'runtime': {'state': 'running'}},
                         {'id': 'sbx_d', 'displayName': 'f-123456-abcdef-1', 'runtime': {'state': 'running'}}]}
    out = js.sweep(str(tmp_path), [], 10 ** 10, DB(lst), sleep=lambda s: None)
    assert out['f']['boxes'] == ['sbx_d', 'sbx_n']


def test_late_path_exact_lines_audit_and_sleeps(tmp_path):
    d = mkjob(tmp_path, 'c', b'== done 01:00:00\n-- box sbx_a\n-- box sbx_b\n', audit=True)

    class Rec(DB):
        def audit(self, jobdir, i):
            self.audits.append((jobdir, i))
            if i == 'sbx_a':
                raise RuntimeError('gone')
            return 'VIOLATION x\n'
    db = Rec({'sandboxes': [{'id': i, 'displayName': 'x', 'runtime': {'state': 'running'}} for i in ('sbx_a', 'sbx_b')]}, stuck={'sbx_a'})
    out = js.sweep(str(tmp_path), [], 10 ** 10, db, sleep=db.sleeps.append)
    assert db.audits == [(str(d), 'sbx_a'), (str(d), 'sbx_b')] and db.sleeps == [5] * (js.RM_TRIES + 1)
    assert out['c'] == {'boxes': ['sbx_a', 'sbx_b'], 'leaked': ['sbx_a'], 'after_done': True,
                        'violations': ['VIOLATION audit of sbx_a could not be read (job_sweep)', 'VIOLATION in the audit of sbx_b (job_sweep): x']}
    assert (d / 'logs' / 'run.log').read_text().splitlines()[-4:] == [
        'VIOLATION audit of sbx_a could not be read (job_sweep)', 'VIOLATION in the audit of sbx_b (job_sweep): x',
        '-- box sbx_a was still live after == done: LEAK (NOT removed)', '-- box sbx_b was still live after == done: LEAK (archived by job_sweep)']


def test_late_path_error_after_a_stuck_box(tmp_path):
    mkjob(tmp_path, 'c', b'== done 01:00:00\n-- box sbx_a\n-- box sbx_b\n-- box sbx_c\n')
    db = DB({'sandboxes': [{'id': i, 'displayName': 'x', 'runtime': {'state': 'running'}} for i in ('sbx_a', 'sbx_b', 'sbx_c')]},
            stuck={'sbx_a'}, fail_rm={'sbx_b'})
    out = js.sweep(str(tmp_path), [], 10 ** 10, db, sleep=lambda s: None)
    assert out['c'] == {'boxes': ['sbx_a', 'sbx_b', 'sbx_c'], 'leaked': ['sbx_a', 'sbx_b', 'sbx_c'], 'after_done': True, 'error': 'RuntimeError'}


def test_late_path_write_failure_is_the_error(tmp_path, monkeypatch):
    mkjob(tmp_path, 'c', b'== done 01:00:00\n-- box sbx_c\n')
    real_open = open

    def ro(path, mode='r', *a, **k):
        if str(path).endswith(os.path.join('c', 'logs', 'run.log')) and 'a' in mode:
            raise OSError(28, 'No space left on device')
        return real_open(path, mode, *a, **k)
    monkeypatch.setattr('builtins.open', ro)
    out = js.sweep(str(tmp_path), [], 10 ** 10, DB({'sandboxes': [{'id': 'sbx_c', 'displayName': 'x', 'runtime': {'state': 'running'}}]}),
                   sleep=lambda s: None)
    assert out['c']['error'] == 'the log could not be written: [Errno 28] No space left on device'


def test_late_listing_error_without_a_message(tmp_path):
    mkjob(tmp_path, 'c', b'== done 01:00:00\n')

    class Two(DB):
        n = 0

        def ls(self):
            Two.n += 1
            if Two.n > 1:
                raise RuntimeError()
            return super().ls()
    assert js.sweep(str(tmp_path), [], 10 ** 10, Two({'sandboxes': []}), sleep=lambda s: None) == {'_late': {'error': 'RuntimeError'}}


def test_main_arguments_and_every_line(tmp_path, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(js.os.path, 'expanduser', lambda p: str(tmp_path) if p == '~' else 'WRONG')
    (tmp_path / 'scratch').mkdir()
    monkeypatch.setattr(js, 'processes', lambda proc='/proc': ['p1'])

    def fake(sc, procs, now, db):
        seen.update(procs=procs, now_is_number=isinstance(now, float))
        return {'_late': {'error': 'e1'}, 'a': {'boxes': [], 'leaked': [], 'error': 'run.log unreadable: not closed and not checked'},
                'b': {'boxes': ['x'], 'leaked': [], 'violations': ['VIOLATION v']}}
    monkeypatch.setattr(js, 'sweep', fake)
    assert js.main() == 1
    assert capsys.readouterr().out.splitlines() == ['job_sweep: late-leak check stopped: e1',
                                                    'job_sweep: a: run.log unreadable: not closed and not checked',
                                                    "job_sweep: closed b: boxes ['x'] leaked []", 'job_sweep: b: VIOLATION v']
    assert seen == {'procs': ['p1'], 'now_is_number': True}
