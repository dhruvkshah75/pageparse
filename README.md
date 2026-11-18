# PageParse

PageParse is a Linux terminal app for turning local PDFs and images into Markdown and structured JSON with NVIDIA Nemotron 3.5 Parse. A separate Chat workspace uses installed Ollama models, including `qwen2.5-coder:3b`.

**Development status.** The model adapters, document pipeline, and export store are implemented. The terminal interface and launchers are still in progress. See the [checkpoint log](docs/checkpoints.md) for verified behavior and the [implementation plan](docs/implementation-plan.md) for release checks.

## Setup

From the repository root, create a lightweight Python 3.12 environment and run the tests.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
```

PageParse connects to separately managed model servers. Keep vLLM, GPU libraries, and model weights in the server environment. Once the terminal interface lands, reinstall the editable package and run `pageparse` or `python -m pageparse`.

## Model servers

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

- Parse accepts scanned and text PDFs, PNG, JPEG, and WebP. Leave the PDF page range blank for all pages or enter a one-based selection such as `1,3-5`. A single CPU worker renders selected PDF pages in order, one page at a time.
- The planned interface previews results before you save. Export writes a Markdown and JSON pair. Partial exports retain completed pages and require confirmation. The JSON includes original page numbers, image dimensions, and normalized and pixel bounding boxes.
- The export index tracks generated files across sessions. Cleanup can remove Markdown, JSON, or both after confirmation. It checks file identity and content and skips changed files and directories outside its cleanup boundary. For app-managed cleanup, use a directory you own that is writable only by you.

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

## Troubleshooting

- **Endpoint offline or model missing.** Run the endpoint check above and verify that the configured URL and model ID match the running server.
- **NVIDIA helper unavailable.** Set `PAGEPARSE_NEMOTRON_HELPERS` to the directory containing the shipped `nemotron_parse` package from your Nemotron installation.
- **Context limit or truncation.** Lower `PAGEPARSE_NEMOTRON_MAX_TOKENS` if vLLM rejects the request at its context limit. If an otherwise accepted request stops at the generation ceiling, try a higher ceiling within the server's available context.
- **GPU memory allocation fails.** On a 4 GB GPU, retry with only the backend you need running.
- **Export or cleanup fails.** Check the destination and state directory permissions. Use a local output filesystem that supports hard links for no-overwrite publication.

See [SPEC.md](SPEC.md) for the full behavior and output contracts.
