#!/bin/bash
# decoy_proof.sh: prove the Claude credential can't leave the box for anywhere but api.anthropic.com (ADR 26-11-10-001).
# Run from the desk: `bash scripts/lid/decoy_proof.sh`. It ships itself to the box (stack_lib.sh) and runs there.
# A claude-code box (the harness that holds the WIF binding) sends its OWN sentinel to a sink on the host, as Bearer, as
# x-api-key and inside a JSON body, plus a no-credential control. The sink stores only sha256 prefixes and whether a value has
# the real token's shape, never a value. PASS = every place the sentinel reached the sink it arrived unchanged (or the proxy
# refused it), the control reached the sink (so a block can be told from a swap), and 0 connector requests.
# Repeat on every Discobox pin move (ADR 26-11-10-001 § Verification). One temporary iptables rule is removed on every exit.
# First kept in the repo 11/17; before that it lived in a desk scratchpad (11/10 dbx19-decoy3).
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/stack_lib.sh"
stack_ship decoy_proof.sh "$@"

# ---- on the box ----
export XDG_RUNTIME_DIR=/run/user/$(id -u) PATH=$HOME/.local/bin:$PATH
R=$HOME/stack/repo; Y=$R/stack/stack.yaml; LB=$R/scripts/lid
get(){ python3 "$LB/stack_units.py" "$Y" get "$1"; }
IP=$(get host.internal_ip); SEC=$(get components.discobox.claude_secret); PORT=18777
W=$HOME/scratch/decoy-$(date -u +%Y%m%dT%H%M%S); L=$W/logs/run.log; mkdir -p $W/logs $W/out; : > $W/out/sink.jsonl
log(){ echo "$(date -u +%FT%TZ) $*" >> $L; echo "$*"; }
RULE=(EPI_STACK_OUTPUT -d $IP/32 -p tcp -m owner --uid-owner $(id -u) -m tcp --dport $PORT -j RETURN)
id=; sink=; stage=setup
rm_box(){ local t st
  for t in 1 2 3 4 5 6; do timeout 600 discobox rm "$1" >/dev/null 2>&1
    sleep 5; st=$(timeout 300 discobox ls -o json 2>/dev/null | python3 -I -c 'import json,sys; i=sys.argv[1]; d=json.load(sys.stdin); print(next(((b.get("runtime") or {}).get("state") for b in d["sandboxes"] or [] if b["id"]==i), "gone"))' "$1")
    case "$st" in archived|deleted|gone) [ "$t" -gt 1 ] && echo "-- box $1 archived on try $t"; return 0;; esac
  done; echo "-- box $1 NOT removed (state $st after 6 tries): LEAK"; return 1; }
cleanup(){ rc=$?; trap - EXIT INT TERM HUP; trap '' PIPE   # a closed pipe never cuts the cleanup short
  # The temporary rule and the sink go first, before any Discobox call that could hang.
  while sudo iptables -w -C "${RULE[@]}" 2>/dev/null; do sudo iptables -w -D "${RULE[@]}"; done
  sudo iptables -w -C "${RULE[@]}" 2>/dev/null && log "-- TEMP RULE STILL PRESENT" || log "-- temp rule removed"
  [ -n "$sink" ] && kill "$sink" 2>/dev/null
  # A create cut short (its timeout, an interrupt) after the server made the box: remove what appeared under the lock.
  if [ -z "$id" ] && [ -n "${creating:-}" ]; then
    if made=$(new_boxes "$before"); then for b in $made; do rm_box "$b" > "$W/out/rm.txt" 2>&1 || { [ $rc -ne 0 ] || rc=6; why="test box $b leaked"; }; while read -r l; do log "$l"; done < "$W/out/rm.txt"; done
    else log "-- the listing could not be read after the create: possible leak"; [ $rc -ne 0 ] || rc=6; fi; fi
  [ -n "$id" ] && { timeout 600 discobox admin audit http --discobox "$id" --limit 100000 -o json > $W/out/audit.json 2>/dev/null; rm_box "$id" > "$W/out/rm.txt" 2>&1 || { [ $rc -ne 0 ] || rc=6; why="test box $id leaked"; }; while read -r l; do log "$l"; done < "$W/out/rm.txt"; }
  if [ $rc -eq 0 ]; then log "== $stage exit 0"; log "== done"; else log "== $stage exit $rc (${why:-unexpected})"; log "== done FAILED at $stage"; fi; exit $rc; }
