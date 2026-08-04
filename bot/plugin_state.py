"""Shared state for JianerCore new-style plugins."""

from __future__ import annotations

import asyncio
import ast
import concurrent.futures
import contextlib
import contextvars
import threading
from pathlib import Path
from typing import Any

from jianer import Client, events, get_dispatch_runner, submit_awaitable
from jianer.plugins import (
    PluginManager,
    PluginManagerState,
    ShutdownReport,
)

PLUGIN_FOLDER = "plugins"
DISABLED_PREFIX = "d_"

_config: Any = None
_logger: Any = None
_plugin_client: Client | None = None
_plugin_manager: PluginManager | None = None
_retiring_managers: dict[str, PluginManager] = {}
_retirement_futures: dict[
    str,
    concurrent.futures.Future[ShutdownReport],
] = {}
_retirement_tasks: dict[str, asyncio.Task[ShutdownReport]] = {}
_retirement_failures: dict[str, ShutdownReport] = {}
_load_result: Any = None
_disabled_plugins: list[str] = []
_help_text = ""
_last_listener_start: tuple[Any, Any] | None = None
_state_lock = threading.RLock()
_sync_reload_lock = threading.Lock()
_runtime_reload_lock = threading.Lock()
_current_plugin_pipeline: contextvars.ContextVar[
    "PluginDispatchPipeline | None"
] = contextvars.ContextVar("current_plugin_dispatch_pipeline", default=None)

_runtime: dict[str, Any] = {
    "reminder": "",
    "bot_name": "",
    "bot_name_en": "",
    "one_slogan": "",
    "confused_word": "",
    "root_users": [],
    "super_users": [],
    "manage_users": [],
    "admins": [],
    "supers": [],
    "cooldowns": {},
    "cooldowns1": {},
    "generating": False,
}

def configure(
    *,
    config: Any,
    logger: Any,
    reminder: str,
    bot_name: str,
    bot_name_en: str,
    one_slogan: str,
    confused_word: str,
    root_users: list,
    cooldowns: dict,
    cooldowns1: dict,
) -> None:
    global _config, _logger
    _config = config
    _logger = logger
    _runtime.update(
        {
            "reminder": reminder,
            "bot_name": bot_name,
            "bot_name_en": bot_name_en,
            "one_slogan": one_slogan,
            "confused_word": confused_word,
            "root_users": root_users,
            "cooldowns": cooldowns,
            "cooldowns1": cooldowns1,
            "config": config,
            "logger": logger,
        }
    )


def get_config() -> Any:
    return _config


def get_logger() -> Any:
    return _logger


def get_runtime() -> dict[str, Any]:
    return _runtime


def set_auth_snapshot(
    admins: list[str],
    supers: list[str],
    root_users: list,
    super_users: list,
    manage_users: list,
) -> None:
    _runtime.update(
        {
            "admins": [str(item) for item in admins],
            "supers": [str(item) for item in supers],
            "root_users": root_users,
            "super_users": super_users,
            "manage_users": manage_users,
        }
    )


def set_generating(value: bool) -> None:
    _runtime["generating"] = bool(value)


def is_generating() -> bool:
    return bool(_runtime.get("generating", False))


class PluginReloadError(RuntimeError):
    """A plugin reload failed before or after activating its candidate."""

    def __init__(
        self,
        message: str,
        *,
        result: Any = None,
        candidate_manager: PluginManager | None = None,
        cleanup_report: ShutdownReport | None = None,
        active_manager: PluginManager | None = None,
    ) -> None:
        super().__init__(message)
        self.result = result
        self.candidate_manager = candidate_manager
        self.cleanup_report = cleanup_report
        self.active_manager = active_manager


class PluginRetirementError(RuntimeError):
    """A new generation is active, but the old generation did not close."""

    def __init__(
        self,
        message: str,
        *,
        result: Any,
        active_manager: PluginManager,
        retirement_report: ShutdownReport,
    ) -> None:
        super().__init__(message)
        self.result = result
        self.active_manager = active_manager
        self.retirement_report = retirement_report


