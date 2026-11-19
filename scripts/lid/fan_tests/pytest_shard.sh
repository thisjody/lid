#!/bin/bash
# disco_fan.sh command: run this shard's share of tests/test_*.py (round-robin by file) with a JUnit report.
set -u
# A src/ layout isn't installed into the venv (no build backend offline): import it from the tree.
[ -d src ] && export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"
mapfile -t ALL < <(ls tests/test_*.py | sort)
MINE=(); for k in "${!ALL[@]}"; do [ $((k % SHARD_COUNT)) -eq "$SHARD_INDEX" ] && MINE+=("${ALL[$k]}"); done
printf '%s\n' "${MINE[@]}" > "$OUT/files.txt"
[ ${#MINE[@]} -gt 0 ] || exit 0
HYPOTHESIS_PROFILE=ci python -m pytest -q -p no:cacheprovider "${MINE[@]}" --junitxml="$OUT/junit.xml"
