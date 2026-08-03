from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jianer.adapters import ConversationKey, ConversationKind

from plugins.JianerAI.agent import AgentRunner
from plugins.JianerAI.providers import AssistantTurn, ProviderResponse
from plugins.JianerAI.service import _AGENT_SYSTEM_RULES
from plugins.JianerAI.tools import ToolContext, ToolRegistry, ToolRisk
from plugins.JianerAI.tools.contracts import ToolExecutionError
from plugins.JianerAI.tools.qweather import (
    QWeatherClient,
    QWeatherConfig,
    QWeatherConfigError,
    qweather_tools,
    register_qweather_tools,
)


TODAY = date(2026, 8, 3)

EXPECTED_PATHS = {
    "qweather_geo.city_lookup": "/geo/v2/city/lookup",
    "qweather_geo.top_city": "/geo/v2/city/top",
    "qweather_geo.poi_lookup": "/geo/v2/poi/lookup",
    "qweather_geo.poi_range": "/geo/v2/poi/range",
    "qweather_weather.current": "/weather/v1/current/{latitude}/{longitude}",
    "qweather_weather.daily": "/weather/v1/daily/{latitude}/{longitude}",
    "qweather_weather.hourly": "/weather/v1/hourly/{latitude}/{longitude}",
    "qweather_minutely.precipitation": "/v7/minutely/5m",
    "qweather_warning.current": "/weatheralert/v1/current/{latitude}/{longitude}",
    "qweather_indices.forecast": "/v7/indices/{days}",
    "qweather_air_quality.current": "/airquality/v1/current/{latitude}/{longitude}",
    "qweather_air_quality.hourly": "/airquality/v1/hourly/{latitude}/{longitude}",
    "qweather_air_quality.daily": "/airquality/v1/daily/{latitude}/{longitude}",
    "qweather_air_quality.station": "/airquality/v1/stations/{location_id}",
    "qweather_time_machine.weather": "/v7/historical/weather",
    "qweather_tropical_cyclone.list": "/v7/tropical/storm-list",
    "qweather_tropical_cyclone.track": "/v7/tropical/storm-track",
    "qweather_tropical_cyclone.forecast": "/v7/tropical/storm-forecast",
    "qweather_ocean.tide": "/v7/ocean/tide",
    "qweather_solar_radiation.forecast": "/solarradiation/v1/forecast/{latitude}/{longitude}",
    "qweather_astronomy.sun": "/v7/astronomy/sun",
    "qweather_astronomy.moon": "/v7/astronomy/moon",
    "qweather_astronomy.solar_elevation": "/v7/astronomy/solar-elevation-angle",
}


class RecordingClient:
    def __init__(self, payload=None):
        self.payload = payload or {"code": "200"}
        self.calls = []
        self.close_calls = 0

    async def request(self, endpoint_name, *, path_values=None, params=None):
        self.calls.append((endpoint_name, dict(path_values or {}), dict(params or {})))
        return self.payload

    async def close(self):
        self.close_calls += 1


def _spec(client, name):
    return {item.name: item for item in qweather_tools(client, today=lambda: TODAY)}[name]


def _call(client, tool_name, arguments):
    return asyncio.run(_spec(client, tool_name).handler(None, arguments))


def _private_key_pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _config(key: Ed25519PrivateKey | None = None) -> QWeatherConfig:
    return QWeatherConfig(
        api_host="abc123.qweatherapi.com",
        project_id="project-123",
        credential_id="credential-456",
        private_key=key or Ed25519PrivateKey.generate(),
    )


