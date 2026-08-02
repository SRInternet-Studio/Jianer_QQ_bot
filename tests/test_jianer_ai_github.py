from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from jianer.adapters import ConversationKey, ConversationKind

from plugins.JianerAI.agent import AgentRunner
from plugins.JianerAI.providers import (
    AssistantTurn,
    ProviderResponse,
    ProviderToolCall,
)
from plugins.JianerAI.tools import (
    ToolCall,
    ToolContext,
    ToolExecutionError,
    ToolRegistry,
    ToolRisk,
    ToolSpec,
    register_builtin_tools,
)
from plugins.JianerAI.tools import github_repository as github_module
from plugins.JianerAI.tools.github_repository import github_repository_tool


def _context() -> ToolContext:
    return ToolContext(
        event=SimpleNamespace(user_id="user-1", self_id="bot-1"),
        actions=SimpleNamespace(protocol="onebot", capabilities=frozenset()),
        conversation=ConversationKey(
            protocol="onebot",
            self_id="bot-1",
            kind=ConversationKind.PRIVATE,
            conversation_id="user-1",
            preset="Normal",
        ),
        canonical_user_id="qq:user-1",
        runtime={},
        memory=SimpleNamespace(),
    )


def _decoded(result):
    return json.loads(result.content)


def _install_transport(monkeypatch, handler):
    real_client = httpx.Client
    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(github_module.httpx, "Client", client_factory)


async def _execute(registry: ToolRegistry, arguments, call_id="github-call"):
    return await registry.execute(
        ToolCall(call_id, "github_repository", arguments),
        _context(),
    )


def _repository_payload():
    return {
        "full_name": "acme/demo",
        "description": "Demo repository",
        "default_branch": "main",
        "language": "Python",
        "topics": ["agent", "bot"],
        "visibility": "public",
        "archived": False,
        "fork": False,
        "stargazers_count": 42,
        "forks_count": 7,
        "open_issues_count": 3,
        "license": {"spdx_id": "MIT"},
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
        "pushed_at": "2026-08-01T00:00:00Z",
        "html_url": "https://github.com/acme/demo",
    }


def _commit_payload(sha="a" * 40):
    return {
        "sha": sha,
        "html_url": f"https://github.com/acme/demo/commit/{sha}",
        "commit": {
            "message": "Add GitHub reader",
            "author": {"name": "Alice", "date": "2026-08-01T00:00:00Z"},
            "verification": {"verified": True},
        },
        "author": {"login": "alice"},
        "parents": [{"sha": "b" * 40}],
        "stats": {"additions": 10, "deletions": 2, "total": 12},
        "files": [
            {
                "filename": "src/tool.py",
                "status": "modified",
                "additions": 10,
                "deletions": 2,
                "changes": 12,
                "patch": "@@ -1 +1 @@\n-old\n+new",
                "blob_url": f"https://github.com/acme/demo/blob/{sha}/src/tool.py",
            }
        ],
    }


def _pull_payload(number=12):
    return {
        "number": number,
        "title": "Add reader",
        "state": "open",
        "draft": False,
        "user": {"login": "alice"},
        "head": {"ref": "feature"},
        "base": {"ref": "main"},
        "body": "PR body",
        "created_at": "2026-07-30T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
        "closed_at": None,
        "merged_at": None,
        "merged": False,
        "mergeable": True,
        "merge_commit_sha": None,
        "commits": 2,
        "changed_files": 1,
        "additions": 10,
        "deletions": 2,
        "html_url": f"https://github.com/acme/demo/pull/{number}",
    }


def _issue_payload(number=9):
    return {
        "number": number,
        "title": "Reader bug",
        "state": "open",
        "user": {"login": "bob"},
        "labels": [{"name": "bug"}],
        "comments": 4,
        "body": "Issue body",
        "assignees": [{"login": "alice"}],
        "milestone": {"title": "v1"},
        "created_at": "2026-07-30T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
        "closed_at": None,
        "html_url": f"https://github.com/acme/demo/issues/{number}",
    }


