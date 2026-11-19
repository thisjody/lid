#!/bin/bash
# idle_shutdown.sh: runs on the build box every 5 min (root cron). Stops the box after 20 min idle.
# Idle means ALL of these hold (ADR 26-11-03-003; ADR 26-11-06-001 decision 6):
#   no ssh sessions; no active job-agent / job-* / disco-* unit; no live Discobox box; 1-min load < 0.5.
# Any error while checking counts as BUSY, never idle: a broken check must not power off a running job.
# --check: print busy/idle and the reasons, change nothing (no counter, no shutdown). Used by stack_test.
# The box is always off when not in use.
# cron runs this with PATH=/usr/bin:/bin, where runuser (/usr/sbin) is missing: the Discobox check then failed on every
# run and the box never powered off (found by the busy-reason log, 2026-11-18). Set the PATH here, never inherit it.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# The counter is root-only (/run), never in a world-writable dir.
S=/run/idle_shutdown.count
CHECK=; [ "${1:-}" = --check ] && CHECK=1
U=${IDLE_BOX_USER:-}   # set by the cron line stack_up writes (host.user); unset reads as busy
DISCOBOX=${IDLE_DISCOBOX:-/home/$U/.local/bin/discobox}
busy=0; why=()
w=$(who) || { busy=1; why+=(who-failed); }; [ -n "$w" ] && { busy=1; why+=(login); }
sp=$(pgrep -a -x 'sshd|sshd-session'); [ $? -le 1 ] || { busy=1; why+=(pgrep-failed); }; grep -q '@' <<<"$sp" && { busy=1; why+=(ssh); }
ja=$(systemctl show -p ActiveState --value job-agent 2>/dev/null) || { busy=1; why+=(unit-state-failed); }
case "$ja" in inactive|failed|"") ;; *) busy=1; why+=(job-agent);; esac
# A disco_job worker runs under nohup on the host (cloning, creating its box) before any box or unit exists.
jw=$(pgrep -f '/scratch/[^ ]*/job/worker\.sh'); [ $? -le 1 ] || { busy=1; why+=(pgrep-failed); }; [ -n "$jw" ] && { busy=1; why+=(disco-job-worker); }
units=$(systemctl list-units --type=service --state=active --no-legend --plain 'job-*' 'disco-*' 2>/dev/null) \
  || { busy=1; why+=(unit-check-failed); }
[ -n "$units" ] && { busy=1; why+=(job-unit); }
la=$(awk '{print $1}' /proc/loadavg 2>/dev/null); [ -n "$la" ] || { busy=1; why+=(loadavg-failed); }
awk -v l="${la:-9}" 'BEGIN{exit !(l>=0.5)}' && { busy=1; why+=(load); }
# Live Discobox boxes, with upstream's predicate (server/internal/model/model.go): awaiting_source, or a runtime that is
# starting/running/stopping on a box not archived or deleted. --auto-start-server=false: cron never starts a server.
uid=$(id -u "$U" 2>/dev/null)
boxes=$( { [ -n "$uid" ] && [ -x "$DISCOBOX" ] && runuser -u "$U" -- env XDG_RUNTIME_DIR="/run/user/$uid" HOME="/home/$U" \
  timeout 20 "$DISCOBOX" --auto-start-server=false ls -o json; } 2>/dev/null | python3 -I -c '
import json, sys
d = json.load(sys.stdin); n = 0
# Strict: a missing key raises, and the check reads busy. Anything not in a known-dead state counts as live, so an unknown
# state after a Discobox upgrade keeps the box up rather than powering it off.
for b in d["sandboxes"] or []:
    r = b["runtime"]; st = r["state"]; rt = r.get("runtimeState")   # nested under runtime (v0.18.0)
    dead = st in ("archived", "deleted") or (rt in ("stopped", "archived", "deleted") and st != "awaiting_source")
    if not dead:
        n += 1
print(n)' 2>/dev/null)
case "$boxes" in
  ''|*[!0-9]*) busy=1; why+=(discobox-check-failed);;
  0) ;;
  *) busy=1; why+=("discobox-boxes=$boxes");;
esac
if [ -n "$CHECK" ]; then
  if [ $busy = 1 ]; then echo "busy: ${why[*]}"; else echo "idle"; fi
  exit 0
fi
# Every run says why, in the journal (journalctl -t idle_shutdown), so a box that never idles can be explained.
if [ $busy = 1 ]; then echo 0 > $S; logger -t idle_shutdown "busy: ${why[*]}"; exit 0; fi
c=$(cat $S 2>/dev/null || echo 0); [[ $c =~ ^[0-9]+$ ]] || c=0   # read as a number, never shell-evaluated
n=$(( c + 1 )); echo $n > $S; logger -t idle_shutdown "idle $n/4"
if [ $n -ge 4 ]; then echo 0 > $S; logger "idle_shutdown: idle 20 min, powering off"; /sbin/shutdown -h now; fi
