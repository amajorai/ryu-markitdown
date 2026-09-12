"""The parse itself: a file path in, a `document.parse` result out.

Everything that can go wrong here is reported as a typed error the job carries,
never as an exception that kills the worker or an empty result that looks like a
blank document:

  * `library_missing`      — `markitdown` was never installed
  * `unsupported_format`   — the library has no converter for this extension
  * `missing_dependency`   — a converter needs an extra/native tool that is absent
  * `parse_failed`         — the converter raised
  * `empty_document`       — the converter succeeded and produced nothing
  * `input_rejected`       — path confinement / archive safety refused the input

**Extension dispatch is the load-bearing detail of this module.** MarkItDown picks
a converter from the file's extension and mimetype hints, and the primary input
form in the contract is a content-addressed blob path — `${RYU_DIR}/blobs/ab/abcd…`
— which has no extension at all. Handing that straight to `convert()` makes every
real Spaces and chat parse fail as "unsupported", while a smoke test that parses
`hello.txt` in place passes. So the caller's `filename` is the dispatch key
(contract §3.4: "its extension is the dispatch key"), and we stage an alias with
the right suffix in a directory we own rather than trusting the on-disk name.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import BACKEND
from .deps import markitdown_version, missing_optional
from .limits import MAX_OUTPUT_BYTES
from .paths import InputError, is_archive, safe_extract, safe_suffix

# MarkItDown raises a small family of exceptions whose import location has moved
# between releases (`markitdown._exceptions` in 0.1.x, package root before that).
# Matching on the class name across the MRO is version-independent and cannot
# itself raise ImportError.
_ERROR_BY_EXCEPTION_NAME: dict[str, tuple[str, str]] = {
    "UnsupportedFormatException": (
        "unsupported_format",
        "MarkItDown has no converter for this format",
    ),
    "MissingDependencyException": (
        "missing_dependency",
        "MarkItDown needs an optional dependency that is not installed — "
        "install the full set with `pip install 'markitdown[all]'`",
    ),
    "FileConversionException": ("parse_failed", "MarkItDown could not convert this file"),
}


class ParseError(RuntimeError):
    """A parse failure with a machine-readable code and a human-readable fix."""

    def __init__(self, code: str, message: str, *, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.missing = missing or []


def _classify(exc: BaseException) -> tuple[str, str] | None:
    """Map a MarkItDown exception to (error_code, prefix), by class name."""
    for klass in type(exc).__mro__:
        mapped = _ERROR_BY_EXCEPTION_NAME.get(klass.__name__)
        if mapped is not None:
            return mapped
    return None


def _workdir() -> Path:
    """Scratch root we own. Core points this at `${RYU_DIR}/cache/markitdown`."""
    configured = (os.environ.get("RYU_MARKITDOWN_WORKDIR") or "").strip()
    root = Path(configured).expanduser() if configured else Path(tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def _dispatchable(path: Path, suffix: str) -> Iterator[Path]:
    """Yield a path whose extension is `suffix`, without copying if avoidable.

    The blob form gives us a file named after its sha256, so the on-disk name
    carries no format hint. Rather than renaming Core's blob (we do not own it) or
    copying up to 200 MiB, we link an alias with the right suffix into our own
    scratch dir and hand *that* to MarkItDown. Symlink first, hardlink second (a
    filesystem that forbids symlinks), copy last (a cross-device blob store).

    Only the copy branch costs anything, and its ceiling is `MAX_INPUT_BYTES`: on
    a host where both link calls fail, a parse transiently doubles the input on
    disk under `RYU_MARKITDOWN_WORKDIR`. Known and accepted — the branch exists so
    a non-filesystem-local blob store still parses at all, and the scratch dir is
    removed in `finally`.

    Archives never reach here: `parse_file` routes them to `_parse_archive`, which
    works from the original `path`. That matters because a compound suffix like
    `.tar.gz` would alias to `document.gz`.
    """
    if not suffix or path.suffix.lower() == suffix:
        yield path
        return
    scratch = Path(tempfile.mkdtemp(prefix=f"dispatch-{uuid.uuid4().hex[:8]}-", dir=_workdir()))
    alias = scratch / f"document{suffix}"
    try:
        try:
            os.symlink(path, alias)
        except (OSError, NotImplementedError, AttributeError):
            try:
                os.link(path, alias)
            except OSError:
                shutil.copy2(path, alias)
        yield alias
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _converter():
    """Build a MarkItDown instance, or raise `library_missing`.

    `enable_plugins=False` is not negotiable: third-party MarkItDown plugins are
    arbitrary code discovered from the venv's entry points, and a document parser
    is not a place to opt into that silently. Older releases predate the keyword
    entirely (and predate plugins), so its absence is tolerated.
    """
    try:
        from markitdown import MarkItDown
    except ImportError as exc:
        raise ParseError(
            "library_missing",
            "the `markitdown` library is not installed in this sidecar's venv — "
            "install it with `pip install 'markitdown[all]'`",
        ) from exc
    try:
        return MarkItDown(enable_plugins=False)
    except TypeError:
        return MarkItDown()


def _convert_kwargs(options: dict[str, Any]) -> dict[str, Any]:
    """Translate caller hints into MarkItDown kwargs.

    Unknown keys are ignored, never an error — a hint one backend understands must
    not fail on another.
    """
    kwargs: dict[str, Any] = {}
    if options.get("keep_data_uris") is not None:
        kwargs["keep_data_uris"] = bool(options["keep_data_uris"])
    return kwargs


def _result_markdown(result: Any) -> str:
    """The converted text, whichever attribute this release exposes it under."""
    for attribute in ("text_content", "markdown", "text"):
        value = getattr(result, attribute, None)
        if isinstance(value, str) and value:
            return value
    return ""


_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_ATX_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_BULLET_RE = re.compile(r"^\s*([-*+]|\d+\.)\s+")
_INLINE_RE = re.compile(r"(\*\*|__|\*|_|`)")
_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")


def _plain_text(markdown: str) -> str:
    """A markup-free view of the markdown, for the contract's `text` field.

    Intentionally shallow: this is a fallback for a consumer that cannot render
    markdown, not a parser. Markdown *is* the primary payload here — MarkItDown
    emits it natively — so nothing downstream depends on this being exact.
    """
    lines: list[str] = []
    for raw in markdown.splitlines():
        if _FENCE_RE.match(raw):
            continue
        line = _ATX_RE.sub("", raw)
        line = _BULLET_RE.sub("", line)
        line = _LINK_RE.sub(r"\1", line)
        line = _INLINE_RE.sub("", line)
        if set(line.strip()) <= {"|", "-", ":", " "} and "|" in line:
            continue  # a table's separator row carries no content
        lines.append(line.replace("|", " ").rstrip() if "|" in line else line.rstrip())
    return "\n".join(lines).strip()


def _truncate(text: str, budget: int) -> tuple[str, bool]:
    """Clip to a byte budget on a character boundary."""
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text, True
    clipped = encoded[:budget].decode("utf-8", errors="ignore")
    return clipped, False


def _convert_one(path: Path, display_name: str, options: dict[str, Any]) -> tuple[str, list[str]]:
    """Convert one non-archive file to markdown, with degradation warnings."""
    suffix = safe_suffix(display_name) or safe_suffix(path.name)
    warnings = [dep.message() for dep in missing_optional(suffix)]
    converter = _converter()
    kwargs = _convert_kwargs(options)
    with _dispatchable(path, suffix) as target:
        try:
            try:
                result = converter.convert(str(target), **kwargs)
            except TypeError:
                # An older release that does not know one of our hint kwargs. The
                # hint is optional; losing it must not lose the document.
                result = converter.convert(str(target))
        except ParseError:
            raise
        except Exception as exc:  # noqa: BLE001 — every failure becomes a typed job error
            mapped = _classify(exc)
            if mapped is not None:
                code, prefix = mapped
                raise ParseError(code, f"{prefix}: `{display_name}`: {exc}") from exc
            raise ParseError(
                "parse_failed",
                f"converting `{display_name}` failed: {type(exc).__name__}: {exc}",
            ) from exc
    return _result_markdown(result), warnings


def parse_file(
    path: Path, filename: str | None = None, options: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Parse one file (or one archive of files) into a `document.parse` result.

    `filename` is the caller's ORIGINAL name and is the dispatch key; `path` is
    where the bytes live and may be a bare sha256 with no extension.
    """
    options = options or {}
    display_name = Path((filename or path.name).replace("\\", "/")).name or path.name

    if is_archive(display_name):
        markdown, warnings, sources = _parse_archive(path, display_name, options)
    else:
        markdown, warnings = _convert_one(path, display_name, options)
        sources = [display_name]

    if not markdown.strip():
        # "Succeeded with nothing in it" is the silent drop wearing a different
        # hat — Core's `normalize_job` would rewrite it to `failed` anyway, so we
        # fail it here where we can still say WHY. The commonest cause by far is a
        # scanned PDF: MarkItDown has no OCR.
        raise ParseError(
            "empty_document",
            f"`{display_name}` converted successfully but produced no text. "
            "MarkItDown does not do OCR, so a scanned or image-only document comes "
            "back empty here — bind the `unstructured` or `mineru` backend for that.",
        )

    markdown, whole_md = _truncate(markdown, MAX_OUTPUT_BYTES)
    text, whole_text = _truncate(_plain_text(markdown), MAX_OUTPUT_BYTES)
    return {
        "backend": BACKEND,
        "backend_version": markitdown_version(),
        "markdown": markdown,
        "text": text,
        "warnings": warnings,
        "truncated": not (whole_md and whole_text),
        "metadata": {
            "filename": display_name,
            "sources": sources,
            "page_count": None,
        },
    }


