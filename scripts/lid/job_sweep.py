"""Close jobs whose worker died without writing `== done` (a host reboot, an OOM kill, a SIGKILL), on the build box.

A job is ~/scratch/<job>/ with job/worker.sh and logs/run.log (disco_job.sh and disco_fan.sh both lay it out so). It is an
orphan when its log has no `== done` line, no running process has its job/worker.sh on the command line, and the log has
not been written for MIN_AGE_S (a job just launched may not have its worker yet). For each orphan this:
  - stops the box's agent, then audits each live box when the job shipped an audit.py (Claude jobs), into out/audit.txt;
    an audit that cannot be read is logged as a VIOLATION (as the worker itself does);
  - archives each live box and confirms it (RM_TRIES tries); a box that will not archive is logged as LEAK;
  - closes the log: `== <stage> exit 99 (worker gone ...)`, then `== done FAILED at <stage> (worker gone) HH:MM:SS`
    (Claude jobs) or `== done FAIL (worker gone)` (fan jobs), the forms boxwatch and the soak read.
A job that wrote `== done` but still owns a live box (its own cleanup failed) has the box archived and a LEAK line added.
It never closes a log on a guess: if the box listing cannot be read (or has no sandboxes list), it stops before touching
anything. A failure while closing one job (an rm timeout) is logged in that job, its unconfirmed boxes as LEAK, and the
sweep goes on to the next.
Run by job-sweep.timer (a user unit of the box user) at boot and every 5 minutes; stack_up.sh installs it.
"""
import json
import os
import re
import subprocess
import sys
import time

MIN_AGE_S = 120
RM_TRIES = 6
DONE = re.compile(r'^== done', re.M)
LOG_BOX = re.compile(r'^-- (?:shard \d+ )?box (sbx_\w+)', re.M)
STAGE = re.compile(r'^== (\w+) \d\d:\d\d:\d\d$', re.M)
GONE = ('archived', 'deleted', 'gone')


def processes(proc='/proc'):
    out = []
    for p in sorted(os.listdir(proc)):
        if not p.isdigit():
            continue
        try:
            raw = open(os.path.join(proc, p, 'cmdline'), 'rb').read()
        except OSError:
            continue
        if raw:
            out.append(raw.replace(b'\0', b' ').decode('utf-8', 'replace').strip())
    return out


def orphans(scratch, procs, now, unreadable=None):
    found = []
    for name in sorted(os.listdir(scratch)):
        d = os.path.join(scratch, name)
        worker, log = os.path.join(d, 'job', 'worker.sh'), os.path.join(d, 'logs', 'run.log')
        if not (os.path.isfile(worker) and os.path.isfile(log)):
            continue
        try:
            if DONE.search(open(log, errors='replace').read()):
                continue
        except OSError:
            if unreadable is not None:
                unreadable.append(name)   # never closed on a guess, but reported every sweep
            continue
        if now - os.path.getmtime(log) < MIN_AGE_S:
            continue
        if any(worker in p.split() for p in procs):
            continue
        found.append(name)
    return found


def _state(b):
    return (b.get('runtime') or {}).get('state')


def fan_box(job, name):
    """A fan box is named exactly <job>-<HHMMSS>-<6 hex>-<shard> (disco_fan_lib/worker.sh); a prefix match would also
    claim the boxes of a job whose name starts with this one's (`mut` and `mut-2`)."""
    return re.fullmatch(re.escape(job) + r'-\d{6}-[0-9a-f]{6}-\d+', name or '') is not None


def boxes_of(job, jobdir, log, listing):
    boxes = listing.get('sandboxes') or []
    state = {b.get('id'): _state(b) for b in boxes}
    ids = {i for i in LOG_BOX.findall(log) if state.get(i) not in GONE}
    for b in boxes:
        if _state(b) in GONE:
            continue
        ld = (((b.get('config') or {}).get('source') or {}).get('localDirectory') or '')
        if ld.startswith(jobdir + '/') or fan_box(job, b.get('displayName') or b.get('name')):
            ids.add(b.get('id'))
    return sorted(ids)


def close_lines(log, fan, leaked, t):
    starts = STAGE.findall(log)
    stage = starts[-1] if starts else 'unknown'
    lines = [f'-- box {i} NOT removed: LEAK' for i in leaked]
    lines.append(f'== {stage} exit 99 (worker gone: host restart or kill; closed by job_sweep)')
    lines.append('== done FAIL (worker gone)' if fan else f'== done FAILED at {stage} (worker gone) {t}')
    return lines


