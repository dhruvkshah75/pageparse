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

## Stage 4 · Textual workspaces

- Reinstalled the editable client with both launchers. `python -m pytest -q` passed 67 tests. Both `pageparse` and `python -m pageparse` opened the styled Parse workspace with its keyboard footer in a 120×36 pseudo-terminal and exited on Ctrl+Q.
- Exercised both 80×24 and 120×36 layouts, the file picker, ordered page rendering, all preview formats, explicit export, and confirmed deletion. Verified that a confirmed partial export colliding on disk requires fresh confirmation, and that exports can be managed with both backends offline.
- Verified keyboard switching during inference, parse and chat cancellation, completed-turn history, incomplete-turn handling, model switching, transcript limits, and discovery refresh.
- Textual redirects stderr to an object without a usable file descriptor. This interfered with Python's multiprocessing resource tracker when starting the first render. The app now spawns a fresh Python renderer with an explicit private pipe and independent standard streams. Renderer protocol and cancellation escalation tests pass with this entry point.

## Stage 5 · Installed app and live release

Built `pageparse-0.1.0-py3-none-any.whl` with `python -m pip wheel --no-deps --wheel-dir <check-dir> .` and installed it in a fresh Python 3.12 environment. Both wheel entry points opened the same styled Parse workspace and footer from outside the checkout in a 120×36 terminal. Ctrl+Q closed both cleanly.

### Deterministic acceptance

The checkout suite passed 67 tests. The app tests exercise offline → missing → ready discovery, ordered PNG requests and coordinates for selected source pages, preview without file writes, cancellation and partial-export reconfirmation, offline cleanup after restart, and chat history that excludes stopped turns. Export store tests independently verify format-specific and paired deletion and preservation of neighboring files.

### Live acceptance

Ran `python tests/live_smoke.py` against the existing Ollama service and vLLM started using the laptop command in [AGENTS.md](../AGENTS.md). The script generates all input fixtures in a temporary directory and drives the actual Textual workspaces. Preflight checked `/v1/models` before launch and verified both model IDs after launch.

| Check | Outcome | Evidence |
| --- | --- | --- |
| Preflight | Pass | RTX 3050 with 4,094 MiB VRAM. vLLM 0.32.0 reported `nemotron-parse` with an 8,744-token context. Ollama 0.40.12 advertised `qwen2.5-coder:3b`. |
| Direct PNG, pixel region, export | Pass | At 640×480 pixels, recognized “Quarterly report”. Predicted region was approximately `[54.4, 83.6, 281.3, 115.1]` versus the fixture's `[54, 84, 281, 115]` pixel rectangle. Both output files were published. |
| Multi-page text PDF | Pass | Pages 1 and 2 completed in source order at 800×600 and 1,334×1,584 pixels. The dense page recovered “Small print reference A17”, all six expected table cells, and the last numbered line. |
| Dense-page token ceiling | Pass | A request ceiling of 4,096 finished with `stop` using 2,087 prompt tokens and 1,321 completion tokens. The default remains 4,096. |
| Scanned PDF | Pass | Raster-only PDF rendered at 640×480 and recovered “Quarterly report”. |
| Blank page | Pass | 640×480 blank PDF rendered an empty page. Observed response had `content: ""`, `finish_reason: "stop"`, and usage of 308 prompt, 1 completion, and 309 total tokens. The pinned fixture rejects empty responses without the matching terminal usage pattern. |
| Context rejection | Pass with controlled fixture | `python -m pytest -q tests/test_models.py -k context_rejection` exercised the vLLM HTTP 400 response and verified the message includes the model, image dimensions, and generation-ceiling setting. |
| Nemotron cancel → Ollama | Pass | Cancelled an active dense-page request, reaped the renderer, and released the app slot in about 0.11 s. The first vLLM running/waiting metric showing zero was sampled about 0.08 s after local cleanup. The Qwen turn completed after the switch. |
| Ollama stream, follow-up, and stop | Pass | The first turn produced the fixture code and the follow-up recalled it from conversation history. Stopping a later turn retained its streamed text as incomplete and released the local slot in about 0.07 s. |
| Ollama stop → Nemotron | Pass | Scanned PDF parsing completed after switching back. Ollama's `/api/ps` first showed the model unloaded about 0.07 s after local cleanup. |
| Concurrent backend residency | Pass on test fixtures | Both servers stayed running during the switches. Ollama reported 107,583,897 model bytes in VRAM during its active turn. Neither backend returned a memory allocation error during the checks. |

The live suite also passed with the installed wheel from outside the checkout. Timing measurements in the table are from the first run and reflect when the client and sampled backend endpoints reported their respective states. An idle vLLM request queue leaves the loaded model resident.
