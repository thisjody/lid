#!/bin/bash
# t2/t3: (1) a direct connection has no route; (2) a job that finds the box's egress proxy (127.0.0.1:17008) and uses it is
# blocked there AND recorded, which must FAIL the job on its audit. (3) The box environment carries no credential.
env | grep -iE 'anthropic|claude|token|secret|api_key|password' | sed 's/=.*/=<set>/' > "$OUT/cred_env.txt"
curl -sS -m 10 https://pypi.org/simple/ -o /dev/null 2> "$OUT/direct.err"; echo "direct rc $?" >> "$OUT/probe.txt"
curl -sS -m 10 -x http://127.0.0.1:17008 https://pypi.org/simple/ -o /dev/null 2> "$OUT/proxy.err"; echo "proxy rc $?" >> "$OUT/probe.txt"
exit 0
