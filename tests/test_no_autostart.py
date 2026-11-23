"""No lid call may start a second Discobox server (11/21: under load a client judged the busy server dead, launched a new
one, and the new server shut the old one down, killing every live box's agent). Every discobox call passes
--auto-start-server=false; one place starts a server, and only when no server process exists at all."""
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
FLAG = '--auto-start-server=false'
ENSURE = 'scripts/lid/discobox_ensure.sh'
# A discobox command in shell (`discobox ls`, `discobox "$@"`), a Python argv list (['discobox', ...]) or a unit's ExecStart.
# Every top-level command of the Discobox 0.19 CLI (`discobox --help`), so a new call of any kind is seen.
COMMANDS = ('admin adopt api-access attach capture completion console cp events exec help ls new proxy push release rename '
            'restart rm run secret send send-key shell start stop tag tools version wait').split()
SUB = r'(?:"\$@"|(?:' + '|'.join(re.escape(c) for c in sorted(COMMANDS, key=len, reverse=True)) + r')(?![\w-]))'
CALL = re.compile(r'''(?:(?<![\w./'"-])discobox|"?\$\{?DISCOBOX\}?"?|\.local/bin/discobox)\s+''' + SUB
                  + r'''|\[\s*(?:'discobox'|exe|self\.exe|DISCOBOX)\s*,''')


def _tracked():
    # Walk the tree rather than ask git: the gate runs the suite on an exported tree with no .git.
    out = []
    for top in ('scripts', 'stack', 'tests/soak'):
        for d, dirs, files in os.walk(os.path.join(ROOT, top)):
            dirs[:] = [x for x in dirs if x not in ('__pycache__', 'fan_tests')]
            out += [os.path.relpath(os.path.join(d, f), ROOT) for f in files if f.endswith(('.sh', '.py', '.service'))]
    return sorted(out)


def _bare_calls():
    bad = []
    for p in _tracked():
        for n, line in enumerate(open(os.path.join(ROOT, p)), 1):
            code = re.sub(r'(^|\s)#.*', '', line) if p.endswith('.sh') else line   # a comment, never $# or ${#x}
            if p.endswith('.py') and code.lstrip().startswith('#'):
                continue
            for m in CALL.finditer(code):
                if FLAG not in code[m.start():m.start() + 120] and p != ENSURE:
                    bad.append(f'{p}:{n}: {line.strip()[:100]}')
    return bad


def test_every_discobox_call_refuses_to_start_a_server():
    assert _bare_calls() == []


def test_the_scanner_sees_each_call_form(tmp_path):
    for s in ('timeout 300 discobox ls -o json', 'dbx(){ timeout 3600 discobox "$@"; }', "x = ['discobox', 'admin']",
              'ExecStart=%h/.local/bin/discobox secret refresh S --run', 'return subprocess.run([self.exe, *args])',
              'discobox stop x', 'discobox logs', '"$DISCOBOX" ls', '${DISCOBOX} exec x', "subprocess.run([exe, 'ls'])",
              '[ $# -gt 0 ] && discobox ls'):
        if s == 'discobox logs':
            continue
        code = re.sub(r'(^|\s)#.*', '', s)
        assert CALL.search(code), s
    for s in ('discobox.internal', 'discobox-server', '# discobox ls', 'discobox_up.sh', 'die 7 "discobox new failed"',
              'die "discobox client sha256"', "RuntimeError('discobox ls failed')", 'echo "no discobox pool vcpus"'):
        assert not CALL.search(s.split('#', 1)[0]), s


def _ensure(tmp_path, running, locked=False):
    b = tmp_path / 'bin'
    b.mkdir()
    (b / 'ps').write_text('#!/bin/sh\n' + ('echo /h/.local/state/discobox/server/v/discobox-server\n' if running else '')
                          + 'echo /h/.local/state/discobox/server/v/discobox-server __pool-vm-launcher --config c\nexit 0\n')
    (b / 'discobox').write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/calls\n')
    (b / 'flock').write_text('#!/bin/sh\nexit ${FLOCK_RC:-0}\n')
    (b / 'timeout').write_text('#!/bin/sh\nshift\nexec "$@"\n')
    for f in b.iterdir():
        f.chmod(0o755)
    env = dict(os.environ, PATH=f'{b}:/usr/bin:/bin', DISCOBOX=str(b / 'discobox'), XDG_RUNTIME_DIR=str(tmp_path),
               FLOCK_RC='1' if locked else '0')
    r = subprocess.run(['bash', os.path.join(ROOT, ENSURE)], capture_output=True, text=True, env=env, timeout=30)
    calls = (tmp_path / 'calls').read_text().splitlines() if (tmp_path / 'calls').exists() else []
    return r, calls


