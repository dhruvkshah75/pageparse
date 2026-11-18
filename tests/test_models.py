import asyncio
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
from PIL import Image
import pytest

from pageparse.config import Settings, endpoint_label
from pageparse.models import (
    Nemotron, Ollama, PROMPT, ServiceError, completion_content, validate_layout,
)

BLOCK = "<x_0.1><y_0.2>Example<x_0.7><y_0.3><class_Title>"


def png():
    stream = io.BytesIO()
    Image.new("RGB", (100, 200)).save(stream, "PNG")
    return stream.getvalue()


def helper(raw, *, image_size):
    assert raw == BLOCK
    assert image_size == (100, 200)
    return {"markdown": "# Example", "blocks": [{
        "class": "Title", "text": "Example", "content": "Example",
        "bbox": [.1, .2, .7, .3], "bbox_pixels": [10, 40, 70, 60],
    }], "warnings": [], "raw_out": raw}


def test_nemotron_request_and_coordinates():
    image = png()

    def route(request):
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer EMPTY"
        assert json.loads(request.content) == {
            "model": "nemotron-parse", "temperature": 0, "top_k": 1,
            "repetition_penalty": 1.1, "skip_special_tokens": False,
            "max_tokens": 4096, "stream": False,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(image).decode(),
                }},
            ]}],
        }
        return httpx.Response(200, json={"choices": [
            {"finish_reason": "stop", "message": {"content": BLOCK}},
        ]})

    model = Nemotron(Settings(), httpx.MockTransport(route), helper)
    payload = asyncio.run(model.parse(image, 100, 200))
    assert payload["blocks"][0]["bbox_pixels"] == [10, 40, 70, 60]
    assert payload["coordinate_frame"] == "content_normalized"
    assert "raw_out" not in payload


def test_discovery_failure_missing_and_recovery():
    responses = iter([None, ["nemotron-parse-v2"], ["nemotron-parse"]])

    def route(request):
        assert request.url.path == "/v1/models"
        if (models := next(responses)) is None:
            raise httpx.ConnectError("private URL")
        return httpx.Response(200, json={"data": [{"id": model} for model in models]})

    model = Nemotron(Settings(), httpx.MockTransport(route), helper)

    async def statuses():
        return [(await model.status()).state for _ in range(3)]

    assert asyncio.run(statuses()) == ["offline", "model missing", "ready"]
    assert endpoint_label("http://user:pass@[::1]:9000/internal?secret=yes") == "http://[::1]:9000"


def test_context_rejection():
    route = lambda request: httpx.Response(400, json={"error": {"message": "context length: private"}})
    model = Nemotron(Settings(), httpx.MockTransport(route), helper)
    with pytest.raises(ServiceError, match="nemotron-parse rejected 100×200.*PAGEPARSE") as raised:
        asyncio.run(model.parse(png(), 100, 200))
    assert raised.value.code == "context_limit"
    assert "private" not in str(raised.value)


@pytest.mark.parametrize("body,code", [
    (None, "invalid_completion"),
    ({"choices": []}, "invalid_completion"),
    ({"choices": [{"finish_reason": "tool_calls", "message": {"content": BLOCK}}]}, "invalid_completion"),
    ({"choices": [{"finish_reason": "length", "message": {"content": BLOCK}}]}, "truncated"),
    ({"choices": [{"finish_reason": "length"}]}, "truncated"),
])
def test_completion_envelope(body, code):
    with pytest.raises(ServiceError) as raised:
        completion_content(body)
    assert raised.value.code == code


@pytest.mark.parametrize("raw", [
    " ", "</s>", "unexpected" + BLOCK, BLOCK + "unexpected",
    "<class_Text>" + BLOCK, BLOCK + "<x_0><y_0>", "<x_0><y_0>" + BLOCK,
    BLOCK.replace("Example", "<predict_bbox>Example"), BLOCK + "<class_Text>",
])
def test_reject_unexplained_content(raw):
    with pytest.raises(ServiceError, match="layout blocks"):
        validate_layout(raw)


def test_wrappers_and_exact_blank_allowlist():
    assert validate_layout("<s>" + BLOCK + "\n" + BLOCK + "</s>")
    choice = {"finish_reason": "stop", "message": {"content": ""}}
    observed = {"choices": [choice], "usage": {
        "prompt_tokens": 308, "completion_tokens": 1, "total_tokens": 309,
    }}
    assert not validate_layout(completion_content(observed))
    with pytest.raises(ServiceError):
        completion_content({"choices": [choice]})
    with pytest.raises(ServiceError):
        completion_content({**observed, "usage": {"completion_tokens": 0}})
    # A synthetic form tests the exact-match mechanism without enabling it in production.
    assert not validate_layout("</s>", frozenset({"</s>"}))
    with pytest.raises(ServiceError):
        validate_layout(" </s>", frozenset({"</s>"}))


@pytest.mark.parametrize("frames,expected", [
    ([{"message": {"content": "a"}, "done": False}, {"message": {"content": "b"}, "done": True}], "ab"),
    ([{"error": "model does not support chat"}], "does not support chat"),
    ([{"error": "private body echoed by server"}], "generation error"),
    ([{"message": {"content": "a"}, "done": False}], "before finishing"),
    (None, "does not support chat"),
])
def test_ollama_stream(frames, expected):
    def route(request):
        assert request.url.path == "/api/chat"
        assert json.loads(request.content) == {
            "model": "qwen2.5-coder:3b", "stream": True, "keep_alive": 0,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        if frames is None:
            return httpx.Response(400, json={"error": "model does not support chat"})
        return httpx.Response(200, text="\n".join(map(json.dumps, frames)))

    async def chat():
        model = Ollama(Settings(), httpx.MockTransport(route))
        return "".join([piece async for piece in model.chat(
            "qwen2.5-coder:3b", [{"role": "user", "content": "Hello"}],
        )])

    if expected == "ab":
        assert asyncio.run(chat()) == expected
    else:
        with pytest.raises(ServiceError, match=expected) as raised:
            asyncio.run(chat())
        assert "private body" not in str(raised.value)


def test_installed_nemotron_helper():
    directory = Path(os.getenv("PAGEPARSE_NEMOTRON_HELPERS", "/opt/nemotron/model"))
    if "PAGEPARSE_NEMOTRON_HELPERS" not in os.environ and not (directory / "nemotron_parse").is_dir():
        pytest.skip("NVIDIA helper not installed")
    subprocess.run([sys.executable, "-c", """
import importlib.util
import sys
from pathlib import Path
from pageparse.models import load_helper, postprocess
root = Path(sys.argv[1]).resolve()
assert all(importlib.util.find_spec(name) is None for name in ('torch', 'transformers', 'vllm'))
helper = load_helper(root)
assert Path(sys.modules['nemotron_parse'].__file__).resolve().is_relative_to(root)
payload = postprocess(sys.argv[2], 100, 200, helper)
assert 'Example' in payload['markdown']
assert payload['blocks'][0]['bbox'] == [.1, .2, .7, .3]
assert payload['blocks'][0]['bbox_pixels'] == [10, 40, 70, 60]
assert not {'torch', 'transformers', 'vllm'} & sys.modules.keys()
""", str(directory), BLOCK], check=True)
