"""Every list a source publishes, kept in runs: a run's first list whole (its base), each later
list as a zstd difference against that base, never against another difference. Any list is
rebuilt from two files at most, and a damaged difference loses only its own list.

    <folder>/<base's stamp>/<base's stamp>.json.zst        the base, whole
    <folder>/<base's stamp>/<base's stamp>.copy.json.zst   its second copy
    <folder>/<base's stamp>/<stamp>.diff.zst               a later list, against the base

A stamp is when the source made the list, in UTC: 2026-09-29T063603Z, or
2026-09-29T063603.215Z when the source gives fractions of a second.

How long a run lasts is the data's to say, not a setting. A new list goes into the latest run
as a difference while that difference is no bigger than the run's average bytes per list so far
(both copies of the base and every difference, over the run's lists). The first list whose
difference is bigger starts a new run, whole: each difference carries every change since its
base, so past that point a new base costs less than going on. On GoatBots' 2025, a list a day,
this kept 16.4% of what whole lists take; the best fixed period (14 days) kept 16.5%. Mana Pool,
at about 48 lists a day, gets runs of under a day; Card Kingdom, at about 4, runs of days.

Two limits the data can't set. A run spans at most 30 days, so a base lost with its copy takes
at most a month of lists. And a zstd difference reaches back at most 2 GiB, its largest window:
a base and list bigger than that together start a new run.

The base is kept twice because it's the one file its whole run needs. Every file carries zstd's
checksum. Reading a base for a new difference reads both copies: a damaged one is set aside
(<name>.damaged-<UTC time>) and written again from the other. Every file is read back, and its
list checked against the list's SHA-256, before it counts as kept.
"""

import hashlib
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

if sys.version_info >= (3, 14):
    from compression import zstd
else:
    from backports import zstd

from riffle import times

LEVEL = 19  # half the bytes of level 9 on a 201 MB list, in about a minute (measured 2026-09-29)
MOST_DAYS = timedelta(days=30)  # the lists a base lost with its copy can take
REACH = 1 << 31  # bytes a difference reaches back: zstd's largest window
SMALLEST = 8  # bytes zstd needs in a base to make a difference against it
BASE, COPY, DIFF = ".json.zst", ".copy.json.zst", ".diff.zst"
_P = zstd.CompressionParameter


class Unverified(OSError):
    """A file written that doesn't read back as its list: the disk is at fault. It's set aside."""


class Damaged(OSError):
    """Neither copy of a run's base reads whole."""


@dataclass(frozen=True)
class Kept:
    stamp: str  # the list's name: when it was made, UTC
    path: Path  # its file: the base's first copy, or its difference
    kind: str  # "base" or "diff"
    size: int  # the list's own bytes, as served
    stored: int  # bytes kept for it: both copies of a base
    sha256: str  # of the list as served
    file_sha256: str  # of the file kept; a base's two copies are the same bytes
    notes: tuple[str, ...] = field(default=())  # damage found and dealt with on the way


def name(at: datetime) -> str:
    """The stamp a list made at `at` is kept under."""
    at = at.astimezone(UTC)
    fraction = f".{at.microsecond:06d}".rstrip("0") if at.microsecond else ""
    return f"{at:%Y-%m-%dT%H%M%S}{fraction}Z"


def parse(stamp: str) -> datetime | None:
    """When a stamp says its list was made; None for a name that isn't one."""
    for fmt in ("%Y-%m-%dT%H%M%SZ", "%Y-%m-%dT%H%M%S.%fZ"):
        try:
            return datetime.strptime(stamp, fmt).replace(tzinfo=UTC)
        except ValueError:
            pass
    return None


def stamp_of(path: Path) -> str | None:
    """The stamp a kept list's file is named by; None for anything else in a run (a base's copy,
    a file being written, one set aside)."""
    if path.name.endswith(COPY):
        return None
    for suffix in (DIFF, BASE):
        if path.name.endswith(suffix):
            return path.name.removesuffix(suffix)
    return None


def runs(folder: Path) -> list[Path]:
    """The folder's runs, oldest first."""
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_dir() and parse(p.name))


def kept(folder: Path) -> dict[str, Path]:
    """Every list kept in folder, by stamp: the file it's rebuilt from."""
    found: dict[str, Path] = {}
    for run in runs(folder):
        for path in run.iterdir():
            stamp = stamp_of(path)
            if stamp is not None:
                found[stamp] = path
    return found


def copies(run: Path) -> tuple[Path, Path]:
    """A run's base and its second copy."""
    return run / f"{run.name}{BASE}", run / f"{run.name}{COPY}"


