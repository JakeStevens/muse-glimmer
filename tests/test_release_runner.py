"""Runner shutdown and registration cleanup, using a real local child process."""

import base64
import hashlib
import importlib.util
import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest


@pytest.mark.parametrize("outcome", ["success", "failure", "poll_error"])
def test_runner_cleanup(tmp_path, monkeypatch, outcome):
    path = Path(__file__).resolve().parents[1] / "packaging/release_runner.py"
    spec = importlib.util.spec_from_file_location("release_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/bin/tool")
    monkeypatch.setattr(module.tempfile, "tempdir", str(tmp_path))

    archive = tmp_path / "runner.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for name, contents in {
            "config.sh": "#!/bin/sh\nprintf '\ufeff{\"agentId\":42}' > .runner\n",
            "run.sh": "#!/bin/sh\nexec sleep 60\n",
        }.items():
            info = tarfile.TarInfo(name)
            info.mode = 0o755
            info.size = len(contents.encode())
            bundle.addfile(info, io.BytesIO(contents.encode()))

    requests = []
    polls = 0

    def api(endpoint, method="GET"):
        nonlocal polls
        requests.append((endpoint, method))
        if endpoint.endswith("/runs/123"):
            polls += 1
            if polls > 1 and outcome == "poll_error":
                raise RuntimeError("Cannot read run status")
            return {
                "path": ".github/workflows/wheel.yml",
                "event": "push",
                "head_sha": "a" * 40,
                "html_url": "https://github.com/owner/repo/actions/runs/123",
                "status": "queued" if polls == 1 else "completed",
                "conclusion": outcome,
            }
        if "/contents/" in endpoint:
            return {"content": base64.b64encode(b"muse-glimmer-${{ github.run_id }}").decode()}
        if endpoint == "repos/actions/runner/releases/latest":
            return {
                "tag_name": "v1",
                "assets": [
                    {
                        "name": "actions-runner-osx-arm64-1.tar.gz",
                        "digest": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(),
                    }
                ],
            }
        if endpoint.endswith("registration-token"):
            return {"token": "private-registration-token"}
        if endpoint.endswith("/cancel") or method == "DELETE":
            return None
        raise AssertionError(endpoint)

    def command(*args):
        if args[0] == "xcrun":
            return "Metal compiler"
        if args[:3] == ("gh", "repo", "view"):
            return "owner/repo"
        assert args[:3] == ("gh", "release", "download")
        shutil.copy2(archive, Path(args[-1]) / "actions-runner-osx-arm64-1.tar.gz")
        return ""

    children = []
    popen = subprocess.Popen

    def start(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(module, "api", api)
    monkeypatch.setattr(module, "command", command)
    monkeypatch.setattr(module.subprocess, "Popen", start)
    if outcome == "poll_error":
        with pytest.raises(RuntimeError, match="Cannot read run status"):
            module.run(123)
    else:
        assert module.run(123) == (0 if outcome == "success" else 1)
    assert children and all(child.poll() is not None for child in children)
    assert ("repos/owner/repo/actions/runners/42", "DELETE") in requests
    assert (("repos/owner/repo/actions/runs/123/cancel", "POST") in requests) == (
        outcome == "poll_error"
    )
    assert not any(p.name.startswith("muse-glimmer-runner-") for p in tmp_path.iterdir())
