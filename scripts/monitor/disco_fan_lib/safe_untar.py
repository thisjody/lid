"""Unpack a box-supplied (untrusted) .tgz into a NEW directory.

Only regular files and directories; relative paths; no "..", no absolute paths, no symlinks, hard links, devices or FIFOs.
Files are created with O_EXCL|O_NOFOLLOW, so nothing is ever written through an existing path. The destination must not exist.
Exit non-zero, having written nothing further, on the first refused member. (Python 3.11.2 on the lid has no tarfile data filter.)
Usage: python3 safe_untar.py <archive.tgz> <new-dest-dir>
"""
import os
import sys
import tarfile

MAX_BYTES = 2 * 1024 ** 3  # a shard's whole output, unpacked
MAX_MEMBERS = 200_000      # and its file count (inodes on the lid)


def main(src: str, dest: str) -> None:
    os.makedirs(dest, exist_ok=False)
    total = count = 0
    with tarfile.open(src, "r:gz") as t:
        for m in t:
            count += 1
            if count > MAX_MEMBERS:
                raise SystemExit("refused archive: too many members")
            parts = [p for p in m.name.split("/") if p not in ("", ".")]
            if m.name.startswith("/") or ".." in parts or not parts or not (m.isreg() or m.isdir()):
                raise SystemExit(f"refused archive member: {m.name!r} type={m.type!r}")
            path = os.path.join(dest, *parts)
            if m.isdir():
                os.makedirs(path, exist_ok=True)
                continue
            total += m.size
            if total > MAX_BYTES:
                raise SystemExit("refused archive: over the size limit")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as f, t.extractfile(m) as r:
                while chunk := r.read(1 << 20):
                    f.write(chunk)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