class PluginDispatchPipeline:
    """Hold one manager generation across all phases of a host message."""

    def __init__(
        self,
        event: Any,
        actions: Any,
    ) -> None:
        self.event = event
        self.actions = actions
        self.manager: PluginManager | None = None
        self._entered = False
        self._owner_task: asyncio.Task[Any] | None = None
        self._context_token: contextvars.Token | None = None

    async def __aenter__(self) -> "PluginDispatchPipeline":
        if self._entered:
            raise RuntimeError("plugin dispatch pipeline cannot be re-entered")
        self._entered = True
        client = get_plugin_client()
        if client is not None:
            with client._lifecycle_lock:
                manager = client.plugin_manager
                if manager is not None and manager._acquire_dispatch():
                    self.manager = manager
        self._owner_task = asyncio.current_task()
        self._context_token = _current_plugin_pipeline.set(self)
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        try:
            self.close()
        finally:
            token, self._context_token = self._context_token, None
            if token is not None:
                _current_plugin_pipeline.reset(token)

    def close(self) -> None:
        manager = self.manager
        self.manager = None
        if manager is not None:
            manager._release_dispatch()

    async def observe(self, *, message_text: str | None = None) -> None:
        manager = self._require_entered_manager()
        if manager is None:
            return
        await manager._dispatch_hook(
            "on_message_observe",
            _dispatch_event(self.event, message_text),
            self.actions,
            stop_on_true=False,
        )

    async def dispatch_normal(
        self,
        *,
        message_text: str | None = None,
        run_observers: bool = False,
    ) -> bool:
        manager = self._require_entered_manager()
        if manager is None:
            return False
        return await manager._dispatch_with_acquired_lease(
            _dispatch_event(self.event, message_text),
            self.actions,
            run_observers=run_observers,
        )

    async def dispatch_fallback(
        self,
        *,
        message_text: str | None = None,
    ) -> bool:
        manager = self._require_entered_manager()
        if manager is None:
            return False
        return await manager._dispatch_hook(
            "on_message_fallback",
            _dispatch_event(self.event, message_text),
            self.actions,
            stop_on_true=True,
        )

    def _require_entered_manager(self) -> PluginManager | None:
        if not self._entered:
            raise RuntimeError(
                "use PluginDispatchPipeline as an asynchronous context manager"
            )
        return self.manager


def plugin_dispatch_pipeline(
    event: Any,
    actions: Any,
) -> PluginDispatchPipeline:
    return PluginDispatchPipeline(event, actions)


