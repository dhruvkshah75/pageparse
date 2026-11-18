# PageParse

PageParse is a Linux terminal app for turning local PDFs and images into Markdown and structured JSON with NVIDIA Nemotron 3.5 Parse. A separate Chat workspace uses installed Ollama models, including `qwen2.5-coder:3b`.

## Setup

From the repository root, create a lightweight Python 3.12 environment and install the client.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install .
./run-pageparse
```

Keep vLLM, GPU libraries, and model weights in the server environment.

## Start PageParse and its servers together

Run the launcher from the repository root.

```sh
./run-pageparse          # Nemotron, then open Parse
./run-pageparse --image  # Same as the default
./run-pageparse --chat   # Ollama, then open Chat
./run-pageparse --both   # Nemotron and Ollama, then open Parse
```

The launcher starts the selected local servers in the background and opens the TUI when their APIs are ready. It reuses servers that are already running. **Ctrl+Q** quits the TUI and stops the servers started by this launch. Startup logs go under `$XDG_STATE_HOME/pageparse/logs/` or `~/.local/state/pageparse/logs/`.

The launcher uses the existing installation in `/opt/nemotron` and the `ollama` command on your PATH. Ollama uses your existing models. You can set `OLLAMA_MODELS` to choose their directory. On a 4 GB GPU, `--both` can leave less GPU memory for Ollama, which may run some work on the CPU.

Use `pageparse` or `python -m pageparse` when you already manage the services yourself. Those entry points open the TUI from the current directory.

## Manually managed servers

Press **F4** to refresh discovery after starting a service.

**Nemotron.** Check the model endpoint first. If it already lists `nemotron-parse`, reuse that server. If the endpoint is down, start the server in another terminal. The command below uses the repository's 4 GB laptop setup. Adjust the paths for a different installation.

```sh
curl -fsS http://localhost:8000/v1/models
```

```sh
HF_HOME=/opt/nemotron/hf_cache /opt/nemotron/.venv/bin/vllm serve /opt/nemotron/model \
  --host 127.0.0.1 \
  --served-model-name nemotron-parse \
  --trust-remote-code \
  --max-num-seqs 1 \
  --max-model-len 8744 \
  --gpu-memory-utilization 0.80
```

**Ollama.** Check the installed tags with the commands below. If the service is down, start it separately with `ollama serve`. Chat prefers `qwen2.5-coder:3b` when it is installed and accepts other tags that support chat.

```sh
ollama list
curl -fsS http://localhost:11434/api/tags
```

## Documents and exports

Copy an image or screenshot to your desktop clipboard and press **Ctrl+V** in Parse. If your terminal intercepts Ctrl+V, press **F8**. The source field shows the image dimensions when it is ready. Press **F5** to parse, then **F7** to export. Clipboard images stay in memory.

On Linux, the app can read images through the system GTK 4 bindings, `wl-paste` on Wayland, or `xclip` on X11. If your Wayland session blocks the GTK clipboard reader, install `wl-clipboard` with your distribution's package manager.

- Parse accepts scanned and text PDFs, PNG, JPEG, and WebP. Leave the PDF page range blank for all pages or enter a one-based selection such as `1,3-5`. A single CPU worker renders selected PDF pages in order, one page at a time.
- Preview results in Rendered, Markdown, or JSON before saving. Rendered displays tables, and the Markdown tab and exported .md file use Markdown pipe tables. Merged headers become per-column labels. JSON preserves the original HTML, including exact row and column spans. Use the format menu beside the output directory to save Markdown, JSON, or both. Export defaults to both and saves to `outputs/` under the directory where you launched PageParse. Edit the Output directory field to choose another folder. PageParse creates the default directory if it is missing. Partial exports retain completed pages, require confirmation, and use a `.partial` filename. The JSON includes original page numbers, image dimensions, and normalized and pixel bounding boxes.
- The export index tracks generated files across sessions. The Exports tab updates when you open it and checks for externally deleted files while it is visible. Press Space to select export groups and choose whether to remove Markdown, JSON, or both. Confirm the exact paths before permanent deletion. It checks file identity and content and skips changed files and directories outside its cleanup boundary. For app-managed cleanup, use a directory you own that is writable only by you. If an existing `outputs/` directory is group-writable, run `chmod go-w outputs` from the repository root.

## Use the terminal workspaces

| Key | Action |
| --- | --- |
| **F1** | Parse |
| **F2** | Chat |
| **F3** | Exports |
| **F4** | Refresh servers and the export index |
| **F5** | Start parsing or send the current chat message |
| **F6** | Cancel the active parse or stop the active chat |
| **F7** | Export the current result from Parse |
| **Ctrl+V** or **F8** | Paste a clipboard image in Parse |
| **Ctrl+Q** | Quit |

- **Chat.** Use the multiline composer, then Send. Stop keeps streamed text visible as incomplete. Subsequent requests include completed turns. Use New chat to clear the transcript.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `PAGEPARSE_VLLM_URL` | `http://localhost:8000/v1` | vLLM API root |
| `PAGEPARSE_NEMOTRON_MODEL` | `nemotron-parse` | Exact served ID for document parsing |
| `PAGEPARSE_NEMOTRON_HELPERS` | `/opt/nemotron/model` | Directory containing the NVIDIA `nemotron_parse` helper package |
| `PAGEPARSE_VLLM_API_KEY` | `EMPTY` | Bearer token for vLLM |
| `PAGEPARSE_OLLAMA_URL` | `http://localhost:11434` | Ollama API root |
| `PAGEPARSE_NEMOTRON_MAX_TOKENS` | `4096` | Maximum generated tokens per page |
| `XDG_STATE_HOME` | `~/.local/state` | Base directory for `pageparse/exports.sqlite3` |

The configured endpoints receive page images and chat prompts for their respective workspaces.

## Troubleshooting

- **Endpoint offline or model missing.** Run the endpoint check above and verify that the configured URL and model ID match the running server.
- **NVIDIA helper unavailable.** Set `PAGEPARSE_NEMOTRON_HELPERS` to the directory containing the shipped `nemotron_parse` package from your Nemotron installation.
- **Context limit or truncation.** Lower `PAGEPARSE_NEMOTRON_MAX_TOKENS` if vLLM rejects the request at its context limit. If an otherwise accepted request stops at the generation ceiling, try a higher ceiling within the server's available context.
- **GPU memory allocation fails.** vLLM can hold GPU memory while idle, and Ollama can use CPU fallback. On a 4 GB GPU, retry with only the backend you need running.
- **Export or cleanup fails.** Check the destination and state directory permissions. Use an accessible output folder without symlink components on a local filesystem that supports hard links. Retry if the index is busy. Exports lists staging paths that require attention.

See [SPEC.md](SPEC.md) for the full behavior and output contracts.

## Verification

```sh
python -m pip install -e '.[test]'
python -m pytest -q
python tests/live_smoke.py
```

The live script creates temporary synthetic inputs and requires both model servers. See the [checkpoint and release results](docs/checkpoints.md) for the laptop measurements.
