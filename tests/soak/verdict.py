"""The soak verdict, a pure function over the run's records and the criteria written before the first job.

records:
  jobs:   [{id, kind (claude|fan), launched (epoch s), launch_rc, done_at (box epoch s or None), verdict (PASS|FAIL|None),
            never_forgive, worker_gone, cred ({readable, swapped, hosts} or None)}]     box-side truth (box_status.py)
  chaos:  [{id, kind, at, until, ok, proof}]         every fault the soak tried, ok only if its post-condition held
  leaks:  [{at, live ([{id, job}] or None if unreadable), pending ([job ids] not yet done)}]   one per poll
  decoys: [{at, result (PASS|FAIL)}]
  window: {start, end}
criteria: {hours, chaos ([planned ids]), decoy_every_s}

A job may FAIL only if its box-side failure time falls inside the window of a proven chaos that can affect it (AFFECTS:
the server kill and the reboot), and a job whose worker died (closed by job_sweep) only inside a proven reboot; the one
job whose launch c5 killed is excused for not running, never for failing. After each proven chaos a job of each kind
PASSes (recovery shown). The soak runs at least MIN_HOURS; a job whose log or audit records a LEAK, a VIOLATION or an unrecorded box never passes. Every Claude job that
passes shows at least one swapped exchange, and every swapped exchange of any job went to LOCK_HOST. Every planned chaos
happened and proved itself; the run lasted its hours; decoys passed and were spaced; no box outlived its job.
"""
import re

LOCK_HOST = 'api.anthropic.com'
MIN_HOURS = 3
# The chaos that can make a job fail: only these forgive a FAIL, and only at the job's own (box-side) failure time.
AFFECTS = {'kill-discobox-server': ('claude', 'fan'), 'reboot': ('claude', 'fan')}
MEM_HEADROOM = 0.9
MAX_JOB_S = 1800   # the longest a soak job may run (disco_job --timeout-min 25, plus its cleanup)
LEAK_MIN_SPAN_S = 60
DECOY_SLACK_S = 900
WINDOW_SLACK_S = 60


def _inside(t0, t1, windows):
    return [c['id'] for c in windows if c['at'] <= t1 and c['until'] >= t0]


def _at(t, windows):
    return [c['id'] for c in windows if c['at'] <= t <= c['until']]


def _host(h):
    return re.sub(r':\d+$', '', str(h))


def verdict(rec, crit):
    fails = []
    chaos = rec.get('chaos') or []
    proven = [c for c in chaos if c.get('ok')]
    reboots = [c for c in proven if c.get('kind') == 'reboot']
    c5_jobs = {c.get('job') for c in chaos if c.get('job')}
    jobs = rec.get('jobs') or []
    if not jobs:
        fails.append('no jobs ran')
    for j in jobs:
        jid = j['id']
        if j.get('never_forgive'):
            fails.append(f'job {jid}: its log or audit records a LEAK or VIOLATION')
        c = j.get('cred')
        affecting = [x for x in proven if j.get('kind') in AFFECTS.get(x.get('kind'), ())]
        if c and c.get('hosts'):
            other = [h for h in c['hosts'] if _host(h) != LOCK_HOST]
            if other:
                fails.append(f'job {jid}: credential exchange to {", ".join(other)}')
        if j.get('verdict') is None:
            if j.get('launch_rc') not in (0, None):
                if jid not in c5_jobs and not _at(j['launched'], affecting):
                    fails.append(f"job {jid}: launch failed (rc {j['launch_rc']}) with no proven chaos in its window")
            else:
                fails.append(f"job {jid}: never finished (no '== done')")
            continue
        end = j['done_at'] if j.get('done_at') is not None else j['launched']
        if j['verdict'] == 'FAIL':
            if j.get('worker_gone'):
                if not any(x['at'] - MAX_JOB_S <= j['launched'] <= x['until'] and x['at'] <= end <= x['until'] for x in reboots):
                    fails.append(f'job {jid}: worker died with no reboot in its window')
            elif not _at(end, affecting):
                fails.append(f'job {jid}: FAIL with no chaos that affects it (proven, at its failure time)')
        elif j.get('kind') == 'claude':
            if not c or not c.get('readable'):
                fails.append(f'job {jid}: PASS without a readable audit')
            elif not c.get('swapped'):
                fails.append(f'job {jid}: PASS with no credential exchange in its audit')
    have = {c['id']: c for c in chaos}
    for cid in crit.get('chaos') or []:
        if cid not in have:
            fails.append(f'chaos {cid}: planned but never tried')
        elif not have[cid].get('ok'):
            fails.append(f"chaos {cid}: not proven ({have[cid].get('proof')})")
    if not chaos:
        fails.append('no chaos was injected')
    order = sorted(chaos, key=lambda x: x['at'])
    for i, x in enumerate(order):
        if not x.get('ok'):
            continue
        hi = order[i + 1]['at'] if i + 1 < len(order) else ((rec.get('window') or {}).get('end') or float('inf'))
        for kind in ('claude', 'fan'):
            if not any(j.get('kind') == kind and j.get('verdict') == 'PASS' and x['until'] <= j['launched'] < hi for j in jobs):
                fails.append(f"chaos {x['id']}: no {kind} job PASSed after it and before the next (recovery not shown)")
    if float(crit.get('hours') or 0) < MIN_HOURS:
        fails.append(f"the soak must run {MIN_HOURS} h; the criteria say {crit.get('hours')} h")
    w = rec.get('window') or {}
    need = float(crit.get('hours') or 0) * 3600
    ran = (w.get('end') or 0) - (w.get('start') or 0)
    if ran + WINDOW_SLACK_S < need:
        fails.append(f'ran {ran / 3600:.2f} h of {crit.get("hours")} h')
    fails += _leaks(rec.get('leaks') or [], chaos)
    fails += _decoys(rec.get('decoys') or [], w, crit.get('decoy_every_s'))
    return ('PASS' if not fails else 'FAIL'), fails


