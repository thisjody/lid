#!/bin/bash
# A gate check as a disco_fan.sh command: every critical guard removed must turn its test red.
set -u
python3 tests/red/red_checks.py | tee "$OUT/red.txt"; exit "${PIPESTATUS[0]}"   # the checks' verdict, not tee's
