"""One test per review finding, written to fail before its fix and pass after. Each test runs the real script or code path with fakes in place of the box, gcloud and Discobox."""
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import io

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
LIB = os.path.join(ROOT, 'scripts', 'lid')
sys.path.insert(0, LIB)


def sh(script, env=None, cwd=None):
    return subprocess.run(['bash', '-c', script], capture_output=True, text=True, env={**os.environ, **(env or {})}, cwd=cwd)


# idle_shutdown.sh must assign DISCOBOX (it was swallowed by a comment), and with no user it reads busy.
def test_idle_shutdown_assigns_discobox(tmp_path):
    src = open(os.path.join(LIB, 'idle_shutdown.sh')).read()
    head = src[:src.index('busy=0')]
    out = sh(head + '\necho "D=[$DISCOBOX]"', env={'IDLE_BOX_USER': 'builder'}).stdout
    assert 'D=[/home/builder/.local/bin/discobox]' in out


# every check error reads busy, and a disco_job worker in its clone stage (no box yet) reads busy.
def test_idle_check_busy_while_a_job_worker_runs():
    src = open(os.path.join(LIB, 'idle_shutdown.sh')).read()
    assert 'job/worker' in src and 'disco-job-worker' in src, 'the idle check does not look for a running disco_job worker'


# stack_secret.sh must store the secret under the secret's name, not the box's.
def test_stack_secret_keeps_the_secret_name():
    src = open(os.path.join(LIB, 'stack_secret.sh')).read()
    names = re.findall(r'^\s*NAME=', src, re.M) + re.findall(r';\s*NAME=', src)
    assert len(names) == 1, f'NAME is assigned {len(names)} times'


# stack_check.sh's Python block must run (it imported nothing for sys).
def test_stack_check_python_block_compiles_and_names_sys():
    src = open(os.path.join(LIB, 'stack_check.sh')).read()
    m = re.search(r"<<'PY'\n(.*?)\nPY\n", src, re.S)
    assert m
    code = m.group(1)
    assert re.search(r'\bimport sys\b', code), 'sys is used but never imported'
    compile(code, 'stack_check_py', 'exec')


# see test_wif_token_jq_ran_and_token_not_in_argv below (it also proves jq ran, so it cannot pass vacuously).


# an audit whose JSON lacks the "exchanges" key is a violation, not a clean audit.
def test_disco_job_audit_refuses_missing_exchanges(tmp_path):
    src = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_job.sh')).read()
    m = re.search(r'cat > "\$T/audit.py" <<\'PY\'\n(.*?)\nPY\n', src, re.S)
    assert m, 'audit.py heredoc not found'
    p = tmp_path / 'audit.py'
    p.write_text(m.group(1))
    fake = tmp_path / 'bin'
    fake.mkdir()
    (fake / 'discobox').write_text('#!/bin/bash\necho \'{"renamed": []}\'\n')
    (fake / 'discobox').chmod(0o755)
    r = subprocess.run([sys.executable, str(p), 'sbx_x', str(tmp_path)], capture_output=True, text=True, cwd=tmp_path,
                       env={**os.environ, 'PATH': f'{fake}:{os.environ["PATH"]}'})
    assert r.returncode != 0 or 'VIOLATION' in (r.stdout + r.stderr), r.stdout


# rm_box must not read a listing without "sandboxes" as "gone".
def test_rm_box_refuses_a_listing_without_sandboxes():
    from tests.test_rm_box import rm_box_source
    script = f'''FAILS=""; DB(){{ case "$1" in ls) echo '{{"renamed":[]}}';; esac; }}; log(){{ echo "LOG $*"; }}; sleep(){{ :; }}
{rm_box_source()}
rm_box sbx_x; echo "rc=$? fails=[$FAILS]"'''
    out = sh(script).stdout
    assert 'rc=1' in out, out


