#!/bin/bash
# Sourced by the stack_*.sh scripts (ADR 26-11-03-001). On the desk, stack_ship copies stack/stack.yaml and scripts/lid to
# ~/stack/repo on the box (with stack/agentgateway/) and re-runs the calling script there through lid_run.sh (OUR box only). On the box it returns.
# stdin is NOT forwarded (</dev/null): nothing secret travels this path. stack_secret.sh has its own stdin path.
STACK_REPO=$(cd -P "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# On the box, the shipped repo sits at ~/stack/repo; the desk's checkout never does.
stack_on_box(){ [ "$STACK_REPO" = "$HOME/stack/repo" ]; }
stack_quiet(){ grep -v -e NumPy -e 'iap/docs' -e '^WARNING:' -e '^$' || true; }
stack_ship(){ # stack_ship <script> [args...]
  stack_on_box && return 0
  local s=$1; shift; local b
  # Tracked files only (plus the untracked site file): a stray cache in scripts/lid once doubled this one ssh argument
  # past Linux's 128 KB limit for a single argument.
  local f; f=$(git -C "$STACK_REPO" ls-files stack/stack.yaml stack/agentgateway stack/discobox scripts/lid) || { echo "stack_ship: $STACK_REPO is not a git checkout" >&2; exit 1; }
  # shellcheck disable=SC2086
  b=$(COPYFILE_DISABLE=1 tar -h -C "$STACK_REPO" -czf - stack/site.yaml $f 2>/dev/null | base64 | tr -d '\n')
  bash "$STACK_REPO/scripts/lid_run.sh" "set -e; mkdir -p ~/stack; rm -rf ~/stack/repo.new; mkdir ~/stack/repo.new; echo $b | base64 -d | tar -m --warning=no-unknown-keyword -xzf - -C ~/stack/repo.new; rm -rf ~/stack/repo; mv ~/stack/repo.new ~/stack/repo; bash ~/stack/repo/scripts/lid/$s $*" </dev/null 2>&1 | stack_quiet
  exit "${PIPESTATUS[0]}"
}

# Box ids from the listing (fails when it cannot be read).
box_ids(){ timeout 300 discobox --auto-start-server=false ls -o json 2>/dev/null | python3 -I -c 'import json,sys; print("\n".join(b["id"] for b in json.load(sys.stdin)["sandboxes"] or []))'; }
# new_boxes BEFORE: the ids that appeared since BEFORE, a listing taken under the box-cap lock (so only the caller's own
# create can have added one). Used when a create was cut short after the server made the box. Fails on an unreadable listing.
new_boxes(){ local now; now=$(box_ids) || return 1; comm -13 <(sort <<<"$1") <(sort <<<"$now"); }
# box_room: run under the box-cap lock before any create. Fails (with the reason on stdout) when the live boxes have reached
# the pool's measured ceiling (stack/stack.yaml pool.max_boxes: the next box would not start), or either cannot be read.
box_room(){ local cap n
  cap=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1] + "/scripts/lid"); from stack_site import load
m = load(sys.argv[1] + "/stack/stack.yaml"); print(int([c for c in m["components"] if c["name"] == "discobox"][0]["pool"]["max_boxes"]))' "$STACK_REPO") \
    || { echo "the box ceiling could not be read (stack/stack.yaml pool.max_boxes)"; return 1; }
  n=$(timeout 300 discobox --auto-start-server=false ls -o json 2>/dev/null | python3 -I -c 'import json,sys
def dead(b):
    r = b.get("runtime") or {}; st = r.get("state"); rt = r.get("runtimeState")
    return st in ("archived", "deleted") or (rt in ("stopped", "archived", "deleted") and st != "awaiting_source")
print(sum(1 for b in json.load(sys.stdin)["sandboxes"] or [] if not dead(b)))') || { echo "the box listing could not be read"; return 1; }
  [ "$n" -lt "$cap" ] || { echo "$n boxes live: at the pool's box ceiling ($cap, pool.max_boxes)"; return 1; }; }