def reload_plugins(logger: Any | None = None):
    """Load plugins synchronously outside an event loop.

    Startup uses the direct synchronous path. Later synchronous callers retain
    compatibility by running the asynchronous two-phase reload only when no
    event loop is active. Runtime handlers must use
    :func:`reload_plugins_async`.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "reload_plugins() cannot run inside an event loop; "
            "use 'await reload_plugins_async()'"
        )

    with _sync_reload_lock:
        if get_plugin_manager() is None:
            return _load_plugins_initial(logger=logger)
        return asyncio.run(reload_plugins_async(logger=logger))


def _load_plugins_initial(logger: Any | None = None):
    """Install the first manager without creating a temporary event loop."""

    client = _ensure_plugin_client()
    try:
        manager, result, disabled, help_text = _build_staged_manager(
            client,
            logger=logger,
        )
    except PluginReloadError as exc:
        candidate = exc.candidate_manager
        if candidate is not None:
            exc.cleanup_report = asyncio.run(_retire_manager(candidate))
        raise
    try:
        replaced = client.swap_plugin_manager(manager, expected=None)
        if replaced is not None:
            raise RuntimeError("initial plugin load unexpectedly replaced a manager")
    except Exception as exc:
        cleanup_report = asyncio.run(_retire_manager(manager))
        raise PluginReloadError(
            f"initial plugin activation failed: {exc}",
            result=result,
            candidate_manager=manager,
            cleanup_report=cleanup_report,
        ) from exc

    _publish_manager(manager, result, disabled, help_text)
    return result


async def reload_plugins_async(logger: Any | None = None):
    """Stage, atomically swap, then shut down one runtime plugin generation."""

    async with _runtime_reload_guard():
        client = _ensure_plugin_client()
        expected = get_plugin_manager()
        manager: PluginManager | None = None
        result: Any = None
        stage_task = asyncio.create_task(
            asyncio.to_thread(
                _build_staged_manager,
                client,
                logger=logger,
            )
        )
        try:
            manager, result, disabled, help_text = await asyncio.shield(
                stage_task
            )
        except asyncio.CancelledError:
            candidate: PluginManager | None = None
            try:
                staged = await asyncio.shield(stage_task)
            except PluginReloadError as exc:
                candidate = exc.candidate_manager
            except Exception:
                pass
            else:
                candidate = staged[0]
            if candidate is not None:
                await _retire_manager(candidate)
            raise
        except PluginReloadError as exc:
            candidate = exc.candidate_manager
            if candidate is not None:
                exc.cleanup_report = await _retire_manager(candidate)
            raise
        except Exception as exc:
            if manager is not None:
                cleanup_report = await _retire_manager(manager)
            else:
                cleanup_report = None
            raise PluginReloadError(
                f"plugin staging failed: {exc}",
                result=result,
                candidate_manager=manager,
                cleanup_report=cleanup_report,
            ) from exc

        try:
            old_manager = client.swap_plugin_manager(manager, expected=expected)
        except Exception as exc:
            cleanup_report = await _retire_manager(manager)
            raise PluginReloadError(
                f"plugin swap failed: {exc}",
                result=result,
                candidate_manager=manager,
                cleanup_report=cleanup_report,
            ) from exc

        _publish_manager(manager, result, disabled, help_text)
        retirement_error: PluginRetirementError | None = None
        if old_manager is not None:
            current_pipeline = _current_plugin_pipeline.get()
            if (
                current_pipeline is not None
                and current_pipeline.manager is old_manager
                and current_pipeline._owner_task is asyncio.current_task()
            ):
                current_pipeline.close()
            report = await _retire_manager(old_manager)
            if not report.completed:
                _log_shutdown_errors("old plugin manager", report)
                retirement_error = PluginRetirementError(
                    "new plugin generation is active, but the old "
                    "generation did not shut down cleanly",
                    result=result,
                    active_manager=manager,
                    retirement_report=report,
                )
        try:
            await _replay_listener_start(client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise PluginReloadError(
                "new plugin generation is active, but replaying the "
                "listener-start lifecycle event failed",
                result=result,
                active_manager=manager,
            ) from exc
        if retirement_error is not None:
            raise retirement_error
        return result


async def shutdown_plugins() -> ShutdownReport:
    """Close the active plugin manager and every plugin-owned callback."""

    global _plugin_client, _plugin_manager, _load_result
    global _disabled_plugins, _help_text, _last_listener_start

    async with _runtime_reload_guard():
        with _state_lock:
            client = _plugin_client
            manager = _plugin_manager

        reports: list[ShutdownReport] = []
        if client is not None:
            client_report = await _close_client_safely(client)
            reports.append(client_report)
            if client_report.completed:
                _clear_retirement_failure(client_report.manager_id)
            elif (
                manager is not None
                and manager.state == PluginManagerState.CLOSED
            ):
                _record_retirement_failure(client_report)
        elif manager is not None:
            reports.append(await _retire_manager(manager))

        with _state_lock:
            retiring = list(_retiring_managers.values())
        for retiring_manager in retiring:
            if retiring_manager is manager:
                continue
            with _state_lock:
                retry_future = _retirement_futures.get(
                    retiring_manager.manager_id
                )
            if retry_future is not None and not retry_future.done():
                continue
            reports.append(await _retire_manager(retiring_manager))

        retirement_errors = await _wait_for_retirement_futures()
        if retirement_errors:
            reports.append(
                ShutdownReport(
                    manager_id="bot-plugin-retirement",
                    completed=False,
                    errors=retirement_errors,
                )
            )

        with _state_lock:
            failure_reports = list(_retirement_failures.values())
        report_ids = {item.manager_id for item in reports}
        reports.extend(
            item
            for item in failure_reports
            if item.manager_id not in report_ids
        )

        if not reports:
            with _state_lock:
                _last_listener_start = None
            return ShutdownReport(
                manager_id="bot-plugin-state",
                completed=True,
                errors=(),
            )

        manager_closed = (
            manager is None or manager.state == PluginManagerState.CLOSED
        )
        client_closed = client is None or bool(getattr(client, "_closed", False))
        if manager_closed and client_closed:
            with _state_lock:
                if _plugin_client is client and _plugin_manager is manager:
                    _plugin_client = None
                    _plugin_manager = None
                    _load_result = None
                    _disabled_plugins = []
                    _help_text = ""
                    _last_listener_start = None
        errors = tuple(
            error
            for report in reports
            for error in report.errors
        )
        report = ShutdownReport(
            manager_id=(
                manager.manager_id
                if manager is not None
                else "bot-plugin-state"
            ),
            completed=all(item.completed for item in reports),
            errors=errors,
        )
        if not report.completed:
            _log_shutdown_errors("plugin runtime", report)
        return report


def get_plugin_client() -> Client | None:
    with _state_lock:
        return _plugin_client


def retiring_plugin_managers() -> list[PluginManager]:
    with _state_lock:
        return list(_retiring_managers.values())


def plugin_retirement_failures() -> list[ShutdownReport]:
    with _state_lock:
        return list(_retirement_failures.values())


def get_plugin_manager() -> PluginManager | None:
    with _state_lock:
        return _plugin_manager


def get_load_result() -> Any:
    with _state_lock:
        return _load_result


def loaded_plugins() -> list[str]:
    if _load_result is None:
        return []
    return list(getattr(_load_result, "loaded", []))


def disabled_plugins() -> list[str]:
    return list(_disabled_plugins)


def failed_plugins() -> list[str]:
    if _load_result is None:
        return []
    return list(getattr(_load_result, "failed", []))


def plugin_warnings() -> list[str]:
    if _load_result is None:
        return []
    return list(getattr(_load_result, "warnings", []))


def plugin_help_text() -> str:
    return _help_text


async def dispatch_plugins(
    event: Any,
    actions: Any,
    *,
    message_text: str | None = None,
) -> bool:
    async with plugin_dispatch_pipeline(
        event,
        actions,
    ) as pipeline:
        return await pipeline.dispatch_normal(
            message_text=message_text,
            run_observers=True,
        )


async def observe_plugins(
    event: Any,
    actions: Any,
    *,
    message_text: str | None = None,
) -> None:
    async with plugin_dispatch_pipeline(
        event,
        actions,
    ) as pipeline:
        await pipeline.observe(message_text=message_text)


async def dispatch_subscriptions(event: Any, actions: Any) -> None:
    """Deliver framework events to setup-time ``Client.subscribe`` hooks."""

    global _last_listener_start
    if isinstance(event, events.HyperListenerStartNotify):
        with _state_lock:
            _last_listener_start = (event, actions)
    client = get_plugin_client()
    if client is None:
        return
    await client.distributor(event, actions)


async def _replay_listener_start(client: Client) -> None:
    """Restore setup-time tasks after a hot-reload generation swap."""

    with _state_lock:
        latest = _last_listener_start
    if latest is None:
        return
    try:
        await client.distributor(*latest)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger = _logger
        if logger is not None:
            logger.exception(
                "replaying the listener-start lifecycle event failed"
            )
        raise


async def dispatch_normal(
    event: Any,
    actions: Any,
    *,
    message_text: str | None = None,
    run_observers: bool = False,
) -> bool:
    async with plugin_dispatch_pipeline(
        event,
        actions,
    ) as pipeline:
        return await pipeline.dispatch_normal(
            message_text=message_text,
            run_observers=run_observers,
        )


async def dispatch_fallback(
    event: Any,
    actions: Any,
    *,
    message_text: str | None = None,
) -> bool:
    async with plugin_dispatch_pipeline(
        event,
        actions,
    ) as pipeline:
        return await pipeline.dispatch_fallback(message_text=message_text)


# Descriptive aliases for host code that groups plugin operations by prefix.
observe = observe_plugins
dispatch_plugins_normal = dispatch_normal
dispatch_plugins_fallback = dispatch_fallback


def get_plugin_module(plugin_id: str) -> Any | None:
    manager = get_plugin_manager()
    if manager is None:
        return None
    plugin = manager.plugins.get(plugin_id)
    return getattr(plugin, "module", None)


class _MessageTextEventProxy:
    """Override command text while retaining the adapter event interface."""

    def __init__(self, event: Any, message_text: str) -> None:
        self._event = event
        self.msg_str = message_text

    def __getattr__(self, name: str) -> Any:
        return getattr(self._event, name)


def _dispatch_event(event: Any, message_text: str | None) -> Any:
    if message_text is None:
        return event
    return _MessageTextEventProxy(event, message_text)


def _ensure_plugin_client() -> Client:
    global _plugin_client
    with _state_lock:
        if _plugin_client is None or bool(
            getattr(_plugin_client, "_closed", False)
        ):
            _plugin_client = Client()
        return _plugin_client


def _build_staged_manager(
    client: Client,
    *,
    logger: Any | None,
) -> tuple[PluginManager, Any, list[str], str]:
    manager = PluginManager(logger=logger or _logger)
    result: Any = None
    try:
        result = manager.load_plugins(PLUGIN_FOLDER)
        failures = list(getattr(result, "failed", []) or [])
        if failures:
            raise PluginReloadError(
                "plugin load failed: " + "; ".join(str(item) for item in failures),
                result=result,
                candidate_manager=manager,
            )
        manager.setup_client(client, activate=False)
        disabled = _scan_disabled_plugins()
        help_text = _render_help_text(result)
    except PluginReloadError:
        raise
    except Exception as exc:
        raise PluginReloadError(
            f"plugin setup failed: {exc}",
            result=result,
            candidate_manager=manager,
        ) from exc

    return manager, result, disabled, help_text


def _publish_manager(
    manager: PluginManager,
    result: Any,
    disabled: list[str],
    help_text: str,
) -> None:
    global _plugin_manager, _load_result, _disabled_plugins, _help_text
    with _state_lock:
        _plugin_manager = manager
        _load_result = result
        _disabled_plugins = disabled
        _help_text = help_text


@contextlib.asynccontextmanager
async def _runtime_reload_guard():
    acquired = False
    try:
        while not acquired:
            acquired = _runtime_reload_lock.acquire(blocking=False)
            if not acquired:
                await asyncio.sleep(0.01)
        yield
    finally:
        if acquired:
            _runtime_reload_lock.release()


async def _retire_manager(manager: PluginManager) -> ShutdownReport:
    with _state_lock:
        _retiring_managers[manager.manager_id] = manager

    retiring_alconna_commands = _snapshot_alconna_commands(manager.manager_id)
    task = asyncio.create_task(manager.shutdown())
    report: ShutdownReport | None = None
    try:
        try:
            report = await asyncio.shield(task)
        except asyncio.CancelledError:
            report = await asyncio.shield(task)
            raise
    finally:
        if manager.state == PluginManagerState.CLOSED:
            _reconcile_alconna_commands(retiring_alconna_commands)
            with _state_lock:
                if _retiring_managers.get(manager.manager_id) is manager:
                    _retiring_managers.pop(manager.manager_id, None)
            if report is not None:
                if report.completed:
                    _clear_retirement_failure(report.manager_id)
                else:
                    _record_retirement_failure(report)
        else:
            _schedule_retirement_retry(manager)
    if report is None:
        raise RuntimeError("plugin manager shutdown did not return a report")
    return report


def _snapshot_alconna_commands(manager_id: str) -> list[Any]:
    """Keep command objects alive until Arclet's weak registry is reconciled."""

    try:
        from jianer.plugins.builtin import alconna
    except Exception:
        return []
    commands: list[Any] = []
    seen: set[int] = set()
    lock = getattr(alconna, "_MATCHER_LOCK", contextlib.nullcontext())
    with lock:
        manager_matchers = getattr(alconna, "_MATCHERS", {}).get(manager_id, {})
        for matchers in manager_matchers.values():
            for matcher in matchers:
                command = getattr(matcher, "command", None)
                if command is not None and id(command) not in seen:
                    seen.add(id(command))
                    commands.append(command)
    return commands