@pytest.mark.parametrize(
    ("tool_name", "arguments", "endpoint", "path_values", "params"),
    [
        (
            "qweather_geo",
            {"operation": "city_lookup", "location": "北京", "administrative_area": "北京", "country_code": "cn", "number": 3, "language": "zh"},
            "qweather_geo.city_lookup",
            {},
            {"location": "北京", "adm": "北京", "range": "cn", "number": 3, "lang": "zh"},
        ),
        (
            "qweather_geo",
            {"operation": "top_city", "country_code": "US", "number": 5},
            "qweather_geo.top_city",
            {},
            {"range": "us", "number": 5},
        ),
        (
            "qweather_geo",
            {"operation": "poi_lookup", "location": "故宫", "poi_type": "scenic", "city": "北京"},
            "qweather_geo.poi_lookup",
            {},
            {"location": "故宫", "type": "scenic", "city": "北京"},
        ),
        (
            "qweather_geo",
            {"operation": "poi_range", "latitude": 39.9, "longitude": 116.4, "poi_type": "TSTA", "radius_km": 8},
            "qweather_geo.poi_range",
            {},
            {"location": "116.4,39.9", "type": "TSTA", "radius": 8},
        ),
        (
            "qweather_weather",
            {"operation": "current", "latitude": 39.92, "longitude": 116.41, "local_time": True, "language": "en"},
            "qweather_weather.current",
            {"latitude": "39.92", "longitude": "116.41"},
            {"localTime": "true", "lang": "en"},
        ),
        (
            "qweather_weather",
            {"operation": "daily", "latitude": 39.92, "longitude": 116.41, "days": 10},
            "qweather_weather.daily",
            {"latitude": "39.92", "longitude": "116.41"},
            {"days": 10},
        ),
        (
            "qweather_weather",
            {"operation": "hourly", "latitude": 39.92, "longitude": 116.41, "hours": 240},
            "qweather_weather.hourly",
            {"latitude": "39.92", "longitude": "116.41"},
            {"hours": 240},
        ),
        (
            "qweather_minutely",
            {"operation": "precipitation", "latitude": 39.92, "longitude": 116.41},
            "qweather_minutely.precipitation",
            {},
            {"location": "116.41,39.92"},
        ),
        (
            "qweather_warning",
            {"operation": "current", "latitude": 39.92, "longitude": 116.41, "local_time": False},
            "qweather_warning.current",
            {"latitude": "39.92", "longitude": "116.41"},
            {"localTime": "false"},
        ),
        (
            "qweather_indices",
            {"operation": "forecast", "location_id": "101010100", "days": "3d", "index_types": [1, 2, 3]},
            "qweather_indices.forecast",
            {"days": "3d"},
            {"location": "101010100", "type": "1,2,3"},
        ),
        (
            "qweather_air_quality",
            {"operation": "current", "latitude": 39.92, "longitude": 116.41},
            "qweather_air_quality.current",
            {"latitude": "39.92", "longitude": "116.41"},
            {},
        ),
        (
            "qweather_air_quality",
            {"operation": "hourly", "latitude": 39.92, "longitude": 116.41, "language": "zh"},
            "qweather_air_quality.hourly",
            {"latitude": "39.92", "longitude": "116.41"},
            {"lang": "zh"},
        ),
        (
            "qweather_air_quality",
            {"operation": "daily", "latitude": 39.92, "longitude": 116.41, "local_time": True},
            "qweather_air_quality.daily",
            {"latitude": "39.92", "longitude": "116.41"},
            {"localTime": "true"},
        ),
        (
            "qweather_air_quality",
            {"operation": "station", "location_id": "P58911"},
            "qweather_air_quality.station",
            {"location_id": "P58911"},
            {},
        ),
        (
            "qweather_time_machine",
            {"operation": "weather", "location_id": "101010100", "date": "20260730", "unit": "imperial"},
            "qweather_time_machine.weather",
            {},
            {"location": "101010100", "date": "20260730", "unit": "i"},
        ),
        (
            "qweather_tropical_cyclone",
            {"operation": "list", "basin": "NP", "year": 2026},
            "qweather_tropical_cyclone.list",
            {},
            {"basin": "NP", "year": 2026},
        ),
        (
            "qweather_tropical_cyclone",
            {"operation": "track", "storm_id": "NP_2421"},
            "qweather_tropical_cyclone.track",
            {},
            {"stormid": "NP_2421"},
        ),
        (
            "qweather_tropical_cyclone",
            {"operation": "forecast", "storm_id": "NP_2421"},
            "qweather_tropical_cyclone.forecast",
            {},
            {"stormid": "NP_2421"},
        ),
        (
            "qweather_ocean",
            {"operation": "tide", "location_id": "P2951", "date": "20260810"},
            "qweather_ocean.tide",
            {},
            {"location": "P2951", "date": "20260810"},
        ),
        (
            "qweather_solar_radiation",
            {"operation": "forecast", "latitude": 39.92, "longitude": 116.41, "hours": 60, "interval_minutes": 15, "tilt": 30, "azimuth": 180, "extra": ["weather", "poa"], "local_time": True},
            "qweather_solar_radiation.forecast",
            {"latitude": "39.92", "longitude": "116.41"},
            {"hours": 60, "interval": 15, "tilt": 30, "azimuth": 180, "extra": "weather,poa", "localTime": "true"},
        ),
        (
            "qweather_astronomy",
            {"operation": "sun", "location_id": "101010100", "date": "20260803"},
            "qweather_astronomy.sun",
            {},
            {"location": "101010100", "date": "20260803"},
        ),
        (
            "qweather_astronomy",
            {"operation": "moon", "location_id": "101010100", "date": "20260804", "language": "zh"},
            "qweather_astronomy.moon",
            {},
            {"location": "101010100", "date": "20260804", "lang": "zh"},
        ),
        (
            "qweather_astronomy",
            {"operation": "solar_elevation", "latitude": 39.92, "longitude": 116.41, "date": "20260803", "time": "1230", "timezone": "+0800", "altitude_m": 43.5},
            "qweather_astronomy.solar_elevation",
            {},
            {"location": "116.41,39.92", "date": "20260803", "time": "1230", "tz": "0800", "alt": "43.5"},
        ),
    ],
)
def test_all_23_operations_map_to_fixed_paths_and_queries(
    tool_name, arguments, endpoint, path_values, params
):
    async def scenario():
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"code": "200"}, request=request)

        client = QWeatherClient(
            _config(),
            http_client=httpx.AsyncClient(
                base_url="https://abc123.qweatherapi.com",
                transport=httpx.MockTransport(respond),
                follow_redirects=False,
            ),
        )
        spec = _spec(client, tool_name)
        result = await spec.handler(None, arguments)
        await client.close()
        return result, requests

    result, requests = asyncio.run(scenario())
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == EXPECTED_PATHS[endpoint].format(**path_values)
    assert dict(request.url.params) == {key: str(value) for key, value in params.items()}
    assert request.headers["Authorization"].startswith("Bearer ")
    assert result["operation"] == arguments["operation"]
    assert result["provider"]["must_display"] is True
    assert result["provider"]["url"] == "https://www.qweather.com"
    serialized = json.dumps(result, ensure_ascii=False)
    assert "project-123" not in serialized
    assert "credential-456" not in serialized
    assert "Bearer " not in serialized


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("qweather_weather", {"operation": "current", "latitude": 1.234, "longitude": 2}),
        ("qweather_weather", {"operation": "current", "latitude": 1, "longitude": 2, "hours": 3}),
        ("qweather_weather", {"operation": "current", "latitude": 1, "longitude": 2, "url": "https://example.com"}),
        ("qweather_weather", {"operation": "daily", "latitude": 1, "longitude": 2, "days": 11}),
        ("qweather_indices", {"operation": "forecast", "location_id": "1", "days": "1d", "index_types": [0, 1]}),
        ("qweather_time_machine", {"operation": "weather", "location_id": "1", "date": "20260803"}),
        ("qweather_tropical_cyclone", {"operation": "list", "basin": "NP", "year": 2024}),
        ("qweather_ocean", {"operation": "tide", "location_id": "P1", "date": "20260813"}),
        ("qweather_solar_radiation", {"operation": "forecast", "latitude": 1, "longitude": 2, "extra": ["poa"]}),
        ("qweather_astronomy", {"operation": "sun", "location_id": "1", "date": "20261002"}),
        ("qweather_astronomy", {"operation": "solar_elevation", "latitude": 1, "longitude": 2, "date": "20260803", "time": "2460", "timezone": "+0800"}),
        ("qweather_astronomy", {"operation": "solar_elevation", "latitude": 1, "longitude": 2, "date": "20260803", "time": "1200", "timezone": "+1460"}),
        ("qweather_astronomy", {"operation": "solar_elevation", "latitude": 1, "longitude": 2, "date": "20260803", "time": "1200", "timezone": "0800"}),
        ("qweather_air_quality", {"operation": "station", "location_id": "https://example.com"}),
    ],
)
def test_operation_validation_rejects_invalid_or_irrelevant_parameters(tool_name, arguments):
    client = RecordingClient()
    with pytest.raises(ToolExecutionError) as captured:
        _call(client, tool_name, arguments)
    assert captured.value.code == "qweather_invalid_request"
    assert client.calls == []


