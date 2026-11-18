"""Client settings and safe endpoint labels."""
import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx

NEMOTRON_ROOT = Path("/opt/nemotron")


def endpoint_label(url: str) -> str:
    try:
        parsed = httpx.URL(url)
        if parsed.scheme not in ("http", "https") or not parsed.host:
            return "invalid endpoint"
        host = parsed.host
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return f"{parsed.scheme}://{host}:{port}"
    except (httpx.InvalidURL, ValueError):
        return "invalid endpoint"


@dataclass(frozen=True)
class Settings:
    vllm_url: str = "http://localhost:8000/v1"
    nemotron_model: str = "nemotron-parse"
    helpers: Path = NEMOTRON_ROOT / "model"
    api_key: str = field(default="EMPTY", repr=False)
    ollama_url: str = "http://localhost:11434"
    max_tokens: int = 4096
    index: Path = field(default_factory=lambda: Path.home() / ".local/state/pageparse/exports.sqlite3")
    nemotron_root: Path = NEMOTRON_ROOT

    @classmethod
    def from_env(cls):
        env = os.environ
        try:
            max_tokens = int(env.get("PAGEPARSE_NEMOTRON_MAX_TOKENS", "4096"))
            if max_tokens <= 0:
                raise ValueError
        except ValueError:
            raise ValueError("PAGEPARSE_NEMOTRON_MAX_TOKENS must be a positive integer.") from None
        state = Path(env.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        nemotron_root = Path(env.get("PAGEPARSE_NEMOTRON_ROOT") or NEMOTRON_ROOT).expanduser().absolute()
        return cls(
            vllm_url=env.get("PAGEPARSE_VLLM_URL", cls.vllm_url),
            nemotron_model=env.get("PAGEPARSE_NEMOTRON_MODEL", cls.nemotron_model),
            helpers=Path(env.get("PAGEPARSE_NEMOTRON_HELPERS") or nemotron_root / "model").expanduser(),
            api_key=env.get("PAGEPARSE_VLLM_API_KEY", "EMPTY"),
            ollama_url=env.get("PAGEPARSE_OLLAMA_URL", cls.ollama_url),
            max_tokens=max_tokens,
            index=state / "pageparse/exports.sqlite3",
            nemotron_root=nemotron_root,
        )
