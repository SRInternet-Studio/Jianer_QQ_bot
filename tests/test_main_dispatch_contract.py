from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    start_index = MAIN_SOURCE.index(start)
    end_index = MAIN_SOURCE.index(end, start_index)
    return MAIN_SOURCE[start_index:end_index]


def test_message_pipeline_runs_normal_plugins_before_host_side_effects():
    assert MAIN_SOURCE.index("await observe_plugins(event, actions)") < MAIN_SOURCE.index(
        "elif isinstance(event, Events.PrivateMessageEvent):"
    )

    private_branch = _between(
        "elif isinstance(event, Events.PrivateMessageEvent):",
        "elif isinstance(event, Events.GroupMessageEvent):",
    )
    assert private_branch.index("await execute_plugins(") < private_branch.index(
        "await get_user_nickname("
    )
    assert private_branch.index("await get_user_nickname(") < private_branch.index(
        "await execute_plugin_fallback("
    )

    group_branch = _between(
        "elif isinstance(event, Events.GroupMessageEvent):",
        "@Listener.reg",
    )
    normal_index = group_branch.index("if not plugin_dispatched:")
    mention_fallback_index = group_branch.index(
        "if qq_mentioned_me or feishu_mention_like:"
    )
    nickname_index = group_branch.index("await get_user_nickname(")
    ping_index = group_branch.index("await _bot_group_commands.cmd_ping(")
    emoji_index = group_branch.index("if has_emoji(host_message)")
    fallback_index = group_branch.rindex("await execute_plugin_fallback(")
    assert normal_index < mention_fallback_index < nickname_index
    assert nickname_index < ping_index < fallback_index
    assert normal_index < emoji_index < fallback_index

    mention_block = group_branch[mention_fallback_index:nickname_index]
    assert "await execute_plugin_fallback(" in mention_block
    assert "send_help_visual(" not in mention_block
    assert "QQ 群聊只接受前缀触发 AI" not in group_branch

    compliment_index = group_branch.index(
        'f"{bot_name}真棒" in host_message',
        ping_index,
    )
    ping_block = group_branch[ping_index:compliment_index]
    compliment_block = group_branch[compliment_index:emoji_index]
    assert "\n            return" in ping_block
    assert "\n            return" in compliment_block


def test_group_feishu_binding_commands_fail_closed_on_qq_protocols():
    group_branch = _between(
        "elif isinstance(event, Events.GroupMessageEvent):",
        "@Listener.reg",
    )

    bind_index = group_branch.index('if order.startswith("绑定QQ "):')
    bind_guard = group_branch.index(
        "if not is_feishu_protocol():",
        bind_index,
    )
    bind_write = group_branch.index("bind_feishu_user(", bind_index)
    assert bind_index < bind_guard < bind_write

    query_index = group_branch.index('elif "我的绑定" == order:')
    query_guard = group_branch.index(
        "if not is_feishu_protocol():",
        query_index,
    )
    query_read = group_branch.index("get_bound_qq(", query_index)
    assert query_index < query_guard < query_read
