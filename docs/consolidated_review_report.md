# PageParse Goldfish review

**Verdict:** Resolve the export safety and recovery contracts before implementing Exports. The parser and TUI can proceed with fixtures once their test contracts are pinned down. Live model support remains a release check.

**Reviewed:** [SPEC.md](../SPEC.md) and [implementation plan](implementation-plan.md), with [AGENTS.md](../AGENTS.md) as the local environment baseline. Nine fresh-context reviewers completed the five EGM stages. One reviewer also probed the installed NVIDIA postprocessor. Neither inference server was exercised.

## Blockers

### B1. Define where PageParse can safely delete exports

The spec promises to skip replacements and unowned files. Checking identity and then unlinking by name leaves an interval in which an independent writer can replace that name. A directory descriptor anchors the parent directory, but the final entry can still change. The same issue affects rollback and cleanup of staging files.

Decide which destinations support in-app deletion and what concurrent writes the ownership guarantee covers. A managed destination can support a stronger guarantee when PageParse can enforce control of directory mutations. If users can enable deletion in ordinary export folders, specify the trust assumptions and treatment of changes before and during deletion. Record and verify the original directory identity as well as file identity. Keep index entries when that directory is unavailable or replaced, and clear a missing-file row only after confirming absence there. Apply the chosen rule to rollback and recovery. Add a test for replacement at the deletion boundary that matches the documented guarantee. [Export contract](../SPEC.md#result-and-export-contract), [Stage 3](implementation-plan.md#implementation-stages)

### B2. Make export recovery safe across app instances and crashes

One app instance can reconcile another instance's pending row while files are still being published. The current sequence also writes staging files before creating the pending row, so an early crash can strand generated files outside the index.

Define an index-scoped process lock across publication, rollback, reconciliation, and deletion, with a bounded wait and a visible busy state. Define the version 1 tables, constraints, and staging and final identities. State how staging is journaled before writing, when a row becomes recoverable, and what restart does with zero, one, or two owned final files. Specify whether the guarantee covers process exits, power loss, or both. Test startup during a paused write, a crash before publication, and a crash after each file publishes. Also define what happens if an export name is taken after a partial export is confirmed. Any new paths need their own confirmation. If the destination cannot provide atomic no-clobber publication, keep the preview and explain how to choose another destination. [Export contract](../SPEC.md#result-and-export-contract), [Stage 3](implementation-plan.md#implementation-stages)

## Major findings

| ID | Finding | Proposed change |
| --- | --- | --- |
| M1 | Nemotron's request budget and the lightweight NVIDIA helper import are critical to proving the first vertical slice. | Make the helper import and a full postprocessed completion fixture explicit Stage 1 exit checks. Use the server as the authority on image-inclusive context unless a verified preflight is required. Define the context rejection message. Record the render dimensions, effective context and token settings, and a complete result from a representative live request when available. [Parse contract](../SPEC.md#parse-contract), [Stage 1](implementation-plan.md#implementation-stages) |
| M2 | The installed NVIDIA helper returned zero blocks for both an empty string and a dangling coordinate token. It also discarded a dangling token after a valid block. The JSON contract leaves several field shapes open. | Specify accepted finish reasons, credible blank-page completions, and behavior for incomplete block sequences. Add fixtures for blank, plain text without valid layout, truncated, and mixed complete/incomplete layouts. Define the field shapes, coordinate convention, warning records, nullable pre-page failures, and stable error codes with a complete JSON example. [Parse and result contracts](../SPEC.md#parse-contract) |
| M3 | The longest-edge rule can admit a width above NVIDIA's recommended 1,664 pixels. Resizing after decode also leaves source-image memory unbounded by the output cap. Page results and chat history grow throughout a session. | Define an aspect-preserving width and height envelope, a source-pixel policy before decoded copies, and practical limits or measured bounds for retained results and chat history. Test wide pages and oversized images. Use reference snippets, a known table, and bounding-box checks to make live rendering acceptance repeatable. [Parse contract](../SPEC.md#parse-contract), [release acceptance](implementation-plan.md#release-acceptance) |
| M4 | The app can finish cancelling while a backend still uses the GPU. The release checklist can pass without observing that handoff. | State whether one-job arbitration is per app instance. Separate client-job state from server availability. Add live checks for cancellation and immediate switching in both directions and record what each backend releases. Make memory-related recovery guidance specific to the observed backend error. [Chat and terminal interaction](../SPEC.md#chat-and-terminal-interaction), [release acceptance](implementation-plan.md#release-acceptance) |
| M5 | The goals can be read as requiring capability filtering for every installed Ollama tag, while the API contract permits any tag and handles unsupported chat models at request time. | State the concrete selection rules. Parse requires the configured Nemotron ID. Chat lists installed tags and uses the documented error path for unsupported tags, unless capability filtering is explicitly added. [Goals](../SPEC.md#goals-and-scope), [Chat and terminal interaction](../SPEC.md#chat-and-terminal-interaction) |
| M6 | Trust and resource limits are spread across the spec. The term local can hide the effect of choosing a custom endpoint. | Add a short risks and limits section covering where documents and chat are sent, which endpoint values are safe to display, export directory trust, cancellation limits, and rendering trade-offs. Keep live verification items separate. [Configuration](../SPEC.md#components-and-configuration), [open verification points](../SPEC.md#open-verification-points) |
| M7 | Stage 1 promises a launcher before Stage 4 creates the app. The test setup and installed-build check lack exact commands. | Make Stage 1 independently installable and runnable through service tests. Add both app entry points in Stage 4 and explicitly update package metadata to include the Textual stylesheet. Specify the build backend, test dependency setup, stage commands, and a built-package launch from outside the repo. [Implementation stages](implementation-plan.md#implementation-stages) |
| M8 | The file list assigns owners, but the plan leaves renderer messages, adapter results, test doubles, and cancellation responsibilities open. | Specify inputs and outputs between services. Define render worker metadata, page, error, and shutdown messages, who owns the HTTP client and process, and the join deadline and termination fallback. Release the app job slot after local cleanup and test cancellation during both render and inference, including late results. [Parse contract](../SPEC.md#parse-contract), [implementation stages](implementation-plan.md#implementation-stages) |
| M9 | Several acceptance tests describe an action without enough observable outcomes. | Use controlled endpoints for offline-to-online checks. Assert offline versus missing model, malformed completions, error or incomplete streams, the raw table preview, workspace switching while busy, and the exact contents of follow-up requests after a stopped turn. For partial exports, assert the confirmation, retained page, cancelled status, and omitted pages. Include keyboard checks at 80×24 and 120×36. [Implementation stages](implementation-plan.md#implementation-stages), [release acceptance](implementation-plan.md#release-acceptance) |

## Minor findings

- Explain why switching between parsing and local chat in a single TUI matters to the intended user. Replace the transient repository inventory and subjective overview wording.
- Fix the two NVIDIA README links by targeting the actual file and naming the sections or using their anchors.
- Distinguish the clean-client compatibility check from live GPU and server checks in release wording. Break the wide implementation table into stages with separate build and verification steps.

## Comprehension review

### What this proposes

PageParse turns selected PDF pages and still images into Markdown and JSON with Nemotron, keeps completed pages after failure or cancellation, and saves only on request. Exports uses a persistent index for cleanup. Chat streams installed Ollama models and includes only complete turns in future requests. The build proceeds through adapters, document processing, exports, the TUI, and installation checks.

### How the existing system works, per this document

The repository starts with setup instructions and the specification. PageParse will connect to two externally managed services and load the NVIDIA postprocessor from the local model installation. The specification records that earlier endpoint probes failed, so live inference and server-side cancellation remain unverified.

## Review traceability

| Stage | Goldfish reviewers | Consolidated findings |
| --- | --- | --- |
| 5. Initial Zoom Out | Zoom Out and Inventive Catalyst | B1 ownership boundary, B2 live-writer race, M1 early helper and inference checks, M4 GPU handoff, M5 discovery and Chat rationale |
| 6. Comprehension | Comprehension | B1 parent identity, B2 recovery survivor policy and confirmation paths, M1 context-budget evidence, M2 result schema, M4 backend cancellation |
| 7. Design Critique | Design, Structural, and Style | B1 unlink policy, B2 staging and recovery order, M1 context mechanism, M2 completion validity, M3 render and memory bounds, M4 GPU guidance, M5 tag selection, M6 risks and link repairs |
| 8. Intermediate Zoom Out | Zoom Out | B1 trusted-directory boundary and unavailable paths, B2 fail-closed publication and crash recovery, M1 and M2 Nemotron fixture contracts, M3 resource limits, M4 per-instance job and live verification scope, M5 and M6 documentation scope |
| 9. Implementation Readiness | Implementation Readiness, Codebase Details, and Style | B1 and B2 storage safety tests, M1 and M2 helper probing and live checks, M3 and M4 acceptance metrics, M7 packaging sequence, M8 service and worker boundaries, M9 end-to-end assertions |

Resolve B1 and B2 in both documents and turn the chosen guarantees into Stage 3 tests. Use M1, M2, M7, and M8 to define stage exit checks and service handoffs before the implementation agent builds across those boundaries. The remaining items can be addressed within their affected stages.