def _violations(i, text):
    """Carry each VIOLATION line of the audit into the job's log (a truncated audit also exits 1: Discobox.audit returns its
    text then, so the real cause is the one recorded)."""
    return [f'VIOLATION in the audit of {i} (job_sweep): {l[len("VIOLATION"):].strip()}'
            for l in (text or '').splitlines() if l.startswith('VIOLATION')]


def _listing(db):
    d = db.ls()
    if not (isinstance(d, dict) and 'sandboxes' in d and isinstance(d['sandboxes'], (list, type(None)))):
        raise RuntimeError('the box listing has no sandboxes list')
    return d


def _archived(db, i):
    st = next((_state(b) for b in _listing(db).get('sandboxes') or [] if b.get('id') == i), 'gone')
    return st in GONE


def _close(scratch, job, listing, db, sleep, t):
    jobdir = os.path.join(scratch, job)
    logp = os.path.join(jobdir, 'logs', 'run.log')
    try:
        log = open(logp, errors='replace').read()
    except OSError:
        return {'boxes': [], 'leaked': [], 'error': 'run.log unreadable: not closed and not checked'}
    conf = os.path.join(jobdir, 'job', 'job.conf')
    try:
        fan = os.path.isfile(conf) and 'SHARDS=' in open(conf, errors='replace').read()
    except OSError:
        return {'boxes': [], 'leaked': [], 'error': 'job.conf unreadable: not closed'}
    boxes, leaked, notes, error, handled = boxes_of(job, jobdir, log, listing), [], [], None, []
    try:
        for i in boxes:
            if os.path.isfile(os.path.join(jobdir, 'job', 'audit.py')):
                try:
                    db.stop_agent(i)
                except Exception:
                    pass
                try:
                    notes += _violations(i, db.audit(jobdir, i))
                except Exception:
                    notes.append(f'VIOLATION audit of {i} could not be read (job_sweep)')
            for _ in range(RM_TRIES):
                db.rm(i)
                sleep(5)
                if _archived(db, i):
                    break
            else:
                leaked.append(i)
            handled.append(i)
    except Exception as e:
        error = str(e) or type(e).__name__
        notes.append(f'-- sweep: {error}')
        leaked += [b for b in boxes if b not in handled and b not in leaked]
    try:
        with open(logp, 'a') as f:
            f.write(''.join(l + '\n' for l in notes + close_lines(log, fan, leaked, t)))
    except OSError as e:
        error = error or f'the log could not be written: {e}'
    out = {'boxes': boxes, 'leaked': leaked}
    if error:
        out['error'] = error
    viol = [n for n in notes if n.startswith('VIOLATION')]
    if viol:
        out['violations'] = viol
    return out


def closed_with_live_boxes(scratch, db, unreadable=None):
    """Jobs whose log has `== done` but that still own a live box: their cleanup failed to archive it, which is a leak.
    The logs are read FIRST and the listing taken after: both drivers archive before writing `== done`, so a box still live
    in a listing taken after the done line was read is really live (a listing from before would race a clean finish)."""
    done = {}
    for name in sorted(os.listdir(scratch)):
        d = os.path.join(scratch, name)
        log = os.path.join(d, 'logs', 'run.log')
        if not (os.path.isfile(os.path.join(d, 'job', 'worker.sh')) and os.path.isfile(log)):
            continue
        try:
            text = open(log, errors='replace').read()
        except OSError:
            if unreadable is not None:
                unreadable.append(name)
            continue
        if DONE.search(text):
            done[name] = (d, set(LOG_BOX.findall(text)))
    if not done:
        return {}
    listing = _listing(db)
    found = {}
    for name, (d, ids) in done.items():
        live = sorted(b.get('id') for b in listing.get('sandboxes') or [] if _state(b) not in GONE and (
            b.get('id') in ids or (((b.get('config') or {}).get('source') or {}).get('localDirectory') or '').startswith(d + '/')
            or fan_box(name, b.get('displayName') or b.get('name'))))
        if live:
            found[name] = live
    return found


