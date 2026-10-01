from __future__ import annotations

"""FastAPI app factory for the LAN Jianer long-memory console."""

import hmac
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
    from fastapi.responses import FileResponse, PlainTextResponse
except ImportError as exc:  # pragma: no cover - dependency is installed by requirements.txt
    raise RuntimeError("Memory console requires fastapi; install the root requirements") from exc

from .adapter import MemoryConsoleAdapter


@dataclass(frozen=True, slots=True)
class ConsoleConfig:
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8787
    token: str = ""

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any] | None = None) -> "ConsoleConfig":
        raw = dict(config or {})
        nested = raw.get("jianer_ai_memory_console")
        section = dict(nested) if isinstance(nested, Mapping) else raw
        def pick(name: str, flat_name: str, env_name: str, default: Any) -> Any:
            value = section.get(name)
            if value is None and section is raw:
                value = raw.get(flat_name)
            if value is None:
                value = os.getenv(env_name, default)
            return value

        enabled_value = pick("enabled", "jianer_ai_memory_console_enabled", "JIANER_AI_MEMORY_CONSOLE_ENABLED", "0")
        enabled = enabled_value if isinstance(enabled_value, bool) else str(enabled_value).lower() in {"1", "true", "yes", "on"}
        return cls(
            enabled=bool(enabled),
            host=str(pick("host", "jianer_ai_memory_console_host", "JIANER_AI_MEMORY_CONSOLE_HOST", "0.0.0.0")),
            port=max(1, int(pick("port", "jianer_ai_memory_console_port", "JIANER_AI_MEMORY_CONSOLE_PORT", "8787"))),
            token=str(pick("token", "jianer_ai_memory_console_token", "JIANER_AI_MEMORY_CONSOLE_TOKEN", "")).strip(),
        )


def _require_token(expected: str, provided: str | None) -> None:
    if not expected:
        raise HTTPException(status_code=503, detail="memory console token is not configured")
    if not provided or not hmac.compare_digest(str(provided), expected):
        raise HTTPException(status_code=401, detail="invalid memory console token")


