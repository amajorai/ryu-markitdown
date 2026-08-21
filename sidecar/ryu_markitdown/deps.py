"""System-dependency detection — and the short list is the selling point.

MarkItDown is the one `document.parse` backend with **no required native tool**.
Everything in `BASE_EXTENSIONS` parses with pure Python, which is why this backend
carries `"default": true`: it is the one that works on a machine where the user
installed nothing on purpose.

The two entries below are therefore **optional only**. There is deliberately no
`REQUIRED_BY_EXT` table mirroring `unstructured`'s: a required-dependency gate
here would refuse formats this library reads perfectly well. Their absence
narrows the advertised format list (`formats.capability_extensions`) instead of
failing a job, so a user never gets a job error naming a tool that was never
needed for the file they submitted.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class SystemDep:
    """One native dependency: how to detect it and how to install it."""

    key: str
    # Any one of these on PATH satisfies the dependency.
    binaries: tuple[str, ...]
    purpose: str
    brew: str
    apt: str

    def present(self) -> bool:
        return any(shutil.which(binary) for binary in self.binaries)

    def describe(self) -> dict[str, object]:
        return {
            "key": self.key,
            "present": self.present(),
            "required": False,
            "purpose": self.purpose,
            "install": {"brew": self.brew, "apt": self.apt},
        }

    def message(self) -> str:
        return (
            f"{self.key} is not installed — {self.purpose}. "
            f"Install it with `{self.brew}` (macOS) or `{self.apt}` (Debian/Ubuntu)."
        )


EXIFTOOL = SystemDep(
    key="exiftool",
    binaries=("exiftool",),
    purpose="reading EXIF metadata out of images and audio files",
    brew="brew install exiftool",
    apt="apt-get install -y libimage-exiftool-perl",
)
FFMPEG = SystemDep(
    key="ffmpeg",
    binaries=("ffmpeg",),
    purpose="decoding non-WAV audio before speech transcription",
    brew="brew install ffmpeg",
    apt="apt-get install -y ffmpeg",
)

ALL_DEPS: tuple[SystemDep, ...] = (EXIFTOOL, FFMPEG)

# Extension → dependencies that unlock output but whose absence only narrows what
# this backend advertises. Reported as job `warnings` if a caller submits one of
# these anyway, never as an error.
OPTIONAL_BY_EXT: dict[str, tuple[SystemDep, ...]] = {
    ".jpg": (EXIFTOOL,),
    ".jpeg": (EXIFTOOL,),
    ".png": (EXIFTOOL,),
    ".tiff": (EXIFTOOL,),
    ".tif": (EXIFTOOL,),
    ".wav": (EXIFTOOL,),
    ".mp3": (EXIFTOOL, FFMPEG),
    ".m4a": (EXIFTOOL, FFMPEG),
}


def missing_optional(ext: str) -> list[SystemDep]:
    """Dependencies whose absence degrades but does not break this extension."""
    return [dep for dep in OPTIONAL_BY_EXT.get(ext.lower(), ()) if not dep.present()]


def markitdown_version() -> str | None:
    """Installed `markitdown` version, or None when the library is absent."""
    try:
        import markitdown  # noqa: F401
    except Exception:
        return None
    try:
        from importlib.metadata import version as pkg_version

        return pkg_version("markitdown")
    except Exception:
        # Importable but not installed as a distribution (a source checkout on
        # PYTHONPATH). It is present; we just cannot name its version.
        return getattr(markitdown, "__version__", "unknown")


def snapshot() -> dict[str, object]:
    """Everything a caller needs to explain why a parse will or will not work."""
    version = markitdown_version()
    deps = [dep.describe() for dep in ALL_DEPS]
    return {
        "backend": "markitdown",
        "library_available": version is not None,
        "library_version": version,
        "system_dependencies": deps,
        # Optional-only, so this list narrows the format list rather than
        # disabling the backend. It is never a reason a job failed.
        "missing_system_dependencies": [dep["key"] for dep in deps if not dep["present"]],
    }
