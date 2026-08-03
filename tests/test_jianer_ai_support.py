from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.JianerAI.presets import PresetError, PresetStore
from plugins.JianerAI.providers import (
    MediaAttachment,
    ProviderRegistry,
)
from plugins.JianerAI.speech import (
    SpeechOptions,
    SpeechSynthesizer,
    sanitize_for_speech,
)
from plugins.JianerAI.suffix import SuffixStore


def _write_config(config_dir: Path, name: str, **overrides) -> None:
    raw = {
        "FriendlyName": name.title(),
        "Model": f"{name}-model",
        "ResponseType": "openai",
        "ApiKey": "test-secret",
        "BaseUrl": "https://provider.invalid/v1",
        "Temperature": 0.25,
        "MaxTokens": 256,
        **overrides,
    }
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / f"{name}.ai.json").write_text(
        json.dumps(raw),
        encoding="utf-8",
    )


def test_openai_config_and_chat_use_injected_transport_without_network(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "aiconfig"
    _write_config(config_dir, "alpha", Personality="config personality")
    calls = []

    async def transport(provider, config, payload):
        calls.append((provider, config, payload))
        return {"choices": [{"message": {"content": "answer  "}}]}

    registry = ProviderRegistry(config_dir, transport=transport)
    result = asyncio.run(
        registry.chat(
            "alpha",
            "hello https://private.invalid/image.png",
            history=[
                {"role": "system", "content": "ignored history system"},
                {"role": "user", "content": "old"},
                {"role": "tool", "content": "ignored"},
            ],
            system_prompt="session personality",
        )
    )

    assert registry.list_models() == {"alpha": "Alpha"}
    assert result == "answer"
    provider, config, payload = calls[0]
    assert provider == "openai"
    assert config.api_key == "test-secret"
    assert payload["messages"] == [
        {"role": "system", "content": "session personality"},
        {"role": "user", "content": "old"},
        {
            "role": "user",
            "content": "hello https://private.invalid/image.png",
        },
    ]


def test_provider_accepts_only_resolved_media_bytes(tmp_path: Path) -> None:
    config_dir = tmp_path / "aiconfig"
    _write_config(config_dir, "vision")
    captured = {}

    def transport(provider, config, payload):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": "seen"}}]}

    registry = ProviderRegistry(config_dir, transport=transport)
    resolution = SimpleNamespace(
        status=SimpleNamespace(value="ok"),
        error_code=None,
        mime="image/png",
        size=8,
        data=b"\x89PNG\r\n\x1a\n",
        source="adapter:abc",
    )
    attachment = MediaAttachment.from_resolution(resolution)
    assert asyncio.run(
        registry.chat("vision", "look", attachments=[attachment])
    ) == "seen"
    content = captured["payload"]["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert "adapter:abc" not in content[1]["image_url"]["url"]

    failed = SimpleNamespace(
        status=SimpleNamespace(value="rejected"),
        error_code=SimpleNamespace(value="origin_not_allowed"),
        mime=None,
        size=0,
        data=None,
        source="https://private.invalid",
    )
    with pytest.raises(ValueError, match="origin_not_allowed"):
        MediaAttachment.from_resolution(failed)
    with pytest.raises(TypeError, match="MediaAttachment"):
        asyncio.run(
            registry.chat(
                "vision",
                "look",
                attachments=["https://x.invalid/a.png"],
            )
        )


def test_native_gemini_payload_embeds_resolved_bytes(tmp_path: Path) -> None:
    config_dir = tmp_path / "aiconfig"
    _write_config(
        config_dir,
        "gemini",
        ResponseType="gemini",
        APIKey="test-secret",
        ApiKey=None,
        BaseURL="https://generativelanguage.googleapis.com",
        BaseUrl=None,
    )
    calls = []

    async def transport(provider, config, payload):
        calls.append((provider, payload))
        return {
            "candidates": [
                {"content": {"parts": [{"text": "line one"}, {"text": "line two"}]}}
            ]
        }

    registry = ProviderRegistry(config_dir, transport=transport)
    audio = MediaAttachment(data=b"ID3audio", mime="audio/mpeg")
    result = asyncio.run(
        registry.chat(
            "gemini",
            "listen",
            history=[{"role": "assistant", "content": "before"}],
            attachments=[audio],
        )
    )

    assert result == "line one\nline two"
    provider, payload = calls[0]
    assert provider == "gemini"
    assert payload["contents"][0]["role"] == "model"
    inline = payload["contents"][-1]["parts"][1]["inlineData"]
    assert inline["mimeType"] == "audio/mpeg"
    assert inline["data"] == "SUQzYXVkaW8="


def test_registry_reports_bad_config_without_exposing_secret(tmp_path: Path) -> None:
    config_dir = tmp_path / "aiconfig"
    _write_config(config_dir, "good")
    (config_dir / "bad.ai.json").write_text(
        '{"ApiKey":"do-not-report","Model":',
        encoding="utf-8",
    )

    registry = ProviderRegistry(config_dir, transport=lambda *_: None)

    assert registry.list_models() == {"good": "Good"}
    assert "bad.ai.json" in registry.load_errors
    assert "do-not-report" not in registry.load_errors["bad.ai.json"]


def test_existing_presets_load_default_and_render_legacy_placeholders() -> None:
    project_root = Path(__file__).resolve().parents[1]
    store = PresetStore(
        project_root / "prerequisites" / "current.json",
        project_root / "prerequisites",
    )

    assert set(store.list_choices()) == {
        "XingYu",
        "Normal",
        "p7993817",
        "p9930414",
        "p9360874",
        "p9428138",
    }
    default = store.get_default()
    assert default.key == "XingYu"
    assert default.name == "机娘"
    assert default.info == "是机娘desu~！"
    assert "机娘" in default.template
    rendered = store.render(
        "做我女朋友",
        bot_name="简儿",
        bot_name_en="Jianer",
        event_user="小明",
        event_user_id="user-1",
    )
    assert "简儿" in rendered
    assert "小明" in rendered
    assert "{self." not in rendered


def test_preset_store_atomic_upsert_assignment_and_delete(tmp_path: Path) -> None:
    preset_dir = tmp_path / "prerequisites"
    preset_dir.mkdir()
    (preset_dir / "Normal.txt").write_text("default", encoding="utf-8")
    (preset_dir / "current.json").write_text(
        json.dumps(
            {
                "Normal": {
                    "name": "Default",
                    "uid": [],
                    "info": "",
                    "path": "Normal.txt",
                }
            }
        ),
        encoding="utf-8",
    )
    store = PresetStore(preset_dir / "current.json", default_key="Normal")

    store.upsert(
        key="p1234567",
        name="Helper",
        info="Useful",
        template=(
            "Hello {self.event_user_id}; tools={agent_tools}; "
            "info={agent_tools_info}"
        ),
        legacy_user_ids=("42",),
    )
    assert store.find_legacy_assignment(42).key == "p1234567"
    assert store.find_legacy_assignment("qq:42").key == "p1234567"
    assert store.render(
        "Helper",
        bot_name="b",
        bot_name_en="b",
        event_user="u",
        event_user_id=42,
        agent_tools="alpha, beta",
        agent_tools_info="- alpha: first\n- beta: second",
    ) == (
        "Hello 42; tools=alpha, beta; "
        "info=- alpha: first\n- beta: second"
    )
    assert store.delete("p1234567") is True
    assert not (preset_dir / "p1234567.txt").exists()
    with pytest.raises(PresetError, match="default"):
        store.delete("Normal")


def test_preset_store_rejects_template_path_traversal(tmp_path: Path) -> None:
    preset_dir = tmp_path / "presets"
    preset_dir.mkdir()
    (preset_dir / "current.json").write_text(
        json.dumps(
            {
                "Normal": {
                    "name": "Default",
                    "path": "../outside.txt",
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PresetError, match="plain filename"):
        PresetStore(preset_dir / "current.json")


def test_suffix_is_persisted_and_applied_only_by_explicit_ai_method(
    tmp_path: Path,
) -> None:
    path = tmp_path / "suffix_config.json"
    store = SuffixStore(path)
    store.set_global("喵")
    store.set_for_identity("alice", "呀")

    assert store.apply_ai_reply("你好。再见", "alice") == "你好呀。再见呀"
    assert store.apply_ai_reply("已经呀。", "alice") == "已经呀。"
    assert store.apply_ai_reply("你好", "bob") == "你好喵"
    assert SuffixStore(path).get("alice") == "呀"
    assert not hasattr(store, "process_text")


def test_suffix_store_preserves_legacy_bare_qq_identity_keys(
    tmp_path: Path,
) -> None:
    path = tmp_path / "suffix_config.json"
    path.write_text(
        json.dumps(
            {
                "global_suffix": "全局",
                "user_suffixes": {
                    "12345": "旧后缀",
                    "qq:67890": "规范后缀",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = SuffixStore(path)

    assert store.get("qq:12345") == "旧后缀"
    assert store.get("12345") == "旧后缀"
    store.set_for_identity("qq:12345", "新后缀")
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["user_suffixes"]["qq:12345"] == "新后缀"
    assert "12345" not in persisted["user_suffixes"]
    assert store.clear_for_identity("12345") is True
    assert store.get("qq:12345") == "全局"


def test_speech_uses_instance_temp_dir_and_shutdown_cleans_it(
    tmp_path: Path,
) -> None:
    calls = []

    async def backend(text: str, output_path: Path, options: SpeechOptions):
        calls.append((text, output_path, options))
        output_path.write_bytes(b"fake-mp3")

    first = SpeechSynthesizer(temp_parent=tmp_path, backend=backend)
    second = SpeechSynthesizer(temp_parent=tmp_path, backend=backend)
    assert first.temp_dir != second.temp_dir

    artifact = asyncio.run(
        first.synthesize_artifact(
            "**你好** 😊",
            {"voiceColor": "test-voice"},
        )
    )
    assert artifact is not None
    assert artifact.mime == "audio/mpeg"
    assert artifact.path.parent == first.temp_dir
    assert calls[0][0] == "你好"
    assert calls[0][2].voice == "test-voice"
    first_dir = first.temp_dir
    asyncio.run(first.shutdown())
    asyncio.run(first.shutdown())
    assert not first_dir.exists()
    assert second.temp_dir.exists()
    asyncio.run(second.shutdown())


def test_speech_shutdown_waits_for_inflight_work_and_is_single_flight(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def backend(text: str, output_path: Path, options: SpeechOptions):
            started.set()
            await release.wait()
            output_path.write_bytes(b"audio")

        speech = SpeechSynthesizer(temp_parent=tmp_path, backend=backend)
        synthesize_task = asyncio.create_task(speech.synthesize("hello"))
        await started.wait()
        first_shutdown = asyncio.create_task(speech.shutdown())
        second_shutdown = asyncio.create_task(speech.shutdown())
        await asyncio.sleep(0)
        assert not first_shutdown.done()
        assert not second_shutdown.done()
        release.set()
        output_path = await synthesize_task
        assert output_path is not None
        assert output_path.suffix == ".mp3"
        await asyncio.gather(first_shutdown, second_shutdown)
        assert not speech.temp_dir.exists()

    asyncio.run(scenario())


def test_speech_sanitizer_keeps_math_but_removes_markdown_and_emoji() -> None:
    assert sanitize_for_speech("# 结果 **x² + y²** 😊") == "结果 x² + y²"