def _parse_archive(
    path: Path, display_name: str, options: dict[str, Any]
) -> tuple[str, list[str], list[str]]:
    """Expand an archive into a scratch dir and convert every member we can read.

    We expand it ourselves rather than letting MarkItDown's own ZipConverter do
    it, because the member-name checks in `paths.safe_extract` are the contract's
    security floor (§5.4) and handing the archive to the library would route
    around them. One unreadable member must not sink the whole archive, so
    per-member failures become warnings and the rest of the documents still come
    back.
    """
    sections: list[str] = []
    warnings: list[str] = []
    sources: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ryu-markitdown-", dir=_workdir()) as scratch:
        root = Path(scratch).resolve()
        try:
            members = safe_extract(path, root)
        except InputError as exc:
            raise ParseError("input_rejected", str(exc)) from exc
        for member in sorted(members):
            relative = str(member.relative_to(root))
            if is_archive(relative):
                # A nested archive would go to MarkItDown's ZipConverter, whose
                # extraction has not been through the checks above. Refuse it as a
                # warning rather than recursing without a depth bound.
                warnings.append(f"{relative}: nested archives are not expanded")
                continue
            try:
                member_markdown, member_warnings = _convert_one(member, relative, options)
            except ParseError as exc:
                warnings.append(f"{relative}: {exc}")
                continue
            if not member_markdown.strip():
                warnings.append(f"{relative}: produced no text")
                continue
            sources.append(relative)
            warnings.extend(f"{relative}: {warning}" for warning in member_warnings)
            # Each member nests under a heading named for its path inside the
            # archive, and its own headings are pushed one level down, so an
            # archive does not render as a flat run of `#` with no way to tell
            # where one document ends.
            sections.append(f"# {relative}\n\n{_demote_headings(member_markdown)}")
    return "\n\n".join(sections), warnings, sources


_HEADING_LINE_RE = re.compile(r"^(\s{0,3})(#{1,5})(\s+)", re.MULTILINE)


def _demote_headings(markdown: str) -> str:
    """Push every heading one level deeper, stopping at h6."""
    return _HEADING_LINE_RE.sub(lambda m: f"{m.group(1)}#{m.group(2)}{m.group(3)}", markdown)
