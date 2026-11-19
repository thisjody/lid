#!/bin/bash
# Usage: scripts/monitor/disco_job.sh <job> <repo-https-url> <git-ref> <prompt-file> [--env K=V]... [--allow host,host] [--setup file] [--timeout-min N] [--tools gateway]
# One Discobox job on the build box, launched from the desk (lid_run.sh only; the site's own box, host.name). Returns once the job is
# running under nohup on the lid; watch it with boxwatch.sh. On the lid: clones to ~/scratch/<job>/src, starts a locked box (up to
# MAXBOX in parallel), runs --setup in the box, runs the prompt headless, finishes when the agent writes /tmp/job/DONE, exports commits to ~/scratch/<job>/out/patches,
# saves the audit summary to out/audit.txt, archives the box. logs/run.log gets == <stage> / == <stage> exit N / == done on every path.
# Lockdown is REQUIRED, not applied: the job refuses to start unless the Claude credential's route is
# exactly api.anthropic.com on the secret and on its binding (ADR 26-11-10-001). Harness egress is Discobox's own default, so a
# box can provision itself at boot; --allow still goes on the box's own level. No credential ever enters the box.
# --tools gateway (opt-in, ADRs 26-11-03-002, 26-11-03-004): the job gets ONE MCP server, agentgateway on the lid
# (http://<host.internal_ip>:18800/mcp), which multiplexes every ToolHive server in stack/stack.yaml (time_*, github_* tools). Adds the
# lid's address to the box's egress allow (EPI_STACK leaves only the gateway port open on it), writes /tmp/job/mcp.json for that
# one server, and runs claude with --mcp-config /tmp/job/mcp.json --strict-mcp-config. Hosted connectors stay off
# (ENABLE_CLAUDEAI_MCP_SERVERS=false). Credentials stay in ToolHive's containers; the job gets a URL, never a token.
# The per-server paths (--tools time|github) are gone: the ToolHive proxies are loopback-only and refuse this user, so a
# sandbox can no longer reach them except through the gateway. Without --tools nothing changes.
# --src-tar FILE (opt-in, for a private repo): REPO must be the word `local`; FILE (a .tgz of the tree) is uploaded to the lid and
# committed there as W/src's only commit, so nothing is cloned and no credential is needed. Env EPI_PLUGIN_DIR=<path relative to
# the tree> makes the agent run with --plugin-dir <path> (a plugin shipped inside the tarball, e.g. claude-security for a scan).
set -euo pipefail
D=$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd); LID=$D/../lid_run.sh
JOB=${1:?job}; REPO=${2:?repo url}; REF=${3:?git ref}; PROMPT=${4:?prompt file}; shift 4
ENVS=(); ALLOW=; SETUP=; TMIN=40; TOOLS=; SRCTAR=
while [ $# -gt 0 ]; do case "$1" in
  --env) ENVS+=("$2"); shift 2;; --allow) ALLOW=$2; shift 2;; --setup) SETUP=$2; shift 2;; --timeout-min) TMIN=$2; shift 2;; --tools) TOOLS=$2; shift 2;; --src-tar) SRCTAR=$2; shift 2;;
  *) echo "unknown arg: $1" >&2; exit 2;; esac; done
