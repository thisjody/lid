#!/bin/bash
# A gate check as a disco_fan.sh command (deps: tests/requirements-mutation.txt): mutmut 3 over the three guard modules
# (pyproject.toml [tool.mutmut]). The result lists every mutant by status; survivors are triaged in VERIFICATION.md.
set -u
export HYPOTHESIS_PROFILE=red
python3 -m mutmut run --max-children 4 > "$OUT/mutmut_run.txt" 2>&1; rc=$?
python3 -m mutmut results --all true > "$OUT/mutmut_results.txt" 2>&1 || python3 -m mutmut results > "$OUT/mutmut_results.txt" 2>&1
# Class methods carry 'ǁ' in their names (xǁClassǁmethod__mutmut_N): match any non-space name, not only [A-Za-z0-9_.].
for m in $(grep -oE '^ *[^ :]+__mutmut_[0-9]+: survived' "$OUT/mutmut_results.txt" | awk -F: '{print $1}' | tr -d ' '); do
  echo "=== $m"; python3 -m mutmut show "$m"
done > "$OUT/mutmut_survivors.txt" 2>&1
tail -5 "$OUT/mutmut_run.txt"; grep -cE ': survived' "$OUT/mutmut_results.txt"
exit $rc
