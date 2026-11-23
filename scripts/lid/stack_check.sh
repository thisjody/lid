#!/bin/bash
# stack_check.sh: is the composed stack current? (ADR 26-11-03-001 §5). Runs on the DESK; prints a short ▍STACK section.
#   - each pin in stack/stack.yaml vs the upstream's latest release (gh api, read-only)
#   - GitHub security advisories on each upstream repo published since the pinned release
#   - drift: what is installed on the box vs the pin (only if the box is RUNNING; never starts it)
#   - secret expiry: warns 14 days before `secret_expires`, and flags a secret that isn't loaded/dated
# Exit 0 always (it's a report); lines starting "!" need attention.
set -uo pipefail
R=$(cd -P "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
site(){ python3 "$R/scripts/lid/stack_units.py" "$R/stack/stack.yaml" get "host.$1"; }
st=$(gcloud compute instances describe "${LID_INSTANCE:-$(site name)}" --project "$(site project)" --zone "$(site zone)" --format='value(status)' 2>/dev/null)
BOX=
if [ "$st" = RUNNING ]; then
  BOX=$(bash "$R/scripts/lid_run.sh" 'export XDG_RUNTIME_DIR=/run/user/$(id -u) PATH=$HOME/.local/bin:$PATH
    echo "toolhive $(thv version 2>/dev/null | awk "/^ToolHive/{print \$2}")"
    echo "discobox $(discobox --auto-start-server=false version 2>/dev/null | awk "{print \$NF}" | sort -u | tr "\n" ",")"
    echo "agentgateway v$(/usr/local/bin/agentgateway -V 2>/dev/null | awk "{print \$NF}")"; echo "unitsys epistemic-agw.service $(systemctl is-active epistemic-agw.service)"
    echo "podman v$(podman version --format "{{.Client.Version}}" 2>/dev/null)"
    for h in netavark aardvark-dns crun; do echo "$h $(/usr/local/libexec/podman/$h --version 2>/dev/null | awk "NR==1{print \$NF}")"; done
    for u in $(systemctl --user list-units "thv-*" --plain --no-legend 2>/dev/null | awk "{print \$1}"); do echo "unit $u $(systemctl --user is-active $u)"; done
    podman ps --format "image {{.Names}} {{.Image}}" 2>/dev/null
    for i in $(podman ps --format "{{.Image}}" 2>/dev/null | grep -v @); do echo "digest $i $(podman image inspect --format "{{.Digest}}" $i)"; done' </dev/null 2>/dev/null)
fi
cd "$R" && BOXSTATE="$st" BOX="$BOX" python3 -I - <<'PY'
import datetime as dt, json, os, re, subprocess, yaml
import sys; sys.path.insert(0, 'scripts/lid'); from stack_site import load; m = load('stack/stack.yaml')
comp = {c['name']: c for c in m['components']}
box, state = os.environ.get('BOX', ''), os.environ.get('BOXSTATE') or 'unknown'
today = dt.date.today()
out = ['▍STACK  (box %s)' % ('ON' if state == 'RUNNING' else 'OFF: drift not checked' if state else 'unknown')]

def gh(path):
    r = subprocess.run(['gh', 'api', path], capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None

def ver(s):
    m = re.search(r'(\d+(?:\.\d+)*)', s or '')
    return tuple(int(x) for x in m.group(1).split('.')) if m else None

def patched_by(a, tag):
    fixed = [ver(v.get('patched_versions')) for v in a.get('vulnerabilities') or []]
    return bool(fixed) and None not in fixed and ver(tag) is not None and all(f <= ver(tag) for f in fixed)

def check(label, repo_url, tag):
    mm = re.match(r'https://github.com/([^/]+/[^/\s)]+)', repo_url or '')
    if not mm:
        out.append('  %-10s %s (no GitHub upstream to compare)' % (label, tag)); return
    repo = mm.group(1)
    latest = gh('repos/%s/releases/latest' % repo) or {}
    pinned = gh('repos/%s/releases/tags/%s' % (repo, tag)) or {}
    since = pinned.get('published_at') or '1970-01-01T00:00:00Z'
    lt = latest.get('tag_name', '?')
    line = '  %-10s pin %s, latest %s' % (label, tag, lt)
    if lt not in ('?', tag):
        line = '! ' + line[2:] + ' (upgrade available)'
    out.append(line)
    adv = gh('repos/%s/security-advisories?per_page=100&state=published' % repo) or []
    # An advisory published after the pinned release still counts unless the pin already carries its fix
    # (advisories often land hours after the release that patches them: ToolHive v0.62.0, 11/4).
    new = [a for a in adv if (a.get('published_at') or '') > since and not patched_by(a, tag)]
    for a in new[:5]:
        out.append('!   advisory %s %s %s: %s' % (a['ghsa_id'], a.get('severity'), a['published_at'][:10], (a.get('summary') or '')[:70]))
    if len(new) > 5:
        out.append('!   +%d more advisories since %s' % (len(new) - 5, since[:10]))

check('discobox', comp['discobox']['upstream'], comp['discobox']['pin']['tag'])
check('toolhive', comp['toolhive']['upstream'], comp['toolhive']['pin']['tag'])
check('agentgw', comp['agentgateway']['upstream'], comp['agentgateway']['pin']['tag'])
for s in comp['toolhive']['servers']:
    tag = s['image'].rsplit(':', 1)[1]
    if s['name'] == 'github':
        check('github-mcp', s['upstream'], tag)
    else:
        out.append('  %-10s image %s (dockyard build of %s; bump with the registry)' % (s['name'], tag, s['upstream'].split('github.com/')[-1]))
check('podman', comp['podman']['upstream'], comp['podman']['pin']['tag'])
for h, hp in comp['podman']['helpers'].items():
    check(h[:10], hp['upstream'], hp['tag'])

if box:
    have = dict(l.split(' ', 1) for l in box.splitlines() if l.startswith(('toolhive ', 'discobox ', 'podman ', 'agentgateway ', 'netavark ', 'aardvark-dns ', 'crun ')))
    want = {'toolhive': comp['toolhive']['pin']['tag'], 'discobox': comp['discobox']['pin']['tag'] + ',',
            'podman': comp['podman']['pin']['tag'], 'agentgateway': comp['agentgateway']['pin']['tag']}
    want.update({h: hp['tag'].lstrip('v') for h, hp in comp['podman']['helpers'].items()})
    drift = ['%s %s (pin %s)' % (k, have.get(k), v.rstrip(',')) for k, v in want.items() if have.get(k) != v]
    for s in comp['toolhive']['servers']:
        u = [l for l in box.splitlines() if l.startswith('unit %s ' % s['unit'])]
        img = [l for l in box.splitlines() if l.startswith('image %s ' % s['name'])]
        if img and s['digest'] not in img[0]:
            drift.append('%s runs %s (pin %s)' % (s['name'], img[0].split()[-1], s['digest'][:19]))
        if not u and not s.get('secret'):
            drift.append('%s unit not running' % s['name'])
    eg = comp['toolhive'].get('egress_image')   # pinned egress proxy (ADR 26-11-17-002): every *-egress container runs exactly it
    for l in box.splitlines():
        f = l.split()
        if eg and len(f) == 3 and f[0] == 'image' and f[1].endswith('-egress') and f[2].split('@')[-1] != eg.split('@')[-1]:   # podman shows repo@digest, no tag
            drift.append('%s runs %s (pin %s)' % (f[1], f[2], eg.split('@')[-1][:19]))
    if 'unitsys epistemic-agw.service active' not in box:
        drift.append('epistemic-agw.service not active')
    aux = [l.split(' ', 2)[1:] for l in box.splitlines() if l.startswith('digest ')]
    out.append(('! drift: ' + '; '.join(drift)) if drift else '  drift: none (box matches the manifest)')
    # ToolHive pulls these itself by :latest for --isolate-network; it can't be told a digest, so the manifest records the
    # digest we last reviewed (toolhive.aux_images) and a change is flagged for review, never taken silently.
    known = comp['toolhive'].get('aux_images') or {}
    for img, d in aux:
        name = img.rsplit('/', 1)[-1].split(':')[0]
        if name not in known:
            out.append('! aux image %s %s: not recorded in stack.yaml toolhive.aux_images' % (img, d[:19]))
        elif known[name] != d.strip():
            out.append('! aux image %s CHANGED: reviewed %s, running %s (review, then update toolhive.aux_images)' % (img, known[name][:19], d.strip()[:19]))
        else:
            out.append('  aux image %s %s (as reviewed; :latest pulled by thv --isolate-network)' % (img, d[:19]))

for s in comp['toolhive']['servers']:
    if not s.get('secret'):
        continue
    e = s.get('secret_expires')
    if not e:
        out.append('! secret %s: no expiry recorded (not loaded yet? scripts/lid/stack_secret.sh %s --expires YYYY-MM-DD)' % (s['secret'], s['secret']))
        continue
    days = (dt.date.fromisoformat(str(e)) - today).days
    if days < 0:
        out.append('! secret %s EXPIRED %s: rotate with stack_secret.sh' % (s['secret'], e))
    elif days <= 14:
        out.append('! secret %s expires %s (%d days): rotate with stack_secret.sh' % (s['secret'], e, days))
    else:
        out.append('  secret %s expires %s (%d days)' % (s['secret'], e, days))
print('\n'.join(out))
PY