def _reconcile_alconna_commands(retiring_commands: list[Any]) -> None:
    """Remove a retired generation and repoint Arclet at live matchers.

    Arclet stores the command chosen for a name through a weak reference. During
    staged reload the new matcher is compiled, but the weak name entry can still
    point at the retiring generation. Once that generation is released,
    ``current_count`` leaks and repeated hot reloads eventually raise
    ``ExceedMaxCount``. Re-registering the live generation after deletion keeps
    the parser registry and its counter consistent.
    """

    if not retiring_commands:
        return
    try:
        from arclet.alconna import command_manager
        from jianer.plugins.builtin import alconna
    except Exception:
        return

    live_commands: list[Any] = []
    seen: set[int] = set()
    lock = getattr(alconna, "_MATCHER_LOCK", contextlib.nullcontext())
    with lock:
        matcher_groups = [
            matchers
            for plugins in getattr(alconna, "_MATCHERS", {}).values()
            for matchers in plugins.values()
        ]
        matcher_groups.append(getattr(alconna, "_LEGACY_MATCHERS", ()))
        for matchers in matcher_groups:
            for matcher in matchers:
                command = getattr(matcher, "command", None)
                if command is not None and id(command) not in seen:
                    seen.add(id(command))
                    live_commands.append(command)

    for command in retiring_commands:
        try:
            command_manager.delete(command)
        except Exception:
            if _logger is not None:
                _logger.exception("failed to unregister retired Alconna command")
    for command in live_commands:
        try:
            command_manager.register(command)
        except Exception:
            if _logger is not None:
                _logger.exception("failed to register live Alconna command")


