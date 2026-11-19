#!/bin/bash
# stack_test.sh: the invariant tests for the composed stack (ADR 26-11-03-001 §4, 26-11-03-002 step 1). Run from the desk;
# it ships itself to the build box. Creates one credential-free shell sandbox, deny-by-default from creation (ADR 26-11-10-001;
# the Claude route lock is the preflight), with the allow on the box's own level with egress allowing ONLY the host address, then asserts (ADR 26-11-03-004:
# jobs reach ToolHive only through agentgateway on $IP:18800):
#   (a) no GitHub/ToolHive credential in the sandbox env (every process) or filesystem
#   (b) through the gateway: tools/list carries tools from BOTH servers; a time call and a github read call complete
#   (c) the sandbox still cannot reach the internet, nor any non-gateway port on the host
#   (d) through the gateway: no write tool is offered and a write call is refused
#   (e) Discobox's own audit of the sandbox: no hosted-connector traffic, nothing allowed beyond the host and Anthropic
#   (f) the sandbox cannot reach the ToolHive proxies directly by any route (host IP, loopback via the proxy, passt's
#       host-loopback mapping); the gateway is the only stack TCP listener on the host IP
#   (g) no GitHub credential in the gateway's config or process environment; the config is root-owned and unchanged
#   (h) the gateway's own user can open the ToolHive proxies and nothing else (no internet)
#   (i) the github server can actually read GitHub (ToolHive's isolated-network egress; not a gateway property)
#   (j) every high loopback listener a sandbox could reach is Discobox's, passt's or thv's
#   (k) Claude Code runs in a fresh Claude box (the pool's provisioned agent is not broken)
#   INFO: what the multiplexed initialize carries (expected: every open target's instructions; hide per caller with a target condition, ADR 004)
# Prints PASS/FAIL/SKIP per invariant; exits nonzero on any FAIL. The sandbox is always removed.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/stack_lib.sh"
stack_ship stack_test.sh "$@"

# ---- on the box ----
export XDG_RUNTIME_DIR=/run/user/$(id -u) PATH=$HOME/.local/bin:$PATH
R=$HOME/stack/repo; Y=$R/stack/stack.yaml; L=$R/scripts/lid; W=$(mktemp -d); id=; fails=0
get(){ python3 "$L/stack_units.py" "$Y" get "$1"; }
IP=$(get host.internal_ip)
port_of(){ python3 -I - "$Y" "$1" <<'PY'
import sys, yaml
sys.path.insert(0, __import__('os').path.join(__import__('os').path.dirname(sys.argv[1]), '..', 'scripts', 'lid')); from stack_site import load; m = load(sys.argv[1]); t = [c for c in m['components'] if c['name'] == 'toolhive'][0]
print([s for s in t['servers'] if s['name'] == sys.argv[2]][0]['port'])
PY
}
TP=$(port_of time); GP=$(port_of github); TB=$(get components.toolhive.bind)
GWP=$(get components.agentgateway.port); GWU=$(get components.agentgateway.user); AU=$(get components.agentgateway.unit)
GW=http://$IP:$GWP/mcp
res(){ printf '%-4s (%s) %s\n' "$1" "$2" "$3"; [ "$1" != FAIL ] || fails=$((fails+1)); }
rm_box(){ local t st
  for t in 1 2 3 4 5 6; do timeout 600 discobox rm "$1" >/dev/null 2>&1
    sleep 5; st=$(timeout 300 discobox ls -o json 2>/dev/null | python3 -I -c 'import json,sys; i=sys.argv[1]; d=json.load(sys.stdin); print(next(((b.get("runtime") or {}).get("state") for b in d["sandboxes"] or [] if b["id"]==i), "gone"))' "$1")
    case "$st" in archived|deleted|gone) [ "$t" -gt 1 ] && echo "-- box $1 archived on try $t"; return 0;; esac
  done; echo "-- box $1 NOT removed (state $st after 6 tries): LEAK"; return 1; }
