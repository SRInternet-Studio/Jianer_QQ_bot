import asyncio
import logging
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from jianer import events, run_awaitable
from jianer.plugins import PluginManagerState, ShutdownReport

from bot import plugin_state


def _write(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def _basic_plugin(*, setup_body: str = "", marker: str = "old") -> str:
    body = f"""
        from jianer.plugins import PluginMetadata

        __plugin_meta__ = PluginMetadata(
            name="jianerbot-plugin-lifecycle-test",
        )

        async def on_message_observe(event, actions):
            actions.calls.append("observe:" + event.msg_str)

        async def on_message(event, actions):
            actions.calls.append("{marker}:" + event.msg_str)
            return False

        async def on_message_fallback(event, actions):
            actions.calls.append("fallback:" + event.msg_str)
            return True
    """
    return textwrap.dedent(body) + "\n" + textwrap.dedent(setup_body)


def _listener_plugin(marker: str, *, fail_replay: bool = False) -> str:
    failure = (
        'raise RuntimeError("listener replay exploded")'
        if fail_replay
        else ""
    )
    return _basic_plugin(
        marker=f"{marker}-handler",
        setup_body=f"""
        async def _started(event, actions):
            actions.calls.append("{marker}-listener")
            {failure}

        def setup(client, manager):
            client.subscribe(_started, events.HyperListenerStartNotify)
        """,
    ).replace(
        "from jianer.plugins import PluginMetadata",
        "from jianer import events\nfrom jianer.plugins import PluginMetadata",
        1,
    )


@pytest.fixture(autouse=True)
def _isolated_plugin_runtime(tmp_path, monkeypatch):
    asyncio.run(plugin_state.shutdown_plugins())
    with plugin_state._state_lock:
        plugin_state._retirement_failures.clear()
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    monkeypatch.setattr(plugin_state, "PLUGIN_FOLDER", str(plugins_dir))
    plugin_state.configure(
        config=SimpleNamespace(),
        logger=logging.getLogger("plugin_state_lifecycle_test"),
        reminder="~",
        bot_name="Jianer",
        bot_name_en="Jianer",
        one_slogan="test",
        confused_word="cannot do that",
        root_users=["1"],
        cooldowns={},
        cooldowns1={},
    )
    yield plugins_dir
    asyncio.run(plugin_state.shutdown_plugins())
    with plugin_state._state_lock:
        plugin_state._retirement_failures.clear()


def test_initial_load_owns_client_and_exposes_three_dispatch_phases(
    _isolated_plugin_runtime,
):
    plugin_file = _isolated_plugin_runtime / "lifecycle.py"
    _write(plugin_file, _basic_plugin())

    result = plugin_state.reload_plugins()

    manager = plugin_state.get_plugin_manager()
    client = plugin_state.get_plugin_client()
    assert result.failed == []
    assert manager is not None
    assert client is not None
    assert manager.client is client
    assert client.plugin_manager is manager
    assert manager.state == PluginManagerState.ACTIVE
    assert plugin_state.get_runtime()["config"] is plugin_state.get_config()
    assert plugin_state.get_runtime()["logger"] is plugin_state.get_logger()

    actions = SimpleNamespace(calls=[])
    event = SimpleNamespace(msg_str="original")

    async def dispatch_phases():
        await plugin_state.observe_plugins(
            event,
            actions,
            message_text="normalized",
        )
        handled = await plugin_state.dispatch_normal(
            event,
            actions,
            message_text="normalized",
        )
        fallback = await plugin_state.dispatch_fallback(
            event,
            actions,
            message_text="normalized",
        )
        return handled, fallback

    assert asyncio.run(dispatch_phases()) == (False, True)
    assert actions.calls == [
        "observe:normalized",
        "old:normalized",
        "fallback:normalized",
    ]


def test_setup_subscriptions_receive_host_lifecycle_events(
    _isolated_plugin_runtime,
):
    plugin_file = _isolated_plugin_runtime / "lifecycle.py"
    _write(
        plugin_file,
        _basic_plugin(
            setup_body="""
            async def _started(event, actions):
                actions.calls.append("listener-started:" + event.type)

            def setup(client, manager):
                client.subscribe(_started, events.HyperListenerStartNotify)
            """,
        ).replace(
            "from jianer.plugins import PluginMetadata",
            "from jianer import events\nfrom jianer.plugins import PluginMetadata",
            1,
        ),
    )
    plugin_state.reload_plugins()
    actions = SimpleNamespace(calls=[])
    event = events.HyperListenerStartNotify(0, "milky")

    asyncio.run(plugin_state.dispatch_subscriptions(event, actions))

    assert actions.calls == ["listener-started:milky"]


def test_hot_reload_replays_listener_start_to_new_active_generation(
    _isolated_plugin_runtime,
):
    plugin_file = _isolated_plugin_runtime / "lifecycle.py"
    _write(plugin_file, _listener_plugin("old"))
    plugin_state.reload_plugins()
    old_manager = plugin_state.get_plugin_manager()
    actions = SimpleNamespace(calls=[])
    event = events.HyperListenerStartNotify(0, "milky")
    asyncio.run(plugin_state.dispatch_subscriptions(event, actions))

    _write(plugin_file, _listener_plugin("new-generation"))
    result = asyncio.run(plugin_state.reload_plugins_async())
    new_manager = plugin_state.get_plugin_manager()

    assert actions.calls == ["old-listener", "new-generation-listener"]
    assert result is plugin_state.get_load_result()
    assert new_manager is not None and new_manager is not old_manager
    assert new_manager.state == PluginManagerState.ACTIVE
    assert old_manager is not None
    assert old_manager.state == PluginManagerState.CLOSED


def test_listener_replay_failure_is_reported_but_new_generation_stays_active(
    _isolated_plugin_runtime,
):
    plugin_file = _isolated_plugin_runtime / "lifecycle.py"
    _write(plugin_file, _listener_plugin("old"))
    plugin_state.reload_plugins()
    old_manager = plugin_state.get_plugin_manager()
    actions = SimpleNamespace(calls=[])
    event = events.HyperListenerStartNotify(0, "milky")
    asyncio.run(plugin_state.dispatch_subscriptions(event, actions))

    _write(
        plugin_file,
        _listener_plugin("new-generation-failing", fail_replay=True),
    )
    with pytest.raises(
        plugin_state.PluginReloadError,
        match="new plugin generation is active",
    ) as captured:
        asyncio.run(plugin_state.reload_plugins_async())

    error = captured.value
    active_manager = plugin_state.get_plugin_manager()
    active_result = plugin_state.get_load_result()
    assert actions.calls == [
        "old-listener",
        "new-generation-failing-listener",
    ]
    assert error.candidate_manager is None
    assert error.active_manager is active_manager
    assert error.result is active_result
    assert active_manager is not None and active_manager is not old_manager
    assert active_manager.state == PluginManagerState.ACTIVE
    assert old_manager is not None
    assert old_manager.state == PluginManagerState.CLOSED

    actions.calls.clear()
    handled = asyncio.run(
        plugin_state.dispatch_plugins(
            SimpleNamespace(msg_str="after-replay-error"), actions
        )
    )
    assert handled is False
    assert "new-generation-failing-handler:after-replay-error" in actions.calls


def test_setup_failure_keeps_old_manager_and_removes_candidate_callbacks(
    _isolated_plugin_runtime,
):
    plugin_file = _isolated_plugin_runtime / "lifecycle.py"
    _write(plugin_file, _basic_plugin())
    plugin_state.reload_plugins()
    old_manager = plugin_state.get_plugin_manager()
    client = plugin_state.get_plugin_client()
    assert old_manager is not None
    assert client is not None
    callbacks_before = list(
        client.records.get(events.GroupMemberIncreaseEvent, ())
    )

    _write(
        plugin_file,
        _basic_plugin(
            marker="candidate",
            setup_body="""
            async def _notice(event, actions):
                return None

            def setup(client, manager):
                client.subscribe(_notice, events.GroupMemberIncreaseEvent)
                raise RuntimeError("setup exploded")
            """,
        ).replace(
            "from jianer.plugins import PluginMetadata",
            "from jianer import events\nfrom jianer.plugins import PluginMetadata",
            1,
        ),
    )

    with pytest.raises(plugin_state.PluginReloadError) as captured:
        asyncio.run(plugin_state.reload_plugins_async())

    candidate = captured.value.candidate_manager
    assert candidate is not None
    assert candidate.state == PluginManagerState.CLOSED
    assert plugin_state.get_plugin_manager() is old_manager
    assert old_manager.state == PluginManagerState.ACTIVE
    assert list(client.records.get(events.GroupMemberIncreaseEvent, ())) == (
        callbacks_before
    )

    actions = SimpleNamespace(calls=[])
    handled = asyncio.run(
        plugin_state.dispatch_normal(
            SimpleNamespace(msg_str="still-active"),
            actions,
        )
    )
    assert handled is False
    assert actions.calls == ["old:still-active"]


def test_import_failure_rolls_back_without_replacing_active_generation(
    _isolated_plugin_runtime,
):
    plugin_file = _isolated_plugin_runtime / "lifecycle.py"
    _write(plugin_file, _basic_plugin())
    plugin_state.reload_plugins()
    old_manager = plugin_state.get_plugin_manager()

    _write(
        _isolated_plugin_runtime / "broken.py",
        """
        from jianer.plugins import PluginMetadata

        __plugin_meta__ = PluginMetadata(
            name="jianerbot-plugin-broken-generation",
        )

        raise RuntimeError("import exploded")
        """,
    )

    with pytest.raises(plugin_state.PluginReloadError) as captured:
        asyncio.run(plugin_state.reload_plugins_async())

    candidate = captured.value.candidate_manager
    assert candidate is not None
    assert candidate.state == PluginManagerState.CLOSED
    assert captured.value.result.failed
    assert plugin_state.get_plugin_manager() is old_manager
    assert old_manager is not None
    assert old_manager.state == PluginManagerState.ACTIVE


def test_concurrent_runtime_reloads_are_serialized(_isolated_plugin_runtime):
    plugin_file = _isolated_plugin_runtime / "lifecycle.py"
    _write(plugin_file, _basic_plugin())
    plugin_state.reload_plugins()
    original_manager = plugin_state.get_plugin_manager()
    assert original_manager is not None

    async def scenario():
        shutdown_started = asyncio.Event()
        allow_shutdown = asyncio.Event()
        real_shutdown = original_manager.shutdown

        async def delayed_shutdown(*, timeout=30.0):
            shutdown_started.set()
            await allow_shutdown.wait()
            return await real_shutdown(timeout=timeout)

        original_manager.shutdown = delayed_shutdown
        first = asyncio.create_task(plugin_state.reload_plugins_async())
        await asyncio.wait_for(shutdown_started.wait(), timeout=2)
        first_generation = plugin_state.get_plugin_manager()
        assert first_generation is not original_manager

        second = asyncio.create_task(plugin_state.reload_plugins_async())
        await asyncio.sleep(0)
        assert not second.done()
        assert plugin_state.get_plugin_manager() is first_generation

        allow_shutdown.set()
        await asyncio.wait_for(first, timeout=2)
        await asyncio.wait_for(second, timeout=2)
        return first_generation, plugin_state.get_plugin_manager()

    first_generation, final_generation = asyncio.run(scenario())
    assert final_generation is not None
    assert final_generation is not first_generation
    assert first_generation.state == PluginManagerState.CLOSED
    assert final_generation.state == PluginManagerState.ACTIVE


def test_runtime_reload_lock_is_safe_across_event_loops(
    _isolated_plugin_runtime,
):
    plugin_file = _isolated_plugin_runtime / "lifecycle.py"
    _write(plugin_file, _basic_plugin())
    plugin_state.reload_plugins()
    start = threading.Barrier(3)
    results = []
    errors = []

    def reload_in_thread():
        start.wait(timeout=2)
        try:
            results.append(asyncio.run(plugin_state.reload_plugins_async()))
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=reload_in_thread),
        threading.Thread(target=reload_in_thread),
    ]
    for thread in threads:
        thread.start()
    start.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    manager = plugin_state.get_plugin_manager()
    assert manager is not None
    assert manager.state == PluginManagerState.ACTIVE


