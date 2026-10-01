from __future__ import annotations

"""Small compatibility facade over Jianer's long-memory core.

The web layer must not know whether a deployment has finished switching to
``SDKMemoryKernel``.  Every method therefore returns JSON-safe dictionaries and
reports unsupported operations explicitly.
"""

import asyncio
import inspect
from collections.abc import Mapping, Sequence
from typing import Any


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        return {str(key): _jsonable(item) for key, item in vars(value).items() if not key.startswith("_")}
    return str(value)


def _redact_config(value: Any, *, key: str = "") -> Any:
    if isinstance(value, Mapping):
        return {str(name): _redact_config(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_config(item, key=key) for item in value]
    if key.casefold() in {"api_key", "apikey", "token", "secret", "password"} and value:
        return "[redacted]"
    return value


class MemoryConsoleAdapter:
    """Normalize runtime calls used by the console.

    ``kernel`` is the long-memory SDK kernel. ``memory_store`` supplies the
    surrounding Jianer service APIs such as transcripts and identity data.
    """

    def __init__(self, kernel: Any = None, memory_store: Any = None, config: Mapping[str, Any] | None = None) -> None:
        self.kernel = kernel
        self.memory_store = memory_store
        self._kernel_bridge = memory_store if callable(getattr(memory_store, "_call_kernel", None)) else None
        self.config = dict(config or getattr(kernel, "config", {}) or {})

    @property
    def ready(self) -> bool:
        if self.kernel is not None:
            ready = getattr(self.kernel, "is_runtime_ready", None)
            try:
                return bool(ready()) if callable(ready) else True
            except Exception:
                return False
        return self.memory_store is not None

    async def _call(self, target: Any, *args: Any, **kwargs: Any) -> Any:
        if not callable(target):
            return None
        result = target(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _kernel_call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self._kernel_bridge is not None:
            async def operation(bound_kernel: Any) -> Any:
                method = getattr(bound_kernel, name, None)
                if not callable(method):
                    return None
                result = method(*args, **kwargs)
                return await result if inspect.isawaitable(result) else result

            return await asyncio.to_thread(self._kernel_bridge._call_kernel, operation)
        return await self._call(getattr(self.kernel, name, None), *args, **kwargs)

    async def stats(self) -> dict[str, Any]:
        if self.kernel is not None:
            try:
                data = await self._kernel_call("memory_stats")
                return {"ready": self.ready, "backend": "jianer_long_memory", "stats": _jsonable(data or {})}
            except Exception as exc:
                return {"ready": False, "backend": "jianer_long_memory", "error": str(exc), "stats": {}}
        if self.memory_store is not None:
            memorix_stats = getattr(self.memory_store, "memorix_stats", None)
            if callable(memorix_stats):
                try:
                    return {"ready": True, "backend": "jianer_long_memory", "stats": _jsonable(memorix_stats())}
                except Exception as exc:
                    return {"ready": False, "backend": "jianer_long_memory", "error": str(exc), "stats": {}}
            method = getattr(self.memory_store, "get_memory_status", None)
            try:
                data = await self._call(method, canonical_user_id="", preset="")
                return {"ready": True, "backend": "legacy", "stats": _jsonable(data or {})}
            except Exception as exc:
                return {"ready": False, "backend": "legacy", "error": str(exc), "stats": {}}
        return {"ready": False, "backend": "unconfigured", "stats": {}}

    async def embedding_status(self) -> dict[str, Any]:
        if self._kernel_bridge is not None:
            result: dict[str, Any] = {"available": True, "state": "ready" if self.ready else "not_initialized"}
            for name, key in (
                ("_embedding_degraded_snapshot", "degraded"),
                ("_vector_health_snapshot", "vector_health"),
                ("_vector_pools_status", "vector_pools"),
                ("_runtime_capability_status", "capabilities"),
                ("_current_embedding_status_dimension", "dimension"),
                ("_current_embedding_fingerprint", "fingerprint"),
            ):
                try:
                    result[key] = _jsonable(await self._kernel_call(name))
                except Exception as exc:
                    result[key] = {"error": str(exc)}
            return result
        kernel = self.kernel
        if kernel is None:
            store_status = getattr(self.memory_store, "embedding_status", None)
            if callable(store_status):
                return _jsonable(await self._call(store_status))
            return {"available": False, "state": "unconfigured", "reason": "Jianer long-memory kernel is not attached"}
        result: dict[str, Any] = {"available": bool(getattr(kernel, "embedding_manager", None)), "state": "ready" if self.ready else "not_initialized"}
        for name, key in (
            ("_embedding_degraded_snapshot", "degraded"),
            ("_vector_health_snapshot", "vector_health"),
            ("_vector_pools_status", "vector_pools"),
            ("_runtime_capability_status", "capabilities"),
        ):
            method = getattr(kernel, name, None)
            if callable(method):
                try:
                    result[key] = _jsonable(method())
                except Exception as exc:
                    result[key] = {"error": str(exc)}
        for name, key in (("_current_embedding_status_dimension", "dimension"), ("_current_embedding_fingerprint", "fingerprint")):
            method = getattr(kernel, name, None)
            if callable(method):
                try:
                    result[key] = _jsonable(method())
                except Exception as exc:
                    result[key] = {"error": str(exc)}
        return result

    async def search(self, query: str, limit: int = 10, mode: str = "search", **scope: Any) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            return {"summary": "", "hits": [], "error": "query is required"}
        if self.kernel is not None:
            try:
                from plugins.JianerAI.memorix.core.runtime.models import KernelSearchRequest
            except Exception:
                KernelSearchRequest = None
            if KernelSearchRequest is not None:
                request = KernelSearchRequest(query=query, limit=max(1, min(int(limit), 100)), mode=mode, **{key: value for key, value in scope.items() if value is not None})
                return _jsonable(await self._kernel_call("search_memory", request))
            return {"summary": "", "hits": [], "error": "Jianer long-memory search request type is unavailable"}
        store = self.memory_store
        method = getattr(store, "query_memories", None) if store is not None else None
        if callable(method):
            try:
                result = await self._call(method, canonical_user_id=str(scope.get("person_id") or ""), preset=str(scope.get("preset") or "default"), query=query, limit=int(limit))
                return {"summary": "", "hits": _jsonable(result or [])}
            except Exception as exc:
                return {"summary": "", "hits": [], "error": str(exc)}
        return {"summary": "", "hits": [], "error": "search is unavailable"}

    async def _metadata_query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        if self._kernel_bridge is not None:
            async def operation(bound_kernel: Any) -> Any:
                metadata = getattr(bound_kernel, "metadata_store", None)
                query = getattr(metadata, "query", None)
                return query(sql, tuple(params)) if callable(query) else []

            result = await asyncio.to_thread(self._kernel_bridge._call_kernel, operation)
            return [_jsonable(item) for item in (result or [])]
        metadata = getattr(self.kernel, "metadata_store", None) if self.kernel is not None else None
        query = getattr(metadata, "query", None)
        if not callable(query):
            return []
        result = await self._call(query, sql, tuple(params))
        return [_jsonable(item) for item in (result or [])]

    async def list_items(self, resource: str, limit: int = 50, offset: int = 0, include_deleted: bool = False) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        metadata = getattr(self.kernel, "metadata_store", None) if self.kernel is not None else None
        if metadata is None:
            return {"resource": resource, "items": [], "unavailable": True}
        direct = {
            "sources": ("get_all_sources", (), {}),
            "recycle-bin": ("list_delete_operations", (), {"limit": limit}),
            "deleted-relations": ("get_deleted_relations", (), {"limit": limit}),
        }.get(resource)
        if direct:
            method, args, kwargs = direct
            try:
                rows = await self._call(getattr(metadata, method, None), *args, **kwargs)
                return {"resource": resource, "items": _jsonable(rows or [])}
            except Exception as exc:
                return {"resource": resource, "items": [], "error": str(exc)}
        table = {"paragraphs": "paragraphs", "relations": "relations", "episodes": "episodes", "profiles": "person_profile_snapshots", "facts": "fact_claims"}.get(resource)
        if not table:
            return {"resource": resource, "items": [], "unavailable": True}
        # Relations use a separate ``deleted_relations`` table in the long-memory core;
        # unlike paragraphs, the active table has no ``is_deleted`` column.
        # Keep the console's include_deleted contract explicit instead of
        # issuing an invalid predicate against ``relations``.
        if resource == "relations":
            try:
                active = await self._metadata_query(
                    "SELECT * FROM relations ORDER BY rowid DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
                if not include_deleted:
                    return {"resource": resource, "items": active}
                deleted = await self._metadata_query(
                    "SELECT * FROM deleted_relations ORDER BY rowid DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
                for item in deleted:
                    item.setdefault("is_deleted", 1)
                return {"resource": resource, "items": active + deleted}
            except Exception as exc:
                return {"resource": resource, "items": [], "error": str(exc)}
        deleted_clause = "" if include_deleted else " WHERE COALESCE(is_deleted, 0) = 0" if resource == "paragraphs" else ""
        try:
            rows = await self._metadata_query(f"SELECT * FROM {table}{deleted_clause} ORDER BY rowid DESC LIMIT ? OFFSET ?", (limit, offset))
            return {"resource": resource, "items": rows}
        except Exception as exc:
            return {"resource": resource, "items": [], "error": str(exc)}

    async def graph(self, query: str = "", limit: int = 200) -> dict[str, Any]:
        if self.kernel is None:
            return {"nodes": [], "edges": [], "unavailable": True}
        try:
            action = "search" if query.strip() else "get_graph"
            data = await self._kernel_call("memory_graph_admin", action=action, query=query, limit=max(1, min(int(limit), 500)))
            return _jsonable(data or {"nodes": [], "edges": []})
        except Exception as exc:
            return {"nodes": [], "edges": [], "error": str(exc)}

    async def profile(self, person_id: str, limit: int = 10) -> dict[str, Any]:
        if self.kernel is None:
            return {"person_id": person_id, "unavailable": True}
        try:
            return _jsonable(await self._kernel_call("get_person_profile", person_id=person_id, limit=max(1, min(int(limit), 100))))
        except Exception as exc:
            return {"person_id": person_id, "error": str(exc)}

    async def maintain(self, action: str, target: str = "", hours: float | None = None, reason: str = "") -> dict[str, Any]:
        if self.kernel is None:
            return {"success": False, "unavailable": True}
        try:
            return _jsonable(await self._kernel_call("maintain_memory", action=action, target=target, hours=hours, reason=reason))
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    async def admin(self, area: str, action: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if self.kernel is None:
            return {"success": False, "unavailable": True}
        method = getattr(self.kernel, f"memory_{area}_admin", None)
        if not callable(method):
            return {"success": False, "unavailable": True, "area": area, "action": action}
        try:
            # The route action is authoritative.  Removing a duplicate
            # payload key also keeps ``/api/delete`` and ``/api/restore``
            # from raising ``TypeError: multiple values for action`` when
            # their JSON body includes the UI's selected action.
            arguments = dict(payload or {})
            arguments.pop("action", None)
            return _jsonable(await self._call(method, action=action, **arguments))
        except Exception as exc:
            return {"success": False, "error": str(exc), "area": area, "action": action}

    async def config_snapshot(self) -> dict[str, Any]:
        return _jsonable(_redact_config(self.config))
