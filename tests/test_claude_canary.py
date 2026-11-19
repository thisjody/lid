"""The Claude canary (scripts/lid/claude_canary.sh) and the Claude job's fail-fast on a broken agent.

Found 2026-11-18 on a fresh box: the pool's provisioned image held a Claude Code install whose
platform-native optional dependency npm had silently skipped, so `claude` in every new box was the package's 500-byte
placeholder ("claude native binary not installed") and every Claude job failed. `discobox admin pool clear-cache` made the
pool provision again, and the next box ran. The canary makes a fresh Claude box, checks that `claude --version` prints a
version, always removes the box, and with --repair clears the pool's cache once (only when no other box is live, since the
clear stops every box on the pool) and checks again. A Claude job fails at preflight, not after its agent timeout.
"""
import json
import os
import re
import subprocess

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
CANARY = os.path.join(ROOT, 'scripts', 'lid', 'claude_canary.sh')
GOOD = '2.1.327 (Claude Code)'
BROKEN = 'BROKEN'

FAKE = r'''#!/bin/bash
F=$FAKE_DIR
case "$1" in
  new) cat > /dev/null; n=$(( $(cat $F/n 2>/dev/null || echo 0) + 1 )); echo $n > $F/n; echo "sbx_c$n" >> $F/made
       if flock -n "$HOME/scratch/.box-cap.lock" true; then echo unlocked >> $F/newlock; else echo locked >> $F/newlock; fi
       [ -n "${KILL_ON_NEW:-}" ] && kill -TERM "$(cat $F/pid)"
       [ -n "${NEW_FAIL:-}" ] && exit 1
       echo "{\"id\": \"sbx_c$n\"}";;
  shell) id=$2; shift 3
       case "$*" in *claude*) k=${id#sbx_c}; v=$(sed -n "${k}p" $F/versions)
         [ -n "${WARN:-}" ] && { echo "warning: the box agent is slow" >&2; echo "note: stdout chatter"; }
         # As the real shell does: the command runs in the box, so its own 2>&1 merges claude's stderr into stdout.
         if [ "$v" = BROKEN ]; then case "$*" in *'2>&1'*) echo "Error: claude native binary not installed.";; *) echo "Error: claude native binary not installed." >&2;; esac; exit 1; fi; echo "$v";;
       *) [ -n "${KILL_ON_READY:-}" ] && { kill -TERM "$(cat $F/pid)"; exit 1; }; exit 0;; esac;;
  rm) echo "$2" >> $F/removed; echo "$2" >> $F/rmcalls;;
  ls) [ -n "${LS_FAIL_AFTER_NEW:-}" ] && [ -f $F/n ] && exit 1
      python3 - "$F" "${RM_STUCK:-}" <<'PY'
import json, os, sys
f = sys.argv[1]
r = lambda n: open(os.path.join(f, n)).read().split() if os.path.exists(os.path.join(f, n)) else []
gone = set() if sys.argv[2] else set(r('removed'))
boxes = [{'id': i, 'runtime': {'state': 'archived' if i in gone else 'running', 'runtimeState': 'running'}} for i in r('made')]
boxes += [{'id': i, 'runtime': {'state': 'running', 'runtimeState': 'running'}} for i in r('others')]
print(json.dumps({'sandboxes': boxes}))
PY
  ;;
  admin) [ "$2 $3" = "pool clear-cache" ] && { echo "$4" >> $F/cleared
       if flock -n "$HOME/scratch/.box-cap.lock" true; then echo unlocked >> $F/clearlock; else echo locked >> $F/clearlock; fi; };;
esac
'''


def run(tmp_path, versions, repair=False, others=(), env=None, wrap=False):
    b = tmp_path / 'bin'
    b.mkdir()
    (b / 'discobox').write_text(FAKE)
    (b / 'sleep').write_text('#!/bin/sh\nexit 0\n')
    for f in ('discobox', 'sleep'):
        (b / f).chmod(0o755)
    st = tmp_path / 'state'
    st.mkdir()
    (st / 'versions').write_text('\n'.join(versions) + '\n')
    if others:
        (st / 'others').write_text('\n'.join(others) + '\n')
    (tmp_path / 'scratch').mkdir()
    args = ['bash', CANARY, 'krun'] + (['--repair'] if repair else [])
    if wrap:   # the canary's own pid is recorded so the fake can signal it
        args = ['bash', '-c', f'echo $$ > {st}/pid; exec ' + ' '.join(args)]
    r = subprocess.run(args, capture_output=True, text=True, timeout=120,
                       env={'PATH': f'{b}:' + os.environ['PATH'], 'FAKE_DIR': str(st), 'HOME': str(tmp_path), **(env or {})})
    read = lambda n: (st / n).read_text().split() if (st / n).exists() else []
    return r, read('made'), read('removed'), read('cleared')


