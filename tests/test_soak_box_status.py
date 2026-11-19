"""The soak's box-side status (tests/soak/box_status.py), run against a fake scratch dir and a fake listing."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from tests.soak import box_status as B  # noqa: E402


def mk(root, name, log, audit_json=None, audit_txt=None, mtime=1_800_000_500):
    d = root / name
    (d / 'logs').mkdir(parents=True)
    (d / 'out').mkdir()
    p = d / 'logs' / 'run.log'
    p.write_text(log)
    os.utime(p, (mtime, mtime))
    if audit_json is not None:
        (d / 'out' / 'audit.json').write_text(audit_json if isinstance(audit_json, str) else json.dumps(audit_json))
    if audit_txt is not None:
        (d / 'out' / 'audit.txt').write_text(audit_txt)
    return d


def test_jobs_and_fields(tmp_path):
    mk(tmp_path, 'soak-1-1', '== box 01:00:00\n== setup 01:00:20\n== done 01:05:00\n',
       audit_json={'exchanges': [{'host': 'api.anthropic.com', 'swappedSecretIds': ['s']}, {'host': 'deb.debian.org', 'swappedSecretIds': []},
                                 {'host': 'api.anthropic.com'}]})
    mk(tmp_path, 'soak-1-2', '== run 01:00:00\n== done FAIL\n')
    mk(tmp_path, 'soak-1-3', '== agent 01:00:00\n')
    mk(tmp_path, 'other-1', '== done\n')
    s = B.status(str(tmp_path), 'soak-1-', {'sandboxes': []})
    assert sorted(s['jobs']) == ['soak-1-1', 'soak-1-2', 'soak-1-3']
    j1, j2, j3 = (s['jobs'][k] for k in ('soak-1-1', 'soak-1-2', 'soak-1-3'))
    assert (j1['verdict'], j1['done_at'], j1['stages']) == ('PASS', 1_800_000_500, {'box': '01:00:00', 'setup': '01:00:20'})
    assert j1['cred'] == {'readable': True, 'swapped': 1, 'hosts': ['api.anthropic.com']}
    assert (j2['verdict'], j2['cred']) == ('FAIL', None)
    assert (j3['verdict'], j3['done'], j3['done_at']) == (None, None, None)
    assert s['live'] == []


def test_failed_done_line_is_fail(tmp_path):
    mk(tmp_path, 'soak-1-1', '== setup exit 99 (worker gone: host restart or kill; closed by job_sweep)\n== done FAILED at setup (worker gone) 02:00:00\n')
    j = B.status(str(tmp_path), 'soak-1-', {'sandboxes': []})['jobs']['soak-1-1']
    assert (j['verdict'], j['worker_gone'], j['never_forgive']) == ('FAIL', True, False)


def test_never_forgive_markers(tmp_path):
    mk(tmp_path, 'soak-1-1', '-- box sbx_a NOT removed (state running after 6 tries): LEAK\n== done FAILED at agent 02:00:00\n')
    mk(tmp_path, 'soak-1-2', '== done 02:00:00\n', audit_txt='VIOLATION metadata server answered 200\n')
    mk(tmp_path, 'soak-1-3', '-- unrecorded-box sbx_q\n== done FAIL\n')
    mk(tmp_path, 'soak-1-4', '== done 02:00:00\n', audit_txt='blocked: {}\n')
    j = B.status(str(tmp_path), 'soak-1-', {'sandboxes': []})['jobs']
    assert [j[k]['never_forgive'] for k in sorted(j)] == [True, True, True, False]


def test_done_only_at_line_start_and_last_one_wins(tmp_path):
    mk(tmp_path, 'soak-1-1', '-- echo == done\n== agent 01:00:00\n')
    mk(tmp_path, 'soak-1-2', '== done FAILED at x 01:00:00\n== done 01:10:00\n')
    j = B.status(str(tmp_path), 'soak-1-', {'sandboxes': []})['jobs']
    assert j['soak-1-1']['done'] is None and j['soak-1-2']['verdict'] == 'PASS'


def test_unreadable_audit(tmp_path):
    for n, a in ((1, 'not json'), (2, {'exchanges': None}), (3, ['a', 'list'])):
        mk(tmp_path, f'soak-1-{n}', '== done 01:00:00\n', audit_json=a)
    j = B.status(str(tmp_path), 'soak-1-', {'sandboxes': []})['jobs']
    assert all(j[k]['cred'] == {'readable': False, 'swapped': 0, 'hosts': []} for k in j), j


def test_live_boxes_mapped_to_jobs(tmp_path):
    mk(tmp_path, 'soak-1-1', '== agent 01:00:00\n')
    mk(tmp_path, 'soak-1-10', '== run 01:00:00\n')
    lst = {'sandboxes': [
        {'id': 'sbx_a', 'runtime': {'state': 'running'}, 'config': {'source': {'localDirectory': str(tmp_path / 'soak-1-1' / 'src')}}},
        {'id': 'sbx_b', 'displayName': 'soak-1-10-010101-abcdef-0', 'runtime': {'state': 'running'}},
        {'id': 'sbx_c', 'displayName': 'soak-1-1-010101-abcdef-0', 'runtime': {'state': 'running'}},
        {'id': 'sbx_d', 'displayName': 'stranger', 'runtime': None, 'config': None},
        {'id': 'sbx_e', 'displayName': 'soak-1-1-010101-abcdef-9', 'runtime': {'state': 'archived'}},
    ]}
    live = B.status(str(tmp_path), 'soak-1-', lst)['live']
    assert live == [{'id': 'sbx_a', 'job': 'soak-1-1'}, {'id': 'sbx_b', 'job': 'soak-1-10'},
                    {'id': 'sbx_c', 'job': 'soak-1-1'}, {'id': 'sbx_d', 'job': None}]


def test_unreadable_listing_is_null(tmp_path):
    mk(tmp_path, 'soak-1-1', '== agent 01:00:00\n')
    assert B.status(str(tmp_path), 'soak-1-', None)['live'] is None
    assert B.status(str(tmp_path), 'soak-1-', {'sandboxes': None})['live'] == []


def test_listing_failure_modes(tmp_path):
    bad = tmp_path / 'bad.sh'
    bad.write_text('#!/bin/sh\necho nope; exit 1\n'); bad.chmod(0o755)
    junk = tmp_path / 'junk.sh'
    junk.write_text('#!/bin/sh\necho notjson\n'); junk.chmod(0o755)
    good = tmp_path / 'good.sh'
    good.write_text('#!/bin/sh\necho \'{"sandboxes": []}\'\n'); good.chmod(0o755)
    assert B.listing(str(bad)) is None and B.listing(str(junk)) is None and B.listing(str(tmp_path / 'missing')) is None
    assert B.listing(str(good)) == {'sandboxes': []}


def test_no_scratch_dir(tmp_path):
    assert B.status(str(tmp_path / 'none'), 'soak-', {'sandboxes': []}) == {'jobs': {}, 'live': []}


def test_host_metrics(tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()
    (proc / 'loadavg').write_text('2.50 1.00 0.50 1/100 1234\n')
    (proc / 'meminfo').write_text('MemTotal:       30000000 kB\nMemFree: 1000 kB\nMemAvailable:   20000000 kB\n')
    assert B.host(str(proc)) == {'load1': 2.5, 'mem_used_mb': 9765, 'mem_total_mb': 29296}
    assert B.host(str(tmp_path / 'none')) is None


def test_fan_audit_markers_never_forgive(tmp_path):
    mk(tmp_path, 'soak-1-1', '-- audit sbx_a: FAIL 2 provisioning exchange(s) NOT refused (deny-by-default broken): x\n== done FAIL\n')
    mk(tmp_path, 'soak-1-2', '-- audit sbx_a: INCOMPLETE (no exchanges list)\n== done FAIL\n')
    mk(tmp_path, 'soak-1-3', '-- sweep for unrecorded boxes FAILED (ls schema?)\n== done FAIL\n')
    mk(tmp_path, 'soak-1-4', '-- audit sbx_a: ok 12 exchanges\n== done PASS\n')
    j = B.status(str(tmp_path), 'soak-1-', {'sandboxes': []})['jobs']
    assert [j[k]['never_forgive'] for k in sorted(j)] == [True, True, True, False]


def test_a_listing_without_a_sandboxes_list_is_unreadable(tmp_path):
    mk(tmp_path, 'soak-1-1', '== agent 01:00:00\n')
    for bad in ({}, {'error': 'x'}, {'sandboxes': 'x'}, [], 'x'):
        assert B.status(str(tmp_path), 'soak-1-', bad)['live'] is None, bad


def test_cause_is_the_last_stage_exit_reason(tmp_path):
    mk(tmp_path, 'soak-1-1', '== preflight 01:00:00\n== preflight exit 5 (6 boxes already live)\n== done FAILED at preflight 01:00:01\n')
    mk(tmp_path, 'soak-1-2', '== done 01:00:01\n')
    j = B.status(str(tmp_path), 'soak-1-', {'sandboxes': []})['jobs']
    assert j['soak-1-1']['cause'] == '6 boxes already live' and j['soak-1-2']['cause'] is None


def test_hosts_strip_ports(tmp_path):
    mk(tmp_path, 'soak-1-1', '== done 01:00:00\n', audit_json={'exchanges': [{'host': 'api.anthropic.com:443', 'swappedSecretIds': ['s']}]})
    assert B.status(str(tmp_path), 'soak-1-', {'sandboxes': []})['jobs']['soak-1-1']['cred']['hosts'] == ['api.anthropic.com']



def test_listing_never_starts_the_server(tmp_path):
    exe = tmp_path / 'db.sh'
    exe.write_text('#!/bin/sh\ncase "$*" in *--auto-start-server=false*) echo \'{"sandboxes": []}\';; *) echo started; exit 1;; esac\n')
    exe.chmod(0o755)
    assert B.listing(str(exe)) == {'sandboxes': []}
