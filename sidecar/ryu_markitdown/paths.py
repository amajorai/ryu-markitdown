"""Path confinement, filename hygiene, and safe archive expansion.

Three separate jobs, all fail-closed:

1. `resolve_input` — the parse request names a file by *path* (Core hands over a
   content-addressed blob under `${RYU_DIR}/blobs/…`, never an upload), so the
   path is attacker-influenced input. It is resolved through symlinks and then
   required to live under an allow-listed root. Without the post-resolution
   containment check, a symlink planted inside the blob dir reads `/etc/shadow`
   and returns it as "document text".

2. `safe_suffix` — only the *extension* of a caller-supplied filename is ever
   used, and only after it is validated as a short alphanumeric token. The
   filename is both the dispatch key (MarkItDown picks a converter from it) and,
   on the inline path, part of a write target; a crafted name must be able to
   steer neither.

3. `safe_extract` — an archive's member names are attacker-controlled strings.
   Absolute names, `..` segments, and symlink/hardlink/device members are all
   rejected outright rather than sanitised, because a rewritten name is a guess
   at intent and a refusal is not. MarkItDown reads ZIPs, so this is live
   surface: this repo has already shipped a zip-slip bug once.
"""

from __future__ import annotations

import os
import re
import tarfile
import zipfile
from pathlib import Path

from .limits import MAX_ARCHIVE_BYTES, MAX_ARCHIVE_MEMBERS, MAX_INPUT_BYTES


class InputError(ValueError):
    """A caller-supplied path, filename or archive that we refuse to open."""


def allowed_roots() -> list[Path]:
    """Directories a parse input may live under.

    `RYU_MARKITDOWN_ROOTS` is a `os.pathsep`-separated list Core sets from the
    manifest (`${RYU_DIR}` is the only token the manifest may interpolate). With
    nothing set we fall back to the reserved `RYU_DIR` Core injects into every
    child environment, and then to **nothing**: an empty allow-list means
    *nothing is readable*, never *everything*.

    Deliberately NOT falling back to a hardcoded `~/.ryu`: that path is not
    profile-aware, so under `bun dev` (`~/.ryu-dev`) every blob parse would be
    rejected as "outside the allowed roots" — a failure invisible to a
    release-profile smoke test. See `docs/document-parsing.md` §4.3 step 3.
    """
    raw = (os.environ.get("RYU_MARKITDOWN_ROOTS") or "").strip()
    if not raw:
        raw = (os.environ.get("RYU_DIR") or "").strip()
    roots: list[Path] = []
    for part in raw.split(os.pathsep):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            roots.append(Path(candidate).expanduser().resolve())
        except OSError:
            continue
    return roots


def _is_within(child: Path, parent: Path) -> bool:
    """True when `child` is `parent` or sits beneath it.

    `Path.is_relative_to` is 3.9+, and both sides are already fully resolved, so
    this is a pure lexical comparison over real paths.
    """
    return child == parent or parent in child.parents


def resolve_input(raw_path: str) -> Path:
    """Resolve a requested input path, or raise `InputError`.

    Symlinks are followed *before* the containment test on purpose: the question
    is where the bytes actually live, not what the name looks like.
    """
    candidate = (raw_path or "").strip()
    if not candidate:
        raise InputError("missing `path`")
    try:
        resolved = Path(candidate).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InputError(f"cannot open `{candidate}`: {exc}") from exc
    if not resolved.is_file():
        raise InputError(f"`{candidate}` is not a regular file")

    roots = allowed_roots()
    if not any(_is_within(resolved, root) for root in roots):
        readable = ", ".join(str(root) for root in roots) or "(none)"
        raise InputError(
            f"`{candidate}` resolves outside the allowed roots ({readable}); "
            "set RYU_MARKITDOWN_ROOTS to widen them"
        )

    size = resolved.stat().st_size
    if size > MAX_INPUT_BYTES:
        raise InputError(
            f"input is {size} bytes, over the {MAX_INPUT_BYTES}-byte limit "
            "(raise RYU_MARKITDOWN_MAX_INPUT_BYTES to allow it)"
        )
    return resolved


# A dispatch suffix is a dot plus 1-15 alphanumerics — 16 characters at most, the
# ceiling §5.5 of the parsing contract puts on what a caller's filename may
# contribute. Anything else (a path separator, a NUL, a 200-character
# "extension") is dropped rather than escaped or clipped: clipping would fabricate
# a plausible extension out of a hostile one, and the only legitimate use of this
# token is to name a converter and to end a temp filename.
_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,15}$")


