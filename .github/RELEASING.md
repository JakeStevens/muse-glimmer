# Releasing muse-glimmer

Releases run on a temporary GitHub Actions runner on your Mac. The workflow
builds one wheel, runs the full pytest suite and installation checks on Python
3.11 and 3.13, and runs a live model smoke test on Python 3.13. After approval,
it publishes to PyPI, verifies the published bytes and provenance, and creates
a GitHub Release with the same wheel, `SHA256SUMS`, and release notes.

## Setup

Use an Apple Silicon Mac with Python 3.11+, Git, [GitHub CLI](https://cli.github.com/),
and [Xcode with Metal](../README.md#quickstart) (see "Install from source"). Your GitHub account needs
administration access to this repository to register temporary runners.

The smoke test pulls model `1.0` with its recommended variant (currently DFlash).
Allow roughly 20 GB for cached weights and enough free memory to serve the model.
Downloads reuse the runner user's Hugging Face cache across releases. Each test
uses an isolated home directory and a dedicated localhost server on a free port;
it does not change your selected model or use an existing server.

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
[pyproject.toml](../pyproject.toml) and `__version__` in
[python/src/muse_glimmer/__init__.py](../python/src/muse_glimmer/__init__.py)
to the same new, unused version. The wheel checks verify that the CLI agrees
with the installed package metadata.

```sh
release_version=$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
git add pyproject.toml python/src/muse_glimmer/__init__.py
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

When all tests pass, open that URL and choose
**Review deployments → pypi → Approve and deploy**. Review the commit, wheel name,
size, and SHA-256 in the build summary before approving.

The workflow uploads the tested wheel and its attestation, then downloads it
from PyPI to check the checksum, installation, and provenance. That downloaded
wheel becomes the GitHub Release asset; it is never rebuilt for GitHub. `.devN`
and other prerelease versions are marked as prereleases. The package's PyPI
page uses the repository README, with links pinned to the release tag.

Actions retains pytest reports and the live smoke JSON/server log in the
`test-results-*` artifacts, including on failure. The smoke test exercises tools,
parallel child sessions, warm/edited prefixes, streaming/disconnect recovery,
and concurrent multi-turn isolation. Reversed tool-result order remains an
explicit skip pending [ExecuTorch #25299](https://github.com/pytorch/executorch/issues/25299).

Wait for `Result: success`. The helper stops and unregisters the runner and
deletes its temporary checkout, tooling, and build cache. **Ctrl+C cancels the run.**

## Build without publishing

```sh
gh workflow run wheel.yml --ref main
gh run list --workflow wheel.yml --event workflow_dispatch --branch main
python packaging/release_runner.py RUN_ID
```

Replace `RUN_ID` with the new run's ID. A run on `main` runs the same build,
full test suite, and live smoke gates without publishing to either registry.

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

GitHub uploads remain a draft until both assets are attached. Retrying completes
a draft or verifies an already published release's assets without replacing them.
