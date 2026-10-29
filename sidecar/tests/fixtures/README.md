# Positive document fixtures

These documents contain only synthetic test content and have no external links,
embedded macros, media, personal information or provider dependencies.

- `layout.pdf` (1,667 bytes), SHA256
  `a6f5083ea7b43909a711d5da00a0a37a7e98b364f28d8c87897b25ecd61538fb`:
  the fixed one-page text PDF reused from the Docling test fixtures. Its title is
  `Ryu Document Parsing`, paragraph is `Docling real local PDF layout conversion proof.`,
  and visible table values are `Region`, `Total`, `EU`, `12`. MarkItDown must
  extract these values; this PDF check does not promise layout reconstruction.
- `table.docx` (2,045 bytes), SHA256
  `74c30cc94610a4facd6b473d940f07044f39c7e4ff64f9a2534828bcb70160b5`:
  a genuine minimal OOXML document assembled with Python's standard-library
  ZIP writer. It contains `[Content_Types].xml`, `_rels/.rels` pointing to the
  main Word document, and `word/document.xml`. The title is `Ryu Document Parsing`,
  paragraph is `MarkItDown real local DOCX conversion proof.`, and an actual
  `w:tbl` has header cells `Region` / `Total` and data cells `EU` / `12`.
  ZIP entries use a fixed 2020-01-01 timestamp, stored compression, Unix file
  mode 0644, UTF-8 XML, and the listed order. No Office, python-docx or fixture
  generator dependency is needed when running the suite.

The test reads and hashes these committed bytes, submits them to the real
converter, and checks its actual output. Fixture content is not a replacement
converter response.