def test_async_reload_staging_does_not_block_event_loop(
    _isolated_plugin_runtime,
    monkeypatch,
):
    plugin_file = _isolated_plugin_runtime / "lifecycle.py"
    _write(plugin_file, _basic_plugin())
    plugin_state.reload_plugins()

    real_build = plugin_state._build_staged_manager
    release_build = threading.Event()
    timer = threading.Timer(0.1, release_build.set)

    def slow_build(*args, **kwargs):
        release_build.wait(timeout=2)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(plugin_state, "_build_staged_manager", slow_build)

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while not release_build.is_set():
                ticks += 1
                await asyncio.sleep(0.005)

        heartbeat_task = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        timer.start()
        await asyncio.wait_for(plugin_state.reload_plugins_async(), timeout=2)
        await heartbeat_task
        return ticks

    try:
        ticks = asyncio.run(scenario())
    finally:
        timer.cancel()
    assert ticks >= 5


def test_cancellation_during_threaded_staging_cleans_candidate(
    _isolated_plugin_runtime,
    monkeypatch,
):
    plugin_file = _isolated_plugin_runtime / "lifecycle.py"
    _write(plugin_file, _basic_plugin())
    plugin_state.reload_plugins()
    old_manager = plugin_state.get_plugin_manager()
    assert old_manager is not None

    real_build = plugin_state._build_staged_manager
    build_started = threading.Event()
    allow_build = threading.Event()
    staged_managers = []

    def delayed_build(*args, **kwargs):
        build_started.set()
        allow_build.wait(timeout=2)
        staged = real_build(*args, **kwargs)
        staged_managers.append(staged[0])
        return staged

    monkeypatch.setattr(plugin_state, "_build_staged_manager", delayed_build)

    async def scenario():
        reload_task = asyncio.create_task(plugin_state.reload_plugins_async())
        for _ in range(200):
            if build_started.is_set():
                break
            await asyncio.sleep(0.005)
        assert build_started.is_set()
        reload_task.cancel()
        await asyncio.sleep(0)
        assert not reload_task.done()
        allow_build.set()
        with pytest.raises(asyncio.CancelledError):
            await reload_task

    asyncio.run(scenario())

    assert len(staged_managers) == 1
    assert staged_managers[0].state == PluginManagerState.CLOSED
    assert plugin_state.get_plugin_manager() is old_manager
    assert old_manager.state == PluginManagerState.ACTIVE


