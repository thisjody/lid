#!/bin/bash
# disco_fan worker: runs on the lid as the system unit disco-fan-<job> (so idle_shutdown sees it), from ~/scratch/<job>/job.
# Guarantees (ADR 26-11-06-001):
# - every box is stopped, audited and archived on every exit path, including boxes created but never recorded (found by name);
# - a shard's result is systemd's ExecMainStatus for its unit, never a file the job can write;
# - box output is unpacked as untrusted (safe_untar.py) under out/shard-<i>/box/; host files (rc, TIMEOUT, audit.json) sit
#   beside it and are written with O_NOFOLLOW;
# - any audit exchange, blocked or not, fails the job, and the audit runs only after the shard unit is stopped.
export XDG_RUNTIME_DIR=/run/user/$(id -u) PATH=$HOME/.local/bin:$PATH
J=$(cd "$(dirname "$0")" && pwd); . "$J/job.conf"; W=$(dirname "$J"); L=$W/logs/run.log; OUT=$W/out; mkdir -p "$OUT"
ids=(); cuts=(); stage=preflight; closed=; FAILS=; RUN=
log(){ echo "$*" >> "$L"; }
begin(){ stage=$1; log "== $1 $(date -u +%T)"; }
ok(){ log "== $stage exit 0"; }
DB(){ timeout 1800 discobox --auto-start-server=false "$@"; }   # bounded: a hung call never holds the job or its cleanup
in_box(){ local id=$1; shift; timeout 900 discobox --auto-start-server=false shell "$id" -- "$@" </dev/null; }   # bounded: a hung shell never holds cleanup
put(){ python3 -I -c 'import os,sys; fd=os.open(sys.argv[1], os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW, 0o644); os.write(fd, sys.argv[2].encode()); os.close(fd)' "$1" "$2"; }
# A shard's state, from systemd, failing closed. `systemctl show` on a unit systemd doesn't know still exits 0 and prints
# ExecMainStatus=0, so a result counts only from a LOADED unit whose main process EXITED (ExecMainCode=1) with an exit time.
# Prints: "running" | "done <status> <Result>" | "lost <why>".
shard_state(){ in_box "$1" systemctl show fan-shard -p LoadState -p ActiveState -p SubState -p ExecMainCode -p ExecMainStatus \
    -p ExecMainExitTimestamp -p Result 2>/dev/null | python3 -I -c '
import sys
d = dict(l.rstrip("\n").split("=", 1) for l in sys.stdin if "=" in l)
if d.get("LoadState") != "loaded":
    print("lost", "unit-" + (d.get("LoadState") or "unreadable")); sys.exit()
if d.get("ActiveState") in ("activating", "reloading", "deactivating") or (d.get("ActiveState") == "active" and d.get("SubState") == "running"):
    print("running"); sys.exit()
if not d.get("ExecMainExitTimestamp"):
    print("lost", "no-exit-recorded"); sys.exit()
if d.get("ExecMainCode") != "1":
    print("done", "killed-" + (d.get("ExecMainStatus") or "?"), d.get("Result") or "?"); sys.exit()
print("done", d.get("ExecMainStatus") or "?", d.get("Result") or "?")'; }
# The most one shard's result archive may be when it is pulled to the lid (as disco_job.sh caps every file it takes).
PULL_CAP=$((512 << 20))
collect(){ # $1 shard index, $2 box id: stop the unit (kills its cgroup), then pull out/ as an untrusted archive
  local d=$OUT/shard-$1 tmp sz
  in_box "$2" systemctl stop fan-shard >/dev/null 2>&1 || true
  in_box "$2" tar -C /tmp/job -czf /tmp/job.result.tgz out >/dev/null 2>&1 || { log "-- shard $1: no output to collect"; FAILS="$FAILS collect-$1"; return 0; }
  # The archive's size is read in the box before anything is copied to the lid (safe_untar's limits apply only after the copy).
  sz=$(in_box "$2" stat -c %s /tmp/job.result.tgz 2>/dev/null) || sz=   # under set -e a failed read refuses the shard, never the run
  if ! [[ $sz =~ ^[0-9]+$ ]]; then log "-- shard $1: result archive size unreadable: refused"; FAILS="$FAILS collect-$1"; return 0; fi
  [ "$sz" -le "$PULL_CAP" ] || { log "-- shard $1: result archive $sz bytes, over the ${PULL_CAP}-byte cap: refused"; FAILS="$FAILS collect-$1"; return 0; }
  tmp=$(mktemp -d "$W/pull.XXXXXX")
  if DB cp "$2:/tmp/job.result.tgz" "$tmp/result.tgz" >/dev/null 2>&1 && python3 "$J/safe_untar.py" "$tmp/result.tgz" "$d/box" >> "$L" 2>&1; then :
  else log "-- shard $1: result archive refused or missing"; FAILS="$FAILS collect-$1"; fi
  rm -rf "$tmp"
}
audit_try(){ # $1 shard label, $2 box id, $3 cutoff (optional): any exchange after the cutoff fails the job
  # Discobox >= 0.19 (ADR 26-11-10-001): a box provisions itself at boot, so a deny-by-default shell box records its own
  # REFUSED provisioning fetches before the job starts. Before the cutoff every exchange must be blocked (else deny-by-default
  # failed); after it, any exchange at all fails, as before. No cutoff (a box never launched) = strict: any exchange fails.
  mkdir -p "$OUT/shard-$1"
  python3 -I - "$2" "$OUT/shard-$1" "${3:-}" <<'PY' 2>&1
import json, os, subprocess, sys
from datetime import datetime
bid, d, cut = sys.argv[1], sys.argv[2], sys.argv[3]
import re
ts = lambda t: datetime.fromisoformat(re.sub(r'(\.\d{6})\d+', r'\1', t).replace('Z', '+00:00'))
LIMIT = 1000000
raw = subprocess.check_output(['timeout', '600', 'discobox', '--auto-start-server=false', 'admin', 'audit', 'http', '--discobox', bid,
                               '--limit', str(LIMIT), '-o', 'json'])
a = json.loads(raw); ex = a.get('exchanges')
fd = os.open(d + '/audit.json', os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644); os.write(fd, raw); os.close(fd)
if ex is None or not isinstance(ex, list):
    print(f"-- audit {bid}: INCOMPLETE (no exchanges list)"); sys.exit(1)
if len(ex) >= LIMIT:   # the API truncates silently at --limit: a full page may hide exchanges
    print(f"-- audit {bid}: INCOMPLETE (truncated at {LIMIT} exchanges)"); sys.exit(1)
if a.get('unavailablePools'):
    print(f"-- audit {bid}: INCOMPLETE (unavailable pools {a['unavailablePools']})"); sys.exit(1)
pre = [e for e in ex if cut and ts(e['createdAt']) < ts(cut)]
post = [e for e in ex if e not in pre]
# Discobox's own pool services (*.discobox.internal: the bundle asset store, the git cache) answer provisioning by design.
internal = lambda e: e.get('host', '').split(':')[0].endswith('.discobox.internal')
leak = [e for e in pre if not e.get('blocked') and not internal(e)]
if leak:
    seen = [" ".join(str(e.get(k)) for k in ("method", "host", "status")) for e in leak[:5]]
    print(f"-- audit {bid}: FAIL {len(leak)} provisioning exchange(s) NOT refused (deny-by-default broken): {seen}"); sys.exit(1)
if post:
    seen = [" ".join(str(e.get(k)) for k in ("method", "host", "status")) for e in post[:5]]
    print(f"-- audit {bid}: FAIL {len(post)} exchange(s) after {cut or 'create'}: {seen}"); sys.exit(1)
print(f"-- audit {bid}: clean (0 exchanges after {cut or 'create'}; {len(pre)} provisioning exchanges: {sum(map(internal, pre))} from Discobox's own store, the rest refused)")
PY
}
# An audit that could not be read or judged in full (the server down or restarting) is taken once more after the
# server answers again. A real failure is never retried: it is logged and counted as it came (8f3bda5 review).
audit(){ local o; o=$(audit_try "$@") && { echo "$o" >> "$L"; return 0; }; case "$o" in *": FAIL "*) echo "$o" >> "$L"; FAILS="$FAILS audit-$1"; return 1;; esac; server_wait || true; o=$(audit_try "$@") || FAILS="$FAILS audit-$1"; echo "$o" >> "$L"; }
# `discobox rm` can return 0 and leave a box running in state "failed" (seen on 0.19.1 and 0.19.5, 11/17): a leaked VM that
# also keeps the host from idling. Remove, then confirm from the listing; retry; a box never archived fails the job (leak).
rm_box(){ local t st
  for t in 1 2 3 4 5 6; do [ "$t" -gt 1 ] && { server_wait || true; }; DB rm "$1" >/dev/null 2>&1
    sleep 5; st=$(DB ls -o json 2>/dev/null | python3 -I -c 'import json,sys; i=sys.argv[1]; d=json.load(sys.stdin); print(next(((b.get("runtime") or {}).get("state") for b in d["sandboxes"] or [] if b["id"]==i), "gone"))' "$1")
    case "$st" in archived|deleted|gone) [ "$t" -gt 1 ] && log "-- box $1 archived on try $t"; return 0;; esac
  done; log "-- box $1 NOT removed (state $st after 6 tries): LEAK"; FAILS="$FAILS leak-$1"; return 1; }