# The test box is removed (and confirmed) before the verdict, so a leak fails the run.
cleanup(){ [ -n "$id" ] && { rm_box "$id" || { echo "FAIL (cleanup) test box $id leaked"; leaked=1; }; id=; }
  # A create cut short (its timeout, an interrupt) after the server made the box: remove what appeared under the lock.
  if [ -z "$id" ] && [ -n "${creating:-}" ]; then
    if made=$(new_boxes "$before"); then for b in $made; do rm_box "$b" || { echo "FAIL (cleanup) test box $b leaked"; leaked=1; }; done
    else echo "FAIL (cleanup) the listing could not be read after the create: possible leak"; leaked=1; fi; creating=; fi
  rm -rf "$W"; }
trap cleanup EXIT
# A signal is trapped, not left to the EXIT trap alone: bash then defers it until the foreground command (a box create)
# returns, so cleanup's sweep sees the box. An ssh drop over lid_run (no pty) closes stdout instead: SIGPIPE is ignored so
# the next write fails and set -e runs cleanup, rather than bash dying with no cleanup at all.
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
trap '' PIPE
inbox(){ discobox shell "$id" -- "$@"; }

echo "stack_test $(date -u +%FT%TZ) on $(hostname): thv $(thv version | awk '/^ToolHive/{print $2}'), discobox $(discobox version | awk 'NR==1{print $NF}'), agentgateway $(/usr/local/bin/agentgateway -V 2>/dev/null)"
# Same refusal as disco_job.sh's preflight: the lockdown must already be in place.
rl=$(python3 "$L/route_lock.py" "$(get components.discobox.claude_secret)") || { echo "FAIL (pre) $rl"; exit 1; }   # ADR 26-11-10-001
systemctl --user is-active -q thv-time.service || { echo "FAIL (pre) thv-time.service not active: run stack_up.sh"; exit 1; }
systemctl is-active -q "$AU" || { echo "FAIL (pre) $AU not active: run stack_up.sh"; exit 1; }

# ADR 26-11-10-001: Claude boxes now run on Discobox's own (open) egress defaults, so this test needs no credential and uses a
# credential-free shell box created deny-by-default, like disco_fan.sh; its refused boot-provisioning fetches pass (e).
# Under the shared box-cap lock (disco_job.sh): a canary repair never clears the cache between a count and this create.
mkdir -p "$HOME/scratch"; exec 9>>"$HOME/scratch/.box-cap.lock"; flock -w 1800 9 || { echo "FAIL (pre) the box-cap lock was not free in 30 min"; exit 1; }
room=$(box_room) || { echo "FAIL (pre) $room"; exit 1; }
before=$(box_ids) || { echo "FAIL (pre) the box listing could not be read"; exit 1; }; creating=1
timeout -k 30 900 discobox admin box create --name "stack-test-$(date +%s)" --harness shell --egress-deny-by-default --no-project-skills --wait -o json > "$W/box.json" 2> "$W/box.err" || { echo "FAIL (pre) box create: $(tail -1 "$W/box.err")"; exit 1; }
id=$(python3 -I -c "import json,sys;print(json.load(open(sys.argv[1]))['id'])" "$W/box.json"); creating=; flock -u 9
discobox admin box update "$id" --egress-allow "$IP" > /dev/null
up=; for i in $(seq 1 40); do inbox true </dev/null >/dev/null 2>&1 && { up=1; break; }; sleep 5; done
[ -n "$up" ] || { echo "FAIL (pre) sandbox $id shell never came up"; exit 1; }
sleep 15   # the box-level allow converges within 15s (observed 2026-10-30)
echo "sandbox $id, egress allow: $IP only; MCP endpoint $GW"
inbox bash -c 'cat > /tmp/mcp_probe.py' < "$L/mcp_probe.py"
inbox bash -c 'command -v python3 curl | tr "\n" " "; echo; env | grep -i "^https\?_proxy=" | sed "s#//[^@]*@#//***@#"' </dev/null > "$W/tools.txt" 2>&1 || true
sed 's/^/  sandbox: /' "$W/tools.txt"

# (a) no credential. Names only are printed, never values.
inbox bash -s > "$W/a.txt" 2>&1 <<'A' || true
n=0
for p in /proc/[0-9]*/environ; do tr '\0' '\n' < "$p" 2>/dev/null; done | cut -d= -f1 | sort -u | grep -E '^(GH_|GITHUB_|TOOLHIVE_|THV_|GITHUB_PERSONAL_ACCESS_TOKEN)' && n=1
for f in ~/.config/gh ~/.git-credentials ~/.netrc ~/.config/toolhive ~/.local/share/toolhive ~/.config/epistemic-stack ~/.docker/config.json /root/.config/gh /root/.git-credentials; do
  [ -e "$f" ] && { echo "path $f"; n=1; }; done