trap cleanup EXIT; trap 'why=signal; exit 130' INT TERM HUP PIPE
die(){ why=$2; exit $1; }

log "== setup ($W)"
cat > $W/sink.py <<'PY'
import hashlib, http.server, json, re, sys
OUT, IP, PORT = sys.argv[1], sys.argv[2], int(sys.argv[3])
TOK = re.compile(r'sk-ant-oat01-[A-Za-z0-9_-]{95}')
def rec_of(v): return {'sha16': hashlib.sha256(v.encode()).hexdigest()[:16], 'len': len(v), 'token_shape': bool(TOK.fullmatch(v))}
class H(http.server.BaseHTTPRequestHandler):
    def handle_one(self, body):
        rec = {'path': self.path, 'method': self.command}
        for h in ('Authorization', 'x-api-key'):
            v = self.headers.get(h)
            if v is None: continue
            rec[h] = rec_of(v.split(' ', 1)[1] if h == 'Authorization' and ' ' in v else v)
        if body:
            try: rec['body_key'] = rec_of(json.loads(body).get('api_key', ''))
            except Exception: rec['body_key'] = {'unparsed': True, 'token_shape': bool(TOK.search(body))}
        open(OUT, 'a').write(json.dumps(rec) + '\n')   # hashes only; no value is ever stored
        self.send_response(204); self.end_headers()
    def do_GET(self): self.handle_one('')
    def do_POST(self): self.handle_one(self.rfile.read(int(self.headers.get('Content-Length') or 0)).decode('utf-8', 'replace'))
    def log_message(self, *a): pass
http.server.HTTPServer((IP, PORT), H).serve_forever()
PY
python3 $W/sink.py $W/out/sink.jsonl $IP $PORT & sink=$!
sudo iptables -w -I EPI_STACK_OUTPUT 1 "${RULE[@]:1}" || die 2 "temp rule insert failed"
python3 "$LB/route_lock.py" "$SEC" > $W/out/preflight.txt 2>&1 || die 2 "route lock not in place: $(tail -1 $W/out/preflight.txt)"
log "-- $(tail -1 $W/out/preflight.txt)"
log "== setup exit 0"

stage=box; log "== box"
# Under the shared box-cap lock (disco_job.sh): a canary repair never clears the cache between a count and this create.
mkdir -p "$HOME/scratch"; exec 9>>"$HOME/scratch/.box-cap.lock"; flock -w 1800 9 || die 3 "the box-cap lock was not free in 30 min"
room=$(box_room) || die 3 "$room"
before=$(box_ids) || die 3 "the box listing could not be read"; creating=1
echo '{"noSource":true,"harness":"claude-code","env":["ENABLE_CLAUDEAI_MCP_SERVERS=false"]}' | timeout -k 30 900 discobox new --from-json > $W/out/box.json 2> $W/out/box.err || die 3 "new failed: $(tail -1 $W/out/box.err)"
id=$(python3 -I -c "import json,sys;print(json.load(open(sys.argv[1]))['id'])" $W/out/box.json) || die 3 "no id"; creating=; flock -u 9; log "-- box $id"
for i in $(seq 1 60); do discobox shell "$id" -- true </dev/null >/dev/null 2>&1 && break; sleep 10; done
discobox shell "$id" -- true </dev/null >/dev/null 2>&1 || die 3 "box never answered"
discobox admin box update "$id" --egress-allow "$IP" >/dev/null || die 3 "egress allow failed"; sleep 20
log "== box exit 0"

