#!/bin/bash
# claude_canary.sh POOL [--repair]: does Claude Code run in a fresh Claude box on this pool? Runs on the box.
# Makes a credential-free claude-code box (no source), checks that `claude --version` prints a version, and always removes
# the box (confirmed from the listing). Exit 0 when the agent runs, 1 when it does not (or the check could not be made).
#
# Why (stack/GAPS.md G10, found 2026-11-18): Discobox provisions Claude Code once per pool and reuses it. On a fresh pool npm
# had silently skipped the package's platform-native optional dependency, so `claude` in every new box was the package's
# placeholder ("claude native binary not installed") and every Claude job failed. Clearing the pool's cache made it
# provision again. With --repair the canary does that once, and only when no other box is live on the pool, because the
# clear stops every box on it; then it checks again.
set -uo pipefail
POOL=${1:?usage: claude_canary.sh POOL [--repair]}; REPAIR=${2:-}
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)} PATH=$HOME/.local/bin:$PATH
W=$(mktemp -d); id=; creating=; before=
# The box is removed on every exit, an interrupt included (a leaked canary box would count as live forever after). An
# interrupt during the create finds the box by the listing diff, as a failed create does.
done_(){ trap - EXIT INT TERM
  if [ -n "$id" ]; then rm_box "$id"; elif [ -n "$creating" ]; then sweep_new; fi; rm -rf "$W"; }
trap done_ EXIT; trap 'exit 130' INT TERM
# The shared box-cap lock (scripts/monitor/disco_job.sh, disco_fan_lib/worker.sh): creates, and the repair's live count and
# cache clear, happen under it, so no job can make a box between the count and the clear.
mkdir -p "$HOME/scratch"; exec 9>>"$HOME/scratch/.box-cap.lock"
. "$(dirname "${BASH_SOURCE[0]}")/stack_lib.sh"   # box_room: the pool's measured box ceiling

listing(){ timeout 300 discobox ls -o json 2>/dev/null; }
rm_box(){ local t st
  for t in 1 2 3 4 5 6; do timeout 600 discobox rm "$1" >/dev/null 2>&1
    sleep 5; st=$(listing | python3 -I -c 'import json,sys; i=sys.argv[1]; d=json.load(sys.stdin); print(next(((b.get("runtime") or {}).get("state") for b in d["sandboxes"] or [] if b["id"]==i), "gone"))' "$1")
    case "$st" in archived|deleted|gone) return 0;; esac
  done; echo "canary box $1 NOT removed (state $st after 6 tries): LEAK"; return 1; }
# The idle check's predicate (scripts/monitor/disco_job.sh): a box is live unless archived, deleted or stopped.
live(){ listing | python3 -I -c 'import json,sys
def dead(b):
    r = b.get("runtime") or {}; st = r.get("state"); rt = r.get("runtimeState")
    return st in ("archived", "deleted") or (rt in ("stopped", "archived", "deleted") and st != "awaiting_source")
print(sum(1 for b in json.load(sys.stdin)["sandboxes"] or [] if not dead(b)))'; }

ids(){ listing | python3 -I -c 'import json,sys; print("\n".join(b["id"] for b in json.load(sys.stdin)["sandboxes"] or []))'; }
# Remove every box that appeared since $before (the canary holds the box-cap lock, so only its own box can have appeared).
sweep_new(){ local now made b
  now=$(ids) || { echo "the box listing could not be read after a failed create: possible leak (a claude-code box made at $(date -u +%T))"; return 1; }
  made=$(comm -13 <(sort <<<"$before") <(sort <<<"$now"))
  for b in $made; do rm_box "$b"; done; }

# One check: prints the version line and returns 0, or prints why and returns 1. The box is removed either way.
check(){ local v up= rc=1
  flock -w 1800 9 || { echo "the box-cap lock was not free in 30 min"; return 1; }
  room=$(box_room) || { flock -u 9; echo "$room"; return 1; }
  before=$(ids) || { flock -u 9; echo "the box listing could not be read"; return 1; }
  creating=1
  echo '{"noSource":true,"harness":"claude-code","env":["ENABLE_CLAUDEAI_MCP_SERVERS=false"]}' \
    | timeout -k 30 900 discobox new --from-json > "$W/box.json" 2> "$W/box.err"; rc=$?
  id=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$W/box.json" 2>/dev/null) || id=
  if [ "$rc" -ne 0 ] || [ -z "$id" ]; then
    # The server may have made the box before the create failed: remove whatever appeared (no job can create under the lock).
    id=; echo "box create failed: $(tail -1 "$W/box.err")"
    sweep_new; creating=; flock -u 9
    return 1
  fi
  creating=; flock -u 9; rc=1
  for i in $(seq 1 120); do timeout 60 discobox shell "$id" -- true </dev/null >/dev/null 2>&1 && { up=1; break; }; sleep 5; done
  if [ -z "$up" ]; then echo "box $id shell never came up"
  else
    # As jobs run it (bash -c, not a login shell); claude's stderr is merged in the box, and any line may carry the version.
    v=$(timeout 120 discobox shell "$id" -- bash -c 'claude --version 2>&1' </dev/null 2>/dev/null)
    if grep -qE '^[0-9]+\.[0-9]+\.[0-9]+ \(Claude Code\)' <<<"$v"; then echo "claude runs in a fresh box: $(grep -m1 -E '^[0-9]+\.[0-9]+\.[0-9]+ \(Claude Code\)' <<<"$v")"; rc=0
    else echo "claude does not run in a fresh box: $(tr '\n' ' ' <<<"${v:-no output}")"; fi
  fi
  rm_box "$id" || rc=1
  id=   # tried once, whatever the result: rm_box already printed any LEAK
  return $rc
}

check && exit 0
[ "$REPAIR" = --repair ] || exit 1
flock -w 1800 9 || { echo "repair skipped: the box-cap lock was not free in 30 min"; exit 1; }
n=$(live) || { echo "repair skipped: the box listing could not be read"; exit 1; }
[ "$n" = 0 ] || { echo "repair skipped: $n other box(es) live on the pool (clear-cache would stop them); run again when idle"; exit 1; }
timeout 900 discobox admin pool clear-cache "$POOL" >/dev/null 2>&1 || { echo "pool $POOL clear-cache failed"; exit 1; }
flock -u 9
echo "cleared pool $POOL's cache; checking again"
check && { echo "repaired"; exit 0; }
exit 1