git config --global --get-regexp '^credential' && n=1
# Token-shaped strings in any small file outside the OS/package trees (guest Docker's /var/lib layers excluded; files < 2 MB).
sudo -n timeout 120 find /home /root /tmp /etc /run /opt /workspace /srv /var -xdev \( -path /var/lib/docker -o -path /var/lib/containerd -o -path /var/cache -o -path /var/lib/apt -o -path /var/lib/dpkg -o -name node_modules \) -prune -o -type f -size -2M -print0 2>/dev/null \
  | sudo -n timeout 120 xargs -0 grep -IlE 'gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}' 2>/dev/null | head -5 | sed 's/^/file /' | grep . && n=1
echo "scan-done $n"
A
if grep -q '^scan-done 0' "$W/a.txt"; then res PASS a "no GitHub/ToolHive credential in sandbox env (all processes) or filesystem"
else res FAIL a "credential indicators: $(grep -v scan-done "$W/a.txt" | tr '\n' ';')"; fi

# (b) MCP through the gateway, via the sandbox's egress proxy (the path Claude Code uses)
inbox python3 /tmp/mcp_probe.py "$GW" list </dev/null > "$W/b1.txt" 2>&1 || true
inbox python3 /tmp/mcp_probe.py "$GW" call time_get_current_time '{"timezone":"UTC"}' </dev/null > "$W/b2.txt" 2>&1 && b2=0 || b2=$?
inbox python3 /tmp/mcp_probe.py "$GW" call github_get_latest_release '{"owner":"agentgateway","repo":"agentgateway"}' </dev/null > "$W/b3.txt" 2>&1 && b3=0 || b3=$?
nt=$(grep -c '^time_' "$W/b1.txt" || true); ng=$(grep -c '^github_' "$W/b1.txt" || true)
rel=$(grep -oE 'tag_name[^,]{0,6}v[0-9.]+' "$W/b3.txt" | grep -oE 'v[0-9.]+' | head -1 || true)
# The github call counts for (b) when the github server answered it (a tool result, even isError); whether that server can
# reach api.github.com is invariant (i), a ToolHive-layer property.
ganswered=$(grep -q '"content"' "$W/b3.txt" && echo yes || echo no)
if [ "$nt" -gt 0 ] && [ "$ng" -gt 0 ] && [ "$b2" -eq 0 ] && [ "$ganswered" = yes ]; then
  res PASS b "gateway tools/list: $nt time + $ng github; time_get_current_time -> $(grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}' "$W/b2.txt" | head -1); github_get_latest_release routed to and answered by the github server"
else res FAIL b "via gateway: time=$nt github=$ng call(time) rc=$b2 call(github) rc=$b3 answered=$ganswered :: $(tail -1 "$W/b1.txt") / $(head -c 160 "$W/b3.txt")"; fi

# (c) no internet, no other host port. A target counts as reached only if it returned real content.
inbox bash -s "$IP" "$TP" > "$W/c.txt" 2>&1 <<'C' || true
t(){ local label=$1; shift; code=$(curl -sS -m 12 -o /dev/null -w '%{http_code}' "$@" 2>/dev/null); rc=$?; echo "$label rc=$rc http=$code"; }
t github.com:443-via-proxy https://github.com
t api.github.com:443-via-proxy https://api.github.com/zen
t example.com:80-via-proxy http://example.com/
t github.com:443-direct --noproxy '*' https://140.82.112.3 -k
t 1.1.1.1:443-direct --noproxy '*' https://1.1.1.1 -k
t host:22-via-proxy "http://$1:22/"
t host:22-direct --noproxy '*' "http://$1:22/"
t host:5355-direct --noproxy '*' "http://$1:5355/"
C
reached=$(awk '{split($2,a,"=");split($3,b,"="); if (a[2]==0 && b[2]!="403" && b[2]!="502" && b[2]!="503" && b[2]!="000") print $1}' "$W/c.txt" | tr '\n' ' ' || true)
if [ -z "$reached" ] && [ "$(wc -l < "$W/c.txt")" -ge 8 ]; then res PASS c "internet + other host ports unreachable: $(awk '{print $1"("$2","$3")"}' "$W/c.txt" | tr '\n' ' ')"
else res FAIL c "reached: ${reached:-?} :: $(tr '\n' ';' < "$W/c.txt")"; fi

