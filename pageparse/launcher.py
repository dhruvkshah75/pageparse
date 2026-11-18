"""Start the requested local backends for a single PageParse session."""
import argparse
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import NamedTemporaryFile
from time import monotonic, sleep

import httpx

from .config import NEMOTRON_ROOT, Settings, endpoint_label

SYSTEM_OLLAMA_MODELS = Path("/usr/share/ollama/.ollama/models")
STARTUP_TIMEOUT = 180


class LaunchError(Exception):
    pass


@dataclass(frozen=True)
class Server:
    name: str
    api_url: str
    api_path: str
    discovery_path: str
    model: str | None = None
    api_key: str = ""
    nemotron_root: Path = NEMOTRON_ROOT

    def probe(self, client):
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        try:
            response = client.get(self.api_url.rstrip("/") + self.discovery_path, headers=headers)
        except httpx.ConnectError:
            return "offline"
        except (httpx.TimeoutException, httpx.RemoteProtocolError):
            return "starting"
        except (httpx.HTTPError, httpx.InvalidURL):
            raise LaunchError(f"Could not check {self.name} at {endpoint_label(self.api_url)}.") from None

        if response.status_code == 503:
            return "starting"
        if not response.is_success:
            raise LaunchError(f"{self.name} discovery returned HTTP {response.status_code} at {endpoint_label(self.api_url)}.")
        try:
            entries = response.json()["data" if self.model else "models"]
            if not isinstance(entries, list):
                raise ValueError
            identifiers = [entry["id" if self.model else "name"] for entry in entries]
            if not all(isinstance(name, str) and name for name in identifiers):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise LaunchError(f"{endpoint_label(self.api_url)} did not return a valid {self.name} model list.") from None
        if self.model and self.model not in identifiers:
            raise LaunchError(f"The running {self.name} server must serve {self.model!r}.")
        return "ready"

    def local_command(self):
        url = httpx.URL(self.api_url)
        if (url.scheme != "http" or url.host not in ("localhost", "127.0.0.1", "::1")
                or url.path.rstrip("/") != self.api_path or url.query or url.fragment or url.userinfo):
            raise LaunchError(f"Start {self.name} at {endpoint_label(self.api_url)} separately or configure a local HTTP endpoint.")
        host = "127.0.0.1" if url.host == "localhost" else url.host
        port = str(url.port or 80)
        env = os.environ.copy()
        if self.model:
            runtime = self.nemotron_root / ".venv/bin/vllm"
            weights = self.nemotron_root / "model"
            if not os.access(runtime, os.X_OK) or not weights.is_dir():
                raise LaunchError(
                    f"Nemotron setup is missing in {self.nemotron_root}. See the model setup in README.md "
                    "or set PAGEPARSE_NEMOTRON_ROOT to your existing installation."
                )
            env["HF_HOME"] = str(self.nemotron_root / "hf_cache")
            command = [
                str(runtime), "serve", str(weights),
                "--host", host, "--port", port, "--served-model-name", self.model,
                "--trust-remote-code", "--max-num-seqs", "1", "--max-model-len", "8744",
                "--gpu-memory-utilization", "0.80",
            ]
            if self.api_key:
                env["VLLM_API_KEY"] = self.api_key
            else:
                env.pop("VLLM_API_KEY", None)
        else:
            env["OLLAMA_HOST"] = f"[{host}]:{port}" if host == "::1" else f"{host}:{port}"
            if ("OLLAMA_MODELS" not in env and not (Path.home() / ".ollama/models/manifests").is_dir()
                    and (SYSTEM_OLLAMA_MODELS / "manifests").is_dir()):
                env["OLLAMA_MODELS"] = str(SYSTEM_OLLAMA_MODELS)
                env["OLLAMA_NOPRUNE"] = "1"
            command = ["ollama", "serve"]
        return command, env


@contextmanager
def startup_lock(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "startup.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LaunchError("Another PageParse launcher is starting servers. Retry after it opens.") from None
        yield


def ensure_server(server, client, owned, log_dir):
    state = server.probe(client)
    if state == "ready":
        print(f"Using running {server.name} at {endpoint_label(server.api_url)}.", flush=True)
        return

    process = None
    log_path = None
    if state == "offline":
        command, env = server.local_command()
        with NamedTemporaryFile(prefix=f"{server.name.lower()}-", suffix=".log", dir=log_dir, delete=False) as log:
            log_path = log.name
            try:
                process = subprocess.Popen(
                    command, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as error:
                raise LaunchError(f"Could not start {server.name}. Check {command[0]} and its permissions. {error.strerror}.") from None
            owned.append(process)
        print(f"Starting {server.name} in the background. Log: {log_path}", flush=True)
    else:
        print(f"Waiting for the existing {server.name} server.", flush=True)

    deadline = monotonic() + STARTUP_TIMEOUT
    while monotonic() < deadline:
        try:
            if server.probe(client) == "ready":
                return
        except LaunchError as error:
            if log_path:
                raise LaunchError(f"{error} See {log_path}.") from None
            raise
        if process is not None and process.poll() is not None:
            raise LaunchError(f"{server.name} exited during startup. See {log_path}.")
        sleep(.5)
    details = f" See {log_path}." if log_path else ""
    raise LaunchError(f"{server.name} did not become ready within {STARTUP_TIMEOUT} seconds.{details}")


def stop_owned(owned):
    for process in reversed(owned):
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    for process in reversed(owned):
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="run-pageparse", description="Start the selected PageParse servers and open the TUI.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--image", action="store_true", help="use Nemotron for document parsing (default)")
    modes.add_argument("--chat", action="store_true", help="use Ollama and open Chat")
    modes.add_argument("--both", action="store_true", help="start both backends")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    owned = []
    try:
        settings = Settings.from_env()
        servers = []
        if not args.chat:
            servers.append(Server(
                "Nemotron", settings.vllm_url, "/v1", "/models",
                settings.nemotron_model, settings.api_key, settings.nemotron_root,
            ))
        if args.chat or args.both:
            servers.append(Server("Ollama", settings.ollama_url, "", "/api/tags"))

        log_dir = settings.index.parent / "logs"
        with startup_lock(log_dir), httpx.Client(trust_env=False, timeout=2) as client:
            for server in servers:
                ensure_server(server, client, owned, log_dir)

        from .app import PageParse

        PageParse(settings, initial_workspace="chat" if args.chat else "parse").run()
    except KeyboardInterrupt:
        return 130
    except (LaunchError, OSError, ValueError) as error:
        print(f"PageParse: {error}", file=sys.stderr)
        return 1
    finally:
        stop_owned(owned)
    return 0


def exit_on_signal(signum, frame):
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, exit_on_signal)
    signal.signal(signal.SIGHUP, exit_on_signal)
    raise SystemExit(main())
