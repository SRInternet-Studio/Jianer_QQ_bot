from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

from plugins.JianerAI.service import RuntimeOptions
from plugins.JianerAI.tools.web_browser import BrowserManager, BrowserOptions


class _FakeRoute:
    def __init__(self) -> None:
        self.continued = False
        self.aborted = False

    async def continue_(self) -> None:
        self.continued = True

    async def abort(self, reason: str) -> None:
        self.aborted = True


class _FakeRequest:
    def __init__(
        self,
        url: str,
        *,
        method: str = "GET",
        resource_type: str = "media",
    ) -> None:
        self.url = url
        self.method = method
        self.resource_type = resource_type


class _FakeContext:
    def __init__(self) -> None:
        self.pages: tuple[Any, ...] = ()
        self.routes: list[tuple[str, Any]] = []
        self.websocket_routes: list[tuple[str, Any]] = []
        self.closed = False

    def set_default_timeout(self, value: float) -> None:
        self.default_timeout = value

    def set_default_navigation_timeout(self, value: float) -> None:
        self.default_navigation_timeout = value

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    async def route_web_socket(self, pattern: str, handler: Any) -> None:
        self.websocket_routes.append((pattern, handler))

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, context: _FakeContext, calls: list[tuple[str, dict]]) -> None:
        self._context = context
        self._calls = calls

    async def launch_persistent_context(
        self, profile: str, **kwargs: Any
    ) -> _FakeContext:
        self._calls.append((profile, kwargs))
        return self._context


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium

    async def stop(self) -> None:
        self.stopped = True


class _FakePlaywrightManager:
    def __init__(self, playwright: _FakePlaywright) -> None:
        self._playwright = playwright

    async def start(self) -> _FakePlaywright:
        return self._playwright


def test_browser_options_and_manager_have_no_url_policy() -> None:
    options = BrowserOptions()
    assert not hasattr(options, "allow_all_urls")
    manager = BrowserManager(options)
    for attribute in (
        "_validate_url",
        "_validate_or_raise",
        "_blocked_navigation",
        "_blocked_requests",
        "_blocked_websockets",
        "_blocked_downloads",
    ):
        assert not hasattr(manager, attribute)


def test_route_request_never_aborts_private_or_media_requests() -> None:
    manager = BrowserManager(BrowserOptions())
    route = _FakeRoute()
    request = _FakeRequest("http://127.0.0.1:8080/secret.mp4")
    asyncio.run(manager._route_request(route, request))
    assert route.continued is True
    assert route.aborted is False


def test_browser_context_disables_all_blocking_options(
    tmp_path: Any,
) -> None:
    context = _FakeContext()
    calls: list[tuple[str, dict]] = []
    playwright = _FakePlaywright(_FakeChromium(context, calls))
    module = ModuleType("playwright.async_api")
    module.async_playwright = lambda: _FakePlaywrightManager(playwright)  # type: ignore[attr-defined]
    original = sys.modules.get("playwright.async_api")
    sys.modules["playwright.async_api"] = module
    try:
        manager = BrowserManager(
            BrowserOptions(
                profile_dir=tmp_path / "profile",
                audit_path=tmp_path / "audit.jsonl",
            )
        )

        async def run() -> None:
            await manager._start_locked()
            await manager.shutdown()

        asyncio.run(run())
    finally:
        if original is None:
            sys.modules.pop("playwright.async_api", None)
        else:
            sys.modules["playwright.async_api"] = original
    _, kwargs = calls[0]
    assert kwargs == {"headless": True, "accept_downloads": True}
    assert [pattern for pattern, _ in context.routes] == ["**/*"]
    assert context.websocket_routes == []


def test_runtime_options_have_no_browser_url_policy() -> None:
    options = RuntimeOptions.from_runtime(
        {"config": SimpleNamespace(others={})}
    )
    assert not hasattr(options, "agent_browser_allow_all_urls")
