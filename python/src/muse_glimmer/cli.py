from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import Settings, load_settings


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="muse-glimmer", description="Serve Muse Glimmer locally")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    command = commands.add_parser("pull", help="Download or select a model", allow_abbrev=False)
    command.add_argument(
        "model_version", nargs="?", help="Model version from --list (e.g. 1.0); optional filter for --list"
    )
    command.add_argument(
        "--variant",
        help="Checkpoint folder name; defaults to the model version's preferred variant",
    )
    actions = command.add_mutually_exclusive_group()
    actions.add_argument(
        "--force", action="store_true", help="Re-download the model even if it is cached"
    )
    actions.add_argument(
        "--list", action="store_true",
        help="Show available model versions, variants, and the selected artifact's location",
    )
    actions.add_argument(
        "--remove", action="store_true",
        help="Delete the specified download, or the selected model when no version is given",
    )
    command = commands.add_parser("serve", help="Start the local server")
    command.add_argument("--config", type=Path)
    command.add_argument("--model", help="Local .pte file or directory containing one .pte")
    command.add_argument(
        "--launch",
        choices=["muse", "pi", "omp", "hermes", "opencode", "open-webui"],
        help="Launch a client using the local server",
    )
    command.add_argument("--host")
    command.add_argument("--port", type=int)
    command.add_argument("--context", type=int)
    command.add_argument("--concurrency", type=int)
    command.add_argument("--prefix-cache-entries", type=int)
    command.add_argument(
        "--prefix-checkpoint-prompt-end",
        choices=["initial", "every"],
        help="Cache the first prompt per session or every turn (requires prefix caching)",
    )
    command.add_argument("--dflash", action=argparse.BooleanOptionalAction, default=None)
    command.add_argument("--dflash-n-draft", type=int)
    return root


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args = root.parse_args(argv)
    if args.command == "pull":
        if args.list and args.variant is not None:
            root.error("--variant cannot be used with --list")
        if args.remove and args.variant is not None and args.model_version is None:
            root.error("--variant requires a model version")
        if not (args.list or args.remove) and args.model_version is None:
            root.error("a model version is required; use pull --list to see available models")
    try:
        if args.command == "pull":
            from .download import list_models, pull, remove_model

            if args.list:
                list_models(args.model_version)
                return 0
            if args.remove:
                model = remove_model(args.model_version, args.variant)
                if model is not None:
                    print(f"Removed artifact: {model.name}\nLocation: {model.absolute()}")
                return 0
            model = pull(args.model_version, args.variant, force=args.force)
            print(f"Selected artifact: {model.name}\nLocation: {model.absolute()}")
            print("Run muse-glimmer serve to start serving.")
            return 0

        values = {key: value for key, value in vars(args).items() if key in Settings.model_fields}
        settings = load_settings(values, args.config)
        if settings.model:
            model = Path(settings.model)
        else:
            from .download import default_model

            pulled = default_model()
            model = pulled.path if pulled else None
            if pulled:
                settings = load_settings(
                    values, args.config, model_defaults={"dflash": pulled.dflash}
                )
        if model is None:
            raise ValueError("Run muse-glimmer pull 1.0 or pass --model /path/to/model.pte.")
        from .runtime import resolve_model, serve

        model = resolve_model(model)
        print(f"Model: {model.absolute()}", file=sys.stderr, flush=True)
        print(
            f"Config: {settings.model_dump_json(exclude={'model'}, indent=2)}",
            file=sys.stderr,
            flush=True,
        )
        if args.launch in {"muse", "pi", "omp", "hermes", "opencode"}:
            from .launch import launch_agent

            return launch_agent(model, settings, args.launch)
        if args.launch == "open-webui":
            from .webui import launch_open_webui

            return launch_open_webui(model, settings)
        serve(model, settings)
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, ImportError) as error:
        print(f"muse-glimmer: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
