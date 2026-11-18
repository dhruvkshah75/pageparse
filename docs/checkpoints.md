# Implementation checkpoints

## Stage 1 · Runtime and model contracts

- Installed the editable package and its test extra in a fresh Python 3.12.3 client environment. PyMuPDF uses the 1.x series to keep optional layout runtime dependencies out of the client.
- `python -m pytest -q tests/test_models.py -k 'not installed_nemotron_helper'` passed 23 tests. `PAGEPARSE_NEMOTRON_HELPERS=/opt/nemotron/model python -m pytest -q tests/test_models.py -k installed_nemotron_helper` passed and verified the NVIDIA helper without Torch, Transformers, or vLLM installed.
- The vLLM 0.32.0 server reached readiness on the 4 GB RTX 3050 with the documented flags. `GET /v1/models` reported `nemotron-parse` and context 8,744. The active Ollama installation advertised `qwen2.5-coder:3b` at `GET /api/tags`.
- Submitted synthetic 640×480 white and text PNGs with a 4,096-token ceiling. The text produced “Quarterly report” with a complete region block and 308 prompt plus 10 completion tokens. The blank returned exactly empty content, `finish_reason: "stop"`, and 308 prompt plus one completion token. The fixture pins the empty string and its terminal usage evidence. An empty body without that evidence remains invalid.