def test_github_tool_registration_and_safe_error_contract(monkeypatch):
    async def scenario():
        registry = ToolRegistry()
        register_builtin_tools(registry)
        try:
            specs = {item.name: item for item in registry.available(_context())}
            spec = specs["github_repository"]
            assert spec.risk is ToolRisk.READ_ONLY
            assert spec.timeout_seconds == 15.0
            assert spec.max_output_chars == 24000
            assert "GITHUB_TOKEN" in spec.description
            assert "除非用户明确要求" in spec.description

            registry.register(
                ToolSpec(
                    name="safe_failure",
                    description="returns a safe failure",
                    input_schema={
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                    handler=lambda *_: (_ for _ in ()).throw(
                        ToolExecutionError("safe_code", "安全错误。")
                    ),
                )
            )
            failure = await registry.execute(
                ToolCall("safe", "safe_failure", {}), _context()
            )
            assert failure.error_code == "safe_code"
            assert _decoded(failure)["message"] == "安全错误。"
        finally:
            await registry.shutdown()

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    asyncio.run(scenario())


@pytest.mark.parametrize(
    "arguments,error_code",
    [
        (
            {"action": "get_repository", "repository": "https://github.com/acme/demo"},
            "github_invalid_repository",
        ),
        (
            {
                "action": "read_file",
                "repository": "acme/demo",
                "path": "../secret",
            },
            "github_invalid_path",
        ),
        (
            {
                "action": "read_file",
                "repository": "acme/demo",
                "path": "src\\tool.py",
            },
            "github_invalid_path",
        ),
        (
            {
                "action": "search_code",
                "repository": "acme/demo",
                "query": "PluginManager repo:other/project",
            },
            "github_invalid_query",
        ),
        (
            {
                "action": "list_directory",
                "repository": "acme/demo",
                "ref": "main\nother",
            },
            "github_invalid_ref",
        ),
    ],
)
def test_github_tool_rejects_untrusted_scope_and_paths(
    monkeypatch, arguments, error_code
):
    def handler(request):
        raise AssertionError(f"network should not be reached: {request.url}")

    _install_transport(monkeypatch, handler)
    monkeypatch.setenv("GITHUB_TOKEN", "host-secret")

    async def scenario():
        registry = ToolRegistry()
        registry.register(github_repository_tool())
        try:
            result = await _execute(registry, arguments)
            assert result.ok is False
            assert result.error_code == error_code
            assert "host-secret" not in result.content
        finally:
            await registry.shutdown()

    asyncio.run(scenario())


def test_github_search_requires_host_token_without_network(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def handler(request):
        raise AssertionError("code search must not run without a token")

    _install_transport(monkeypatch, handler)

    async def scenario():
        registry = ToolRegistry()
        registry.register(github_repository_tool())
        try:
            result = await _execute(
                registry,
                {
                    "action": "search_code",
                    "repository": "acme/demo",
                    "query": "PluginManager",
                },
            )
            assert result.error_code == "github_token_required"
        finally:
            await registry.shutdown()

    asyncio.run(scenario())


def test_github_repository_directory_file_and_search(monkeypatch):
    requests = []
    file_content = "first line\nsecond line\nthird line\n"

    def handler(request: httpx.Request):
        requests.append(request)
        assert request.headers["x-github-api-version"] == "2026-03-10"
        assert request.headers["authorization"] == "Bearer host-secret"
        assert request.headers["user-agent"].startswith("JianerAI-GitHub-Reader/")
        path = request.url.path
        if path == "/repos/acme/demo":
            return httpx.Response(200, json=_repository_payload())
        if path == "/repos/acme/demo/contents":
            assert request.url.params["ref"] == "main"
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "tool.py",
                        "path": "src/tool.py",
                        "type": "file",
                        "size": 32,
                        "sha": "a" * 40,
                        "html_url": "https://github.com/acme/demo/blob/main/src/tool.py",
                    },
                    {
                        "name": "src",
                        "path": "src",
                        "type": "dir",
                        "size": 0,
                        "sha": "b" * 40,
                        "html_url": "https://github.com/acme/demo/tree/main/src",
                    },
                ],
            )
        if path == "/repos/acme/demo/contents/src/tool.py":
            assert request.url.params["ref"] == "main"
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "path": "src/tool.py",
                    "size": len(file_content.encode()),
                    "encoding": "base64",
                    "content": base64.b64encode(file_content.encode()).decode(),
                    "sha": "a" * 40,
                    "html_url": "https://github.com/acme/demo/blob/main/src/tool.py",
                },
            )
        if path == "/search/code":
            assert request.url.params["q"] == "PluginManager repo:acme/demo"
            assert request.url.params["per_page"] == "5"
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "incomplete_results": False,
                    "items": [
                        {
                            "name": "tool.py",
                            "path": "src/tool.py",
                            "sha": "a" * 40,
                            "html_url": "https://github.com/acme/demo/blob/main/src/tool.py",
                            "repository": {"full_name": "acme/demo"},
                        },
                        {
                            "name": "leak.py",
                            "path": "leak.py",
                            "sha": "c" * 40,
                            "html_url": "https://github.com/other/repo/blob/main/leak.py",
                            "repository": {"full_name": "other/repo"},
                        },
                    ],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    _install_transport(monkeypatch, handler)
    monkeypatch.setenv("GITHUB_TOKEN", "host-secret")

    async def scenario():
        registry = ToolRegistry()
        registry.register(github_repository_tool())
        try:
            repo = await _execute(
                registry, {"action": "get_repository", "repository": "acme/demo"}
            )
            directory = await _execute(
                registry,
                {
                    "action": "list_directory",
                    "repository": "acme/demo",
                    "ref": "main",
                },
            )
            file_result = await _execute(
                registry,
                {
                    "action": "read_file",
                    "repository": "acme/demo",
                    "path": "src/tool.py",
                    "ref": "main",
                    "start_line": 2,
                    "line_count": 1,
                },
            )
            search = await _execute(
                registry,
                {
                    "action": "search_code",
                    "repository": "acme/demo",
                    "query": "PluginManager",
                    "limit": 5,
                },
            )
        finally:
            await registry.shutdown()

        repo_data = _decoded(repo)["data"]
        assert repo_data["action"] == "get_repository"
        assert repo_data["result"]["stars"] == 42
        directory_data = _decoded(directory)["data"]
        assert [item["type"] for item in directory_data["result"]["entries"]] == [
            "dir",
            "file",
        ]
        file_data = _decoded(file_result)["data"]
        assert file_data["result"]["content"] == "2: second line"
        assert file_data["truncated"] is True
        search_data = _decoded(search)["data"]
        assert [item["path"] for item in search_data["result"]["matches"]] == [
            "src/tool.py"
        ]
        assert "host-secret" not in json.dumps(search_data)

    asyncio.run(scenario())
    assert len(requests) == 4


