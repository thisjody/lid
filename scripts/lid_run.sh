#!/bin/bash
# lid_run.sh: run a command on the build box, over IAP ssh. The only path from the desk to the box.
# The box comes from stack/site.yaml (host.project, host.zone, host.name). LID_INSTANCE may name the box or host.rebuild_name
# (a replacement being stood up from nothing, ADR 26-11-17-002); any other name is refused. Before running anything it
# ssh-es in and asserts the remote hostname is that name, so a misconfig or a stale tunnel can never run on another machine.
# host.refuse_patterns: command substrings refused outright (for example another project's host names).
set -euo pipefail
H=$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)
site(){ python3 "$H/lid/stack_units.py" "$H/../stack/stack.yaml" get "host.$1" "${@:2}"; }
PROJECT=$(site project) && ZONE=$(site zone) && NAME=$(site name) && NEXT=$(site rebuild_name --optional) || { echo "lid_run: stack/site.yaml unreadable" >&2; exit 2; }
INSTANCE=${LID_INSTANCE:-$NAME}
[ "$INSTANCE" = "$NAME" ] || { [ -n "$NEXT" ] && [ "$INSTANCE" = "$NEXT" ]; } || { echo "REFUSE: LID_INSTANCE=$INSTANCE is not host.name or host.rebuild_name" >&2; exit 98; }
export LID_INSTANCE=$INSTANCE

CMD="$*"
if [ -z "$CMD" ]; then
  echo "usage: lid_run.sh <command to run on the build box>" >&2
  exit 2
fi

REFUSE=$(site refuse_patterns --optional) || { echo "lid_run: host.refuse_patterns unreadable" >&2; exit 2; }
set -f   # the patterns are words, never globs
for p in $REFUSE; do
  case "$CMD" in *"$p"*) echo "REFUSE: command contains '$p' (host.refuse_patterns)" >&2; exit 97 ;; esac
done
set +f

# Assert the remote host, THEN run the command, in a single session.
gcloud compute ssh "$INSTANCE" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap \
  --command "h=\$(hostname); if [ \"\$h\" != \"$INSTANCE\" ]; then echo \"REFUSE: remote host is \$h, not $INSTANCE — aborting\" >&2; exit 99; fi; $CMD"