[[ $JOB =~ ^[a-z0-9][a-z0-9._-]*$ ]] || { echo "bad job name" >&2; exit 2; }
case "$JOB" in stack|bin) echo "refuse: $JOB is a reserved name on the box" >&2; exit 2;; esac
if [ -n "$SRCTAR" ]; then { [ "$REPO" = local ] && [ -f "$SRCTAR" ]; } || { echo "--src-tar needs REPO=local and an existing file" >&2; exit 2; }
else [[ $REPO =~ ^https://[^@]+$ ]] || { echo "repo must be a public https URL with no credential" >&2; exit 2; }; fi
[[ $TMIN =~ ^[0-9]+$ ]] || { echo "bad --timeout-min" >&2; exit 2; }
[ -f "$PROMPT" ] || { echo "no prompt file" >&2; exit 2; }
[ -z "$SETUP" ] || [ -f "$SETUP" ] || { echo "no setup file" >&2; exit 2; }
TURL=; TUNIT=
if [ -n "$TOOLS" ]; then
  [ "$TOOLS" = gateway ] || { echo "--tools: only 'gateway' (ADR 26-11-03-004: ToolHive servers are reached through agentgateway; '$TOOLS' is one of its targets)" >&2; exit 2; }
  read -r TIP TPORT TPATH TUNIT < <(cd "$D/../.." && python3 -I -c '
import sys
sys.path.insert(0, "scripts/lid"); from stack_site import load; m = load("stack/stack.yaml"); a = [c for c in m["components"] if c["name"] == "agentgateway"][0]
print(m["host"]["internal_ip"], a["port"], a["path"], a["unit"])') || { echo "--tools: no agentgateway component in stack/stack.yaml" >&2; exit 2; }
  TURL="http://$TIP:$TPORT$TPATH"; ALLOW=${ALLOW:+$ALLOW,}$TIP
fi
# A credential never goes into a box by --env: refuse credential-looking names and values (case-insensitive).
cred_env(){ local k=${1%%=*} v=${1#*=}; k=$(tr '[:lower:]' '[:upper:]' <<<"$k")
  case "$k" in *KEY*|*TOKEN*|*PASS*|*SECRET*|*AUTH*|*CRED*|*SESSION*|*COOKIE*) return 0;; esac
  case "$k" in ENABLE_CLAUDEAI_MCP_SERVERS|ANTHROPIC_*|CLAUDE_*|*_PROXY|NO_PROXY|NODE_OPTIONS|NODE_EXTRA_CA_CERTS|SSL_CERT_*|PATH|HOME|LD_*|PYTHON*|GIT_*|BASH_ENV|ENV) return 0;; esac
  [[ $v =~ (ghp_|gho_|ghs_|ghu_|github_pat_|sk-ant-|xox[abp]-|AKIA|ya29\.|-----BEGIN) ]] && return 0; return 1; }
env_ok(){ [[ $1 == *[[:cntrl:]]* ]] && return 1; [[ ${1%%=*} =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1; return 0; }
for e in ${ENVS[@]+"${ENVS[@]}"}; do cred_env "$e" && { echo "refuse: ${e%%=*} looks like a credential" >&2; exit 2; }
  env_ok "$e" || { echo "refuse: --env ${e%%=*}: a NAME=value with no control characters" >&2; exit 2; }; done

T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
cp "$PROMPT" "$T/prompt.md"; if [ -n "$SETUP" ]; then cp "$SETUP" "$T/setup.sh"; else : > "$T/setup.sh"; fi
# The Claude credential's secret id lives in stack/stack.yaml (components.discobox.claude_secret), one place.
SECID=$(python3 "$D/../lid/stack_units.py" "$D/../../stack/stack.yaml" get components.discobox.claude_secret) || { echo "no components.discobox.claude_secret in stack/stack.yaml or stack/site.yaml" >&2; exit 2; }
GITNAME=$(python3 "$D/../lid/stack_units.py" "$D/../../stack/stack.yaml" get host.commit_name) && GITMAIL=$(python3 "$D/../lid/stack_units.py" "$D/../../stack/stack.yaml" get host.commit_email) \
  || { echo "no host.commit_name / host.commit_email in stack/site.yaml" >&2; exit 2; }
{ printf 'JOB=%q\nREPO=%q\nREF=%q\nALLOW=%q\nTMIN=%q\nTOOLS=%q\nTURL=%q\nTUNIT=%q\nSRCTAR=%q\nSEC=%q\nGITNAME=%q\nGITMAIL=%q\n' "$JOB" "$REPO" "$REF" "$ALLOW" "$TMIN" "$TOOLS" "$TURL" "$TUNIT" "${SRCTAR:+1}" "$SECID" "$GITNAME" "$GITMAIL"; printf 'ENVS=('; for e in ${ENVS[@]+"${ENVS[@]}"}; do printf '%q ' "$e"; done; echo ')'; } > "$T/job.conf"
cat > "$T/worker.sh" <<'WORKER'
#!/bin/bash
# Runs on the lid. Everything here reaches the log; the trap closes every exit path.
export XDG_RUNTIME_DIR=/run/user/1000 PATH=$HOME/.local/bin:$PATH GIT_TERMINAL_PROMPT=0
J=$(cd "$(dirname "$0")" && pwd); . "$J/job.conf"; W=$(dirname "$J"); L=$W/logs/run.log; OUT=$W/out; mkdir -p "$OUT"
# The Claude credential is Workload Identity Federation: Discobox runs bin/wif_token.sh to mint a 10-minute token from the lid's
# Google identity, and the box only ever holds a sentinel. The agent runs headless (claude -p, API mode), which is what the plan's
# API credits cover; the interactive TUI sends a Claude Code subscription header the credits refuse. MAXBOX caps parallel jobs.
MAXBOX=6; id=; stage=preflight; closed=
log(){ echo "$*" >> "$L"; }
stage(){ log "== $1 exit 0"; stage=$2; log "== $2 $(date -u +%T)"; }
# Every Discobox call is bounded (a hung shell or ls never holds the job, its cleanup, or the idle check forever).
dbx(){ timeout -k 30 3600 discobox "$@"; }
inbox(){ dbx shell "$id" -- "$@"; }
quick(){ timeout -k 10 60 discobox shell "$id" -- "$@"; }
# The most this job takes back from its box in any one file (agent output is untrusted).
CAP=$((512 << 20))
# take FILE CMD...: CMD's stdout into FILE, capped at CAP; returns CMD's own status (not head's) and logs a truncation.
take(){ local f=$1; shift; "$@" | head -c "$CAP" > "$f"; local r=("${PIPESTATUS[@]}")
  [ "$(stat -c %s "$f" 2>/dev/null || echo 0)" -ge "$CAP" ] && log "-- $(basename "$f") hit the ${CAP}-byte cap: truncated"
  return "${r[0]}"; }
# discobox rm can return 0 and leave a box running (0.19.1 and 0.19.5, 11/17): confirm from the listing, retry, else LEAK.
rm_box(){ local t st
  for t in 1 2 3 4 5 6; do dbx rm "$1" >/dev/null 2>&1
    sleep 5; st=$(timeout 300 discobox ls -o json 2>/dev/null | python3 -I -c 'import json,sys; i=sys.argv[1]; d=json.load(sys.stdin); print(next(((b.get("runtime") or {}).get("state") for b in d["sandboxes"] or [] if b["id"]==i), "gone"))' "$1")
    case "$st" in archived|deleted|gone) [ "$t" -gt 1 ] && log "-- box $1 archived on try $t"; return 0;; esac
  done; log "-- box $1 NOT removed (state $st after 6 tries): LEAK"; return 1; }
finish(){ rc=$?; [ -n "$closed" ] && return; closed=1; trap - EXIT; trap '' INT TERM HUP; flock -u 9 2>/dev/null || true
  # The box-cap lock (fd 9) is released first, so a slow cleanup never blocks other jobs' creates.
  # A host shutdown (or systemctl unable to answer: D-Bus already gone): the server is going away, so an audit or archive now would only write VIOLATION/LEAK. No == done:
  # job_sweep closes the job after boot (worker gone), audits and archives its box.
  sysst=$(systemctl is-system-running 2>/dev/null); { [ -e /run/nologin ] || [[ $sysst =~ ^(stopping|offline)?$ ]]; } && { log "== $stage exit 98 (host shutting down: left for job_sweep after boot; ${why:-no reason yet})"; exit 98; }
  # A create that failed after the server made the box leaves no id: find it by its source path, this job's own $W/src (sweep).
  if [ -z "$id" ]; then
    id=$(timeout 300 discobox ls -o json 2>/dev/null | python3 -I -c 'import json,sys; w=sys.argv[1]; print(next((b["id"] for b in json.load(sys.stdin)["sandboxes"] or [] if ((b.get("config") or {}).get("source") or {}).get("localDirectory")==w and (b.get("runtime") or {}).get("state") not in ("archived","deleted")), ""))' "$W/src" 2>/dev/null)
    [ $? -eq 0 ] || log "-- sweep FAILED: the box listing could not be read (an orphaned box may remain)"
    [ -n "$id" ] && log "-- sweep: box $id found by its source path, auditing and removing it"
  fi
  if [ -n "$id" ]; then
    # A failed or timed-out job keeps its work: everything since base, committed or not (toolhive-5284 lost 890 lines on 11/3).
    if [ "$rc" -ne 0 ] && [ -n "${base:-}" ]; then
      take "$OUT/wip.patch" inbox bash -c "cd '$W/src' && git add -A && git diff --cached --binary $base" </dev/null 2>/dev/null \
        && log "-- saved out/wip.patch ($(grep -c '^diff --git' "$OUT/wip.patch") files)" || log "-- wip export failed"
      take "$OUT/agent.jsonl" inbox cat /tmp/job/agent.jsonl </dev/null 2>/dev/null || true
    fi
    # An audit that cannot be read is a violation, not a pass: the connector check never ran (Discobox 0.19 renamed
    # --discobox-id to --discobox and dbx19-dry3 "passed" with no audit at all, 11/10).
    # The agent stops first, so nothing it sends after the audit snapshot goes unrecorded.
    timeout 60 discobox shell "$id" -- sudo -n systemctl stop job-agent </dev/null >/dev/null 2>&1 || true
    { echo "box $id"; python3 "$J/audit.py" "$id" "$OUT" 2>&1 || echo "VIOLATION audit could not be read"; } > "$OUT/audit.txt" || true
    grep -q '^VIOLATION' "$OUT/audit.txt" && { log "-- lockdown violation, see out/audit.txt"; [ "$rc" -ne 0 ] || rc=3; }
    rm_box "$id" || { [ "$rc" -ne 0 ] || rc=3; }
  fi
  if [ "$rc" -eq 0 ]; then log "== $stage exit 0"; log "== done $(date -u +%T)"
  else log "== $stage exit $rc (${why:-unexpected})"; log "== done FAILED at $stage $(date -u +%T)"; fi; exit "$rc"; }
trap finish EXIT; trap 'why=signal; exit 130' INT TERM HUP
die(){ why=$2; exit "$1"; }
log "== $stage $(date -u +%T)"

# Refuse, don't repair: a lockdown that drifted is a thing for the desk to look at.
python3 "$J/preflight.py" "$SEC" > "$W/preflight.txt" 2>&1 || die 4 "lockdown not in place: $(tail -1 "$W/preflight.txt")"
# The host firewall must be in force: this user needs no instance credentials, so the cloud metadata server must refuse it.
curl -s -m 5 -o /dev/null http://169.254.169.254/ 2>/dev/null && fwrc=0 || fwrc=$?
[ "$fwrc" = 7 ] || [ "$fwrc" = 56 ] || [ "$fwrc" = 35 ] || die 4 "firewall not in force (curl rc $fwrc to the metadata server)"
systemctl is-active -q epistemic-stack-fw.service || die 4 "firewall not in force (epistemic-stack-fw.service is not active)"
# Every host loopback listener a box could reach must still be Discobox's, passt's or ToolHive's (stack_test (j), now).
sudo -n ss -tlnpH > "$W/listeners.txt" 2>&1 && odd=$(python3 -I "$J/loopback_listeners.py" < "$W/listeners.txt") || die 4 "unexpected host listener(s) a box could reach: ${odd:-the listing failed}"

# Discobox runs a token's refresh command only when asked by name; wif-refresh.timer (scripts/lid/) asks every 4 min.
systemctl --user is-active -q wif-refresh.timer || die 4 "wif-refresh.timer not active: the Claude token would die 10 min in"
dbx secret refresh "$SEC" --run >/dev/null || die 4 "WIF token refresh failed"
[ -z "$TOOLS" ] || systemctl is-active -q "$TUNIT" || die 4 "--tools $TOOLS: $TUNIT not active on the lid (scripts/lid/stack_up.sh)"

stage preflight clone
if [ -n "$SRCTAR" ]; then
  # Private tree, uploaded beside the job: one commit, so base and the patch export work as for a clone.
  mkdir -p "$W/src" && tar -xzf "$J/src.tgz" -C "$W/src" --exclude=.git --exclude="*/.git" && ! find "$W/src" -name .git | grep -q . && git -C "$W/src" init -q && git -C "$W/src" add -A \
    && git -C "$W/src" -c user.name=job -c user.email=job@localhost commit -qm "src $REF" >> "$W/clone.log" 2>&1 || die 6 "src-tar unpack failed"
else
git -c credential.helper= clone -q "$REPO" "$W/src" >> "$W/clone.log" 2>&1 || die 6 "clone failed"
git -C "$W/src" -c advice.detachedHead=false checkout -q "$REF" >> "$W/clone.log" 2>&1 || die 6 "ref not found"
fi
base=$(git -C "$W/src" rev-parse HEAD); echo "$base" > "$OUT/base.txt"; log "-- base $base"

stage clone box
cd "$W/src"; python3 "$J/req.py" > "$W/req.json"
# The cap is atomic: the live count and the create happen under one lock, so parallel launches cannot all see room.
exec 9>>"$HOME/scratch/.box-cap.lock"
flock -w 1800 9 || die 5 "the box-cap lock was not free in 30 min"
live=$(dbx ls -o json | python3 -I -c 'import json,sys
def dead(b):
    r = b.get("runtime") or {}; st = r.get("state"); rt = r.get("runtimeState")
    return st in ("archived", "deleted") or (rt in ("stopped", "archived", "deleted") and st != "awaiting_source")
print(sum(1 for b in json.load(sys.stdin)["sandboxes"] or [] if not dead(b)))') \
  || die 5 "the box listing could not be read for the cap"
[ "$live" -ge "$MAXBOX" ] && die 5 "$MAXBOX boxes already live"
timeout -k 30 900 discobox new --from-json < "$W/req.json" > "$W/box.json" 2> "$W/box.err" || die 7 "discobox new failed"
flock -u 9
id=$(python3 -I -c "import json,sys;print(json.load(open(sys.argv[1]))['id'])" "$W/box.json") || die 7 "no box id"; log "-- box $id"
[ -z "$ALLOW" ] || dbx admin box update "$id" --egress-allow "$ALLOW" >/dev/null || die 7 "box egress update failed"
# 10 min: with several boxes provisioning at once on the pool, a shell can take over 3 min (11/10 dbx19-dry2, 11/17 fin-par2).
up=; for i in $(seq 1 120); do inbox mkdir -p /tmp/job </dev/null >/dev/null 2>&1 && { up=1; break; }; sleep 5; done
[ -n "$up" ] || die 7 "box shell never came up"
# Discobox >= 0.19 installs Claude Code (npm, latest) and node at boot, so the agent version floats per box
# (stack/GAPS.md G10). Record what this job actually ran: the harness bundle and manifest digests, and the versions in the box.
{ dbx admin harnesses get claude-code -o json | python3 -I -c 'import json,sys; d=json.load(sys.stdin); print("harness claude-code revision %s bundle %s manifest %s" % (d.get("revision"), d.get("bundleDigest"), d.get("manifestDigest")))'
  inbox bash -c 'v=$(claude --version 2>&1); echo "claude $(grep -m1 -E "^[0-9]+\.[0-9]+\.[0-9]+ \(Claude Code\)" <<<"$v" || head -1 <<<"$v")"; echo "node $(node --version 2>&1)"' </dev/null; } > "$OUT/versions.txt" 2>&1 || true   # a record; the claude check below decides
log "-- versions: $(tr '\n' ';' < "$OUT/versions.txt")"
# The record must show a runnable claude: a pool whose provisioned agent is broken fails every Claude job (11/18). One failed
# probe is a re-probe, not a broken agent.
claude_ok(){ grep -qE '^claude [0-9]+\.[0-9]+\.[0-9]+ \(Claude Code\)' "$1"; }
claude_ok "$OUT/versions.txt" || { sleep 10; quick bash -c 'v=$(claude --version 2>&1); echo "claude $(grep -m1 -E "^[0-9]+\.[0-9]+\.[0-9]+ \(Claude Code\)" <<<"$v" || head -1 <<<"$v")"' </dev/null > "$OUT/claude_recheck.txt" 2>&1; claude_ok "$OUT/claude_recheck.txt"; } || die 7 "claude does not run in the box ($(cat "$OUT/claude_recheck.txt" 2>/dev/null | head -1)): the pool's provisioned agent is broken; repair with scripts/lid/claude_canary.sh POOL --repair when no box is live (stack/GAPS.md G10)"
# Job commits are authored as the box owner (host.commit_name / host.commit_email in stack/site.yaml), never as the agent.
inbox bash -c "git config --global user.name $(printf %q "$GITNAME") && git config --global user.email $(printf %q "$GITMAIL")" </dev/null || die 7 "git identity failed"

stage box setup
if [ -s "$J/setup.sh" ]; then inbox bash -s < "$J/setup.sh" > "$OUT/setup.log" 2>&1 || die 8 "setup failed, see out/setup.log"; fi

stage setup agent
if [ -n "$TOOLS" ]; then
  printf '{"mcpServers":{"%s":{"type":"http","url":"%s"}}}\n' "$TOOLS" "$TURL" | inbox bash -c 'cat > /tmp/job/mcp.json' || die 9 "mcp config failed"
  log "-- tools: $TOOLS at $TURL (box egress allow: $ALLOW)"
fi
{ cat "$J/prompt.md"; printf '\n\n---\nWhen you are completely finished: write your report to /tmp/job/REPORT.md, commit any changes the task asked for, then run `touch /tmp/job/DONE`. Do not create DONE until everything else is finished.\n'; } | inbox bash -c 'cat > /tmp/job/PROMPT.md' || die 9 "prompt copy failed"
# Headless run, detached inside the box: one pass, and one --continue nudge if it ends without DONE. The sentinel the harness
# templated into .credentials.json goes in as ANTHROPIC_AUTH_TOKEN; Discobox swaps it for the real token on the way out.
inbox bash -s <<'RUN' > "$W/launch.log" 2>&1 || die 9 "agent launch failed"
cat > /tmp/job/agent.sh <<'A'
#!/bin/bash
. /tmp/job/env.sh 2>/dev/null
cd "$1" || exit 1
export CLAUDE_CONFIG_DIR=/tmp/job/cc ANTHROPIC_AUTH_TOKEN=$(jq -r .claudeAiOauth.accessToken ~/.claude/.credentials.json)
# Builds compile for minutes; the 2-minute default Bash timeout killed coder-probe's sqlc install (exit 137).
export BASH_DEFAULT_TIMEOUT_MS=1800000 BASH_MAX_TIMEOUT_MS=3600000
# Watchdog: coder-probe's claude sat 50 min with no output and no tool running. Kill after 10 idle minutes; the nudge resumes.
MCP=(); [ -f /tmp/job/mcp.json ] && MCP=(--mcp-config /tmp/job/mcp.json --strict-mcp-config)
[ -n "${EPI_PLUGIN_DIR:-}" ] && MCP+=(--plugin-dir "$EPI_PLUGIN_DIR")
run(){
  claude -p "$@" ${MCP[@]+"${MCP[@]}"} --dangerously-skip-permissions --output-format stream-json --verbose >> /tmp/job/agent.jsonl 2>> /tmp/job/agent.err &
  local c=$! last=-1 idle=0 s busy
  while kill -0 "$c" 2>/dev/null; do
    sleep 60; s=$(stat -c %s /tmp/job/agent.jsonl 2>/dev/null || echo 0)
    if [ "$s" != "$last" ]; then last=$s; idle=0; continue; fi
    busy=$(pgrep -P "$c" | xargs -r ps -o args= -p | grep -cvE 'mcp-language-server|gopls')
    [ "$busy" -gt 0 ] && { idle=0; continue; }
    idle=$((idle+1)); [ "$idle" -ge 10 ] && { echo "stalled 10m with no tool running, killed" >> /tmp/job/agent.err; kill "$c"; }
  done; wait "$c"; }
run "Read /tmp/job/PROMPT.md and follow it exactly."
[ -f /tmp/job/DONE ] || { echo nudged >> /tmp/job/agent.err; run --continue "You stopped before finishing. Complete the task in /tmp/job/PROMPT.md, including REPORT.md and DONE."; }
echo $? > /tmp/job/agent.exit
A
chmod +x /tmp/job/agent.sh; mkdir -p /tmp/job/cc
# Not setsid/nohup: each `discobox shell` runs in a discobox-exec unit with KillMode=control-group and TasksMax=138, so
# a child dies with the unit and a Go build hits "fork failed". A unit of its own, carrying this shell's env (proxy vars).
export -p > /tmp/job/env.sh
sudo -n systemd-run --unit=job-agent --uid="$(id -u)" --gid="$(id -g)" -p TasksMax=infinity -p WorkingDirectory="$PWD" \
  --collect /tmp/job/agent.sh "$PWD" || exit 1
echo "agent unit job-agent in $PWD"
RUN
log "-- $(tail -1 "$W/launch.log")"
end=$(( $(date +%s) + TMIN*60 )); beat=$(date +%s); first_miss=; gone=0
# One bounded call per poll (quick: 60 s) reports the box's state as a token. A failed or slow call is a miss against a
# 5-minute clock, never read as "no DONE"; "gone" (the agent unit inactive with no DONE or agent.exit) must hold two polls.
while :; do
  now=$(date +%s)
  st=$(quick bash -c 'if [ -f /tmp/job/DONE ]; then echo done; elif [ -f /tmp/job/agent.exit ]; then echo exit; elif systemctl is-active -q job-agent; then echo running; else echo gone; fi' </dev/null 2>/dev/null)
  case "$st" in
    done) break;;
    exit) quick test -f /tmp/job/DONE </dev/null && break
      quick tail -5 /tmp/job/agent.err > "$OUT/agent_err_tail.txt" </dev/null 2>&1 || true; die 10 "agent ended without DONE";;
    running) first_miss=; gone=0;;
    gone) first_miss=; gone=$((gone + 1)); [ "$gone" -ge 2 ] && die 8 "agent unit gone without DONE (the box restarted under it?)";;
    *) first_miss=${first_miss:-$now}; [ $((now - first_miss)) -ge 300 ] && die 8 "box unreachable for 5 min";;
  esac
  [ "$now" -ge "$end" ] && die 124 "timed out after ${TMIN}m"
  [ $((now-beat)) -ge 300 ] && { log "-- working $(date -u +%T)"; beat=$now; }
  sleep 20