def _schedule_retirement_retry(manager: PluginManager) -> None:
    with _state_lock:
        existing = _retirement_futures.get(manager.manager_id)
        if existing is not None and not existing.done():
            return
        runner = get_dispatch_runner()
        if runner.is_runner_thread():
            future: concurrent.futures.Future[ShutdownReport] = (
                concurrent.futures.Future()
            )
            task = asyncio.create_task(_retirement_retry_worker(manager))
            _retirement_tasks[manager.manager_id] = task
            task.add_done_callback(
                lambda completed, manager_id=manager.manager_id: (
                    _bridge_retirement_task(manager_id, completed, future)
                )
            )
        else:
            future = submit_awaitable(_retirement_retry_worker(manager))
        _retirement_futures[manager.manager_id] = future
        future.add_done_callback(
            lambda completed, manager_id=manager.manager_id: (
                _finish_retirement_future(manager_id, completed)
            )
        )


async def _retirement_retry_worker(
    manager: PluginManager,
) -> ShutdownReport:
    while True:
        await asyncio.sleep(0.25)
        try:
            report = await manager.shutdown()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger = _logger
            if logger is not None:
                logger.exception(
                    "retiring plugin manager retry failed: %s",
                    manager.manager_id,
                )
            continue
        if report.completed or manager.state == PluginManagerState.CLOSED:
            if report.completed:
                _clear_retirement_failure(report.manager_id)
            else:
                _record_retirement_failure(report)
            break
        _log_shutdown_errors("retiring plugin manager", report)

    if manager.state == PluginManagerState.CLOSED:
        with _state_lock:
            if _retiring_managers.get(manager.manager_id) is manager:
                _retiring_managers.pop(manager.manager_id, None)
    return report