# (d) the GitHub server is read-only, as seen through the gateway (github-mcp-server read-only mode + the gateway's CEL rule)
if systemctl --user is-active -q thv-github.service; then
  writes=$(sed -n 's/^github_//p' "$W/b1.txt" | grep -E '^(create|update|delete|merge|push|add|fork|assign|request|submit|dismiss|rerun|cancel|run|issue_write|sub_issue_write|.*_write$)' | tr '\n' ' ' || true)
  inbox python3 /tmp/mcp_probe.py "$GW" call github_issue_write '{"method":"create","owner":"example-owner","repo":"nonexistent","title":"stack_test"}' </dev/null > "$W/d2.txt" 2>&1 && drc=0 || drc=$?
  n=$(grep -c '^github_' "$W/b1.txt" || true)
  if [ "$n" -gt 0 ] && [ -z "$writes" ] && [ "$drc" -ne 0 ]; then res PASS d "$n github tools via gateway, all read; github_issue_write refused: $(head -c 160 "$W/d2.txt")"
  else res FAIL d "tools=$n writes=[${writes}] issue_write rc=$drc $(head -c 160 "$W/d2.txt")"; fi
else res SKIP d "thv-github.service not running: secret github-readonly not loaded (scripts/lid/stack_secret.sh)"; fi

# (e) Discobox's own audit of this sandbox
sleep 3
discobox admin audit http --discobox "$id" --limit 1000000 -o json > "$W/audit.json" 2>/dev/null || true
erc=0; python3 -I - "$W/audit.json" "$IP" > "$W/e.txt" <<'PY' || erc=$?
import collections, json, sys
ex = json.load(open(sys.argv[1]))['exchanges']   # a missing key or a null fails (e)
assert ex is not None, 'audit has no exchanges list'
h = lambda e: e.get('host', '').split(':')[0]
ok = collections.Counter(h(e) for e in ex if not e.get('blocked'))
mcp = [e for e in ex if 'mcp-proxy' in e.get('host', '') or 'mcp_servers' in e.get('url', '')]
extra = [k for k in ok if k not in (sys.argv[2], 'api.anthropic.com') and not k.endswith('.discobox.internal')]
print('mcp-proxy=%d allowed=%s blocked=%d extra=%s' % (len(mcp), dict(ok), sum(1 for e in ex if e.get('blocked')), extra))
sys.exit(1 if mcp or extra else 0)
PY
if [ "$erc" -eq 0 ] && grep -q 'mcp-proxy=0' "$W/e.txt" && grep -q 'extra=\[\]' "$W/e.txt"; then res PASS e "Discobox audit: $(cat "$W/e.txt")"
else res FAIL e "Discobox audit: $(cat "$W/e.txt")"; fi

