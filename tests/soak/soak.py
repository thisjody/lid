"""Chaos soak and load to saturation, driven from the desk through lid_run.sh, disco_job.sh and
disco_fan.sh, judged from the box's own records.

  python3 tests/soak/soak.py soak  <out-dir> --hours 3 --prompt <file> --fan-repo <dir> --fan-cmd <script>
  python3 tests/soak/soak.py load  <out-dir> --prompt <file> --fan-repo <dir> --fan-cmd <script>
  python3 tests/soak/soak.py judge <out-dir>

Before the first job, <out-dir>/criteria.json records the hours, the chaos plan and the sha256 of verdict.py, soak.py and
box_status.py; `judge` refuses (CRITERION CHANGED) if any of them changed since. Every record goes to records.json as it
happens, and a rerun with the same <out-dir> resumes: jobs in flight are polled again and decoys stay spaced.

The truth is the box's: each poll runs box_status.py on the box (one ssh round trip) for every soak job's done line, its
box-side finish time, the LEAK/VIOLATION markers, the job_sweep close of a dead worker, the audit's swapped exchanges,
and every live box mapped to its job. Each chaos event is recorded only with the post-condition that proves it happened.
The verdict is verdict.verdict (soak) or verdict.load_verdict (load).
"""
import argparse
import base64
import concurrent.futures as cf
import hashlib
import math
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
DISCO_JOB = os.path.join(ROOT, 'scripts', 'monitor', 'disco_job.sh')
C5_TRIES = 5
# After gcloud's ssh child appears, measured on the desk 11/18: the remote command starts ~1.4 s later and the launch ends
# ~0.8 s after that. The aim only; where the kill landed is read from the box.
C5_KILL_AFTER_S = (1.2, 2.4)
LID = os.path.join(ROOT, 'scripts', 'lid_run.sh')
try:
    from .verdict import load_verdict, verdict
except ImportError:
    sys.path.insert(0, HERE)
    from verdict import load_verdict, verdict  # noqa: E402

CHAOS_PLAN = [  # (offset s from start, id, kind, recovery window s); spaced so each leaves time for a PASS of each kind after it
    (10 * 60, 'c1', 'kill-discobox-server', 1200),   # a job on a dead box fails within 5 min of unreachable + its cleanup
    (45 * 60, 'c2', 'restart-thv-github', 180),
    (65 * 60, 'c3', 'kill-agentgateway', 120),
    (85 * 60, 'c4', 'reboot', 900),
    (115 * 60, 'c5', 'drop-iap', 120),
]
IN_FLIGHT = 3
POLL_S = 30
DECOY_EVERY_S = 3600
LAUNCH_CUTOFF_S = 1800
DRAIN_S = 2400
FINAL_READ_S = 600
MAXBOX = 6


def _max_boxes():
    """The pool's measured box ceiling (stack/stack.yaml pool.max_boxes): the load test fans to it."""
    sys.path.insert(0, os.path.join(ROOT, 'scripts', 'lid'))
    from stack_site import load as load_stack
    m = load_stack(os.path.join(ROOT, 'stack', 'stack.yaml'))
    return int([c for c in m['components'] if c['name'] == 'discobox'][0]['pool']['max_boxes'])


FAN_CAP = _max_boxes()