def test_pipeline_lease_keeps_all_phases_on_one_generation_during_reload(
    _isolated_plugin_runtime,
):
    plugin_file = _isolated_plugin_runtime / "lifecycle.py"
    _write(plugin_file, _basic_plugin(marker="old"))
    plugin_state.reload_plugins()
    old_manager = plugin_state.get_plugin_manager()
    assert old_manager is not None

    actions = SimpleNamespace(calls=[])
    event = SimpleNamespace(msg_str="raw-session")

    async def scenario():
        async with plugin_state.plugin_dispatch_pipeline(
            event,
            actions,
        ) as pipeline:
            assert pipeline.manager is old_manager
            await pipeline.observe()
            _write(plugin_file, _basic_plugin(marker="new"))
            reload_task = asyncio.create_task(
                plugin_state.reload_plugins_async()
            )
            for _ in range(200):
                if plugin_state.get_plugin_manager() is not old_manager:
                    break
                await asyncio.sleep(0.005)
            assert plugin_state.get_plugin_manager() is not old_manager
            assert not reload_task.done()
            assert await pipeline.dispatch_normal(
                message_text="normalized-session"
            ) is False
            assert await pipeline.dispatch_fallback(
                message_text="normalized-session"
            ) is True
        await asyncio.wait_for(reload_task, timeout=2)

    asyncio.run(scenario())

    assert actions.calls == [
        "observe:raw-session",
        "old:normalized-session",
        "fallback:normalized-session",
    ]
    assert old_manager.state == PluginManagerState.CLOSED