def test_config_loads_dotenv_relative_key_and_environment_override(tmp_path: Path):
    key = Ed25519PrivateKey.generate()
    key_dir = tmp_path / "secrets"
    key_dir.mkdir()
    (key_dir / "qweather.pem").write_bytes(_private_key_pem(key))
    (tmp_path / ".env").write_text(
        "QWEATHER_API_HOST=file.qweatherapi.com\n"
        "QWEATHER_PROJECT_ID=project-file\n"
        "QWEATHER_CREDENTIAL_ID=credential-file\n"
        "QWEATHER_PRIVATE_KEY_PATH=secrets/qweather.pem\n",
        encoding="utf-8",
    )

    config = QWeatherConfig.load(
        tmp_path,
        environ={"QWEATHER_API_HOST": "ENV.qweatherapi.com"},
    )

    assert config is not None
    assert config.api_host == "env.qweatherapi.com"
    assert config.project_id == "project-file"
    assert isinstance(config.private_key, Ed25519PrivateKey)
    assert "private_key" not in repr(config)


def test_config_missing_partial_invalid_host_and_invalid_key_are_safe(tmp_path: Path):
    assert QWeatherConfig.load(tmp_path, environ={}) is None
    with pytest.raises(QWeatherConfigError, match="配置不完整"):
        QWeatherConfig.load(tmp_path, environ={"QWEATHER_API_HOST": "x.qweatherapi.com"})

    values = {
        "QWEATHER_API_HOST": "https://evil.example/path",
        "QWEATHER_PROJECT_ID": "secret-project",
        "QWEATHER_CREDENTIAL_ID": "secret-credential",
        "QWEATHER_PRIVATE_KEY_PATH": "secret.pem",
    }
    with pytest.raises(QWeatherConfigError) as invalid_host:
        QWeatherConfig.load(tmp_path, environ=values)
    assert "secret-project" not in str(invalid_host.value)

    (tmp_path / "secret.pem").write_text("not a private key", encoding="utf-8")
    values["QWEATHER_API_HOST"] = "valid.qweatherapi.com"
    with pytest.raises(QWeatherConfigError) as invalid_key:
        QWeatherConfig.load(tmp_path, environ=values)
    assert "secret-project" not in str(invalid_key.value)
    assert "not a private key" not in str(invalid_key.value)


