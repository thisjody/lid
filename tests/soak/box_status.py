"""Run ON the build box by soak.py (piped to `python3 -I -`): one JSON object with the box-side truth for every job whose
name starts with a prefix, and every live Discobox box mapped to its job.

  python3 -I - <prefix> [discobox-exe]     (stdin: this file)

For each job ~/scratch/<job>/ (prefix match):
  done        the `== done` line, or null if there is none yet
  done_at     the run.log mtime (box epoch seconds) once done, else null: the box's own finish time, never the desk's
  verdict     PASS | FAIL | null       (a done line with FAIL in it is FAIL)
  never_forgive  true if the log or audit records a LEAK, a VIOLATION, an unrecorded box, or a fan audit FAIL/INCOMPLETE
  cause       the reason on the last `== <stage> exit N (reason)` line, or null
  worker_gone    true if job_sweep closed it (the worker died: a reboot or a kill)
  stages      {stage: 'HH:MM:SS'} from `== <stage> HH:MM:SS` lines (box-create latency comes from these)
  cred        for a job with out/audit.json: {readable, swapped (exchanges whose swappedSecretIds is non-empty), hosts}
live: [{id, job}] for every box not archived/deleted; job is the owning job (by source path or displayName) or null;
      null if the listing could not be read or has no sandboxes list.
host: {load1, mem_used_mb, mem_total_mb} from /proc, or null.
"""
import json
import os
import re
import subprocess
import sys

DONE = re.compile(r'^(== done.*)$', re.M)
STAGE = re.compile(r'^== (\w+) (\d\d:\d\d:\d\d)$', re.M)
NEVER = re.compile(r'LEAK|VIOLATION|unrecorded|-- audit \S+: (?:FAIL|INCOMPLETE)')
CAUSE = re.compile(r'^== \w+ exit \d+ \((.*)\)$', re.M)
PORT = re.compile(r':\d+$')
GONE = ('archived', 'deleted')


def cred(path):
    try:
        ex = json.load(open(path)).get('exchanges')
    except (OSError, ValueError, AttributeError):
        return {'readable': False, 'swapped': 0, 'hosts': []}
    if not isinstance(ex, list):
        return {'readable': False, 'swapped': 0, 'hosts': []}
    sw = [e for e in ex if isinstance(e, dict) and e.get('swappedSecretIds')]
    return {'readable': True, 'swapped': len(sw), 'hosts': sorted({PORT.sub('', str(e.get('host'))) for e in sw})}


def job(d):
    log = open(os.path.join(d, 'logs', 'run.log'), errors='replace').read()
    m = DONE.findall(log)
    done = m[-1] if m else None
    audit_txt = os.path.join(d, 'out', 'audit.txt')
    extra = open(audit_txt, errors='replace').read() if os.path.isfile(audit_txt) else ''
    aj = os.path.join(d, 'out', 'audit.json')
    return {
        'done': done,
        'done_at': os.path.getmtime(os.path.join(d, 'logs', 'run.log')) if done else None,
        'verdict': None if done is None else ('FAIL' if 'FAIL' in done else 'PASS'),
        'never_forgive': bool(NEVER.search(log) or NEVER.search(extra)),
        'cause': (CAUSE.findall(log) or [None])[-1],
        'worker_gone': 'closed by job_sweep' in log,
        'stages': {k: v for k, v in STAGE.findall(log) if k != 'done'},
        'cred': cred(aj) if os.path.exists(aj) else None,
    }


def owner(b, jobs, scratch):
    ld = (((b.get('config') or {}).get('source') or {}).get('localDirectory') or '')
    name = b.get('displayName') or b.get('name') or ''
    for j in jobs:
        if ld.startswith(os.path.join(scratch, j) + '/') or re.fullmatch(re.escape(j) + r'-\d{6}-[0-9a-f]{6}-\d+', name):
            return j
    return None


def status(scratch, prefix, listing):
    names = sorted(n for n in (os.listdir(scratch) if os.path.isdir(scratch) else [])
                   if n.startswith(prefix) and os.path.isfile(os.path.join(scratch, n, 'logs', 'run.log')))
    jobs = {n: job(os.path.join(scratch, n)) for n in names}
    live = None
    if isinstance(listing, dict) and 'sandboxes' in listing and isinstance(listing['sandboxes'], (list, type(None))):
        live = [{'id': b.get('id'), 'job': owner(b, jobs, scratch)}
                for b in (listing.get('sandboxes') or []) if ((b.get('runtime') or {}).get('state')) not in GONE]
    return {'jobs': jobs, 'live': live}


def host(proc='/proc'):
    try:
        load1 = float(open(os.path.join(proc, 'loadavg')).read().split()[0])
        mem = dict(l.split(':', 1) for l in open(os.path.join(proc, 'meminfo')) if ':' in l)
        kb = lambda k: int(mem[k].split()[0])
        return {'load1': load1, 'mem_used_mb': (kb('MemTotal') - kb('MemAvailable')) // 1024, 'mem_total_mb': kb('MemTotal') // 1024}
    except (OSError, ValueError, KeyError, IndexError):
        return None


def listing(exe):
    try:
        r = subprocess.run([exe, '--auto-start-server=false', 'ls', '-o', 'json'], capture_output=True, text=True, timeout=300)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def main(argv):
    home = os.path.expanduser('~')
    exe = argv[2] if len(argv) > 2 else os.path.join(home, '.local', 'bin', 'discobox')
    s = status(os.path.join(home, 'scratch'), argv[1], listing(exe))
    s['host'] = host()
    print(json.dumps(s))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
