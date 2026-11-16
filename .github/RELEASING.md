# Releasing muse-glimmer

Releases run on a temporary GitHub Actions runner on your Mac. The workflow
builds the wheel, tests it on Python 3.11 and 3.13, publishes through PyPI Trusted
Publishing, and verifies the published file and provenance.

## Setup

Use an Apple Silicon Mac with Python 3.11+, Git, [GitHub CLI](https://cli.github.com/),
and [Xcode with Metal](../README.md#from-source). Your GitHub account needs
administration access to this repository to register temporary runners.

Authenticate once and enable workflow pushes:

```sh
gh auth login
gh auth refresh --hostname github.com --scopes workflow
gh auth setup-git
```

The [PyPI Trusted Publisher](https://pypi.org/manage/project/muse-glimmer/settings/publishing/)
is already configured for:

| Field | Value |
| --- | --- |
| Owner | `mergennachin` |
| Repository | `muse-glimmer` |
| Workflow filename | `wheel.yml` |
| Environment | `pypi` |

GitHub's `pypi` environment requires deployment approval. No PyPI API token is needed.

## Publish a release

Run these commands from the repository. Start on an up-to-date `main` branch
with the intended changes committed. Set `[project].version` in
[pyproject.toml](../pyproject.toml) to a new, unused version.

```sh
release_version=$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
git add pyproject.toml
git commit -m "Release ${release_version}"
git tag -a "v${release_version}" -m "muse-glimmer ${release_version}"
git push --atomic origin main "v${release_version}"
gh run list --workflow wheel.yml --branch "v${release_version}"
```

Use the run ID from the last command in place of `RUN_ID`:

```sh
python packaging/release_runner.py RUN_ID
```

Keep this command running. It downloads and verifies the GitHub runner, registers
it for this run, and prints the workflow URL. No machine-specific workflow edits
are needed.

When the build and installation checks pass, open that URL and choose
**Review deployments → pypi → Approve and deploy**. Review the commit, wheel name,
size, and SHA-256 in the build summary before approving.

The workflow uploads the tested wheel and its attestation, then downloads it
from PyPI to check the checksum, installation, and provenance. The package's
PyPI page uses the repository README, with links pinned to the release tag.

Wait for `Result: success`. The helper stops and unregisters the runner and
deletes its temporary checkout, tooling, and build cache. **Ctrl+C cancels the run.**

## Build without publishing

```sh
gh workflow run wheel.yml --ref main
gh run list --workflow wheel.yml --event workflow_dispatch --branch main
python packaging/release_runner.py RUN_ID
```

Replace `RUN_ID` with the new run's ID. A run on `main` builds and tests the wheel
without publishing it.

## Retry a failed run

Inspect the failure first:

```sh
gh run view RUN_ID --log-failed
```

For a transient failure with unchanged code:

```sh
gh run rerun RUN_ID --failed
python packaging/release_runner.py RUN_ID
```

If an upload may have succeeded, check PyPI before retrying. If the release
contents need to change, use a new version and tag; do not move a published tag
or replace an existing PyPI file.