done

stage agent export
take "$OUT/agent.jsonl" inbox cat /tmp/job/agent.jsonl </dev/null 2>/dev/null || log "-- no agent.jsonl"
take "$OUT/REPORT.md" inbox cat /tmp/job/REPORT.md </dev/null 2>/dev/null || log "-- no REPORT.md"
take "$OUT/patches.tgz" inbox bash -s "$W/src" "$base" 2>"$W/export.err" <<'EXP' || die 11 "export failed"
cd "$1" || exit 1; p=/tmp/job/patches; mkdir -p $p
git status --short > /tmp/job/worktree_status.txt
for b in $(git for-each-ref --format='%(refname:short)' refs/heads) HEAD; do
  [ "$(git rev-list --count "$2..$b")" -gt 0 ] && git format-patch -q -o "$p/$(echo "$b" | tr / _)" "$2..$b" >/dev/null
done
tar -C /tmp/job -czf - patches worktree_status.txt
EXP
# Untrusted (the agent has root in its box): regular files and dirs only, never through an existing path (safe_untar.py).
U=$OUT/patches.unpacked; python3 "$J/safe_untar.py" "$OUT/patches.tgz" "$U" || die 11 "export unpack refused"
# Only the two names the export packs come back into out/; anything else in the archive stays in patches.unpacked.
mkdir -p "$U/patches"; mv "$U/patches" "$OUT/patches" && { [ -f "$U/worktree_status.txt" ] && mv "$U/worktree_status.txt" "$OUT/" || : > "$OUT/worktree_status.txt"; } && rm -f "$OUT/patches.tgz" || die 11 "export unpack failed"
c=$(find "$OUT/patches" -name '*.patch' | wc -l); log "-- $c patch files, $(wc -l < "$OUT/worktree_status.txt") uncommitted paths"
WORKER
cp "$D/../lid/route_lock.py" "$T/route_lock.py"; cp "$D/../lid/loopback_listeners.py" "$T/loopback_listeners.py"; cp "$D/disco_fan_lib/safe_untar.py" "$T/safe_untar.py"   # the one copy of the lock (ADR 26-11-10-001)
cat > "$T/preflight.py" <<'PY'
import json, os, subprocess, sys
# The lock is the Claude credential's route (route_lock.py). Harness egress is Discobox's own default and is only reported.
rc = subprocess.call([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'route_lock.py'), sys.argv[1]])
if rc: sys.exit('lockdown not in place')
eg = json.loads(subprocess.check_output(['discobox', 'admin', 'harnesses', 'get', 'claude-code', '-o', 'json'])).get('egress') or {}
print('harness egress', json.dumps(eg))
PY
cat > "$T/req.py" <<'PY'
import json, os, shlex
conf = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'job.conf')).read()
envs = shlex.split(conf.split('ENVS=(', 1)[1].rsplit(')', 1)[0])
# Discobox >= 0.19: `new --from-json` needs "source" (0.18's `new --json` took the cwd). The worker runs this from $W/src.
# The harness is named, never left to the project default: the route lock checks the claude-code harness's bindings.
print(json.dumps({'source': os.getcwd(), 'harness': 'claude-code', 'includeDirty': False, 'declaredSources': False, 'env': ['ENABLE_CLAUDEAI_MCP_SERVERS=false', *envs]}))
PY
cat > "$T/audit.py" <<'PY'
# Summary from Discobox's own audit. VIOLATION lines make the job fail: the lockdown broke.
import collections, json, subprocess, sys
box, out = sys.argv[1:3]
LIMIT = 1000000
raw = subprocess.check_output(['discobox', 'admin', 'audit', 'http', '--discobox', box, '--limit', str(LIMIT), '-o', 'json'], timeout=600)  # default limit is 1000
open(out + '/audit.json', 'wb').write(raw)
ex = json.loads(raw)['exchanges']   # a missing key raises, and a null is refused: the job reads VIOLATION, never 'clean'
if ex is None:
    sys.exit('audit has no exchanges list')
