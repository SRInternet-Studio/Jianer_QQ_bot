from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from dotenv import dotenv_values

from plugins.JianerAI.tools.contracts import (
    ToolContext,
    ToolExecutionError,
    ToolRisk,
    ToolSpec,
)
from plugins.JianerAI.tools.registry import ToolRegistry


_LOGGER = logging.getLogger(__name__)
_CONFIG_KEYS = (
    "QWEATHER_API_HOST",
    "QWEATHER_PROJECT_ID",
    "QWEATHER_CREDENTIAL_ID",
    "QWEATHER_PRIVATE_KEY_PATH",
)
_HOST_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+qweatherapi\.com$"
)
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_LOCATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DATE_PATTERN = re.compile(r"^\d{8}$")
_TIME_PATTERN = re.compile(r"^\d{4}$")
_TIMEZONE_PATTERN = re.compile(r"^[+-]?\d{4}$")
_LANGUAGES = (
    "zh",
    "zh-hans",
    "zh-hant",
    "en",
    "de",
    "es",
    "fr",
    "it",
    "ja",
    "ko",
    "ru",
    "hi",
    "th",
    "ar",
    "pt",
    "bn",
    "ms",
    "nl",
    "el",
    "la",
    "sv",
    "id",
    "pl",
    "tr",
    "cs",
    "et",
    "vi",
    "fil",
    "fi",
    "he",
    "is",
    "nb",
)
_INDEX_TYPES = (0, 1, 2, 3, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15, 16)
_QWEATHER_URL = "https://www.qweather.com"
_MAX_RESULT_BYTES = 64_000


class QWeatherConfigError(ValueError):
    """A configuration problem whose text is safe to write to logs."""


