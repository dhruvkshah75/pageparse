## 1. Local Model & Environment Layout (`/opt/nemotron`)
- `/opt/nemotron/model` — Local copy of `nvidia/Nemotron-3.5-Parse` containing `model.safetensors`, `config.json`, `README.md`, `postprocessing.py`, `latex2html.py`, and the `nemotron_parse` package.
- `/opt/nemotron/.venv` — Isolated Python virtual environment with `vllm`, `torch`, `nemotron_parse`, `textual`, `pymupdf` (`fitz`), `pillow`, `numpy`, and `beautifulsoup4` already installed.
- `/opt/nemotron/hf_cache` — Hugging Face cache directory (`HF_HOME`).

## 2. Local Inference Server (vLLM)
- **API Endpoint:** `http://localhost:8000/v1` (OpenAI-compatible Chat Completions API)
- **Served Model Name:** `nemotron-parse`
- **Before starting the server:** Check if `http://localhost:8000/v1/models` is already responding. Never launch a second server instance if one is already running.
- **Server Start Command (VRAM-capped for laptop GPU):**
  ```bash
  HF_HOME=/opt/nemotron/hf_cache /opt/nemotron/.venv/bin/vllm serve /opt/nemotron/model \
    --served-model-name nemotron-parse \
    --trust-remote-code \
    --max-num-seqs 1 \
    --max-model-len 8744 \
    --gpu-memory-utilization 0.80
   ```
## 3. Storage & Dependency Rules
- **Keep the project small:** Keep this repository in `/home` lightweight—only include TUI source code and small client packages (`textual`, `rich`, `pymupdf`, `httpx`, `openai`, `pillow`, `numpy`, `beautifulsoup4`, etc.).
- **No heavy ML/CUDA packages in `/home`:** Never install large ML or GPU frameworks (`torch`, `torchvision`, `torchaudio`, `vllm`, `transformers`, `nvidia-*` CUDA wheels) or download model weights into `/home`. Those already live in `/opt/nemotron`.
