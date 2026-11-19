#!/bin/bash
# stack_secret.sh: load one secret into ToolHive's encrypted store on the build box, then converge and test (ADR 26-11-03-002).
#   pbpaste | bash scripts/lid/stack_secret.sh github-readonly --expires YYYY-MM-DD
# The value is read from stdin (or typed, hidden, if stdin is a terminal) and goes straight down the ssh pipe into
# `thv secret set`. It is never in argv, an env var, a file, a log, or output. The name must be a `secret:` in stack.yaml.
# --expires records the token's expiry in stack/stack.yaml (local edit; commit it) so stack_check.sh warns 14 days ahead.
# Starts the box if it is off (the idle shutdown powers it off again later). Runs stack_up.sh then stack_test.sh.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/stack_lib.sh"
NAME=${1:?usage: stack_secret.sh <secret-name> [--expires YYYY-MM-DD] (value on stdin)}; shift
EXP=; while [ $# -gt 0 ]; do case "$1" in --expires) EXP=$2; shift 2;; *) echo "unknown arg: $1" >&2; exit 2;; esac; done
[[ $NAME =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "bad secret name" >&2; exit 2; }
[ -z "$EXP" ] || [[ $EXP =~ ^20[0-9]{2}-[01][0-9]-[0-3][0-9]$ ]] || { echo "--expires must be YYYY-MM-DD" >&2; exit 2; }
Y=$STACK_REPO/stack/stack.yaml
grep -qE "^ +secret: $NAME( |$)" "$Y" || { echo "refuse: '$NAME' is not a secret: in stack/stack.yaml" >&2; exit 2; }
stack_on_box && { echo "run this from the desk, not on the box" >&2; exit 2; }

# Read the value once, into a pipe-only variable; strip a trailing newline from pbpaste/echo. Never echoed.
if [ -t 0 ]; then read -rs -p "paste $NAME (hidden), then Enter: " V; echo; else V=$(cat); fi
V=$(printf %s "$V" | tr -d '\r\n')
[ -n "$V" ] || { echo "empty value, nothing stored" >&2; exit 2; }
case "$NAME" in github-*) [[ $V == github_pat_* ]] || { unset V; echo "refuse: not a fine-grained token (github_pat_...)" >&2; exit 2; };; esac

U=$STACK_REPO/scripts/lid/stack_units.py; P=(--project "$(python3 "$U" "$Y" get host.project)" --zone "$(python3 "$U" "$Y" get host.zone)"); BOXNAME=${LID_INSTANCE:-$(python3 "$U" "$Y" get host.name)}
st=$(gcloud compute instances describe "$BOXNAME" "${P[@]}" --format='value(status)')
if [ "$st" != RUNNING ]; then
  echo "box $st: starting (the idle shutdown powers it off again)"; gcloud compute instances start "$BOXNAME" "${P[@]}" --quiet >/dev/null 2>&1
  for i in $(seq 1 30); do bash "$STACK_REPO/scripts/lid_run.sh" true </dev/null >/dev/null 2>&1 && break; sleep 10; done
fi
# A box this started is left for the idle shutdown to power off (20 minutes after the last ssh), never stopped from here:
# a job another desk launched meanwhile would be killed, and this ssh session itself always reads busy.

# Make sure ~/stack is on the box (thv_prime.sh, the store), then send the value over stdin only.
bash "$STACK_REPO/scripts/lid/stack_up.sh" > /dev/null 2>&1 || true
printf %s "$V" | bash "$STACK_REPO/scripts/lid_run.sh" "export XDG_RUNTIME_DIR=/run/user/\$(id -u) PATH=\$HOME/.local/bin:\$PATH; \$HOME/stack/bin/thv_prime.sh && thv secret set $NAME >/dev/null && thv secret list | grep -qw $NAME && echo 'stored $NAME in the ToolHive encrypted store'" 2>&1 | stack_quiet
rc=${PIPESTATUS[1]}; unset V
[ "$rc" -eq 0 ] || { echo "store failed (rc $rc)" >&2; exit 1; }
[ -z "$EXP" ] || { sed -i '' -E "/secret: $NAME( |\$)/,/secret_expires:/ s/secret_expires: [^ #]*/secret_expires: $EXP/" "$Y"; echo "stack.yaml: secret_expires $EXP (commit it)"; }
bash "$STACK_REPO/scripts/lid/stack_up.sh"
bash "$STACK_REPO/scripts/lid/stack_test.sh"