def file_sha256(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _unpack(data: bytes, base: bytes | None = None) -> bytes:
    """A zstd frame's contents; ZstdError if it's damaged, cut off or against another base."""
    against = zstd.ZstdDict(base, is_raw=True).as_prefix if base is not None else None
    options: dict[int, int] = {zstd.DecompressionParameter.window_log_max: 31}
    reader = zstd.ZstdDecompressor(zstd_dict=against, options=options)
    out = reader.decompress(data)
    if not reader.eof:
        raise zstd.ZstdError("cut off")
    return out


def _pack(data: bytes, base: bytes | None = None) -> bytes:
    options: dict[int, int] = {_P.compression_level: LEVEL, _P.checksum_flag: 1}
    if base is None:
        return zstd.ZstdCompressor(options=options).compress(data, zstd.ZstdCompressor.FLUSH_FRAME)
    window = min(31, (len(base) + len(data)).bit_length())
    options |= {_P.window_log: window, _P.enable_long_distance_matching: 1}
    packer = zstd.ZstdCompressor(options=options, zstd_dict=zstd.ZstdDict(base, is_raw=True).as_prefix)
    return packer.compress(data, zstd.ZstdCompressor.FLUSH_FRAME)


def _write(path: Path, data: bytes) -> None:
    """Whole or not at all, and on the disk before it's read back."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    try:
        with part.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


def _set_aside(path: Path) -> Path:
    dest = path.with_name(f"{path.name}.damaged-{name(times.now())}")
    path.replace(dest)
    return dest


def read_base(run: Path) -> bytes:
    """A run's base as served, from whichever copy reads whole; Damaged if neither does."""
    for path in copies(run):
        try:
            return _unpack(path.read_bytes())
        except (OSError, zstd.ZstdError):
            continue
    raise Damaged(f"{run}: neither copy of its base reads whole")


def _base(run: Path) -> tuple[bytes, list[str]]:
    """A run's base for a new difference, and what was repaired: both copies are read, and a
    damaged or missing one is set aside and written again from the other."""
    whole: tuple[bytes, bytes] | None = None  # (file, list)
    bad = []
    for path in copies(run):
        try:
            raw = path.read_bytes()
            listed = _unpack(raw)
        except (OSError, zstd.ZstdError):
            bad.append(path)
            continue
        whole = whole or (raw, listed)
    if whole is None:
        raise Damaged(f"{run.name}: neither copy of its base reads whole")
    notes = []
    for path in bad:
        said = f"{path.name} was missing"
        if path.exists():
            said = f"{path.name} was damaged: set aside as {_set_aside(path).name}"
        _write(path, whole[0])
        notes.append(f"{said}, and written again from the other copy")
    return whole[1], notes


def _verify(path: Path, sha256: str, base: bytes | None = None) -> None:
    """Read a file back as its list; a file that doesn't is set aside and Unverified."""
    try:
        back = _unpack(path.read_bytes(), base)
        ok = hashlib.sha256(back).hexdigest() == sha256
    except (OSError, zstd.ZstdError):
        ok = False
    if not ok:
        aside = _set_aside(path) if path.exists() else path
        raise Unverified(f"{path.name} didn't read back as the list written; set aside as {aside.name}")


def keep(folder: Path, at: datetime, data: bytes) -> Kept:
    """Keep a list made at `at`, as served: as a difference against the latest run's base while
    the run's rule allows, otherwise whole, starting a new run. Read back and checked before it
    counts; Unverified if it doesn't read back as the list."""
    stamp, sha256 = name(at), hashlib.sha256(data).hexdigest()
    notes: list[str] = []
    found = runs(folder)
    latest = found[-1] if found else None
    started = parse(latest.name) if latest else None
    if latest is not None and started is not None and abs(at - started) < MOST_DAYS:
        try:
            base, notes = _base(latest)
        except Damaged as e:
            notes = [f"{e}: its lists can't be rebuilt until one is restored; this list starts a new run"]
            base = b""
        if len(base) >= SMALLEST and len(base) + len(data) <= REACH:
            diff = _pack(data, base)
            lists, stored = _run_so_far(latest)
            if len(diff) * lists <= stored:
                path = latest / f"{stamp}{DIFF}"
                _write(path, diff)
                _verify(path, sha256, base)
                sums = (sha256, file_sha256(path))
                return Kept(stamp, path, "diff", len(data), len(diff), *sums, tuple(notes))
    whole = _pack(data)
    first, second = copies(folder / stamp)
    for path in (first, second):
        _write(path, whole)
        _verify(path, sha256)
    return Kept(stamp, first, "base", len(data), 2 * len(whole), sha256, file_sha256(first), tuple(notes))


def _run_so_far(run: Path) -> tuple[int, int]:
    """A run's lists, and the bytes they take: both copies of its base and every difference."""
    lists = stored = 0
    for path in run.iterdir():
        if path.name.endswith((DIFF, BASE)):  # BASE ends COPY too
            stored += path.stat().st_size
            lists += stamp_of(path) is not None
    return lists, stored


def rebuild(path: Path) -> bytes:
    """A kept list as served, from its file: a base, or a difference with its run's base."""
    if path.name.endswith(DIFF):
        return _unpack(path.read_bytes(), read_base(path.parent))
    return _unpack(path.read_bytes())
