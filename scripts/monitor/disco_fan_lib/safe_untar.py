"""Unpack a box-supplied (untrusted) .tgz into a NEW directory.

Only regular files and directories; relative paths; no "..", no absolute paths, no symlinks, hard links, devices or FIFOs.
Files are created with O_EXCL|O_NOFOLLOW, so nothing is ever written through an existing path. The destination must not exist.
Exit non-zero, having written nothing further, on the first refused member. (Python 3.11.2 on the lid has no tarfile data filter.)
tarfile parses extended headers before a member reaches the loop below, and on 3.11 its pax parser can be driven to GBs
of memory or minutes of CPU by one small header. The export is GNU tar's default format, which writes GNU long-name
headers and never pax, so every header is checked before tarfile parses it: pax headers (local, global, Solaris) and
GNU sparse members are refused; a long-name header must be 0..MAX_XHDR bytes (a path up to PATH_MAX), and at most two
may precede a member (a long link name, then a long name). A corrupt or truncated header anywhere refuses the archive
(tarfile would otherwise end it there, silently). Members are not kept after they are handled.
Usage: python3 safe_untar.py <archive.tgz> <new-dest-dir>
"""
import os
import sys
import tarfile

MAX_BYTES = 2 * 1024 ** 3  # a shard's whole output, unpacked
MAX_MEMBERS = 200_000      # and its file count (inodes on the lid)
MAX_XHDR = 4096 + 1        # one GNU long-name header: PATH_MAX and its NUL; a longer name could not be extracted anyway
PAX_TYPES = (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE)
LONG_TYPES = (tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK)


class CappedInfo(tarfile.TarInfo):
    """Checks each header's type and declared size before tarfile reads anything that follows it."""

    # Every header block is parsed here: through frombuf on upstream releases, through _frombuf on those carrying the
    # 2025 tarfile security patches (Debian's 3.11.2 on the lid host among them), whose frombuf only calls _frombuf.
    @classmethod
    def frombuf(cls, *args, **kwargs):
        return cls._refuse_bad_header(super().frombuf, *args, **kwargs)

    @classmethod
    def _frombuf(cls, *args, **kwargs):
        return cls._refuse_bad_header(super()._frombuf, *args, **kwargs)

    @staticmethod
    def _refuse_bad_header(parse, *args, **kwargs):
        try:
            return parse(*args, **kwargs)
        except (tarfile.InvalidHeaderError, tarfile.TruncatedHeaderError) as e:
            raise SystemExit(f"refused archive: bad header: {e}")

    def _proc_member(self, tarfile_):
        if self.type in PAX_TYPES:
            raise SystemExit(f"refused archive: pax extended header type={self.type!r}")
        if self.type == tarfile.GNUTYPE_SPARSE:
            raise SystemExit(f"refused archive member: {self.name!r} type={self.type!r}")
        if self.type not in LONG_TYPES:
            return super()._proc_member(tarfile_)
        if not 0 <= self.size <= MAX_XHDR:
            raise SystemExit(f"refused archive: extended header of {self.size} bytes")
        depth = getattr(tarfile_, "xhdr_depth", 0)
        if depth >= 2:
            raise SystemExit("refused archive: chained extended headers")
        tarfile_.xhdr_depth = depth + 1
        try:
            return super()._proc_member(tarfile_)
        finally:
            tarfile_.xhdr_depth = depth


def main(src: str, dest: str) -> None:
    os.makedirs(dest, exist_ok=False)
    total = count = 0
    with tarfile.open(src, "r:gz", tarinfo=CappedInfo) as t:
        while (m := t.next()) is not None:
            # tarfile keeps every member it has read; nothing here needs the earlier ones
            t.members.clear()
            count += 1
            if count > MAX_MEMBERS:
                raise SystemExit("refused archive: too many members")
            parts = [p for p in m.name.split("/") if p not in ("", ".")]
            if m.name.startswith("/") or ".." in parts or not parts or not (m.isreg() or m.isdir()):
                raise SystemExit(f"refused archive member: {m.name!r} type={m.type!r}")
            if m.size < 0:
                raise SystemExit(f"refused archive member: {m.name!r} has a negative size")
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
    try:
        main(sys.argv[1], sys.argv[2])
    except (tarfile.TarError, EOFError, ValueError, RecursionError, OSError) as e:
        raise SystemExit(f"refused archive: {type(e).__name__}: {e}")
