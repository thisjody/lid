"""The narrowed requirements may contain nothing the lock doesn't pin AND hash.

Every line of both files must be `name==version [; markers] --hash=sha256:<64 hex>...` (continuations joined); any other
requirement line (a direct URL, an editable, an unhashed pin, extras) is refused. Every (name, version) in the target must be in
the lock, and every target hash must be among the lock's hashes for that pin.
Usage: python3 lockcheck.py <lock.txt> <target.txt>
"""
import re
import sys

LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s;]+)\s*(?:;[^-]*)?((?:\s*--hash=sha256:[0-9a-f]{64})+)\s*$")


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse(path: str) -> dict:
    text = re.sub(r"\\\n", " ", open(path).read())
    reqs = {}
    for raw in text.splitlines():
        line = raw.split(" #")[0].strip()
        if not line or line.startswith("#"):
            continue
        m = LINE.match(line)
        if not m:
            raise SystemExit(f"unrecognized requirement line in {path}: {line[:120]!r}")
        reqs[(norm(m.group(1)), m.group(2))] = set(re.findall(r"sha256:([0-9a-f]{64})", m.group(3)))
    return reqs


def main(lock_path: str, target_path: str) -> None:
    lock, tgt = parse(lock_path), parse(target_path)
    extra = sorted(set(tgt) - set(lock))
    if extra:
        raise SystemExit(f"narrowed set adds pins not in the lock: {extra}")
    bad = sorted(k for k, h in tgt.items() if not h <= lock[k])
    if bad:
        raise SystemExit(f"hashes not in the lock for: {bad}")
    print(f"-- wheelhouse: {len(tgt)} pins, every pin and hash from the lock")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
