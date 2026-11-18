# PageParse implementation specification

**Author:** Dhruv Shah  
**Date:** 2026-11-18  
**Status:** Proposed for implementation  
**Platform:** Linux, Python 3.12, terminal

## Overview and problem

PageParse is a local terminal app that turns PDFs and images into Markdown and structured JSON with Nemotron 3.5 Parse. Users can inspect pages, save results, and reclaim disk space from the same app. Chat lets them switch to an installed Ollama text model, including Qwen 2.5 Coder 3B, without leaving the terminal app.

## Goals and scope

- Accept a local PDF, PNG, JPEG, or WebP through an in-app browser or a pasted path. Render selected PDF pages to PNG before inference. Support both scanned and text-based PDFs in the first release.
- Run one document or chat job at a time per app instance. Show the selected model, connection status, progress, and cancellation controls. Keep completed pages available after a failed or cancelled run.
- Preview document results before writing to disk. Choose Markdown, JSON, or both when exporting. Remember exports across app sessions and let users permanently remove generated Markdown, generated JSON, or both from the TUI.
- Enable Parse when vLLM serves the configured Nemotron model ID. List installed Ollama tags in Chat, prefer `qwen2.5-coder:3b` when present, and surface unsupported chat tags from the server. Provide session chat with streaming and follow-up turns.
- The TUI connects to model servers. The repository launcher can reuse existing servers or start and stop the selected local servers for its session. Model downloads, automatic retention, batch jobs across multiple documents, and additional document adapters are outside the first release.

## Critical user journeys

| Given | When | Then |
| --- | --- | --- |
| A working Nemotron server and a three-page scanned PDF | The user selects pages 2–3 and starts Parse | Two PNGs are sent sequentially. The user can preview and export results in original page order with page numbers 2 and 3 and coordinates tied to each rendered image. |
| Page 1 completed and page 2 is running | The user cancels, or page 2 times out or reaches its generation limit | Page 1 remains in preview. A partial export requires explicit confirmation. The result identifies the failed page or cancellation and accounts for the remaining pages. |
| The app is restarted after a successful export | The user selects an export and confirms deleting its Markdown | The Markdown is removed and the JSON remains listed. The TUI reports the actual outcome, including any changed, missing, or inaccessible files. |
| Ollama serves `qwen2.5-coder:3b` | The user selects it, sends a prompt, and asks a follow-up | The first reply streams into Chat and the follow-up includes prior completed turns. Stop and New chat are available. |
| Either server is stopped | The user starts the app or refreshes connections | The affected workspace shows the endpoint and recovery instructions. The rest of the app, including export cleanup, remains usable. |

## Technical plan

### Components and configuration

The Textual app drives Parse, Chat, and Exports. A document job coordinates source decoding, model inference, and page results. The Nemotron adapter owns its prompt, response validation, and NVIDIA postprocessing. The Ollama adapter owns discovery and streaming chat. An export store writes the selected formats, tracks ownership, and performs cleanup.

`PDF → PyMuPDF → one RGB PNG → Nemotron via vLLM → NVIDIA postprocessing → page result → preview → Markdown and JSON`

`Local image → Pillow → one RGB PNG → same Nemotron request path`

`User prompt → Ollama → streamed reply → session transcript`

Use Textual, HTTPX, PyMuPDF, Pillow, NumPy, and Beautiful Soup as client dependencies. Use Python's standard library SQLite for the export index. Install the client in a lightweight environment or use the existing environment under `/opt/nemotron`. Load `nemotron_parse.postprocess_document` lazily, with the configured helper directory taking precedence over installed packages. Verify that the imported package resolves inside that directory. If the helper is unavailable, show the configured path and a remedy in Parse and keep Chat and Exports available. Verify the import without GPU packages in Stage 1. [Nemotron README, Tables and postprocessing](/opt/nemotron/model/README.md#postprocessing)

| Environment variable | Default and purpose |
| --- | --- |
| `PAGEPARSE_VLLM_URL` | `http://localhost:8000/v1`, OpenAI-compatible API root |
| `PAGEPARSE_NEMOTRON_MODEL` | `nemotron-parse`, exact model ID eligible for document parsing |
| `PAGEPARSE_NEMOTRON_HELPERS` | `/opt/nemotron/model`, trusted directory containing the NVIDIA helper sources |
| `PAGEPARSE_VLLM_API_KEY` | `EMPTY`, optional bearer token for configured vLLM servers |
| `PAGEPARSE_OLLAMA_URL` | `http://localhost:11434`, Ollama API root |
| `PAGEPARSE_NEMOTRON_MAX_TOKENS` | Initial smoke-test value `4096`, positive integer generation ceiling. Confirm on the live server before shipping. |
| `XDG_STATE_HOME` | Export index at `$XDG_STATE_HOME/pageparse/exports.sqlite3`, with `~/.local/state` as the base fallback |