CHAOS_CMD = {
    'kill-discobox-server':
        'p=$(pgrep -u "$(id -un)" -f "[d]iscobox-server$" | head -1); [ -n "$p" ] || { echo "NOPID"; exit 0; }; '
        'kill -9 "$p"; sleep 3; if kill -0 "$p" 2>/dev/null; then echo "ALIVE $p"; else echo "KILLED $p"; fi',
    'restart-thv-github':
        'export XDG_RUNTIME_DIR=/run/user/$(id -u); u=thv-github.service; '
        'b=$(systemctl --user show -p ActiveEnterTimestampMonotonic --value $u); systemctl --user restart $u; sleep 5; '
        'a=$(systemctl --user show -p ActiveEnterTimestampMonotonic --value $u); '
        'if systemctl --user is-active -q $u && [ "$a" != "$b" ]; then echo "RESTARTED $b $a"; else echo "NOT RESTARTED $b $a"; fi; '
        'ip=$(hostname -I | cut -d" " -f1); ok=; for i in $(seq 1 12); do '
        'python3 -I ~/stack/repo/scripts/lid/mcp_probe.py --direct "http://$ip:18800/mcp" list > /tmp/soak_gw.txt 2>&1 '
        '&& grep -q "^time_" /tmp/soak_gw.txt && grep -q "^github_" /tmp/soak_gw.txt && { ok=1; break; }; sleep 10; done; '
        '[ -n "$ok" ] || echo "GATEWAY DOWN after the restart"',
    'kill-agentgateway':
        'p=$(pgrep -f "[/]usr/local/bin/agentgateway" | head -1); [ -n "$p" ] || { echo "NOPID"; exit 0; }; '
        'sudo -n kill -9 "$p"; sleep 15; q=$(pgrep -f "[/]usr/local/bin/agentgateway" | head -1); '
        'if [ -n "$q" ] && [ "$q" != "$p" ]; then echo "RESTARTED $p $q"; else echo "NOT RESTARTED $p ${q:-none}"; fi; '
        'ip=$(hostname -I | cut -d" " -f1); ok=; for i in $(seq 1 12); do '
        'python3 -I ~/stack/repo/scripts/lid/mcp_probe.py --direct "http://$ip:18800/mcp" list > /tmp/soak_gw.txt 2>&1 '
        '&& grep -q "^time_" /tmp/soak_gw.txt && grep -q "^github_" /tmp/soak_gw.txt && { ok=1; break; }; sleep 10; done; '
        '[ -n "$ok" ] || echo "GATEWAY DOWN after the restart"',
}
PROOF_WORD = {'kill-discobox-server': 'KILLED', 'restart-thv-github': 'RESTARTED', 'kill-agentgateway': 'RESTARTED'}


def sha256(p):
    return hashlib.sha256(open(p, 'rb').read()).hexdigest()


