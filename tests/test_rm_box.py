"""The fan cleanup's leak check (rm_box in scripts/monitor/disco_fan_lib/worker.sh), with a faked `discobox`.

`discobox rm` can return 0 and leave a box running (seen 2026-11-17). rm_box must confirm the archive from the listing,
retry, and when the box never archives, log a LEAK and add it to FAILS so the job's verdict is FAIL.
"""
import os
import re
import subprocess

WORKER = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'monitor', 'disco_fan_lib', 'worker.sh')


def rm_box_source():
    s = open(WORKER).read()
    m = re.search(r'^rm_box\(\)\{.*?^\s*done; log .*?; return 1; \}\n', s, re.S | re.M)
    assert m, 'rm_box not found in worker.sh'
    return m.group(0)


def run(states):
    """states: the box's listed state after each rm call (the fake's ls answers from this list, one per call)."""
    fake = f'''
STATES=({' '.join(states)}); N=0; FAILS=""
DB(){{ case "$1" in
  rm) N=$((N+1));;
  ls) i=$((N-1)); [ $i -ge ${{#STATES[@]}} ] && i=$((${{#STATES[@]}}-1)); printf '{{"sandboxes":[{{"id":"sbx_x","runtime":{{"state":"%s"}}}}]}}' "${{STATES[$i]}}";;
esac; }}
log(){{ echo "LOG $*"; }}
sleep(){{ :; }}
{rm_box_source()}
rm_box sbx_x; echo "rc=$? fails=[$FAILS] rm_calls=$N"
'''
    return subprocess.run(['bash', '-c', fake], capture_output=True, text=True).stdout


def test_archived_at_once():
    out = run(['archived'])
    assert 'rc=0' in out and 'fails=[]' in out and 'rm_calls=1' in out


def test_archived_on_retry_is_logged():
    out = run(['failed', 'archived'])
    assert 'rc=0' in out and 'archived on try 2' in out and 'fails=[]' in out


def test_never_archived_is_a_leak_and_fails_the_job():
    out = run(['failed'] * 8)
    assert 'rc=1' in out and 'LEAK' in out and 'leak-sbx_x' in out and 'rm_calls=6' in out


def test_gone_from_the_listing_counts_as_removed():
    fake_gone = run(['deleted'])
    assert 'rc=0' in fake_gone