def _archive_late(scratch, job, boxes, db, sleep):
    jobdir = os.path.join(scratch, job)
    leaked, notes, error, handled = [], [], None, []
    try:
        for i in boxes:
            if os.path.isfile(os.path.join(jobdir, 'job', 'audit.py')):
                try:
                    db.stop_agent(i)
                except Exception:
                    pass
                try:
                    notes += _violations(i, db.audit(jobdir, i))
                except Exception:
                    notes.append(f'VIOLATION audit of {i} could not be read (job_sweep)')
            for _ in range(RM_TRIES):
                db.rm(i)
                sleep(5)
                if _archived(db, i):
                    break
            else:
                leaked.append(i)
            handled.append(i)
    except Exception as e:
        error = str(e) or type(e).__name__
        notes.append(f'-- sweep: {error}')
        leaked += [b for b in boxes if b not in handled and b not in leaked]
    try:
        with open(os.path.join(jobdir, 'logs', 'run.log'), 'a') as f:
            f.write(''.join(l + '\n' for l in notes))
            for i in boxes:
                f.write(f"-- box {i} was still live after == done: LEAK ({'NOT removed' if i in leaked else 'archived by job_sweep'})\n")
    except OSError as e:
        error = error or f'the log could not be written: {e}'
    out = {'boxes': boxes, 'leaked': leaked, 'after_done': True}
    if error:
        out['error'] = error
    viol = [n for n in notes if n.startswith('VIOLATION')]
    if viol:
        out['violations'] = viol
    return out


def sweep(scratch, procs, now, db, sleep=time.sleep, t=None):
    t = t or time.strftime('%H:%M:%S', time.gmtime())
    listing = _listing(db)
    unreadable = []
    out = {job: _close(scratch, job, listing, db, sleep, t) for job in orphans(scratch, procs, now, unreadable)}
    try:
        for job, boxes in closed_with_live_boxes(scratch, db, unreadable).items():
            if job not in out:
                out[job] = _archive_late(scratch, job, boxes, db, sleep)
    except Exception as e:
        out['_late'] = {'error': str(e) or type(e).__name__}
    for name in unreadable:
        out.setdefault(name, {'boxes': [], 'leaked': [], 'error': 'run.log unreadable: not closed and not checked'})
    return out


class Discobox:
    def __init__(self, exe):
        self.exe = exe

    def _run(self, args, timeout):
        return subprocess.run([self.exe, *args], capture_output=True, text=True, timeout=timeout)

    def ls(self):
        r = self._run(['ls', '-o', 'json'], 300)
        if r.returncode != 0:
            raise RuntimeError('discobox ls failed: ' + r.stderr.strip()[:200])
        return json.loads(r.stdout)

    def rm(self, i):
        self._run(['rm', i], 600)

    def stop_agent(self, i):
        self._run(['shell', i, '--', 'sudo', '-n', 'systemctl', 'stop', 'job-agent'], 60)

    def audit(self, jobdir, i):
        r = subprocess.run([sys.executable, '-I', os.path.join(jobdir, 'job', 'audit.py'), i, os.path.join(jobdir, 'out')],
                           capture_output=True, text=True, timeout=600)
        with open(os.path.join(jobdir, 'out', 'audit.txt'), 'a') as f:
            f.write(f'box {i} (audited by job_sweep)\n' + r.stdout)
        if r.returncode != 0 and not any(l.startswith('VIOLATION') for l in r.stdout.splitlines()):
            raise RuntimeError('audit failed')
        if r.returncode != 0:   # it printed its VIOLATION(s), then failed: both are recorded, never a finished audit
            return r.stdout + f'\nVIOLATION audit exited {r.returncode}\n'
        return r.stdout


def main():
    home = os.path.expanduser('~')
    scratch = os.path.join(home, 'scratch')
    if not os.path.isdir(scratch):
        return 0
    try:
        done = sweep(scratch, processes(), time.time(), Discobox(os.path.join(home, '.local', 'bin', 'discobox')))
    except Exception as e:
        print(f'job_sweep: stopped before closing anything: {e}')
        return 1
    for job, r in done.items():
        if job == '_late':
            print(f"job_sweep: late-leak check stopped: {r['error']}")
            continue
        if 'not closed' in (r.get('error') or ''):
            print(f"job_sweep: {job}: {r['error']}")
            continue
        print(f"job_sweep: closed {job}: boxes {r['boxes']} leaked {r['leaked']}" + (f" error {r['error']}" if r.get('error') else ''))
        for v in r.get('violations') or []:
            print(f'job_sweep: {job}: {v}')
    return 1 if any(r.get('leaked') or r.get('error') or r.get('violations') for r in done.values()) else 0


if __name__ == '__main__':
    sys.exit(main())
