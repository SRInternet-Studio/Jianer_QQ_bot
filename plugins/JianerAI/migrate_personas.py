"""Explicit migration helper for JianerAI structured persona JSON files.

Usage:
    python -m plugins.JianerAI.migrate_personas --model gemini
    python -m plugins.JianerAI.migrate_personas --model gemini --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from plugins.JianerAI.persona import PersonaGenerator
from plugins.JianerAI.presets import PresetStore
from plugins.JianerAI.providers import ProviderRegistry


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prerequisites-dir",
        type=Path,
        default=Path("prerequisites"),
        help="directory containing current.json and persona templates",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("aiconfig"),
        help="directory containing *.ai.json model configs",
    )
    parser.add_argument(
        "--model",
        default="",
        help="model key used to generate profiles",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write persona JSON files; without this flag only preview",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="regenerate even when a matching persona JSON already exists",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help="backup directory; defaults to a timestamped directory",
    )
    return parser


def _select_model(
    providers: ProviderRegistry,
    requested: str,
    config_path: Path,
) -> str:
    candidates = [requested]
    if config_path.is_file():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            others = raw.get("others", {}) if isinstance(raw, dict) else {}
            candidates.extend(
                [
                    str(others.get("persona_generation_model") or ""),
                    str(others.get("default_mode") or ""),
                ]
            )
        except (OSError, json.JSONDecodeError):
            pass
    available = providers.list_models()
    for candidate in candidates:
        key = str(candidate or "").strip()
        if key and key in available:
            return key
    if available:
        return next(iter(available))
    raise RuntimeError("没有可用的 aiconfig 模型")


def _backup(store: PresetStore, root: Path, requested: Path | None) -> Path:
    target = requested or root / (
        ".persona-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    target.mkdir(parents=True, exist_ok=True)
    if store.config_path.is_file():
        shutil.copy2(store.config_path, target / store.config_path.name)
    for preset in store.list_presets():
        source = store.template_path(preset.key)
        if source.is_file():
            shutil.copy2(source, target / source.name)
    if store.persona_dir.is_dir():
        shutil.copytree(
            store.persona_dir,
            target / "personas",
            dirs_exist_ok=True,
        )
    return target


async def _migrate(args: argparse.Namespace) -> int:
    root = args.prerequisites_dir.resolve()
    store = PresetStore(root / "current.json", root)
    providers = ProviderRegistry(args.config_dir.resolve())
    model = _select_model(
        providers,
        args.model,
        root.parent / "config.json",
    )
    generator = PersonaGenerator(providers)
    presets = store.list_presets()
    if not args.apply:
        print(f"dry-run: model={model}, presets={len(presets)}")
        for preset in presets:
            print(
                f"- {preset.key}: {preset.persona_status}, "
                f"source={len(preset.template)} chars"
            )
        return 0

    backup = _backup(store, root, args.backup_dir)
    print(f"backup: {backup}")
    created = 0
    skipped = 0
    failed = 0
    for preset in presets:
        if not args.force and preset.persona_status == "structured":
            skipped += 1
            print(f"skip {preset.key}: already structured")
            continue
        print(f"generate {preset.key} ({preset.name})...", flush=True)
        try:
            profile = await generator.generate(
                model_key=model,
                source_text=preset.template,
                source_name=preset.name,
                info=preset.info,
            )
            store.write_persona(preset.key, profile)
            created += 1
            print(
                f"  ok: source={len(preset.template)} chars, "
                f"structured={len(profile.to_json())} chars, "
                f"extra_settings={len(profile.extra_settings)}"
            )
        except Exception as exc:
            failed += 1
            print(
                f"  failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
    print(
        f"done: generated={created}, skipped={skipped}, failed={failed}, "
        f"backup={backup}"
    )
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_migrate(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
