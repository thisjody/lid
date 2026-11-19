#!/bin/bash
# A gate check as a disco_fan.sh command: the whole suite plus static checks, one JUnit report.
# Runs inside a deny-by-default Discobox box with the hash-pinned tests/requirements.txt installed (disco_fan.sh --deps).
set -u
rc=0
for f in $(git ls-files 2>/dev/null '*.sh' || find . -name '*.sh' -not -path './.git/*'); do bash -n "$f" || { echo "bash -n FAILED: $f"; rc=1; }; done > "$OUT/bash_n.txt" 2>&1
python3 -m py_compile $(find . -name '*.py' -not -path './.git/*') > "$OUT/py_compile.txt" 2>&1 || rc=1
python3 scripts/lid/route_lock.py --self-test > "$OUT/route_lock_self_test.txt" 2>&1 || rc=1
HYPOTHESIS_PROFILE=gate python3 -m pytest -q -p no:cacheprovider tests scripts/lid/fan_tests/test_fan_lib.py --junitxml="$OUT/junit.xml" -o junit_family=xunit2 > "$OUT/pytest.txt" 2>&1 || rc=1
tail -3 "$OUT/pytest.txt"
exit $rc
