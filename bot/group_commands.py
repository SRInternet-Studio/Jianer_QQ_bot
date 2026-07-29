"""群消息命令处理：从 main.py handler 中抽出的独立分支。

每个函数接收必要依赖作为参数，不依赖 main.py 模块作用域。
"""
import datetime
import os
import time

from Tools.capture_screenshot import capture_screenshot


async def cmd_about(actions, Manager, Segments, event, bot_name, bot_name_en, ONE_SLOGAN, version_name):
    """处理 ~关于：渲染 HTML 模板截图后发送图片。"""
    framework = await actions.get_version_info()
    framework = framework.data.raw

    template_path = os.path.join("static", "about_template.html")
    with open(template_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    html_content = html_content.replace("{{bot_name}}", str(bot_name))
    html_content = html_content.replace("{{bot_name_en}}", str(bot_name_en))
    html_content = html_content.replace("{{ONE_SLOGAN}}", str(ONE_SLOGAN))
    html_content = html_content.replace("{{version_name}}", str(version_name))
    html_content = html_content.replace("{{app_name}}", str(framework.get("app_name", "Unknown")))
    html_content = html_content.replace("{{protocol_version}}", str(framework.get("protocol_version", "")))
    html_content = html_content.replace("{{app_version}}", str(framework.get("app_version", "")))
    html_content = html_content.replace("{{year}}", str(datetime.datetime.now().year))

    temp_html_path = os.path.abspath(os.path.join("static", f"about_temp_{int(time.time())}.html"))
    with open(temp_html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    url = f"file:///{temp_html_path.replace(chr(92), '/')}"
    image_path = await capture_screenshot(url, "about_image", "png")

    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(image_path)))

    try:
        os.remove(temp_html_path)
        os.remove(image_path)
    except Exception:
        pass


async def cmd_broadcast_blacklist_menu(actions, Manager, Segments, event, bot_name, bot_name_en, reminder):
    """处理 ~群发黑名单：返回管理面板说明。"""
    await actions.send(group_id=event.group_id, message=Manager.Message(
        Segments.Reply(event.message_id),
        Segments.Text(f'''{bot_name} {bot_name_en} - 群发黑名单管理控制面板
————————————————————
{reminder}列出黑名单 —> 显示所有黑名单群组
{reminder}删除黑名单 +群号 —> 允许群发消息到该群
{reminder}添加黑名单 +群号 —> 禁止群发消息到该群
''')))


async def cmd_ping(actions, Manager, Segments, event):
    """处理 ping。"""
    await actions.send(group_id=event.group_id, message=Manager.Message(
        Segments.Text("pong! 爆炸！v(◦'ωˉ◦)~♡ ")))


async def cmd_grass(actions, Manager, Segments, event):
    """处理 ~生草。"""
    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("🌿")))


async def cmd_generate_placeholder(actions, Manager, Segments, event):
    """处理 ~生成（占位图片）。"""
    await actions.send(group_id=event.group_id, message=Manager.Message(
        Segments.Image(os.path.abspath("./assets/sc114.png"))))


async def cmd_plugin_view(actions, Manager, Segments, event, bot_name, bot_name_en,
                          loaded_plugins, disabled_plugins, failed_plugins,
                          warnings=None):
    """处理 ~插件视角：列出已加载/已禁用/加载失败的插件。"""
    warnings = warnings or []
    status = f'''{bot_name} {bot_name_en} - 插件视角
————————————————————
✅ 已加载插件 ({len(loaded_plugins)}):
{chr(10).join(f"{i+1}. {str(plugin)}" for i, plugin in enumerate(loaded_plugins)) if loaded_plugins else "无"}

❌ 已禁用插件 ({len(disabled_plugins)}):
{chr(10).join(
    f"{i+1}. {str(plugin).replace('d_', '').split('.')[0]}"
    for i, plugin in enumerate(disabled_plugins)) if disabled_plugins else "无"}

⚠️ 加载失败 ({len(failed_plugins)}):
{chr(10).join(f"{i+1}. {str(plugin)}"
    for i, plugin in enumerate(failed_plugins))
if failed_plugins else "无"}

⚠️ 加载警告 ({len(warnings)}):
{chr(10).join(f"{i+1}. {str(warning)}"
    for i, warning in enumerate(warnings))
if warnings else "无"}'''
    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(status)))