def safe_suffix(filename: str | None) -> str:
    """The lowercase extension of a caller-supplied filename, or `""`.

    Takes at most 16 characters and nothing else from the name: the rest of any
    file we write is ours.
    """
    if not filename:
        return ""
    base = Path(str(filename).replace("\\", "/")).name
    suffix = Path(base).suffix.lower()
    return suffix if _SUFFIX_RE.match(suffix) else ""


ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")


def is_archive(name: str) -> bool:
    """Whether we should expand this input and parse its members ourselves.

    Takes a *name*, not a path, because on the blob form the file on disk is a
    bare sha256 and the caller's `filename` is the only thing that carries an
    extension.
    """
    lowered = (name or "").lower()
    return any(lowered.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


def _check_member_name(name: str) -> str:
    """Reject a member name that escapes, or return its normalised relative form."""
    if not name or name in (".", "./"):
        raise InputError("archive member with an empty name")
    if name.startswith("/") or name.startswith("\\"):
        raise InputError(f"archive member `{name}` is an absolute path")
    pure = Path(name.replace("\\", "/"))
    if pure.is_absolute() or pure.drive or pure.root:
        raise InputError(f"archive member `{name}` is an absolute path")
    if any(part == ".." for part in pure.parts):
        raise InputError(f"archive member `{name}` contains a parent-directory reference")
    return str(pure)


def _finalise(dest_root: Path, relative: str) -> Path:
    """Belt-and-braces containment check on the concrete destination path."""
    target = (dest_root / relative).resolve()
    if not _is_within(target, dest_root):
        raise InputError(f"archive member `{relative}` escapes the extraction directory")
    return target


def safe_extract(archive: Path, dest_root: Path) -> list[Path]:
    """Expand `archive` into `dest_root`, returning the extracted regular files.

    Directories are created as needed; every other member kind (symlink,
    hardlink, fifo, device) is refused, since none of them can carry document
    bytes and all of them can redirect a later write.
    """
    dest_root = dest_root.resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        return _extract_zip(archive, dest_root)
    try:
        if tarfile.is_tarfile(archive):
            return _extract_tar(archive, dest_root)
    except (OSError, tarfile.TarError) as exc:
        raise InputError(f"unreadable archive: {exc}") from exc
    raise InputError(f"`{archive.name}` is not a readable zip or tar archive")


def _extract_zip(archive: Path, dest_root: Path) -> list[Path]:
    written: list[Path] = []
    total = 0
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise InputError(
                f"archive has {len(infos)} members, over the {MAX_ARCHIVE_MEMBERS} limit"
            )
        for info in infos:
            relative = _check_member_name(info.filename)
            target = _finalise(dest_root, relative)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            # The high 16 bits of external_attr are the unix mode; S_IFLNK there
            # is a symlink member, which `ZipFile.extract` would write as a file
            # containing the link target and some tools would then follow.
            mode = info.external_attr >> 16
            if mode and (mode & 0o170000) == 0o120000:
                raise InputError(f"archive member `{info.filename}` is a symlink")
            total += info.file_size
            if total > MAX_ARCHIVE_BYTES:
                raise InputError(f"archive expands past the {MAX_ARCHIVE_BYTES}-byte limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                _copy_bounded(src, dst, MAX_ARCHIVE_BYTES)
            written.append(target)
    return written


def _extract_tar(archive: Path, dest_root: Path) -> list[Path]:
    written: list[Path] = []
    total = 0
    with tarfile.open(archive) as tf:
        count = 0
        for member in tf:
            count += 1
            if count > MAX_ARCHIVE_MEMBERS:
                raise InputError(f"archive has over {MAX_ARCHIVE_MEMBERS} members")
            relative = _check_member_name(member.name)
            target = _finalise(dest_root, relative)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if member.issym() or member.islnk():
                raise InputError(f"archive member `{member.name}` is a link")
            if not member.isfile():
                raise InputError(f"archive member `{member.name}` is not a regular file")
            total += member.size
            if total > MAX_ARCHIVE_BYTES:
                raise InputError(f"archive expands past the {MAX_ARCHIVE_BYTES}-byte limit")
            src = tf.extractfile(member)
            if src is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with src, target.open("wb") as dst:
                _copy_bounded(src, dst, MAX_ARCHIVE_BYTES)
            written.append(target)
    return written


def _copy_bounded(src, dst, ceiling: int) -> None:
    """Stream member bytes, stopping if the declared size was a lie."""
    remaining = ceiling
    while True:
        chunk = src.read(64 * 1024)
        if not chunk:
            return
        remaining -= len(chunk)
        if remaining < 0:
            raise InputError("archive member is larger than its declared size")
        dst.write(chunk)