def _bridge_retirement_task(
    manager_id: str,
    task: asyncio.Task[ShutdownReport],
    future: concurrent.futures.Future[ShutdownReport],
) -> None:
    try:
        result = task.result()
    except asyncio.CancelledError:
        future.cancel()
    except BaseException as exc:
        future.set_exception(exc)
    else:
        future.set_result(result)
    finally:
        with _state_lock:
            if _retirement_tasks.get(manager_id) is task:
                _retirement_tasks.pop(manager_id, None)


def _finish_retirement_future(
    manager_id: str,
    future: concurrent.futures.Future[ShutdownReport],
) -> None:
    with _state_lock:
        if _retirement_futures.get(manager_id) is future:
            _retirement_futures.pop(manager_id, None)


async def _wait_for_retirement_futures(
    *,
    timeout: float = 35.0,
) -> tuple[str, ...]:
    with _state_lock:
        futures = list(_retirement_futures.items())
    errors: list[str] = []
    for manager_id, future in futures:
        try:
            wrapped = asyncio.wrap_future(future)
            await asyncio.wait_for(asyncio.shield(wrapped), timeout=timeout)
        except asyncio.TimeoutError:
            errors.append(
                f"{manager_id} retirement retry did not finish within "
                f"{timeout:g} seconds"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors.append(f"{manager_id} retirement retry failed: {exc}")
    return tuple(errors)


def _record_retirement_failure(report: ShutdownReport) -> None:
    with _state_lock:
        _retirement_failures[report.manager_id] = report


def _clear_retirement_failure(manager_id: str) -> None:
    with _state_lock:
        _retirement_failures.pop(manager_id, None)


async def _close_client_safely(client: Client) -> ShutdownReport:
    task = asyncio.create_task(client.aclose())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.shield(task)
        raise


def _log_shutdown_errors(label: str, report: ShutdownReport) -> None:
    logger = _logger
    if logger is None:
        return
    logger.error(
        "%s shutdown incomplete: %s",
        label,
        "; ".join(report.errors) or "unknown error",
    )


def find_plugin_path(plugin_name: str, *, enable: bool) -> Path | None:
    folder = Path(PLUGIN_FOLDER).resolve()
    wanted = plugin_name.strip()
    if not wanted:
        return None

    candidates: list[Path] = []
    names = {wanted}
    if wanted.startswith("jianerbot-plugin-"):
        names.add(wanted.removeprefix("jianerbot-plugin-"))
    names.add(wanted.replace("-", "_"))

    if enable:
        prefixes = [DISABLED_PREFIX]
    else:
        prefixes = [""]

    for base in names:
        for prefix in prefixes:
            candidates.extend(
                [
                    folder / f"{prefix}{base}.py",
                    folder / f"{prefix}{base}.pyw",
                    folder / f"{prefix}{base}",
                ]
            )

    for candidate in candidates:
        if candidate.exists():
            return candidate

    for entry in folder.iterdir() if folder.exists() else []:
        if enable and not entry.name.startswith(DISABLED_PREFIX):
            continue
        if not enable and entry.name.startswith(DISABLED_PREFIX):
            continue
        metadata_name = _read_metadata_name(_entry_file(entry))
        if metadata_name == wanted:
            return entry
    return None


def _scan_disabled_plugins() -> list[str]:
    folder = Path(PLUGIN_FOLDER)
    if not folder.exists():
        return []
    disabled = []
    for entry in folder.iterdir():
        if entry.name.startswith(DISABLED_PREFIX):
            disabled.append(_display_name(entry.name[len(DISABLED_PREFIX) :]))
    return disabled


def _render_help_text(result: Any) -> str:
    lines = []
    plugin_map = getattr(result, "plugin_map", {}) or {}
    for plugin_id in getattr(result, "dependency_order", []) or []:
        if plugin_id == "jianerbot-plugin-alconna":
            continue
        plugin = plugin_map.get(plugin_id)
        metadata = getattr(plugin, "metadata", None)
        usage = getattr(metadata, "usage", "") if metadata else ""
        description = getattr(metadata, "description", "") if metadata else ""
        text = usage or description
        if text:
            lines.extend(str(text).splitlines())
    reminder = _runtime.get("reminder", "")
    return "".join(
        f"\n       {line.strip().replace('{reminder}', reminder)}"
        for line in lines
        if line.strip()
    )


def _display_name(filename: str) -> str:
    return Path(filename).stem


def _entry_file(entry: Path) -> Path:
    if entry.is_dir():
        return entry / "setup.py"
    return entry


def _read_metadata_name(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__plugin_meta__"
            for target in node.targets
        ):
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            return None
        if value.args:
            try:
                name = ast.literal_eval(value.args[0])
                return str(name)
            except (ValueError, TypeError):
                return None
        for keyword in value.keywords:
            if keyword.arg == "name":
                try:
                    name = ast.literal_eval(keyword.value)
                    return str(name)
                except (ValueError, TypeError):
                    return None
    return None
