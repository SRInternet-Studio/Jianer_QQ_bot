from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from plugins.JianerAI.tools.contracts import (
    ToolContext,
    ToolExecutionError,
    ToolRisk,
    ToolSpec,
)


_API_BASE = "https://api.github.com"
_API_VERSION = "2026-03-10"
_ACTIONS = (
    "get_repository",
    "list_directory",
    "read_file",
    "search_code",
    "list_commits",
    "get_commit",
    "list_pull_requests",
    "get_pull_request",
    "list_issues",
    "get_issue",
)
_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SCOPE_QUALIFIER_PATTERN = re.compile(
    r"(?:^|\s)(?:repo|org|user)\s*:", re.IGNORECASE
)
_MAX_HTTP_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_FILE_BYTES = 512 * 1024
_MAX_DIRECTORY_ENTRIES = 100
_MAX_DIRECTORY_OUTPUT_CHARS = 18000
_MAX_CHANGED_FILES = 30
_MAX_CHANGED_FILES_OUTPUT_CHARS = 10000
_MAX_PATCH_CHARS = 4000
_MAX_TOTAL_PATCH_CHARS = 12000
_MAX_BODY_CHARS = 12000
_MAX_FILE_CONTENT_CHARS = 16000
_MAX_LINE_CHARS = 2000


def github_repository_tool() -> ToolSpec:
    return ToolSpec(
        name="github_repository",
        description=(
            "只读查看 GitHub.com 仓库、目录、文本代码、提交、Pull Request 和 Issue。"
            "search_code 需要宿主配置 GITHUB_TOKEN；其他操作可匿名读取公开仓库。"
            "结果中的 GitHub URL 仅供回答依据，除非用户明确要求来源或链接，否则"
            "不要在最终回答中展示 URL。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "要执行的 GitHub 只读操作。",
                    "enum": list(_ACTIONS),
                },
                "repository": {
                    "type": "string",
                    "description": "严格的 owner/name 仓库名，不接受 URL。",
                    "minLength": 3,
                    "maxLength": 140,
                },
                "path": {
                    "type": "string",
                    "description": "仓库内 POSIX 相对路径。",
                    "maxLength": 1024,
                },
                "ref": {
                    "type": "string",
                    "description": "可选分支、标签或提交引用。",
                    "minLength": 1,
                    "maxLength": 255,
                },
                "query": {
                    "type": "string",
                    "description": "仓库内代码搜索词，不接受额外仓库范围。",
                    "minLength": 1,
                    "maxLength": 256,
                },
                "commit_sha": {
                    "type": "string",
                    "description": "7 到 64 位十六进制提交 SHA。",
                    "minLength": 7,
                    "maxLength": 64,
                },
                "number": {
                    "type": "integer",
                    "description": "Pull Request 或 Issue 编号。",
                    "minimum": 1,
                },
                "state": {
                    "type": "string",
                    "description": "Pull Request 或 Issue 状态。",
                    "enum": ["open", "closed", "all"],
                    "default": "open",
                },
                "limit": {
                    "type": "integer",
                    "description": "列表或搜索结果数量，范围 1 到 20。",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 10,
                },
                "start_line": {
                    "type": "integer",
                    "description": "读取文件的起始行号，从 1 开始。",
                    "minimum": 1,
                    "default": 1,
                },
                "line_count": {
                    "type": "integer",
                    "description": "读取行数，范围 1 到 300。",
                    "minimum": 1,
                    "maximum": 300,
                    "default": 200,
                },
            },
            "required": ["action", "repository"],
            "additionalProperties": False,
        },
        handler=_github_repository,
        risk=ToolRisk.READ_ONLY,
        timeout_seconds=15.0,
        max_output_chars=24000,
    )