def test_direct_reload_inside_owning_pipeline_releases_lease_before_shutdown(
    _isolated_plugin_runtime,
):
    plugin_file = _isolated_plugin_runtime / "lifecycle.py"
    _write(plugin_file, _basic_plugin())
    plugin_state.reload_plugins()
    old_manager = plugin_state.get_plugin_manager()
    assert old_manager is not None

    async def scenario():
        async with plugin_state.plugin_dispatch_pipeline(
            SimpleNamespace(msg_str="~reload"),
            SimpleNamespace(calls=[]),
        ) as pipeline:
            assert pipeline.manager is old_manager
            started = time.monotonic()
            result = await plugin_state.reload_plugins_async()
            elapsed = time.monotonic() - started
            assert pipeline.manager is None
            return result, elapsed

    result, elapsed = asyncio.run(scenario())

    assert result.failed == []
    assert elapsed < 2
    assert old_manager.state == PluginManagerState.CLOSED
    assert plugin_state.get_plugin_manager() is not old_manager


def test_incomplete_old_shutdown_is_retained_and_retried_on_final_shutdown(
    _isolated_plugin_runtime,
):
    plugin_file = _isolated_plugin_runtime / "lifecycle.py"
    _write(plugin_file, _basic_plugin())
    plugin_state.reload_plugins()
    old_manager = plugin_state.get_plugin_manager()
    assert old_manager is not None
    real_shutdown = old_manager.shutdown
    calls = 0

    async def flaky_shutdown(*, timeout=30.0):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ShutdownReport(
                manager_id=old_manager.manager_id,
                completed=False,
                errors=("simulated drain timeout",),
            )
        return await real_shutdown(timeout=timeout)

    old_manager.shutdown = flaky_shutdown

    with pytest.raises(plugin_state.PluginRetirementError) as captured:
        asyncio.run(plugin_state.reload_plugins_async())

    assert captured.value.retirement_report.completed is False
    assert old_manager in plugin_state.retiring_plugin_managers()
    report = asyncio.run(plugin_state.shutdown_plugins())
    assert report.completed
    assert calls == 2
    assert old_manager.state == PluginManagerState.CLOSED
    assert plugin_state.retiring_plugin_managers() == []
    with plugin_state._state_lock:
        assert plugin_state._retirement_futures == {}
        assert plugin_state._retirement_tasks == {}


