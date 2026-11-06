from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from plugins.JianerAI.agent_runtime.models import SessionKey
from plugins.JianerAI.agent_runtime.sessions import (
    AgentSessionManager,
    _context_history_budget,
)
from plugins.JianerAI.agent_runtime.store import ContextStore
from plugins.JianerAI.providers import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    ProviderRegistry,
    _extract_context_window_tokens,
    _known_model_context_window,
)
from plugins.JianerAI.tools.registry import ToolRegistry


def _write_model(
    root: Path,
    *,
    key: str,
    model: str,
    provider: str = "OpenAI Chat Completions",
) -> None:
    (root / f"{key}.ai.json").write_text(
        json.dumps(
            {
                "FriendlyName": key,
                "Model": model,
                "ApiKey": "test-key",
                "ResponseType": provider,
            }
        ),
        encoding="utf-8",
    )


def test_context_window_metadata_extracts_common_provider_fields():
    assert _extract_context_window_tokens({"context_window": 65536}) == 65536
    assert (
        _extract_context_window_tokens(
            {"model": {"input_token_limit": "1048576"}}
        )
        == 1_048_576
    )
    assert (
        _extract_context_window_tokens({"context_length": 131072})
        == 131_072
    )
    assert _extract_context_window_tokens({"max_model_len": 32768}) == 32_768
    assert _extract_context_window_tokens({"max_output_tokens": 4096}) is None


def test_known_model_context_window_uses_model_family_fallback():
    assert _known_model_context_window("gpt-4.1-mini") == 1_047_576
    assert _known_model_context_window("deepseek-v4.1-flash") == 131_072
    assert _known_model_context_window("private-unknown-model") is None


def test_provider_registry_reads_and_caches_context_window(tmp_path: Path):
    _write_model(tmp_path, key="demo", model="demo-model")
    calls = 0

    def resolve(_config):
        nonlocal calls
        calls += 1
        return {"model": {"context_length": 65536}}

    registry = ProviderRegistry(
        tmp_path,
        context_window_resolver=resolve,
    )

    async def scenario():
        first = await registry.context_window_tokens("demo")
        second = await registry.context_window_tokens("demo")
        return first, second

    assert asyncio.run(scenario()) == (65536, 65536)
    assert calls == 1


def test_provider_registry_falls_back_without_model_config_fields(
    tmp_path: Path,
):
    _write_model(tmp_path, key="deepseek", model="deepseek-chat")
    registry = ProviderRegistry(
        tmp_path,
        context_window_resolver=lambda _config: None,
    )
    assert asyncio.run(registry.context_window_tokens("deepseek")) == 131_072


def test_provider_registry_uses_conservative_unknown_default(
    tmp_path: Path,
):
    _write_model(tmp_path, key="private", model="private-unknown-model")
    registry = ProviderRegistry(
        tmp_path,
        context_window_resolver=lambda _config: None,
    )
    assert (
        asyncio.run(registry.context_window_tokens("private"))
        == DEFAULT_CONTEXT_WINDOW_TOKENS
    )


def test_history_budget_leaves_room_for_output_and_request_overhead():
    assert (
        _context_history_budget(
            128_000,
            output_reserve=2_000,
            overhead_tokens=10_000,
        )
        == 92_800
    )
    assert (
        _context_history_budget(
            8_000,
            output_reserve=2_000,
            overhead_tokens=7_000,
        )
        == 2_048
    )


def test_agent_session_uses_model_window_for_auto_summary(tmp_path: Path):
    class Providers:
        @staticmethod
        def get(_model):
            return SimpleNamespace(max_tokens=1000)

        @staticmethod
        async def context_window_tokens(_model):
            return 8192

    key = SessionKey(
        scene_type="group",
        scene_id="group-1",
        protocol="milky",
        self_id="bot-1",
    )
    core = SimpleNamespace(
        history=[{"role": "user", "content": "old"}],
        context_overhead_tokens=2000,
        history_token_estimate=lambda: 5000,
    )

    async def scenario():
        manager = AgentSessionManager(
            store=ContextStore(tmp_path / "agent"),
            providers=Providers(),
            tools=ToolRegistry(),
            profiles=SimpleNamespace(active_name="default"),
            model_resolver=lambda _key: "demo",
            owner_ids=(),
        )
        try:
            return await manager.should_auto_summarize(key, core)
        finally:
            await manager.close()

    assert asyncio.run(scenario()) is True
