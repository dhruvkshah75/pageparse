"""HTTP protocols for the externally managed model servers."""
import asyncio
import base64
import importlib
import importlib.util
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

import httpx

from .config import Settings, endpoint_label

PROMPT = "</s><s><predict_bbox><predict_classes><output_markdown><predict_text_in_pic>"
# Add only forms observed on a live empty page.
BLANK_COMPLETIONS = frozenset({""})
_COORD = r"[0-9]+(?:\.[0-9]+)?"
_POINT = rf"<x_{_COORD}><y_{_COORD}>"
_RESERVED = r"</?(?:x_|y_|class_|predict_|output_|image|pad|s>|unk)"
_BLOCK = re.compile(
    rf"{_POINT}(?:(?!{_RESERVED}).)*?{_POINT}<class_[A-Za-z][A-Za-z0-9_-]*>",
    re.DOTALL,
)
_HELPER_LOCK = Lock()


async def run_sync(function, *args):
    """Run blocking local work and drain it before releasing a cancelled job."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


class ServiceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Discovery:
    state: str
    endpoint: str
    message: str
    models: tuple[str, ...] = ()


def load_helper(directory: Path):
    root = directory.resolve()
    name = "nemotron_parse"
    with _HELPER_LOCK:
        # Keep the public package name for the shipped helper's absolute imports.
        previous = {key: value for key, value in sys.modules.copy().items()
                    if key == name or key.startswith(name + ".")}
        existing = previous.get(name)
        if existing and Path(existing.__file__).resolve().is_relative_to(root):
            return existing.postprocess_document
        try:
            spec = importlib.machinery.PathFinder.find_spec(name, [str(root)])
            if not spec or not spec.loader or not spec.origin:
                raise ImportError
            if not Path(spec.origin).resolve().is_relative_to(root):
                raise ImportError
            for key in previous:
                del sys.modules[key]
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module.postprocess_document
        except Exception:
            for key in list(sys.modules):
                if key == name or key.startswith(name + "."):
                    del sys.modules[key]
            sys.modules.update(previous)
            raise ServiceError(
                "model_unavailable",
                f"NVIDIA helper unavailable at {directory}. Set PAGEPARSE_NEMOTRON_HELPERS to the model helper directory.",
            ) from None


def completion_content(body):
    try:
        choice = body["choices"][0]
        reason = choice["finish_reason"]
        if reason == "length":
            raise ServiceError("truncated", "The page reached the generation limit.")
        raw = choice["message"]["content"]
        if reason not in ("stop", "eos") or not isinstance(raw, str):
            raise ValueError
        if raw in BLANK_COMPLETIONS:
            usage = body["usage"]
            prompt = usage["prompt_tokens"]
            tokens = usage["completion_tokens"]
            if (reason != "stop" or type(prompt) is not int or prompt < 1
                    or type(tokens) is not int or tokens != 1
                    or usage.get("total_tokens") != prompt + tokens):
                raise ValueError
        return raw
    except (KeyError, IndexError, ValueError, TypeError):
        raise ServiceError("invalid_completion", "Nemotron returned an invalid completion.") from None


def validate_layout(raw: str, blank_forms=BLANK_COMPLETIONS) -> bool:
    if raw in blank_forms:
        return False
    content = raw.strip().removeprefix("<s>").removesuffix("</s>").strip()
    if content:
        pos = 0
        while match := _BLOCK.match(content, pos):
            pos = match.end()
            while pos < len(content) and content[pos].isspace():
                pos += 1
        if pos == len(content):
            return True
    raise ServiceError("invalid_completion", "Nemotron returned incomplete, empty, or invalid layout blocks.")


def postprocess(raw, width, height, helper):
    has_blocks = validate_layout(raw)
    if not has_blocks:
        return {"markdown": "", "blocks": [], "warnings": [], "coordinate_frame": "content_normalized"}
    try:
        payload = helper(raw, image_size=(width, height))
        if not payload["blocks"] or not isinstance(payload["markdown"], str):
            raise ValueError
        blocks = []
        for block in payload["blocks"]:
            result = {key: block[key] for key in ("class", "text", "content", "bbox", "bbox_pixels")}
            for key in ("bbox", "bbox_pixels"):
                box = result[key]
                if len(box) != 4 or not all(math.isfinite(value) for value in box):
                    raise ValueError
                result[key] = list(box)
            if not all(isinstance(result[key], str) for key in ("class", "text", "content")):
                raise ValueError
            blocks.append(result)
        warnings = [{"block_index": warning["block_index"], "message": warning["message"]}
                    for warning in payload["warnings"]]
        return {"markdown": payload["markdown"], "blocks": blocks, "warnings": warnings,
                "coordinate_frame": "content_normalized"}
    except Exception:
        raise ServiceError("postprocess_failed", "NVIDIA postprocessing failed for this page.") from None


def _error_text(response):
    try:
        error = response.json()["error"]
        return str(error.get("message", "") if isinstance(error, dict) else error).lower()
    except (KeyError, ValueError, TypeError):
        return ""


def _resource_error(text):
    return any(marker in text for marker in ("out of memory", "unable to allocate", "cuda error", "requires more system memory"))


class Backend:
    def __init__(self, url, name, transport=None, headers=None):
        self.url = url.rstrip("/")
        self.endpoint = endpoint_label(url)
        self.name = name
        self.transport = transport
        self.headers = headers

    def client(self, read=180):
        return httpx.AsyncClient(
            transport=self.transport, trust_env=False, headers=self.headers,
            timeout=httpx.Timeout(read, connect=3, write=10, pool=3),
        )

    async def discover(self, path, key, model_key, missing_message, required=None):
        try:
            async with self.client(read=5) as client:
                response = await client.get(self.url + path)
                response.raise_for_status()
                entries = response.json()[key]
                if not isinstance(entries, list):
                    raise ValueError
                models = tuple(dict.fromkeys(entry[model_key] for entry in entries))
                if not all(isinstance(model, str) and model for model in models):
                    raise ValueError
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, TypeError, KeyError):
            return Discovery("offline", self.endpoint, f"Check the {self.name} server and its configured URL.")
        if not models or (required and required not in models):
            return Discovery("model missing", self.endpoint, missing_message, models)
        return Discovery("ready", self.endpoint, "Ready", models)

    def request_error(self, error):
        if isinstance(error, httpx.TimeoutException):
            return ServiceError("timeout", f"{self.name} timed out. Check the server and retry.")
        if isinstance(error, httpx.ConnectError):
            return ServiceError("model_unavailable", f"Could not connect to {self.name}. Check the server and refresh.")
        return ServiceError("request_failed", f"{self.name} request failed. Check the server and retry.")

    def memory_error(self):
        return ServiceError("request_failed", f"{self.name} could not allocate memory. Retry with only this backend active.")


class Nemotron(Backend):
    def __init__(self, settings: Settings, transport=None, helper=None):
        headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else None
        super().__init__(settings.vllm_url, "vLLM", transport, headers)
        self.settings = settings
        self.helper = helper

    def get_helper(self):
        return self.helper or load_helper(self.settings.helpers)

    async def status(self):
        model = self.settings.nemotron_model
        result = await self.discover("/models", "data", "id", f"Serve {model} in vLLM and refresh.", model)
        if result.state == "ready":
            try:
                await run_sync(self.get_helper)
            except ServiceError as error:
                return Discovery("helper unavailable", self.endpoint, str(error), result.models)
        return result

    async def parse(self, png: bytes, width: int, height: int):
        helper = await run_sync(self.get_helper)
        model = self.settings.nemotron_model
        data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        payload = {
            "model": model, "stream": False, "temperature": 0, "top_k": 1,
            "repetition_penalty": 1.1, "skip_special_tokens": False,
            "max_tokens": self.settings.max_tokens,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]}],
        }
        try:
            async with self.client() as client:
                response = await client.post(self.url + "/chat/completions", json=payload)
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            raise self.request_error(error) from None
        if response.is_error:
            detail = _error_text(response)
            if response.status_code == 400 and any(key in detail for key in ("context", "maximum model length", "max_model_len", "maximum sequence length")):
                raise ServiceError("context_limit", f"{model} rejected {width}×{height} pixels at the context limit. Reduce PAGEPARSE_NEMOTRON_MAX_TOKENS.")
            if _resource_error(detail):
                raise self.memory_error()
            if response.status_code == 404:
                raise ServiceError("model_unavailable", f"vLLM could not find {model}. Refresh models.")
            raise ServiceError("request_failed", f"vLLM rejected the request (HTTP {response.status_code}).")
        try:
            body = response.json()
        except ValueError:
            body = None
        raw = completion_content(body)
        return await run_sync(postprocess, raw, width, height, helper)


class Ollama(Backend):
    def __init__(self, settings: Settings, transport=None):
        super().__init__(settings.ollama_url, "Ollama", transport)

    async def status(self):
        return await self.discover("/api/tags", "models", "name", "Install an Ollama model and refresh.")

    async def chat(self, model, messages):
        try:
            async with self.client() as client:
                async with client.stream("POST", self.url + "/api/chat", json={
                    "model": model, "messages": messages, "stream": True, "keep_alive": 0,
                }) as response:
                    if response.is_error:
                        await response.aread()
                        detail = _error_text(response)
                        if _resource_error(detail):
                            raise self.memory_error()
                        if "does not support chat" in detail or "does not support generate" in detail:
                            raise ServiceError("request_failed", "Ollama reports that this model does not support chat.")
                        raise ServiceError("request_failed", f"Ollama rejected the chat request (HTTP {response.status_code}).")
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        frame = json.loads(line)
                        if type(frame) is not dict:
                            raise ValueError
                        if "error" in frame:
                            detail = str(frame["error"]).lower()
                            if _resource_error(detail):
                                raise self.memory_error()
                            if "does not support chat" in detail:
                                raise ServiceError("request_failed", "Ollama reports that this model does not support chat.")
                            raise ServiceError("request_failed", "Ollama returned a generation error. Check the server.")
                        done = frame.get("done")
                        if type(done) is not bool:
                            raise ValueError
                        message = frame.get("message", {} if done else None)
                        if type(message) is not dict or not isinstance(message.get("content", ""), str):
                            raise ValueError
                        if chunk := message.get("content"):
                            yield chunk
                        if done:
                            return
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            raise self.request_error(error) from None
        except (ValueError, TypeError):
            raise ServiceError("request_failed", "Ollama returned an invalid chat stream.") from None
        raise ServiceError("request_failed", "Ollama disconnected before finishing the reply.")