def _leaks(leaks, chaos):
    fails, seen = [], {}
    for l in leaks:
        if l.get('live') is None:
            if not _inside(l['at'], l['at'], chaos):
                fails.append(f"leak check at {l['at']}: listing unreadable outside any chaos window")
            continue
        pending = set(l.get('pending') or [])
        for b in l['live']:
            if b.get('job') is None or b.get('job') not in pending:
                seen.setdefault(b['id'], []).append(l['at'])
    if leaks:
        last = leaks[-1]
        if last.get('live') is None:
            fails.append('final leak check: listing unreadable')
        else:
            for b in last['live']:
                fails.append(f"final leak check: box {b['id']} (job {b.get('job')}) still live")
    for i, ts in seen.items():
        if len(ts) >= 2 and max(ts) - min(ts) >= LEAK_MIN_SPAN_S:
            fails.append(f'leak: box {i} outlived its job ({len(ts)} checks over {max(ts) - min(ts):.0f} s)')
    return fails


def _decoys(decoys, w, every):
    fails = []
    decoys = sorted(decoys, key=lambda d: d['at'])
    for d in decoys:
        if d.get('result') != 'PASS':
            fails.append(f"decoy at {d['at']}: {d.get('result')}")
    if every and w.get('start') is not None and w.get('end') is not None:
        marks = [w['start']] + [d['at'] for d in decoys] + [w['end']]
        gap = max(b - a for a, b in zip(marks, marks[1:]))
        if gap > every + DECOY_SLACK_S:
            fails.append(f'decoy gap {gap:.0f} s exceeds {every + DECOY_SLACK_S} s')
    return fails


def load_verdict(rec, crit):
    """Row i: every job PASSes except the over-cap step's refusals; the over-cap step never holds more than MAXBOX boxes
    and refuses the rest at preflight; Claude PASSes show a swapped exchange to LOCK_HOST only; nothing is left live."""
    fails, maxbox, fan_cap = [], int(crit.get('maxbox') or 6), int(crit.get('fan_cap') or 0)
    if fan_cap < 1:
        fails.append('criteria: no fan_cap (the pool box ceiling the fan step fills)')
    steps = rec.get('load') or []
    need = ['1', '2', '4', '6', f'over-cap-{maxbox + 1}', 'backlog-12', f'fan-{fan_cap}']
    missing = [n for n in need if n not in {str(s.get('step')) for s in steps}]
    if missing:
        fails.append(f'load steps missing: {missing}')
    for s in steps:
        name = str(s.get('step'))
        if name.startswith('over-cap'):
            if s.get('boxed', 0) != maxbox or s.get('live_max', 0) > maxbox:
                fails.append(f"load over-cap step: {s.get('boxed')} boxes and {s.get('live_max')} live for {s.get('jobs')} jobs (cap {maxbox})")
            if s.get('boxed', 0) + s.get('refused', 0) != s.get('jobs', 0) or s.get('failed', 0):
                fails.append(f"load over-cap step: {s.get('refused')} refused, {s.get('failed')} failed of {s.get('jobs')}")
        else:
            if s.get('failed', 0):
                fails.append(f"load step {name}: {s.get('failed')} job(s) FAIL or unfinished")
            if s.get('refused', 0):
                fails.append(f"load step {name}: {s.get('refused')} job(s) refused by the cap")
        if s.get('refused', 0) < 1 and name.startswith('over-cap'):
            fails.append(f"load over-cap step: nothing was refused (the cap of {maxbox} was never exercised)")
        if s.get('mem_used_max_mb') and s.get('mem_total_mb') and s['mem_used_max_mb'] > MEM_HEADROOM * s['mem_total_mb']:
            fails.append(f"load step {name}: memory {s['mem_used_max_mb']} of {s['mem_total_mb']} MB (over {int(MEM_HEADROOM * 100)}%)")
        if name.startswith('fan') and s.get('live_max', 0) > fan_cap:
            fails.append(f"load step {name}: {s.get('live_max')} boxes live at once (box ceiling {fan_cap})")
        if not name.startswith('fan') and s.get('live_max', 0) > maxbox:
            fails.append(f"load step {name}: {s.get('live_max')} boxes live at once (cap {maxbox})")
    for j in rec.get('jobs') or []:
        if j.get('never_forgive'):
            fails.append(f"job {j['id']}: its log or audit records a LEAK or VIOLATION")
        c = j.get('cred')
        if c and any(_host(h) != LOCK_HOST for h in c.get('hosts') or []):
            fails.append(f"job {j['id']}: credential exchange to {', '.join(h for h in c['hosts'] if _host(h) != LOCK_HOST)}")
        if j.get('kind') == 'claude' and j.get('verdict') == 'PASS' and not (c and c.get('readable') and c.get('swapped')):
            fails.append(f"job {j['id']}: PASS without a swapped exchange in a readable audit")
    leaks = rec.get('leaks') or []
    if not leaks or leaks[-1].get('live') is None:
        fails.append('final leak check: listing unreadable')
    elif leaks[-1]['live']:
        fails.append(f"final leak check: {len(leaks[-1]['live'])} box(es) still live: {[b['id'] for b in leaks[-1]['live']]}")
    return ('PASS' if not fails else 'FAIL'), fails