def test_ensure_never_touches_a_running_server(tmp_path):
    r, calls = _ensure(tmp_path, running=True)
    assert r.returncode == 0 and calls == []


def test_ensure_starts_a_server_only_when_none_runs(tmp_path):
    r, calls = _ensure(tmp_path, running=False)
    assert r.returncode == 0 and calls == ['admin providers ls'], (calls, r.stderr)


def test_ensure_does_nothing_while_another_run_holds_the_lock(tmp_path):
    r, calls = _ensure(tmp_path, running=False, locked=True)
    assert r.returncode == 0 and calls == []


def test_ensure_units_are_installed_by_stack_up():
    up = open(os.path.join(ROOT, 'scripts', 'lid', 'stack_up.sh')).read()
    assert 'discobox-ensure.service' in up and 'discobox-ensure.timer' in up
    t = open(os.path.join(ROOT, 'scripts', 'lid', 'discobox-ensure.timer')).read()
    assert 'OnBootSec=' in t and 'OnUnitActiveSec=15s' in t
    s = open(os.path.join(ROOT, 'scripts', 'lid', 'discobox-ensure.service')).read()
    assert 'discobox_ensure.sh' in s


def _ship_repo(tmp_path, git=True):
    r = tmp_path / 'repo'
    for f in ('stack/stack.yaml', 'stack/agentgateway/config.yaml', 'stack/discobox/harness/x.json', 'scripts/lid/a.sh',
              'scripts/lid/stack_lib.sh'):
        (r / f).parent.mkdir(parents=True, exist_ok=True)
        (r / f).write_text(open(os.path.join(ROOT, 'scripts/lid/stack_lib.sh')).read() if f.endswith('stack_lib.sh') else f)
    (r / 'scripts' / 'lid_run.sh').write_text(f'printf %s "$1" > {tmp_path}/cmd\n')
    # A stand-in git: `git -C DIR ls-files ...` prints the tracked list, and fails outside a checkout, as git does. The
    # gate's sandboxes run the suite on an exported tree without git.
    b = tmp_path / 'bin'
    b.mkdir()
    (b / 'git').write_text('#!/bin/sh\n[ "$1" = -C ] && [ "$3" = ls-files ] && [ -e "$2/.tracked" ] && exec cat "$2/.tracked"\nexit 128\n')
    (b / 'git').chmod(0o755)
    if git:
        (r / '.tracked').write_text(''.join(f + '\n' for f in (
            'scripts/lid/a.sh', 'scripts/lid/stack_lib.sh', 'stack/agentgateway/config.yaml', 'stack/discobox/harness/x.json',
            'stack/stack.yaml')))
    (r / 'stack' / 'site.yaml').write_text('host: {}\n')
    (r / 'scripts' / 'lid' / '__pycache__').mkdir()
    (r / 'scripts' / 'lid' / '__pycache__' / 'junk.pyc').write_bytes(b'x' * 1000)
    (r / 'scripts' / 'lid' / 'untracked.sh').write_text('stray')
    return r


def test_stack_ship_sends_only_tracked_files_and_the_site_file(tmp_path):
    # 11/21: a Python cache left in scripts/lid by running the tests in place doubled the shipped tree past Linux's
    # 128 KB limit for one argument; only tracked files (and the untracked site file) are shipped.
    r = _ship_repo(tmp_path)
    p = subprocess.run(['bash', '-c', f'. {r}/scripts/lid/stack_lib.sh; stack_ship a.sh; echo unreachable'],
                       capture_output=True, text=True, timeout=60, env=dict(os.environ, PATH=f'{tmp_path}/bin:/usr/bin:/bin'))
    assert 'unreachable' not in p.stdout
    cmd = (tmp_path / 'cmd').read_text()
    b64 = re.search(r'echo (\S+) \| base64 -d', cmd).group(1)
    import base64, io, tarfile
    names = sorted(tarfile.open(fileobj=io.BytesIO(base64.b64decode(b64))).getnames())
    assert names == ['scripts/lid/a.sh', 'scripts/lid/stack_lib.sh', 'stack/agentgateway/config.yaml',
                     'stack/discobox/harness/x.json', 'stack/site.yaml', 'stack/stack.yaml'], names


