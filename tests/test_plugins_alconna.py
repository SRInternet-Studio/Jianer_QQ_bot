import ast
import asyncio
import logging
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import plugin_state
from jianer import segments as Segments


PLUGIN_ENTRIES = [
    ROOT / "plugins" / "CheckAccount.py",
    ROOT / "plugins" / "CheckGroup.py",
    ROOT / "plugins" / "GenerateFromACG.py",
    ROOT / "plugins" / "GenerateFromPixiv.py",
    ROOT / "plugins" / "LikePlugin.py",
    ROOT / "plugins" / "AdvancedQuote" / "setup.py",
    ROOT / "plugins" / "JianerAI" / "setup.py",
    ROOT / "plugins" / "MaimaiDX" / "setup.py",
    ROOT / "plugins" / "RunCommand" / "setup.py",
]

EXPECTED_PLUGIN_IDS = {
    "jianerbot-plugin-alconna",
    "jianerbot-plugin-advanced-quote",
    "jianerbot-plugin-check-account",
    "jianerbot-plugin-check-group",
    "jianerbot-plugin-generate-acg",
    "jianerbot-plugin-generate-pixiv",
    "jianerbot-plugin-jianer-ai",
    "jianerbot-plugin-like",
    "jianerbot-plugin-maimaidx",
    "jianerbot-plugin-run-command",
}


class FakeActions:
    def __init__(self):
        self.sent = []
        self.deleted = []
        self.custom = SimpleNamespace()

    async def send(self, message, **target):
        self.sent.append((target, message))
        return SimpleNamespace(
            data=SimpleNamespace(message_id=len(self.sent)),
        )

    async def del_message(self, message_id):
        self.deleted.append(message_id)

    async def get_version_info(self):
        return SimpleNamespace(data=SimpleNamespace(raw={"app_name": "NapCat"}))

    async def get_msg(self, message_id):
        return SimpleNamespace(data=None)


def _event(text: str, *, message=None, private: bool = False):
    values = {
        "msg_str": text,
        "message": [] if message is None else message,
        "user_id": 2,
        "self_id": 999,
        "message_id": "message-1",
        "time_str": "12:00:00",
    }
    if not private:
        values["group_id"] = 100
    return SimpleNamespace(**values)


def _dispatch(event, actions, text: str | None = None) -> bool:
    return asyncio.run(
        plugin_state.dispatch_plugins(
            event,
            actions,
            message_text=event.msg_str if text is None else text,
        )
    )


@pytest.fixture
def loaded_plugins(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin_state, "PLUGIN_FOLDER", str(ROOT / "plugins"))
    plugin_state.configure(
        config=SimpleNamespace(
            others={
                "jianer_ai_db_path": str(tmp_path / "jianer_ai.db"),
            }
        ),
        logger=logging.getLogger("plugins_alconna_test"),
        reminder="~",
        bot_name="Jianer",
        bot_name_en="Jianer",
        one_slogan="test",
        confused_word="{bot_name} cannot do that",
        root_users=["1"],
        cooldowns={},
        cooldowns1={},
    )
    plugin_state.set_auth_snapshot(
        admins=["1"],
        supers=["1"],
        root_users=["1"],
        super_users=["1"],
        manage_users=[],
    )
    plugin_state.set_generating(False)
    result = plugin_state.reload_plugins()
    assert result.failed == []
    module = plugin_state.get_plugin_module("jianerbot-plugin-jianer-ai")
    service = module.get_service()
    assert service is not None
    assert service.options.database_path == (tmp_path / "jianer_ai.db").resolve()
    return result