def test_github_anonymous_directory_result_is_bounded(monkeypatch):
    def handler(request):
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json=[
                {
                    "name": f"file-{index}.py",
                    "path": f"src/file-{index}.py",
                    "type": "file",
                    "size": index,
                    "sha": f"{index:040x}",
                    "html_url": f"https://github.com/acme/demo/blob/main/src/file-{index}.py",
                }
                for index in range(101)
            ],
        )

    _install_transport(monkeypatch, handler)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    async def scenario():
        registry = ToolRegistry()
        registry.register(github_repository_tool())
        try:
            result = await _execute(
                registry,
                {"action": "list_directory", "repository": "acme/demo"},
            )
            payload = _decoded(result)["data"]
            assert payload["truncated"] is True
            assert 1 <= payload["result"]["entry_count"] <= 100
            assert len(payload["result"]["entries"]) == payload["result"][
                "entry_count"
            ]
        finally:
            await registry.shutdown()

    asyncio.run(scenario())


def test_github_commit_pull_request_and_issue_actions(monkeypatch):
    sha = "a" * 40
    pull = _pull_payload()
    issue = _issue_payload()

    def handler(request: httpx.Request):
        path = request.url.path
        if path == "/repos/acme/demo/commits":
            assert request.url.params["sha"] == "main"
            assert request.url.params["path"] == "src"
            return httpx.Response(200, json=[_commit_payload(sha)])
        if path == f"/repos/acme/demo/commits/{sha}":
            return httpx.Response(200, json=_commit_payload(sha))
        if path == "/repos/acme/demo/pulls":
            assert request.url.params["state"] == "all"
            return httpx.Response(200, json=[pull])
        if path == "/repos/acme/demo/pulls/12":
            return httpx.Response(200, json=pull)
        if path == "/repos/acme/demo/pulls/12/files":
            return httpx.Response(200, json=_commit_payload(sha)["files"])
        if path == "/repos/acme/demo/issues":
            issue_as_pr = dict(_issue_payload(12), pull_request={"url": "hidden"})
            return httpx.Response(200, json=[issue_as_pr, issue])
        if path == "/repos/acme/demo/issues/9":
            return httpx.Response(200, json=issue)
        raise AssertionError(f"unexpected request: {request.url}")

    _install_transport(monkeypatch, handler)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    async def scenario():
        registry = ToolRegistry()
        registry.register(github_repository_tool())
        try:
            list_commits = await _execute(
                registry,
                {
                    "action": "list_commits",
                    "repository": "acme/demo",
                    "ref": "main",
                    "path": "src",
                },
            )
            commit = await _execute(
                registry,
                {
                    "action": "get_commit",
                    "repository": "acme/demo",
                    "commit_sha": sha,
                },
            )
            list_pulls = await _execute(
                registry,
                {
                    "action": "list_pull_requests",
                    "repository": "acme/demo",
                    "state": "all",
                },
            )
            pull_detail = await _execute(
                registry,
                {
                    "action": "get_pull_request",
                    "repository": "acme/demo",
                    "number": 12,
                },
            )
            list_issues = await _execute(
                registry,
                {
                    "action": "list_issues",
                    "repository": "acme/demo",
                    "state": "open",
                },
            )
            issue_detail = await _execute(
                registry,
                {
                    "action": "get_issue",
                    "repository": "acme/demo",
                    "number": 9,
                },
            )
        finally:
            await registry.shutdown()

        assert _decoded(list_commits)["data"]["result"]["commits"][0]["sha"] == sha
        commit_data = _decoded(commit)["data"]["result"]
        assert commit_data["stats"]["total"] == 12
        assert commit_data["files"][0]["patch"].startswith("@@")
        assert _decoded(list_pulls)["data"]["result"]["pull_requests"][0][
            "number"
        ] == 12
        pull_data = _decoded(pull_detail)["data"]["result"]
        assert pull_data["changed_files"] == 1
        assert pull_data["files"][0]["filename"] == "src/tool.py"
        issue_data = _decoded(list_issues)["data"]["result"]
        assert [item["number"] for item in issue_data["issues"]] == [9]
        assert _decoded(issue_detail)["data"]["result"]["body"] == "Issue body"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,headers,error_code",
    [
        (401, {}, "github_unauthorized"),
        (404, {}, "github_not_found"),
        (403, {"x-ratelimit-remaining": "0"}, "github_rate_limited"),
        (403, {"x-ratelimit-remaining": "10"}, "github_forbidden"),
        (429, {}, "github_rate_limited"),
        (503, {}, "github_unavailable"),
    ],
)
def test_github_http_failures_are_sanitized(
    monkeypatch, status, headers, error_code
):
    def handler(request):
        return httpx.Response(
            status,
            headers=headers,
            json={"message": "private upstream details host-secret"},
        )

    _install_transport(monkeypatch, handler)
    monkeypatch.setenv("GITHUB_TOKEN", "host-secret")

    async def scenario():
        registry = ToolRegistry()
        registry.register(github_repository_tool())
        try:
            result = await _execute(
                registry,
                {"action": "get_repository", "repository": "acme/demo"},
            )
            assert result.error_code == error_code
            assert "private upstream" not in result.content
            assert "host-secret" not in result.content
        finally:
            await registry.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload,error_code",
    [
        (
            {
                "type": "file",
                "size": 512 * 1024 + 1,
                "encoding": "base64",
                "content": "",
            },
            "github_file_too_large",
        ),
        (
            {
                "type": "file",
                "size": 3,
                "encoding": "base64",
                "content": base64.b64encode(b"a\x00b").decode(),
            },
            "github_binary_file",
        ),
        (
            {
                "type": "file",
                "size": 2,
                "encoding": "base64",
                "content": base64.b64encode(b"\xff\xfe").decode(),
            },
            "github_binary_file",
        ),
    ],
)
def test_github_file_size_and_binary_limits(monkeypatch, payload, error_code):
    def handler(request):
        return httpx.Response(200, json=payload)

    _install_transport(monkeypatch, handler)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    async def scenario():
        registry = ToolRegistry()
        registry.register(github_repository_tool())
        try:
            result = await _execute(
                registry,
                {
                    "action": "read_file",
                    "repository": "acme/demo",
                    "path": "artifact.bin",
                },
            )
            assert result.error_code == error_code
        finally:
            await registry.shutdown()

    asyncio.run(scenario())


