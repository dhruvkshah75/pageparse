# PageParse implementation plan

Use [SPEC.md](../SPEC.md) for product behavior and external contracts. Follow [AGENTS.md](../AGENTS.md) for local model setup and dependency limits. Work through the stages in order. A deterministic stage exit can pass with fixture services. Record live results separately.

## Implementation stages

### 1. Establish runtime and model contracts

**Create:** `pyproject.toml`, `.gitignore`, `pageparse/__init__.py`, `pageparse/config.py`, `pageparse/models.py`, and `tests/test_models.py`.

- Use setuptools and Python 3.12. Declare the six client dependencies from the spec and a `test` extra with pytest. Ignore local environments, build outputs, and caches. Keep the package importable without importing the NVIDIA helper. Add the launchers in Stage 4.
- Implement settings and the two HTTP adapters with an injectable HTTPX transport. Define sanitized statuses and errors for discovery and inference. Load the configured NVIDIA helper lazily and use it to process validated completions. Keep the pre-validation separate from the helper renderer. Use recorded or synthetic completions for the deterministic tests.
- Assert exact Nemotron request fields with one in-memory PNG, exact model-ID matching, discovery failure and recovery, missing IDs, context rejection, malformed envelope, unsupported finish reason, truncation taking precedence, stray layout tokens before and after a complete block, coordinate conversion, and distinct treatment of verified blank forms and ambiguous empty responses. Cover a valid Ollama stream, an unsupported tag, an error frame, and EOF before `done`.
- **Exit:** In a fresh client environment, run the install and protocol tests below. Include a test named `test_installed_nemotron_helper` that explicitly loads the configured local helper, postprocesses a complete fixture, and verifies that the import does not load torch, transformers, or vLLM. The regular hardware-free suite may skip this environment-specific test when the helper is absent. When checking this laptop, run it with the helper directory configured and require a pass. Record the import as unavailable on machines without the helper.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q tests/test_models.py -k 'not installed_nemotron_helper'
PAGEPARSE_NEMOTRON_HELPERS=/opt/nemotron/model python -m pytest -q tests/test_models.py -k installed_nemotron_helper
```

### 2. Build document processing and worker lifecycle

**Create:** `pageparse/documents.py`, `pageparse/jobs.py`, `tests/test_documents.py`, and `tests/test_jobs.py`.

- Implement source inspection, page range validation, one-page-at-a-time rendering, image normalization, and complete or partial result assembly. Generate small fixture PDFs and images in tests. Include rotated, scanned, wide, and text PDFs and EXIF-oriented, transparent, animated, and oversized direct images.
- Spawn one renderer per PDF job. The child opens the source and owns all PyMuPDF objects. The job sends one render request at a time and receives one result. The UI receives events from the job, never the renderer directly. The job owns its request task and the renderer. The app owns the shared inference slot, gives each job an ID, and ignores events from previous jobs.
- On cancellation, mark the job cancelled before signalling the worker and request task. Ignore any result that arrives afterward. Close the active HTTP request and renderer channel. Give a spawned worker two seconds to exit on shutdown, then terminate it and wait one second. If still alive, kill and reap it before releasing the slot. Wait and join off the UI thread. Report an unexpected process exit as a render failure.
- **Exit:** Run `python -m pytest -q tests/test_models.py tests/test_documents.py tests/test_jobs.py`. Assert range validation, ordered sequential requests, image bounds and orientation, and the result budget. Test cancellation during render and HTTP, late results, an unexpected child exit, and escalation for an unresponsive child. Assert that local cleanup precedes slot release and earlier pages survive truncation, failure, and cancellation.

### Cross-stage service handoffs

| Owner | Inputs | Outputs |
| --- | --- | --- |
| Model adapters | Effective configuration and injected HTTP transport. Nemotron takes PNG bytes and exact dimensions. Ollama takes a tag and ordered role/content messages. | Backend discovery state and model IDs. For Parse, one validated page payload with Markdown, blocks, warnings, and coordinate frame. For Chat, ordered content chunks and a terminal success or failure. |
| PDF renderer | `open` with source path, `render` with a one-based page number, and `shutdown`. Only one render request in flight. | `metadata` with page count, `page` with page number, PNG bytes and dimensions, or `error` with code, message, and nullable page number. Shutdown closes the document before exit. |
| Document job | Job ID, source, selected pages, model ID, adapter, and cancellation signal. | Progress with active page and counts, committed page records, and one terminal result with status and failure details after local cleanup. |
| Export store | Terminal document result with at least one page, destination and paths, or confirmed indexed file IDs for deletion. | Saved paths, collision with candidate paths, incomplete publication with survivor paths, a retryable busy result, or a sanitized error. Cleanup returns an outcome for each requested file. |
| Textual app | Settings and injectable model, job, and export services. | A single inference slot per app instance, workspace state changes, and clear status for readiness, progress, failure, cancellation, and release. |

### 3. Build export storage and cleanup

**Create:** `pageparse/exports.py` and `tests/test_exports.py`.

- Use a version 1 SQLite schema with `export_groups` and `export_files`. Groups have a unique ID, creation time, source basename, absolute original parent path, parent device and inode, and one state from pending, saved, incomplete, and attention required. File rows reference the group and use `(group_id, format)` as the primary key, with format restricted to Markdown or JSON. Store the planned staging and final basenames, staging identity, published identity, SHA-256 digest, size, modification time, and a per-file status. Constrain active final paths to be unique using the absolute parent path and final basename, represented in a unique indexed absolute path column. Remove resolved absent or deleted file records to release their claims. Keep ambiguous entries claimed for inspection.
- Acquire an advisory lock on a sibling of the index before mutation or reconciliation. Hold it across SQLite transactions and the associated filesystem operations. Time out after two seconds and return a busy status. Confirm paths before acquiring it, then check those same paths under lock. A collision on a previously confirmed pair produces a proposed new pair for another confirmation.
- Commit a pending group and random exclusive sibling staging names before creation. Create stages without following symlinks, record identities before writing content, then flush completed content and commit staging digests and metadata. Publish with same-directory atomic no-clobber linking. The final entry initially shares the recorded staging device and inode, so recovery can verify it even if publication happened before the index update. Return a destination error if the operation is unsupported. After both publications, resolve stage entries and commit saved state. Interrupted or failed cleanup stays recorded.
- Use the directory and file checks from the [export contract](../SPEC.md#result-and-export-contract) for rollback, deletion, and recovery. Reconcile under the lock. With zero verified finals and all entries resolved, clear verified stages and pending records. With one verified final, report incomplete, and with two, report saved. If any entry remains ambiguous or inaccessible, record attention required and retain verified survivors. An empty staging file from the creation-to-identity window stays recorded for inspection unless ownership can be established. An attempted deletion of a file already missing in the verified original parent clears that file record. Loss of the parent path leaves the record for retry.
- **Exit:** Run `python -m pytest -q tests/test_models.py tests/test_documents.py tests/test_jobs.py tests/test_exports.py`. Assert on disk contents and index rows after a second app starts mid-publish, process exit before staging content and after each final publication, a failed rollback, a collision after confirmation, and a destination without no-clobber support. Test moved, replaced, inaccessible, and untrusted parents, missing files, symlinks, externally changed targets detected before deletion, cancelled deletion, and a retry after a failed deletion. Assert that originals and unrelated files survive and that deleting Markdown alone leaves JSON available.

### 4. Assemble the Textual app

**Create:** `pageparse/__main__.py`, `pageparse/app.py`, `pageparse/parse_view.py`, `pageparse/chat_view.py`, `pageparse/exports_view.py`, `pageparse/pageparse.tcss`, and `tests/test_app.py`.

**Modify:** `pyproject.toml` to register the `pageparse` console command and package the stylesheet.

- Wire the shared settings, discovery, and inference slot. Connect each workspace to the existing services. Keep modal confirmation out of export-store locking. Both launchers should call the same app entry point.
- Use Textual's test driver with fixture-backed services. Check local file selection and page range, Parse → Preview → Export → Delete, accessible raw Markdown for HTML tables, usable workspace switching while busy, and unavailable invalid actions during a running job. At 80×24 and 120×36, exercise keyboard navigation, Stop and Cancel, and export and deletion confirmation. Test turn history and partial exports as listed below.
- **Exit:** Run `python -m pip install -e '.[test]'` again to install the launchers. Run `python -m pytest -q`. Smoke-launch `pageparse` and `python -m pageparse` and confirm both reach the same initial workspace.

### 5. Verify the installed app

**Create:** `README.md` with setup, commands, model server checks, supported inputs, Ollama tags, configuration, and cleanup and troubleshooting guidance.

- From the checkout, build a wheel and install it into a fresh client environment. Open both launchers from outside the checkout in a terminal. Quit after each smoke launch. Confirm that workspace styling and the keyboard footer load from the wheel. Run the fixture suite from the checkout in the development environment and record test results separately from live results.
- **Exit:** Both installed launchers work outside the checkout, the full deterministic suite passes, and the live checklist has one recorded outcome per item.

```sh
pageparse_check_dir="$(mktemp -d)"
python -m pip wheel --no-deps --wheel-dir "$pageparse_check_dir" .
python3.12 -m venv "$pageparse_check_dir/venv"
"$pageparse_check_dir/venv/bin/python" -m pip install "$pageparse_check_dir"/pageparse-*.whl
cd "$pageparse_check_dir"
"$pageparse_check_dir/venv/bin/pageparse"
"$pageparse_check_dir/venv/bin/python" -m pageparse
```

## Release acceptance

### Deterministic acceptance

- Use configured fixture endpoints that start disconnected and later respond. Verify refresh distinguishes offline, model missing, and ready. Verify Exports works while both endpoints are offline.
- Parse a known multi-page fixture with a subset of pages. Assert one PNG request per selected page in source order, exact pixel dimensions and bounding-box mapping, Markdown and schema v1 JSON, and no page-image or export files from preview. Cancel after one page. Assert that the completed page remains, the partial export needs explicit confirmation, and the JSON records cancellation and omitted pages via the selected and completed lists.
- Restart and remove only the partial export's Markdown. Verify its JSON and the source survive. Delete a second pair and verify a neighboring unrelated file survives.
- Send a chat turn and verify the next request includes its completed user and assistant messages. Stop a later turn. Verify its visible assistant text is marked incomplete and neither message from that turn appears in the following request.

### Live release acceptance

- **Preflight.** Check the configured vLLM `/v1/models` before any launch. Use [AGENTS.md](../AGENTS.md) for the laptop server command. Confirm the reported Nemotron ID and the installed Ollama tag via `/api/tags`. Record versions, effective model IDs, and the effective vLLM context limit. The laptop configuration starts at 8,744 tokens.
- **Nemotron.** Parse a direct image, multi-page text PDF, scanned PDF, and blank page. Exercise one page with known small text, a representative table, and a known region. Compare selected text and table cells to the input and confirm the known region aligns with a pixel box within the submitted dimensions. Record image dimensions, requested generation ceiling, available token usage, and completion status on the dense page. Keep the default only if that page completes within the configured context. Record the exact verified blank response form and update the fixture for it. Verify the context-rejection message with a server rejection or a controlled endpoint fixture and record which was used.
- **Ollama.** Stream a prompt and follow-up with `qwen2.5-coder:3b`. Stop a later reply and confirm local cancellation behavior. Test switching from a cancelled Nemotron job to Ollama and from a stopped Ollama turn to Nemotron on the target laptop. Record local cleanup, observed backend work release, and any GPU contention separately.
- Record pass, fail, or unavailable per check, with the commands and fixtures used. Keep an unverified live check pending when the endpoint or model is unavailable.

*Drafted with EGM*
