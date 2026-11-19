"""t1: the union of the shards' JUnit reports equals one host run, test for test (name + outcome)."""
import glob, sys, xml.etree.ElementTree as ET

def outcomes(paths):
    out = {}
    for p in paths:
        for tc in ET.parse(p).iter("testcase"):
            key = f"{tc.get('classname')}::{tc.get('name')}"
            kids = {c.tag for c in tc}
            o = "failed" if kids & {"failure", "error"} else "skipped" if "skipped" in kids else "passed"
            assert key not in out, f"test ran in two shards: {key}"
            out[key] = o
    return out

host = outcomes([sys.argv[1]])
fan = outcomes(sorted(glob.glob(sys.argv[2])))
only_h, only_f = sorted(set(host) - set(fan)), sorted(set(fan) - set(host))
diff = sorted(k for k in set(host) & set(fan) if host[k] != fan[k])
print(f"host {len(host)} tests, fan {len(fan)} tests; only host {len(only_h)}, only fan {len(only_f)}, outcome differs {len(diff)}")
for k in (only_h + only_f + diff)[:20]:
    print("  ", k, host.get(k), fan.get(k))
sys.exit(0 if not (only_h or only_f or diff) else 1)
