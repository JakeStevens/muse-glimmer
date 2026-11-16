"""Run one GitHub release on this Mac, then remove the temporary runner.

Requires Python 3.11+, Git, Xcode with Metal, and an authenticated gh CLI.
For a release, bump pyproject.toml, commit, and push the matching v<version> tag.
Get its run ID with `gh run list --workflow wheel.yml`, then run:

    python packaging/release_runner.py RUN_ID

Approve the pypi deployment in GitHub when prompted. Ctrl+C cancels the run.
For a build-only check, first use `gh workflow run wheel.yml --ref main`.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command(*args: str) -> str:
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def api(path: str, method: str = "GET"):
    output = command("gh", "api", "--method", method, path)
    return json.loads(output) if output.strip() else None


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            process.wait()
            return
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def run(run_id: int) -> int:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("The release runner requires an Apple Silicon Mac.")
    for tool in ("gh", "git", "xcrun"):
        if not shutil.which(tool):
            raise RuntimeError(f"Missing {tool}; see this script's setup instructions.")
    command("xcrun", "-sdk", "macosx", "metal", "--version")
    repository = command(
        "gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"
    ).strip()
    run_path = f"repos/{repository}/actions/runs/{run_id}"
    workflow = api(run_path)
    if workflow["path"] != ".github/workflows/wheel.yml" or workflow["event"] not in {
        "push",
        "workflow_dispatch",
    }:
        raise RuntimeError("Expected a wheel.yml tag or manual workflow run.")
    if workflow["status"] == "completed":
        print(f"Run already completed: {workflow['conclusion']}")
        return 0 if workflow["conclusion"] == "success" else 1
    source = api(
        f"repos/{repository}/contents/.github/workflows/wheel.yml?ref={workflow['head_sha']}"
    )
    if "muse-glimmer-${{ github.run_id }}" not in base64.b64decode(source["content"]).decode():
        raise RuntimeError(
            "Start a run from the updated workflow; this run does not support the local runner helper."
        )

    print(workflow["html_url"], flush=True)
    name = f"muse-glimmer-{run_id}-{uuid.uuid4().hex[:8]}"
    runner_id = None
    process = None
    completed = False
    with tempfile.TemporaryDirectory(prefix="muse-glimmer-runner-") as temporary:
        work = Path(temporary)
        runner = work / "runner"
        runner.mkdir()
        try:
            release = api("repos/actions/runner/releases/latest")
            filename = f"actions-runner-osx-arm64-{release['tag_name'].removeprefix('v')}.tar.gz"
            asset = next(a for a in release["assets"] if a["name"] == filename)
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or ""):
                raise RuntimeError("GitHub did not provide a SHA-256 digest for the runner.")
            print(f"Downloading runner {release['tag_name']}...", flush=True)
            command(
                "gh",
                "release",
                "download",
                release["tag_name"],
                "--repo",
                "actions/runner",
                "--pattern",
                filename,
                "--dir",
                str(work),
            )
            archive = work / filename
            with archive.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            if digest != asset["digest"].removeprefix("sha256:"):
                raise RuntimeError("Runner checksum mismatch.")
            with tarfile.open(archive) as bundle:
                bundle.extractall(runner, filter="data")

            # Keep job tooling and native build caches inside the temporary directory.
            env = {
                key: os.environ[key]
                for key in ("HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "DEVELOPER_DIR")
                if key in os.environ
            }
            env.update(
                PATH=os.pathsep.join(
                    dict.fromkeys(
                        [str(Path(shutil.which(tool)).parent) for tool in ("gh", "git", "xcrun")]
                        + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
                    )
                ),
                UV_CACHE_DIR=str(work / "uv-cache"),
                UV_PYTHON_INSTALL_DIR=str(work / "python"),
                MUSE_GLIMMER_BUILD_CACHE=str(work / "build"),
            )
            token = api(f"repos/{repository}/actions/runners/registration-token", "POST")["token"]
            # Do not include the registration token in an exception's command line.
            try:
                configured = subprocess.run(
                    [
                        "./config.sh",
                        "--unattended",
                        "--url",
                        f"https://github.com/{repository}",
                        "--token",
                        token,
                        "--name",
                        name,
                        "--labels",
                        f"muse-glimmer-{run_id}",
                        "--work",
                        "_work",
                        "--disableupdate",
                    ],
                    cwd=runner,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError("Runner configuration timed out.") from None
            if configured.returncode:
                raise RuntimeError(
                    (configured.stdout + configured.stderr).replace(token, "[redacted]")
                )
            runner_id = json.loads((runner / ".runner").read_text(encoding="utf-8-sig"))["agentId"]
            with (work / "runner.log").open("w") as log:
                process = subprocess.Popen(
                    ["./run.sh"],
                    cwd=runner,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                previous = None
                while True:
                    workflow = api(run_path)
                    status = workflow["status"]
                    if status != previous:
                        print(
                            "Approve the pypi deployment in GitHub."
                            if status == "waiting"
                            else f"Run: {status}",
                            flush=True,
                        )
                        previous = status
                    if status == "completed":
                        completed = True
                        print(f"Result: {workflow['conclusion']}", flush=True)
                        return 0 if workflow["conclusion"] == "success" else 1
                    if process.poll() is not None:
                        raise RuntimeError(
                            "Runner exited early:\n" + (work / "runner.log").read_text()[-4000:]
                        )
                    time.sleep(15)
        finally:
            try:
                if process is not None and not completed:
                    api(run_path + "/cancel", "POST")
            finally:
                try:
                    if process is not None:
                        stop(process)
                finally:
                    # Registration can succeed even if config.sh fails afterward.
                    if runner_id is None:
                        runners = api(f"repos/{repository}/actions/runners?per_page=100")["runners"]
                        runner_id = next((r["id"] for r in runners if r["name"] == name), None)
                    if runner_id is not None:
                        api(f"repos/{repository}/actions/runners/{runner_id}", "DELETE")
                        print("Temporary runner removed.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("run_id", type=int, help="GitHub Actions run ID from the release page")
    args = parser.parse_args()

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        return run(args.run_id)
    except KeyboardInterrupt:
        print("Cancelled.")
        return 130
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(f"release runner: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
