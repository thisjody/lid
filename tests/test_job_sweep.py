"""The orphaned-job sweep (scripts/lid/job_sweep.py): a job whose worker died without writing `== done` (a host reboot, an
OOM kill, a SIGKILL) gets its boxes audited and archived, and its log closed with a FAIL the soak can read. It must never
touch a live job. A package import, so mutation testing maps these tests to it."""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from scripts.lid import job_sweep as js  # noqa: E402

NOW = 1_800_000_000.0


def mkjob(root, name, log, conf='REPO=x\n', age=600, audit=False):
    d = root / name
    (d / 'job').mkdir(parents=True)
    (d / 'logs').mkdir()
    (d / 'out').mkdir()
    (d / 'job' / 'worker.sh').write_text('#!/bin/bash\n')
    (d / 'job' / 'job.conf').write_text(conf)
    if audit:
        (d / 'job' / 'audit.py').write_text('')
    p = d / 'logs' / 'run.log'
    if log is not None:
        p.write_text(log)
        os.utime(p, (NOW - age, NOW - age))
    return d


# -- which jobs are orphans ------------------------------------------------------------------------------------------

def test_orphans(tmp_path):
    mkjob(tmp_path, 'done-claude', '== preflight 01:00:00\n== preflight exit 0\n== done 01:02:00\n')
    mkjob(tmp_path, 'done-fan', '== run 01:00:00\n== done PASS\n', conf='SHARDS=2\n')
    mkjob(tmp_path, 'orphan-claude', '== preflight 01:00:00\n== preflight exit 0\n== agent 01:01:00\n')
    mkjob(tmp_path, 'orphan-fan', '== boxes 01:00:00\n', conf='SHARDS=2\n')
    mkjob(tmp_path, 'live', '== agent 01:01:00\n')
    mkjob(tmp_path, 'fresh', '== preflight 01:00:00\n', age=30)
    mkjob(tmp_path, 'no-log', None)
    (tmp_path / 'not-a-job').mkdir()
    procs = ['/bin/bash ' + str(tmp_path / 'live' / 'job' / 'worker.sh')]
    assert js.orphans(str(tmp_path), procs, NOW) == ['orphan-claude', 'orphan-fan']


def test_a_worker_of_a_similarly_named_job_does_not_protect_another(tmp_path):
    mkjob(tmp_path, 'a', '== agent 01:01:00\n')
    procs = ['/bin/bash ' + str(tmp_path / 'ab' / 'job' / 'worker.sh')]
    assert js.orphans(str(tmp_path), procs, NOW) == ['a']


def test_done_only_counts_at_line_start(tmp_path):
    mkjob(tmp_path, 'x', '== agent 01:01:00\n-- note: wrote == done to a file\n')
    assert js.orphans(str(tmp_path), [], NOW) == ['x']


def test_min_age_boundary(tmp_path):
    mkjob(tmp_path, 'edge', '== agent 01:01:00\n', age=js.MIN_AGE_S)
    mkjob(tmp_path, 'young', '== agent 01:01:00\n', age=js.MIN_AGE_S - 1)
    assert js.orphans(str(tmp_path), [], NOW) == ['edge']


# -- which boxes are a job's -----------------------------------------------------------------------------------------

LS = {'sandboxes': [
    {'id': 'sbx_a', 'displayName': 'x', 'runtime': {'state': 'running'}, 'config': {'source': {'localDirectory': '/h/scratch/j1/src'}}},
    {'id': 'sbx_b', 'displayName': 'j2-0101-ab-0', 'runtime': {'state': 'running'}, 'config': {}},
    {'id': 'sbx_c', 'displayName': 'j2-0101-ab-1', 'runtime': {'state': 'archived'}, 'config': {}},
    {'id': 'sbx_d', 'displayName': 'j22-0101-ab-0', 'runtime': {'state': 'running'}, 'config': {}},
    {'id': 'sbx_e', 'displayName': 'other', 'runtime': None, 'config': None},
]}


def test_boxes_of_a_claude_job():
    log = '-- base abc\n-- box sbx_z\n'
    assert js.boxes_of('j1', '/h/scratch/j1', log, LS) == ['sbx_a', 'sbx_z']


def test_boxes_of_a_fan_job():
    log = '-- shard 0 box sbx_b\n-- shard 1 box sbx_c\n'
    assert js.boxes_of('j2', '/h/scratch/j2', log, LS) == ['sbx_b']   # archived ones are done; j22-... is not j2's


def test_boxes_of_null_listing():
    assert js.boxes_of('j1', '/h/scratch/j1', '-- box sbx_z\n', {'sandboxes': None}) == ['sbx_z']


def test_a_box_named_in_the_log_but_archived_is_skipped():
    assert js.boxes_of('j9', '/h/scratch/j9', '-- shard 1 box sbx_c\n', LS) == []


# -- how the log is closed -------------------------------------------------------------------------------------------

