"""Load to saturation, 2026-11-18 on a test box: the pool (6 vCPU, 20 GiB) starts 8 boxes and the 9th dies before
its sandbox agent is healthy (container exit 255), twice in a row. The box ceiling is a measured manifest value
(pool.max_boxes); fan jobs are capped by it (boxes), separately from the CPU cap (shards x workers <= 2 x vCPU)."""
import os
import re
import subprocess

import pytest
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'scripts', 'lid'))
from stack_site import load  # noqa: E402
from tests.soak import soak  # noqa: E402


def _pool():
    m = load(os.path.join(ROOT, 'stack', 'stack.yaml'))
    return [c for c in m['components'] if c['name'] == 'discobox'][0]['pool']


def test_the_measured_box_ceiling_is_in_the_manifest():
    assert _pool()['max_boxes'] == 8


def test_the_load_test_fans_to_the_manifest_ceiling():
    assert soak.FAN_CAP == _pool()['max_boxes'] and soak.MAXBOX <= soak.FAN_CAP


def _caps(shards, workers):
    s = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_fan.sh')).read()
    a = s.index('VCPU=$(cd "$ROOT"')
    b = s.index('\n', s.index('> box cap'))
    script = f'ROOT={ROOT}; SHARDS={shards}; WORKERS={workers}\n{s[a:b]}\necho "OK CAP=$CAP"\n'
    return subprocess.run(['bash', '-c', script], capture_output=True, text=True)


def test_fan_shards_are_capped_by_the_box_ceiling_and_cpu_separately():
    r = _caps(8, 1)
    assert r.returncode == 0 and 'OK CAP=8' in r.stdout, r.stdout + r.stderr   # job.conf's CAP is the box ceiling
    r = _caps(9, 1)
    assert r.returncode == 2 and 'box cap 8' in r.stderr, r.stderr
    r = _caps(4, 3)
    assert r.returncode == 0, r.stderr                                            # 12 = 2 x 6 vCPU
    r = _caps(4, 4)
    assert r.returncode == 2 and 'vCPU' in r.stderr, r.stderr


def test_the_worker_counts_live_boxes_against_the_box_ceiling():
    w = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_fan_lib', 'worker.sh')).read()
    assert re.search(r'\[ \$\(\(live \+ SHARDS\)\) -le "\$CAP" \]', w) and 'pool.max_boxes' in w



def _lib_room(tmp_path, live):
    b = tmp_path / 'bin'
    b.mkdir(parents=True)
    boxes = ','.join('{"id": "sbx_%d", "runtime": {"state": "running", "runtimeState": "running"}}' % i for i in range(live))
    (b / 'discobox').write_text('#!/bin/sh\necho \'{"sandboxes": [%s]}\'\n' % boxes)
    (b / 'discobox').chmod(0o755)
    lib = os.path.join(ROOT, 'scripts', 'lid', 'stack_lib.sh')
    return subprocess.run(['bash', '-c', f'. {lib}; box_room'], capture_output=True, text=True,
                          env={'PATH': f'{b}:' + os.environ['PATH'], 'HOME': str(tmp_path)})


def test_box_room_refuses_at_the_ceiling(tmp_path):
    assert _lib_room(tmp_path / 'a', 7).returncode == 0
    r = _lib_room(tmp_path / 'b', 8)
    assert r.returncode != 0 and 'ceiling' in (r.stdout + r.stderr), r.stdout + r.stderr


@pytest.mark.parametrize('script', [('scripts', 'lid', 'stack_test.sh'), ('scripts', 'lid', 'decoy_proof.sh'),
                                    ('scripts', 'lid', 'claude_canary.sh')])
def test_lock_holding_creators_check_the_ceiling_before_creating(script):
    s = open(os.path.join(ROOT, *script)).read()
    create = re.search(r'timeout -k \d+ \d+ discobox (admin box create|new --from-json)', s).start()
    lock = s.rfind('flock -w 1800 9', 0, create)
    assert lock != -1 and 'box_room' in s[lock:create], script


def test_the_canary_refuses_at_the_ceiling(tmp_path):
    from tests import test_claude_canary as tc
    r, made, removed, cleared = tc.run(tmp_path, [tc.GOOD], others=[f'sbx_job{i}' for i in range(8)])
    assert r.returncode == 1 and made == [] and 'ceiling' in r.stdout, r.stdout + r.stderr
