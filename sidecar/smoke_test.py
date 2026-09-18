"""Smoke test: the contract holds with or without the parser library installed.

Covers the acceptance criteria that need no native tooling:
  - an unauthenticated request is rejected (fail-closed bearer gate)
  - GET /health is open, POST /health is NOT
  - /capability answers even when `markitdown` is absent
  - POST /parse returns 202 + a job_id immediately, and the job reaches a
    terminal state (succeeded with the library installed, `library_missing`
    without it — either way, never a hang and never a crash)
  - **extension dispatch off the caller's `filename`**, which is the one failure
    mode that would ship green: Core's primary input form is a content-addressed
    blob path named after its sha256, with no extension at all. A test that only
    parses `hello.md` in place cannot see this. The `_dispatchable` half is
    checked directly so it is exercised even without the library installed.
  - path confinement rejects `..`, absolute paths outside the roots, and
    symlinks pointing out of the allowed roots
  - archive expansion rejects traversal members
  - only a validated short extension is ever taken from a caller's filename

A real `.pdf`/`.docx` parse needs `pip install 'markitdown[all]'`; the test
prints which mode it ran in.
"""

from __future__ import annotations

import os
import tempfile
import time
import zipfile
from pathlib import Path

# The server reads RYU_EXT_TOKEN at import time for its fail-closed auth gate;
# set it before importing `app` and present it as the bearer on every request.
os.environ.setdefault("RYU_EXT_TOKEN", "smoke-token")

# Confine parse inputs to this run's scratch dir so the confinement test has a
# real boundary to cross. Must also be set before the modules read it.
_SCRATCH = Path(tempfile.mkdtemp(prefix="ryu-markitdown-smoke-"))
_ROOT = _SCRATCH / "root"
_ROOT.mkdir()
os.environ["RYU_MARKITDOWN_ROOTS"] = str(_ROOT)
os.environ["RYU_MARKITDOWN_WORKDIR"] = str(_SCRATCH / "work")

from fastapi.testclient import TestClient  # noqa: E402

from ryu_markitdown.parser import _dispatchable, _plain_text  # noqa: E402
from ryu_markitdown.paths import InputError, safe_extract, safe_suffix  # noqa: E402
from ryu_markitdown.server import app  # noqa: E402

TOKEN = os.environ["RYU_EXT_TOKEN"]
client = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})
anon = TestClient(app)

TERMINAL = {"succeeded", "failed", "cancelled"}
FIXTURE = "# Ryu Document Parsing\n\nMarkItDown converts documents to markdown.\n"


def _await_terminal(job_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    snap: dict = {}
    while time.time() < deadline:
        snap = client.get(f"/jobs/{job_id}").json()
        if snap["status"] in TERMINAL:
            return snap
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} never reached a terminal state: {snap}")


def test_auth_is_fail_closed() -> None:
    assert anon.post("/parse", json={"path": "/x"}).status_code == 401
    assert anon.get("/jobs").status_code == 401
    assert anon.get("/capability").status_code == 401
    # /health is exempt on GET only.
    assert anon.get("/health").status_code == 200
    assert anon.post("/health").status_code == 401
    print("auth: unauthenticated rejected, GET /health open, POST /health closed")


def test_capability_answers_without_the_library() -> dict:
    cap = client.get("/capability").json()
    assert cap["capability"] == "document.parse", cap
    assert cap["backend"] == "markitdown", cap
    for expected in (".pdf", ".docx", ".xlsx", ".pptx", ".zip"):
        assert expected in cap["formats"], cap
    # Legacy binary Office needs LibreOffice; claiming it would send the user a
    # blank document instead of an answer.
    assert ".doc" not in cap["formats"] and ".ppt" not in cap["formats"], cap
    assert cap["limits"]["timeout_secs"] > 0, cap
    print(
        f"capability: available={cap['available']} "
        f"library={cap['library_version']} formats={len(cap['formats'])} "
        f"missing={cap['missing_dependencies']}"
    )
    return cap


def test_parse_roundtrip(available: bool) -> None:
    fixture = _ROOT / "hello.md"
    fixture.write_text(FIXTURE, encoding="utf-8")
    submitted = client.post("/parse", json={"path": str(fixture), "filename": "hello.md"})
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]
    assert job_id, submitted.text

    snap = _await_terminal(job_id)
    assert snap["filename"] == "hello.md", snap
    if available:
        assert snap["status"] == "succeeded", snap
        result = snap["result"]
        assert "MarkItDown" in result["markdown"], result["markdown"][:200]
        assert result["backend"] == "markitdown", result
        assert result["truncated"] is False, result
        print(f"parse: succeeded, {len(result['markdown'])} md chars")
    else:
        assert snap["status"] == "failed", snap
        assert snap["error_code"] == "library_missing", snap
        print(f"parse: library absent, clean job error -> {snap['error'][:80]}...")