def test_incomplete_old_shutdown_is_retried_in_background(
    _isolated_plugin_runtime,
):
    plugin_file = _isolated_plugin_runtime / "lifecycle.py"
    _write(plugin_file, _basic_plugin())
    plugin_state.reload_plugins()
    old_manager = plugin_state.get_plugin_manager()
    assert old_manager is not None
    real_shutdown = old_manager.shutdown
    calls = 0

    async def flaky_shutdown(*, timeout=30.0):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ShutdownReport(
                manager_id=old_manager.manager_id,
                completed=False,
                errors=("simulated drain timeout",),
            )
        return await real_shutdown(timeout=timeout)

    old_manager.shutdown = flaky_shutdown

    with pytest.raises(plugin_state.PluginRetirementError):
        run_awaitable(plugin_state.reload_plugins_async())
    assert old_manager in plugin_state.retiring_plugin_managers()
    for _ in range(200):
        if old_manager.state == PluginManagerState.CLOSED:
            break
        time.sleep(0.01)
    else:
        pytest.fail("retiring manager was not retried after event loop exit")

    assert calls == 2
    assert old_manager.state == PluginManagerState.CLOSED
    assert plugin_state.retiring_plugin_managers() == []


def test_terminal_shutdown_hook_failure_is_reported_after_generation_swap(
    _isolated_plugin_runtime,
):
    plugin_file = _isolated_plugin_runtime / "lifecycle.py"
    _write(
        plugin_file,
        _basic_plugin(
            setup_body="""
                async def shutdown(client, manager):
                    raise RuntimeError("shutdown exploded")
            """,
        ),
    )
    plugin_state.reload_plugins()
    old_manager = plugin_state.get_plugin_manager()
    assert old_manager is not None

    _write(plugin_file, _basic_plugin(marker="new"))
    with pytest.raises(plugin_state.PluginRetirementError) as captured:
        asyncio.run(plugin_state.reload_plugins_async())

    active_manager = plugin_state.get_plugin_manager()
    assert active_manager is not None
    assert active_manager is not old_manager
    assert active_manager.state == PluginManagerState.ACTIVE
    assert old_manager.state == PluginManagerState.CLOSED
    assert any(
        "shutdown exploded" in error
        for error in captured.value.retirement_report.errors
    )
    assert plugin_state.plugin_retirement_failures() == [
        captured.value.retirement_report
    ]

    final_report = asyncio.run(plugin_state.shutdown_plugins())
    assert final_report.completed is False
    assert any("shutdown exploded" in error for error in final_report.errors)


