"""Result pulls are size-capped, truncated audits are never clean, and remote calls carry kill timeouts.

- fan results are pulled from a box only under a size cap (a shard could fill the lid's disk before safe_untar's limits);
- an audit that comes back at its --limit is truncated, never 'clean' (Claude jobs, fan jobs, the decoy proof);
- the decoy's metadata probes record only a status, a length and a hash, never a body;
- disco_job.sh's bounded calls also carry a kill timeout (-k), as the other creators do.
"""
import os
import re
import subprocess

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _src(*p):
    return open(os.path.join(ROOT, *p)).read()


def _collect(tmp_path, size):
    w = _src('scripts', 'monitor', 'disco_fan_lib', 'worker.sh')
    fn = re.search(r'^collect\(\)\{.*?^\}\n', w, re.S | re.M).group(0)
    cap = re.search(r'^PULL_CAP=.*$', w, re.M).group(0)
    script = f'''
OUT={tmp_path}/out; W={tmp_path}; J={tmp_path}; L={tmp_path}/run.log; FAILS=""; mkdir -p $OUT
log(){{ echo "$*" >> "$L"; }}
in_box(){{ case "$2" in stat) echo {size};; *) return 0;; esac; }}
DB(){{ echo "DB $*" >> {tmp_path}/db.calls; return 0; }}
{cap}
{fn}
collect 0 sbx_x
echo "FAILS=$FAILS"
'''
    r = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
    calls = (tmp_path / 'db.calls').read_text() if (tmp_path / 'db.calls').exists() else ''
    return r.stdout, calls, (tmp_path / 'run.log').read_text() if (tmp_path / 'run.log').exists() else ''


def test_an_oversized_fan_result_is_refused_before_it_is_copied(tmp_path):
    out, calls, log = _collect(tmp_path, 10 << 30)
    assert 'collect-0' in out and ' cp ' not in calls and 'cap' in log, (out, calls, log)


def test_an_unreadable_result_size_is_refused(tmp_path):
    out, calls, log = _collect(tmp_path, '')
    assert 'collect-0' in out and ' cp ' not in calls, (out, calls, log)


def test_a_result_within_the_cap_is_copied(tmp_path):
    out, calls, log = _collect(tmp_path, 1 << 20)
    assert ' cp ' in calls, (out, calls, log)


def test_every_audit_treats_a_full_page_as_truncated():
    job = _src('scripts', 'monitor', 'disco_job.sh')
    a = job[job.index('cat > "$T/audit.py"'):]
    a = a[:a.index('\nPY\n')]
    assert re.search(r"len\(ex\) >= LIMIT", a) and 'VIOLATION audit truncated' in a and "str(LIMIT)" in a
    w = _src('scripts', 'monitor', 'disco_fan_lib', 'worker.sh')
    au = w[w.index('audit(){'):]
    assert re.search(r"len\(ex\) >= LIMIT", au) and 'truncated' in au and 'str(LIMIT)' in au
    d = _src('scripts', 'lid', 'decoy_proof.sh')
    assert re.search(r'len\(ex\) >= (\d+)', d) and 'truncated' in d
    lim = re.search(r'--limit (\d+)', d).group(1)
    assert re.search(r'len\(ex\) >= ' + lim, d)


def test_decoy_metadata_probes_keep_no_body():
    d = _src('scripts', 'lid', 'decoy_proof.sh')
    loop = next(l for l in d.splitlines() if 'computeMetadata' in l)
    m = re.search(r'\b(\w+) -H Metadata-Flavor:Google', loop).group(1)
    fn = next(l for l in d.splitlines() if l.startswith(f'{m}(){{'))
    assert m != 'c' and 'head -c' not in fn and 'sha256sum' in fn, (m, fn)


def test_disco_job_bounded_calls_carry_a_kill_timeout():
    job = _src('scripts', 'monitor', 'disco_job.sh')
    assert re.search(r'^dbx\(\)\{ timeout -k \d+ \d+ discobox', job, re.M)
    assert re.search(r'timeout -k \d+ 900 discobox new --from-json', job)



