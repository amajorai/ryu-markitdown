# ryu-markitdown

MarkItDown for Ryu — document parsing via Microsoft's MIT-licensed MarkItDown: the default `document.parse` backend, and the one that works on a machine where the user installed nothing else.

> **The public home of `ryu-markitdown`.** Source, builds, and releases live here —
> binaries for every platform are attached to each release.
>
> This tree is generated from the Ryu monorepo, so commits pushed here
> directly are replaced on the next sync. **Pull requests are welcome** —
> open them here and they are ported into the monorepo, then flow back out.
> Ryu as a whole: https://github.com/amajorai/ryu

## Source & build

The **source of record** for the universal Ryu TTS sidecar — a self-contained
Python HTTP front over several text-to-speech engines. Install its
dependencies (`pip install -r sidecar/requirements.txt`) and run
`python -m ryu_tts` from `sidecar/`; Core manages it as a sidecar in a
full Ryu install.

## License

Apache-2.0 — see [LICENSE](./LICENSE).

---

# MarkItDown — `document.parse` backend (the default)

Turns documents into markdown using Microsoft's MIT-licensed
[`markitdown`](https://github.com/microsoft/markitdown) library. This is one of
several interchangeable backends behind the swappable `document.parse`
capability: enable it, pick it in the provider selector, and everything that
ingests a document (Spaces, RAG, chat attachments) routes through it. Nothing in
Core is bound to it — the swap is manifest data.

It is the **default** provider (`"default": true` in the manifest's `provides`
block, and the only one of the four that carries the key). Not because it is the
most capable — it is not — but because it is the one that works on a machine
where the user installed nothing on purpose.

## What it is good at

**It installs.** `pip install 'markitdown[all]'` is pure Python: no ONNX runtime,
no layout-detection model download, no CUDA, no `brew`/`apt` packages to chase.
Tens of megabytes and under a minute, against 1–2 GB and four native tools for
`unstructured`. On a laptop with no `poppler`, no `tesseract` and no LibreOffice —
which is most laptops — this is the backend that returns text instead of an
error naming a package.

**It emits markdown natively.** MarkItDown's whole design goal is markdown
output for LLM consumption, so a spreadsheet comes back as a real markdown table
and a Word document keeps its heading levels. No element-to-markdown rendering
layer is needed on top:

```
## Sheet
| Region | Total |
| --- | --- |
| EU | 12 |
```

**It covers what people actually attach.** PDF, DOCX, PPTX, XLSX/XLS, HTML, CSV,
JSON, XML, EPUB, Outlook `.msg`, Jupyter notebooks, and ZIP/TAR archives expanded
member by member. Each archive member is nested under a heading named for its
path inside the archive and its own headings are pushed one level down, so an
archive does not render as a flat run of `#`. One unreadable member becomes a
warning, not a failed archive.

## What it costs

**No OCR. This is the big one.** MarkItDown extracts embedded text; it does not
look at pixels. A scanned PDF, a photographed contract, or a PDF that is really a
wrapper around page images comes back with **nothing**. The sidecar refuses to
call that a success — a conversion that produces no text fails the job with
`error_code: "empty_document"` and a message saying so and naming the backends
that do OCR. "Success with nothing in it" is the silent drop this whole capability
exists to kill, so it is never reported as one.

**No legacy binary Office.** `.doc`, `.ppt`, `.odt` and `.rtf` need LibreOffice or
pandoc, and carrying those would forfeit the reason this backend is the default.
They are deliberately absent from the advertised format list rather than
advertised and blank. `.xls` *is* supported (through `xlrd`, pure Python).

**No element-level structure.** `unstructured` returns typed fragments (`Title`,
`NarrativeText`, `Table`, `ListItem`) that a consumer can chunk on. MarkItDown
returns a markdown string. If you want structure-aware chunking rather than
character-count chunking, that is the backend for it. Job results here therefore
carry `markdown` + `text` and no `elements` array — the contract makes it
optional for exactly this reason.

**Optional native tools, and only for media.** Images and audio are advertised by
`GET /capability` **only when their tool is on PATH**, because without it the
conversion returns nothing useful and a format list that promises `.png` and
delivers an empty string is the same silent lie:

| Tool | Unlocks | macOS | Debian/Ubuntu |
| --- | --- | --- | --- |
| exiftool | EXIF metadata from images and audio (`.jpg`, `.png`, `.tiff`, `.wav`, …) | `brew install exiftool` | `apt-get install -y libimage-exiftool-perl` |
| ffmpeg | decoding non-WAV audio before speech transcription (`.mp3`, `.m4a`) | `brew install ffmpeg` | `apt-get install -y ffmpeg` |

Neither is *required*: there is no format in the base list that fails without
them, so unlike `unstructured` this sidecar has no required-dependency gate at
all.

**Hardware.** CPU-only, no GPU, no model in memory. A few hundred MB of RSS at
peak on a large PDF; **1 GB RAM is comfortable**. Conversion is seconds for a
typical document — the 600 s per-parse timeout is the contract's shared number,
not a hint about how long this backend takes. Two parses run concurrently by
default (`RYU_MARKITDOWN_MAX_WORKERS`).

## Choosing between backends

| If your corpus is… | Bind |
| --- | --- |
| modern PDFs, Office files, HTML, spreadsheets — and you want it to just work | **markitdown** (this one) |
| a fifteen-year shared drive with `.doc`/`.ppt`/`.msg`, or anything scanned | `unstructured` |
| research papers and complex tables where layout fidelity is the product | `docling` |
| heavy PDF/OCR work with a GPU to spend on it | `mineru` |

Start here. Swap when a document comes back `empty_document` or the format list
does not name what you have — both of those are visible answers, which is the
point.

## HTTP contract

Reachable at `/api/ext/@ryu/markitdown/*`. Every path below is declared in
`manifest.json`; an undeclared path is refused with a 404 at the proxy before it
reaches this process.

```
GET    /health          -> { ok, backend, available, library_version, missing_dependencies }
GET    /capability      -> { capability, backend, formats, system_dependencies, limits }
POST   /parse           -> 202 { job_id, status }
GET    /jobs            -> { jobs: [ snapshot without result ] }
GET    /jobs/{job_id}   -> snapshot (result present once succeeded)
DELETE /jobs/{job_id}   -> snapshot (cooperative cancel)
```

`POST /parse` takes exactly one of `path` (absolute, confined to
`RYU_MARKITDOWN_ROOTS`) or `content_base64`, plus `filename`, the advisory
`blob_sha256` / `mime` / `size_bytes`, and optional `options` (`keep_data_uris`).
Unknown option keys are ignored rather than rejected — a hint one backend
understands must not fail on another.

### `filename` is the dispatch key, on **both** input forms

MarkItDown picks a converter from the file's extension, and Core's primary input
form is a content-addressed blob path — `${RYU_DIR}/blobs/ab/abcd…` — whose name
is a sha256 with no extension at all. So the caller's **original** `filename` is
what dispatch reads, and the sidecar stages a suffixed alias (symlink, or
hardlink, or copy) in a directory it owns rather than trusting or renaming the
on-disk name. Only a validated extension — a dot plus at most 15 alphanumerics —
is ever taken from that filename; the rest of any file this process writes is its
own. The job snapshot reports the original filename too, so a chip shows
`Q3 report.pdf` and not a sha.

A succeeded job's `result` is:

```jsonc
{
  "backend": "markitdown",
  "backend_version": "0.1.5",
  "markdown": "# Quarterly Report\n\n…",   // primary payload
  "text": "Quarterly Report\n\n…",          // markup-free fallback
  "warnings": [],
  "truncated": false,
  "metadata": { "filename": "q3.xlsx", "sources": ["q3.xlsx"], "page_count": null }
}
```

Failed jobs carry `error`, `error_code` (`library_missing`, `unsupported_format`,
`missing_dependency`, `parse_failed`, `empty_document`, `input_rejected`,
`timeout`) and `missing_dependencies`.

### Why submit-and-poll rather than one request

The ext-proxy's activity guard drops as soon as response headers arrive, so a
`lazy` + `idle_stop_secs` sidecar can be reaped **mid-request**. A 90-second parse
behind a single HTTP call is therefore killable. Every parse is a job: `POST
/parse` answers 202 immediately and the caller polls, which re-arms the guard on
each hit.

## Security posture

- **Fail-closed bearer.** `RYU_EXT_TOKEN` is read at import time and compared with
  `hmac.compare_digest`. No token configured means *reject everything*. `/health`
  is exempt on **GET only** — the predicate is `path == "/health" and method ==
  "GET"`, so the route cannot become an unauthenticated hole if it grows a body.
- **Path confinement.** A parse input is resolved through symlinks *first* and
  then required to live under `RYU_MARKITDOWN_ROOTS` (default `${RYU_DIR}`).
  Without the post-resolution check, a symlink planted in the blob dir turns this
  service into an arbitrary-file-read primitive. An empty allow-list means
  *nothing is readable*, never everything — and the fallback chain is
  `RYU_MARKITDOWN_ROOTS` → the reserved `RYU_DIR` Core injects → reject. It
  deliberately does **not** bottom out at a hardcoded `~/.ryu`, which is not
  profile-aware and would reject every blob parse under `bun dev` (`~/.ryu-dev`)
  in a way a release-profile test cannot see.
- **Archive safety.** ZIP is a headline MarkItDown format, so this is live surface
  rather than theory. The sidecar expands archives **itself** instead of handing
  them to MarkItDown's own `ZipConverter`, precisely so the member checks apply:
  absolute member names, `..` segments, and symlink/hardlink/device members are
  refused outright — not sanitised. Member count and expanded bytes are capped, a
  member larger than its declared size aborts the extraction, and a member that is
  itself an archive is refused into `warnings` rather than recursed into (which
  would route back around these checks).
- **No plugins.** `MarkItDown(enable_plugins=False)`. Third-party MarkItDown
  plugins are arbitrary code discovered from the venv's entry points, and a
  document parser is not a place to opt into that silently.
- **Bounded everything.** Input bytes, output bytes, wall-clock per parse,
  concurrent workers, retained jobs, archive members and archive bytes all have
  caps (see `sidecar/ryu_markitdown/limits.py`); each is env-overridable and
  reported by `/capability`. A result over the output cap is clipped and flagged
  `truncated: true`, never dropped.
- The sidecar makes **no network calls** and never fetches a URL. (MarkItDown can
  read YouTube transcripts and Azure Document Intelligence; neither is wired here
   — a `document.parse` provider reads local paths and inline bytes, and turning a
  caller-supplied string into a server-side fetch would be an SSRF primitive on
  the node's own loopback surface.)

### Timeout honesty

CPython cannot kill a running thread, so the per-parse watchdog marks the job
`failed` at the deadline and stops waiting on it; the worker thread may run to
completion in the background and its result is discarded. From the caller's side
this is real enforcement — the job never hangs. The ceiling on wasted work is
`RYU_MARKITDOWN_MAX_WORKERS` stuck parses, after which new submissions queue.

## Configuration

| Env | Default | Meaning |
| --- | --- | --- |
| `RYU_MARKITDOWN_PORT` | 8094 | Bind port. Core injects the **profile-shifted** value (dev profile = +1000, so 9094). |
| `RYU_MARKITDOWN_HOST` | `127.0.0.1` | Bind host. Loopback only — this process reads local files. |
| `RYU_EXT_TOKEN` | — | Shared bearer. Unset ⇒ every route except `GET /health` returns 401. |
| `RYU_MARKITDOWN_ROOTS` | `${RYU_DIR}` | `os.pathsep`-separated roots a parse input may live under. |
| `RYU_MARKITDOWN_WORKDIR` | temp dir | Where inline uploads, dispatch aliases and expanded archives are staged. |
| `RYU_MARKITDOWN_MAX_INPUT_BYTES` | 200 MiB | Largest input file or archive. |
| `RYU_MARKITDOWN_MAX_OUTPUT_BYTES` | 8 MiB | Result cap; over it the payload is clipped and `truncated` is true. |
| `RYU_MARKITDOWN_TIMEOUT_SECS` | 600 | Wall-clock ceiling per parse. |
| `RYU_MARKITDOWN_MAX_WORKERS` | 2 | Concurrent parses. |
| `RYU_MARKITDOWN_MAX_JOBS` | 64 | Retained jobs before the oldest terminal ones are evicted. |
| `RYU_MARKITDOWN_MAX_ARCHIVE_MEMBERS` | 512 | Members expanded from one archive. |
| `RYU_MARKITDOWN_MAX_ARCHIVE_BYTES` | 512 MiB | Total expanded archive bytes. |

## Developing

```bash
cd sidecar
uv venv --python 3.11 .venv && source .venv/bin/activate
uv pip install -e ".[all]" httpx
python smoke_test.py            # contract tests; runs with or without the library
python -m ryu_markitdown        # serve on 127.0.0.1:8094
```

`smoke_test.py` covers the fail-closed bearer, the open `GET /health` (and the
closed `POST /health`), `/capability` answering without the library, a real parse
round-trip, **extension dispatch off an extensionless blob path**, the inline
form, path-confinement rejection (outside-root, `..`, escaping symlink), archive
traversal rejection, the `empty_document` failure, and filename hygiene. It
prints which mode it ran in — a run without `markitdown` installed exercises the
clean-failure path, not the parse path, and both modes must pass.