def test_cancellation_after_swap_waits_for_old_generation_cleanup(
    _isolated_plugin_runtime,
):
    plugin_file = _isolated_plugin_runtime / "lifecycle.py"
    _write(plugin_file, _basic_plugin())
    plugin_state.reload_plugins()
    old_manager = plugin_state.get_plugin_manager()
    assert old_manager is not None
    real_shutdown = old_manager.shutdown

    async def scenario():
        shutdown_started = asyncio.Event()
        allow_shutdown = asyncio.Event()

        async def delayed_shutdown(*, timeout=30.0):
            shutdown_started.set()
            await allow_shutdown.wait()
            return await real_shutdown(timeout=timeout)

        old_manager.shutdown = delayed_shutdown
        reload_task = asyncio.create_task(plugin_state.reload_plugins_async())
        await asyncio.wait_for(shutdown_started.wait(), timeout=2)
        new_manager = plugin_state.get_plugin_manager()
        assert new_manager is not old_manager

        reload_task.cancel()
        await asyncio.sleep(0)
        assert not reload_task.done()
        allow_shutdown.set()
        with pytest.raises(asyncio.CancelledError):
            await reload_task
        return new_manager

    new_manager = asyncio.run(scenario())

    assert old_manager.state == PluginManagerState.CLOSED
    assert old_manager not in plugin_state.retiring_plugin_managers()
    assert plugin_state.get_plugin_manager() is new_manager
    assert new_manager is not None
    assert new_manager.state == PluginManagerState.ACTIVE


def test_sync_reload_rejects_running_event_loop(_isolated_plugin_runtime):
    plugin_file = _isolated_plugin_runtime / "lifecycle.py"
    _write(plugin_file, _basic_plugin())
    plugin_state.reload_plugins()
    active = plugin_state.get_plugin_manager()

    async def call_sync_api():
        with pytest.raises(RuntimeError, match="reload_plugins_async"):
            plugin_state.reload_plugins()

    asyncio.run(call_sync_api())
    assert plugin_state.get_plugin_manager() is active