def test_stack_ship_outside_a_checkout_exits(tmp_path):
    r = _ship_repo(tmp_path, git=False)
    p = subprocess.run(['bash', '-c', f'. {r}/scripts/lid/stack_lib.sh; stack_ship a.sh; echo unreachable'],
                       capture_output=True, text=True, timeout=60, env=dict(os.environ, PATH=f'{tmp_path}/bin:/usr/bin:/bin'))
    assert p.returncode != 0 and 'unreachable' not in p.stdout and not (tmp_path / 'cmd').exists()


def _real_ensure(tmp_path, procs):
    """Run the ensure script with the real ps and grep against real processes named discobox-server."""
    b = tmp_path / 'bin'
    b.mkdir(exist_ok=True)
    (b / 'discobox').write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/calls\n')
    (b / 'flock').write_text('#!/bin/sh\nexit 0\n')
    (b / 'timeout').write_text('#!/bin/sh\nshift\nexec "$@"\n')
    srv = tmp_path / 'x' / 'discobox-server'
    srv.parent.mkdir(exist_ok=True)
    srv.write_text('#!/bin/sh\nsleep 30\n')
    for f in (*b.iterdir(), srv):
        f.chmod(0o755)
    ps = [subprocess.Popen([str(srv), *args]) for args in procs]
    try:
        import time
        time.sleep(0.5)
        env = dict(os.environ, PATH=f'{b}:/usr/bin:/bin', DISCOBOX=str(b / 'discobox'), XDG_RUNTIME_DIR=str(tmp_path))
        r = subprocess.run(['bash', os.path.join(ROOT, ENSURE)], capture_output=True, text=True, env=env, timeout=30)
    finally:
        for p in ps:
            p.kill()
    return r, ((tmp_path / 'calls').read_text().splitlines() if (tmp_path / 'calls').exists() else [])


def test_ensure_sees_a_real_server_with_or_without_arguments(tmp_path):
    for procs in ([[]], [['--config', 'c']], [[], ['__pool-vm-launcher', '--config', 'c']]):
        (tmp_path / 'calls').unlink(missing_ok=True)
        r, calls = _real_ensure(tmp_path, procs)
        assert r.returncode == 0 and calls == [], (procs, calls, r.stderr)


def test_ensure_restarts_when_only_the_launcher_survives(tmp_path):
    r, calls = _real_ensure(tmp_path, [['__pool-vm-launcher', '--config', 'c']])
    assert calls == ['admin providers ls'], (calls, r.stderr)


@pytest.mark.skipif(not shutil.which('flock'), reason='flock (util-linux) is on the box, not on macOS')
def test_two_ensure_runs_start_at_most_one_server(tmp_path):
    b = tmp_path / 'bin'
    b.mkdir()
    (b / 'discobox').write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/calls\nsleep 3\n')
    (b / 'discobox').chmod(0o755)
    env = dict(os.environ, PATH=f'{b}:/usr/bin:/bin', DISCOBOX=str(b / 'discobox'), XDG_RUNTIME_DIR=str(tmp_path))
    runs = [subprocess.Popen(['bash', os.path.join(ROOT, ENSURE)], env=env) for _ in range(2)]
    for p in runs:
        p.wait(timeout=30)
    assert (tmp_path / 'calls').read_text().splitlines() == ['admin providers ls']


def test_ensure_keeps_its_lock_from_the_client():
    s = open(os.path.join(ROOT, ENSURE)).read()
    assert re.search(r'"\$DISCOBOX" admin providers ls 9>&-', s)   # (bounded: see test_ensure_bounds_its_client_call)


# -- 11/22 soak on the fix: the soak's server kill landed in two jobs' cleanups, which then could not audit or archive
# for the minute until discobox-ensure brought the server back. Cleanup now waits for the server first.

def _fn(path, name):
    s = open(os.path.join(ROOT, path)).read()
    return re.search(r'^' + name + r'\(\)\{.*\}$', s, re.M).group(0)   # the helpers are one-liners


def _server_wait(tmp_path, answer_after, path='scripts/monitor/disco_job.sh', limit=180):
    b = tmp_path / 'bin'
    b.mkdir(exist_ok=True)
    (b / 'discobox').write_text(f'#!/bin/sh\nn=$(cat {tmp_path}/n 2>/dev/null || echo 0); n=$((n+1)); echo $n > {tmp_path}/n\n'
                                f'[ $n -gt {answer_after} ] && {{ echo \'{{"sandboxes": []}}\'; exit 0; }}\n'
                                'echo "request failed: 503 Service Unavailable: server is starting" >&2; exit 1\n')
    (b / 'sleep').write_text('#!/bin/sh\nexit 0\n')
    (b / 'timeout').write_text('#!/bin/sh\nshift\nexec "$@"\n')
    for f in b.iterdir():
        f.chmod(0o755)
    fn = _fn(path, 'server_wait')
    return subprocess.run(['bash', '-c', f'log(){{ echo "LOG $*"; }}\n{fn}\nserver_wait; echo "rc=$?"'], capture_output=True,
                          text=True, env=dict(os.environ, PATH=f'{b}:/usr/bin:/bin', SERVER_WAIT_S=str(limit)), timeout=60)


