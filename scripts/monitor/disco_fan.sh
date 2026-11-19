#!/bin/bash
# Usage: scripts/monitor/disco_fan.sh <job> <repo-dir> <sha> <cmd-script> --shards N [--workers W] [--deps requirements.txt] [--timeout-min M] [--require out-relpath]
#
# A command job (no agent) fanned out across N Discobox boxes on the build box (ADR 26-11-06-001). Launched from the desk via
# lid_run.sh only (the site's own box, host.name). Returns once the job runs as the system unit disco-fan-<job> on the lid; watch
# ~/scratch/<job>/logs/run.log (== <stage> / == <stage> exit N / == done PASS|FAIL) with boxwatch.sh, or read out/summary.json.
# The lid side is in disco_fan_lib/ (worker.sh, box_run.sh, safe_untar.py, lockcheck.py); see worker.sh for the guarantees.
#
# - Source: `git archive <sha>` of a LOCAL repo, copied into each box with `discobox cp`. Never a clone, never a credential.
# - Boxes: the built-in `shell` harness (0 bound secrets), --egress-deny-by-default from creation, the shard as user `job`.
# - The shard's result is the unit's exit status as systemd records it, never a file the job can write.
# - Results come back as an untrusted archive and are unpacked strictly (safe_untar.py).
# - --deps: a hash-pinned requirements file; nothing the lock doesn't pin and hash gets in (lockcheck.py).
# - Capacity: shards x workers <= 2 x the pool vCPU in stack/stack.yaml; beyond that the job is refused.
# - Any egress audit exchange fails the job. Every box is stopped, audited and archived on every path.
# - --require <relpath>: a shard that exits 0 without writing OUT/<relpath> (e.g. junit.xml) fails.
# - Limit, by design: the driver guarantees isolation and a result the job can't forge after it exits. It can't make a
#   malicious job tell the truth about its own run; code under test is trusted to report itself, never to reach out.
set -euo pipefail
D=$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd); LID=$D/../lid_run.sh; ROOT=$D/../..
JOB=${1:?job}; REPO=${2:?repo dir}; SHA=${3:?sha}; CMD=${4:?cmd script}; shift 4
SHARDS=; WORKERS=1; DEPS=; TMIN=30; REQUIRE=
while [ $# -gt 0 ]; do case "$1" in
  --shards) SHARDS=$2; shift 2;; --workers) WORKERS=$2; shift 2;; --deps) DEPS=$2; shift 2;; --timeout-min) TMIN=$2; shift 2;; --require) REQUIRE=$2; shift 2;;
  *) echo "unknown arg: $1" >&2; exit 2;; esac; done
[[ $JOB =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]] || { echo "bad job name (a-z 0-9 -, at most 41)" >&2; exit 2; }
for n in "$SHARDS" "$WORKERS" "$TMIN"; do [[ $n =~ ^[1-9][0-9]{0,3}$ ]] || { echo "--shards, --workers, --timeout-min: integers 1-9999" >&2; exit 2; }; done
[ -f "$CMD" ] || { echo "no cmd script" >&2; exit 2; }
[ -z "$REQUIRE" ] || [[ $REQUIRE =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$ && $REQUIRE != *..* ]] || { echo "bad --require path" >&2; exit 2; }
git -C "$REPO" rev-parse --verify --quiet "$SHA^{commit}" >/dev/null || { echo "not a commit in $REPO: $SHA" >&2; exit 2; }
if [ -n "$DEPS" ]; then
  [ -f "$DEPS" ] || { echo "no deps file" >&2; exit 2; }
  grep -q -- '--hash=sha256:' "$DEPS" || { echo "--deps must be hash-pinned (e.g. uv export --format requirements-txt)" >&2; exit 2; }
fi
VCPU=$(cd "$ROOT" && python3 -I -c '
import sys; sys.path.insert(0, "scripts/lid"); from stack_site import load; m = load("stack/stack.yaml")
p = [c for c in m["components"] if c["name"] == "discobox"][0]["pool"]; print(int(p["vcpus"]), int(p["max_boxes"]))') \
  || { echo "no discobox pool vcpus/max_boxes in stack/stack.yaml" >&2; exit 2; }
CAP=${VCPU#* }; VCPU=${VCPU% *}
# Two limits: CPU (shards x workers <= 2 x pool vCPU) and boxes (the pool's measured ceiling, pool.max_boxes). CAP goes to
# job.conf: the worker counts live boxes plus its shards against it, under the box-cap lock.
[ $((SHARDS * WORKERS)) -le $((2 * VCPU)) ] || { echo "refuse: $SHARDS shards x $WORKERS workers = $((SHARDS * WORKERS)) > 2 x pool vCPU $VCPU (stack/stack.yaml)" >&2; exit 2; }
[ "$SHARDS" -le "$CAP" ] || { echo "refuse: $SHARDS shards > box cap $CAP (pool.max_boxes, measured; stack/stack.yaml)" >&2; exit 2; }

T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
git -C "$REPO" archive --format=tar "$SHA" > "$T/src.tar"
cp "$CMD" "$T/cmd.sh"; if [ -n "$DEPS" ]; then cp "$DEPS" "$T/req.lock.txt"; fi
printf 'JOB=%q\nCAP=%q\nSHA=%q\nSHARDS=%q\nWORKERS=%q\nTMIN=%q\nDEPS=%q\nREQUIRE=%q\n' "$JOB" "$CAP" "$(git -C "$REPO" rev-parse "$SHA")" "$SHARDS" "$WORKERS" "$TMIN" "${DEPS:+1}" "$REQUIRE" > "$T/job.conf"
cp "$D"/disco_fan_lib/{worker.sh,box_run.sh,safe_untar.py,lockcheck.py,pip.lock} "$D/../lid/loopback_listeners.py" "$T/"

tar -C "$T" -cf - . | bash "$LID" "set -e; W=\$HOME/scratch/$JOB; mkdir -p \$HOME/scratch; mkdir \"\$W\" 2>/dev/null || { echo \"refuse: \$W exists\" >&2; exit 3; }
mkdir -p \"\$W/job\" \"\$W/logs\" \"\$W/out\"; tar -C \"\$W/job\" -xf -
sudo -n systemd-run --unit=disco-fan-$JOB --uid=\$(id -u) --gid=\$(id -g) --setenv=HOME=\$HOME --collect -p KillMode=mixed -p TimeoutStopSec=4h bash \"\$W/job/worker.sh\" >/dev/null
echo \"launched disco-fan-$JOB: \$W/logs/run.log\""