# Cleanup must reach a server that answers real calls: if it died, discobox-ensure brings it back within about half a
# minute (11/22 soak). A server that stayed gone for the whole wait is not waited for again in this cleanup.
server_wait(){ [ -n "${SERVER_GONE:-}" ] && return 1; local end=$(( $(date +%s) + ${SERVER_WAIT_S:-180} )); while [ "$(date +%s)" -lt "$end" ]; do timeout 10 discobox --auto-start-server=false ls -o json 2>/dev/null | grep -q '"sandboxes"' && return 0; sleep 5; done; SERVER_GONE=1; log "-- the Discobox server did not answer for ${SERVER_WAIT_S:-180} s"; return 1; }
finish(){ rc=$?; set +e; [ -n "$closed" ] && return; closed=1; trap - EXIT; trap '' INT TERM HUP; flock -u 9 2>/dev/null
  # A host shutdown: leave the job for job_sweep after boot (no == done, no audit or archive on a dying server).
  sysst=$(systemctl is-system-running 2>/dev/null); { [ -e /run/nologin ] || [[ $sysst =~ ^(stopping|offline)?$ ]]; } && { log "== $stage exit 98 (host shutting down: left for job_sweep after boot; ${why:-no reason yet})"; exit 98; }
  [ "$rc" -ne 0 ] && { log "== $stage exit $rc${why:+ ($why)}"; FAILS="$FAILS $stage"; }
  log "== cleanup $(date -u +%T)"
  server_wait || { sysst=$(systemctl is-system-running 2>/dev/null); { [ -e /run/nologin ] || [[ $sysst =~ ^(stopping|offline)?$ ]]; } && { log "== $stage exit 98 (host shutting down during cleanup: left for job_sweep after boot)"; exit 98; }; }
  # Stop every shard first (kills its cgroup) so nothing runs between the audit and the archive.
  for id in "${ids[@]}"; do in_box "$id" systemctl stop fan-shard >/dev/null 2>&1; done
  for i in "${!ids[@]}"; do audit "$i" "${ids[$i]}" "${cuts[$i]:-}"; rm_box "${ids[$i]}"; done
  # A box created but never recorded (a failed --wait, a failed id parse) is found by name, audited and archived too.
  if [ -n "$RUN" ]; then
    sweep_list(){ DB ls -o json 2>/dev/null | python3 -I -c '
import json, sys
pre = sys.argv[1]
for b in json.load(sys.stdin)["sandboxes"] or []:
    name = b["displayName"] or ""   # v0.18.0 key: a missing key raises (the sweep fails loud below); a null name is ""
    if name.startswith(pre) and (b.get("runtime") or {}).get("state") not in ("archived", "deleted"):
        print(b["id"])' "$JOB-$RUN-"; }
    found=$(sweep_list) || { server_wait || true; found=$(sweep_list) || found="$found SWEEP_FAILED"; }
    for id in $found; do
      if [ "$id" = SWEEP_FAILED ]; then log "-- sweep for unrecorded boxes FAILED (ls schema?)"; FAILS="$FAILS sweep"; continue; fi
      case " ${ids[*]} " in *" $id "*) continue;; esac
      log "-- unrecorded box $id found by name: stopping, auditing, archiving"; FAILS="$FAILS unrecorded-box"
      in_box "$id" systemctl stop fan-shard >/dev/null 2>&1; audit "x-$id" "$id"; rm_box "$id"
    done
  fi
  log "== cleanup exit 0"
  python3 -I - "$OUT" "$SHARDS" "$FAILS" "$SHA" <<'PY' >> "$L" 2>&1
import json, os, sys
out, n, fails, sha = sys.argv[1], int(sys.argv[2]), sys.argv[3].split(), sys.argv[4]
def host_file(p):
    return open(p).read().strip() if os.path.isfile(p) and not os.path.islink(p) else None
shards = [{"shard": i, "rc": host_file(f"{out}/shard-{i}/rc"), "timeout": host_file(f"{out}/shard-{i}/TIMEOUT") is not None}
          for i in range(n)]
ok = not fails and all(s["rc"] == "0" and not s["timeout"] for s in shards)
summary = {"sha": sha, "verdict": "PASS" if ok else "FAIL", "fails": fails, "shards": shards}
fd = os.open(out + "/summary.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
os.write(fd, json.dumps(summary, indent=1).encode()); os.close(fd)
print("-- summary", json.dumps(summary))
PY
  v=$(python3 -I -c "import json;print(json.load(open('$OUT/summary.json'))['verdict'])" 2>/dev/null || echo FAIL)
  log "== done $v"
}
trap finish EXIT; trap 'exit 130' INT TERM HUP
set -eo pipefail   # any failing step ends the job; finish() still stops, audits and archives every box

begin preflight
python3 -I - <<'PY' >> "$L" 2>&1
import json, subprocess
g = lambda *a: json.loads(subprocess.check_output(['timeout', '300', 'discobox', '--auto-start-server=false', *a, '-o', 'json']))
h = g('admin', 'harnesses', 'get', 'shell')
assert h.get('boundSecrets') == 0, f"shell harness has bound secrets: {h.get('boundSecrets')}"
assert not (h.get('imageEgressHosts') or []), f"shell harness image egress: {h.get('imageEgressHosts')}"
assert not ((h.get('egress') or {}).get('allow') or []), f"shell harness egress allow list: {h.get('egress')}"
b = g('admin', 'harnesses', 'secrets', 'ls', 'shell')
assert 'secretBindings' in b and 'secrets' in b and not b['secretBindings'] and not b['secrets'], f"shell harness secret bindings (or an unread listing): {b}"
# Discobox >= 0.19 (ADR 26-11-10-001): a project-wide binding reaches every discobox, shell boxes included.
p = g('secret', 'bound', '--project-wide')
assert 'boundSecrets' in p and not p['boundSecrets'], f"a secret is bound project-wide (or an unread listing): {p}"
print('-- preflight: shell harness has no secret, no image egress, nothing bound project-wide')
PY
# The host firewall must be in force: this user needs no instance credentials, so the cloud metadata server must refuse it.
curl -s -m 5 -o /dev/null http://169.254.169.254/ 2>/dev/null && fwrc=0 || fwrc=$?
[ "$fwrc" = 7 ] || [ "$fwrc" = 56 ] || [ "$fwrc" = 35 ] || { log "-- firewall not in force (curl rc $fwrc to the metadata server)"; exit 4; }
systemctl is-active -q epistemic-stack-fw.service || { log "-- firewall not in force (epistemic-stack-fw.service is not active)"; exit 4; }
sudo -n ss -tlnpH > "$W/listeners.txt" 2>&1 && odd=$(python3 -I "$J/loopback_listeners.py" < "$W/listeners.txt") || { log "-- unexpected host listener(s) a box could reach: ${odd:-the listing failed}"; exit 4; }
ok

begin boxes
RUN=$(date -u +%H%M%S)-$(od -An -N3 -tx1 /dev/urandom | tr -d " \n")   # unique across reruns and concurrent jobs (the cleanup sweep matches this prefix)
# The host-wide box cap: the same lock disco_job takes; live boxes plus this job's shards stay within CAP (pool.max_boxes,
# the pool's measured ceiling, stack/stack.yaml).
exec 9>>"$HOME/scratch/.box-cap.lock"
flock -w 1800 9 || { log "-- the box-cap lock was not free in 30 min"; exit 5; }
live=$(DB ls -o json | python3 -I -c 'import json,sys
def dead(b):
    r = b.get("runtime") or {}; st = r.get("state"); rt = r.get("runtimeState")
    return st in ("archived", "deleted") or (rt in ("stopped", "archived", "deleted") and st != "awaiting_source")
print(sum(1 for b in json.load(sys.stdin)["sandboxes"] or [] if not dead(b)))') || { log "-- the box listing could not be read for the cap"; exit 5; }
[ $((live + SHARDS)) -le "$CAP" ] || { why="$live boxes already live + $SHARDS shards > cap $CAP"; log "-- refuse: $why"; exit 5; }
for i in $(seq 0 $((SHARDS - 1))); do
  j=$(DB admin box create --name "$JOB-$RUN-$i" --harness shell --egress-deny-by-default --no-project-skills --wait -o json 2>>"$L") \
    || { log "-- box create for shard $i failed (stderr above)"; exit 4; }
  ids+=("$(python3 -I -c 'import json,sys;print(json.loads(sys.argv[1])["id"])' "$j")")
  log "-- shard $i box ${ids[$i]}"
done
flock -u 9
# Discobox >= 0.19: wait until each box's boot provisioning has settled (ready or failed; under deny-by-default most
# features fail, which is expected), then stamp the audit cutoff. Up to 10 minutes.
for i in "${!ids[@]}"; do
  for t in $(seq 1 60); do
    st=$(DB ls -o json 2>/dev/null | python3 -I -c '
import json, sys
d = json.load(sys.stdin); items = d if isinstance(d, list) else next(v for v in d.values() if isinstance(v, list))
b = next((b for b in items if b.get("id") == sys.argv[1]), {})
p = ((b.get("runtime") or {}).get("agentStatus") or {}).get("provision") or {}
busy = [k for k, f in (p.get("features") or {}).items() if f.get("state") not in ("ready", "failed")]
print("settled" if p.get("state") in ("ready", "failed") and not busy else "busy")' "${ids[$i]}")
    [ "$st" = settled ] && break; sleep 10
  done
  [ "$st" = settled ] || { log "-- shard $i: box provisioning did not settle in 10 min"; exit 4; }
  cuts[$i]=$(date -u +%FT%T.%6NZ); log "-- shard $i provisioning settled; audit cutoff ${cuts[$i]}"
done
# The shell harness bundle these boxes ran (stack/GAPS.md G10: bundles follow the server version, unpinned by digest).
DB admin harnesses get shell -o json 2>/dev/null | python3 -I -c 'import json,sys; d=json.load(sys.stdin); print("harness shell revision %s bundle %s manifest %s" % (d.get("revision"), d.get("bundleDigest"), d.get("manifestDigest")))' > "$OUT/versions.txt" 2>&1 || true
log "-- versions: $(tr '\n' ';' < "$OUT/versions.txt")"
ok

begin probe
read -r PYV GLIBC ARCH < <(in_box "${ids[0]}" python3 -I -c 'import sys,platform;print("%d.%d"%sys.version_info[:2], platform.libc_ver()[1].split(".")[1], platform.machine())')
# The box's answer is untrusted: it reaches bash arithmetic and command arguments, so each value must be a plain token.
[[ $PYV =~ ^3\.[0-9]{1,2}$ ]] && [[ $GLIBC =~ ^[0-9]{1,3}$ ]] && [[ $ARCH =~ ^(x86_64|aarch64)$ ]] || { log "-- probe: the box's python/glibc/arch answer is not valid"; exit 5; }
log "-- box python $PYV glibc 2.$GLIBC $ARCH"
ok

if [ "$DEPS" = 1 ]; then
  begin wheelhouse
  WH=$W/wheelhouse; mkdir -p "$WH"
  # Resolve for the newest manylinux tag uv knows at or below the box's glibc (a wheel for an older glibc runs on a newer one).
  ug=$GLIBC
  UV=$HOME/.local/bin/uv   # pinned by sha256 in stack.yaml (components.uv), installed by stack_up
  # --only-binary :all: (binary wheels only) means no package is ever built, so no package code runs on the host.
  until "$UV" pip compile "$J/req.lock.txt" --python-platform "${ARCH}-manylinux_2_${ug}" --python-version "$PYV" \
      --generate-hashes --no-header --no-annotate --only-binary :all: -o "$W/req.target.txt" >> "$W/compile.log" 2>&1; do
    ug=$((ug - 1)); [ "$ug" -ge 17 ] || { cat "$W/compile.log" >> "$L"; exit 6; }
  done
  log "-- resolved for ${ARCH}-manylinux_2_${ug} (box glibc 2.$GLIBC), python $PYV"
  python3 "$J/lockcheck.py" "$J/req.lock.txt" "$W/req.target.txt" >> "$L" 2>&1
  PLAT=(); for g in $(seq "$GLIBC" -1 17); do PLAT+=(--platform "manylinux_2_${g}_${ARCH}"); done
  PLAT+=(--platform "manylinux2014_${ARCH}" --platform "manylinux2010_${ARCH}" --platform "manylinux1_${ARCH}")
  # pip itself comes from a hash-pinned lock (pip.lock) into a throwaway venv, never fetched by version alone.
  UV_PYTHON_DOWNLOADS=never "$UV" venv -q --python /usr/bin/python3 "$W/pipenv" >> "$L" 2>&1 && "$UV" pip install -q --python "$W/pipenv/bin/python" --require-hashes --only-binary :all: -r "$J/pip.lock" >> "$L" 2>&1 \
    || { log "-- pip from pip.lock failed"; exit 6; }
  "$W/pipenv/bin/python" -m pip download --require-hashes --no-deps -r "$W/req.target.txt" -d "$WH" \
      --only-binary=:all: --python-version "$PYV" --implementation cp --abi "cp${PYV/./}" --abi abi3 --abi none "${PLAT[@]}" \
      >> "$L" 2>&1
  cp "$W/req.target.txt" "$WH/req.txt"; ( cd "$WH" && sha256sum ./*.whl > SHA256SUMS )
  tar -C "$WH" -czf "$W/wheelhouse.tgz" .
  ok
fi

begin launch
for i in "${!ids[@]}"; do id=${ids[$i]}
  printf 'JOB=%q\nSHARD_INDEX=%q\nSHARD_COUNT=%q\nWORKERS=%q\nTMIN=%q\n' "$JOB" "$i" "$SHARDS" "$WORKERS" "$TMIN" > "$W/env.$i"
  in_box "$id" mkdir -p /tmp/job
  for f in "$J/src.tar" "$J/cmd.sh" "$J/box_run.sh" ${DEPS:+"$W/wheelhouse.tgz"}; do DB cp "$f" "$id:/tmp/job/$(basename "$f")" >/dev/null; done
  DB cp "$W/env.$i" "$id:/tmp/job/env" >/dev/null
  # The shard runs as an unprivileged user, not the box's root: a job must not depend on (or have) root.
  # Root owns the runner, the command and env (read-only to the job); the job owns only out/, src/, venv/ and wheelhouse/.
  in_box "$id" bash -c 'id -u job >/dev/null 2>&1 || useradd -m -U job; cd /tmp/job && chown root:root . ./* && chmod 0755 . && chmod 0644 ./* && mkdir -p out src venv wheelhouse && chown job:job out src venv wheelhouse'
  # Keepalive lease (a box stops itself after 30 idle minutes, Discobox ADR 0108): taken by root and checked.
  in_box "$id" touch -d "+$((TMIN + 10)) minutes" "/run/discobox/keepalive/$JOB"
  in_box "$id" test -f "/run/discobox/keepalive/$JOB" || { log "-- shard $i: keepalive lease missing"; exit 7; }
  # No --collect: the unit's ExecMainStatus must survive its exit, because that is the result.
  # RemainAfterExit: the unit stays loaded after a SUCCESSFUL exit too (transient units are otherwise collected at once).
  # The runner is root (it kills the job's leftovers before reporting); the command inside it runs as `job`.
  in_box "$id" systemd-run --unit=fan-shard -p RemainAfterExit=yes -p TasksMax=infinity -p KillMode=control-group \
      bash /tmp/job/box_run.sh >/dev/null
  log "-- shard $i launched"
done
ok

begin run
T0=$(date +%s); left=$SHARDS; declare -A miss
while [ "$left" -gt 0 ]; do sleep 20; left=0
  for i in "${!ids[@]}"; do d=$OUT/shard-$i; mkdir -p "$d"
    if [ -f "$d/rc" ] || [ -f "$d/TIMEOUT" ]; then continue; fi
    read -r state status result <<< "$(shard_state "${ids[$i]}")"
    case "$state" in
      done)
        put "$d/rc" "$status"; collect "$i" "${ids[$i]}"; log "-- shard $i done rc=$status ($result)"
        if [ -n "$REQUIRE" ] && [ "$status" = 0 ] && ! [ -f "$d/box/out/$REQUIRE" ]; then
          log "-- shard $i: rc=0 but required output out/$REQUIRE is missing"; FAILS="$FAILS require-$i"; fi;;
      lost)
        # An unreadable state (a transient `discobox shell` failure) gets three reads in a row; a missing unit is LOST at once.
        if [ "$status" = unit-unreadable ] && [ "$(( ${miss[$i]:-0} + 1 ))" -lt 3 ]; then
          miss[$i]=$(( ${miss[$i]:-0} + 1 )); left=$((left + 1)); log "-- shard $i: state unreadable (${miss[$i]}/3)"; continue; fi
        put "$d/rc" "lost:$status"; collect "$i" "${ids[$i]}"; log "-- shard $i LOST ($status)"; FAILS="$FAILS lost-$i";;
      *)   # running (or any state not yet final): only the job's deadline ends it
        miss[$i]=0
        if [ $(( $(date +%s) - T0 )) -gt $(( (TMIN + 5) * 60 )) ]; then
          collect "$i" "${ids[$i]}"; put "$d/TIMEOUT" "1"; log "-- shard $i TIMEOUT after $((TMIN + 5)) min"
        else left=$((left + 1)); fi;;
    esac
  done
done
log "-- wall $(( $(date +%s) - T0 ))s"
ok
