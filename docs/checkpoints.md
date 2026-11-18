# Implementation checkpoints

## Stage 1 · Runtime and model contracts

- Installed the editable package and its test extra in a fresh Python 3.12.3 client environment. PyMuPDF uses the 1.x series to keep optional layout runtime dependencies out of the client.
- `python -m pytest -q tests/test_models.py -k 'not installed_nemotron_helper'` passed 23 tests. `PAGEPARSE_NEMOTRON_HELPERS=/opt/nemotron/model python -m pytest -q tests/test_models.py -k installed_nemotron_helper` passed and verified the NVIDIA helper without Torch, Transformers, or vLLM installed.
- The vLLM 0.32.0 server reached readiness on the 4 GB RTX 3050 with the documented flags. `GET /v1/models` reported `nemotron-parse` and context 8,744. The active Ollama installation advertised `qwen2.5-coder:3b` at `GET /api/tags`.
- Submitted synthetic 640×480 white and text PNGs with a 4,096-token ceiling. The text produced “Quarterly report” with a complete region block and 308 prompt plus 10 completion tokens. The blank returned exactly empty content, `finish_reason: "stop"`, and 308 prompt plus one completion token. The fixture pins the empty string and its terminal usage evidence. An empty body without that evidence remains invalid.

## Stage 2 · Document processing

- `python -m pytest -q tests/test_models.py tests/test_documents.py tests/test_jobs.py` passed 45 tests.
- Exercised cropped and rotated text PDF rendering, raster-only and wide pages, image orientation and transparency, pre-decode size limits, locked and invalid PDFs, and ordered page selection.
- Verified sequential requests, retained-result limits, truncation and timeouts, late HTTP reply handling, and cancellation of a renderer that ignored termination. The job returned after reaping the child and retained its completed page.

## Stage 3 · Export storage and cleanup

- `python -m pytest -q tests/test_models.py tests/test_documents.py tests/test_jobs.py tests/test_exports.py` passed 59 tests.
- Verified process exits before staging identity, before content, and after either final link. A second process received a retryable lock error while publication was in progress and recovered the saved pair afterward.
- Verified no-clobber collisions, unsupported publication, failed rollback with an indexed survivor, trusted-directory enforcement, and deletion with identity and digest checks. Confirmed originals, unrelated files, and unselected formats survive cleanup.