def test_close_lines_claude():
    lines = js.close_lines('== preflight 01:00:00\n== preflight exit 0\n== agent 01:01:00\n', fan=False, leaked=[], t='02:00:00')
    assert lines == ['== agent exit 99 (worker gone: host restart or kill; closed by job_sweep)',
                     '== done FAILED at agent (worker gone) 02:00:00']


def test_close_lines_fan_with_leak():
    lines = js.close_lines('== boxes 01:00:00\n', fan=True, leaked=['sbx_b'], t='02:00:00')
    assert lines == ['-- box sbx_b NOT removed: LEAK',
                     '== boxes exit 99 (worker gone: host restart or kill; closed by job_sweep)',
                     '== done FAIL (worker gone)']


def test_close_lines_no_stage():
    assert js.close_lines('', fan=False, leaked=[], t='02:00:00')[-1] == '== done FAILED at unknown (worker gone) 02:00:00'


# -- the whole sweep, against a fake discobox ------------------------------------------------------------------------

class FakeDB:
    def __init__(self, listing, stuck=(), fail_rm=()):
        self.listing, self.stuck, self.removed, self.audited, self.stopped = listing, set(stuck), [], [], []
        self.fail_rm, self.calls = set(fail_rm), []

    def stop_agent(self, i):
        self.stopped.append(i)
        self.calls.append(('stop', i))

    def ls(self):
        d = json.loads(json.dumps(self.listing))
        for b in (d.get('sandboxes') if isinstance(d, dict) and isinstance(d.get('sandboxes'), list) else None) or []:
            if b['id'] in self.removed and b['id'] not in self.stuck:
                b['runtime'] = {'state': 'archived'}
        return d

    def rm(self, i):
        if i in self.fail_rm:
            raise RuntimeError('rm timed out')
        self.removed.append(i)

    def audit(self, jobdir, i):
        self.audited.append(i)
        self.calls.append(('audit', i))


def test_sweep_closes_an_orphan_and_archives_its_boxes(tmp_path):
    scratch = tmp_path / 'scratch'
    d = mkjob(scratch, 'j1', '== agent 01:01:00\n-- box sbx_a\n', audit=True)
    db = FakeDB({'sandboxes': [{'id': 'sbx_a', 'displayName': 'x', 'runtime': {'state': 'running'},
                                'config': {'source': {'localDirectory': str(d / 'src')}}}]})
    out = js.sweep(str(scratch), [], NOW, db, sleep=lambda s: None, t='02:00:00')
    assert out == {'j1': {'boxes': ['sbx_a'], 'leaked': []}}
    assert db.audited == ['sbx_a'] and db.removed == ['sbx_a']
    log = (d / 'logs' / 'run.log').read_text().splitlines()
    assert log[-1] == '== done FAILED at agent (worker gone) 02:00:00'


def test_sweep_records_a_leak(tmp_path):
    scratch = tmp_path / 'scratch'
    d = mkjob(scratch, 'j2', '== boxes 01:00:00\n-- shard 0 box sbx_b\n', conf='SHARDS=1\n')
    db = FakeDB({'sandboxes': [{'id': 'sbx_b', 'displayName': 'j2-0-0', 'runtime': {'state': 'running'}, 'config': {}}]}, stuck={'sbx_b'})
    out = js.sweep(str(scratch), [], NOW, db, sleep=lambda s: None, t='02:00:00')
    assert out['j2']['leaked'] == ['sbx_b'] and db.removed.count('sbx_b') == js.RM_TRIES
    log = (d / 'logs' / 'run.log').read_text()
    assert '-- box sbx_b NOT removed: LEAK' in log and log.endswith('== done FAIL (worker gone)\n')


def test_sweep_leaves_live_and_done_jobs_alone(tmp_path):
    scratch = tmp_path / 'scratch'
    live = mkjob(scratch, 'live', '== agent 01:01:00\n-- box sbx_a\n')
    done = mkjob(scratch, 'done', '== done 01:02:00\n')
    db = FakeDB({'sandboxes': [{'id': 'sbx_a', 'displayName': 'x', 'runtime': {'state': 'running'}, 'config': {}}]})
    before = (live / 'logs' / 'run.log').read_text(), (done / 'logs' / 'run.log').read_text()
    out = js.sweep(str(scratch), ['bash ' + str(live / 'job' / 'worker.sh')], NOW, db, sleep=lambda s: None, t='02:00:00')
    assert out == {} and db.removed == [] and db.audited == []
    assert ((live / 'logs' / 'run.log').read_text(), (done / 'logs' / 'run.log').read_text()) == before


def test_sweep_does_nothing_when_the_listing_fails(tmp_path):
    scratch = tmp_path / 'scratch'
    d = mkjob(scratch, 'j1', '== agent 01:01:00\n')

    class Broken(FakeDB):
        def ls(self):
            raise RuntimeError('server down')
    db = Broken({'sandboxes': []})
    with pytest.raises(RuntimeError):
        js.sweep(str(scratch), [], NOW, db, sleep=lambda s: None, t='02:00:00')
    assert (d / 'logs' / 'run.log').read_text() == '== agent 01:01:00\n'   # never closed on a guess