def test_jwt_header_claims_signature_cache_and_refresh():
    async def scenario():
        key = Ed25519PrivateKey.generate()
        now = [1_900_000_000]
        client = QWeatherClient(
            _config(key),
            http_client=httpx.AsyncClient(
                base_url="https://abc123.qweatherapi.com",
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
            ),
            clock=lambda: now[0],
        )
        first = await client.token()
        cached = await client.token()
        header = jwt.get_unverified_header(first)
        claims = jwt.decode(
            first,
            key.public_key(),
            algorithms=["EdDSA"],
            options={"verify_exp": False, "verify_iat": False},
        )
        assert first == cached
        assert header["alg"] == "EdDSA"
        assert header["kid"] == "credential-456"
        assert claims == {"sub": "project-123", "iat": now[0] - 30, "exp": now[0] + 870}

        now[0] += 811
        refreshed = await client.token()
        assert refreshed != first
        await client.close()

    asyncio.run(scenario())


def test_default_http_client_uses_https_gzip_and_disables_redirects():
    async def scenario():
        client = QWeatherClient(_config())
        assert str(client._http.base_url) == "https://abc123.qweatherapi.com"
        assert client._http.headers["Accept-Encoding"] == "gzip"
        assert client._http.follow_redirects is False
        await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (400, {"type": "about:blank", "status": 400}, "qweather_invalid_request"),
        (401, {"status": 401}, "qweather_unauthorized"),
        (403, {"status": 403}, "qweather_forbidden"),
        (429, {"status": 429}, "qweather_rate_limited"),
        (503, {"status": 503}, "qweather_unavailable"),
        (200, {"code": "401"}, "qweather_unauthorized"),
        (200, {"code": "429"}, "qweather_rate_limited"),
        (200, {"code": "204"}, "qweather_invalid_request"),
    ],
)
def test_http_problem_and_legacy_errors_are_stable(status, payload, expected):
    async def scenario():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(status, json=payload, request=request)
        )
        client = QWeatherClient(
            _config(),
            http_client=httpx.AsyncClient(
                base_url="https://abc123.qweatherapi.com", transport=transport
            ),
        )
        with pytest.raises(ToolExecutionError) as captured:
            await client.request("qweather_geo.top_city")
        assert captured.value.code == expected
        assert "project-123" not in captured.value.safe_message
        await client.close()

    asyncio.run(scenario())