# (f) the sandbox reaches ToolHive only through the gateway. Every direct route must fail; the gateway itself must answer.
inbox bash -s "$IP" "$TP" "$GP" "$GWP" > "$W/f.txt" 2>&1 <<'FF' || true
t(){ local label=$1; shift; code=$(curl -sS -m 12 -o /dev/null -w '%{http_code}' "$@" 2>/dev/null); rc=$?; echo "$label rc=$rc http=$code"; }
PX=${HTTP_PROXY:-$http_proxy}
GA=$(ip route 2>/dev/null | awk '/^default/{print $3; exit}'); GA=${GA:-192.168.127.1}
t toolhive-time:host-ip-via-proxy "http://$1:$2/mcp"
t toolhive-github:host-ip-via-proxy "http://$1:$3/mcp"
t toolhive-time:host-ip-direct --noproxy '*' "http://$1:$2/mcp"
t toolhive-time:loopback-via-proxy --noproxy '' -x "$PX" "http://127.0.0.1:$2/mcp"
t toolhive-github:localhost-via-proxy --noproxy '' -x "$PX" "http://localhost:$3/mcp"
t toolhive-time:passt-gw-$GA-direct --noproxy '*' "http://$GA:$2/mcp"
t toolhive-github:passt-gw-$GA-direct --noproxy '*' "http://$GA:$3/mcp"
t CONTROL-gateway:host-ip-via-proxy "http://$1:$4/mcp"
FF
freach=$(awk '$1 !~ /^CONTROL/ {split($2,a,"=");split($3,b,"="); if (a[2]==0 && b[2]!="403" && b[2]!="502" && b[2]!="503" && b[2]!="000") print $1}' "$W/f.txt" | tr '\n' ' ' || true)
fctl=$(awk '$1 ~ /^CONTROL/ {split($2,a,"=");split($3,b,"="); if (a[2]==0 && b[2]!="403" && b[2]!="502" && b[2]!="503" && b[2]!="000") print "ok"}' "$W/f.txt")
lst=$(sudo -n ss -ltnH | awk '{print $4}' | grep -E "^$IP:|:($TP|$GP)\$" | sort -u | tr '\n' ' ')
if [ -z "$freach" ] && [ "$fctl" = ok ] && [ "$(wc -l < "$W/f.txt")" -ge 8 ] && [ "$lst" = "$IP:$GWP $TB:$TP $TB:$GP " ]; then
  res PASS f "ToolHive unreachable from the sandbox except via the gateway: $(awk '{print $1"("$2","$3")"}' "$W/f.txt" | tr '\n' ' '); host listeners: $lst"
else res FAIL f "reached: ${freach:-none} control=${fctl:-FAILED} listeners=[$lst] :: $(tr '\n' ';' < "$W/f.txt")"; fi

# (g) no GitHub credential in the gateway's config or its process environment; the config is root-owned and unchanged.
ACFG=/etc/epistemic-stack/agentgateway.yaml; apid=$(systemctl show -p MainPID --value "$AU")
genv=$(sudo -n cat "/proc/$apid/environ" | tr '\0' '\n' | cut -d= -f1 | grep -E '^(GH_|GITHUB_|TOOLHIVE_|THV_)' | tr '\n' ' ' || true)
gtok=$( { sudo -n cat "$ACFG"; sudo -n cat "/proc/$apid/environ" | tr '\0' '\n'; } | grep -cE 'gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}' || true)
AGWC=$(mktemp); python3 "$L/stack_units.py" "$Y" render-agw "$R/stack/agentgateway/config.yaml" > "$AGWC"
gsame=$(sudo -n cmp -s "$AGWC" "$ACFG" && echo same || echo DIFFERS)
gown=$(sudo -n stat -c '%U:%G %a' "$ACFG")
gw=$(sudo -n -u "$GWU" test -w "$ACFG" && echo WRITABLE || echo read-only)
gnames=$(sudo -n cat "/proc/$apid/environ" | tr '\0' '\n' | cut -d= -f1 | sort | tr '\n' ',')
gself=$(cat "/proc/$apid/environ" >/dev/null 2>&1 && echo READABLE || echo unreadable)
gkey=$(python3 -I -c 'import sys,yaml; c=yaml.safe_load(open(sys.argv[1])); print("session-key-in-config" if "session" in (c.get("config") or {}) or "backendAuth" in str(c) else "none")' "$R/stack/agentgateway/config.yaml")
if [ -z "$genv" ] && [ "$gtok" = 0 ] && [ "$gsame" = same ] && [ "$gown" = "root:root 644" ] && [ "$gw" = read-only ] && [ "$gkey" = none ] && [ -n "$gnames" ] && [ "$gself" = unreadable ]; then
  res PASS g "gateway pid $apid: env names [$gnames] (no GH_/GITHUB_/TOOLHIVE_), $gself to uid $(id -u), 0 token-shaped strings in config+env; config $gsame as repo, $gown, $gw to $GWU; no session key or backendAuth in config"
else res FAIL g "env=[$genv] names=[$gnames] self=$gself tokens=$gtok config=$gsame owner=$gown $gw keys=$gkey"; fi

