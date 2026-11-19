"""Row a of the gate: each critical guard, removed, must turn its test RED.

For every mutation: copy the tree to a temp dir, apply the one change (the pattern must be found exactly once), run the
named tests, and expect FAILURE. The unmutated tree must PASS the same tests first. Prints one line per check and exits
non-zero if any guard's removal goes unnoticed. Usage: python3 tests/red/red_checks.py (from the repo root).
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
RL = 'scripts/lid/route_lock.py'
SU = 'scripts/monitor/disco_fan_lib/safe_untar.py'
LC = 'scripts/monitor/disco_fan_lib/lockcheck.py'
WK = 'scripts/monitor/disco_fan_lib/worker.sh'
T_RL, T_SU, T_FAN, T_RM, T_IP = 'tests/test_route_lock.py', 'tests/test_safe_untar.py', 'scripts/lid/fan_tests/test_fan_lib.py', 'tests/test_rm_box.py', 'tests/test_inprocess.py'

MUTATIONS = [  # (id, file, old, new, tests that must go red)
    ('route-host-exact', RL, "if sorted(r.get('hosts') or []) != EXACT:", "if False:", [T_RL]),
    ('route-needs-binding', RL, "    if not mine:\n        return 'the secret has no binding'\n", "", [T_RL]),
    ('route-blocked-is-not-open', RL, "opened = [r for r in routes if not r.get('blocked')]", "opened = list(routes)", [T_RL]),
    ('route-binding-checked', RL, "        p = route_problem(b.get('routes'), f'binding {b.get(\"id\")}')\n", "        p = None\n", [T_RL]),
    ('untar-type', SU, ' or not (m.isreg() or m.isdir())', '', [T_SU, T_FAN, T_IP]),
    ('untar-dotdot', SU, '".." in parts or ', '', [T_SU, T_FAN, T_IP]),
    ('untar-absolute', SU, 'm.name.startswith("/") or ', '', [T_SU, T_IP]),
    ('untar-excl', SU, 'os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW', 'os.O_WRONLY | os.O_CREAT | os.O_TRUNC', [T_FAN, T_IP]),
    ('lock-hash-subset', LC, 'bad = sorted(k for k, h in tgt.items() if not h <= lock[k])', 'bad = []', [T_FAN]),
    ('lock-extra-pins', LC, 'extra = sorted(set(tgt) - set(lock))', 'extra = []', [T_FAN]),
    ('fan-leak-fails-job', WK, 'FAILS="$FAILS leak-$1"; return 1; }', 'return 0; }', [T_RM]),
    ('fan-leak-confirms', WK, 'case "$st" in archived|deleted|gone)', 'case "$st" in *)', [T_RM]),
    ('route-other-secrets', RL, "    if others:\n        return 'another secret is bound to the claude-code harness: ' + ', '.join(map(str, others))\n", '', ['tests/test_regressions.py::test_lock_refuses_other_secrets_on_the_harness']),
]


def pytest(tree, tests):
    env = dict(os.environ, HYPOTHESIS_PROFILE='red', PYTHONDONTWRITEBYTECODE='1')
    r = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-x', '-p', 'no:cacheprovider', *tests], cwd=tree,
                       capture_output=True, text=True, env=env)
    return r.returncode, (r.stdout + r.stderr).strip().splitlines()[-1:] or ['']


def main():
    rc, tail = pytest(ROOT, sorted({t for m in MUTATIONS for t in m[4]}))
    print(f'baseline (unmutated) {"PASS" if rc == 0 else "FAIL"}: {tail[0]}')
    if rc != 0:
        return 2
    missed = []
    for mid, path, old, new, tests in MUTATIONS:
        with tempfile.TemporaryDirectory() as d:
            tree = os.path.join(d, 't')
            shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns('.git', '__pycache__', '.hypothesis'))
            p = os.path.join(tree, path)
            s = open(p).read()
            if s.count(old) != 1:
                print(f'{mid}: PATTERN NOT FOUND ONCE ({s.count(old)}) in {path}')
                missed.append(mid)
                continue
            open(p, 'w').write(s.replace(old, new))
            rc, tail = pytest(tree, tests)
            red = rc != 0
            print(f'{mid}: {"RED (guard is tested)" if red else "GREEN - GUARD NOT TESTED"}: {tail[0][:100]}')
            if not red:
                missed.append(mid)
    print('RESULT', 'PASS' if not missed else f'FAIL {missed}')
    return 1 if missed else 0


if __name__ == '__main__':
    sys.exit(main())