def test_extensionless_blob_dispatch(available: bool) -> None:
    """The blob form: the file on disk has NO extension, `filename` carries it.

    This is what Core actually submits (`${RYU_DIR}/blobs/ab/abcd…`). MarkItDown
    dispatches on extension, so a backend that ignores `filename` on the path
    form fails every real parse while passing `test_parse_roundtrip`.
    """
    sha = "a" * 64
    blob_dir = _ROOT / "blobs" / sha[:2]
    blob_dir.mkdir(parents=True, exist_ok=True)
    blob = blob_dir / sha
    blob.write_text(FIXTURE, encoding="utf-8")

    submitted = client.post(
        "/parse",
        json={
            "path": str(blob),
            "blob_sha256": sha,
            "filename": "Quarterly Report.md",
            "mime": "text/markdown",
            "size_bytes": blob.stat().st_size,
        },
    )
    assert submitted.status_code == 202, submitted.text
    snap = _await_terminal(submitted.json()["job_id"])
    # The chip shows the user's filename, never the sha.
    assert snap["filename"] == "Quarterly Report.md", snap
    assert snap["error_code"] != "unsupported_format", snap
    if available:
        assert snap["status"] == "succeeded", snap
        assert "MarkItDown" in snap["result"]["markdown"], snap["result"]["markdown"][:200]
        print("dispatch: extensionless blob parsed via the caller's `filename`")
    else:
        assert snap["error_code"] == "library_missing", snap
        print("dispatch: extensionless blob reached the converter (library absent)")

    # And the mechanism itself, which needs no library at all: the path handed to
    # MarkItDown must carry the suffix even though the blob does not.
    with _dispatchable(blob, ".md") as aliased:
        assert aliased.suffix == ".md", aliased
        assert aliased != blob, aliased
        assert aliased.read_text(encoding="utf-8") == FIXTURE, aliased
    # A file that already has the right suffix is passed through, not copied.
    plain = _ROOT / "hello.md"
    with _dispatchable(plain, ".md") as same:
        assert same == plain, same
    print("dispatch: alias carries the suffix, matching suffixes pass through")


def test_inline_parse_accepted() -> None:
    import base64

    body = base64.b64encode(FIXTURE.encode("utf-8")).decode("ascii")
    r = client.post("/parse", json={"content_base64": body, "filename": "note.md"})
    assert r.status_code == 202, r.text
    snap = _await_terminal(r.json()["job_id"])
    assert snap["filename"] == "note.md", snap
    print("inline: content_base64 accepted and reaches a terminal state")


def test_path_confinement() -> None:
    outside = _SCRATCH / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    rejected = client.post("/parse", json={"path": str(outside)})
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["error_code"] == "input_rejected", rejected.text

    traversal = client.post("/parse", json={"path": f"{_ROOT}/../outside.txt"})
    assert traversal.status_code == 400, traversal.text

    link = _ROOT / "link.txt"
    link.symlink_to(outside)
    escaped = client.post("/parse", json={"path": str(link)})
    assert escaped.status_code == 400, escaped.text

    neither = client.post("/parse", json={})
    assert neither.status_code == 400, neither.text
    print("confinement: outside-root, `..`, and escaping symlink all rejected")


def test_archive_traversal_rejected() -> None:
    bomb = _ROOT / "evil.zip"
    with zipfile.ZipFile(bomb, "w") as zf:
        zf.writestr("../escaped.txt", "pwned")
    try:
        safe_extract(bomb, _SCRATCH / "extract")
        raise AssertionError("traversal member was extracted")
    except InputError as exc:
        assert "parent-directory" in str(exc), exc

    absolute = _ROOT / "absolute.zip"
    with zipfile.ZipFile(absolute, "w") as zf:
        zf.writestr("/etc/passwd", "pwned")
    try:
        safe_extract(absolute, _SCRATCH / "extract2")
        raise AssertionError("absolute member was extracted")
    except InputError as exc:
        assert "absolute" in str(exc), exc

    # A benign archive still expands, contained.
    good = _ROOT / "good.zip"
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("docs/readme.md", FIXTURE)
    dest = _SCRATCH / "extract3"
    written = safe_extract(good, dest)
    assert [p.name for p in written] == ["readme.md"], written
    assert dest.resolve() in written[0].parents, written
    print("archive: `..` and absolute members rejected, benign members contained")