def _github_repository(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    del context
    action = str(arguments["action"]).strip()
    repository = _normalize_repository(arguments["repository"])
    token = str(os.environ.get("GITHUB_TOKEN") or "").strip() or None
    if action == "search_code" and token is None:
        raise ToolExecutionError(
            "github_token_required",
            "GitHub 代码搜索需要宿主配置 GITHUB_TOKEN。",
        )
    with _create_client(token) as client:
        handler = {
            "get_repository": _get_repository,
            "list_directory": _list_directory,
            "read_file": _read_file,
            "search_code": _search_code,
            "list_commits": _list_commits,
            "get_commit": _get_commit,
            "list_pull_requests": _list_pull_requests,
            "get_pull_request": _get_pull_request,
            "list_issues": _list_issues,
            "get_issue": _get_issue,
        }[action]
        result, truncated = handler(client, repository, arguments)
    return {
        "action": action,
        "repository": repository,
        "result": result,
        "truncated": bool(truncated),
    }


def _create_client(token: str | None) -> httpx.Client:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "JianerAI-GitHub-Reader/1.0",
        "X-GitHub-Api-Version": _API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(
        base_url=_API_BASE,
        headers=headers,
        timeout=httpx.Timeout(8.0, connect=3.0),
        follow_redirects=False,
    )


def _get_repository(
    client: httpx.Client,
    repository: str,
    arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    del arguments
    data = _require_mapping(
        _request_json(client, f"/repos/{repository}"),
        "GitHub 返回了无效的仓库数据。",
    )
    license_data = data.get("license")
    license_value = None
    if isinstance(license_data, Mapping):
        license_value = _clean_text(
            license_data.get("spdx_id") or license_data.get("name"),
            100,
        ) or None
    topics = [
        _clean_text(item, 100)
        for item in _as_sequence(data.get("topics"))[:20]
        if _clean_text(item, 100)
    ]
    return (
        {
            "full_name": _clean_text(data.get("full_name"), 140),
            "description": _trim_text(data.get("description"), 2000),
            "default_branch": _clean_text(data.get("default_branch"), 255),
            "language": _clean_text(data.get("language"), 100) or None,
            "topics": topics,
            "visibility": _clean_text(data.get("visibility"), 20)
            or ("private" if data.get("private") else "public"),
            "archived": bool(data.get("archived")),
            "fork": bool(data.get("fork")),
            "stars": _safe_int(data.get("stargazers_count")),
            "forks": _safe_int(data.get("forks_count")),
            "open_issues": _safe_int(data.get("open_issues_count")),
            "license": license_value,
            "created_at": _clean_text(data.get("created_at"), 40),
            "updated_at": _clean_text(data.get("updated_at"), 40),
            "pushed_at": _clean_text(data.get("pushed_at"), 40),
            "html_url": _github_html_url(data.get("html_url")),
        },
        len(_as_sequence(data.get("topics"))) > 20,
    )


def _list_directory(
    client: httpx.Client,
    repository: str,
    arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    path = _normalize_path(arguments.get("path"), allow_empty=True)
    ref = _optional_ref(arguments.get("ref"))
    endpoint = f"/repos/{repository}/contents"
    if path:
        endpoint += f"/{quote(path, safe='/')}"
    data = _request_json(client, endpoint, params=_optional_params(ref=ref))
    if isinstance(data, Mapping):
        raise ToolExecutionError(
            "github_not_directory",
            "指定路径不是目录，请改用 read_file。",
        )
    entries = []
    for item in _as_sequence(data):
        if not isinstance(item, Mapping):
            continue
        item_path = _clean_text(item.get("path"), 1024)
        if not item_path:
            continue
        entries.append(
            {
                "name": _clean_text(item.get("name"), 255),
                "path": item_path,
                "type": _clean_text(item.get("type"), 30),
                "size": _safe_int(item.get("size")),
                "sha": _clean_text(item.get("sha"), 64),
                "html_url": _github_html_url(item.get("html_url")),
            }
        )
    entries.sort(key=lambda item: (item["type"] != "dir", item["path"].casefold()))
    truncated = len(entries) > _MAX_DIRECTORY_ENTRIES
    selected_entries = []
    output_chars = 0
    for item in entries[:_MAX_DIRECTORY_ENTRIES]:
        item_chars = len(json.dumps(item, ensure_ascii=False))
        if output_chars + item_chars > _MAX_DIRECTORY_OUTPUT_CHARS:
            truncated = True
            break
        selected_entries.append(item)
        output_chars += item_chars
    entries = selected_entries
    return (
        {
            "path": path,
            "ref": ref,
            "entry_count": len(entries),
            "entries": entries,
        },
        truncated,
    )


def _read_file(
    client: httpx.Client,
    repository: str,
    arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    path = _normalize_path(arguments.get("path"), allow_empty=False)
    ref = _optional_ref(arguments.get("ref"))
    start_line = int(arguments.get("start_line", 1))
    line_count = int(arguments.get("line_count", 200))
    endpoint = f"/repos/{repository}/contents/{quote(path, safe='/')}"
    data = _request_json(client, endpoint, params=_optional_params(ref=ref))
    if not isinstance(data, Mapping) or data.get("type") != "file":
        raise ToolExecutionError(
            "github_not_file",
            "指定路径不是可读取的普通文件。",
        )
    declared_size = _safe_int(data.get("size"))
    if declared_size > _MAX_FILE_BYTES:
        raise ToolExecutionError(
            "github_file_too_large",
            "文件超过 512 KiB 读取上限。",
        )
    if str(data.get("encoding") or "").casefold() != "base64":
        raise ToolExecutionError(
            "github_file_too_large",
            "GitHub 未返回可安全解码的文件内容。",
        )
    encoded = "".join(str(data.get("content") or "").split())
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ToolExecutionError(
            "github_invalid_response",
            "GitHub 返回了无效的文件内容。",
        ) from exc
    if len(raw) > _MAX_FILE_BYTES:
        raise ToolExecutionError(
            "github_file_too_large",
            "文件超过 512 KiB 读取上限。",
        )
    if b"\x00" in raw:
        raise ToolExecutionError(
            "github_binary_file",
            "该文件是二进制文件，不能作为代码文本读取。",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolExecutionError(
            "github_binary_file",
            "该文件不是 UTF-8 文本，不能作为代码读取。",
        ) from exc
    lines = text.splitlines()
    selected = lines[start_line - 1 : start_line - 1 + line_count]
    output_lines: list[str] = []
    used_chars = 0
    content_truncated = False
    for line_number, line in enumerate(selected, start=start_line):
        if len(line) > _MAX_LINE_CHARS:
            line = line[:_MAX_LINE_CHARS]
            content_truncated = True
        rendered = f"{line_number}: {line}"
        remaining = _MAX_FILE_CONTENT_CHARS - used_chars
        if remaining <= 0:
            content_truncated = True
            break
        if len(rendered) > remaining:
            output_lines.append(rendered[:remaining])
            used_chars += remaining
            content_truncated = True
            break
        output_lines.append(rendered)
        used_chars += len(rendered) + 1
    end_line = start_line + len(output_lines) - 1 if output_lines else 0
    truncated = (
        content_truncated
        or start_line > 1
        or end_line < len(lines)
    )
    return (
        {
            "path": path,
            "ref": ref,
            "sha": _clean_text(data.get("sha"), 64),
            "size": len(raw),
            "total_lines": len(lines),
            "start_line": start_line,
            "end_line": end_line,
            "content": "\n".join(output_lines),
            "html_url": _github_html_url(data.get("html_url")),
        },
        truncated,
    )


def _search_code(
    client: httpx.Client,
    repository: str,
    arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    query_value = _required_text(arguments, "query", 256)
    if _SCOPE_QUALIFIER_PATTERN.search(query_value):
        raise ToolExecutionError(
            "github_invalid_query",
            "代码搜索词不能包含 repo、org 或 user 范围。",
        )
    limit = int(arguments.get("limit", 10))
    data = _require_mapping(
        _request_json(
            client,
            "/search/code",
            params={"q": f"{query_value} repo:{repository}", "per_page": limit},
        ),
        "GitHub 返回了无效的代码搜索结果。",
    )
    matches = []
    for item in _as_sequence(data.get("items")):
        if not isinstance(item, Mapping):
            continue
        repo_data = item.get("repository")
        full_name = (
            _clean_text(repo_data.get("full_name"), 140)
            if isinstance(repo_data, Mapping)
            else ""
        )
        if full_name.casefold() != repository.casefold():
            continue
        matches.append(
            {
                "name": _clean_text(item.get("name"), 255),
                "path": _clean_text(item.get("path"), 1024),
                "sha": _clean_text(item.get("sha"), 64),
                "html_url": _github_html_url(item.get("html_url")),
            }
        )
        if len(matches) >= limit:
            break
    total_count = _safe_int(data.get("total_count"))
    return (
        {
            "query": query_value,
            "total_count": total_count,
            "match_count": len(matches),
            "matches": matches,
        },
        bool(data.get("incomplete_results")) or total_count > len(matches),
    )


def _list_commits(
    client: httpx.Client,
    repository: str,
    arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    limit = int(arguments.get("limit", 10))
    ref = _optional_ref(arguments.get("ref"))
    path = _normalize_path(arguments.get("path"), allow_empty=True)
    params: dict[str, Any] = {"per_page": limit}
    if ref:
        params["sha"] = ref
    if path:
        params["path"] = path
    data = _as_sequence(
        _request_json(client, f"/repos/{repository}/commits", params=params)
    )
    commits = [_commit_summary(item) for item in data[:limit] if isinstance(item, Mapping)]
    return (
        {"ref": ref, "path": path, "commit_count": len(commits), "commits": commits},
        len(data) >= limit,
    )


def _get_commit(
    client: httpx.Client,
    repository: str,
    arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    commit_sha = _required_text(arguments, "commit_sha", 64)
    if _COMMIT_PATTERN.fullmatch(commit_sha) is None:
        raise ToolExecutionError(
            "github_invalid_commit",
            "commit_sha 必须是 7 到 64 位十六进制 SHA。",
        )
    data = _require_mapping(
        _request_json(
            client,
            f"/repos/{repository}/commits/{commit_sha}",
            params={"per_page": _MAX_CHANGED_FILES, "page": 1},
        ),
        "GitHub 返回了无效的提交数据。",
    )
    files, files_truncated = _normalize_changed_files(data.get("files"))
    stats = data.get("stats") if isinstance(data.get("stats"), Mapping) else {}
    result = _commit_summary(data)
    result.update(
        {
            "stats": {
                "additions": _safe_int(stats.get("additions")),
                "deletions": _safe_int(stats.get("deletions")),
                "total": _safe_int(stats.get("total")),
            },
            "parents": [
                _clean_text(item.get("sha"), 64)
                for item in _as_sequence(data.get("parents"))[:10]
                if isinstance(item, Mapping)
            ],
            "files": files,
        }
    )
    return result, files_truncated


def _list_pull_requests(
    client: httpx.Client,
    repository: str,
    arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    limit = int(arguments.get("limit", 10))
    state = str(arguments.get("state", "open"))
    data = _as_sequence(
        _request_json(
            client,
            f"/repos/{repository}/pulls",
            params={
                "state": state,
                "sort": "updated",
                "direction": "desc",
                "per_page": limit,
            },
        )
    )
    pulls = [_pull_summary(item) for item in data[:limit] if isinstance(item, Mapping)]
    return (
        {"state": state, "pull_request_count": len(pulls), "pull_requests": pulls},
        len(data) >= limit,
    )


def _get_pull_request(
    client: httpx.Client,
    repository: str,
    arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    number = _required_number(arguments)
    data = _require_mapping(
        _request_json(client, f"/repos/{repository}/pulls/{number}"),
        "GitHub 返回了无效的 Pull Request 数据。",
    )
    files_data = _request_json(
        client,
        f"/repos/{repository}/pulls/{number}/files",
        params={"per_page": _MAX_CHANGED_FILES, "page": 1},
    )
    files, files_truncated = _normalize_changed_files(files_data)
    result = _pull_summary(data)
    body, body_truncated = _trim_with_flag(data.get("body"), _MAX_BODY_CHARS)
    result.update(
        {
            "body": body,
            "merged": bool(data.get("merged")),
            "mergeable": data.get("mergeable"),
            "merge_commit_sha": _clean_text(data.get("merge_commit_sha"), 64) or None,
            "commits": _safe_int(data.get("commits")),
            "changed_files": _safe_int(data.get("changed_files")),
            "additions": _safe_int(data.get("additions")),
            "deletions": _safe_int(data.get("deletions")),
            "files": files,
        }
    )
    return result, files_truncated or body_truncated


def _list_issues(
    client: httpx.Client,
    repository: str,
    arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    limit = int(arguments.get("limit", 10))
    state = str(arguments.get("state", "open"))
    data = _as_sequence(
        _request_json(
            client,
            f"/repos/{repository}/issues",
            params={
                "state": state,
                "sort": "updated",
                "direction": "desc",
                "per_page": 100,
                "page": 1,
            },
        )
    )
    issues = []
    for item in data:
        if not isinstance(item, Mapping) or "pull_request" in item:
            continue
        issues.append(_issue_summary(item))
        if len(issues) >= limit:
            break
    actual_issue_count = sum(
        1 for item in data if isinstance(item, Mapping) and "pull_request" not in item
    )
    return (
        {"state": state, "issue_count": len(issues), "issues": issues},
        len(data) >= 100 or actual_issue_count > len(issues),
    )


def _get_issue(
    client: httpx.Client,
    repository: str,
    arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    number = _required_number(arguments)
    data = _require_mapping(
        _request_json(client, f"/repos/{repository}/issues/{number}"),
        "GitHub 返回了无效的 Issue 数据。",
    )
    if "pull_request" in data:
        raise ToolExecutionError(
            "github_not_issue",
            "该编号属于 Pull Request，请使用 get_pull_request。",
        )
    result = _issue_summary(data)
    body, body_truncated = _trim_with_flag(data.get("body"), _MAX_BODY_CHARS)
    result.update(
        {
            "body": body,
            "assignees": [
                _clean_text(item.get("login"), 100)
                for item in _as_sequence(data.get("assignees"))[:20]
                if isinstance(item, Mapping)
            ],
            "milestone": (
                _clean_text(data["milestone"].get("title"), 300)
                if isinstance(data.get("milestone"), Mapping)
                else None
            ),
        }
    )
    return result, body_truncated or len(_as_sequence(data.get("assignees"))) > 20


def _request_json(
    client: httpx.Client,
    endpoint: str,
    *,
    params: Mapping[str, Any] | None = None,
) -> Any:
    try:
        response = client.get(endpoint, params=dict(params or {}))
    except httpx.TimeoutException as exc:
        raise ToolExecutionError("github_timeout", "GitHub 请求超时。") from exc
    except httpx.HTTPError as exc:
        raise ToolExecutionError(
            "github_unavailable",
            "无法连接 GitHub API。",
        ) from exc
    status = response.status_code
    if status == 401:
        raise ToolExecutionError(
            "github_unauthorized",
            "GitHub 令牌无效或没有访问权限。",
        )
    if status == 404:
        raise ToolExecutionError(
            "github_not_found",
            "仓库或资源不存在，或当前令牌无权访问。",
        )
    if status == 429 or (
        status == 403 and response.headers.get("x-ratelimit-remaining") == "0"
    ):
        raise ToolExecutionError(
            "github_rate_limited",
            "GitHub API 请求额度已用尽，请稍后再试。",
        )
    if status == 403:
        raise ToolExecutionError(
            "github_forbidden",
            "当前 GitHub 凭据不允许执行此读取操作。",
        )
    if status == 422:
        raise ToolExecutionError(
            "github_invalid_request",
            "GitHub 拒绝了当前查询参数。",
        )
    if 300 <= status < 400:
        raise ToolExecutionError(
            "github_redirect_blocked",
            "GitHub 返回了未允许的重定向。",
        )
    if status >= 500:
        raise ToolExecutionError(
            "github_unavailable",
            "GitHub API 暂时不可用。",
        )
    if status < 200 or status >= 300:
        raise ToolExecutionError("github_failed", "GitHub 请求失败。")
    if len(response.content) > _MAX_HTTP_RESPONSE_BYTES:
        raise ToolExecutionError(
            "github_response_too_large",
            "GitHub 响应超过安全读取上限。",
        )
    try:
        return response.json()
    except ValueError as exc:
        raise ToolExecutionError(
            "github_invalid_response",
            "GitHub 返回了无效的 JSON 数据。",
        ) from exc


def _normalize_repository(value: Any) -> str:
    repository = str(value or "").strip()
    if repository.count("/") != 1 or "://" in repository:
        raise ToolExecutionError(
            "github_invalid_repository",
            "repository 必须使用 owner/name 格式。",
        )
    owner, name = repository.split("/", 1)
    if (
        _OWNER_PATTERN.fullmatch(owner) is None
        or _REPOSITORY_PATTERN.fullmatch(name) is None
        or name in {".", ".."}
        or name.casefold().endswith(".git")
    ):
        raise ToolExecutionError(
            "github_invalid_repository",
            "repository 必须是有效的 GitHub owner/name。",
        )
    return f"{owner}/{name}"


def _normalize_path(value: Any, *, allow_empty: bool) -> str:
    path = str(value or "").strip()
    if not path:
        if allow_empty:
            return ""
        raise ToolExecutionError("github_path_required", "当前操作需要 path。")
    if (
        path.startswith(("/", "\\"))
        or "\\" in path
        or "://" in path
        or any(ord(char) < 32 for char in path)
    ):
        raise ToolExecutionError(
            "github_invalid_path",
            "path 必须是仓库内 POSIX 相对路径。",
        )
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ToolExecutionError(
            "github_invalid_path",
            "path 不能包含空段、点段或路径穿越。",
        )
    return path


def _optional_ref(value: Any) -> str | None:
    if value is None:
        return None
    ref = str(value).strip()
    if (
        not ref
        or len(ref) > 255
        or "\\" in ref
        or any(ord(char) < 32 for char in ref)
    ):
        raise ToolExecutionError("github_invalid_ref", "ref 不是有效的 Git 引用。")
    return ref


def _optional_params(*, ref: str | None) -> dict[str, str]:
    return {"ref": ref} if ref else {}


def _required_text(
    arguments: Mapping[str, Any],
    name: str,
    limit: int,
) -> str:
    value = str(arguments.get(name) or "").strip()
    if not value:
        raise ToolExecutionError(f"github_{name}_required", f"当前操作需要 {name}。")
    if len(value) > limit or any(ord(char) < 32 for char in value):
        raise ToolExecutionError(f"github_invalid_{name}", f"{name} 不符合安全限制。")
    return value


def _required_number(arguments: Mapping[str, Any]) -> int:
    value = arguments.get("number")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ToolExecutionError("github_number_required", "当前操作需要有效的 number。")
    return value


def _require_mapping(value: Any, message: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ToolExecutionError("github_invalid_response", message)
    return value


def _as_sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _commit_summary(data: Mapping[str, Any]) -> dict[str, Any]:
    commit = data.get("commit") if isinstance(data.get("commit"), Mapping) else {}
    author_data = commit.get("author") if isinstance(commit.get("author"), Mapping) else {}
    user_data = data.get("author") if isinstance(data.get("author"), Mapping) else {}
    return {
        "sha": _clean_text(data.get("sha"), 64),
        "message": _trim_text(commit.get("message"), 1000),
        "author": _clean_text(user_data.get("login") or author_data.get("name"), 100),
        "authored_at": _clean_text(author_data.get("date"), 40),
        "verified": bool(
            commit.get("verification", {}).get("verified")
            if isinstance(commit.get("verification"), Mapping)
            else False
        ),
        "html_url": _github_html_url(data.get("html_url")),
    }


def _pull_summary(data: Mapping[str, Any]) -> dict[str, Any]:
    user = data.get("user") if isinstance(data.get("user"), Mapping) else {}
    head = data.get("head") if isinstance(data.get("head"), Mapping) else {}
    base = data.get("base") if isinstance(data.get("base"), Mapping) else {}
    return {
        "number": _safe_int(data.get("number")),
        "title": _clean_text(data.get("title"), 500),
        "state": _clean_text(data.get("state"), 20),
        "draft": bool(data.get("draft")),
        "author": _clean_text(user.get("login"), 100),
        "head": _clean_text(head.get("ref"), 255),
        "base": _clean_text(base.get("ref"), 255),
        "created_at": _clean_text(data.get("created_at"), 40),
        "updated_at": _clean_text(data.get("updated_at"), 40),
        "closed_at": _clean_text(data.get("closed_at"), 40) or None,
        "merged_at": _clean_text(data.get("merged_at"), 40) or None,
        "html_url": _github_html_url(data.get("html_url")),
    }


def _issue_summary(data: Mapping[str, Any]) -> dict[str, Any]:
    user = data.get("user") if isinstance(data.get("user"), Mapping) else {}
    labels = []
    for item in _as_sequence(data.get("labels"))[:20]:
        if isinstance(item, Mapping):
            label = _clean_text(item.get("name"), 100)
        else:
            label = _clean_text(item, 100)
        if label:
            labels.append(label)
    return {
        "number": _safe_int(data.get("number")),
        "title": _clean_text(data.get("title"), 500),
        "state": _clean_text(data.get("state"), 20),
        "author": _clean_text(user.get("login"), 100),
        "labels": labels,
        "comments": _safe_int(data.get("comments")),
        "created_at": _clean_text(data.get("created_at"), 40),
        "updated_at": _clean_text(data.get("updated_at"), 40),
        "closed_at": _clean_text(data.get("closed_at"), 40) or None,
        "html_url": _github_html_url(data.get("html_url")),
    }


def _normalize_changed_files(value: Any) -> tuple[list[dict[str, Any]], bool]:
    raw_files = _as_sequence(value)
    files = []
    patch_chars = 0
    output_chars = 0
    truncated = len(raw_files) >= _MAX_CHANGED_FILES
    for item in raw_files[:_MAX_CHANGED_FILES]:
        if not isinstance(item, Mapping):
            continue
        patch = str(item.get("patch") or "")
        remaining = _MAX_TOTAL_PATCH_CHARS - patch_chars
        if len(patch) > _MAX_PATCH_CHARS:
            patch = patch[:_MAX_PATCH_CHARS]
            truncated = True
        if len(patch) > remaining:
            patch = patch[: max(0, remaining)]
            truncated = True
        patch_chars += len(patch)
        normalized = {
            "filename": _clean_text(item.get("filename"), 500),
            "previous_filename": _clean_text(item.get("previous_filename"), 500)
            or None,
            "status": _clean_text(item.get("status"), 30),
            "additions": _safe_int(item.get("additions")),
            "deletions": _safe_int(item.get("deletions")),
            "changes": _safe_int(item.get("changes")),
            "patch": patch or None,
            "html_url": _github_html_url(item.get("blob_url")),
        }
        item_chars = len(json.dumps(normalized, ensure_ascii=False))
        if output_chars + item_chars > _MAX_CHANGED_FILES_OUTPUT_CHARS:
            truncated = True
            break
        files.append(normalized)
        output_chars += item_chars
    return files, truncated


def _github_html_url(value: Any) -> str | None:
    url = str(value or "").strip()
    if not url or len(url) > 2048 or any(char.isspace() for char in url):
        return None
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return url


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _trim_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _trim_with_flag(value: Any, limit: int) -> tuple[str, bool]:
    text = str(value or "").strip()
    return text[:limit], len(text) > limit