Start with the current working directory in the local file picker and its `outputs/` subdirectory as the default export destination. Display endpoint scheme, host, and port in connection status. Use bounded connect and read timeouts, starting with 3 seconds for connection, 5 seconds for discovery reads, and 180 seconds for inference read inactivity. Keep request bodies, credentials, document contents, and prompts out of logs and diagnostic text. Use direct HTTP connections and surface configured endpoint errors by backend.

### Parse contract

1. Validate that the source is a readable regular file, matches a supported format, and opens successfully. Provide actionable errors for bad input, password-required PDFs, and zero-page PDFs. Validate content using the relevant decoder. Treat an image as page 1 and reject animated images with guidance to select a still image.
2. Accept blank page selection for all pages, or comma-separated numbers and inclusive ranges such as `1,3-5`. Use one-based page numbers, reject invalid and out-of-range selections before inference, and deduplicate and sort valid selections into source order.
3. Open the PDF in a single spawned rendering process. Keep the document and all PyMuPDF objects there. Exchange metadata and one rendered page at a time with the app. Render the rotated and cropped page at up to 150 DPI, fitting it proportionally within 1,664 pixels wide by 2,048 high. Encode an RGB PNG in memory. For a direct image, check its dimensions at header read and reject more than 40 million source pixels before decoding. Apply EXIF orientation, composite transparency onto white, fit within the same envelope without upscaling, and encode RGB PNG. Keep Pillow's decompression-bomb protection enabled. Release page image buffers after each request. Close the PDF and reap the worker on completion, cancellation, or failure. [PyMuPDF multiprocessing guide](https://pymupdf.readthedocs.io/en/latest/recipes-multiprocessing.html), [Nemotron README, Input](/opt/nemotron/model/README.md)
4. Discover vLLM models at `GET /models` relative to its API root. Send one non-streaming `POST /chat/completions` per page. Use a user message with the exact task prompt `</s><s><predict_bbox><predict_classes><output_markdown><predict_text_in_pic>` and one base64 PNG data URL in an `image_url` part. Send `temperature=0`, `top_k=1`, `repetition_penalty=1.1`, `skip_special_tokens=false`, and the configured generation ceiling as `max_tokens`. Let the server account for image and text tokens against its context limit. On a context rejection, show the model and submitted dimensions and suggest reducing `PAGEPARSE_NEMOTRON_MAX_TOKENS`. [Nemotron README, vLLM request example](/opt/nemotron/model/README.md#vllm-inference-example)
5. Require a completion choice with string content and terminal `stop` or `eos`. Handle `length` first as `truncated`, even if its body appears complete. Reject other reasons and malformed bodies. For nonblank results, require a sequence of complete coordinate and class blocks with no unexplained text or unmatched layout tokens, including tokens before or after a valid block. Allow only documented control wrappers and repairs covered by fixtures. Use the shipped `postprocess_document` with submitted dimensions for rendering and coordinates. A zero-block result is a blank success only when its raw completion matches an empty-page form verified against the installed model and pinned in a fixture. Treat an ambiguous empty response as `invalid_completion` until that check identifies a supported form.
6. Snapshot request inputs at Start. A job progresses from validating to running, then complete, failed, or cancelled. Commit a page after response validation and postprocessing. Limit retained page records to 32 MiB of serialized UTF-8 JSON per job. Before adding a page that exceeds the budget, stop with `result_limit` and retain prior results. The first page failure stops the job. On cancel, stop issuing requests, cancel the active request, and ignore results arriving after cancellation. Release the app's job slot after local cleanup. Keep rendering, calls, postprocessing, and disk work off the UI thread. The live checklist separately observes backend work that may outlast local cancellation.

### Result and export contract

- JSON is UTF-8 with integer `schema_version` initially set to 1. Top-level keys are `source_name` (basename), `source_type` (`pdf` or `image`), `model` (served ID), `status` (`complete`, `failed`, or `cancelled`), `selected_pages` (ordered integers), `pages` (completed records), and `failure` (null or an object with `page_number`, `code`, and `message`). When cancelled with no error, `failure` is null. Consumers can find omitted pages by comparing `selected_pages` with the completed records.
- Page records use positive integer page numbers and submitted image dimensions. `coordinate_frame` is `content_normalized`. Each block's `bbox` and `bbox_pixels` are arrays of four finite numbers in `[x1, y1, x2, y2]` order. `bbox` preserves the helper's relative coordinates. `bbox_pixels` uses the submitted image's top-left origin and the helper's sorted and clipped pixel coordinates. Preserve the string fields `class`, `text`, and `content`. `warnings` is an array of objects with a zero-based integer `block_index` and string `message`. Exclude raw model output and absolute input paths.
- `failure.page_number` is null for an error before any page is active. Use stable codes `invalid_source`, `invalid_page_range`, `source_limit`, `render_failed`, `model_unavailable`, `timeout`, `context_limit`, `truncated`, `invalid_completion`, `postprocess_failed`, `request_failed`, and `result_limit`. Use a sanitized, user-facing message. A failure before any successful page has no export.
- Markdown is UTF-8 and contains each completed page's Markdown in order with an original page number marker and a horizontal rule between pages. Add a document-level partial-result notice for failed or cancelled jobs. Convert model HTML tables to Markdown pipe tables in the rendered preview, Markdown tab, and Markdown export. Combine merged headers into per-column labels. Preserve the original HTML table markup in the JSON preview and export, and identify JSON as the fallback when a table cannot be converted.
- Export is explicit and requires at least one completed page. Preview writes neither format. Derive the basename from the source stem and add `.partial` for a partial result. For collisions, use the first available shared suffix, such as `report-2.md` and `report-2.json`. Confirm the exact selected paths for a partial result. If a collision is found after confirmation, show the new paths and require fresh confirmation. Never overwrite a path. Display the actual published paths. If the destination falls outside the cleanup boundary, show that condition before writing and identify any staging paths that require manual attention afterward.
- Serialize recovery, publication, rollback, and deletion across PageParse processes sharing the index with one advisory lock per index. Acquire it off the UI thread with a two-second timeout and report a retryable busy state. Release it before waiting for confirmation. Journal planned sibling staging and final names before creating files. Record exclusive staging file identities before writing content and final staging digests before any publication. Publish each file atomically without replacement. If the filesystem cannot publish safely, report the destination error and keep the preview. A second-file failure permits verified rollback of the first file under the cleanup rules below. Index and report any survivor as incomplete. Parse and export states are independent.
- On startup, reconcile pending groups under the same lock. With zero verified final files and all entries resolved, remove verified staging files and clear the pending group. Mark a fully published selection as saved. If only part of the selection survives, list it as incomplete. Clean up staging entries with verified ownership. Keep ambiguous or inaccessible entries recorded for inspection or retry. An early crash may leave an empty staging entry whose identity was never recorded. Preserve it if ownership cannot be established. Recovery covers process exits. Power-loss durability is outside the first-release guarantee.
- Use versioned SQLite transactions for index changes. Record group identity, creation time, source basename, expected file count, parent device and inode, and per-file staging and final paths, format, digest, size, modification time, device, and inode. Keep document contents out of the index. An unavailable index disables export and cleanup with a remedy. Preview and chat remain usable.
- Exports lists tracked files by group with their paths, sizes where available, and state. Reload the list after in-app deletion, when the tab opens, and periodically while it is visible to clear confirmed external deletions. Allow multiselection and Markdown-only, JSON-only, or paired deletion. Confirm exact paths, total known bytes, and permanent deletion. Enable cleanup in trusted local output directories whose owners and permission bits indicate cooperative use. Require app-user ownership and skip directories writable by group or others. This is a baseline check. Independent same-user processes and other actors with effective write access must refrain from concurrent name replacement during cleanup. Apply this trust boundary to deletion, rollback, and recovery cleanup.
- Open the original parent without following symlink components and verify its recorded identity. Check that each candidate is a regular file with matching metadata and digest, then recheck its identity immediately before directory-relative unlink. Skip changed, unowned, symlinked, or inaccessible entries and keep their records. Clear a missing record only after confirming absence in the accessible original parent. Record each deletion outcome independently and support retry. Use indexed paths for cleanup. Preserve original inputs and unrelated files. Directory-relative rechecking detects replacements visible before the final check. A concurrent replacement between that check and unlink remains outside the guarantee.

Example schema v1 export after truncation on page 2:

```json
{
  "schema_version": 1,
  "source_name": "report.pdf",
  "source_type": "pdf",
  "model": "nemotron-parse",
  "status": "failed",
  "selected_pages": [1, 2, 3],
  "pages": [
    {
      "page_number": 1,
      "width_px": 1000,
      "height_px": 1500,
      "coordinate_frame": "content_normalized",
      "markdown": "# Quarterly report",
      "blocks": [
        {
          "class": "Title",
          "bbox": [0.1, 0.2, 0.7, 0.3],
          "bbox_pixels": [100, 300, 700, 450],
          "text": "Quarterly report",
          "content": "Quarterly report"
        }
      ],
      "warnings": []
    }
  ],
  "failure": {
    "page_number": 2,
    "code": "truncated",
    "message": "Page 2 reached the generation limit."
  }
}
```

### Chat and terminal interaction

- Use `GET /api/tags` for Ollama discovery and `POST /api/chat` with the selected tag, role/content messages, `stream: true`, and `keep_alive: 0` to request immediate unload after generation on the laptop. Parse newline-delimited JSON, append assistant content in order, and require a successful terminal `done` frame. Show installed tags and select `qwen2.5-coder:3b` initially when present. Allow other tags and surface unsupported chat models from the server response. Display error frames and early disconnects as turn failures. [Ollama model listing](https://docs.ollama.com/api/tags), [Ollama chat](https://docs.ollama.com/api/chat), [Qwen2.5 Coder catalog](https://ollama.com/library/qwen2.5-coder)
- Hold transcript state in memory. Commit the user/assistant pair to outgoing history only after a complete response. Keep failed or stopped text visible and marked as incomplete. Cap retained transcript and pending turn text together at 256 KiB of UTF-8. Reject a new prompt that exceeds the cap. If streaming reaches the cap, stop, keep the portion that fits visible as incomplete, and offer New chat. New chat and model switches start fresh conversations and are available when generation is idle. Send plain text and display model content as data.
- Build three Textual workspaces with a common header and discoverable keyboard footer. Parse uses a source and page pane beside a preview pane with Rendered, Markdown, and JSON modes. Show a progress bar with completed count, active page, elapsed time, and job state. Chat uses a scrolling transcript and multiline composer with explicit Send, Stop, and New chat. Exports uses a keyboard-selectable list and a modal confirmation. Include textual status labels, visible focus, and mouse controls. Stack panes at widths below 100 columns and verify the core journeys at 80×24 and 120×36.
- Keep workspace switching and input focus functional during inference. Make Start, Cancel, Send, and Export available as buttons and keyboard actions. Disable actions when their inputs or job state make them invalid. Use explicit refresh plus initial asynchronous discovery to distinguish ready, offline, and model missing states.

## Alternatives considered

| Alternative | Trade-off and decision |
| --- | --- |
| Generic chat client for both backends | A shared HTTP client is enough for transport. Model-specific adapters own Nemotron's task tokens, image payload, and layout postprocessing and Ollama's streaming format. |
| Launch servers inside the TUI | The repository launcher handles process ownership and startup. The TUI connects to the resulting endpoints and can also run against independently managed services. |
| Automatic retention or folder-wide deletion | Either could reclaim storage sooner. Use explicit save and indexed, confirmed file deletion so the user controls which results are retained. |

## Risks and limits

- Configured endpoints receive the page images or chat inputs for their workspace, including when the host is remote. Keep credentials, URL user information, paths and query parameters out of connection status and diagnostics.
- Cleanup assumes cooperative use of a trusted local directory. The index lock coordinates app instances sharing that index. See the [export contract](#result-and-export-contract) for the ownership boundary and recovery behavior.
- Local cancellation leaves backend idle status unconfirmed unless the backend supplies evidence that work ended. Ollama's unload request and an idle app slot do not guarantee enough GPU memory. On a backend memory allocation error, name the affected backend and suggest retrying with only that backend active.
- The rendering envelope bounds request dimensions at the cost of detail on wide or densely printed pages. Retained result and transcript budgets bound accumulated output. A context limit can still be reached before the transcript budget.

## Open verification points

- Stage 1 verifies the NVIDIA helper import in a lightweight client environment, independently of live model availability. Continue fixture-driven work if endpoints cannot be reached.
- The [live release checklist](docs/implementation-plan.md#live-release-acceptance) records the actual model tags, Nemotron generation budget and blank-page form, accuracy on known small text and table cells, and backend behavior after cancellation. Live support remains pending until those checks pass.

## Implementation plan

See the [PageParse implementation plan](docs/implementation-plan.md) for the file-by-file build order and release acceptance checks.

*Drafted with EGM*
