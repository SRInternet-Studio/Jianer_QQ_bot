"""简儿记忆子命令路由。"""
import datetime
import re


def _parse_interval_seconds(s: str) -> int:
    s = (s or "").strip().lower()
    m = re.match(r"^(\d+)\s*([smhd]?)$", s)
    if not m:
        return 0
    n = int(m.group(1))
    unit = m.group(2)
    if unit in ("s", ""):
        return n
    if unit == "m":
        return n * 60
    if unit == "h":
        return n * 3600
    if unit == "d":
        return n * 86400
    return 0


async def cmd_memory(actions, Manager, Segments, event, user_message,
                     reminder, memory_service, memory_mode, memory_db_path):
    cmd = user_message[len(reminder):].strip()
    parts = [p for p in cmd.split() if p]
    action = parts[1] if len(parts) >= 2 else "帮助"

    async def _send(text):
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(text)))

    if action in ("帮助", "help"):
        await _send(f"""简儿记忆
————————————————————
记忆AI配置: {memory_mode}
数据库: {memory_db_path}

指令:
{reminder}简儿记忆 状态
{reminder}简儿记忆 开启 / 关闭
{reminder}简儿记忆 间隔 6h/30m/3600
{reminder}简儿记忆 立即生成
""")
    elif action == "状态":
        st = await memory_service.get_status(event.group_id, event.user_id, False)
        if not st:
            await _send("未找到记忆状态。")
            return
        last_at = st.get("last_generated_at", 0) or 0
        last_at_str = "从未" if int(last_at) <= 0 else datetime.datetime.fromtimestamp(int(last_at)).strftime("%Y-%m-%d %H:%M:%S")
        await _send(f"""简儿记忆状态
————————————————————
开启: {bool(st.get("enabled", 0))}
间隔(秒): {st.get("interval_seconds", 0)}
上次生成: {last_at_str}
原始记录: {st.get("raw_count", 0)} (+{st.get("new_raw_count", 0)})
个人/本群记忆: {st.get("mem_count", 0)}
全局记忆: {st.get("global_count", 0)}
""")
    elif action == "开启":
        await memory_service.set_enabled(event.group_id, event.user_id, False, True)
        await _send("已开启简儿记忆。")
    elif action == "关闭":
        await memory_service.set_enabled(event.group_id, event.user_id, False, False)
        await _send("已关闭简儿记忆。")
    elif action == "间隔":
        if len(parts) < 3:
            await _send("用法：#简儿记忆 间隔 6h/30m/3600")
            return
        seconds = _parse_interval_seconds(parts[2])
        if seconds <= 0:
            await _send("间隔格式无效。")
            return
        await memory_service.set_interval_seconds(event.group_id, event.user_id, False, seconds)
        await _send(f"已设置简儿记忆间隔为 {seconds} 秒。")
    elif action == "立即生成":
        ok = await memory_service.generate_now(event.group_id, event.user_id, False)
        await _send("已生成一轮简儿记忆。" if ok else "暂无足够新增聊天记录生成记忆。")
    else:
        await _send("指令不支持，发送 #简儿记忆 帮助。")