def lid(cmd, timeout=600):
    try:
        r = subprocess.run(['bash', LID, cmd], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, ''
    return r.returncode, r.stdout


def _lid_full(cmd, timeout=600):
    """lid() with stderr kept, for a reason when a poll fails."""
    try:
        r = subprocess.run(['bash', LID, cmd], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, '', f'timed out after {timeout} s'
    return r.returncode, r.stdout, r.stderr


STATUS_WHY = {'why': None, 'retried': None, 'read_at': None}   # why the last status() was unreadable, what its retry
# recovered from, and when the listing it returned was read


def status(prefix):
    """The box-side truth, or None. One failed poll (an IAP or ssh hiccup) is retried once after 10 s; a poll that stays
    unreadable is None, its reason in STATUS_WHY (soak #2 on 6ffb7d4: 1 of 261 polls, no reason kept)."""
    b = base64.b64encode(open(os.path.join(HERE, 'box_status.py'), 'rb').read()).decode()
    why, STATUS_WHY['retried'], STATUS_WHY['read_at'] = None, None, None
    for attempt in range(2):
        rc, out, err = _lid_full(f'echo {b} | base64 -d | python3 -I - {prefix}', timeout=300)
        read_at = time.time()
        if rc == 0:
            for line in reversed(out.strip().splitlines()):
                if line.startswith('{'):
                    try:
                        d = json.loads(line)
                    except ValueError:
                        why = 'rc 0 but an unparseable JSON status line'
                        break
                    STATUS_WHY['why'], STATUS_WHY['retried'], STATUS_WHY['read_at'] = None, why, read_at
                    return d
            else:
                why = 'rc 0 but no JSON status line'
        else:
            tail = [l for l in (err or out or '').strip().splitlines() if l.strip()][-1:]
            why = f"rc {rc}: {tail[0][:200] if tail else 'no output'}"
        if attempt == 0:
            time.sleep(10)
    STATUS_WHY['why'] = why
    return None


def launch(kind, jid, a, shards=None):
    if kind == 'claude':
        cmd = ['bash', os.path.join(ROOT, 'scripts/monitor/disco_job.sh'), jid, 'https://github.com/octocat/Hello-World', 'master',
               a.prompt, '--timeout-min', '25']
    else:
        sha = subprocess.check_output(['git', '-C', a.fan_repo, 'rev-parse', 'HEAD'], text=True).strip()
        cmd = ['bash', os.path.join(ROOT, 'scripts/monitor/disco_fan.sh'), jid, a.fan_repo, sha, a.fan_cmd,
               '--shards', str(shards or 2), '--timeout-min', '15']
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=900).returncode
    except subprocess.TimeoutExpired:
        return 124


def decoy():
    try:
        r = subprocess.run(['bash', os.path.join(ROOT, 'scripts/lid/decoy_proof.sh')], capture_output=True, text=True, timeout=1200)
    except subprocess.TimeoutExpired:
        return 'FAIL'
    return 'PASS' if (r.returncode == 0 and '\nPASS' in r.stdout) else 'FAIL'


def _boot_id():
    rc, out = lid('cat /proc/sys/kernel/random/boot_id', timeout=120)
    lines = out.strip().splitlines()
    return lines[-1].strip() if rc == 0 and lines else None


def chaos(cid, kind, jid=None, a=None):
    """Inject one fault and prove it happened. Returns {ok, proof, at, back} (+ rc for drop-iap: the killed launch's).
    c2/c3 also prove the gateway serves both targets again; c5 kills a REAL job launch (jid) mid-flight."""
    at = time.time()
    if kind in CHAOS_CMD:
        rc, out = lid(CHAOS_CMD[kind], timeout=360)
        lines = [l for l in out.strip().splitlines() if l.strip()]
        proof = next((l for l in lines if l.startswith(('KILLED', 'RESTARTED', 'NOT ', 'ALIVE', 'NOPID'))), '')
        down = any(l.startswith('GATEWAY DOWN') for l in lines)
        return {'ok': rc == 0 and proof.startswith(PROOF_WORD[kind]) and not down,
                'proof': f"rc {rc}: {proof[:100]}{'; GATEWAY DOWN' if down else ''}", 'at': at, 'back': time.time()}
    if kind == 'reboot':
        before = _boot_id()
        if not before:
            return {'ok': False, 'proof': 'boot_id unreadable before the reboot', 'at': at, 'back': time.time()}
        lid('(sleep 2; sudo -n systemctl reboot) >/dev/null 2>&1 &', timeout=120)
        t_end = time.time() + 900
        while time.time() < t_end:
            time.sleep(20)
            now = _boot_id()
            if now and now != before:
                return {'ok': True, 'proof': f'boot_id {before[:8]} -> {now[:8]}', 'at': at, 'back': time.time()}
        return {'ok': False, 'proof': f'boot_id still {before[:8]} or unreadable after 900 s', 'at': at, 'back': time.time()}
    if kind == 'drop-iap':
        # Kill a real launch's one box call (gcloud compute ssh over IAP) mid-flight, then read from the box where the kill
        # landed: an ssh process on the desk does not mean the tunnel is up (its ProxyCommand still has to connect), so the
        # desk's timing is an aim, never the proof. c5 is proven only when the launch had reached the box (its job dir exists)
        # and the next call works. The command has no pty, so a killed client does not stop it on the box: once it started it
        # usually completes and its worker runs ('worker': a real job, judged like any other, not excused). 'mid' (a job dir
        # and no worker) is the rarer cut; that job is excused for not running. A kill before the box, or a launch that
        # finished first (a real job, `missed`), means another try under the next name.
        missed, notes = [], []
        for k in range(1, C5_TRIES + 1):
            name = f'{jid}-{k}'
            p = subprocess.Popen(['bash', DISCO_JOB, name, 'https://github.com/octocat/Hello-World', 'master', a.prompt,
                                  '--timeout-min', '25'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            in_flight, t_end = False, time.time() + 60
            while time.time() < t_end and p.poll() is None:
                if subprocess.run(['pgrep', '-g', str(p.pid), '-x', 'ssh'], capture_output=True).returncode == 0:
                    in_flight = True
                    break
                time.sleep(0.1)
            if in_flight:
                time.sleep(random.uniform(*C5_KILL_AFTER_S))
                in_flight = p.poll() is None   # the launch may have finished in that moment: then it beat the kill
            if not in_flight:
                if p.poll() is None:   # no ssh within 60 s
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait()
                    notes.append(f'try {k}: no ssh within 60 s')
                elif p.returncode == 0:
                    missed.append(name)
                    notes.append(f'try {k}: the launch finished first (a real job)')
                else:
                    notes.append(f'try {k}: the launch failed on its own, rc {p.returncode}')
                continue
            os.killpg(p.pid, signal.SIGKILL)
            rc = p.wait()
            back = None
            for _ in range(6):
                if lid('true', timeout=60)[0] == 0:
                    back = time.time()
                    break
                time.sleep(20)
            for _ in range(3):   # one failed query is a retry, not an unproven c5
                q, where = lid(f'W=~/scratch/{name}; if [ -e $W/logs/worker.out ]; then echo worker; elif [ -d $W ]; then echo mid; '
                               f'else echo none; fi', timeout=120)
                if q == 0 and where.strip():
                    break
                time.sleep(10)
            where = where.strip().splitlines()[-1] if q == 0 and where.strip() else 'unreadable'
            if where == 'none':
                notes.append(f'try {k}: killed before the box')
                continue
            ok = rc != 0 and back is not None and where in ('mid', 'worker')
            notes.append(f'try {k}: killed with the launch on the box ({where}): reached the box, rc {rc}; next call ok: {back is not None}')
            return {'ok': ok, 'proof': '; '.join(notes), 'at': at, 'back': back or time.time(), 'rc': rc, 'job': name,
                    'missed': missed, 'job_ran': where == 'worker'}
        notes.append(f'c5 never hit a launch on the box in {C5_TRIES} tries')
        return {'ok': False, 'proof': '; '.join(notes), 'at': at, 'back': time.time(), 'rc': None, 'job': f'{jid}-{C5_TRIES}',
                'missed': missed, 'job_ran': False}
    return {'ok': False, 'proof': f'unknown chaos kind {kind}', 'at': at, 'back': at}


class Run:
    def __init__(self, out):
        self.out = out
        os.makedirs(out, exist_ok=True)
        self.path = os.path.join(out, 'records.json')
        self.lock = threading.RLock()
        self.rec = json.load(open(self.path)) if os.path.exists(self.path) else {
            'jobs': [], 'chaos': [], 'leaks': [], 'decoys': [], 'window': {}, 'load': [], 'host': []}

    def save(self):
        with self.lock:
            tmp = f'{self.path}.{os.getpid()}.{threading.get_ident()}.tmp'
            json.dump(self.rec, open(tmp, 'w'), indent=1)
            os.replace(tmp, self.path)


def repo_state():
    """The sha under test: the repo's HEAD, whether tracked files are dirty, and whether the box runs this tree's scripts/lid."""
    sha = subprocess.run(['git', '-C', ROOT, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(['git', '-C', ROOT, 'status', '--porcelain', '--untracked-files=no'], capture_output=True, text=True).stdout.strip())
    names = sorted(f for f in os.listdir(os.path.join(ROOT, 'scripts', 'lid')) if f.endswith(('.py', '.sh', '.service', '.timer')))
    local = {n: sha256(os.path.join(ROOT, 'scripts', 'lid', n)) for n in names}
    rc, out = lid('cd ~/stack/repo/scripts/lid && sha256sum ' + ' '.join(names), timeout=120)
    box = {l.split()[1]: l.split()[0] for l in out.splitlines() if len(l.split()) == 2}
    return {'sha': sha, 'dirty': dirty, 'deployed_ok': rc == 0 and box == local}


def ready(a):
    st = repo_state()
    if st['dirty'] or not st['deployed_ok']:
        print(f"refuse: the tree is {'dirty' if st['dirty'] else 'clean'} and the box runs "
              f"{'this' if st['deployed_ok'] else 'a different'} scripts/lid (run stack_up.sh from this sha first)")
        return None
    return st


def write_criteria(a, extra):
    crit = os.path.join(a.out, 'criteria.json')
    if not os.path.exists(crit):
        d = {'verdict_py_sha256': sha256(os.path.join(HERE, 'verdict.py')), 'soak_py_sha256': sha256(os.path.abspath(__file__)),
             'box_status_py_sha256': sha256(os.path.join(HERE, 'box_status.py')), 'written': time.time()}
        d.update(extra)
        json.dump(d, open(crit, 'w'), indent=1)
    return json.load(open(crit))


FIELDS = ('done', 'done_at', 'verdict', 'never_forgive', 'worker_gone', 'stages', 'cred', 'cause')


def absorb(rec, st):
    """Copy the box-side truth into the job records; return the ids still in flight."""
    jobs = st['jobs'] if st else {}
    for j in rec['jobs']:
        b = jobs.get(j['id'])
        if b:
            j.update({k: b.get(k) for k in FIELDS})
    return [j['id'] for j in rec['jobs'] if j.get('verdict') is None and j.get('launch_rc') in (0, None)]


def poll(r, prefix):
    t0 = time.time()   # an unreadable poll is stamped when it started; a sighting when its listing was read (a retry can
    # read it minutes later, and the leak span is measured between sightings)
    STATUS_WHY.update(why=None, retried=None, read_at=None)   # never a previous poll's
    st = status(prefix)
    pending = absorb(r.rec, st)
    on_box = {jid for jid, b in (st['jobs'] if st else {}).items() if b.get('done') is None}
    leak = {'at': (STATUS_WHY.get('read_at') or t0) if st else t0, 'live': st['live'] if st else None,
            'pending': sorted(set(pending) | on_box)}
    if st is None:
        leak['why'] = STATUS_WHY.get('why')
    elif STATUS_WHY.get('retried'):
        leak['retried'] = STATUS_WHY['retried']
    r.rec['leaks'].append(leak)
    if st and st.get('host'):
        r.rec['host'].append(dict(st['host'], at=time.time()))
    r.save()
    return st, pending


def soak(a):
    st = ready(a)
    if st is None:
        return 2
    r = Run(a.out)
    crit = write_criteria(a, {'mode': 'soak', 'repo_sha': st['sha'], 'hours': a.hours, 'chaos': [c[1] for c in CHAOS_PLAN],
                              'chaos_plan': [list(c) for c in CHAOS_PLAN], 'decoy_every_s': DECOY_EVERY_S})
    start = r.rec['window'].get('start') or time.time()
    end = start + float(crit['hours']) * 3600
    r.rec['window'] = {'start': start, 'end': None}
    r.save()
    prefix = f'soak-{int(start)}-'
    done_chaos = {c['id'] for c in r.rec['chaos']}
    # A run cut off during c5 left its pre-record and maybe launches on the box under try names it never recorded: it is
    # started again in a new out-dir, never resumed.
    if any(j['id'] == f'{prefix}{cid}' for j in r.rec['jobs'] for _, cid, kind, _ in CHAOS_PLAN if kind == 'drop-iap' and cid not in done_chaos):
        print('refuse: this soak was interrupted during c5 (drop-iap); start a new run in a new out-dir')
        return 2
    last_decoy = max((d['at'] for d in r.rec['decoys']), default=None)
    next_decoy = start if last_decoy is None else last_decoy + DECOY_EVERY_S
    n = len(r.rec['jobs'])
    while time.time() < end:
        for off, cid, kind, rec_s in CHAOS_PLAN:
            if cid not in done_chaos and time.time() >= start + off:
                jid = None
                if kind == 'drop-iap':
                    jid = f'{prefix}{cid}'
                    r.rec['jobs'].append({'id': jid, 'kind': 'claude', 'launched': time.time(), 'launch_rc': None, 'done_at': None, 'verdict': None})
                    r.save()
                c = chaos(cid, kind, jid=jid, a=a)
                until = max(c['at'] + rec_s, c['back'] + 600) if kind == 'reboot' else c['at'] + rec_s
                rec = {'id': cid, 'kind': kind, 'at': c['at'], 'until': until, 'ok': c['ok'], 'proof': c['proof']}
                if jid:
                    rec['job'] = c.get('job', jid)
                    jr = next(j for j in r.rec['jobs'] if j['id'] == jid)
                    jr['id'], jr['launch_rc'] = rec['job'], (0 if c.get('job_ran') else c.get('rc'))   # a started worker: a real job
                    for m in c.get('missed') or []:   # launches that beat the kill reached the box: real jobs, judged as such
                        r.rec['jobs'].append({'id': m, 'kind': 'claude', 'launched': c['at'], 'launch_rc': 0, 'done_at': None, 'verdict': None})
                r.rec['chaos'].append(rec)
                done_chaos.add(cid)
                r.save()
        if time.time() >= next_decoy:
            r.rec['decoys'].append({'at': time.time(), 'result': decoy()})
            next_decoy = time.time() + DECOY_EVERY_S
            r.save()
        _, pending = poll(r, prefix)
        while len(pending) < IN_FLIGHT and time.time() < end - LAUNCH_CUTOFF_S:
            n += 1
            kind = 'claude' if n % 2 else 'fan'
            jid = f'{prefix}{n}'
            r.rec['jobs'].append({'id': jid, 'kind': kind, 'launched': time.time(), 'launch_rc': None, 'done_at': None, 'verdict': None})
            r.save()
            rc = launch(kind, jid, a)
            next(j for j in r.rec['jobs'] if j['id'] == jid)['launch_rc'] = rc
            r.save()
            if rc == 0:
                pending.append(jid)
        time.sleep(POLL_S)
    t_end = time.time() + DRAIN_S
    while time.time() < t_end:
        _, pending = poll(r, prefix)
        if not pending:
            break
        time.sleep(POLL_S)
    t_end = time.time() + FINAL_READ_S
    while True:
        st, _ = poll(r, prefix)
        if st is not None or time.time() >= t_end:
            break
        time.sleep(POLL_S)
    r.rec['decoys'].append({'at': time.time(), 'result': decoy()})
    r.rec['window']['end'] = time.time()
    r.save()
    return judge(a)


def _secs(hms):
    h, m, s = (int(x) for x in hms.split(':'))
    return h * 3600 + m * 60 + s


def create_latency(j):
    st = j.get('stages') or {}
    for a, b in (('box', 'setup'), ('boxes', 'probe')):
        if a in st and b in st:
            return (_secs(st[b]) - _secs(st[a])) % 86400
    return None


def pct(xs, q):
    """Nearest-rank percentile."""
    xs = sorted(xs)
    return xs[min(len(xs), max(1, math.ceil(q * len(xs)))) - 1] if xs else None


def run_step(r, a, prefix, name, jobs, limit=None, timeout_s=3600):
    """Launch jobs [(kind, shards)]: all at once (limit None) or keeping `limit` in flight. Wait for all; sample the host."""
    ids, queue, t0, host = [], list(enumerate(jobs)), time.time(), []
    def go(i, kind, shards):
        jid = f'{prefix}{name}-{i}'
        r.rec['jobs'].append({'id': jid, 'kind': kind, 'step': name, 'launched': time.time(), 'launch_rc': None, 'done_at': None, 'verdict': None})
        r.save()
        return jid, launch(kind, jid, a, shards=shards)
    while True:
        st = status(prefix)
        absorb(r.rec, st)
        if st and st.get('host'):
            host.append(st['host'])
        live = st['live'] if st else None
        r.rec['leaks'].append({'at': time.time(), 'live': live, 'pending': []})
        mine = [j for j in r.rec['jobs'] if j['id'] in ids]
        in_flight = [j for j in mine if j.get('verdict') is None and j.get('launch_rc') in (0, None)]
        if queue:
            take = len(queue) if limit is None else max(0, limit - len(in_flight))
            batch, queue = queue[:take], queue[take:]
            if batch:
                with cf.ThreadPoolExecutor(max_workers=len(batch)) as ex:
                    for jid, rc in ex.map(lambda x: go(x[0], *x[1]), batch):
                        ids.append(jid)
                        next(j for j in r.rec['jobs'] if j['id'] == jid)['launch_rc'] = rc
                r.save()
                continue
        elif not in_flight or time.time() > t0 + timeout_s:
            break
        r.save()
        time.sleep(20)
    mine = [j for j in r.rec['jobs'] if j['id'] in ids]
    lat = [x for x in (create_latency(j) for j in mine) if x is not None]
    walls = [j['done_at'] - j['launched'] for j in mine if j.get('done_at')]
    refused = [j for j in mine if j.get('verdict') == 'FAIL' and 'boxes already live' in (j.get('cause') or '')]
    boxed = [j for j in mine if {'setup', 'probe'} & set(j.get('stages') or {})]   # the box was made and answered
    lives = [len([b for b in (l['live'] or []) if b.get('job') in ids]) for l in r.rec['leaks'] if l['at'] >= t0]
    s = {'step': name, 'jobs': len(mine), 'boxed': len(boxed), 'refused': len(refused),
         'failed': len([j for j in mine if j.get('verdict') != 'PASS']) - len(refused),
         'create_p50_s': pct(lat, 0.5), 'create_p99_s': pct(lat, 0.99), 'create_max_s': max(lat) if lat else None,
         'wall_max_s': max(walls) if walls else None, 'wall_s': time.time() - t0,
         'load1_max': max((h['load1'] for h in host), default=None),
         'mem_used_max_mb': max((h['mem_used_mb'] for h in host), default=None),
         'mem_total_mb': max((h['mem_total_mb'] for h in host), default=None), 'live_max': max(lives, default=0)}
    r.rec['load'].append(s)
    r.save()
    print(json.dumps(s))
    return s


def load(a):
    if os.path.exists(os.path.join(a.out, 'records.json')):
        print('refuse: a load run is not resumable (its steps measure concurrency from a clean start); use a new out-dir')
        return 2
    st = ready(a)
    if st is None:
        return 2
    r = Run(a.out)
    write_criteria(a, {'mode': 'load', 'repo_sha': st['sha'], 'maxbox': MAXBOX, 'fan_cap': FAN_CAP})
    start = r.rec['window'].get('start') or time.time()
    r.rec['window'] = {'start': start, 'end': None}
    prefix = f'load-{int(start)}-'
    r.rec['load'] = []
    for n in (1, 2, 4, 6):
        run_step(r, a, prefix, str(n), [('claude', None)] * n)
    run_step(r, a, prefix, f'over-cap-{MAXBOX + 1}', [('claude', None)] * (MAXBOX + 1))
    run_step(r, a, prefix, 'backlog-12', [('claude', None)] * 12, limit=MAXBOX, timeout_s=3 * 3600)
    run_step(r, a, prefix, f'fan-{FAN_CAP}', [('fan', FAN_CAP)])
    t_end = time.time() + FINAL_READ_S
    while True:
        st = status(prefix)
        r.rec['leaks'].append({'at': time.time(), 'live': st['live'] if st else None, 'pending': []})
        if st is not None or time.time() >= t_end:
            break
        time.sleep(POLL_S)
    absorb(r.rec, st)
    r.rec['window']['end'] = time.time()
    r.save()
    json.dump({'steps': r.rec['load']}, open(os.path.join(a.out, 'summary.json'), 'w'), indent=1)
    return judge(a)


def judge(a):
    crit = json.load(open(os.path.join(a.out, 'criteria.json')))
    for key, path in (('verdict_py_sha256', os.path.join(HERE, 'verdict.py')), ('soak_py_sha256', os.path.abspath(__file__)),
                      ('box_status_py_sha256', os.path.join(HERE, 'box_status.py'))):
        if crit.get(key) != sha256(path):
            print(f'CRITERION CHANGED: {os.path.basename(path)} differs from the one hashed before the run')
            return 2
    if crit.get('mode') == 'soak' and crit.get('chaos_plan') != [list(c) for c in CHAOS_PLAN]:
        print('CRITERION CHANGED: the chaos plan in criteria.json is not the one in soak.py')
        return 2
    rec = json.load(open(os.path.join(a.out, 'records.json')))
    v, fails = (load_verdict(rec, crit) if crit.get('mode') == 'load' else verdict(rec, crit))
    json.dump({'verdict': v, 'fails': fails}, open(os.path.join(a.out, 'verdict.json'), 'w'), indent=1)
    print(v, *fails, sep='\n  ')
    return 0 if v == 'PASS' else 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument('mode', choices=['soak', 'load', 'judge'])
    p.add_argument('out')
    p.add_argument('--hours', type=float, default=3)
    p.add_argument('--prompt')
    p.add_argument('--fan-repo')
    p.add_argument('--fan-cmd')
    a = p.parse_args()
    return {'soak': soak, 'load': load, 'judge': judge}[a.mode](a)


if __name__ == '__main__':
    sys.exit(main())
