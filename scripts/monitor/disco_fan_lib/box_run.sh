#!/bin/bash
# Runs inside one box as the systemd unit fan-shard, as ROOT; everything the job supplies runs as the unprivileged user `job`.
# Its EXIT STATUS is the shard's result: the worker reads it from systemd (ExecMainStatus), never from a file the job can write.
# This script, the command and env are root-owned and read-only to the job. When the command returns, every remaining `job`
# process is killed BEFORE this exits, so nothing the job left behind can change out/ (or anything else) after the result.
cd /tmp/job && . ./env || exit 95
as_job(){ runuser -u job -- "$@"; }
rc=0
as_job tar -C src -xf src.tar || exit 96
VENV=
if [ -f wheelhouse.tgz ]; then
  as_job tar -C wheelhouse -xzf wheelhouse.tgz || exit 97
  ( cd wheelhouse && as_job sha256sum -c --quiet SHA256SUMS ) >> out/install.log 2>&1 || exit 97
  as_job python3 -m venv --clear /tmp/job/venv >> out/install.log 2>&1 || exit 97
  as_job /tmp/job/venv/bin/pip install -q --no-index --find-links wheelhouse --require-hashes --only-binary=:all: --no-deps \
    -r wheelhouse/req.txt >> out/install.log 2>&1 || exit 97
  VENV=/tmp/job/venv
fi
cd /tmp/job/src
timeout "${TMIN}m" runuser -u job -- env SHARD_INDEX="$SHARD_INDEX" SHARD_COUNT="$SHARD_COUNT" WORKERS="$WORKERS" \
  OUT=/tmp/job/out VENV="$VENV" HOME=/home/job PATH="${VENV:+$VENV/bin:}$PATH" bash /tmp/job/cmd.sh \
  > /tmp/job/out/cmd.log 2>&1 || rc=$?
pkill -KILL -u job 2>/dev/null; sleep 1; pkill -KILL -u job 2>/dev/null
exit "$rc"
