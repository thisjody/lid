#!/bin/bash
# Start the Discobox server only when no server process runs at all. Every other lid call passes
# --auto-start-server=false: a client that finds a busy server slow launches a second one, and the new server shuts the
# running one down, killing every live box's agent (seen 11/21 under six Claude jobs). Run at boot and every 15 s by
# discobox-ensure.timer, and by discobox_up.sh; a real crash is recovered within about half a minute.
# One run at a time: two runs that both see no server would both start one, and the second would shut the first down.
# A server is any process running discobox-server, with or without arguments, except its pool VM launcher child, which
# has the same name and outlives a killed server.
DISCOBOX=${DISCOBOX:-$HOME/.local/bin/discobox}
exec 9>"${XDG_RUNTIME_DIR:-/tmp}/discobox-ensure.lock"
flock -n 9 || exit 0
ps -u "$(id -u)" -o args= | grep -E '(^|/)discobox-server( |$)' | grep -qv -- ' __pool-vm-launcher' && exit 0
# The client never inherits the lock (fd 9), so nothing it starts can hold it, and it cannot hang past the unit's own
# timeout (KillMode=process would let a hung client outlive the unit).
timeout 120 "$DISCOBOX" admin providers ls 9>&- >/dev/null 2>&1 || true