def test_audit_failure_does_not_stop_the_archive(tmp_path):
    scratch = tmp_path / 'scratch'
    d = mkjob(scratch, 'j1', '== agent 01:01:00\n-- box sbx_a\n', audit=True)

    class NoAudit(FakeDB):
        def audit(self, jobdir, i):
            raise RuntimeError('audit failed')
    db = NoAudit({'sandboxes': [{'id': 'sbx_a', 'displayName': 'x', 'runtime': {'state': 'running'}, 'config': {}}]})
    js.sweep(str(scratch), [], NOW, db, sleep=lambda s: None, t='02:00:00')
    log = (d / 'logs' / 'run.log').read_text()
    assert db.removed == ['sbx_a'] and 'VIOLATION audit of sbx_a could not be read (job_sweep)' in log


def test_process_list_reads_proc(tmp_path):
    p = tmp_path / 'proc'
    (p / '12').mkdir(parents=True)
    (p / '12' / 'cmdline').write_bytes(b'/bin/bash\x00/home/u/scratch/j/job/worker.sh\x00')
    (p / 'self').mkdir()
    (p / 'abc').mkdir()
    assert js.processes(str(p)) == ['/bin/bash /home/u/scratch/j/job/worker.sh']


def test_units_are_installed_by_stack_up():
    root = os.path.join(os.path.dirname(__file__), '..')
    up = open(os.path.join(root, 'scripts', 'lid', 'stack_up.sh')).read()
    assert 'job-sweep.service' in up and 'job-sweep.timer' in up
    t = open(os.path.join(root, 'scripts', 'lid', 'job-sweep.timer')).read()
    assert 'OnBootSec=' in t and 'OnUnitActiveSec=' in t
    s = open(os.path.join(root, 'scripts', 'lid', 'job-sweep.service')).read()
    assert 'python3 -I' in s and 'job_sweep.py' in s




def test_the_agent_is_stopped_before_the_audit(tmp_path):
    scratch = tmp_path / 'scratch'
    mkjob(scratch, 'j1', '== agent 01:01:00\n-- box sbx_a\n', audit=True)
    db = FakeDB({'sandboxes': [{'id': 'sbx_a', 'displayName': 'x', 'runtime': {'state': 'running'}, 'config': {}}]})
    js.sweep(str(scratch), [], NOW, db, sleep=lambda s: None, t='02:00:00')
    assert db.calls[:2] == [('stop', 'sbx_a'), ('audit', 'sbx_a')]


def test_one_jobs_failure_does_not_stop_the_others(tmp_path):
    scratch = tmp_path / 'scratch'
    a = mkjob(scratch, 'a', '== agent 01:01:00\n-- box sbx_a\n')
    b = mkjob(scratch, 'b', '== agent 01:01:00\n-- box sbx_b\n')
    db = FakeDB({'sandboxes': [{'id': i, 'displayName': i, 'runtime': {'state': 'running'}, 'config': {}} for i in ('sbx_a', 'sbx_b')]},
                fail_rm={'sbx_a'})
    out = js.sweep(str(scratch), [], NOW, db, sleep=lambda s: None, t='02:00:00')
    assert out['a']['error'] and out['b'] == {'boxes': ['sbx_b'], 'leaked': []}
    assert (a / 'logs' / 'run.log').read_text().endswith('== done FAILED at agent (worker gone) 02:00:00\n')
    assert '-- sweep: rm timed out' in (a / 'logs' / 'run.log').read_text() and 'LEAK' in (a / 'logs' / 'run.log').read_text()


def test_a_job_without_a_conf_is_closed_as_a_claude_job(tmp_path):
    scratch = tmp_path / 'scratch'
    d = mkjob(scratch, 'j1', '== agent 01:01:00\n')
    (d / 'job' / 'job.conf').unlink()
    js.sweep(str(scratch), [], NOW, FakeDB({'sandboxes': []}), sleep=lambda s: None, t='02:00:00')
    assert (d / 'logs' / 'run.log').read_text().endswith('== done FAILED at agent (worker gone) 02:00:00\n')


def test_a_listing_without_a_sandboxes_list_stops_the_sweep(tmp_path):
    scratch = tmp_path / 'scratch'
    d = mkjob(scratch, 'j1', '== agent 01:01:00\n')
    for bad in ({}, {'sandboxes': 'x'}, []):
        with pytest.raises(RuntimeError):
            js.sweep(str(scratch), [], NOW, FakeDB(bad), sleep=lambda s: None, t='02:00:00')
    assert (d / 'logs' / 'run.log').read_text() == '== agent 01:01:00\n'


def test_main_reports_what_it_did(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(js.os.path, 'expanduser', lambda p: str(tmp_path))
    (tmp_path / 'scratch').mkdir()
    class Boom:
        def __init__(self, exe):
            pass
        def ls(self):
            raise RuntimeError('server down')
    monkeypatch.setattr(js, 'Discobox', Boom)
    monkeypatch.setattr(js, 'processes', lambda proc='/proc': [])
    assert js.main() == 1 and 'stopped before closing anything: server down' in capsys.readouterr().out
