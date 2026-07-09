import gc
import re
import subprocess

from jianer import common as Manager, segments as Segments
from jianer.plugins import PluginMetadata

from Tools import tools as t
from bot import plugin_state
from plugins.RunCommand.DANGEROUS_PATTERNS import DANGEROUS_PATTERNS
from plugins.RunCommand.execute_command import execute_command


__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-run-command",
    description="Run a server command for root or super users.",
    usage="{reminder}runcommand (命令，必填) —> 通过命令实现更多功能（需要SU）",
    requires={"jianerbot-plugin-alconna"},
)

TRIGGER = "runcommand"


async def dispatch(event, actions):
    if plugin_state.current_stage() != "command":
        return False

    order = plugin_state.current_order()
    if order != TRIGGER and not order.startswith(f"{TRIGGER} "):
        return False

    runtime = plugin_state.get_runtime()
    root_users = [str(item) for item in runtime.get("root_users", [])]
    super_users = [str(item) for item in runtime.get("super_users", [])]
    bot_name = runtime.get("bot_name", "")
    confused_word = runtime.get("confused_word", "{bot_name}不能这么做。")

    if str(event.user_id) not in {*root_users, *super_users}:
        await actions.send(
            group_id=event.group_id,
            message=Manager.Message(Segments.Text(confused_word.format(bot_name=bot_name))),
        )
        return True

    command = order.removeprefix(TRIGGER).strip()
    if not command:
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("命令为空。")))
        return True

    if hasattr(t, "replace_at_with_nickname"):
        command = await t.replace_at_with_nickname(command, Manager, Segments, actions)
    command_lower = command.lower()

    print(f"检查并执行命令: {command}")
    if root_users:
        admin_log = f"用户 {await get_user_nickname(event.user_id, actions)} 在 {event.time_str} 执行了以下命令：\n{command}"
        await actions.send(user_id=root_users[0], message=Manager.Message(Segments.Text(admin_log)))

    blocked_pattern = _match_dangerous(command_lower)
    if blocked_pattern:
        print(f"检测到危险命令: {blocked_pattern}")
        await actions.send(
            group_id=event.group_id,
            message=Manager.Message(Segments.Text(f"命令执行结果:\n❌ ERROR 危险命令，已屏蔽。\nℹ️ INFO 不被允许的命令：{command}")),
        )
        return True

    result = execute_command(command, subprocess)
    if result["returncode"] == 0:
        if str(command_lower.split(" ")[0]) == "echo":
            message = str(result["stdout"]).strip("\n")
        else:
            message = f"命令执行结果:\nℹ️ INFO 执行成功\nℹ️ INFO {result['stdout']}."
    elif result["stderr"]:
        message = f"命令执行结果:\n❌ ERROR 执行失败，命令可能有误\nℹ️ INFO {result['stderr']}."
    else:
        message = f"命令执行结果:\n❌ ERROR 执行失败，命令可能有误\nℹ️ INFO {result['stderr']}.\n❌ ERROR 返回码：{result['returncode']}."

    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(message)))
    return True


def _match_dangerous(command_lower: str) -> str | None:
    for pattern in DANGEROUS_PATTERNS:
        try:
            re.compile(pattern)
            if re.search(pattern, command_lower):
                return pattern
        except re.error as exc:
            print(f"无效屏蔽词条: {pattern}\n错误: {exc}")
    return None


async def get_user_info(uid, actions):
    try:
        gc.collect()
        info = Manager.Ret.fetch(await actions.custom.get_stranger_info(user_id=uid, no_cache=True))
        if "nickname" not in info.data.raw:
            raise ValueError(f"{uid} is not a valid user ID.")
        return True, info.data.raw
    except Exception as exc:
        print(f"tools: 获取用户 {uid} 信息失败: {exc}")
        return False, str(uid)


async def get_user_nickname(uid, actions) -> str:
    success, user_info = await get_user_info(uid, actions)
    if success:
        return f"@{user_info['nickname']}({uid})"
    return str(uid)
