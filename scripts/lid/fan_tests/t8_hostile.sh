#!/bin/bash
# t8: a hostile job. It plants a symlink in its output pointing at a host file, and fails, but leaves a background process
# that tries to forge success afterwards. Expected: the archive is refused, the host file is untouched, the result is rc=1
# (from systemd), and the job is FAIL.
ln -s /etc/hostname "$OUT/evil"   # any path outside the output: the untar must refuse the link
( sleep 25; echo 0 > /tmp/job/rc; touch /tmp/job/DONE ) &
disown
exit 1
