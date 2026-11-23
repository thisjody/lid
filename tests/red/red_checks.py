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
    ('untar-pax', SU, 'if self.type in PAX_TYPES:', 'if False:', [T_SU, T_IP]),
    ('untar-gnu-sparse', SU, 'if self.type == tarfile.GNUTYPE_SPARSE:', 'if False:', [T_SU, T_IP]),
    ('untar-xhdr-cap', SU, 'if not 0 <= self.size <= MAX_XHDR:', 'if False:', [T_SU, T_IP]),
    ('untar-xhdr-chain', SU, 'if depth >= 2:', 'if False:', [T_IP]),
    ('untar-members-clear', SU, 't.members.clear()', 'pass', [T_IP]),
    # whole-tree review 8: the orphaned-job sweep audits fan boxes too, and every exchange there must have been refused
    ('sweep-fan-audit', 'scripts/lid/job_sweep.py', 'if not (claude or _is_fan(jobdir)):', 'if not claude:', ['tests/test_job_sweep.py']),
    ('sweep-fan-open', 'scripts/lid/job_sweep.py', '    if open_:\n', '    if False:\n', ['tests/test_job_sweep.py']),
    ('sweep-unknown-type', 'scripts/lid/job_sweep.py', "        return [f'VIOLATION {i} not audited (job type unknown: no audit.py, no SHARDS=)']", '        return []', ['tests/test_job_sweep.py']),
    # 11/21: no lid call may start a second Discobox server; only discobox_ensure.sh starts one, and only when none runs
    ('no-autostart-poll', 'scripts/monitor/disco_job.sh', 'quick(){ timeout -k 10 60 discobox --auto-start-server=false shell', 'quick(){ timeout -k 10 60 discobox shell', ['tests/test_no_autostart.py']),
    ('ensure-skips-running', 'scripts/lid/discobox_ensure.sh', " | grep -qv -- ' __pool-vm-launcher' && exit 0\n", "\n", ['tests/test_no_autostart.py']),
    ('ship-tracked-only', 'scripts/lid/stack_lib.sh', 'git -C "$STACK_REPO" ls-files', 'printf "%s\\n"', ['tests/test_no_autostart.py']),
    ('cleanup-waits-job', 'scripts/monitor/disco_job.sh', '  server_wait || {', '  true || {', ['tests/test_no_autostart.py']),
    ('cleanup-waits-fan', WK, '  server_wait || {', '  true || {', ['tests/test_no_autostart.py']),
    ('ensure-killmode', 'scripts/lid/discobox-ensure.service', 'KillMode=process\n', '', ['tests/test_no_autostart.py']),
    ('fan-audit-retry', WK, 'audit(){ local o; o=$(audit_try "$@") && { echo "$o" >> "$L"; return 0; }; case "$o" in *": FAIL "*) echo "$o" >> "$L"; FAILS="$FAILS audit-$1"; return 1;; esac; server_wait || true; ', 'audit(){ local o; ', ['tests/test_no_autostart.py']),
    ('fan-audit-fail-not-retried', WK, 'case "$o" in *": FAIL "*) echo "$o" >> "$L"; FAILS="$FAILS audit-$1"; return 1;; esac; ', '', ['tests/test_no_autostart.py']),
    ('job-err-capped', 'scripts/monitor/disco_job.sh', 'capped(){ head -c "$ERR_CAP" > "$1"; cat > /dev/null; }', 'capped(){ cat > "$1"; }', ['tests/test_caps_and_audits.py']),
    ('untar-bad-header', SU, 'except (tarfile.InvalidHeaderError, tarfile.TruncatedHeaderError) as e:', 'except ZeroDivisionError as e:', [T_IP]),
    ('untar-oserror-clean', SU, 'RecursionError, OSError)', 'RecursionError)', [T_SU]),
    # Only tarfile builds with the 2025 security patches call _frombuf (the gate's Debian 3.13 does; upstream 3.11.0 and
    # 3.13.5 do not), so this row goes red on the gate and would pass on an unpatched Python.
    ('untar-bad-header-patched', SU, '    @classmethod\n    def _frombuf(cls, *args, **kwargs):\n        return cls._refuse_bad_header(super()._frombuf, *args, **kwargs)\n', '', [T_IP]),
    # untar-negative-size is not here: the gate's sandboxes run Debian's 3.13, whose tarfile refuses a base-256 negative
    # size itself ("invalid offset", seen in gate run u2 on c1f96c1), so the guard's removal is invisible there. The guard
    # matters on the lid host's 3.11.2; tests/test_inprocess.py goes red without it on 3.11.0 and 3.13.5.
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
