from __future__ import annotations

import asyncio
import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from jianer.adapters import ConversationKey, ConversationKind

from plugins.JianerAI.agent import AgentRunner
from plugins.JianerAI.providers import AssistantTurn, ProviderResponse
from plugins.JianerAI.tools import (
    BrowserManager,
    BrowserOptions,
    ToolCall,
    ToolContext,
    ToolRegistry,
    ToolRisk,
    validate_public_http_url,
    web_browser_tool,
)


class _SiteHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path == "/":
            self._html(
                """
                <html><head><title>Browser Test</title></head><body>
                <h1>Test form</h1>
                <script src="/blocked.js"></script>
                <form method="post" action="/submit">
                  <input name="password" type="password" placeholder="Password">
                  <select name="color"><option>Blue</option><option>Green</option></select>
                  <button type="submit">Sign in</button>
                  <input name="upload" type="file">
                </form>
                <a href="/download">Download</a>
                </body></html>
                """
            )
        elif self.path == "/cookie":
            self._html(f"<html><body>{self.headers.get('Cookie', '')}</body></html>")
        elif self.path == "/storage-set":
            self._html(
                "<html><body>stored<script>"
                "localStorage.setItem('shared-state', 'yes')"
                "</script></body></html>"
            )
        elif self.path == "/storage-read":
            self._html(
                "<html><body><script>"
                "document.body.append(localStorage.getItem('shared-state') || 'missing')"
                "</script></body></html>"
            )
        elif self.path == "/nav1":
            self._html(
                '<html><head><title>Nav One</title></head><body>'
                '<a href="/nav2">Next page</a></body></html>'
            )
        elif self.path == "/nav2":
            self._html(
                "<html><head><title>Nav Two</title></head><body>second page</body></html>"
            )
        elif self.path == "/redirect":
            port = self.server.server_address[1]
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{port}/blocked")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/ws":
            self._html(
                """
                <html><body>websocket page<script>
                const ws = new WebSocket('ws://127.0.0.1:9/socket');
                ws.onerror = () => document.body.append(' websocket blocked');
                </script></body></html>
                """
            )
        elif self.path == "/download":
            payload = b"not downloadable"
            self.send_response(200)
            self.send_header("Content-Disposition", "attachment; filename=test.txt")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        fields = parse_qs(self.rfile.read(length).decode("utf-8"))
        reflected = html.escape(fields.get("password", [""])[0])
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Set-Cookie", "logged=yes; Path=/; Max-Age=3600")
        payload = (
            "<html><head><title>Submitted</title></head><body>"
            f"submitted {reflected}</body></html>"
        ).encode("utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _html(self, text: str):
        payload = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        return


@pytest.fixture
def browser_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _context(conversation_id: str = "group-1") -> ToolContext:
    return ToolContext(
        event=SimpleNamespace(user_id="user-1"),
        actions=SimpleNamespace(protocol="onebot", capabilities=frozenset()),
        conversation=ConversationKey(
            protocol="onebot",
            self_id="bot-1",
            kind=ConversationKind.GROUP,
            conversation_id=conversation_id,
            preset="Normal",
        ),
        canonical_user_id="qq:user-1",
        runtime={},
        memory=SimpleNamespace(),
    )


def _data(result):
    return json.loads(result.content)["data"]


def _ref(snapshot, *, role=None, input_type=None, name=None):
    for element in snapshot["elements"]:
        if role is not None and element.get("role") != role:
            continue
        if input_type is not None and element.get("type") != input_type:
            continue
        if name is not None and element.get("name") != name:
            continue
        return element["ref"]
    raise AssertionError("element was not present in browser snapshot")


def test_web_browser_interacts_with_form_shares_cookie_and_redacts_audit(
    tmp_path: Path, browser_site: str
):
    async def scenario():
        seen_urls = []

        async def local_policy(url: str):
            seen_urls.append(url)
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"}:
                raise ValueError("blocked scheme")
            if parsed.path.startswith("/blocked") or parsed.hostname == "localhost":
                raise ValueError("blocked test target")

        options = BrowserOptions(
            profile_dir=tmp_path / "profile",
            audit_path=tmp_path / "audit.jsonl",
            max_pages=2,
            idle_seconds=10,
        )
        manager = BrowserManager(options, url_validator=local_policy)
        spec = web_browser_tool(manager=manager)
        assert spec.risk is ToolRisk.PRIVILEGED
        registry = ToolRegistry(
            allowed_risks=frozenset({ToolRisk.READ_ONLY, ToolRisk.PRIVILEGED})
        )
        registry.register(spec)
        first = _context("group-1")
        second = _context("group-2")

        opened = _data(
            await registry.execute(
                ToolCall("open", "web_browser", {"action": "open", "url": browser_site}),
                first,
            )
        )
        assert opened["title"] == "Browser Test"
        assert opened["blocked_requests"] >= 1
        assert all(element.get("type") != "file" for element in opened["elements"])
        reloaded = _data(
            await registry.execute(
                ToolCall("reload", "web_browser", {"action": "reload"}), first
            )
        )
        assert reloaded["title"] == "Browser Test"
        selected = _data(
            await registry.execute(
                ToolCall(
                    "select",
                    "web_browser",
                    {
                        "action": "select",
                        "element_ref": _ref(reloaded, role="select"),
                        "value": "Green",
                    },
                ),
                first,
            )
        )
        scrolled = _data(
            await registry.execute(
                ToolCall(
                    "scroll",
                    "web_browser",
                    {"action": "scroll", "direction": "down", "amount": 300},
                ),
                first,
            )
        )
        waited = _data(
            await registry.execute(
                ToolCall(
                    "wait",
                    "web_browser",
                    {"action": "wait", "wait_seconds": 0.01},
                ),
                first,
            )
        )
        assert scrolled["title"] == waited["title"] == "Browser Test"
        password_ref = _ref(waited, input_type="password")

        secret = "never-store-this-password"
        filled_result = await registry.execute(
            ToolCall(
                "fill",
                "web_browser",
                {
                    "action": "fill",
                    "element_ref": password_ref,
                    "value": secret,
                },
            ),
            first,
        )
        filled = _data(filled_result)
        assert secret not in filled_result.content
        assert secret in first.sensitive_values

        pressed = _data(
            await registry.execute(
                ToolCall(
                    "press",
                    "web_browser",
                    {
                        "action": "press",
                        "element_ref": _ref(filled, input_type="password"),
                        "key": "Tab",
                    },
                ),
                first,
            )
        )

        stale = await registry.execute(
            ToolCall(
                "stale",
                "web_browser",
                {"action": "click", "element_ref": password_ref},
            ),
            first,
        )
        assert stale.error_code == "stale_element_ref"

        submit_ref = _ref(pressed, role="button", name="Sign in")
        submitted = _data(
            await registry.execute(
                ToolCall(
                    "submit",
                    "web_browser",
                    {"action": "click", "element_ref": submit_ref},
                ),
                first,
            )
        )
        assert submitted["title"] == "Submitted"
        assert "submitted" in submitted["text"]
        assert secret not in json.dumps(submitted, ensure_ascii=False)
        assert "[REDACTED]" in submitted["text"]

        cookie_page = _data(
            await registry.execute(
                ToolCall(
                    "cookie",
                    "web_browser",
                    {"action": "open", "url": f"{browser_site}/cookie"},
                ),
                second,
            )
        )
        assert "logged=yes" in cookie_page["text"]
        await registry.execute(
            ToolCall(
                "storage-set",
                "web_browser",
                {"action": "open", "url": f"{browser_site}/storage-set"},
            ),
            second,
        )
        still_first = _data(
            await registry.execute(
                ToolCall("snapshot", "web_browser", {"action": "snapshot"}),
                first,
            )
        )
        assert still_first["title"] == "Submitted"

        nav_one = _data(
            await registry.execute(
                ToolCall(
                    "nav-one",
                    "web_browser",
                    {"action": "open", "url": f"{browser_site}/nav1"},
                ),
                first,
            )
        )
        nav_two = _data(
            await registry.execute(
                ToolCall(
                    "nav-two",
                    "web_browser",
                    {
                        "action": "click",
                        "element_ref": _ref(nav_one, role="link", name="Next page"),
                    },
                ),
                first,
            )
        )
        assert nav_two["title"] == "Nav Two"
        back = _data(
            await registry.execute(
                ToolCall("back", "web_browser", {"action": "back"}), first
            )
        )
        assert back["title"] == "Nav One"
        forward = _data(
            await registry.execute(
                ToolCall("forward", "web_browser", {"action": "forward"}), first
            )
        )
        assert forward["title"] == "Nav Two"

        await registry.execute(
            ToolCall(
                "websocket",
                "web_browser",
                {"action": "open", "url": f"{browser_site}/ws"},
            ),
            first,
        )
        websocket_wait = _data(
            await registry.execute(
                ToolCall(
                    "websocket-wait",
                    "web_browser",
                    {"action": "wait", "wait_seconds": 0.05},
                ),
                first,
            )
        )
        assert websocket_wait["blocked_websockets"] >= 1

        download_page = _data(
            await registry.execute(
                ToolCall(
                    "download-page",
                    "web_browser",
                    {"action": "open", "url": browser_site},
                ),
                first,
            )
        )
        download_result = _data(
            await registry.execute(
                ToolCall(
                    "download",
                    "web_browser",
                    {
                        "action": "click",
                        "element_ref": _ref(
                            download_page, role="link", name="Download"
                        ),
                    },
                ),
                first,
            )
        )
        assert download_result["blocked_downloads"] >= 1

        redirected = await registry.execute(
            ToolCall(
                "redirect",
                "web_browser",
                {"action": "open", "url": f"{browser_site}/redirect"},
            ),
            second,
        )
        assert redirected.error_code == "browser_url_blocked"
        assert any(url.endswith("/blocked") for url in seen_urls)

        audit = options.audit_path.read_text(encoding="utf-8")
        rows = [json.loads(line) for line in audit.splitlines()]
        assert rows[-1]["method"] == "POST"
        assert rows[-1]["result"] == 200
        assert rows[-1]["canonical_user"] == "qq:user-1"
        assert secret not in audit
        assert "Cookie" not in audit
        assert "?" not in json.dumps(rows, ensure_ascii=False)

        closed = _data(
            await registry.execute(
                ToolCall("close", "web_browser", {"action": "close"}), first
            )
        )
        assert closed == {"action": "close", "closed": True}
        after_close = await registry.execute(
            ToolCall("closed-page", "web_browser", {"action": "snapshot"}), first
        )
        assert after_close.error_code == "browser_page_not_open"

        await registry.shutdown()
        assert manager._context is None

        restarted_manager = BrowserManager(options, url_validator=local_policy)
        restarted_registry = ToolRegistry(
            allowed_risks=frozenset({ToolRisk.READ_ONLY, ToolRisk.PRIVILEGED})
        )
        restarted_registry.register(
            web_browser_tool(manager=restarted_manager)
        )
        persisted = _data(
            await restarted_registry.execute(
                ToolCall(
                    "persisted-cookie",
                    "web_browser",
                    {"action": "open", "url": f"{browser_site}/cookie"},
                ),
                _context("after-restart"),
            )
        )
        assert "logged=yes" in persisted["text"]
        storage = _data(
            await restarted_registry.execute(
                ToolCall(
                    "persisted-storage",
                    "web_browser",
                    {"action": "open", "url": f"{browser_site}/storage-read"},
                ),
                _context("after-restart"),
            )
        )
        assert "yes" in storage["text"]
        await restarted_registry.shutdown()

    asyncio.run(scenario())


def test_web_browser_privileged_exposure_page_limit_idle_and_shutdown(
    tmp_path: Path, browser_site: str
):
    async def scenario():
        async def local_policy(url: str):
            if urlsplit(url).scheme not in {"http", "https"}:
                raise ValueError("blocked")

        options = BrowserOptions(
            profile_dir=tmp_path / "profile",
            audit_path=tmp_path / "audit.jsonl",
            max_pages=1,
            idle_seconds=0.1,
        )
        manager = BrowserManager(options, url_validator=local_policy)
        spec = web_browser_tool(manager=manager)
        denied_registry = ToolRegistry()
        denied_registry.register(spec)
        assert "web_browser" not in {
            item.name for item in denied_registry.available(_context())
        }

        registry = ToolRegistry(
            allowed_risks=frozenset({ToolRisk.READ_ONLY, ToolRisk.PRIVILEGED})
        )
        registry.register(web_browser_tool(manager=manager))
        first = _context("first")
        second = _context("second")
        await registry.execute(
            ToolCall("one", "web_browser", {"action": "open", "url": browser_site}),
            first,
        )
        await registry.execute(
            ToolCall("two", "web_browser", {"action": "open", "url": browser_site}),
            second,
        )
        evicted = await registry.execute(
            ToolCall("old", "web_browser", {"action": "snapshot"}), first
        )
        assert evicted.error_code == "browser_page_not_open"

        await asyncio.sleep(1.1)
        idle = await registry.execute(
            ToolCall("idle", "web_browser", {"action": "snapshot"}), second
        )
        assert idle.error_code == "browser_page_not_open"
        await registry.shutdown()
        closed = await registry.execute(
            ToolCall("closed", "web_browser", {"action": "snapshot"}), second
        )
        assert closed.error_code == "registry_closed"

    asyncio.run(scenario())


def test_production_browser_url_policy_blocks_local_and_unsafe_urls():
    async def scenario():
        for url in (
            "http://127.0.0.1/",
            "http://[::1]/",
            "http://localhost/",
            "http://user:password@example.com/",
            "file:///etc/passwd",
            "ftp://example.com/file",
            "http://192.168.1.1/",
            "http://169.254.169.254/latest/meta-data/",
        ):
            with pytest.raises(ValueError):
                await validate_public_http_url(url)

    asyncio.run(scenario())


def test_web_browser_requires_explicit_agent_allowlist_entry(tmp_path: Path):
    class Provider:
        def __init__(self):
            self.requests = []
            self.chat_calls = []

        def supports_tools(self, model):
            return True

        async def complete_request(self, model, request):
            self.requests.append(request)
            return ProviderResponse("done", (), AssistantTurn(text="done"))

        async def chat(self, model, message, **kwargs):
            self.chat_calls.append(message)
            return "plain"

    async def scenario():
        manager = BrowserManager(
            BrowserOptions(
                profile_dir=tmp_path / "profile",
                audit_path=tmp_path / "audit.jsonl",
            )
        )
        registry = ToolRegistry(
            allowed_risks=frozenset({ToolRisk.READ_ONLY, ToolRisk.PRIVILEGED})
        )
        registry.register(web_browser_tool(manager=manager))
        denied_provider = Provider()
        denied = AgentRunner(
            denied_provider,
            registry,
            allowed_tool_names=frozenset(),
        )
        assert await denied.run(
            model="model",
            message="open a page",
            history=(),
            system_prompt="",
            attachments=(),
            context=_context(),
            enabled=True,
        ) == "plain"
        assert denied_provider.requests == []

        allowed_provider = Provider()
        allowed = AgentRunner(
            allowed_provider,
            registry,
            allowed_tool_names=frozenset({"web_browser"}),
        )
        assert await allowed.run(
            model="model",
            message="open a page",
            history=(),
            system_prompt="",
            attachments=(),
            context=_context(),
            enabled=True,
        ) == "done"
        assert [tool.name for tool in allowed_provider.requests[0].tools] == [
            "web_browser"
        ]
        await registry.shutdown()

    asyncio.run(scenario())