stage=probe; log "== probe"
discobox shell "$id" -- bash -c '
IP='"$IP"'; P='"$PORT"'
S=$(jq -r .claudeAiOauth.accessToken ~/.claude/.credentials.json 2>/dev/null)
[ -n "$S" ] && [ "$S" != null ] || { echo "NO_SENTINEL"; exit 1; }
echo "sentinel_sha16 $(printf %s "$S" | sha256sum | cut -c1-16) len ${#S}"
echo "mcp_switch ${ENABLE_CLAUDEAI_MCP_SERVERS:-unset}"
c(){ rm -f /tmp/r; curl -s -m 15 -o /tmp/r -w %{http_code} "$@"; echo " rc=$? $(head -c 160 /tmp/r 2>/dev/null | tr -d "\n")"; }
# The metadata probes record only a status, a length and a hash, never a body.
meta(){ rm -f /tmp/r; curl -s -m 15 -o /tmp/r -w %{http_code} "$@"; echo " rc=$? len $([ -f /tmp/r ] && wc -c < /tmp/r || echo 0) sha16 $( { cat /tmp/r 2>/dev/null || :; } | sha256sum | cut -c1-16)"; rm -f /tmp/r; }
echo "bearer $(c -H "Authorization: Bearer $S" http://$IP:$P/bearer)"
echo "xkey $(c -H "x-api-key: $S" http://$IP:$P/xkey)"
echo "body $(c -X POST -H "content-type: application/json" -d "{\"api_key\":\"$S\"}" http://$IP:$P/body)"
echo "spoof $(c -H "Host: api.anthropic.com" -H "Authorization: Bearer $S" http://$IP:$P/spoof)"   # a Host that names the route, aimed at the sink
echo "control $(c http://$IP:$P/control)"
for m in instance/service-accounts/default/identity?audience=https://api.anthropic.com instance/service-accounts/default/token project/project-id; do echo "metadata_$(echo ${m%%\?*} | tr / _) $(meta -H Metadata-Flavor:Google http://169.254.169.254/computeMetadata/v1/$m)"; done
echo "internet $(c https://example.com)"
for hp in 127.0.0.1:22 127.0.0.1:25 192.168.127.1:22 192.168.127.1:25; do echo "hostlo_proxy_${hp//[.:]/_} $(c --max-time 8 http://$hp/)"; echo "hostlo_direct_${hp//[.:]/_} $(c --max-time 8 --noproxy "*" http://$hp/)"; done
' </dev/null > $W/out/probe.txt 2>&1 || die 4 "probe failed: $(tail -1 $W/out/probe.txt)"
sleep 5
log "== probe exit 0"

stage=judge; log "== judge"
discobox admin audit http --discobox "$id" --limit 100000 -o json > $W/out/audit.json 2>$W/out/audit.err || die 5 "audit could not be read: $(tail -1 $W/out/audit.err)"
python3 -I - $W/out <<'PY' > $W/out/RESULT.txt 2>&1; r=$?
import json, sys
o = sys.argv[1]
probe = dict(l.split(' ', 1) for l in open(o + '/probe.txt').read().splitlines() if ' ' in l)
sent = probe['sentinel_sha16'].split()[0]
sink = [json.loads(l) for l in open(o + '/sink.jsonl')]
ex = json.load(open(o + '/audit.json'))['exchanges']
if ex is None:
    sys.exit('audit has no exchanges list: unreadable, not clean')
if len(ex) >= 100000:   # the API truncates silently at --limit: a full page is never 'clean'
    sys.exit('audit truncated at 100000 exchanges: incomplete, not clean')
fails = []; by = {r['path']: r for r in sink}
for p, k in (('/bearer', 'Authorization'), ('/xkey', 'x-api-key'), ('/body', 'body_key'), ('/spoof', 'Authorization')):
    r = by.get(p)
    if r is None: print(f'{p}: refused before the sink (nothing swapped): {probe.get(p[1:], "")[:120]}')
    elif k not in r: fails.append(f'{p}: reached the sink without the value (test inconclusive)')
    # The sentinel is token-shaped by design (the secret's --format), so shape can't tell them apart; the hash does.
    elif r[k].get('sha16') != sent: fails.append(f'{p}: value at the sink is NOT the sentinel (swapped)')
    else: print(f'{p}: sink saw the sentinel unchanged')
if '/control' not in by: fails.append('control did not reach the sink (a block cannot be told from a swap)')
for k, v in probe.items():
    if k.startswith('hostlo_') and ('SSH-' in v or 'ESMTP' in v or v.split()[0] not in ('000', '403', '502')):
        fails.append(f'{k}: a host loopback service answered a box: {v[:80]}')
    if k.startswith('metadata_') and v.split()[0] == '200':
        fails.append(f'{k}: the GCE metadata server answered a box (200)')
if probe['mcp_switch'].strip() != 'false': fails.append('mcp switch not false')
mcp = [e for e in ex if 'mcp-proxy' in (e.get('host') or '') or 'mcp_servers' in (e.get('url') or '')]
if mcp: fails.append(f'{len(mcp)} connector requests')
print('audit exchanges:', len(ex))
print('FAIL:' if fails else 'PASS', *fails, sep='\n  ')
sys.exit(1 if fails else 0)
PY
cat $W/out/RESULT.txt
[ $r -eq 0 ] || die 5 "decoy proof FAILED, see $W/out/RESULT.txt"
log "-- $(tail -1 $W/out/RESULT.txt)"