def create_app(
    kernel: Any = None,
    *,
    memory_store: Any = None,
    config: Mapping[str, Any] | ConsoleConfig | None = None,
    token: str | None = None,
    static_dir: str | Path | None = None,
) -> FastAPI:
    """Create an authenticated LAN console.

    An explicit token is required in production.  ``ConsoleConfig.enabled`` is
    checked by the host before starting uvicorn; this factory remains useful in
    tests and embedding scenarios when passed ``token=...``.
    """
    console_config = config if isinstance(config, ConsoleConfig) else ConsoleConfig.from_mapping(config)
    expected_token = str(token if token is not None else console_config.token).strip()
    if not expected_token:
        raise ValueError("jianer_ai_memory_console_token is required to start the memory console")
    adapter = MemoryConsoleAdapter(kernel=kernel, memory_store=memory_store, config=(config if isinstance(config, Mapping) else getattr(kernel, "config", {})))
    app = FastAPI(title="JianerAI Memory Console", version="1.0", docs_url="/api/docs", redoc_url=None)
    app.state.memory_console = adapter
    app.state.memory_console_config = console_config
    app.state.memory_console_token = expected_token

    @app.get("/legal", include_in_schema=False)
    async def legal_notice() -> PlainTextResponse:
        notice_path = Path(__file__).resolve().parent.parent / "memorix" / "NOTICE.md"
        license_path = Path(__file__).resolve().parent.parent / "memorix" / "LICENSE"
        sections = []
        for path in (notice_path, license_path):
            try:
                sections.append(path.read_text(encoding="utf-8"))
            except OSError:
                sections.append(f"{path.name} is unavailable in this installation.")
        return PlainTextResponse("\n\n".join(sections), media_type="text/plain; charset=utf-8")

    async def auth(x_memory_token: str | None = Header(default=None, alias="X-Memory-Token")) -> None:
        _require_token(expected_token, x_memory_token)

    @app.get("/api/health", dependencies=[Depends(auth)])
    async def health() -> dict[str, Any]:
        status = await adapter.embedding_status()
        return {"ok": True, "ready": adapter.ready, "backend": "jianer_long_memory" if kernel is not None else "legacy", "embedding": status}

    @app.get("/api/stats", dependencies=[Depends(auth)])
    async def stats() -> dict[str, Any]:
        return await adapter.stats()

    @app.get("/api/embedding/status", dependencies=[Depends(auth)])
    async def embedding_status() -> dict[str, Any]:
        return await adapter.embedding_status()

    @app.get("/api/search", dependencies=[Depends(auth)])
    async def search(q: str = Query(default=""), limit: int = Query(default=10, ge=1, le=100), mode: str = Query(default="search"), person_id: str = Query(default=""), chat_id: str = Query(default=""), group_id: str = Query(default="")) -> dict[str, Any]:
        return await adapter.search(q, limit, mode, person_id=person_id, chat_id=chat_id, group_id=group_id)

    @app.get("/api/{resource}", dependencies=[Depends(auth)])
    async def list_resource(resource: str, limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0), include_deleted: bool = Query(default=False), q: str = Query(default="")) -> dict[str, Any]:
        if resource == "graph":
            return await adapter.graph(q, limit)
        if resource == "config":
            return {"config": await adapter.config_snapshot(), "console": {"host": console_config.host, "port": console_config.port}}
        allowed = {"paragraphs", "relations", "facts", "sources", "episodes", "profiles", "recycle-bin", "deleted-relations"}
        if resource not in allowed:
            raise HTTPException(status_code=404, detail="resource not found")
        return await adapter.list_items(resource, limit, offset, include_deleted)

    @app.get("/api/graph", dependencies=[Depends(auth)])
    async def graph(q: str = Query(default=""), limit: int = Query(default=200, ge=1, le=500)) -> dict[str, Any]:
        return await adapter.graph(q, limit)

    @app.get("/api/profile/{person_id}", dependencies=[Depends(auth)])
    async def profile(person_id: str, limit: int = Query(default=10, ge=1, le=100)) -> dict[str, Any]:
        return await adapter.profile(person_id, limit)

    @app.post("/api/maintenance", dependencies=[Depends(auth)])
    async def maintenance(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        return await adapter.maintain(str(payload.get("action") or "status"), str(payload.get("target") or ""), payload.get("hours"), str(payload.get("reason") or "console"))

    @app.post("/api/delete", dependencies=[Depends(auth)])
    async def delete_memory(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        return await adapter.admin("delete", str(payload.get("action") or "preview"), payload)

    @app.post("/api/restore", dependencies=[Depends(auth)])
    async def restore_memory(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        return await adapter.admin("delete", "restore", payload)

    @app.post("/api/admin/{area}/{action}", dependencies=[Depends(auth)])
    async def admin(area: str, action: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        if area not in {"source", "episode", "profile", "fact", "runtime", "import", "bundle", "tuning", "v5", "delete", "correction", "fuzzy_modify"}:
            raise HTTPException(status_code=404, detail="admin area not found")
        return await adapter.admin(area, action, payload)

    @app.get("/api/config", dependencies=[Depends(auth)])
    async def config_snapshot() -> dict[str, Any]:
        return {"config": await adapter.config_snapshot(), "console": {"host": console_config.host, "port": console_config.port}}

    root = Path(static_dir) if static_dir else Path(__file__).with_name("static")
    index = root / "index.html"
    if index.exists():
        @app.get("/", include_in_schema=False)
        async def index_page() -> FileResponse:
            return FileResponse(index, media_type="text/html")

        @app.get("/static/{asset:path}", include_in_schema=False)
        async def static_asset(asset: str) -> FileResponse:
            candidate = (root / asset).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                raise HTTPException(status_code=404, detail="asset not found")
            if not candidate.is_file():
                return FileResponse(index, media_type="text/html")
            return FileResponse(candidate)

        @app.get("/{asset:path}", include_in_schema=False)
        async def spa_asset(asset: str) -> FileResponse:
            if asset == "api" or asset.startswith("api/"):
                raise HTTPException(status_code=404, detail="not found")
            candidate = (root / asset).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                raise HTTPException(status_code=404, detail="asset not found")
            return FileResponse(candidate if candidate.is_file() else index)
    return app


def serve(kernel: Any = None, *, memory_store: Any = None, config: Mapping[str, Any] | ConsoleConfig | None = None) -> None:
    """Run the console with uvicorn; intended for a dedicated host process."""
    import uvicorn

    console_config = config if isinstance(config, ConsoleConfig) else ConsoleConfig.from_mapping(config)
    if not console_config.enabled:
        raise RuntimeError("memory console is disabled")
    app = create_app(kernel, memory_store=memory_store, config=console_config)
    uvicorn.run(app, host=console_config.host, port=console_config.port, log_level="info")
