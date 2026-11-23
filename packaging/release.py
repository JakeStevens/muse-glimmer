"""Collect platform wheels and publish the same verified bytes to both registries.

Native builds and installation tests belong on each platform's runner. This
script only inspects archives and hashes, so collection/publication need no GPU.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def wheel_tags(tag: str) -> set[str]:
    """Expand platform tags for our Python/ABI-independent native wheels."""
    if not re.fullmatch(r"py3-none-[a-z0-9_]+(?:\.[a-z0-9_]+)*", tag):
        raise ValueError(f"Invalid wheel tag: {tag}")
    return {f"py3-none-{platform}" for platform in tag.removeprefix("py3-none-").split(".")}


def platforms() -> dict:
    entries = json.loads((ROOT / ".github/release-platforms.json").read_text())
    if not entries:
        raise ValueError("At least one release platform is required")
    tags = set()
    for name, entry in entries.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            raise ValueError(f"Invalid platform identifier: {name}")
        expanded = wheel_tags(entry["wheel_tag"])
        if expanded & tags:
            raise ValueError(f"Overlapping or duplicate wheel tag: {entry['wheel_tag']}")
        if not entry["runner"] or entry["runner"][0] != "self-hosted":
            raise ValueError(f"Expected self-hosted runner labels for {name}")
        if not re.fullmatch(r"[a-z_]+\.yml", entry["workflow"]):
            raise ValueError(f"Invalid build workflow for {name}")
        tags.update(expanded)
    return entries


def context() -> dict:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    commit = command("git", "-C", str(ROOT), "rev-parse", "HEAD")
    if os.environ.get("GITHUB_SHA", commit) != commit:
        raise ValueError("Checkout differs from the workflow's source commit")
    ref = os.environ.get("GITHUB_REF", "")
    if ref.startswith("refs/tags/") and ref != f"refs/tags/v{version}":
        raise ValueError("Tag and package version differ")
    pin = command("git", "-C", str(ROOT), "ls-tree", "HEAD", "third-party/executorch").split()
    if len(pin) != 4 or pin[:2] != ["160000", "commit"]:
        raise ValueError("ExecuTorch must be pinned by a gitlink")
    return dict(version=version, commit=commit, ref=ref, executorch=pin[2])


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inspect_wheel(wheel: Path, platform: str, release: dict) -> dict:
    tag = platforms()[platform]["wheel_tag"]
    expected = f"muse_glimmer-{release['version']}-{tag}.whl"
    if wheel.name != expected:
        raise ValueError(f"Expected {expected}, got {wheel.name}")
    with zipfile.ZipFile(wheel) as archive:
        def one(suffix):
            names = [n for n in archive.namelist() if n.endswith(suffix)]
            if len(names) != 1:
                raise ValueError(f"Expected exactly one {suffix} in {wheel.name}")
            return archive.read(names[0]).decode()

        metadata = Parser().parsestr(one(".dist-info/METADATA"))
        if metadata["Name"] != "muse-glimmer" or metadata["Version"] != release["version"]:
            raise ValueError(f"Package metadata differs from the release: {wheel.name}")
        headers = Parser().parsestr(one(".dist-info/WHEEL")).get_all("Tag", [])
        actual_tags = {value for header in headers for value in wheel_tags(header)}
        if actual_tags != wheel_tags(tag):
            raise ValueError(f"Wheel tags differ from the platform: {wheel.name}")
        provenance = json.loads(one("muse_glimmer/_vendor/provenance.json"))
        if provenance["revision"] != release["executorch"] or "local_changes" in provenance:
            raise ValueError(f"Wheel must use the clean, pinned ExecuTorch source: {wheel.name}")
    return dict(platform=platform, filename=wheel.name, sha256=digest(wheel), size=wheel.stat().st_size)


def write_bundle(directory: Path, wheels: list[dict]) -> None:
    wheels = sorted(wheels, key=lambda entry: entry["platform"])
    (directory / "release.json").write_text(json.dumps({**context(), "wheels": wheels}, indent=2) + "\n")
    (directory / "SHA256SUMS").write_text("".join(f"{w['sha256']}  {w['filename']}\n" for w in wheels))


def load_bundle(directory: Path, platform: str | None = None) -> dict:
    manifest = json.loads((directory / "release.json").read_text())
    expected = context()
    if {key: manifest.get(key) for key in expected} != expected:
        raise ValueError("Artifacts must share this checkout's version, commit, ref and ExecuTorch pin")
    required = {platform} if platform else set(platforms())
    wheels = manifest["wheels"]
    if len(wheels) != len(required) or {w["platform"] for w in wheels} != required:
        raise ValueError("Missing, unexpected or duplicate release platforms")
    if len({w["filename"] for w in wheels}) != len(wheels):
        raise ValueError("Platforms must have distinct wheel filenames")
    if {p.name for p in directory.glob("*.whl")} != {w["filename"] for w in wheels}:
        raise ValueError("Wheel files differ from the manifest")
    for entry in wheels:
        wheel = directory / entry["filename"]
        if wheel.name != entry["filename"] or inspect_wheel(wheel, entry["platform"], expected) != entry:
            raise ValueError(f"Artifact checksum or metadata mismatch: {entry['filename']}")
    return manifest


def attest(directory: Path, manifest: dict) -> None:
    repository = os.environ["GITHUB_REPOSITORY"]
    for entry in manifest["wheels"]:
        workflow = platforms()[entry["platform"]]["workflow"]
        command(
            "gh", "attestation", "verify", str(directory / entry["filename"]),
            "--repo", repository, "--signer-workflow", f"{repository}/.github/workflows/{workflow}",
            "--signer-digest", manifest["commit"],
            "--source-digest", manifest["commit"], "--source-ref", manifest["ref"],
        )


def plan() -> None:
    release = context()
    builds = []
    for name, entry in platforms().items():
        job = dict(platform=name, runner=[*entry["runner"], f"muse-glimmer-{os.environ['GITHUB_RUN_ID']}"])
        builds.append(job)
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for name, value in {"build": {"include": builds}, "platforms": list(platforms())}.items():
            output.write(f"{name}={json.dumps(value)}\n")
    print(f"Release {release['version']} at {release['commit']}: {', '.join(platforms())}")


def record(directory: Path, platform: str) -> None:
    wheels = list(directory.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("Each platform build must produce exactly one wheel")
    entry = inspect_wheel(wheels[0], platform, context())
    write_bundle(directory, [entry])
    summary = json.dumps({**context(), **entry}, indent=2)
    print(summary)
    if "GITHUB_STEP_SUMMARY" in os.environ:
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as output:
            output.write(f"### {platform}\n\n```json\n{summary}\n```\n")


def collect(source: Path, destination: Path, verify_attestations: bool) -> None:
    required = {f"muse-glimmer-{name}" for name in platforms()}
    if {path.name for path in source.iterdir()} != required:
        raise ValueError("Missing or unexpected platform artifacts")
    bundles = []
    for name in platforms():
        directory = source / f"muse-glimmer-{name}"
        manifest = load_bundle(directory, name)
        if verify_attestations:
            attest(directory, manifest)
        bundles.append((directory, manifest))
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("Collection output must be empty")
    wheels = []
    for directory, manifest in bundles:
        for entry in manifest["wheels"]:
            shutil.copyfile(directory / entry["filename"], destination / entry["filename"])
            wheels.append(entry)
    write_bundle(destination, wheels)
    load_bundle(destination)


def published_files(version: str) -> dict:
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/muse-glimmer/{version}/json", timeout=30) as response:
            return {entry["filename"]: entry for entry in json.load(response)["urls"]}
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {}
        raise


def match_published(entry: dict, published: dict) -> None:
    if published["digests"]["sha256"] != entry["sha256"] or published["size"] != entry["size"]:
        raise ValueError(f"PyPI already has different bytes for {entry['filename']}; use a new version")


def require_tag(manifest: dict) -> None:
    if manifest["ref"] != f"refs/tags/v{manifest['version']}":
        raise ValueError("Publishing requires the matching version tag")


def publish(directory: Path) -> None:
    manifest = load_bundle(directory)
    require_tag(manifest)
    attest(directory, manifest)
    published = published_files(manifest["version"])
    if set(published) - {w["filename"] for w in manifest["wheels"]}:
        raise ValueError("PyPI contains unexpected files for this version")
    # Validate every existing file before uploading anything on a partial retry.
    for entry in manifest["wheels"]:
        if entry["filename"] in published:
            match_published(entry, published[entry["filename"]])
    for entry in manifest["wheels"]:
        wheel = str(directory / entry["filename"])
        if entry["filename"] in published:
            print(f"Already published with matching checksum: {entry['filename']}")
            continue
        command(sys.executable, "-m", "pypi_attestations", "sign", wheel)
        command(
            sys.executable, "-m", "pypi_attestations", "verify", "attestation",
            "--identity", f"https://github.com/{os.environ['GITHUB_WORKFLOW_REF']}", wheel,
        )
        command("uv", "publish", "--trusted-publishing", "always", wheel, wheel + ".publish.attestation")


def download_published(source: Path, destination: Path, platform: str | None) -> None:
    manifest = load_bundle(source, platform)
    require_tag(manifest)
    for attempt in range(12):
        published = published_files(manifest["version"])
        if all(w["filename"] in published for w in manifest["wheels"]):
            break
        if attempt == 11:
            raise ValueError("Published files did not appear on PyPI")
        time.sleep(10)
    destination.mkdir(parents=True, exist_ok=True)
    for entry in manifest["wheels"]:
        remote = published[entry["filename"]]
        match_published(entry, remote)
        wheel = destination / entry["filename"]
        with urllib.request.urlopen(remote["url"], timeout=60) as response, wheel.open("wb") as output:
            shutil.copyfileobj(response, output)
        if digest(wheel) != entry["sha256"] or wheel.stat().st_size != entry["size"]:
            raise ValueError(f"Downloaded wheel differs from tested bytes: {wheel.name}")
        command(
            sys.executable, "-m", "pypi_attestations", "verify", "pypi",
            "--repository", f"https://github.com/{os.environ['GITHUB_REPOSITORY']}", str(wheel),
        )
    write_bundle(destination, manifest["wheels"])
    load_bundle(destination, platform)


def github_release(directory: Path) -> None:
    from packaging.version import Version

    manifest = load_bundle(directory)
    require_tag(manifest)
    attest(directory, manifest)
    write_bundle(directory, manifest["wheels"])
    repo, tag = os.environ["GITHUB_REPOSITORY"], manifest["ref"].removeprefix("refs/tags/")
    result = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo, "--json", "isDraft,assets"],
        capture_output=True, text=True,
    )
    if result.returncode and "release not found" not in result.stderr.lower():
        raise RuntimeError(result.stderr)
    existing = json.loads(result.stdout) if result.returncode == 0 else None
    filenames = [w["filename"] for w in manifest["wheels"]] + ["SHA256SUMS"]
    if existing and {a["name"] for a in existing["assets"]} - set(filenames):
        raise ValueError("GitHub Release contains unexpected assets")
    if existing and not existing["isDraft"]:
        with tempfile.TemporaryDirectory() as temporary:
            command("gh", "release", "download", tag, "--repo", repo, "--dir", temporary)
            for name in filenames:
                if digest(Path(temporary) / name) != digest(directory / name):
                    raise ValueError(f"Published GitHub asset differs: {name}")
        return
    rows = []
    for entry in manifest["wheels"]:
        name = entry["platform"]
        info = platforms()[name]
        url = f"https://github.com/{repo}/releases/download/{tag}/{entry['filename']}"
        rows.append(f"| `{name}` — {info['description']} | {info['requirements']} | [Prebuilt wheel]({url}) |")
    notes = (
        "Serve Muse Glimmer locally.\n\n```sh\npip install --pre --upgrade muse-glimmer\n"
        "muse-glimmer pull 1.0\nmuse-glimmer serve\n```\n\n### Prebuilt downloads\n\n"
        "| Platform | Requirements | Download |\n| --- | --- | --- |\n"
        + "\n".join(rows)
        + f"\n\nThese wheels are identical to [PyPI {manifest['version']}](https://pypi.org/project/muse-glimmer/{manifest['version']}/). "
        "They include the native runtime; no source build is needed. Model weights are downloaded separately.\n\n"
        f"[Build provenance](https://github.com/{repo}/attestations) · "
        f"[Release workflow](https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']})\n\n"
        "`SHA256SUMS` contains every wheel's checksum.\n\n"
        + command("gh", "api", "--method", "POST", f"repos/{repo}/releases/generate-notes", "-f", f"tag_name={tag}", "--jq", ".body")
    )
    with tempfile.TemporaryDirectory() as temporary:
        body = Path(temporary) / "notes.md"
        body.write_text(notes)
        flags = ["--prerelease"] if Version(manifest["version"]).is_prerelease else []
        if not existing:
            command("gh", "release", "create", tag, "--repo", repo, "--verify-tag", "--draft", *flags,
                    "--title", manifest["version"], "--notes-file", str(body))
        assets = [f"{directory / w['filename']}#{w['platform']} prebuilt wheel" for w in manifest["wheels"]]
        command("gh", "release", "upload", tag, "--repo", repo, *assets, str(directory / "SHA256SUMS"), "--clobber")
        command("gh", "release", "edit", tag, "--repo", repo, "--draft=false", *flags,
                "--title", manifest["version"], "--notes-file", str(body))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    p = sub.add_parser("record")
    p.add_argument("--platform", required=True)
    p.add_argument("directory", type=Path)
    p = sub.add_parser("collect")
    p.add_argument("source", type=Path)
    p.add_argument("destination", type=Path)
    p.add_argument("--attest", action="store_true")
    for name in ("publish", "github"):
        p = sub.add_parser(name)
        p.add_argument("directory", type=Path)
    p = sub.add_parser("download")
    p.add_argument("source", type=Path)
    p.add_argument("destination", type=Path)
    p.add_argument("--platform")
    args = parser.parse_args()
    if args.command == "plan":
        plan()
    elif args.command == "record":
        record(args.directory, args.platform)
    elif args.command == "collect":
        collect(args.source, args.destination, args.attest)
    elif args.command == "publish":
        publish(args.directory)
    elif args.command == "download":
        download_published(args.source, args.destination, args.platform)
    else:
        github_release(args.directory)


if __name__ == "__main__":
    main()
