import sys
from unittest.mock import MagicMock

import httpx
import pytest

from pageparse import launcher
from pageparse.launcher import LaunchError, Server, ensure_server, startup_lock, stop_owned


def test_existing_nemotron_is_reused_or_reports_wrong_model(tmp_path, monkeypatch):
    server = Server("Nemotron", "http://localhost:8000/v1", "/v1", "/models", "nemotron-parse", "EMPTY")
    names = iter(("nemotron-parse", "other"))
    popen = MagicMock()
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)

    def respond(request):
        assert request.url.path == "/v1/models"
        assert request.headers["authorization"] == "Bearer EMPTY"
        return httpx.Response(200, json={"data": [{"id": next(names)}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        owned = []
        ensure_server(server, client, owned, tmp_path)
        with pytest.raises(LaunchError, match="must serve"):
            ensure_server(server, client, owned, tmp_path)
    assert not owned and not list(tmp_path.iterdir())
    popen.assert_not_called()


def test_owns_and_reaps_only_the_new_process(tmp_path, monkeypatch):
    server = Server("Ollama", "http://localhost:11434", "", "/api/tags")
    states = iter(("offline", "starting", "ready"))
    monkeypatch.setattr(Server, "probe", lambda *args: next(states))
    monkeypatch.setattr(Server, "local_command", lambda self: ([sys.executable, "-c", "import time; time.sleep(60)"], None))
    owned = []
    try:
        ensure_server(server, None, owned, tmp_path)
        process, = owned
        assert process.poll() is None
        assert len(list(tmp_path.glob("ollama-*.log"))) == 1
    finally:
        stop_owned(owned)
    assert process.returncode is not None


def test_startup_lock_and_remote_launch_boundary(tmp_path):
    with startup_lock(tmp_path):
        with pytest.raises(LaunchError, match="Another PageParse launcher"):
            with startup_lock(tmp_path):
                pass
    with startup_lock(tmp_path):
        pass
    remote = Server("Ollama", "http://server.example:11434", "", "/api/tags")
    with pytest.raises(LaunchError, match="separately"):
        remote.local_command()


@pytest.mark.parametrize(("args", "servers", "workspace"), [
    ([], ["Nemotron"], "parse"),
    (["--image"], ["Nemotron"], "parse"),
    (["--chat"], ["Ollama"], "chat"),
    (["--both"], ["Nemotron", "Ollama"], "parse"),
])
def test_launch_flags(tmp_path, monkeypatch, args, servers, workspace):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(launcher, "ensure_server", lambda server, *rest: calls.append(server.name))
    app = MagicMock()
    monkeypatch.setattr("pageparse.app.PageParse", app)
    assert launcher.main(args) == 0
    assert calls == servers
    assert app.call_args.kwargs["initial_workspace"] == workspace
    app.return_value.run.assert_called_once()


def test_custom_nemotron_root_controls_runtime_and_helper(tmp_path, monkeypatch):
    from pageparse.config import Settings

    monkeypatch.setenv("PAGEPARSE_NEMOTRON_ROOT", str(tmp_path))
    monkeypatch.setenv("PAGEPARSE_VLLM_URL", "http://localhost:8000/v1")
    monkeypatch.delenv("PAGEPARSE_NEMOTRON_HELPERS", raising=False)
    settings = Settings.from_env()
    server = Server("Nemotron", settings.vllm_url, "/v1", "/models",
                    settings.nemotron_model, settings.api_key, settings.nemotron_root)
    with pytest.raises(LaunchError, match="Nemotron setup is missing"):
        server.local_command()

    runtime = tmp_path / ".venv/bin/vllm"
    runtime.parent.mkdir(parents=True)
    runtime.touch(mode=0o700)
    settings.helpers.mkdir()
    command, environment = server.local_command()
    assert command[:3] == [str(runtime), "serve", str(settings.helpers)]
    assert environment["HF_HOME"] == str(tmp_path / "hf_cache")
