import time

from arclet.alconna import Alconna, Args, MultiVar
from jianer.plugins import PluginMetadata
from jianer.plugins.builtin.alconna import Command, Receipt, Target, UniMessage

from bot import plugin_state


__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-generate-acg",
    description="Generate ACG images from LoliAPI.",
    usage="{reminder}生图 ACG (随机/电脑壁纸/手机壁纸/头像/背景) —> 制作二次元壁纸\n{reminder}生图 ACG 帮助 —> 查看生图帮助菜单",
    requires={"jianerbot-plugin-alconna"},
)

COOLDOWN_SECONDS = 18

IMAGE_APIS = {
    "随机": "https://www.loliapi.com/acg/",
    "电脑壁纸": "https://www.loliapi.com/acg/pc/",
    "手机壁纸": "https://www.loliapi.com/acg/pe/",
    "头像": "https://www.loliapi.com/acg/pp/",
    "背景": "https://www.loliapi.com/bg/",
}


async def generate_acg(result: str, event, actions) -> bool:
    """Generate an ACG image for a command or another project-side shortcut."""
    runtime = plugin_state.get_runtime()
    result = result.strip()
    bot_name = runtime.get("bot_name", "")
    reminder = runtime.get("reminder", "")
    cooldowns = runtime.get("cooldowns", {})
    user_id = event.user_id
    current_time = time.time()
    target = Target.from_event(event)

    if _is_in_cooldown(user_id, current_time, cooldowns, runtime):
        time_remaining = COOLDOWN_SECONDS - (current_time - cooldowns[user_id])
        await UniMessage.text(
            f"18秒个人cd，请等待 {time_remaining:.1f} 秒后重试"
        ).send(
            target=target,
            actions=actions,
            event=event,
        )
        return True

    loading = None
    try:
        loading = await UniMessage.text(
            f"{bot_name}正在制作超级好看的二次元壁纸 ヾ(≧▽≦*)o"
        ).send(
            target=target,
            actions=actions,
            event=event,
        )

        if "帮助" in result or not result:
            await _recall(loading)
            await UniMessage.text(_help_text(bot_name, reminder)).send(
                target=target,
                actions=actions,
                event=event,
            )
            return True

        api = next((url for keyword, url in IMAGE_APIS.items() if keyword in result), "")
        if not api:
            await _recall(loading)
            await UniMessage.text("指定的类型不存在").send(
                target=target,
                actions=actions,
                event=event,
            )
            await UniMessage.text(_help_text(bot_name, reminder)).send(
                target=target,
                actions=actions,
                event=event,
            )
            return True

        print(f"使用 LoliAPI: {api}")
        message = UniMessage.image(api)
        message.append(UniMessage.text(f"{result}生成 结束！✧*。٩(>ω<*)و✧*。"))
        await message.send(
            target=target,
            actions=actions,
            event=event,
        )
        await _recall(loading)
        cooldowns[user_id] = current_time
    except Exception as exc:
        await _recall(loading)
        await UniMessage.text(
            f"因为 {type(exc)}\n{bot_name}不能生成图片了，请稍候再尝试吧 o(TヘTo)"
        ).send(
            target=target,
            actions=actions,
            event=event,
        )
    return True


_reminder = str(plugin_state.get_runtime().get("reminder", ""))
_acg_command = Alconna(
    f"{_reminder}生图 ACG",
    Args["image_type", MultiVar(str), ""],
)


@Command(_acg_command).handle()
async def _handle_acg(image_type: str, event, actions):
    if getattr(event, "group_id", None) is None:
        return False
    return await generate_acg(image_type, event, actions)


def _is_in_cooldown(user_id, current_time: float, cooldowns: dict, runtime: dict) -> bool:
    if user_id not in cooldowns:
        return False
    if current_time - cooldowns[user_id] >= COOLDOWN_SECONDS:
        return False
    privileged = {
        *[str(item) for item in runtime.get("super_users", [])],
        *[str(item) for item in runtime.get("manage_users", [])],
        *[str(item) for item in runtime.get("root_users", [])],
    }
    return str(user_id) not in privileged


async def _recall(receipt: Receipt | None) -> None:
    if receipt is None or receipt.message_id is None:
        return
    try:
        await receipt.recall()
    except Exception:
        pass


def _help_text(bot_name: str, reminder: str) -> str:
    return f"""\
{bot_name}可生成精美 ACG 壁纸噢~ヾ(≧∪≦*)ノ〃
{reminder}生图 ACG 随机 -> 根据设备自动适配
{reminder}生图 ACG 电脑壁纸 -> 电脑端高清壁纸
{reminder}生图 ACG 手机壁纸 -> 移动端适配壁纸
{reminder}生图 ACG 头像 -> 适合做头像的图片
{reminder}生图 ACG 背景 -> 随机二次元背景

举个例子：{reminder}生图 ACG 随机 -> {bot_name}生成自适应二次元壁纸
快来试试吧Ｏ(≧▽≦)Ｏ"""