def test_a_working_agent_passes_and_the_box_is_removed(tmp_path):
    r, made, removed, cleared = run(tmp_path, [GOOD])
    assert r.returncode == 0, r.stdout + r.stderr
    assert GOOD.split()[0] in r.stdout
    assert made == removed == ['sbx_c1'] and cleared == []


def test_a_broken_agent_fails_without_repair(tmp_path):
    r, made, removed, cleared = run(tmp_path, [BROKEN])
    assert r.returncode == 1
    assert 'does not run' in r.stdout and 'native binary not installed' in r.stdout
    assert made == removed == ['sbx_c1'] and cleared == []


def test_repair_clears_the_pool_cache_once_and_rechecks(tmp_path):
    r, made, removed, cleared = run(tmp_path, [BROKEN, GOOD], repair=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert cleared == ['krun'] and 'repaired' in r.stdout
    assert made == removed == ['sbx_c1', 'sbx_c2']


def test_repair_refused_while_another_box_is_live(tmp_path):
    r, made, removed, cleared = run(tmp_path, [BROKEN, GOOD], repair=True, others=['sbx_job1'])
    assert r.returncode == 1
    assert cleared == [] and made == removed == ['sbx_c1']
    assert '1 other box' in r.stdout


def test_repair_that_does_not_help_fails(tmp_path):
    r, made, removed, cleared = run(tmp_path, [BROKEN, BROKEN], repair=True)
    assert r.returncode == 1
    assert cleared == ['krun'] and made == removed == ['sbx_c1', 'sbx_c2']


def test_an_unreadable_version_is_not_a_pass(tmp_path):
    r, made, removed, cleared = run(tmp_path, ['something else'])
    assert r.returncode == 1 and 'does not run' in r.stdout
    assert made == removed == ['sbx_c1']



def test_a_create_that_fails_after_making_the_box_still_removes_it(tmp_path):
    r, made, removed, _ = run(tmp_path, [GOOD], env={'NEW_FAIL': '1'})
    assert r.returncode == 1
    assert made == ['sbx_c1'] and removed == ['sbx_c1'], (made, removed, r.stdout)


def test_an_interrupted_canary_removes_its_box(tmp_path):
    r, made, removed, _ = run(tmp_path, [GOOD], env={'KILL_ON_READY': '1'}, wrap=True)
    assert r.returncode != 0
    assert made == ['sbx_c1'] and removed == ['sbx_c1'], (made, removed, r.stdout + r.stderr)


def test_the_repair_and_the_creates_hold_the_box_cap_lock(tmp_path):
    r, *_ = run(tmp_path, [BROKEN, GOOD], repair=True)
    st = tmp_path / 'state'
    assert r.returncode == 0, r.stdout + r.stderr
    assert (st / 'clearlock').read_text().split() == ['locked']
    assert (st / 'newlock').read_text().split() == ['locked', 'locked']


def test_warnings_around_the_version_are_not_a_failure(tmp_path):
    r, *_ = run(tmp_path, [GOOD], env={'WARN': '1'})
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_canary_runs_claude_as_jobs_do():
    s = open(CANARY).read()
    assert 'bash -lc' not in s and "bash -c 'claude --version" in s


def _src(*p):
    return open(os.path.join(ROOT, *p)).read()


def test_stack_up_repairs_and_stack_test_checks():
    up = _src('scripts', 'lid', 'stack_up.sh')
    assert re.search(r'claude_canary\.sh"? "\$\(get pool\.name\)" --repair|claude_canary\.sh"? "\$PN" --repair', up) or \
        re.search(r'claude_canary\.sh"? "\$\(get components\.discobox\.pool\.name\)" --repair', up), 'stack_up runs the canary with --repair'
    t = _src('scripts', 'lid', 'stack_test.sh')
    line = next(l for l in t.splitlines() if 'claude_canary.sh' in l and not l.lstrip().startswith('#'))
    assert '--repair' not in line and '(k)' in t


def _claude_gate(tmp_path, versions, recheck):
    s = _src('scripts', 'monitor', 'disco_job.sh')
    a = s.index('claude_ok(){')
    block = s[a:s.index('\n', s.index('does not run in the box', a)) + 1]
    assert s.index('> "$OUT/versions.txt"') < a < s.index('stage box setup')
    (tmp_path / 'versions.txt').write_text(versions)
    rq = 'echo "%s"' % recheck if recheck is not None else 'return 124'
    script = f'OUT={tmp_path}; die(){{ echo "DIE $1 $2"; exit "$1"; }}\nsleep(){{ :; }}\nquick(){{ {rq}; }}\n{block}\nexit 0\n'
    return subprocess.run(['bash', '-c', script], capture_output=True, text=True)


@pytest.mark.parametrize('versions,recheck,expect', [
    ('claude 2.1.327 (Claude Code)\nnode v25.9.0\n', None, 0),
    ('claude Error: claude native binary not installed.\nnode v25.9.0\n', 'claude Error: claude native binary not installed.', 7),
    ('node v25.9.0\n', None, 7),                                   # the probe failed and the re-probe too
    ('node v25.9.0\n', 'claude 2.1.327 (Claude Code)', 0),         # one failed probe is not a broken agent
    ('claude 2.1.327 foo\n', None, 7),                               # a version that is not Claude Code's
])
def test_disco_job_fails_fast_when_claude_does_not_run(tmp_path, versions, recheck, expect):
    r = _claude_gate(tmp_path, versions, recheck)
    assert r.returncode == expect, (versions, recheck, r.stdout, r.stderr)
    if expect:
        assert 'claude_canary.sh' in r.stdout



def test_an_interrupt_during_the_create_still_removes_the_box(tmp_path):
    r, made, removed, _ = run(tmp_path, [GOOD], env={'KILL_ON_NEW': '1'}, wrap=True)
    assert r.returncode != 0
    assert made == ['sbx_c1'] and removed and set(removed) == {'sbx_c1'}, (made, removed, r.stdout + r.stderr)


def test_a_box_that_will_not_archive_is_tried_once_not_twice(tmp_path):
    r, *_ = run(tmp_path, [GOOD], env={'RM_STUCK': '1'})
    calls = (tmp_path / 'state' / 'rmcalls').read_text().split()
    assert r.returncode == 1 and 'LEAK' in r.stdout
    assert calls.count('sbx_c1') == 6, calls


def test_a_failed_create_with_an_unreadable_listing_says_so(tmp_path):
    r, *_ = run(tmp_path, [GOOD], env={'NEW_FAIL': '1', 'LS_FAIL_AFTER_NEW': '1'})
    assert r.returncode == 1 and 'possible leak' in r.stdout


def test_job_probe_and_canary_agree_on_a_warning_before_the_version(tmp_path):
    fb = tmp_path / 'fb'
    fb.mkdir()
    (fb / 'claude').write_text('#!/bin/sh\necho "warning: something chatty" >&2\necho "note: on stdout"\necho "2.1.327 (Claude Code)"\n')
    (fb / 'claude').chmod(0o755)
    s = _src('scripts', 'monitor', 'disco_job.sh')
    rec = next(l for l in s.splitlines() if '> "$OUT/versions.txt"' in l and 'inbox bash -c' in l)
    probe = re.search(r"inbox (bash -c '[^']*')", rec).group(1)
    out = subprocess.run(['bash', '-c', probe.replace('bash -c ', '', 1).strip("'")], capture_output=True, text=True,
                         env={'PATH': f'{fb}:/usr/bin:/bin'}).stdout
    assert re.search(r'^claude 2\.1\.327 ', out, re.M), out
    a = s.index('claude_ok(){')
    block = s[a:s.index('\n', s.index('does not run in the box', a)) + 1]
    (tmp_path / 'versions.txt').write_text('claude warning: something chatty\n')
    script = f'OUT={tmp_path}; die(){{ echo "DIE $1 $2"; exit "$1"; }}\nsleep(){{ :; }}\nquick(){{ "$@"; }}\n{block}\nexit 0\n'
    r = subprocess.run(['bash', '-c', script], capture_output=True, text=True, env={'PATH': f'{fb}:/usr/bin:/bin'})
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize('script', [('scripts', 'lid', 'stack_test.sh'), ('scripts', 'lid', 'decoy_proof.sh')])
def test_other_box_creators_take_the_box_cap_lock(script):
    s = _src(*script)
    m = re.search(r'^(?!\s*#).*\btimeout -k \d+ \d+ discobox (admin box create|new --from-json)', s, re.M)
    assert m, 'the create is bounded by a timeout'
    create = m.start()
    lock = s.rfind('exec 9>>"$HOME/scratch/.box-cap.lock"; flock -w 1800 9', 0, create)
    assert lock != -1 and s.count('flock -w 1800 9') == 1, script
    idline = s.index("['id']", create)
    unlock = s.index('flock -u 9', create)
    assert unlock > idline and unlock - idline < 200, 'released after the id is parsed'



def test_job_and_canary_use_one_version_pattern():
    pat = r'[0-9]+\.[0-9]+\.[0-9]+ \(Claude Code\)'
    assert pat in open(CANARY).read()
    job = _src('scripts', 'monitor', 'disco_job.sh')
    assert job.count(pat) == 3, 'the record, the re-probe and claude_ok'




def test_new_boxes_lists_what_appeared_and_fails_on_an_unreadable_listing(tmp_path):
    b = tmp_path / 'bin'
    b.mkdir()
    (b / 'discobox').write_text('#!/bin/sh\n[ -n "$LS_FAIL" ] && exit 1\necho \'{"sandboxes": [{"id": "sbx_a"}, {"id": "sbx_new"}]}\'\n')
    (b / 'discobox').chmod(0o755)
    lib = os.path.join(ROOT, 'scripts', 'lid', 'stack_lib.sh')
    env = {'PATH': f'{b}:/usr/bin:/bin', 'HOME': str(tmp_path)}
    r = subprocess.run(['bash', '-c', f'. {lib}; new_boxes "sbx_a"'], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.split() == ['sbx_new'], r.stdout + r.stderr
    r = subprocess.run(['bash', '-c', f'. {lib}; new_boxes "sbx_a"'], capture_output=True, text=True, env={**env, 'LS_FAIL': '1'})
    assert r.returncode != 0


@pytest.mark.parametrize('script', [('scripts', 'lid', 'stack_test.sh'), ('scripts', 'lid', 'decoy_proof.sh')])
def test_a_create_cut_short_is_swept_in_cleanup(script):
    s = _src(*script)
    create = re.search(r'timeout -k \d+ \d+ discobox (admin box create|new --from-json)', s).start()
    pre = s[s.rfind('flock -w 1800 9', 0, create):create]
    assert 'before=$(box_ids)' in pre and 'creating=1' in pre, script
    assert 'new_boxes "$before"' in s[s.index('cleanup(){'):s.index('cleanup(){') + 3000] and 'possible leak' in s, script




def test_stack_test_defers_signals_until_the_create_returns(tmp_path):
    """bash runs an EXIT-only trap at once on TERM/HUP, before the create's box exists; a TERM/HUP trap is deferred until
    the foreground create returns, so the sweep in cleanup sees the box."""
    s = _src('scripts', 'lid', 'stack_test.sh')
    line = next(l for l in s.splitlines() if l.startswith('trap cleanup EXIT'))
    rest = s[s.index(line) + len(line):]
    assert re.search(r"^trap '[^']*exit 143' TERM", rest, re.M) and re.search(r"^trap '[^']*exit 129' HUP", rest, re.M) \
        and re.search(r"^trap '[^']*exit 130' INT", rest, re.M), 'each signal trapped to its own exit status'
    probe = tmp_path / 'probe.sh'
    trap = '\n'.join(l for l in rest.splitlines()[:8] if l.startswith('trap '))
    probe.write_text(f"set -euo pipefail\ncleanup(){{ echo \"cleanup sees done=$(cat {tmp_path}/done 2>/dev/null)\"; }}\ntrap cleanup EXIT\n{trap}\n"
                     f"(sleep 2; echo yes > {tmp_path}/done)\necho after\n")
    p = subprocess.Popen(['bash', str(probe)], stdout=subprocess.PIPE, text=True)
    import time; time.sleep(0.7); p.terminate()
    out = p.communicate(timeout=20)[0]
    assert 'cleanup sees done=yes' in out and 'after' not in out, out
    assert p.returncode == 143, p.returncode


def test_stack_test_cleans_up_when_its_output_pipe_closes(tmp_path):
    """An ssh drop over lid_run (no pty) closes stdout: bash gets SIGPIPE on its next write. Ignored, the write fails and
    set -e runs the EXIT trap; untrapped, bash dies and cleanup never runs."""
    s = _src('scripts', 'lid', 'stack_test.sh')
    line = next(l for l in s.splitlines() if l.startswith('trap cleanup EXIT'))
    rest = s[s.index(line) + len(line):]
    traps = '\n'.join(l for l in rest.splitlines()[:8] if l.startswith('trap '))
    probe = tmp_path / 'probe.sh'
    probe.write_text(f"set -euo pipefail\ncleanup(){{ echo ran > {tmp_path}/cleaned; }}\ntrap cleanup EXIT\n{traps}\n"
                     "sleep 0.5\nfor i in $(seq 1 200000); do echo line $i; done\n")
    r = subprocess.run(['bash', '-c', f'bash {probe} | head -1 > /dev/null'], timeout=60)
    assert (tmp_path / 'cleaned').exists(), 'cleanup ran after the pipe closed'
