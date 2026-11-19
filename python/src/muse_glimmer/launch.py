from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from .config import Settings
from .runtime import MODEL_ID, serve

STARTUP_TIMEOUT = 300


def _stop_server(server: subprocess.Popen) -> None:
    if server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    # Also reap a worker left behind if the server crashed or hung during shutdown.
    try:
        os.killpg(server.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    server.wait()


def _stop_agent(agent: subprocess.Popen) -> None:
    if agent.poll() is None:
        agent.terminate()
        try:
            agent.wait(timeout=5)
        except subprocess.TimeoutExpired:
            agent.kill()
            agent.wait()


def _terminate(signum, _frame):
    raise SystemExit(128 + signum)


def _register_pi_extension() -> Path:
    # Subagents start fresh Pi processes without the parent's --extension flag.
    directory = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi/agent")
    extension = directory.expanduser().absolute() / "extensions/muse-glimmer.js"
    source = Path(__file__).with_name("pi.mjs").resolve()
    extension.parent.mkdir(parents=True, exist_ok=True)
    try:
        extension.symlink_to(source)
    except FileExistsError:
        if not extension.is_symlink():
            raise ValueError(f"Cannot register Pi provider: {extension} already exists.") from None
        previous = extension.readlink()
        if previous != source:
            if previous.name != "pi.mjs" or previous.parent.name != "muse_glimmer":
                raise ValueError(f"Cannot replace unrelated Pi extension: {extension}") from None
            # Switch installations atomically; other Pi processes may be discovering extensions.
            with tempfile.TemporaryDirectory(prefix=".muse-glimmer-", dir=extension.parent) as temp:
                replacement = Path(temp) / "provider"
                replacement.symlink_to(source)
                replacement.replace(extension)
    return extension


def launch_agent(model: Path, settings: Settings, name: str) -> int:
    label = {"muse": "Muse", "pi": "Pi", "omp": "Oh My Pi"}[name]
    binary = shutil.which(name) or shutil.which(str(Path.home() / ".local/bin" / name))
    if binary is None:
        install = {
            "muse": "Install Muse and make sure muse is on PATH.",
            "pi": "npm install -g @earendil-works/pi-coding-agent",
            "omp": "Install from https://omp.sh and make sure omp is on PATH.",
        }[name]
        raise ValueError(f"{label} is not installed. {install}")
    pi_extension = _register_pi_extension() if name == "pi" else None

    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(settings.host, settings.host)
    host = f"[{host}]" if ":" in host else host
    origin = f"http://{host}:{settings.port}"
    family = socket.AF_INET6 if ":" in settings.host else socket.AF_INET
    # Bind before starting the child so an existing server can never be mistaken for ours.
    with socket.create_server((settings.host, settings.port), family=family) as listener:
        with tempfile.TemporaryFile(mode="w+b") as log:
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "muse_glimmer.launch",
                    str(model),
                    settings.model_dump_json(),
                    str(listener.fileno()),
                    name,
                ],
                pass_fds=(listener.fileno(),),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
            )
            listener.close()
            agent = None
            old_int = signal.getsignal(signal.SIGINT)
            old_term = signal.signal(signal.SIGTERM, _terminate)
            try:
                print(f"Starting server at {origin}...", file=sys.stderr, flush=True)
                deadline = time.monotonic() + STARTUP_TIMEOUT
                with httpx.Client(trust_env=False, timeout=0.5) as client:
                    while True:
                        if server.poll() is not None:
                            raise ValueError("Server exited before it was ready.")
                        try:
                            if client.get(f"{origin}/health").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        if time.monotonic() >= deadline:
                            raise ValueError("Timed out waiting for the server to start.")
                        time.sleep(0.1)

                env = dict(os.environ)
                if name == "muse":
                    env.update(
                        OPENROUTER_API_KEY="muse-glimmer", OPENROUTER_BASE_URL=f"{origin}/v1"
                    )
                    command = [
                        binary,
                        "--provider",
                        "openrouter",
                        "--model",
                        MODEL_ID,
                        "--base-url",
                        f"{origin}/v1",
                    ]
                else:
                    env[f"MUSE_GLIMMER_{name.upper()}_MODEL"] = json.dumps(
                        {
                            "baseUrl": f"{origin}/v1",
                            "id": MODEL_ID,
                            "contextWindow": settings.context,
                        }
                    )
                    if name == "pi":
                        command = [
                            binary,
                            "--extension",
                            str(pi_extension),
                            "--provider",
                            "muse-glimmer",
                            "--model",
                            MODEL_ID,
                        ]
                    else:
                        # OMP inherits extensions into subagents. Keep its model roles
                        # and auxiliary inference on the local provider for this run.
                        env["OMP_SKIP_SETUP"] = "1"
                        local_model = f"muse-glimmer/{MODEL_ID}"
                        command = [
                            binary,
                            "--extension",
                            str(Path(__file__).with_name("omp.mjs").resolve()),
                            "--model",
                            local_model,
                            "--smol",
                            local_model,
                            "--slow",
                            local_model,
                            "--plan",
                            local_model,
                            "--offline",
                        ]
                print(f"Launching {label} using {origin}/v1", file=sys.stderr, flush=True)
                # The agent owns Ctrl+C; keep its server alive for cancelled turns.
                signal.signal(signal.SIGINT, lambda *_: None)
                agent = subprocess.Popen(command, env=env)
                while True:
                    if server.poll() is not None:
                        raise ValueError(f"Server exited while {label} was running.")
                    try:
                        result = agent.wait(timeout=0.25)
                        return result if result >= 0 else 128 - result
                    except subprocess.TimeoutExpired:
                        pass
            except ValueError:
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - 8192))
                print(log.read().decode(errors="replace"), file=sys.stderr, end="")
                raise
            finally:
                signal.signal(signal.SIGINT, lambda *_: None)
                try:
                    if agent is not None:
                        _stop_agent(agent)
                    _stop_server(server)
                finally:
                    signal.signal(signal.SIGINT, old_int)
                    signal.signal(signal.SIGTERM, old_term)


if __name__ == "__main__":
    with socket.socket(fileno=int(sys.argv[3])) as listener:
        serve(
            Path(sys.argv[1]),
            Settings.model_validate_json(sys.argv[2]),
            listener,
            api_dialect="openrouter" if sys.argv[4] == "muse" else "openai",
            # Agents may start new conversations without releasing old sessions.
            evict_idle_sessions=True,
            queue_requests=sys.argv[4] == "omp",
        )