@dataclass(frozen=True, slots=True)
class QWeatherConfig:
    api_host: str
    project_id: str
    credential_id: str
    private_key: Ed25519PrivateKey = field(repr=False, compare=False)

    @property
    def base_url(self) -> str:
        return f"https://{self.api_host}"

    @classmethod
    def load(
        cls,
        project_root: Path,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> QWeatherConfig | None:
        root = Path(project_root).resolve()
        file_values = dotenv_values(root / ".env")
        environment = os.environ if environ is None else environ
        values = {
            key: str(
                environment[key]
                if key in environment
                else file_values.get(key) or ""
            ).strip()
            for key in _CONFIG_KEYS
        }
        if not any(values.values()):
            return None
        missing = [key for key, value in values.items() if not value]
        if missing:
            raise QWeatherConfigError(
                "QWeather 配置不完整，缺少：" + ", ".join(missing)
            )

        host = _normalize_api_host(values["QWEATHER_API_HOST"])
        project_id = _validate_credential_identifier(
            values["QWEATHER_PROJECT_ID"], "项目 ID"
        )
        credential_id = _validate_credential_identifier(
            values["QWEATHER_CREDENTIAL_ID"], "凭据 ID"
        )
        key_path = Path(values["QWEATHER_PRIVATE_KEY_PATH"])
        if not key_path.is_absolute():
            key_path = root / key_path
        try:
            key_bytes = key_path.resolve().read_bytes()
        except OSError as exc:
            raise QWeatherConfigError("QWeather 私钥文件无法读取。") from exc
        try:
            private_key = load_pem_private_key(key_bytes, password=None)
        except (TypeError, ValueError) as exc:
            raise QWeatherConfigError("QWeather 私钥不是有效的 PKCS#8 PEM。") from exc
        if not isinstance(private_key, Ed25519PrivateKey):
            raise QWeatherConfigError("QWeather 私钥必须是 Ed25519 私钥。")
        return cls(
            api_host=host,
            project_id=project_id,
            credential_id=credential_id,
            private_key=private_key,
        )


@dataclass(frozen=True, slots=True)
class _Endpoint:
    path: str


_ENDPOINTS = {
    "qweather_geo.city_lookup": _Endpoint("/geo/v2/city/lookup"),
    "qweather_geo.top_city": _Endpoint("/geo/v2/city/top"),
    "qweather_geo.poi_lookup": _Endpoint("/geo/v2/poi/lookup"),
    "qweather_geo.poi_range": _Endpoint("/geo/v2/poi/range"),
    "qweather_weather.current": _Endpoint(
        "/weather/v1/current/{latitude}/{longitude}"
    ),
    "qweather_weather.daily": _Endpoint(
        "/weather/v1/daily/{latitude}/{longitude}"
    ),
    "qweather_weather.hourly": _Endpoint(
        "/weather/v1/hourly/{latitude}/{longitude}"
    ),
    "qweather_minutely.precipitation": _Endpoint("/v7/minutely/5m"),
    "qweather_warning.current": _Endpoint(
        "/weatheralert/v1/current/{latitude}/{longitude}"
    ),
    "qweather_indices.forecast": _Endpoint("/v7/indices/{days}"),
    "qweather_air_quality.current": _Endpoint(
        "/airquality/v1/current/{latitude}/{longitude}"
    ),
    "qweather_air_quality.hourly": _Endpoint(
        "/airquality/v1/hourly/{latitude}/{longitude}"
    ),
    "qweather_air_quality.daily": _Endpoint(
        "/airquality/v1/daily/{latitude}/{longitude}"
    ),
    "qweather_air_quality.station": _Endpoint(
        "/airquality/v1/stations/{location_id}"
    ),
    "qweather_time_machine.weather": _Endpoint("/v7/historical/weather"),
    "qweather_tropical_cyclone.list": _Endpoint("/v7/tropical/storm-list"),
    "qweather_tropical_cyclone.track": _Endpoint("/v7/tropical/storm-track"),
    "qweather_tropical_cyclone.forecast": _Endpoint(
        "/v7/tropical/storm-forecast"
    ),
    "qweather_ocean.tide": _Endpoint("/v7/ocean/tide"),
    "qweather_solar_radiation.forecast": _Endpoint(
        "/solarradiation/v1/forecast/{latitude}/{longitude}"
    ),
    "qweather_astronomy.sun": _Endpoint("/v7/astronomy/sun"),
    "qweather_astronomy.moon": _Endpoint("/v7/astronomy/moon"),
    "qweather_astronomy.solar_elevation": _Endpoint(
        "/v7/astronomy/solar-elevation-angle"
    ),
}


class QWeatherClient:
    """Shared asynchronous client with a cached EdDSA JWT."""

    def __init__(
        self,
        config: QWeatherConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._clock = clock
        self._token: str | None = None
        self._token_expiry = 0
        self._token_lock = asyncio.Lock()
        self._http = http_client or httpx.AsyncClient(
            base_url=config.base_url,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": "JianerAI-QWeather/1.0",
            },
            follow_redirects=False,
            timeout=httpx.Timeout(15.0),
        )

    async def token(self) -> str:
        now = int(self._clock())
        if self._token is not None and now < self._token_expiry - 60:
            return self._token
        async with self._token_lock:
            now = int(self._clock())
            if self._token is not None and now < self._token_expiry - 60:
                return self._token
            issued_at = now - 30
            expires_at = issued_at + 900
            self._token = jwt.encode(
                {
                    "sub": self.config.project_id,
                    "iat": issued_at,
                    "exp": expires_at,
                },
                self.config.private_key,
                algorithm="EdDSA",
                headers={
                    "alg": "EdDSA",
                    "kid": self.config.credential_id,
                },
            )
            self._token_expiry = expires_at
            return self._token

    async def request(
        self,
        endpoint_name: str,
        *,
        path_values: Mapping[str, str] | None = None,
        params: Mapping[str, str | int] | None = None,
    ) -> dict[str, Any]:
        endpoint = _ENDPOINTS.get(endpoint_name)
        if endpoint is None:
            raise ToolExecutionError(
                "qweather_invalid_request", "不支持的和风天气操作。"
            )
        try:
            path = endpoint.path.format(**dict(path_values or {}))
        except (KeyError, ValueError) as exc:
            raise ToolExecutionError(
                "qweather_invalid_request", "和风天气请求参数不完整。"
            ) from exc
        try:
            response = await self._http.get(
                path,
                params=dict(params or {}),
                headers={"Authorization": f"Bearer {await self.token()}"},
            )
        except httpx.TimeoutException as exc:
            raise ToolExecutionError(
                "qweather_unavailable", "和风天气请求超时，请稍后重试。"
            ) from exc
        except httpx.RequestError as exc:
            raise ToolExecutionError(
                "qweather_unavailable", "暂时无法连接和风天气服务。"
            ) from exc

        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ToolExecutionError(
                "qweather_unavailable", "和风天气返回了无法解析的响应。"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ToolExecutionError(
                "qweather_unavailable", "和风天气返回了无效响应。"
            )
        _raise_for_qweather_error(response.status_code, payload)
        return dict(payload)

    async def close(self) -> None:
        await self._http.aclose()


_TOOL_OPERATIONS = {
    "qweather_geo": ("city_lookup", "top_city", "poi_lookup", "poi_range"),
    "qweather_weather": ("current", "daily", "hourly"),
    "qweather_minutely": ("precipitation",),
    "qweather_warning": ("current",),
    "qweather_indices": ("forecast",),
    "qweather_air_quality": ("current", "hourly", "daily", "station"),
    "qweather_time_machine": ("weather",),
    "qweather_tropical_cyclone": ("list", "track", "forecast"),
    "qweather_ocean": ("tide",),
    "qweather_solar_radiation": ("forecast",),
    "qweather_astronomy": ("sun", "moon", "solar_elevation"),
}

_TOOL_PARAMETERS = {
    "qweather_geo": (
        "location",
        "administrative_area",
        "country_code",
        "city",
        "poi_type",
        "latitude",
        "longitude",
        "radius_km",
        "number",
        "language",
    ),
    "qweather_weather": (
        "latitude",
        "longitude",
        "days",
        "hours",
        "local_time",
        "language",
    ),
    "qweather_minutely": ("latitude", "longitude", "language"),
    "qweather_warning": (
        "latitude",
        "longitude",
        "local_time",
        "language",
    ),
    "qweather_indices": (
        "location_id",
        "days",
        "index_types",
        "language",
    ),
    "qweather_air_quality": (
        "latitude",
        "longitude",
        "location_id",
        "local_time",
        "language",
    ),
    "qweather_time_machine": ("location_id", "date", "language", "unit"),
    "qweather_tropical_cyclone": ("basin", "year", "storm_id"),
    "qweather_ocean": ("location_id", "date"),
    "qweather_solar_radiation": (
        "latitude",
        "longitude",
        "hours",
        "interval_minutes",
        "tilt",
        "azimuth",
        "extra",
        "local_time",
    ),
    "qweather_astronomy": (
        "location_id",
        "latitude",
        "longitude",
        "date",
        "time",
        "timezone",
        "altitude_m",
        "language",
    ),
}

_OPERATION_PARAMETERS = {
    "qweather_geo.city_lookup": {
        "location", "administrative_area", "country_code", "number", "language"
    },
    "qweather_geo.top_city": {"country_code", "number", "language"},
    "qweather_geo.poi_lookup": {
        "location", "poi_type", "city", "number", "language"
    },
    "qweather_geo.poi_range": {
        "latitude", "longitude", "poi_type", "radius_km", "number", "language"
    },
    "qweather_weather.current": {"latitude", "longitude", "local_time", "language"},
    "qweather_weather.daily": {
        "latitude", "longitude", "days", "local_time", "language"
    },
    "qweather_weather.hourly": {
        "latitude", "longitude", "hours", "local_time", "language"
    },
    "qweather_minutely.precipitation": {"latitude", "longitude", "language"},
    "qweather_warning.current": {"latitude", "longitude", "local_time", "language"},
    "qweather_indices.forecast": {"location_id", "days", "index_types", "language"},
    "qweather_air_quality.current": {"latitude", "longitude", "language"},
    "qweather_air_quality.hourly": {"latitude", "longitude", "language"},
    "qweather_air_quality.daily": {"latitude", "longitude", "local_time", "language"},
    "qweather_air_quality.station": {"location_id", "language"},
    "qweather_time_machine.weather": {"location_id", "date", "language", "unit"},
    "qweather_tropical_cyclone.list": {"basin", "year"},
    "qweather_tropical_cyclone.track": {"storm_id"},
    "qweather_tropical_cyclone.forecast": {"storm_id"},
    "qweather_ocean.tide": {"location_id", "date"},
    "qweather_solar_radiation.forecast": {
        "latitude", "longitude", "hours", "interval_minutes", "tilt", "azimuth",
        "extra", "local_time"
    },
    "qweather_astronomy.sun": {"location_id", "date"},
    "qweather_astronomy.moon": {"location_id", "date", "language"},
    "qweather_astronomy.solar_elevation": {
        "latitude", "longitude", "date", "time", "timezone", "altitude_m"
    },
}

_DESCRIPTIONS = {
    "qweather_geo": "查询和风天气 GeoAPI：城市搜索、热门城市、POI 搜索或附近 POI。",
    "qweather_weather": "查询和风天气 Weather v1 的实时、逐日或逐小时天气。",
    "qweather_minutely": "查询和风天气未来两小时分钟级降水预报。",
    "qweather_warning": "查询和风天气正在生效的官方天气预警。",
    "qweather_indices": "查询和风天气生活指数预报。",
    "qweather_air_quality": "查询和风天气实时、逐小时、逐日空气质量或监测站数据。",
    "qweather_time_machine": "查询和风天气最近十天的历史天气。",
    "qweather_tropical_cyclone": "查询和风天气热带气旋列表、实况路径或预报路径。",
    "qweather_ocean": "查询和风天气潮汐数据。",
    "qweather_solar_radiation": "查询和风天气太阳辐射预报。",
    "qweather_astronomy": "查询和风天气日出日落、月升月落月相或太阳高度角。",
}

_LIST_PATHS = {
    "qweather_geo.city_lookup": ("location",),
    "qweather_geo.top_city": ("topCityList",),
    "qweather_geo.poi_lookup": ("poi",),
    "qweather_geo.poi_range": ("poi",),
    "qweather_weather.daily": ("days",),
    "qweather_weather.hourly": ("hours",),
    "qweather_minutely.precipitation": ("minutely",),
    "qweather_warning.current": ("alerts",),
    "qweather_indices.forecast": ("daily",),
    "qweather_air_quality.current": ("indexes", "pollutants", "stations"),
    "qweather_air_quality.hourly": ("hours",),
    "qweather_air_quality.daily": ("days",),
    "qweather_air_quality.station": ("pollutants",),
    "qweather_time_machine.weather": ("weatherDaily", "weatherHourly"),
    "qweather_tropical_cyclone.list": ("storm",),
    "qweather_tropical_cyclone.track": ("track",),
    "qweather_tropical_cyclone.forecast": ("forecast",),
    "qweather_ocean.tide": ("tideTable", "tideHourly"),
    "qweather_solar_radiation.forecast": ("forecasts",),
    "qweather_astronomy.moon": ("moonPhase",),
}


def qweather_tools(
    client: QWeatherClient,
    *,
    today: Callable[[], date] = date.today,
) -> tuple[ToolSpec, ...]:
    specs: list[ToolSpec] = []
    for index, (tool_name, operations) in enumerate(_TOOL_OPERATIONS.items()):
        handler = _make_handler(tool_name, client, today=today)
        specs.append(
            ToolSpec(
                name=tool_name,
                description=(
                    f"{_DESCRIPTIONS[tool_name]} operation 必须为："
                    + "、".join(operations)
                    + "。取得结果后优先调用 render_information_card 的 render_weather，"
                    "把天气详情和必须展示的归因放入图片。"
                ),
                input_schema=_tool_schema(tool_name, operations),
                handler=handler,
                risk=ToolRisk.READ_ONLY,
                timeout_seconds=20.0,
                max_output_chars=65_536,
                shutdown=client.close if index == 0 else None,
            )
        )
    return tuple(specs)


def register_qweather_tools(
    registry: ToolRegistry,
    *,
    project_root: Path,
    logger: logging.Logger | None = None,
    environ: Mapping[str, str] | None = None,
    client: QWeatherClient | None = None,
) -> bool:
    log = logger or _LOGGER
    if client is None:
        try:
            config = QWeatherConfig.load(project_root, environ=environ)
        except QWeatherConfigError as exc:
            log.warning("未注册 QWeather Tools：%s", exc)
            return False
        if config is None:
            return False
        client = QWeatherClient(config)
    for spec in qweather_tools(client):
        registry.register(spec)
    return True


def _make_handler(
    tool_name: str,
    client: QWeatherClient,
    *,
    today: Callable[[], date],
) -> Callable[[ToolContext, Mapping[str, Any]], Any]:
    async def handler(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        del context
        operation = str(arguments.get("operation", ""))
        if operation not in _TOOL_OPERATIONS[tool_name]:
            raise ToolExecutionError(
                "qweather_invalid_request", "该工具不支持这个 operation。"
            )
        endpoint_name = f"{tool_name}.{operation}"
        request = _build_request(
            tool_name,
            operation,
            arguments,
            current_date=today(),
        )
        payload = await client.request(
            endpoint_name,
            path_values=request["path_values"],
            params=request["params"],
        )
        offset = int(arguments.get("offset", 0))
        limit = int(arguments.get("limit", 50))
        paged_payload, pagination = _paginate_payload(
            endpoint_name,
            payload,
            offset=offset,
            limit=limit,
        )
        upstream_attributions = _extract_attributions(payload)
        result = {
            "operation": operation,
            "provider": {
                "name": "QWeather",
                "attribution": "天气服务由和风天气驱动",
                "url": _QWEATHER_URL,
                "must_display": True,
                "upstream_attributions": upstream_attributions,
                "upstream_attributions_must_display": bool(
                    upstream_attributions
                    and tool_name in {"qweather_warning", "qweather_air_quality"}
                ),
            },
            "pagination": pagination,
            "response": paged_payload,
        }
        encoded_result = json.dumps(
            result, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded_result) > _MAX_RESULT_BYTES:
            raise ToolExecutionError(
                "qweather_response_too_large",
                "和风天气结果超过 64 KiB，请减小 limit 后重试。",
            )
        return result

    return handler


def _build_request(
    tool_name: str,
    operation: str,
    arguments: Mapping[str, Any],
    *,
    current_date: date,
) -> dict[str, dict[str, str | int]]:
    allowed = {
        "operation",
        "offset",
        "limit",
        *_OPERATION_PARAMETERS[f"{tool_name}.{operation}"],
    }
    unexpected = set(arguments) - allowed
    if unexpected:
        raise ToolExecutionError(
            "qweather_invalid_request",
            "当前 operation 不接受参数：" + ", ".join(sorted(unexpected)),
        )
    path_values: dict[str, str] = {}
    params: dict[str, str | int] = {}

    if tool_name == "qweather_geo":
        _build_geo_request(operation, arguments, params)
    elif tool_name == "qweather_weather":
        _put_coordinates(arguments, path_values)
        _optional_bool(arguments, "local_time", params, "localTime")
        _optional_language(arguments, params)
        if operation == "daily":
            _optional_int(arguments, "days", params, "days", 1, 10)
        elif operation == "hourly":
            _optional_int(arguments, "hours", params, "hours", 1, 240)
    elif tool_name == "qweather_minutely":
        params["location"] = _coordinate_pair(arguments)
        _optional_language(arguments, params)
    elif tool_name == "qweather_warning":
        _put_coordinates(arguments, path_values)
        _optional_bool(arguments, "local_time", params, "localTime")
        _optional_language(arguments, params)
    elif tool_name == "qweather_indices":
        path_values["days"] = _required_enum(arguments, "days", ("1d", "3d"))
        params["location"] = _required_location_id(arguments)
        types = arguments.get("index_types")
        if not isinstance(types, list) or not types:
            _invalid("index_types 必须是非空数组。")
        if any(isinstance(value, bool) or value not in _INDEX_TYPES for value in types):
            _invalid("index_types 包含不支持的指数类型。")
        if 0 in types and len(types) != 1:
            _invalid("指数类型 0 只能单独使用。")
        params["type"] = ",".join(str(value) for value in types)
        _optional_language(arguments, params)
    elif tool_name == "qweather_air_quality":
        if operation == "station":
            path_values["location_id"] = _required_location_id(arguments)
        else:
            _put_coordinates(arguments, path_values)
            if operation == "daily":
                _optional_bool(arguments, "local_time", params, "localTime")
        _optional_language(arguments, params)
    elif tool_name == "qweather_time_machine":
        params["location"] = _required_location_id(arguments)
        history_date = _required_date(arguments)
        if not current_date - timedelta(days=10) <= history_date < current_date:
            _invalid("时光机 date 必须是今天之前最近 10 天内的日期。")
        params["date"] = history_date.strftime("%Y%m%d")
        if "unit" in arguments:
            params["unit"] = {"metric": "m", "imperial": "i"}[
                _required_enum(arguments, "unit", ("metric", "imperial"))
            ]
        _optional_language(arguments, params)
    elif tool_name == "qweather_tropical_cyclone":
        if operation == "list":
            params["basin"] = _required_enum(arguments, "basin", ("NP",))
            year = _required_int(arguments, "year", 1900, 9999)
            if year not in {current_date.year, current_date.year - 1}:
                _invalid("year 只支持当前年份或上一年份。")
            params["year"] = year
        else:
            params["stormid"] = _required_identifier(arguments, "storm_id")
    elif tool_name == "qweather_ocean":
        params["location"] = _required_location_id(arguments)
        tide_date = _required_date(arguments)
        if not current_date <= tide_date <= current_date + timedelta(days=9):
            _invalid("潮汐 date 必须在今天起未来 10 天内。")
        params["date"] = tide_date.strftime("%Y%m%d")
    elif tool_name == "qweather_solar_radiation":
        _put_coordinates(arguments, path_values)
        _optional_int(arguments, "hours", params, "hours", 1, 60)
        if "interval_minutes" in arguments:
            params["interval"] = int(
                _required_enum(arguments, "interval_minutes", (15, 30, 60))
            )
        _optional_int(arguments, "tilt", params, "tilt", 0, 90)
        _optional_int(arguments, "azimuth", params, "azimuth", 0, 359)
        extras = arguments.get("extra")
        if extras is not None:
            if not isinstance(extras, list) or not extras:
                _invalid("extra 必须是非空数组。")
            if len(set(extras)) != len(extras) or any(
                item not in {"weather", "poa"} for item in extras
            ):
                _invalid("extra 只支持 weather 和 poa，且不能重复。")
            if "poa" in extras and not {"tilt", "azimuth"}.issubset(arguments):
                _invalid("extra 包含 poa 时必须同时提供 tilt 和 azimuth。")
            params["extra"] = ",".join(extras)
        _optional_bool(arguments, "local_time", params, "localTime")
    elif tool_name == "qweather_astronomy":
        if operation in {"sun", "moon"}:
            params["location"] = _required_location_id(arguments)
            astro_date = _required_date(arguments)
            if not current_date <= astro_date <= current_date + timedelta(days=59):
                _invalid("天文 date 必须在今天起未来 60 天内。")
            params["date"] = astro_date.strftime("%Y%m%d")
            if operation == "moon":
                _optional_language(arguments, params)
        else:
            params["location"] = _coordinate_pair(arguments)
            params["date"] = _required_date(arguments).strftime("%Y%m%d")
            params["time"] = _required_time(arguments)
            params["tz"] = _required_timezone(arguments)
            altitude = arguments.get("altitude_m")
            if isinstance(altitude, bool) or not isinstance(altitude, (int, float)):
                _invalid("solar_elevation 必须提供数字 altitude_m。")
            if not -500 <= float(altitude) <= 9000:
                _invalid("altitude_m 必须在 -500 到 9000 之间。")
            params["alt"] = str(altitude)
    return {"path_values": path_values, "params": params}


def _build_geo_request(
    operation: str,
    arguments: Mapping[str, Any],
    params: dict[str, str | int],
) -> None:
    if operation in {"city_lookup", "poi_lookup"}:
        params["location"] = _required_text(arguments, "location", 80)
    if operation == "city_lookup" and "administrative_area" in arguments:
        params["adm"] = _required_text(arguments, "administrative_area", 80)
    if operation in {"city_lookup", "top_city"} and "country_code" in arguments:
        country = _required_text(arguments, "country_code", 2).lower()
        if re.fullmatch(r"[a-z]{2}", country) is None:
            _invalid("country_code 必须是两个英文字母。")
        params["range"] = country
    if operation in {"poi_lookup", "poi_range"}:
        params["type"] = _required_enum(arguments, "poi_type", ("scenic", "TSTA"))
    if operation == "poi_lookup" and "city" in arguments:
        params["city"] = _required_text(arguments, "city", 80)
    if operation == "poi_range":
        params["location"] = _coordinate_pair(arguments)
        _optional_int(arguments, "radius_km", params, "radius", 1, 50)
    _optional_int(arguments, "number", params, "number", 1, 20)
    _optional_language(arguments, params)


def _tool_schema(tool_name: str, operations: tuple[str, ...]) -> dict[str, Any]:
    all_properties: dict[str, dict[str, Any]] = {
        "operation": {
            "type": "string",
            "enum": list(operations),
            "description": "要执行的固定操作。",
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100_000,
            "default": 0,
            "description": "本地列表分页偏移量。",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "default": 50,
            "description": "本地列表分页条数，最大 50。",
        },
        "location": {"type": "string", "minLength": 1, "maxLength": 80},
        "administrative_area": {"type": "string", "minLength": 1, "maxLength": 80},
        "country_code": {"type": "string", "minLength": 2, "maxLength": 2},
        "city": {"type": "string", "minLength": 1, "maxLength": 80},
        "poi_type": {"type": "string", "enum": ["scenic", "TSTA"]},
        "latitude": {"type": "number", "minimum": -90, "maximum": 90},
        "longitude": {"type": "number", "minimum": -180, "maximum": 180},
        "radius_km": {"type": "integer", "minimum": 1, "maximum": 50},
        "number": {"type": "integer", "minimum": 1, "maximum": 20},
        "language": {"type": "string", "enum": list(_LANGUAGES)},
        "days": {"type": "integer", "minimum": 1, "maximum": 10},
        "hours": {"type": "integer", "minimum": 1, "maximum": 240},
        "local_time": {"type": "boolean"},
        "location_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "index_types": {
            "type": "array",
            "minItems": 1,
            "maxItems": 15,
            "items": {"type": "integer", "enum": list(_INDEX_TYPES)},
        },
        "date": {"type": "string", "minLength": 8, "maxLength": 8},
        "unit": {"type": "string", "enum": ["metric", "imperial"]},
        "basin": {"type": "string", "enum": ["NP"]},
        "year": {"type": "integer", "minimum": 1900, "maximum": 9999},
        "storm_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "interval_minutes": {"type": "integer", "enum": [15, 30, 60]},
        "tilt": {"type": "integer", "minimum": 0, "maximum": 90},
        "azimuth": {"type": "integer", "minimum": 0, "maximum": 359},
        "extra": {
            "type": "array",
            "minItems": 1,
            "maxItems": 2,
            "items": {"type": "string", "enum": ["weather", "poa"]},
        },
        "time": {"type": "string", "minLength": 4, "maxLength": 4},
        "timezone": {"type": "string", "minLength": 4, "maxLength": 5},
        "altitude_m": {"type": "number", "minimum": -500, "maximum": 9000},
    }
    names = ("operation", *_TOOL_PARAMETERS[tool_name], "offset", "limit")
    properties = {name: all_properties[name] for name in names}
    if tool_name == "qweather_indices":
        properties["days"] = {"type": "string", "enum": ["1d", "3d"]}
    if tool_name == "qweather_solar_radiation":
        properties["hours"] = {"type": "integer", "minimum": 1, "maximum": 60}
    return {
        "type": "object",
        "properties": properties,
        "required": ["operation"],
        "additionalProperties": False,
    }


def _paginate_payload(
    endpoint_name: str,
    payload: Mapping[str, Any],
    *,
    offset: int,
    limit: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    output = copy.deepcopy(dict(payload))
    lists = []
    for path in _LIST_PATHS.get(endpoint_name, ()):
        value = output.get(path)
        if not isinstance(value, list):
            continue
        total = len(value)
        page = value[offset : offset + limit]
        output[path] = page
        lists.append(
            {
                "path": path,
                "total": total,
                "returned": len(page),
                "has_more": offset + len(page) < total,
            }
        )
    return output, {"offset": offset, "limit": limit, "lists": lists}


def _extract_attributions(payload: Mapping[str, Any]) -> list[Any]:
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping) and isinstance(metadata.get("attributions"), list):
        return copy.deepcopy(metadata["attributions"])
    refer = payload.get("refer")
    if isinstance(refer, Mapping) and isinstance(refer.get("sources"), list):
        return copy.deepcopy(refer["sources"])
    return []


def _raise_for_qweather_error(status: int, payload: Mapping[str, Any]) -> None:
    if 200 <= status < 300:
        legacy_code = str(payload.get("code", "200"))
        if legacy_code == "200":
            return
        status = int(legacy_code) if legacy_code.isdigit() else 500
    code, message = _map_error(status)
    raise ToolExecutionError(code, message)


def _map_error(status: int) -> tuple[str, str]:
    if status == 401:
        return "qweather_unauthorized", "和风天气鉴权失败，请检查 JWT 配置。"
    if status in {402, 403}:
        return "qweather_forbidden", "当前和风天气凭据无权访问该数据。"
    if status == 429:
        return "qweather_rate_limited", "和风天气请求已达到频率或额度限制。"
    if status in {204, 400, 404, 422}:
        return "qweather_invalid_request", "和风天气拒绝了查询参数或没有匹配数据。"
    return "qweather_unavailable", "和风天气服务暂时不可用。"


def _normalize_api_host(raw: str) -> str:
    candidate = raw.strip()
    try:
        parsed = urlsplit(candidate if "://" in candidate else f"https://{candidate}")
        port = parsed.port
    except ValueError as exc:
        raise QWeatherConfigError(
            "QWEATHER_API_HOST 必须是专属 HTTPS API Host。"
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise QWeatherConfigError("QWEATHER_API_HOST 必须是专属 HTTPS API Host。")
    host = (parsed.hostname or "").lower()
    if _HOST_PATTERN.fullmatch(host) is None:
        raise QWeatherConfigError(
            "QWEATHER_API_HOST 必须是账户专属的 *.qweatherapi.com 主机。"
        )
    return host


def _validate_credential_identifier(value: str, label: str) -> str:
    if _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise QWeatherConfigError(f"QWeather {label} 格式无效。")
    return value


def _invalid(message: str) -> None:
    raise ToolExecutionError("qweather_invalid_request", message)


def _required_text(arguments: Mapping[str, Any], name: str, maximum: int) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        _invalid(f"{name} 必须是 1 到 {maximum} 个字符的字符串。")
    return value.strip()


def _required_identifier(arguments: Mapping[str, Any], name: str) -> str:
    value = _required_text(arguments, name, 64)
    if _LOCATION_ID_PATTERN.fullmatch(value) is None:
        _invalid(f"{name} 格式无效。")
    return value


def _required_location_id(arguments: Mapping[str, Any]) -> str:
    return _required_identifier(arguments, "location_id")


def _required_enum(
    arguments: Mapping[str, Any],
    name: str,
    choices: tuple[Any, ...],
) -> Any:
    value = arguments.get(name)
    if value not in choices:
        _invalid(f"{name} 不是支持的枚举值。")
    return value


def _required_int(
    arguments: Mapping[str, Any],
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    value = arguments.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _invalid(f"{name} 必须是 {minimum} 到 {maximum} 的整数。")
    return value


def _optional_int(
    arguments: Mapping[str, Any],
    name: str,
    params: dict[str, str | int],
    api_name: str,
    minimum: int,
    maximum: int,
) -> None:
    if name in arguments:
        params[api_name] = _required_int(arguments, name, minimum, maximum)


def _coordinate(arguments: Mapping[str, Any], name: str, minimum: int, maximum: int) -> str:
    value = arguments.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid(f"{name} 必须是数字。")
    if not minimum <= float(value) <= maximum:
        _invalid(f"{name} 超出有效范围。")
    try:
        decimal = Decimal(str(value))
    except InvalidOperation:
        _invalid(f"{name} 格式无效。")
    if decimal.as_tuple().exponent < -2:
        _invalid(f"{name} 最多保留两位小数。")
    return format(decimal, "f")


def _put_coordinates(
    arguments: Mapping[str, Any],
    path_values: dict[str, str],
) -> None:
    path_values["latitude"] = _coordinate(arguments, "latitude", -90, 90)
    path_values["longitude"] = _coordinate(arguments, "longitude", -180, 180)


def _coordinate_pair(arguments: Mapping[str, Any]) -> str:
    longitude = _coordinate(arguments, "longitude", -180, 180)
    latitude = _coordinate(arguments, "latitude", -90, 90)
    return f"{longitude},{latitude}"


def _optional_bool(
    arguments: Mapping[str, Any],
    name: str,
    params: dict[str, str | int],
    api_name: str,
) -> None:
    if name in arguments:
        value = arguments[name]
        if not isinstance(value, bool):
            _invalid(f"{name} 必须是布尔值。")
        params[api_name] = "true" if value else "false"


def _optional_language(
    arguments: Mapping[str, Any],
    params: dict[str, str | int],
) -> None:
    if "language" in arguments:
        params["lang"] = _required_enum(arguments, "language", _LANGUAGES)


def _required_date(arguments: Mapping[str, Any]) -> date:
    value = arguments.get("date")
    if not isinstance(value, str) or _DATE_PATTERN.fullmatch(value) is None:
        _invalid("date 必须是 YYYYMMDD 格式。")
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        _invalid("date 不是有效日期。")


def _required_time(arguments: Mapping[str, Any]) -> str:
    value = arguments.get("time")
    if not isinstance(value, str) or _TIME_PATTERN.fullmatch(value) is None:
        _invalid("time 必须是 HHmm 格式。")
    try:
        datetime.strptime(value, "%H%M")
    except ValueError:
        _invalid("time 不是有效时间。")
    return value


def _required_timezone(arguments: Mapping[str, Any]) -> str:
    value = arguments.get("timezone")
    if not isinstance(value, str) or _TIMEZONE_PATTERN.fullmatch(value) is None:
        _invalid("timezone 必须是 HHmm、+HHmm 或 -HHmm 格式。")
    digits = value[1:] if value[0] in "+-" else value
    hours = int(digits[:2])
    minutes = int(digits[2:])
    if hours > 14 or minutes > 59 or (hours == 14 and minutes != 0):
        _invalid("timezone 超出有效 UTC 偏移范围。")
    return value[1:] if value.startswith("+") else value