# stack_units get prints nothing for a null and refuses a missing key loudly.
def test_units_get_null_and_missing(tmp_path):
    y = tmp_path / 'stack.yaml'
    y.write_text('host: {machine: m, rebuild_name: null}\ncomponents: []\n')
    # a null optional value prints nothing
    r = subprocess.run([sys.executable, os.path.join(LIB, 'stack_units.py'), str(y), 'get', 'host.rebuild_name', '--optional'], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == '', repr(r.stdout)
    assert 'None' not in r.stdout
    r = subprocess.run([sys.executable, os.path.join(LIB, 'stack_units.py'), str(y), 'get', 'host.nope'], capture_output=True, text=True)
    assert r.returncode != 0


# site.yaml may set only the site's own keys, never a pin, digest, image or profile.
def test_site_cannot_override_pins(tmp_path):
    import stack_site
    y = tmp_path / 'stack.yaml'
    y.write_text('host: {machine: m}\ncomponents:\n  - name: toolhive\n    pin: {tag: v1}\n')
    (tmp_path / 'site.yaml').write_text('host: {name: b}\ncomponents:\n  toolhive:\n    pin: {tag: v999}\n')
    with pytest.raises(SystemExit):
        stack_site.load(str(y))


# site values that would break a unit, a cron line or a shell are refused.
@pytest.mark.parametrize('key,value', [('user', 'a b'), ('user', 'x\ny'), ('internal_ip', '10.0.0.1; reboot'), ('name', 'Bad_Name!'),
                                       ('commit_email', 'a@b\nc'), ('commit_name', 'x"; rm -rf ~; "')])
def test_site_values_are_validated(tmp_path, key, value):
    import stack_site
    y = tmp_path / 'stack.yaml'
    y.write_text('host: {machine: m}\ncomponents: []\n')
    (tmp_path / 'site.yaml').write_text(json.dumps({'host': {key: value}}))
    with pytest.raises(SystemExit):
        stack_site.load(str(y))


# the agent's patches archive is unpacked strictly (a symlink member is refused, nothing written through it).
def test_disco_job_unpacks_patches_strictly():
    src = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_job.sh')).read()
    assert 'tar -xzf "$OUT/patches.tgz"' not in src
    assert 'safe_untar.py' in src


# credential-looking --env names AND values are refused, case-insensitively.
@pytest.mark.parametrize('kv', ['github_token=x', 'GH_AUTH=abc', 'FOO=ghp_abcdefghijklmnopqrstuvwxyz0123456789', 'X=sk-ant-oat01-abc', 'Y=github_pat_11AAA'])
def test_disco_job_refuses_credential_env(kv, tmp_path):
    src = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_job.sh')).read()
    m = re.search(r'^cred_env\(\)\{.*?return 1; \}\n', src, re.S | re.M)
    assert m, 'cred_env() not found in disco_job.sh'
    out = sh(m.group(0) + f'\ncred_env {kv!r} && echo REFUSED || echo ALLOWED').stdout
    assert 'REFUSED' in out


# the wheelhouse is compiled from binary wheels only, so no package's build code runs on the host.
def test_fan_wheelhouse_binary_only():
    src = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_fan_lib', 'worker.sh')).read().replace('\\\n', ' ')
    cmds = [l for l in src.splitlines() if re.search(r'pip (compile|download|wheel|install)', l) and not l.lstrip().startswith('#')]
    assert cmds
    for line in cmds:
        assert '--only-binary' in line, line
    assert 'uvx uv@' not in src and 'uvx --python' not in src, 'tools fetched by version, unhashed'


# decoy_proof waits for the xtables lock and cleans up on SIGPIPE.
def test_decoy_iptables_waits_and_traps_pipe():
    src = open(os.path.join(LIB, 'decoy_proof.sh')).read()
    assert all(' -w ' in l or 'iptables -w' in l for l in src.splitlines() if 'iptables -' in l and not l.lstrip().startswith('#'))
    assert re.search(r"trap .*PIPE", src)


# fan cleanup ignores further signals once it starts, and every in-box call has a timeout.
def test_fan_cleanup_is_not_interruptible():
    src = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_fan_lib', 'worker.sh')).read()
    fin = src[src.index('finish(){'):]
    assert "trap '' INT TERM HUP" in fin.split('\n', 1)[0] + fin.split('\n', 2)[1]



# decoy_proof's cleanup must not be cut short by SIGPIPE (its trap list resets PIPE too, or ignores it).
def test_decoy_cleanup_survives_sigpipe():
    src = open(os.path.join(LIB, 'decoy_proof.sh')).read()
    cl = src[src.index('cleanup(){'):].split('\n', 1)[0]
    assert "trap '' PIPE" in cl or re.search(r'trap - [A-Z ]*PIPE', cl), cl


# an audit whose "exchanges" is null is unreadable, not clean; decoy_proof reads it strictly too.
def test_audit_null_exchanges_is_a_violation(tmp_path):
    src = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_job.sh')).read()
    m = re.search(r'cat > "\$T/audit.py" <<\'PY\'\n(.*?)\nPY\n', src, re.S)
    p = tmp_path / 'audit.py'
    p.write_text(m.group(1))
    fake = tmp_path / 'bin'
    fake.mkdir()
    (fake / 'discobox').write_text('#!/bin/bash\necho \'{"exchanges": null}\'\n')
    (fake / 'discobox').chmod(0o755)
    r = subprocess.run([sys.executable, str(p), 'sbx_x', str(tmp_path)], capture_output=True, text=True,
                       env={**os.environ, 'PATH': f'{fake}:{os.environ["PATH"]}'})
    assert r.returncode != 0 or 'VIOLATION' in (r.stdout + r.stderr), r.stdout
    assert ".get('exchanges')" not in open(os.path.join(LIB, 'decoy_proof.sh')).read()


# a null required site value fails `get` (only --optional reads null as empty).
def test_units_get_null_required_fails(tmp_path):
    y = tmp_path / 'stack.yaml'
    y.write_text('host: {machine: m, name: null}\ncomponents: []\n')
    u = os.path.join(LIB, 'stack_units.py')
    r = subprocess.run([sys.executable, u, str(y), 'get', 'host.name'], capture_output=True, text=True)
    assert r.returncode != 0
    r = subprocess.run([sys.executable, u, str(y), 'get', 'host.name', '--optional'], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == ''


# the pip venv uses the host's own Python, never a downloaded one.
def test_fan_pip_venv_uses_host_python():
    src = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_fan_lib', 'worker.sh')).read()
    line = next(l for l in src.splitlines() if 'venv' in l and '$UV' in l)
    assert '--python /usr/bin/python3' in line and 'UV_PYTHON_DOWNLOADS=never' in src


# idle checks whose own command fails read busy (load average, the job-agent unit).
def test_idle_load_and_unit_errors_read_busy():
    src = open(os.path.join(LIB, 'idle_shutdown.sh')).read()
    assert 'loadavg-failed' in src and 'unit-state-failed' in src


# every Discobox call in fan cleanup is bounded.
def test_fan_discobox_calls_bounded():
    src = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_fan_lib', 'worker.sh')).read()
    db = next(l for l in src.splitlines() if l.startswith('DB(){'))
    assert 'timeout ' in db, db



def _rl():
    sys.path.insert(0, ROOT)
    from scripts.lid import route_lock
    return route_lock


# the lock also refuses any OTHER secret bound to the claude-code harness, and anything bound project-wide.
def test_lock_refuses_other_secrets_on_the_harness():
    rl = _rl()
    mine = {'id': 'b1', 'secretId': 's', 'targetKind': 'harnessConfig', 'targetId': 'h', 'routes': [{'hosts': ['api.anthropic.com']}]}
    other = {'id': 'b2', 'secretId': 'old', 'targetKind': 'harnessConfig', 'targetId': 'h', 'routes': [{'hosts': ['*']}]}
    elsewhere = {'id': 'b3', 'secretId': 'x', 'targetKind': 'harnessConfig', 'targetId': 'shell', 'routes': [{'hosts': ['*']}]}
    assert rl.others_problem([mine, elsewhere], 'h', 's', []) is None
    assert 'old' in (rl.others_problem([mine, other], 'h', 's', []) or '')
    assert rl.others_problem([mine], 'h', 's', [{'id': 'sec_pw'}]) is not None


# disco_job confirms its box archived (retries), and a box that never archives is logged as a LEAK.
def test_disco_job_confirms_the_archive():
    src = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_job.sh')).read()
    assert 'archived on try' in src and 'LEAK' in src


# every Discobox call disco_job's worker makes is bounded.
def test_disco_job_calls_bounded():
    src = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_job.sh')).read()
    w = src[src.index("cat > \"$T/worker.sh\" <<'WORKER'"):src.index('\nWORKER\n')]
    for l in w.splitlines():
        if re.search(r'(^|[;&|(]\s*)discobox ', l) and not l.lstrip().startswith('#'):
            assert 'timeout ' in l or 'dbx ' in l, l
    assert re.search(r'^dbx\(\)\{ timeout (-k [0-9]+ )?[0-9]+ discobox', w, re.M)


# systemd gives the fan worker's cleanup enough time: TimeoutStopSec covers every bounded step.
def test_fan_unit_stop_timeout_covers_cleanup():
    src = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_fan.sh')).read()
    m = re.search(r'TimeoutStopSec=(\d+)(h|min|s)\b', src)
    assert m and int(m.group(1)) * {'h': 3600, 'min': 60, 's': 1}[m.group(2)] >= 3 * 3600, m and m.group(0)


# decoy_proof also tries Host-header spoofing (Host: api.anthropic.com aimed at the sink).
def test_decoy_tries_host_spoofing():
    src = open(os.path.join(LIB, 'decoy_proof.sh')).read()
    assert 'Host: api.anthropic.com' in src and "'/spoof'" in src


# --env values with control characters are refused (job.conf's %q and shlex disagree on them).
@pytest.mark.parametrize('kv', ['A=x\ny', 'B=a\tb', 'C=\x01'])
def test_disco_job_refuses_control_chars_in_env(kv):
    src = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_job.sh')).read()
    m = re.search(r'^env_ok\(\)\{.*?\}\n', src, re.S | re.M)
    assert m, 'env_ok() not found'
    out = sh(m.group(0) + '\nenv_ok "$KV" && echo OK || echo REFUSED', env={'KV': kv}).stdout   # the real control character, via env
    assert 'REFUSED' in out


# what the agent's box sends back is size-capped on the host.
def test_disco_job_caps_box_output():
    src = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_job.sh')).read()
    assert 'head -c "$CAP"' in src


# fan box names carry a random token, so one job's cleanup sweep can't match another job's boxes.
def test_fan_run_id_is_random():
    src = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_fan_lib', 'worker.sh')).read()
    m = re.search(r'^RUN=(.*)$', src, re.M)
    assert m and ('RANDOM' in m.group(1) or 'urandom' in m.group(1)), m and m.group(1)


# decoy_proof removes its temporary iptables rule FIRST in cleanup, before any Discobox call.
def test_decoy_removes_rule_first():
    src = open(os.path.join(LIB, 'decoy_proof.sh')).read()
    body = src[src.index('cleanup(){'):src.index('trap cleanup EXIT')]
    assert body.index('iptables -w -D') < body.index('discobox')


# the idle counter lives in a root-only place and is read as a number, never shell-evaluated.
def test_idle_counter_root_only_and_numeric(tmp_path):
    src = open(os.path.join(LIB, 'idle_shutdown.sh')).read()
    assert 'S=/run/idle_shutdown.count' in src and '/var/tmp' not in src
    assert re.search(r'\[\[ \$c =~ \^\[0-9\]\+\$ \]\]', src)


# a first install with no claude_secret in site.yaml creates the secret (get --optional), and the message names site.yaml.
def test_discobox_up_first_install_without_secret():
    src = open(os.path.join(LIB, 'discobox_up.sh')).read()
    assert 'get claude_secret --optional' in src
    assert 'components.discobox.claude_secret to it in stack/site.yaml' in src


# the WIF argv test proves jq actually ran.
def test_wif_token_jq_ran_and_token_not_in_argv(tmp_path):
    fake = tmp_path / 'bin'
    fake.mkdir()
    log = tmp_path / 'argv.log'
    (fake / 'curl').write_text(f'#!/bin/bash\necho "curl $@" >> {log}\nif [[ "$*" == *metadata* ]]; then echo JWT_SECRET_VALUE; else cat >/dev/null; echo "{{\\"access_token\\":\\"t\\"}}"; fi\n')
    (fake / 'jq').write_text(f'#!/bin/bash\necho "jq $@ JWT_ENV=${{JWT:+set}}" >> {log}\ncat >/dev/null 2>&1 || true\necho \'{{"access_token":"t"}}\'\n')
    for f in fake.iterdir():
        f.chmod(0o755)
    env_dir = tmp_path / 'etc'
    env_dir.mkdir()
    src = open(os.path.join(LIB, 'wif_token.sh')).read().replace('/etc/epistemic-stack/anthropic_wif.env', str(env_dir / 'wif.env'))
    (env_dir / 'wif.env').write_text('ANTHROPIC_FEDERATION_RULE_ID=r\nANTHROPIC_ORGANIZATION_ID=o\nANTHROPIC_SERVICE_ACCOUNT_ID=s\nANTHROPIC_WORKSPACE_ID=w\n')
    p = tmp_path / 'wif.sh'
    p.write_text(src)
    sh(f'bash {p}', env={'PATH': f'{fake}:/usr/bin:/bin'})
    text = log.read_text()
    assert 'jq ' in text and 'JWT_ENV=set' in text, text
    assert 'JWT_SECRET_VALUE' not in text


# rm_box treats a box missing from the listing as removed (the "gone" path itself).
def test_rm_box_gone_path():
    from tests.test_rm_box import rm_box_source
    script = f'''FAILS=""; DB(){{ case "$1" in ls) echo '{{"sandboxes":[{{"id":"sbx_other","runtime":{{"state":"running"}}}}]}}';; esac; }}; log(){{ echo "LOG $*"; }}; sleep(){{ :; }}
{rm_box_source()}
rm_box sbx_x; echo "rc=$? fails=[$FAILS]"'''
    assert 'rc=0' in sh(script).stdout


# idle_shutdown's decisions, run for real with fakes for every probe it uses.
def _idle(tmp_path, who='', sshd='1 sshd: /usr/sbin/sshd -D', worker='', load='0.01 0.01 0.01 1/1 1', boxes='{"sandboxes": []}', ja='inactive', units=''):
    fake = tmp_path / 'bin'
    fake.mkdir(exist_ok=True)
    def w(n, body):
        (fake / n).write_text('#!/bin/bash\n' + body + '\n')
        (fake / n).chmod(0o755)
    w('who', f'printf "%s" {who!r}')
    w('pgrep', f'case "$*" in *sshd*) echo {sshd!r};; *worker*) [ -n {worker!r} ] && echo {worker!r} || exit 1;; esac')
    w('systemctl', f'case "$*" in *ActiveState*) echo {ja!r};; *list-units*) printf "%s" {units!r};; esac')
    w('runuser', '[ "$1" = -u ] && shift 2; [ "$1" = -- ] && shift; exec "$@"')
    w('id', 'case "$*" in "-u builder") echo 1000;; *) exec /usr/bin/id "$@";; esac')
    w('discobox', f"echo '{boxes}'")
    w('logger', 'echo "LOGGER $*"')
    w('shutdown', 'echo SHUTDOWN')
    src = open(os.path.join(LIB, 'idle_shutdown.sh')).read()
    src = re.sub(r'^export PATH=.*$', 'export PATH=$PATH   # the test injects its fakes through PATH', src, flags=re.M)
    src = src.replace('/proc/loadavg', str(tmp_path / 'loadavg')).replace('/run/idle_shutdown.count', str(tmp_path / 'count')).replace('/sbin/shutdown', 'shutdown')
    (tmp_path / 'loadavg').write_text(load + '\n')
    p = tmp_path / 'idle.sh'
    p.write_text(src)
    env = {'PATH': f'{fake}:/usr/bin:/bin', 'IDLE_BOX_USER': 'builder', 'IDLE_DISCOBOX': str(fake / 'discobox')}
    return sh(f'bash {p} --check', env=env).stdout.strip()


def test_idle_decisions(tmp_path):
    assert _idle(tmp_path) == 'idle'
    assert 'ssh' in _idle(tmp_path, sshd='2 sshd: builder@notty')
    assert 'login' in _idle(tmp_path, who='builder pts/0')
    assert 'disco-job-worker' in _idle(tmp_path, worker='123 bash /home/b/scratch/j/job/worker.sh')
    assert 'load' in _idle(tmp_path, load='0.90 0.5 0.4 1/1 1')
    assert 'discobox-boxes=1' in _idle(tmp_path, boxes='{"sandboxes":[{"runtime":{"state":"running","runtimeState":"running"}}]}')
    assert 'discobox-check-failed' in _idle(tmp_path, boxes='{"nope": 1}')
    assert 'job-agent' in _idle(tmp_path, ja='active')
    assert 'job-unit' in _idle(tmp_path, units='job-x.service loaded active running x')


# the round-3 fix review's findings, as tests.
def test_idle_check_flag_is_live(tmp_path):
    # --check must never advance the counter or shut down (it was swallowed by a comment)
    out = _idle(tmp_path)
    assert out == 'idle' and not (tmp_path / 'count').exists()


def test_take_returns_the_commands_status(tmp_path):
    src = open(os.path.join(ROOT, 'scripts', 'monitor', 'disco_job.sh')).read()
    m = re.search(r'^take\(\)\{.*?return "\$\{r\[0\]\}"; \}\n', src, re.S | re.M)
    assert m, 'take() not found'
    f = tmp_path / 'f'
    out = sh(f'CAP=4; log(){{ echo "LOG $*"; }}\n{m.group(0)}\ntake {f} bash -c "echo hello; exit 7"; echo rc=$?')
    assert 'rc=7' in out.stdout and 'hit the 4-byte cap' in out.stdout and f.read_bytes() == b'hell'


def test_discobox_up_get_passes_optional():
    src = open(os.path.join(LIB, 'discobox_up.sh')).read()
    m = re.search(r'^get\(\)\{.*?\}\n', src, re.M)
    assert '"${@:2}"' in m.group(0)



def _src(*p):
    return open(os.path.join(ROOT, *p)).read()


# the firewall is applied atomically (iptables-restore), never flushed and rebuilt rule by rule; ordered before user sessions.
def test_firewall_atomic():
    s = _src('scripts', 'lid', 'stack_up.sh')
    fw = s[s.index('epistemic_stack_fw.sh > /dev/null <<F'):s.index('\nF\n', s.index('epistemic_stack_fw.sh > /dev/null <<F'))]
    assert 'iptables-restore --noflush' in fw and 'iptables -F' not in fw and 'iptables -A' not in fw
    assert 'ip6tables-restore --noflush' in fw
    assert re.search(r'Before=.*user@', s)


# the box user reaches nothing on loopback but the DNS stub, and nothing on the IPv6 metadata address.
def test_firewall_loopback_allowlist_and_v6_metadata():
    s = _src('scripts', 'lid', 'stack_up.sh')
    assert '-d 127.0.0.53/32 -p tcp --dport 53' in s
    # by loopback ADDRESS, never the whole lo interface: the host's own address routes over lo (the gateway broke on 11/18)
    # system ports + LLMNR only: Discobox delivers job source to the pool on an ephemeral loopback port (11/18 fw-dry1)
    assert '-o lo -d 127.0.0.0/8 -p tcp -m multiport --dports 1:1023,5355 -m owner --uid-owner' in s
    assert '-o lo -d ::1/128 -p tcp -m multiport --dports 1:1023,5355,$PORTS -m owner --uid-owner' in s
    assert not re.search(r'-o lo -p tcp -m owner --uid-owner \\\$U -j REJECT', s)
    assert 'fd20:ce::254' in s


# values the box reports are checked before the host uses them in arithmetic or paths.
def test_fan_box_reported_values_validated():
    s = _src('scripts', 'monitor', 'disco_fan_lib', 'worker.sh')
    assert re.search(r'\[\[ \$GLIBC =~ \^\[0-9\]\{1,3\}\$ \]\]', s)
    assert re.search(r'\[\[ \$PYV =~', s) and re.search(r'\[\[ \$ARCH =~', s)


# disco_job's finish() sweeps for its own box by source path when the create failed without an id.
def test_disco_job_names_and_sweeps_its_box():
    # discobox new has no name flag; every box records config.source.localDirectory, which is this job's own $W/src.
    s = _src('scripts', 'monitor', 'disco_job.sh')
    assert 'localDirectory' in s and 'sweep' in s


# stack_secret no longer stops the box at all (the idle shutdown does).


# a refused --src-tar upload stops the launch.
def test_src_tar_upload_refusal_not_swallowed():
    s = _src('scripts', 'monitor', 'disco_job.sh')
    i = s.index('cat > \\$W/job/src.tgz')
    after = s[i:i + 400]
    assert '|| urc=$?' in after and '[ "$urc" -eq 0 ] ||' in after, after


# --env may not override the job's own switches.
@pytest.mark.parametrize('kv', ['ENABLE_CLAUDEAI_MCP_SERVERS=true', 'ANTHROPIC_BASE_URL=http://x', 'HTTPS_PROXY=http://x',
                                'https_proxy=http://x', 'NODE_OPTIONS=--require=/x', 'CLAUDE_CONFIG_DIR=/x', 'PATH=/x', 'LD_PRELOAD=/x'])
def test_env_refuses_reserved_names(kv):
    s = _src('scripts', 'monitor', 'disco_job.sh')
    fn = re.search(r'^cred_env\(\)\{.*?return 1; \}\n', s, re.S | re.M).group(0) + re.search(r'^env_ok\(\)\{.*?\}\n', s, re.S | re.M).group(0)
    out = sh(fn + '\n{ cred_env "$KV" || ! env_ok "$KV"; } && echo REFUSED || echo OK', env={'KV': kv}).stdout
    assert 'REFUSED' in out, kv


# newer OpenSSH session processes (sshd-session) read busy.
def test_idle_sees_sshd_session(tmp_path):
    assert 'ssh' in _idle(tmp_path, sshd='2 sshd-session: builder@notty')


# the fan preflight refuses a shell harness with its own egress allow list or no deny-by-default.
def test_fan_preflight_checks_harness_egress():
    s = _src('scripts', 'monitor', 'disco_fan_lib', 'worker.sh')
    assert "h.get('egress')" in s or 'h["egress"]' in s or "h['egress']" in s


# The live check lives in decoy_proof: a box must not reach the host's loopback services (sshd, mail).
def test_decoy_probes_host_loopback():
    s = open(os.path.join(LIB, 'decoy_proof.sh')).read()
    assert 'hostlo_proxy_' in s and 'hostlo_direct_' in s and "k.startswith('hostlo_')" in s



def _fw():
    s = _src('scripts', 'lid', 'stack_up.sh')
    i = s.index('epistemic_stack_fw.sh > /dev/null <<F')
    return s, s[i:s.index('\nF\n', i)]


# the box user is refused the ToolHive proxies on loopback (v4 and v6), explicitly; and UDP on loopback except DNS.
def test_fw_toolhive_ports_refused_to_box_user_on_loopback():
    s, fw = _fw()
    assert re.search(r'-o lo -d \$TB -p tcp -m multiport --dports \$PORTS -m owner --uid-owner \\\$U -j REJECT', fw), 'v4 ToolHive reject'
    assert re.search(r'-o lo -d 127\.0\.0\.0/8 -p udp ! --dport 53 -m owner --uid-owner \\\$U -j REJECT', fw), 'v4 udp'
    assert re.search(r'-o lo -d ::1/128 -p udp ! --dport 53 -m owner --uid-owner \\\$U -j REJECT', fw), 'v6 udp'


# the unit's Before= names a real unit (the uid is filled in, not written as a literal $(id -u)).
def test_fw_unit_before_is_concrete():
    s, _ = _fw()
    unit = s[s.index('epistemic-stack-fw.service > /dev/null <<'):]
    unit = unit[:unit.index('\nU\n')]
    assert '$(id -u)' not in unit or "<<U" in unit.split('\n', 1)[0], 'Before= is written literally inside a quoted heredoc'
    assert re.search(r'Before=.*user@', unit)


# the v4 jumps go in before (and regardless of) the v6 step.
def test_fw_v4_jumps_before_v6():
    _, fw = _fw()
    assert fw.index('iptables -I \\$c 1 -j EPI_STACK_\\$c') < fw.index('ip6tables-restore')
    assert re.search(r'ip6tables-restore --noflush <<R6 \|\| ', fw) or 'set +e' in fw[fw.index('ip6tables-restore') - 200:fw.index('ip6tables-restore')]


# jobs refuse to start when the firewall is not in force (the box user's metadata probe must be refused).
def test_jobs_check_the_firewall_before_starting():
    for p in (('scripts', 'monitor', 'disco_job.sh'), ('scripts', 'monitor', 'disco_fan_lib', 'worker.sh')):
        s = _src(*p)
        assert 'firewall not in force' in s, p


# disco_job's finish ignores further signals once cleanup starts.
def test_disco_job_finish_ignores_signals():
    s = _src('scripts', 'monitor', 'disco_job.sh')
    fin = s[s.index('finish(){ rc=$?;'):].split('\n', 1)[0]
    assert "trap '' INT TERM HUP" in fin, fin


# stack_test and decoy_proof confirm their boxes archived (rm can return 0 and leave a box running).
def test_test_drivers_confirm_archive():
    for p in ('stack_test.sh', 'decoy_proof.sh'):
        s = _src('scripts', 'lid', p)
        assert 'archived on try' in s and 'LEAK' in s, p


# disco_job's own rm_box runs for real: a box that never archives is a LEAK and returns 1.
def test_disco_job_rm_box_for_real():
    s = _src('scripts', 'monitor', 'disco_job.sh')
    m = re.search(r'^rm_box\(\)\{.*?LEAK"; return 1; \}\n', s, re.S | re.M)
    assert m, 'disco_job rm_box not found'
    body = m.group(0).replace('timeout 300 discobox', 'fakebox')
    script = f'''dbx(){{ :; }}; fakebox(){{ echo '{{"sandboxes":[{{"id":"sbx_x","runtime":{{"state":"failed"}}}}]}}'; }}; log(){{ echo "LOG $*"; }}; sleep(){{ :; }}
{body}
rm_box sbx_x; echo "rc=$?"'''
    out = sh(script).stdout
    assert 'rc=1' in out and 'LEAK' in out, out


# the firewall probe survives set -e (a refused curl is the expected, passing case).
def test_firewall_probe_survives_set_e():
    for p in (('scripts', 'monitor', 'disco_job.sh'), ('scripts', 'monitor', 'disco_fan_lib', 'worker.sh')):
        s = _src(*p)
        line = next(l for l in s.splitlines() if 'http://169.254.169.254/' in l and 'curl' in l)
        assert '&& fwrc=0 || fwrc=$?' in line, line
        out = sh('set -e\n' + line.replace('http://169.254.169.254/', 'http://127.0.0.1:1/') + '\necho "after rc=$fwrc"').stdout
        assert 'after rc=' in out, out



# under cron's PATH=/usr/bin:/bin, runuser (/usr/sbin) was not found, so the
# Discobox check failed on every run. The script sets its own PATH.
def test_idle_sets_its_own_path():
    s = _src('scripts', 'lid', 'idle_shutdown.sh')
    m = re.search(r'^export PATH=([^\s#]+)', s, re.M)
    assert m and '/usr/sbin' in m.group(1).split(':') and '/usr/bin' in m.group(1).split(':'), m and m.group(0)
    code = [l for l in s.splitlines() if not l.lstrip().startswith('#')]
    first = lambda w: next(i for i, l in enumerate(code) if w in l)
    assert first('export PATH=') < first('runuser')   # the first CODE use (a comment may name runuser)


# a null box list (an empty server) reads as zero boxes, not as a failed check.
def test_idle_null_box_list_is_idle(tmp_path):
    assert _idle(tmp_path, boxes='{"sandboxes": null}') == 'idle'


# jobs also require the firewall unit itself to be active (an IPv6 failure leaves it failed while IPv4 still refuses).
def test_jobs_require_the_firewall_unit_active():
    for p in (('scripts', 'monitor', 'disco_job.sh'), ('scripts', 'monitor', 'disco_fan_lib', 'worker.sh')):
        assert 'systemctl is-active -q epistemic-stack-fw.service' in _src(*p), p


# the route lock refuses any binding at a target kind it does not know is harmless (pool, box, project...).
def test_lock_refuses_bindings_at_other_target_kinds():
    rl = _rl()
    mine = {'id': 'b1', 'secretId': 's', 'targetKind': 'harnessConfig', 'targetId': 'h'}
    pool = {'id': 'b2', 'secretId': 'old', 'targetKind': 'pool', 'targetId': 'p1'}
    assert 'pool' in (rl.others_problem([mine, pool], 'h', 's', []) or '')
    shell = {'id': 'b3', 'secretId': 'x', 'targetKind': 'harnessConfig', 'targetId': 'shell'}
    assert rl.others_problem([mine, shell], 'h', 's', []) is None


# disco_job's orphan sweep logs when it cannot read the listing.
def test_disco_job_sweep_logs_failure():
    assert 'sweep FAILED' in _src('scripts', 'monitor', 'disco_job.sh')


# disco_job stops its agent before the audit snapshot.
def test_disco_job_stops_agent_before_audit():
    s = _src('scripts', 'monitor', 'disco_job.sh')
    fin = s[s.index('finish(){ rc=$?;'):s.index('trap finish EXIT')]
    assert 'systemctl stop job-agent' in fin and fin.index('systemctl stop job-agent') < fin.index('audit.py')


# a leaked test box fails stack_test and decoy_proof.
def test_test_drivers_fail_on_leak():
    st = _src('scripts', 'lid', 'stack_test.sh')
    assert 'leaked=1' in st and re.search(r'\[ -z "\$\{leaked:-\}" \]', st)
    dp = _src('scripts', 'lid', 'decoy_proof.sh')
    assert 'rm_box "$id" > "$W/out/rm.txt"' in dp


# stack_secret leaves powering off to the idle shutdown (its own ssh session always reads busy).
def test_stack_secret_leaves_poweroff_to_idle():
    s = _src('scripts', 'lid', 'stack_secret.sh')
    assert 'instances stop' not in s


# a missing claude_secret says so.
def test_disco_job_missing_secret_message():
    s = _src('scripts', 'monitor', 'disco_job.sh')
    line = next(l for l in s.splitlines() if l.startswith('SECID='))
    assert '|| {' in line and 'claude_secret' in line, line


# stack_test fails if any loopback listener on a high port belongs to something other than the known owners.
def test_stack_test_checks_loopback_listeners():
    assert 'loopback_listeners.py' in _src('scripts', 'lid', 'stack_test.sh')



# stack_test's (j) checks ss's own status and that it listed something, never PASS on a failed or empty listing.
def test_stack_test_j_fails_closed():
    s = _src('scripts', 'lid', 'stack_test.sh')
    j = s[s.index('# (j)'):s.index('cleanup\n[ "$fails" -eq 0 ]')]
    assert '2>/dev/null' not in j and '|| true' not in j, j
    assert 'loopback_listeners.py' in j and 'sudo -n ss' in j, j
    assert 'ss failed' not in j and 'the listing or the check failed' in j, j   # the message names both causes


# the in-box agent stop in finish() has its own short timeout.
def test_finish_agent_stop_is_bounded():
    s = _src('scripts', 'monitor', 'disco_job.sh')
    line = next(l for l in s.splitlines() if 'systemctl stop job-agent' in l)
    assert re.search(r'timeout [0-9]{1,3} discobox', line), line


# stack_secret's header and dead state follow Y8; Z4: one SECID message naming the merged files, no dead fallback.
def test_r8_text():
    st = _src('scripts', 'lid', 'stack_secret.sh')
    assert 'stops it again' not in st and '\nstarted=' not in st
    dj = _src('scripts', 'monitor', 'disco_job.sh')
    assert 'claude_secret in stack/stack.yaml" >&2; exit 2; }' not in dj
    assert 'stack/stack.yaml or stack/site.yaml' in dj





def _between(s, a, b):
    return s[s.index(a):s.index(b, s.index(a))]


# the box cap is atomic: the live count and `discobox new` happen under one flock, after the clone, and the count
# reads the JSON listing (archived, deleted and error boxes do not count).
def test_box_cap_is_atomic():
    s = _src('scripts', 'monitor', 'disco_job.sh')
    pre = _between(s, 'log "== $stage $(date -u +%T)"', 'stage preflight clone')
    assert 'boxes already live' not in pre, 'the cap check moved out of preflight'
    box = _between(s, 'stage clone box', 'stage box setup')
    assert 'flock' in box and box.index('flock') < box.index('boxes already live') < box.index('discobox new --from-json')
    assert 'flock -u 9' in box and box.index('discobox new --from-json') < box.index('flock -u 9')
    assert re.search(r"[\"']archived[\"'], [\"']deleted[\"']", box) and 'awk' not in box   # the idle predicate


# a Claude job whose box stops answering fails within minutes, not at its timeout.
def test_agent_poll_fails_fast_when_the_box_is_gone():
    s = _src('scripts', 'monitor', 'disco_job.sh')
    loop = _between(s, 'end=$(( $(date +%s) + TMIN*60 ))', 'stage agent export')
    assert 'box unreachable' in loop   # the time-based bound itself: test_poll_loop_is_bounded_and_watches_the_agent


# a worker stopped by a host shutdown leaves the job for job_sweep (no == done, no VIOLATION/LEAK written on a
# server that is going away).
def test_workers_leave_a_shutdown_to_the_sweep():
    for p, fin in ((('scripts', 'monitor', 'disco_job.sh'), 'finish(){ rc=$?;'), (('scripts', 'monitor', 'disco_fan_lib', 'worker.sh'), 'finish(){ rc=$?;')):
        s = _src(*p)
        f = s[s.index(fin):]
        assert 'is-system-running' in f and 'left for job_sweep' in f, p
        assert f.index('is-system-running') < re.search(r'audit\.py|audit "\$i"', f).start(), p   # the audit CALL, not a comment


# test_shutdown_check_behaviour superseded by test_shutdown_check_behaviour_without_dbus.



# the cap lock is released first thing in cleanup, and the create itself is bounded under the lock's own wait.
def test_cap_lock_released_in_cleanup_and_create_bounded():
    s = _src('scripts', 'monitor', 'disco_job.sh')
    fin = s[s.index('finish(){ rc=$?;'):]
    assert 'flock -u 9' in fin.split('\n')[0] + fin.split('\n')[1] + fin.split('\n')[2], 'released at the top of finish'
    box = _between(s, 'stage clone box', 'stage box setup')
    assert re.search(r'timeout (?:-k \d+ )?(\d+) discobox new', box) and int(re.search(r'timeout (?:-k \d+ )?(\d+) discobox new', box).group(1)) < 1800


# every box call in the agent poll loop is bounded, none auto-starts the server, and the job fails when the box
# is unreachable for 5 minutes (by time) or the agent unit is gone without DONE or agent.exit.
def test_poll_loop_is_bounded_and_watches_the_agent():
    s = _src('scripts', 'monitor', 'disco_job.sh')
    loop = _between(s, 'end=$(( $(date +%s) + TMIN*60 ))', 'stage agent export')
    calls = [l for l in loop.splitlines() if 'discobox' in l or 'inbox' in l or 'quick ' in l]
    assert calls and all('inbox ' not in l for l in calls), calls
    assert 'is-active' in loop and 'job-agent' in loop and 'agent unit gone' in loop
    assert re.search(r'\$now - \$?first_miss|\$\(\(now ?- ?first_miss\)\)', loop) and 'box unreachable for 5 min' in loop   # server auto-start


# a shutdown is also detected when systemctl cannot answer (D-Bus gone) or /run/nologin exists.
def test_shutdown_check_behaviour_without_dbus(tmp_path):
    fake = tmp_path / 'bin'
    fake.mkdir()
    (fake / 'systemctl').write_text('#!/bin/sh\n[ "$1" = is-system-running ] && { [ -n "$STATE" ] && echo "$STATE"; exit 1; }\nexit 0\n')
    (fake / 'systemctl').chmod(0o755)
    s = _src('scripts', 'monitor', 'disco_job.sh')
    line = next(l for l in s.splitlines() if 'is-system-running' in l)
    for state, expect in (('stopping', 98), ('', 98), ('offline', 98), ('running', 0), ('degraded', 0)):
        script = f'L={tmp_path}/run.log; stage=agent; log(){{ echo "$*" >> "$L"; }}\n{line}\nexit 0\n'   # boot states
        r = subprocess.run(['bash', '-c', script], env={'PATH': f'{fake}:/usr/bin:/bin', 'STATE': state})
        assert r.returncode == expect, (state, r.returncode)
    assert '/run/nologin' in line
    w = _src('scripts', 'monitor', 'disco_fan_lib', 'worker.sh')
    assert next(l for l in w.splitlines() if 'is-system-running' in l).strip() == line.strip()


# the cap count treats stopped boxes as not live, as the idle check does.
def test_cap_count_matches_the_idle_predicate():
    box = _between(_src('scripts', 'monitor', 'disco_job.sh'), 'stage clone box', 'stage box setup')
    assert 'runtimeState' in box and 'stopped' in box   # exactly the idle predicate



def _run_poll(tmp_path, tokens, recheck=()):
    """Run disco_job.sh's real agent poll loop with a scripted box: each poll's state call returns the next token ('' is a
    failed or timed-out call); a DONE re-check returns the next of `recheck`. Time moves 20 s per sleep."""
    s = _src('scripts', 'monitor', 'disco_job.sh')
    loop = s[s.index('end=$(( $(date +%s) + TMIN*60 ))'):s.index('stage agent export')]
    q = tmp_path / 'q'; q.write_text('\n'.join(tokens) + '\n')
    rq = tmp_path / 'rq'; rq.write_text('\n'.join(recheck) + '\n')
    script = f'''
TMIN=25; OUT={tmp_path}; n=0; T0=1000000
date(){{ if [ "$1" = +%s ]; then echo $((T0 + n * 20)); else echo 00:00:00; fi; }}
sleep(){{ n=$((n + 1)); }}
log(){{ :; }}
die(){{ echo "DIE $1 $2"; exit "$1"; }}
pop(){{ local f=$1 l; l=$(head -1 "$f"); tail -n +2 "$f" > "$f.t"; mv "$f.t" "$f"; printf '%s' "$l"; }}
quick(){{ case "$1" in
  bash) t=$(pop {q}); [ -n "$t" ] || return 124; echo "$t";;
  test) t=$(pop {rq}); [ "$t" = yes ];;
  *) return 0;; esac; }}
{loop}
echo LOOP-DONE
'''
    r = subprocess.run(['bash', '-c', script], capture_output=True, text=True, timeout=60)
    return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr


@pytest.mark.parametrize('tokens,recheck,expect', [
    (['running', 'done'], [], 'LOOP-DONE'),
    (['running', 'exit'], ['yes'], 'LOOP-DONE'),                      # DONE appeared just after agent.exit: still a success
    (['running', 'exit'], ['no'], 'DIE 10 agent ended without DONE'),
    (['running', 'gone', 'running', 'done'], [], 'LOOP-DONE'),       # one 'gone' (a slow systemctl) is not enough
    (['running', 'gone', 'gone'], [], 'DIE 8 agent unit gone without DONE (the box restarted under it?)'),
    ([''] * 5 + ['running', 'done'], [], 'LOOP-DONE'),               # a slow or failed call is a miss, never 'no DONE'
    ([''] * 20, [], 'DIE 8 box unreachable for 5 min'),
    (['', 'gone', 'running', 'done'], [], 'LOOP-DONE'),
])
def test_poll_loop_behaviour(tmp_path, tokens, recheck, expect):
    assert _run_poll(tmp_path, tokens, recheck) == expect


def test_poll_loop_one_state_call_per_poll_and_server_may_start():
    s = _src('scripts', 'monitor', 'disco_job.sh')
    loop = _between(s, 'end=$(( $(date +%s) + TMIN*60 ))', 'stage agent export')
    assert len(re.findall(r'quick bash -c', loop)) == 1
    q = next(l for l in s.splitlines() if l.startswith('quick(){'))
    assert '--auto-start-server=false' not in q and re.search(r'timeout (-k \d+ )?\d+ discobox', q)


# The shutdown check: only stopping, offline or no answer (D-Bus gone), or /run/nologin; a host still starting is not
# shutting down; the 98 line keeps the job's own reason.
def test_shutdown_check_boot_states(tmp_path):
    fake = tmp_path / 'bin'
    fake.mkdir()
    (fake / 'systemctl').write_text('#!/bin/sh\n[ "$1" = is-system-running ] && { [ -n "$STATE" ] && echo "$STATE"; exit 1; }\nexit 0\n')
    (fake / 'systemctl').chmod(0o755)
    line = next(l for l in _src('scripts', 'monitor', 'disco_job.sh').splitlines() if 'is-system-running' in l)
    for state, expect in (('starting', 0), ('initializing', 0), ('maintenance', 0), ('stopping', 98), ('', 98), ('offline', 98)):
        script = f'L={tmp_path}/run.log; stage=agent; why="the reason"; log(){{ echo "$*" >> "$L"; }}\n{line}\nexit 0\n'
        r = subprocess.run(['bash', '-c', script], env={'PATH': f'{fake}:/usr/bin:/bin', 'STATE': state})
        assert r.returncode == expect, (state, r.returncode)
    assert 'the reason' in (tmp_path / 'run.log').read_text()


def test_cap_predicate_is_exactly_the_idle_one():
    box = _between(_src('scripts', 'monitor', 'disco_job.sh'), 'stage clone box', 'stage box setup')
    assert '"error"' not in box and "'error'" not in box



# a fan job takes the same box-cap lock and refuses when the host's live boxes plus its shards exceed its cap.
def test_fan_takes_the_box_cap_lock():
    w = _src('scripts', 'monitor', 'disco_fan_lib', 'worker.sh')
    boxes = _between(w, 'begin boxes', 'begin probe')
    assert 'flock' in boxes and '.box-cap.lock' in boxes and 'flock -u 9' in boxes
    assert boxes.index('flock -w') < boxes.index('admin box create') < boxes.index('flock -u 9')
    assert re.search(r'live \+ SHARDS', boxes) and 'CAP' in boxes
    assert 'CAP=%q' in _src('scripts', 'monitor', 'disco_fan.sh')


# every Claude job names its harness, so a different project default cannot change what its box runs.
def test_req_pins_the_claude_harness(tmp_path):
    s = _src('scripts', 'monitor', 'disco_job.sh')
    req = s[s.index("cat > \"$T/req.py\" <<'PY'") + len("cat > \"$T/req.py\" <<'PY'\n"):]
    req = req[:req.index('\nPY\n')]
    (tmp_path / 'req.py').write_text(req)
    (tmp_path / 'job.conf').write_text('JOB=x\nENVS=(A=1 )\n')
    out = subprocess.run([sys.executable, '-I', str(tmp_path / 'req.py')], capture_output=True, text=True, cwd=tmp_path)
    d = json.loads(out.stdout)
    assert d['harness'] == 'claude-code' and 'A=1' in d['env']


# IPv6: the box user is refused every local address but ::1 (all ports), and the gateway user all IPv6.
def test_v6_local_addresses_and_gateway_user():
    s = _src('scripts', 'lid', 'stack_up.sh')
    v6 = s[s.index('ip6tables-restore --noflush <<R6'):s.index('R6\n', s.index('ip6tables-restore --noflush <<R6') + 40)]
    assert re.search(r'-m addrtype --dst-type LOCAL ! -d ::1/128 -p tcp -m owner --uid-owner \\\$U -j REJECT --reject-with tcp-reset', v6), v6
    assert re.search(r'-m addrtype --dst-type LOCAL ! -d ::1/128 -m owner --uid-owner \\\$U -j REJECT', v6)
    assert re.search(r'-A EPI_STACK_OUTPUT -m owner --uid-owner \$GWU -j REJECT', v6)


# two launches of one job name cannot both start: the job's directory is created atomically (no test-then-mkdir).
def test_launch_dirs_are_atomic():
    dj = _src('scripts', 'monitor', 'disco_job.sh')
    assert '[ ! -e \\$W/logs ]' not in dj and '[ ! -e \\$W ]' not in dj
    assert re.search(r'mkdir \\\$W/logs 2>/dev/null \|\| \{ echo \\"refuse', dj)
    df = _src('scripts', 'monitor', 'disco_fan.sh')
    assert 'if [ -e \\"\\$W\\" ]' not in df and re.search(r'mkdir \\"\\\$W\\" 2>/dev/null \|\| \{ echo \\"refuse', df)


# Lead: jobs re-check the loopback listeners at preflight (a listener started after stack_test is caught before a job).
def test_jobs_check_loopback_listeners_at_preflight():
    dj = _src('scripts', 'monitor', 'disco_job.sh')
    assert 'loopback_listeners.py' in dj.split('stage preflight clone')[0] and 'loopback_listeners.py' in dj[dj.index('B=$(cd "$T" && tar'):]
    w = _src('scripts', 'monitor', 'disco_fan_lib', 'worker.sh')
    assert 'loopback_listeners.py' in _between(w, 'begin preflight', 'begin boxes')
    assert 'loopback_listeners.py' in _src('scripts', 'monitor', 'disco_fan.sh')


# a fan job's refusal carries its reason on the stage exit line (the soak reads 'boxes already live' from it).
def test_fan_refusal_reason_on_the_exit_line():
    w = _src('scripts', 'monitor', 'disco_fan_lib', 'worker.sh')
    fin = w[w.index('finish(){ rc=$?;'):]
    assert re.search(r'log "== \$stage exit \$rc\$\{why:\+ \(\$why\)\}"', fin)
    boxes = _between(w, 'begin boxes', 'begin probe')
    assert re.search(r'why="\$live boxes already live', boxes)