def test_a_failed_size_read_refuses_the_shard_under_set_e(tmp_path):
    w = _src('scripts', 'monitor', 'disco_fan_lib', 'worker.sh')
    fn = re.search(r'^collect\(\)\{.*?^\}\n', w, re.S | re.M).group(0)
    cap = re.search(r'^PULL_CAP=.*$', w, re.M).group(0)
    script = f'''set -eo pipefail
OUT={tmp_path}/out; W={tmp_path}; J={tmp_path}; L={tmp_path}/run.log; FAILS=""; mkdir -p $OUT
log(){{ echo "$*" >> "$L"; }}
in_box(){{ case "$2" in stat) return 1;; *) return 0;; esac; }}
DB(){{ echo "DB $*" >> {tmp_path}/db.calls; return 0; }}
{cap}
{fn}
collect 0 sbx_x
echo "FAILS=$FAILS"
'''
    r = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
    assert r.returncode == 0 and 'collect-0' in r.stdout, (r.returncode, r.stdout, r.stderr)
    assert not (tmp_path / 'db.calls').exists() or ' cp ' not in (tmp_path / 'db.calls').read_text()


def _audit_py(tmp_path, n):
    job = _src('scripts', 'monitor', 'disco_job.sh')
    a = job.index('cat > "$T/audit.py" <<\'PY\'\n') + len('cat > "$T/audit.py" <<\'PY\'\n')
    (tmp_path / 'audit.py').write_text(job[a:job.index('\nPY\n', a)] + '\n')
    b = tmp_path / 'bin'
    b.mkdir()
    (b / 'discobox').write_text('#!/usr/bin/env python3\nimport json, os\n'
                                'print(json.dumps({"exchanges": [{"host": "api.anthropic.com", "url": "/x", "status": 200}] * int(os.environ["N"])}))\n')
    (b / 'discobox').chmod(0o755)
    out = tmp_path / 'out'
    out.mkdir()
    return subprocess.run(['python3', '-I', str(tmp_path / 'audit.py'), 'sbx_x', str(out)], capture_output=True, text=True,
                          env={'PATH': f'{b}:' + os.environ['PATH'], 'N': str(n)})


def test_the_job_audit_at_its_limit_is_truncated_and_below_it_is_not(tmp_path):
    job = _src('scripts', 'monitor', 'disco_job.sh')
    limit = int(re.search(r'^LIMIT = (\d+)$', job, re.M).group(1))
    r = _audit_py(tmp_path / 'a', limit) if (tmp_path / 'a').mkdir() is None else None
    assert r.returncode == 1 and 'VIOLATION audit truncated' in r.stdout, (r.returncode, r.stdout[-300:], r.stderr[-300:])
    r = _audit_py(tmp_path / 'b', limit - 1) if (tmp_path / 'b').mkdir() is None else None
    assert 'truncated' not in r.stdout, r.stdout[-300:]


def test_job_sweep_carries_a_truncated_audit_as_its_violation(tmp_path):
    import sys
    sys.path.insert(0, os.path.join(ROOT, 'scripts', 'lid'))
    import job_sweep
    jd = tmp_path / 'job1'
    (jd / 'job').mkdir(parents=True)
    (jd / 'out').mkdir()
    (jd / 'job' / 'audit.py').write_text("import sys\nprint('VIOLATION audit truncated at 1000000 exchanges'); sys.exit(1)\n")
    text = job_sweep.Discobox('discobox').audit(str(jd), 'sbx_x')
    notes = job_sweep._violations('sbx_x', text)
    assert notes and 'truncated' in notes[0], notes
    assert any('exited 1' in n for n in notes), notes   # the audit's own failure is recorded too


def test_decoy_probes_never_read_a_stale_body():
    d = _src('scripts', 'lid', 'decoy_proof.sh')
    for name in ('c', 'meta'):
        fn = next(l for l in d.splitlines() if l.startswith(f'{name}(){{'))
        assert fn.startswith(f'{name}(){{ rm -f /tmp/r;'), fn


def test_quick_calls_carry_a_kill_timeout():
    job = _src('scripts', 'monitor', 'disco_job.sh')
    assert re.search(r'^quick\(\)\{ timeout -k \d+ \d+ discobox shell', job, re.M)


def test_the_refusal_message_names_the_size_once(tmp_path):
    out, calls, log = _collect(tmp_path, 10 << 30)
    assert log.count(str(10 << 30)) == 1, log
    out, calls, log = _collect(tmp_path / 'u' if (tmp_path / 'u').mkdir() is None else None, 'abc')
    assert 'size unreadable' in log and 'abc' not in log, log