def test_all_plugin_entries_use_alconna_contract():
    for path in PLUGIN_ENTRIES:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assigned_names = {
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        async_names = {
            node.name for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
        }

        assert "__plugin_meta__" in assigned_names, path
        if path.parent.name == "MaimaiDX":
            command_source = "\n".join(
                command.read_text(encoding="utf-8")
                for command in (path.parent / "commands").glob("*.py")
            )
            assert "@Command" in command_source, path
            assert "from plugins.MaimaiDX import commands" in source, path
        else:
            assert "@Command" in source, path
        assert "jianerbot-plugin-alconna" in source, path
        assert "dispatch" not in async_names, path


def test_plugin_manager_loads_builtin_and_all_business_plugins(loaded_plugins):
    assert set(loaded_plugins.loaded) == EXPECTED_PLUGIN_IDS
    assert loaded_plugins.warnings == []
    help_text = plugin_state.plugin_help_text()
    for command in (
        "~开",
        "~开群",
        "~生图 ACG",
        "~生图 Pixiv",
        "赞我",
        "~名人名言",
        "~ai管理菜单",
        "帮助maimaiDX",
        "~runcommand",
    ):
        assert command in help_text


def test_maimaidx_ai_tools_register_with_jianer_ai(loaded_plugins):
    module = plugin_state.get_plugin_module("jianerbot-plugin-jianer-ai")
    service = module.get_service()
    assert service is not None
    assert {
        name
        for name in service.tools._tools
        if name.startswith("maimaidx_")
    } == {
        "maimaidx_b50",
        "maimaidx_song_search",
        "maimaidx_song_info",
        "maimaidx_player_song_score",
        "maimaidx_rating_ranking",
    }


def test_maimaidx_registers_every_pinned_upstream_command_and_alias(
    loaded_plugins,
):
    from jianer.plugins.builtin import alconna

    manager = plugin_state.get_plugin_manager()
    matchers = alconna._MATCHERS[manager.manager_id][
        "jianerbot-plugin-maimaidx"
    ]
    expected_names = {
        "AP50",
        "B50",
        "GINFO",
        "Ginfo",
        "INFO",
        "Info",
        "MINFO",
        "Minfo",
        "ap50",
        "b50",
        "ginfo",
        "info",
        "lxbind",
        "minfo",
        "主题",
        "今日舞萌",
        "关闭mai猜歌",
        "分数线",
        "同意别名",
        "同意别称",
        "增加别名",
        "增添别名",
        "帮助maimaiDX",
        "帮助maimaidx",
        "开启mai猜歌",
        "当前别名投票",
        "当前别称投票",
        "当前投票",
        "我的排名",
        "数据源",
        "更新maimai数据",
        "更新别名库",
        "更新完成表",
        "更新定数表",
        "查看排名",
        "添加别名",
        "添加别称",
        "添加本地别名",
        "添加本地别称",
        "牌子条件",
        "猜曲绘",
        "猜歌",
        "申请别名",
        "绑定lx",
        "绑定落雪",
        "重置猜歌",
        "项目地址maimaiDX",
        "项目地址maimaidx",
    }

    assert len(matchers) == 80
    assert {matcher.command.name for matcher in matchers} == expected_names


def test_maimaidx_command_dispatches_through_builtin_alconna(loaded_plugins):
    actions = FakeActions()
    assert _dispatch(_event("项目地址maimaiDX"), actions) is True
    assert len(actions.sent) == 1
    response = str(actions.sent[0][1])
    assert "nonebot-plugin-maimaidx" in response
    assert "v3.0.13 / 83a1bee" in response


def test_repeated_hot_reload_keeps_alconna_registry_bounded(loaded_plugins):
    from arclet.alconna import command_manager

    initial_count = command_manager.current_count
    for _ in range(4):
        result = plugin_state.reload_plugins()
        assert result.failed == []
        assert "jianerbot-plugin-maimaidx" in result.loaded
    assert command_manager.current_count == initial_count


def test_jianer_ai_model_command_receives_plain_string(loaded_plugins):
    module = plugin_state.get_plugin_module("jianerbot-plugin-jianer-ai")
    service = module.get_service()
    model = next(iter(service.providers.list_models()))
    actions = FakeActions()

    assert _dispatch(_event(f"~切换AI {model}"), actions) is True
    assert len(actions.sent) == 1
    response = str(actions.sent[0][1])
    assert f"({model})" in response
    assert "找不到AI配置" not in response
    assert f"('{model}',)" not in response


def test_jianer_ai_persona_command_and_fallback_shortcut(loaded_plugins):
    actions = FakeActions()
    assert _dispatch(_event("~切换角色 机娘"), actions) is True
    assert "是机娘desu~！" in str(actions.sent[-1][1])

    shortcut_actions = FakeActions()
    assert asyncio.run(
        plugin_state.dispatch_fallback(
            _event("~机娘"),
            shortcut_actions,
            message_text="~机娘",
        )
    ) is True
    assert "是机娘desu~！" in str(shortcut_actions.sent[-1][1])

    unrelated_actions = FakeActions()
    assert asyncio.run(
        plugin_state.dispatch_fallback(
            _event("~这不是预设"),
            unrelated_actions,
            message_text="~这不是预设",
        )
    ) is False
    assert unrelated_actions.sent == []


def test_jianer_ai_commands_respect_group_location_blacklist(loaded_plugins):
    module = plugin_state.get_plugin_module("jianerbot-plugin-jianer-ai")
    service = module.get_service()
    original_options = service.options
    actions = FakeActions()
    try:
        service.options = replace(
            original_options,
            blocked_group_ids=frozenset({"100"}),
        )
        assert _dispatch(_event("~ai管理菜单"), actions) is True
    finally:
        service.options = original_options

    assert len(actions.sent) == 1
    assert "Error 403" in str(actions.sent[0][1])


def test_account_at_and_group_validation_commands(loaded_plugins, monkeypatch):
    account = plugin_state.get_plugin_module("jianerbot-plugin-check-account")
    captured = []

    async def fake_user_info(user_id):
        captured.append(user_id)
        return {"user_id": user_id}

    monkeypatch.setattr(account, "get_user_info_from_ws", fake_user_info)
    monkeypatch.setattr(
        account,
        "parser_user_info_napcat",
        lambda *_: ("https://example.com/avatar.png", "account-info"),
    )

    actions = FakeActions()
    account_event = _event("~开 @123", message=[Segments.At("123")])
    assert _dispatch(account_event, actions) is True
    assert captured == [123]
    assert len(actions.sent) == 1

    group_actions = FakeActions()
    assert _dispatch(_event("~开群 invalid"), group_actions) is True
    assert len(group_actions.sent) == 1

    wrong_prefix_actions = FakeActions()
    assert _dispatch(_event("!开 123"), wrong_prefix_actions) is False
    assert wrong_prefix_actions.sent == []


def test_generators_match_commands_and_recall_loading_receipts(
    loaded_plugins,
    monkeypatch,
):
    actions = FakeActions()
    assert _dispatch(_event("~生图 ACG 不存在"), actions) is True
    assert len(actions.sent) == 3
    assert actions.deleted == [1]

    no_arg_actions = FakeActions()
    assert _dispatch(_event("~生图 ACG"), no_arg_actions) is True
    assert len(no_arg_actions.sent) == 2
    assert no_arg_actions.deleted == [1]

    runtime = plugin_state.get_runtime()
    runtime["cooldowns"][2] = time.time()
    cooldown_actions = FakeActions()
    assert _dispatch(_event("~生图 ACG 随机"), cooldown_actions) is True
    assert len(cooldown_actions.sent) == 1
    runtime["cooldowns"].clear()

    pixiv = plugin_state.get_plugin_module("jianerbot-plugin-generate-pixiv")

    async def no_pixiv_result(tags_text):
        assert tags_text == "cat&sky"
        return None

    monkeypatch.setattr(pixiv, "_fetch_pixiv", no_pixiv_result)
    pixiv_actions = FakeActions()
    assert _dispatch(_event("~生图 Pixiv cat&sky"), pixiv_actions) is True
    assert len(pixiv_actions.sent) == 2
    assert pixiv_actions.deleted == [1]
    assert plugin_state.is_generating() is False

    no_pixiv_arg_actions = FakeActions()
    assert _dispatch(_event("~生图 Pixiv"), no_pixiv_arg_actions) is True
    assert len(no_pixiv_arg_actions.sent) == 1

    plugin_state.set_generating(True)
    locked_actions = FakeActions()
    assert _dispatch(_event("~生图 Pixiv cat"), locked_actions) is True
    assert len(locked_actions.sent) == 1
    assert plugin_state.is_generating() is True
    plugin_state.set_generating(False)


def test_like_alias_private_target_quote_and_runcommand_guards(
    loaded_plugins,
    monkeypatch,
):
    like = plugin_state.get_plugin_module("jianerbot-plugin-like")

    monkeypatch.setattr(like.like_manager, "can_like_today", lambda _: False)
    limited_actions = FakeActions()
    assert _dispatch(_event("赞我"), limited_actions) is True
    assert len(limited_actions.sent) == 1

    async def fake_like(event, actions, bot_name, *, action):
        actions.alias = action
        return True

    monkeypatch.setattr(like, "_send_like", fake_like)
    alias_actions = FakeActions()
    assert _dispatch(_event("超湿我"), alias_actions) is True
    assert alias_actions.alias == "超"

    private_actions = FakeActions()
    assert _dispatch(_event("~点赞信息", private=True), private_actions) is True
    assert private_actions.sent[0][0] == {"user_id": 2}

    quote_actions = FakeActions()
    assert _dispatch(_event("~名人名言"), quote_actions) is True
    assert len(quote_actions.sent) == 1

    quote = plugin_state.get_plugin_module("jianerbot-plugin-advanced-quote")

    quote_response = SimpleNamespace(
        message=[Segments.Text("quoted text")],
        sender=SimpleNamespace(
            user_id="ou-feishu-user",
            nickname="Feishu User",
            card="",
        ),
    )

    async def quote_message(message_id):
        assert message_id == "quoted"
        return SimpleNamespace(data=quote_response)

    async def render_quote(
        user_message,
        actions,
        image_url,
        manager,
        segments,
        *,
        content,
    ):
        assert isinstance(user_message[0], Segments.Reply)
        assert content.data is quote_response
        return Segments.Image("file:///quote.png")

    monkeypatch.setattr(quote.Quote, "handle", render_quote)
    quoted_actions = FakeActions()
    quoted_actions.get_msg = quote_message
    quoted_event = _event("~名人名言", message=[Segments.Reply("quoted")])
    assert _dispatch(quoted_event, quoted_actions) is True
    assert len(quoted_actions.sent) == 1
    assert quoted_actions.sent[0][0] == {"group_id": 100}

    async def broken_quote(*args, **kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr(quote.Quote, "handle", broken_quote)
    failed_actions = FakeActions()
    failed_actions.get_msg = quote_message
    assert _dispatch(quoted_event, failed_actions) is True
    assert failed_actions.sent[-1][0] == {"group_id": 100}
    assert "名人名言生成失败" in str(failed_actions.sent[-1][1])

    run_actions = FakeActions()
    assert _dispatch(_event("~runcommand echo hello"), run_actions) is True
    assert len(run_actions.sent) == 1

    run_command = plugin_state.get_plugin_module("jianerbot-plugin-run-command")
    plugin_state.set_auth_snapshot(
        admins=["1", "2"],
        supers=["1", "2"],
        root_users=["1"],
        super_users=["2"],
        manage_users=[],
    )

    async def nickname(*_):
        return "@tester(2)"

    monkeypatch.setattr(run_command, "get_user_nickname", nickname)
    monkeypatch.setattr(
        run_command,
        "execute_command",
        lambda *_: pytest.fail("dangerous command reached subprocess execution"),
    )
    blocked_actions = FakeActions()
    assert _dispatch(_event("~runcommand rm -rf /"), blocked_actions) is True
    assert len(blocked_actions.sent) == 2


def test_like_plugin_stops_before_unsupported_milky_endpoint(
    loaded_plugins,
    monkeypatch,
):
    like = plugin_state.get_plugin_module("jianerbot-plugin-like")
    monkeypatch.setattr(like.like_manager, "can_like_today", lambda _: True)
    calls = []

    async def send_like(**kwargs):
        calls.append(kwargs)

    actions = FakeActions()
    actions.protocol = "milky"
    actions.custom.send_like = send_like

    assert asyncio.run(
        like._send_like(_event("赞我"), actions, "Jianer", action="赞")
    ) is True
    assert calls == []
    assert len(actions.sent) == 1
    assert "不支持QQ名片点赞" in str(actions.sent[0][1])


def test_like_plugin_uses_one_bounded_onebot_action(
    loaded_plugins,
    monkeypatch,
):
    like = plugin_state.get_plugin_module("jianerbot-plugin-like")
    monkeypatch.setattr(like.like_manager, "can_like_today", lambda _: True)
    monkeypatch.setattr(like.like_manager, "get_remaining_likes", lambda _: 0)
    recorded = []
    monkeypatch.setattr(
        like.like_manager,
        "record_like",
        lambda user_id, times: recorded.append((user_id, times)),
    )
    calls = []

    async def send_like(**kwargs):
        calls.append(kwargs)

    actions = FakeActions()
    actions.protocol = "onebot"
    actions.custom.send_like = send_like

    assert asyncio.run(
        like._send_like(_event("赞我"), actions, "Jianer", action="赞")
    ) is True
    assert calls == [{"user_id": 2, "times": like.DAILY_LIMIT}]
    assert recorded == [(2, like.DAILY_LIMIT)]
    assert len(actions.sent) == 2


def test_runtime_reminder_change_rebuilds_all_command_matchers(
    loaded_plugins,
):
    runtime_config = plugin_state.get_runtime()["config"]
    plugin_state.configure(
        config=runtime_config,
        logger=logging.getLogger("plugins_alconna_dynamic_reminder_test"),
        reminder="!",
        bot_name="Jianer",
        bot_name_en="Jianer",
        one_slogan="test",
        confused_word="{bot_name} cannot do that",
        root_users=["1"],
        cooldowns={},
        cooldowns1={},
    )
    plugin_state.set_auth_snapshot(
        admins=["1"],
        supers=["1"],
        root_users=["1"],
        super_users=["1"],
        manage_users=[],
    )
    result = plugin_state.reload_plugins()
    assert result.failed == []

    actions = FakeActions()
    assert _dispatch(_event("!开群 invalid"), actions) is True
    assert len(actions.sent) == 1

    stale_prefix_actions = FakeActions()
    assert _dispatch(_event("~开群 invalid"), stale_prefix_actions) is False
    assert stale_prefix_actions.sent == []

    like = plugin_state.get_plugin_module("jianerbot-plugin-like")
    assert "!点赞信息" in like.EARLY_COMMANDS
    assert "~点赞信息" not in like.EARLY_COMMANDS


@pytest.mark.parametrize(
    "command",
    [
        "~开 123",
        "~开群 100",
        "~生图 ACG 随机",
        "~生图 Pixiv cat",
        "~名人名言",
        "~runcommand echo hello",
    ],
)
def test_group_only_commands_remain_ignored_in_private_messages(
    loaded_plugins,
    command,
):
    actions = FakeActions()
    assert _dispatch(_event(command, private=True), actions) is False
    assert actions.sent == []