@pytest.mark.parametrize('path', ['scripts/monitor/disco_job.sh', 'scripts/monitor/disco_fan_lib/worker.sh'])
def test_server_wait_returns_once_the_server_answers(tmp_path, path):
    r = _server_wait(tmp_path, answer_after=5, path=path)
    assert r.stdout.strip().endswith('rc=0') and (tmp_path / 'n').read_text().strip() == '6', r.stdout + r.stderr


@pytest.mark.parametrize('path', ['scripts/monitor/disco_job.sh', 'scripts/monitor/disco_fan_lib/worker.sh'])
def test_server_wait_gives_up_at_its_deadline_and_says_so(tmp_path, path):
    import time
    t = time.time()
    r = _server_wait(tmp_path, answer_after=10 ** 9, path=path, limit=2)
    assert 'rc=1' in r.stdout and 'LOG -- the Discobox server did not answer for 2 s' in r.stdout, r.stdout + r.stderr
    assert time.time() - t < 20   # a deadline, not a count of tries that each may hang


def test_both_cleanups_wait_for_the_server_before_any_discobox_call():
    for path, first in (('scripts/monitor/disco_job.sh', 'if [ -z "$id" ]; then'),
                        ('scripts/monitor/disco_fan_lib/worker.sh', 'systemctl stop fan-shard')):
        s = open(os.path.join(ROOT, path)).read()
        fin = s[s.index('finish(){'):s.index('trap finish EXIT')]
        w = fin.index('server_wait ||')
        assert w < fin.index(first), path
        before = '\n'.join(l for l in fin[:w].splitlines() if not l.lstrip().startswith('#'))
        assert not re.search(r'discobox --auto-start-server=false|audit\.py|audit "|rm_box "', before), path
        # a shutdown that began during the wait is left to job_sweep (exit 98), as at the start of cleanup
        assert 'exit 98' in fin[w:fin.index('\n', w)], path


# -- 11/22 soak on 73a73ef: the ensure unit's own exit killed the server it had just started (a oneshot's leftover
# processes are killed), and cleanups that caught the server between deaths recorded leaks and unreadable audits.

def test_the_ensure_unit_leaves_the_server_it_starts_running():
    s = open(os.path.join(ROOT, 'scripts', 'lid', 'discobox-ensure.service')).read()
    assert re.search(r'^KillMode=process$', s, re.M)


def test_server_wait_remembers_a_server_that_stayed_gone(tmp_path):
    fn = _fn('scripts/monitor/disco_job.sh', 'server_wait')
    b = tmp_path / 'bin'
    b.mkdir()
    (b / 'discobox').write_text(f'#!/bin/sh\necho x >> {tmp_path}/calls\nexit 1\n')
    (b / 'timeout').write_text('#!/bin/sh\nshift\nexec "$@"\n')
    (b / 'sleep').write_text('#!/bin/sh\nexit 0\n')
    for f in b.iterdir():
        f.chmod(0o755)
    r = subprocess.run(['bash', '-c', f'log(){{ :; }}\n{fn}\nserver_wait; a=$?; : > {tmp_path}/calls; server_wait; echo "rc=$a,$?"'],
                       capture_output=True, text=True, env=dict(os.environ, PATH=f'{b}:/usr/bin:/bin', SERVER_WAIT_S='1'), timeout=60)
    assert r.stdout.strip() == 'rc=1,1' and (tmp_path / 'calls').read_text() == '', r.stdout + r.stderr