def test_github_patch_and_body_are_truncated(monkeypatch):
    pull = _pull_payload()
    pull["body"] = "b" * 13000
    changed_file = _commit_payload()["files"][0]
    changed_file["patch"] = "p" * 5000

    def handler(request):
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=[changed_file])
        return httpx.Response(200, json=pull)

    _install_transport(monkeypatch, handler)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    async def scenario():
        registry = ToolRegistry()
        registry.register(github_repository_tool())
        try:
            result = await _execute(
                registry,
                {
                    "action": "get_pull_request",
                    "repository": "acme/demo",
                    "number": 12,
                },
            )
            payload = _decoded(result)["data"]
            assert payload["truncated"] is True
            assert len(payload["result"]["body"]) == 12000
            assert len(payload["result"]["files"][0]["patch"]) == 4000
        finally:
            await registry.shutdown()

    asyncio.run(scenario())


def test_agent_browses_github_then_reads_code_without_source_by_default(
    monkeypatch,
):
    file_content = "def answer():\n    return 42\n"

    def handler(request):
        if request.url.path == "/repos/acme/demo/contents":
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "answer.py",
                        "path": "answer.py",
                        "type": "file",
                        "size": len(file_content),
                        "sha": "a" * 40,
                        "html_url": "https://github.com/acme/demo/blob/main/answer.py",
                    }
                ],
            )
        if request.url.path == "/repos/acme/demo/contents/answer.py":
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "size": len(file_content),
                    "encoding": "base64",
                    "content": base64.b64encode(file_content.encode()).decode(),
                    "sha": "a" * 40,
                    "html_url": "https://github.com/acme/demo/blob/main/answer.py",
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    class Providers:
        def __init__(self):
            self.requests = []

        def supports_tools(self, model):
            return True

        async def complete_request(self, model, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                call = ProviderToolCall(
                    "github-list", "github_repository", {
                        "action": "list_directory",
                        "repository": "acme/demo",
                    }
                )
                return ProviderResponse("", (call,), AssistantTurn(tool_calls=(call,)))
            if len(self.requests) == 2:
                directory = json.loads(request.turns[-1].content)
                assert directory["data"]["result"]["entries"][0]["path"] == "answer.py"
                call = ProviderToolCall(
                    "github-read", "github_repository", {
                        "action": "read_file",
                        "repository": "acme/demo",
                        "path": "answer.py",
                    }
                )
                return ProviderResponse("", (call,), AssistantTurn(tool_calls=(call,)))
            file_result = json.loads(request.turns[-1].content)
            assert "return 42" in file_result["data"]["result"]["content"]
            answer = "这个函数返回 42。"
            return ProviderResponse(answer, (), AssistantTurn(text=answer))

        async def chat(self, *args, **kwargs):
            raise AssertionError("Agent should use native tools")

    _install_transport(monkeypatch, handler)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    async def scenario():
        registry = ToolRegistry()
        registry.register(github_repository_tool())
        providers = Providers()
        try:
            answer = await AgentRunner(providers, registry).run(
                model="tool-model",
                message="查看 acme/demo 的 answer 函数做了什么",
                history=(),
                system_prompt=(
                    "除非用户明确要求来源，否则最终回答不要展示来源或 URL。"
                ),
                attachments=(),
                context=_context(),
                enabled=True,
            )
        finally:
            await registry.shutdown()
        assert answer == "这个函数返回 42。"
        assert "http" not in answer
        assert len(providers.requests) == 3

    asyncio.run(scenario())


def test_agent_can_return_actual_github_url_when_user_requests_source(monkeypatch):
    source_url = "https://github.com/acme/demo"

    def handler(request):
        return httpx.Response(200, json=_repository_payload())

    class Providers:
        def __init__(self):
            self.calls = 0

        def supports_tools(self, model):
            return True

        async def complete_request(self, model, request):
            self.calls += 1
            if self.calls == 1:
                call = ProviderToolCall(
                    "github-repo",
                    "github_repository",
                    {"action": "get_repository", "repository": "acme/demo"},
                )
                return ProviderResponse("", (call,), AssistantTurn(tool_calls=(call,)))
            result = json.loads(request.turns[-1].content)
            assert result["data"]["result"]["html_url"] == source_url
            answer = f"仓库使用 Python。来源：{source_url}"
            return ProviderResponse(answer, (), AssistantTurn(text=answer))

        async def chat(self, *args, **kwargs):
            raise AssertionError("Agent should use native tools")

    _install_transport(monkeypatch, handler)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    async def scenario():
        registry = ToolRegistry()
        registry.register(github_repository_tool())
        try:
            answer = await AgentRunner(Providers(), registry).run(
                model="tool-model",
                message="查看 acme/demo 并给我仓库来源链接",
                history=(),
                system_prompt="用户明确要求来源时只能返回工具提供的实际 URL。",
                attachments=(),
                context=_context(),
                enabled=True,
            )
        finally:
            await registry.shutdown()
        assert answer.endswith(source_url)

    asyncio.run(scenario())