def test_network_timeout_maps_to_unavailable_without_retry():
    calls = []

    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("timeout", request=request)

    async def scenario():
        client = QWeatherClient(
            _config(),
            http_client=httpx.AsyncClient(
                base_url="https://abc123.qweatherapi.com",
                transport=httpx.MockTransport(fail),
            ),
        )
        with pytest.raises(ToolExecutionError) as captured:
            await client.request("qweather_geo.top_city")
        assert captured.value.code == "qweather_unavailable"
        assert len(calls) == 1
        await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["network", "invalid_json"])
def test_network_failure_and_invalid_json_map_to_unavailable(failure):
    async def scenario():
        def respond(request):
            if failure == "network":
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(200, content=b"not-json", request=request)

        client = QWeatherClient(
            _config(),
            http_client=httpx.AsyncClient(
                base_url="https://abc123.qweatherapi.com",
                transport=httpx.MockTransport(respond),
            ),
        )
        with pytest.raises(ToolExecutionError) as captured:
            await client.request("qweather_geo.top_city")
        assert captured.value.code == "qweather_unavailable"
        await client.close()

    asyncio.run(scenario())


def test_pagination_attribution_and_raw_response_are_preserved():
    payload = {
        "metadata": {"attributions": ["official source", "https://example.test/source"]},
        "indexes": [{"n": value} for value in range(60)],
        "pollutants": [{"n": value} for value in range(60)],
        "stations": [{"n": value} for value in range(60)],
        "untouched": {"value": 7},
    }
    result = _call(
        RecordingClient(payload),
        "qweather_air_quality",
        {"operation": "current", "latitude": 1, "longitude": 2, "offset": 10, "limit": 20},
    )

    assert result["response"]["indexes"] == [{"n": value} for value in range(10, 30)]
    assert result["response"]["untouched"] == {"value": 7}
    assert result["provider"]["upstream_attributions"] == payload["metadata"]["attributions"]
    assert result["provider"]["upstream_attributions_must_display"] is True
    assert result["pagination"] == {
        "offset": 10,
        "limit": 20,
        "lists": [
            {"path": "indexes", "total": 60, "returned": 20, "has_more": True},
            {"path": "pollutants", "total": 60, "returned": 20, "has_more": True},
            {"path": "stations", "total": 60, "returned": 20, "has_more": True},
        ],
    }


def test_oversized_result_fails_instead_of_registry_string_truncation():
    payload = {"hours": [{"text": "x" * 65_000}]}
    with pytest.raises(ToolExecutionError) as captured:
        _call(
            RecordingClient(payload),
            "qweather_weather",
            {"operation": "hourly", "latitude": 1, "longitude": 2, "limit": 1},
        )
    assert captured.value.code == "qweather_response_too_large"


def test_registration_exposes_exact_read_only_tools_and_closes_client_once(tmp_path: Path):
    async def scenario():
        registry = ToolRegistry()
        client = RecordingClient()
        assert register_qweather_tools(registry, project_root=tmp_path, client=client)
        names = {spec.name for _, spec in registry._tools.values()}
        assert names == {
            "qweather_geo",
            "qweather_weather",
            "qweather_minutely",
            "qweather_warning",
            "qweather_indices",
            "qweather_air_quality",
            "qweather_time_machine",
            "qweather_tropical_cyclone",
            "qweather_ocean",
            "qweather_solar_radiation",
            "qweather_astronomy",
        }
        assert all(spec.risk is ToolRisk.READ_ONLY for _, spec in registry._tools.values())
        assert all(spec.max_output_chars == 65_536 for _, spec in registry._tools.values())
        assert sum(spec.shutdown is not None for _, spec in registry._tools.values()) == 1
        await registry.shutdown()
        assert client.close_calls == 1

    asyncio.run(scenario())