# (h) the gateway's user opens the ToolHive proxies and nothing else; this user (uid 1000) cannot open them at all.
h(){ local label=$1 code rc; shift; code=$("$@" -sS -m 8 -o /dev/null -w '%{http_code}' 2>/dev/null) && rc=0 || rc=$?; echo "$label rc=$rc http=$code"; }
{ h "$GWU->toolhive-time" sudo -n -u "$GWU" curl "http://$TB:$TP/mcp"
  h "$GWU->toolhive-github" sudo -n -u "$GWU" curl "http://$TB:$GP/mcp"
  h "$GWU->api.github.com" sudo -n -u "$GWU" curl https://api.github.com/zen
  h "$GWU->host:22" sudo -n -u "$GWU" curl "http://$IP:22/"
  h "uid$(id -u)->toolhive-time" curl "http://$TB:$TP/mcp"
  h "uid$(id -u)->toolhive-github" curl "http://$TB:$GP/mcp"; } > "$W/h.txt"
hok=$(awk '{split($2,a,"=");split($3,b,"="); up=(a[2]==0 && b[2]!="000"); want=($1 ~ /->toolhive/ && $1 !~ /^uid/); if (up!=want) print $1}' "$W/h.txt" | tr '\n' ' ')
if [ -z "$hok" ]; then res PASS h "gateway egress confined: $(awk '{print $1"("$2","$3")"}' "$W/h.txt" | tr '\n' ' ')"
else res FAIL h "unexpected: $hok :: $(tr '\n' ';' < "$W/h.txt")"; fi

# (i) the github server can actually read GitHub (ToolHive layer: its container egresses through thv's isolated network).
if systemctl --user is-active -q thv-github.service; then
  if [ -n "$rel" ]; then res PASS i "github_get_latest_release agentgateway/agentgateway -> $rel"
  else res FAIL i "github server cannot read GitHub: $(sed 's/\\"//g' "$W/b3.txt" | grep -oE 'failed to[^"]{0,200}' | head -1)"; fi
else res SKIP i "thv-github.service not running"; fi

# INFO, not an invariant. The multiplexed initialize merges every open target's instructions, and that text reaches the job's
# context. To hide a target per caller, use a target condition (nothing leaks then; re-tested 11/4, ADR 004).
inbox python3 /tmp/mcp_probe.py "$GW" init </dev/null > "$W/i.txt" 2>&1 || true
python3 -I - "$W/i.txt" <<'PY' || true
import json, re, sys
r = json.loads(open(sys.argv[1]).read() or '{}'); ins = r.get('instructions') or ''
print('INFO initialize via gateway: serverInfo=%s; instructions %d chars, upstream sections %s; mentions write tools: %s' % (
    r.get('serverInfo', {}).get('name'), len(ins), re.findall(r'^\[([a-z0-9_-]+)\]$', ins, re.M),
    sorted(set(re.findall(r"'((?:create|pull_request_review|add_comment)[a-z_]*)'", ins)))))
PY

# (j) host loopback: the box user (and so every sandbox, through passt) can reach high loopback ports, so every loopback
# listener above 1023 must belong to Discobox, passt or thv (whose proxies the firewall refuses to the box user anyway).
# The listing must itself succeed and list something; a failed or empty listing is a FAIL, never a PASS.
if ssout=$(sudo -n ss -tlnpH) && odd=$(printf '%s\n' "$ssout" | python3 -I "$L/loopback_listeners.py"); then
  echo "PASS (j) every high loopback or wildcard listener is Discobox's, passt's or thv's"
else
  echo "FAIL (j) unexpected listener(s) a sandbox could reach, or the listing or the check failed: ${odd:-no output}"; fails=$((fails + 1))
fi
# (k) Claude Code runs in a fresh Claude box (stack/GAPS.md G10). A check only: stack_up repairs.
if ko=$(bash "$L/claude_canary.sh" "$(get components.discobox.pool.name)" 2>&1); then res PASS k "$(tail -1 <<<"$ko")"
else res FAIL k "$(tr '\n' ' ' <<<"$ko")"; fi
cleanup
[ "$fails" -eq 0 ] && [ -z "${leaked:-}" ] && { echo "stack_test: ALL PASS (SKIPs listed above)"; exit 0; } || { echo "stack_test: $fails FAIL${leaked:+ + a leaked test box}"; exit 1; }