def test_compound_suffix_archive_blob(available: bool) -> None:
    """A `.tar.gz` submitted as an extensionless blob — two seams crossing.

    `safe_suffix("bundle.tar.gz")` is `".gz"`, so if archives went through the
    dispatch alias they would be handed to MarkItDown as `document.gz`. They must
    not: `parse_file` routes on `is_archive(display_name)` and `_parse_archive`
    works from the original path. Neither the plain-archive test (real suffix on
    disk) nor the blob-dispatch test (not an archive) crosses both.
    """
    import tarfile

    staged = _SCRATCH / "bundle.tar.gz"
    with tarfile.open(staged, "w:gz") as tf:
        for name, body in (("notes.md", FIXTURE), ("data.csv", "x,y\n1,2\n")):
            member = _SCRATCH / name
            member.write_text(body, encoding="utf-8")
            tf.add(member, arcname=name)
    sha = "e" * 64
    blob = _ROOT / "blobs" / sha[:2] / sha
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(staged.read_bytes())

    snap = _await_terminal(
        client.post(
            "/parse",
            json={"path": str(blob), "blob_sha256": sha, "filename": "bundle.tar.gz"},
        ).json()["job_id"]
    )
    assert snap["filename"] == "bundle.tar.gz", snap
    if available:
        assert snap["status"] == "succeeded", snap
        assert snap["result"]["metadata"]["sources"] == ["data.csv", "notes.md"], snap["result"]
        assert "# notes.md" in snap["result"]["markdown"], snap["result"]["markdown"][:200]
        print("archive blob: `.tar.gz` expanded from an extensionless blob path")
    else:
        # Every member fails `library_missing`, so the archive yields no sections.
        assert snap["error_code"] == "empty_document", snap
        print("archive blob: `.tar.gz` expanded, members failed cleanly (library absent)")


def test_empty_document_fails_loudly(available: bool) -> None:
    """A conversion that produces nothing is `failed`, not `succeeded` with "".

    "Success with nothing in it" is the silent drop wearing a different hat, and
    it is the shape a scanned PDF takes here (MarkItDown has no OCR). Core's
    `normalize_job` would rewrite it anyway; failing at the source names why.
    """
    if not available:
        print("empty: skipped (needs the library to produce an empty conversion)")
        return
    blank = _ROOT / "blank.md"
    blank.write_text("   \n\n", encoding="utf-8")
    snap = _await_terminal(
        client.post("/parse", json={"path": str(blank), "filename": "blank.md"}).json()["job_id"]
    )
    assert snap["status"] == "failed", snap
    assert snap["error_code"] == "empty_document", snap
    assert "OCR" in snap["error"], snap
    print("empty: a blank conversion fails with `empty_document`, not a silent success")


def test_filename_hygiene() -> None:
    assert safe_suffix("Q3 report.PDF") == ".pdf"
    assert safe_suffix("../../etc/passwd") == ""
    assert safe_suffix("a.b/c") == ""  # the separator, not an extension
    assert safe_suffix("evil." + "x" * 40) == ""
    assert safe_suffix("no-extension") == ""
    assert safe_suffix(None) == ""
    print("filename: only a validated short extension is ever taken from a caller")


def test_plain_text_fallback() -> None:
    text = _plain_text(
        "# Heading\n\n- **bold** item\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n"
    )
    assert text.startswith("Heading"), text
    assert "bold item" in text, text
    assert "---" not in text, text
    print("text: markup-free fallback strips headings, bullets, emphasis, rules")


def main() -> None:
    test_auth_is_fail_closed()
    cap = test_capability_answers_without_the_library()
    available = bool(cap["available"])
    test_parse_roundtrip(available)
    test_extensionless_blob_dispatch(available)
    test_inline_parse_accepted()
    test_path_confinement()
    test_archive_traversal_rejected()
    test_compound_suffix_archive_blob(available)
    test_empty_document_fails_loudly(available)
    test_filename_hygiene()
    test_plain_text_fallback()
    mode = "with markitdown installed" if available else "without markitdown"
    print(f"\nSMOKE_OK ({mode})")


if __name__ == "__main__":
    main()