def test_complete_dotenv_registers_tools_without_a_live_request(tmp_path: Path):
    async def scenario():
        key_path = tmp_path / "qweather.pem"
        key_path.write_bytes(_private_key_pem(Ed25519PrivateKey.generate()))
        (tmp_path / ".env").write_text(
            "QWEATHER_API_HOST=test.qweatherapi.com\n"
            "QWEATHER_PROJECT_ID=test-project\n"
            "QWEATHER_CREDENTIAL_ID=test-credential\n"
            "QWEATHER_PRIVATE_KEY_PATH=qweather.pem\n",
            encoding="utf-8",
        )
        registry = ToolRegistry()
        assert register_qweather_tools(registry, project_root=tmp_path, environ={})
        assert len(registry._tools) == 11
        await registry.shutdown()

    asyncio.run(scenario())


def test_unconfigured_and_invalid_registration_skip_safely(tmp_path: Path, caplog):
    registry = ToolRegistry()
    assert not register_qweather_tools(registry, project_root=tmp_path, environ={})
    assert registry._tools == {}

    caplog.set_level(logging.WARNING)
    values = {
        "QWEATHER_API_HOST": "valid.qweatherapi.com",
        "QWEATHER_PROJECT_ID": "private-project",
        "QWEATHER_CREDENTIAL_ID": "private-credential",
        "QWEATHER_PRIVATE_KEY_PATH": "missing-private.pem",
    }
    assert not register_qweather_tools(
        registry,
        project_root=tmp_path,
        environ=values,
    )
    assert "未注册 QWeather Tools" in caplog.text
    assert "private-project" not in caplog.text
    assert "private-credential" not in caplog.text
    assert "missing-private.pem" not in caplog.text


def test_system_rules_include_mandatory_qweather_attribution_exception():
    assert "qweather_" in _AGENT_SYSTEM_RULES
    assert "render_information_card" in _AGENT_SYSTEM_RULES
    assert "render_weather" in _AGENT_SYSTEM_RULES
    assert "天气服务由和风天气驱动" in _AGENT_SYSTEM_RULES
    assert "www.qweather.com" in _AGENT_SYSTEM_RULES
    assert "upstream_attributions" in _AGENT_SYSTEM_RULES
    assert "不得再重复归因、来源或 URL" in _AGENT_SYSTEM_RULES


def test_agent_allowlist_only_declares_explicit_qweather_tool(tmp_path: Path):
    class Provider:
        def __init__(self):
            self.requests = []

        def supports_tools(self, model):
            return True

        async def complete_request(self, model, request):
            self.requests.append(request)
            return ProviderResponse("done", (), AssistantTurn(text="done"))

        async def chat(self, model, message, **kwargs):
            return "fallback"

    async def scenario():
        registry = ToolRegistry()
        client = RecordingClient()
        register_qweather_tools(registry, project_root=tmp_path, client=client)
        provider = Provider()
        runner = AgentRunner(
            provider,
            registry,
            allowed_tool_names=frozenset({"qweather_geo"}),
        )
        conversation = ConversationKey(
            protocol="onebot",
            self_id="bot",
            kind=ConversationKind.PRIVATE,
            conversation_id="user",
            preset="Normal",
        )
        context = ToolContext(
            event=SimpleNamespace(),
            actions=SimpleNamespace(protocol="onebot", capabilities=frozenset()),
            conversation=conversation,
            canonical_user_id="qq:user",
            runtime={},
            memory=None,
        )
        answer = await runner.run(
            model="test",
            message="weather",
            history=(),
            system_prompt="test",
            attachments=(),
            context=context,
            enabled=True,
        )
        assert answer == "done"
        assert [tool.name for tool in provider.requests[0].tools] == ["qweather_geo"]
        await registry.shutdown()

    asyncio.run(scenario())
