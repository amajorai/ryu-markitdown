"""Ryu MarkItDown sidecar — a Core-managed document-parsing runtime.

Wraps Microsoft's MIT-licensed `markitdown` library behind the swappable
`document.parse` capability contract: submit a parse, get a `job_id` back
immediately, poll for the result. Core owns lifecycle, storage, chunking and
embedding; this process only turns bytes on disk into markdown.

Why job-id + poll rather than one long request: the ext-proxy's activity guard
drops as soon as response headers arrive, so a `lazy` + `idle_stop_secs` sidecar
can be reaped *mid-request*. A 90-second PDF parse behind a single HTTP call is
therefore killable; a 202 + poll loop is not (each poll re-arms the guard).

This is the `document.parse` **default** backend (`"default": true` in the
manifest's `provides` block, and it is the only provider that carries the key).
It holds that slot because it is the cheapest honest install of the four: pure
pip, no native toolchain, no model download.
"""

from __future__ import annotations

__version__ = "0.1.0"

# Default HTTP port. Core pins the real (profile-shifted) port at spawn via
# RYU_MARKITDOWN_PORT — under the dev profile every port is +1000, so this
# constant is only the standalone/bare-`python -m` fallback.
DEFAULT_PORT = 8094

# Backend id as it appears in `document.parse` provider binding + /capability.
BACKEND = "markitdown"