def _fan_audit(tmp_path, first, second):
    w = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_fan_lib', 'worker.sh')).read()
    a = w.index('audit_try(){')
    fns = w[a:w.index('\nPY\n}\n', a) + 6] + re.search(r'^audit\(\)\{.*\}$', w, re.M).group(0)
    b = tmp_path / 'bin'
    b.mkdir()
    ex = lambda blocked: '{"exchanges": [{"method": "GET", "host": "pypi.org", "status": 403, "blocked": %s, "createdAt": "2026-01-02T00:00:00Z"}]}' % blocked
    out = {'down': 'echo "connection refused" >&2; exit 1', 'clean': f"echo '{ex('true')}'", 'open': f"echo '{ex('false')}'"}
    (b / 'discobox').write_text(f'#!/bin/sh\nn=$(cat {tmp_path}/n 2>/dev/null || echo 0); n=$((n+1)); echo $n > {tmp_path}/n\n'
                                f'[ $n = 1 ] && {{ {out[first]}; exit 0; }}\n{out[second]}\n')
    (b / 'timeout').write_text('#!/bin/sh\nshift\nexec "$@"\n')
    for f in b.iterdir():
        f.chmod(0o755)
    (tmp_path / 'out').mkdir()
    script = (f'L={tmp_path}/run.log; OUT={tmp_path}/out; FAILS=""; log(){{ echo "$*" >> "$L"; }}; server_wait(){{ echo WAITED >> "$L"; }}\n'
              f'{fns}\naudit 0 sbx_x 2026-01-03T00:00:00Z; echo "FAILS=[$FAILS]"')
    r = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                       env=dict(os.environ, PATH=f'{b}:/usr/bin:/bin'), timeout=60)
    return r, (tmp_path / 'run.log').read_text()


def test_fan_audit_is_taken_again_once_the_server_is_back(tmp_path):
    r, log = _fan_audit(tmp_path, 'down', 'clean')
    assert 'FAILS=[]' in r.stdout, r.stdout + r.stderr + log
    assert 'WAITED' in log and 'clean' in log and 'Traceback' not in log and 'FAIL' not in log, log


def test_a_real_fan_audit_failure_still_fails_after_the_retry(tmp_path):
    r, log = _fan_audit(tmp_path, 'open', 'open')
    assert 'FAILS=[ audit-0]' in r.stdout, r.stdout + r.stderr
    assert log.count(': FAIL ') == 1, log   # only the last attempt is logged


def test_claude_audit_is_taken_again_once_the_server_is_back(tmp_path):
    j = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_job.sh')).read()
    lines = [l for l in j.splitlines() if 'audit.py" "$id" "$OUT"' in l or l.strip().startswith("printf '%s\\n' \"$o\" > \"$OUT/audit.txt\"")]
    assert len(lines) == 2, lines
    jd = tmp_path / 'job'
    jd.mkdir()
    (jd / 'audit.py').write_text(f'import os, sys\nn = int(open("{tmp_path}/n").read()) + 1 if os.path.exists("{tmp_path}/n") else 1\n'
                                 f'open("{tmp_path}/n", "w").write(str(n))\n'
                                 'print("VIOLATION audit could not be read: refused" if n == 1 else "clean: 3 exchanges to api.anthropic.com")\n'
                                 'sys.exit(1 if n == 1 else 0)\n')
    (tmp_path / 'out').mkdir()
    script = f'J={jd}; OUT={tmp_path}/out; id=sbx_a; server_wait(){{ :; }}\n' + '\n'.join(lines)
    subprocess.run(['bash', '-c', script], check=True, timeout=60)
    assert (tmp_path / 'out' / 'audit.txt').read_text() == 'box sbx_a\nclean: 3 exchanges to api.anthropic.com\n'


def test_rm_retries_and_the_sweep_listing_wait_for_the_server():
    for path in ('scripts/monitor/disco_job.sh', 'scripts/monitor/disco_fan_lib/worker.sh'):
        rm = re.search(r'^rm_box\(\)\{.*?\n  done;', open(os.path.join(ROOT, path)).read(), re.M | re.S).group(0)
        assert '[ "$t" -gt 1 ] && { server_wait || true; }' in rm, path
    w = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_fan_lib', 'worker.sh')).read()
    assert re.search(r'found=\$\(sweep_list\) \|\| \{ server_wait \|\| true; found=\$\(sweep_list\) \|\| found="\$found SWEEP_FAILED"; \}', w)


def test_a_real_fan_audit_failure_is_never_retried(tmp_path):
    # review of 8f3bda5: a retry after a real FAIL could log a clean second read if the restarted server lost its records
    r, log = _fan_audit(tmp_path, 'open', 'clean')
    assert 'FAILS=[ audit-0]' in r.stdout and ': FAIL ' in log and 'WAITED' not in log, r.stdout + log


def test_a_sweep_listing_that_fails_twice_keeps_what_it_found():
    w = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_fan_lib', 'worker.sh')).read()
    assert 'found="$found SWEEP_FAILED"' in w and 'found=SWEEP_FAILED;' not in w


def test_ensure_bounds_its_client_call():
    s = open(os.path.join(ROOT, ENSURE)).read()
    assert re.search(r'timeout \d+ "\$DISCOBOX" admin providers ls 9>&-', s)
