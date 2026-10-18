from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from plugins.JianerAI.providers import (
    ChatRequest,
    FunctionTool,
    ModelConfig,
    _build_gemini_payload,
    _gemini_parameters_schema,
)
from plugins.JianerAI.tools.onebot_api_catalog import ONEBOT_API_CATALOG


def _config() -> ModelConfig:
    return ModelConfig(
        key="peachgemini",
        friendly_name="peachgemini",
        provider="Google GenerateContent",
        model="gemini-3.8-flash",
        api_key="sk-test",
    )


def _sibling_nodes(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if ("anyOf" in node or "oneOf" in node) and len(node) > 1:
            found.append(node)
        for value in node.values():
            found.extend(_sibling_nodes(value))
    elif isinstance(node, (list, tuple)):
        for value in node:
            found.extend(_sibling_nodes(value))
    return found


def test_anyof_siblings_are_folded_into_branches() -> None:
    schema = {
        "type": "object",
        "properties": {
            "user_id": {"type": "integer"},
            "group_id": {"type": "integer"},
        },
        "required": [],
        "additionalProperties": False,
        "anyOf": [
            {
                "type": "object",
                "properties": {"user_id": {"type": "integer"}},
                "required": ["user_id"],
            },
            {
                "type": "object",
                "properties": {"group_id": {"type": "integer"}},
                "required": ["group_id"],
            },
        ],
    }
    normalized = _gemini_parameters_schema(schema)
    assert set(normalized) == {"anyOf"}
    first = normalized["anyOf"][0]
    assert first["type"] == "object"
    assert set(first["properties"]) == {"user_id", "group_id"}
    assert first["required"] == ["user_id"]
    assert first["additionalProperties"] is False


def test_nested_anyof_keeps_description_in_branches() -> None:
    schema = {
        "type": "object",
        "properties": {
            "no_cache": {
                "anyOf": [{"type": "boolean"}, {"type": "null"}],
                "description": "是否禁用缓存。",
            }
        },
    }
    normalized = _gemini_parameters_schema(schema)
    node = normalized["properties"]["no_cache"]
    assert set(node) == {"anyOf"}
    assert all(branch["description"] == "是否禁用缓存。" for branch in node["anyOf"])


def test_plain_schema_is_unchanged() -> None:
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    assert _gemini_parameters_schema(schema) == schema


def test_gemini_payload_uses_normalized_tool_schema() -> None:
    schema = {
        "type": "object",
        "properties": {"user_id": {"type": "integer"}},
        "anyOf": [
            {"type": "object", "required": ["user_id"]},
            {"type": "object", "required": []},
        ],
    }
    payload = _build_gemini_payload(
        _config(),
        ChatRequest(
            message="hello",
            tools=(
                FunctionTool(
                    name="onebot_send_msg",
                    description="send",
                    parameters=schema,
                ),
            ),
        ),
    )
    declaration = payload["tools"][0]["functionDeclarations"][0]
    assert set(declaration["parametersJsonSchema"]) == {"anyOf"}


def test_onebot_catalog_schemas_have_no_anyof_siblings() -> None:
    for entry in ONEBOT_API_CATALOG:
        normalized = _gemini_parameters_schema(entry["input_schema"])
        assert _sibling_nodes(normalized) == [], entry["name"]


def test_milky_catalog_schemas_have_no_anyof_siblings() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "milky_api_catalog.json"
    )
    catalog = json.loads(path.read_text(encoding="utf-8"))
    for api in catalog.get("apis", []):
        schema = api.get("input_schema")
        if not isinstance(schema, dict):
            continue
        normalized = _gemini_parameters_schema(schema)
        assert _sibling_nodes(normalized) == [], api.get("name")
