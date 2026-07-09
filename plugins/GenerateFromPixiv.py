import datetime
import os
import time
import traceback
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
from jianer import common as Manager, segments as Segments
from jianer.plugins import PluginMetadata

from Tools.capture_screenshot import capture_screenshot
from bot import plugin_state


__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-generate-pixiv",
    description="Fetch a Pixiv image through the lolicon API.",
    usage="{reminder}生图 Pixiv (标签，必填，用&分割) —> 浏览P站",
    requires={"jianerbot-plugin-alconna"},
)

TRIGGER = "生图 Pixiv "
COOLDOWN_SECONDS = 18
CENSORED_WORDS = {
    "r-18",
    "r-18g",
    "r18",
    "r18g",
    "r_18",
    "r_18g",
    "nsfw",
    "成人向",
    "即将脱落的胸罩",
}


async def dispatch(event, actions):
    if plugin_state.current_stage() != "command":
        return False

    order = plugin_state.current_order()
    if not order.startswith(TRIGGER):
        return False

    runtime = plugin_state.get_runtime()
    if plugin_state.is_generating():
        await actions.send(
            group_id=event.group_id,
            message=Manager.Message(Segments.Text("前面还有一张图在生成哦，请稍候再试吧 (*/ω＼*)")),
        )
        return True

    tags_text = order[len(TRIGGER) :].strip()
    if not tags_text:
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("没有参数。")))
        return True

    cooldowns = runtime.get("cooldowns1", {})
    user_id = event.user_id
    current_time = time.time()
    if user_id in cooldowns and current_time - cooldowns[user_id] < COOLDOWN_SECONDS:
        time_remaining = COOLDOWN_SECONDS - (current_time - cooldowns[user_id])
        await actions.send(
            group_id=event.group_id,
            message=Manager.Message(Segments.Text(f"18秒个人cd，请等待 {time_remaining:.1f} 秒后重试")),
        )
        return True

    bot_name = runtime.get("bot_name", "")
    loading = await actions.send(
        group_id=event.group_id,
        message=Manager.Message(Segments.Text(f"{bot_name}正在从 Pixiv 生成 ヾ(≧▽≦*)o")),
    )
    plugin_state.set_generating(True)
    screenshot_path = None
    try:
        data = await _fetch_pixiv(tags_text)
        if not data:
            await actions.send(
                group_id=event.group_id,
                message=Manager.Message(Segments.Text(f"你给{bot_name}的标签太严格啦，换几个标签试试吧。")),
            )
            return True

        if _is_censored(data.get("tags", [])):
            await actions.send(
                group_id=event.group_id,
                message=Manager.Message(Segments.Text(f"你要的图片实在太涩啦，{bot_name}都不敢看了。")),
            )
            return True

        url = str(data["urls"]["original"])
        screenshot_path = await capture_screenshot(url, "pixiv_image", "png")
        await actions.send(
            group_id=event.group_id,
            message=Manager.Message(Segments.Image(_file_uri(screenshot_path))),
        )
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(_format_info(data))))
        cooldowns[user_id] = current_time
    except Exception:
        print(traceback.format_exc())
        await actions.send(
            group_id=event.group_id,
            message=Manager.Message(Segments.Text(f"{bot_name}生成图片失败了，再试一次吧。")),
        )
    finally:
        await _delete_message(actions, loading)
        plugin_state.set_generating(False)
        if screenshot_path and os.path.exists(screenshot_path):
            os.remove(screenshot_path)

    return True


async def _fetch_pixiv(tags_text: str) -> dict | None:
    params = [("num", "1"), ("r18", "0"), ("excludeAI", "false")]
    params.extend(("tag", tag.strip()) for tag in tags_text.split("&") if tag.strip())
    url = "https://api.lolicon.app/setu/v2?" + urlencode(params)
    print(url)

    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=False),
        timeout=aiohttp.ClientTimeout(10),
    ) as session:
        async with session.get(url=url) as response:
            payload = await response.json()
    data = payload.get("data") or []
    return data[0] if data else None


def _is_censored(tags) -> bool:
    normalized = {str(tag).lower() for tag in tags}
    return bool(normalized & CENSORED_WORDS)


def _format_info(data: dict) -> str:
    source_url = data["urls"]["original"].replace("pixiv.t.sr-studio.top", "i.pximg.net")
    created = datetime.datetime.fromtimestamp(data["uploadDate"] / 1000).strftime("%Y-%m-%d")
    ai_type = "是" if data.get("aiType") == 1 else "否"
    return f"""\
标题：{data['title']}
Pixiv ID：{data['pid']}
作者：{data['author']}
作者ID：{data['uid']}
AI参与：{ai_type}
创作时间：{created}
标签：{data['tags']}
源图：{source_url}"""


def _file_uri(path: str) -> str:
    if path.startswith(("http://", "https://", "file://", "base64://")):
        return path
    return Path(path).resolve().as_uri()


async def _delete_message(actions, receipt):
    message_id = getattr(getattr(receipt, "data", None), "message_id", None)
    if message_id is not None:
        try:
            await actions.del_message(message_id)
        except Exception:
            pass
