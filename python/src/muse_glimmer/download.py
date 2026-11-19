from __future__ import annotations

import os
import platform
import sys
import tempfile
from pathlib import Path, PurePosixPath

import httpx
import yaml
from huggingface_hub import CacheNotFound, HfApi, hf_hub_download, scan_cache_dir
from platformdirs import user_data_path
from pydantic import BaseModel, ConfigDict, TypeAdapter

from .resources import resource_path

# Hub 2 uses httpx2; Hub 1 uses httpx.
try:
    from httpx2 import HTTPError as HubHTTPError
except ImportError:
    HubHTTPError = httpx.HTTPError


class ModelSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: str
    revision: str
    directory: str
    dflash: bool = False


class PulledModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    dflash: bool = False


class ModelVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_variant: str
    variants: dict[str, dict[str, ModelSource]]


def _catalog() -> dict[str, ModelVersion]:
    try:
        contents = yaml.safe_load(resource_path("models.yaml").read_text())
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid model catalog: {error}") from error
    return TypeAdapter(dict[str, ModelVersion]).validate_python(contents)


def _model_source(version: str, variant: str | None = None) -> ModelSource:
    versions = _catalog()
    if version not in versions:
        raise ValueError(
            f"Unknown model version {version!r}. Available model versions: {', '.join(sorted(versions))}."
        )
    model = versions[version]
    if variant is None:
        variant = model.default_variant
    if variant not in model.variants:
        raise ValueError(
            f"Unknown variant {variant!r} for model version {version!r}. "
            f"Available variants: {', '.join(sorted(model.variants))}."
        )
    sources = model.variants[variant]
    target = f"{platform.system().lower()}-{platform.machine().lower()}"
    if target not in sources:
        raise ValueError(
            f"Model version {version!r} variant {variant!r} is not supported on this platform ({target}). "
            f"Supported platforms: {', '.join(sorted(sources))}."
        )
    return sources[target]


def default_model_file() -> Path:
    return user_data_path("muse-glimmer", appauthor=False) / "default-model"


def default_model() -> PulledModel | None:
    try:
        value = default_model_file().read_text().strip()
    except FileNotFoundError:
        return None
    if not value:
        return None
    if value.startswith("{"):
        return PulledModel.model_validate_json(value)
    # Existing installations stored only the model path.
    return PulledModel(path=Path(value))