if len(ex) >= LIMIT:   # the API truncates silently at --limit: a full page may hide exchanges, so it is never 'clean'
    print(f'VIOLATION audit truncated at {LIMIT} exchanges'); sys.exit(1)
host = lambda e: e.get('host', '').split(':')[0]
ok = collections.Counter(host(e) for e in ex if not e.get('blocked'))
no = collections.Counter(host(e) for e in ex if e.get('blocked'))
msgs = [e for e in ex if '/v1/messages' in e.get('url', '')]
mcp = [e for e in ex if 'mcp-proxy' in e.get('host', '') or 'mcp_servers' in e.get('url', '')]
print(f'requests {len(ex)}, blocked {sum(no.values())}')
print(f'v1/messages {len(msgs)}, 200s {sum(e.get("status") == 200 for e in msgs)}')
print(f'mcp-proxy or mcp_servers {len(mcp)}')
print('allowed:', dict(ok.most_common())); print('blocked:', dict(no.most_common()))
for e in ex:
    if e.get('blocked') and e.get('blockedReason'): print('refused:', e.get('host'), e.get('blockedReason'))
if mcp: print('VIOLATION connector traffic in a locked box')
md = [e for e in ex if 200 <= (e.get('status') or 0) < 300 and (host(e) in ('169.254.169.254', 'metadata.google.internal') or host(e).endswith('.metadata.google.internal'))]   # metadata ANSWERED a box
if md: print(f'VIOLATION {len(md)} metadata-server request(s) answered for the box')
PY
B=$(cd "$T" && tar -czf - job.conf prompt.md setup.sh worker.sh preflight.py route_lock.py loopback_listeners.py safe_untar.py req.py audit.py | base64 | tr -d '\n')
if [ -n "$SRCTAR" ]; then
  "$LID" "set -e; W=~/scratch/$JOB; mkdir -p ~/scratch; mkdir \$W 2>/dev/null || { echo \"refuse: \$W already exists\" >&2; exit 3; }; mkdir \$W/job; cat > \$W/job/src.tgz" < "$SRCTAR" > "$T/upload.out" 2>&1 && urc=0 || urc=$?
  grep -v -e NumPy -e 'iap/docs' -e '^WARNING' -e '^$' "$T/upload.out" || true
  [ "$urc" -eq 0 ] || { echo "the --src-tar upload was refused or failed: not launching" >&2; exit 3; }
fi
"$LID" "set -e; W=~/scratch/$JOB; mkdir -p \$W/job; mkdir \$W/logs 2>/dev/null || { echo \"refuse: \$W already exists\" >&2; exit 3; }; echo $B | base64 -d | tar -xzf - -C \$W/job; nohup bash \$W/job/worker.sh > \$W/logs/worker.out 2>&1 < /dev/null & echo \"started $JOB\"" 2>&1 | grep -v -e NumPy -e 'iap/docs' -e '^WARNING' -e '^$'
echo "log on the lid: ~/scratch/$JOB/logs/run.log  (watch with boxwatch.sh)"