def list_models(version: str | None = None) -> None:
    versions = _catalog()
    if version is not None:
        if version not in versions:
            raise ValueError(
                f"Unknown model version {version!r}. Available model versions: {', '.join(sorted(versions))}."
            )
        versions = {version: versions[version]}
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"

    def style(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    selected = default_model()
    print(style("SELECTED MODEL", "1;36"))
    print("==============")
    if selected is None:
        print("Artifact: none")
    else:
        path = selected.path.absolute()
        missing = " " + style("[missing]", "33") if not path.is_file() else ""
        print(f"Artifact: {style(path.name, '1')}{missing}")
        print(f"Location: {path}")
    print()
    target = f"{platform.system().lower()}-{platform.machine().lower()}"
    print(f"Available models for {target}:")
    available = False
    for version, model in sorted(versions.items()):
        variants = sorted(name for name, sources in model.variants.items() if target in sources)
        if variants:
            available = True
            print(f"  {version}")
            for name in variants:
                print(f"    {name}{' [default]' if name == model.default_variant else ''}")
    if not available:
        print("  No models available for this platform.")


def remove_default_model() -> None:
    selected = default_model()
    if selected is None:
        raise ValueError("No default downloaded model to remove.")
    # Resolve parent directories, but keep the snapshot entry rather than its blob.
    model = selected.path.parent.resolve() / selected.path.name
    snapshots = next(
        (p for p in model.parents if p.name == "snapshots" and p.parent.name.startswith("models--")),
        None,
    )
    if snapshots is None or model.suffix != ".pte":
        raise ValueError("Default model is not in a Hugging Face model cache; no files were removed.")
    if not snapshots.parent.exists():
        default_model_file().unlink()
        return
    try:
        cache = scan_cache_dir(snapshots.parent.parent)
    except CacheNotFound as error:
        raise ValueError("Model cache disappeared during removal; try again.") from error
    repo = next((r for r in cache.repos if r.repo_path == snapshots.parent), None)
    if repo is None:
        raise ValueError("Cannot inspect the default model's cache; no files were removed.")
    paths = {model, model.with_name("pos_embed.bin")}
    files = [f for revision in repo.revisions for f in revision.files if f.file_path in paths]
    for file in files:
        if file.blob_path != file.file_path and file.blob_path.parent != repo.repo_path / "blobs":
            raise ValueError("Model cache points outside its blob directory; no files were removed.")
    strategy = cache.delete_files(*files)
    strategy.execute()
    if any(p.exists() or p.is_symlink() for p in strategy.files | strategy.blobs):
        raise OSError("Could not remove all model cache files; default selection was preserved.")
    # Another pull may have selected a different model while the cache was scanned.
    if default_model() == selected:
        default_model_file().unlink(missing_ok=True)


def _export_files(model: str, files: dict[str, int | None]) -> list[str]:
    positions = str(PurePosixPath(model).with_name("pos_embed.bin"))
    return [model, positions] if positions in files else [model]


def _download_size(names: list[str], files: dict[str, int | None]) -> str:
    sizes = [files[name] for name in names]
    if any(size is None for size in sizes):
        return "size unknown"
    total = sum(sizes)
    for unit, scale in [("GB", 10**9), ("MB", 10**6), ("KB", 10**3)]:
        if total >= scale:
            return f"{total / scale:.1f} {unit}"
    return f"{total} bytes"


def _select_model(files: dict[str, int | None], directory: str) -> str:
    directory = str(PurePosixPath(directory))
    models = sorted(
        name
        for name in files
        if name.endswith(".pte") and str(PurePosixPath(name).parent) == directory
    )
    if not models:
        raise ValueError(f"No .pte files found in export directory {directory!r}.")
    if len(models) != 1:
        raise ValueError(
            f"Export directory {directory!r} contains multiple .pte files. "
            "Each variant must contain exactly one .pte file. "
            "For a local copy, use serve --model with the explicit .pte path."
        )
    return models[0]


def pull(version: str, variant: str | None = None, force: bool = False) -> Path:
    source = _model_source(version, variant)
    repo_id = source.repo_id
    try:
        info = HfApi().model_info(repo_id, revision=source.revision, files_metadata=True)
        files = {file.rfilename: file.size for file in info.siblings}
        filename = _select_model(files, source.directory)
        required = _export_files(filename, files)

        print(f"Downloading {source.directory} ({_download_size(required, files)})", flush=True)
        model = Path(hf_hub_download(repo_id, filename, revision=info.sha, force_download=force))
        for companion in required[1:]:
            hf_hub_download(repo_id, companion, revision=info.sha, force_download=force)
    except (httpx.HTTPError, HubHTTPError) as error:
        raise ValueError(f"Could not download Muse Glimmer {version}: {error}") from error
    except ValueError as error:
        # Hub wraps forced-download metadata failures in a generic ValueError.
        if not force or error.__cause__ is None:
            raise
        raise ValueError(f"Could not download Muse Glimmer {version}: {error.__cause__}") from error

    # Keep the snapshot path: resolving its symlink would lose neighboring files.
    destination = default_model_file()
    destination.parent.mkdir(parents=True, exist_ok=True)
    file = tempfile.NamedTemporaryFile(mode="w", dir=destination.parent, delete=False)
    temporary = Path(file.name)
    try:
        with file:
            file.write(
                PulledModel(path=model.absolute(), dflash=source.dflash).model_dump_json() + "\n"
            )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return model
