from Tools.log_helper import project_log, LOG_LEVELS
from Hyper import Configurator
import json
import os
import random
from datetime import datetime
import httpx
import asyncio

Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

TRIGGHT_KEYWORD = "Any"
HELP_MESSAGE = f"签到 -> 签到获取积分和好感度"

DEFAULT_CONFIG = {
    "好感度": {
        "min": 1,
        "max": 10
    },
    "积分": {
        "min": 10,
        "max": 100
    },
    "数据存储路径": "./data/check_in/",
    "签到模式": "text",
    "模板文件": "template.html"
}

class CheckInManager:
    def __init__(self):
        try:
            os.makedirs("./data/check_in/users/", exist_ok=True)
            self.config = self._load_or_create_config()
            self.command_file = os.path.join(self.config["数据存储路径"], "custom_commands.json")
            self.custom_commands = self._load_custom_commands()
            template_path = os.path.join(self.config["数据存储路径"], self.config["模板文件"])
            if not os.path.exists(template_path):
                self._create_default_template(template_path)
            self.browser_lock = asyncio.Lock()
            self.browser = None
            self.page = None
            self.playwright = None
        except Exception as e:
            project_log(f"[签到系统]初始化失败: {e}", level=LOG_LEVELS.ERROR)
            project_log(f"[签到系统]当前工作目录: {os.getcwd()}")
            project_log(f"[签到系统]配置路径: {os.path.abspath('./data/check_in/')}")
            raise e

    def _load_custom_commands(self):
        if os.path.exists(self.command_file):
            try:
                with open(self.command_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                project_log(f"[签到系统]加载自定义签到指令失败: {e}", level=LOG_LEVELS.ERROR)
        return ["签到"]

    def _save_custom_commands(self):
        try:
            with open(self.command_file, "w", encoding="utf-8") as f:
                json.dump(self.custom_commands, f, ensure_ascii=False, indent=2)
        except Exception as e:
            project_log(f"[签到系统]保存自定义签到指令失败: {e}", level=LOG_LEVELS.ERROR)

    def add_command(self, cmd: str) -> bool:
        cmd = cmd.strip()
        if cmd and cmd not in self.custom_commands:
            self.custom_commands.append(cmd)
            self._save_custom_commands()
            return True
        return False

    def remove_command(self, cmd: str) -> bool:
        cmd = cmd.strip()
        if cmd in self.custom_commands and cmd != "签到":
            self.custom_commands.remove(cmd)
            self._save_custom_commands()
            return True
        return False

    def get_commands(self):
        return self.custom_commands

    def _load_or_create_config(self):
        config_path = os.path.join("./data/check_in/", "check_in_config.json")
        try:
            if not os.path.exists(config_path):
                os.makedirs(os.path.dirname(config_path), exist_ok=True)
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
                project_log(f"[签到系统]已创建默认配置文件: {config_path}")
            with open(config_path, "r", encoding="utf-8") as f:
                loaded_config = json.load(f)
                for key, value in DEFAULT_CONFIG.items():
                    if key not in loaded_config:
                        loaded_config[key] = value
                return loaded_config
        except Exception as e:
            project_log(f"[签到系统]配置文件操作失败: {e}", level=LOG_LEVELS.ERROR)
            return DEFAULT_CONFIG

    def _get_user_data_path(self, user_id: str) -> str:
        return os.path.join(self.config["数据存储路径"], "users", f"{user_id}.json")

    def _load_user_data(self, user_id: str) -> dict:
        path = self._get_user_data_path(user_id)
        if not os.path.exists(path):
            return {
                "total_days": 0,
                "好感度": 0,
                "积分": 0,
                "last_check": "",
            }
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_user_data(self, user_id: str, data: dict):
        path = self._get_user_data_path(user_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _load_or_create_total_data(self):
        os.makedirs(self.config["数据存储路径"], exist_ok=True)
        path = os.path.join(self.config["数据存储路径"], self.config["总计数据文件"])
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=2)
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_or_create_daily_data(self):
        today = datetime.now().strftime("%Y-%m-%d")
        
        if self.last_date != today or self.daily_data is None:
            self.daily_data = {"count": 0, "users": []}
            self.last_date = today
            self._save_daily_data()
            project_log(f"[签到系统]创建新的每日签到数据: {today}")
            
        return self.daily_data

    def _save_total_data(self):
        path = os.path.join(self.config["数据存储路径"], self.config["总计数据文件"])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.total_data, f, ensure_ascii=False, indent=2)

    def _save_daily_data(self):
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            path = os.path.join(self.config["数据存储路径"], 
                              f"{today}_{self.config['每日数据文件']}")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.daily_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            project_log(f"[签到系统]保存每日数据出错: {e}", level=LOG_LEVELS.ERROR)

    def _create_default_template(self, template_path):
        try:
            if not os.path.exists(os.path.dirname(template_path)):
                os.makedirs(os.path.dirname(template_path), exist_ok=True)
                
            default_template = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <style>
        body {
            font-family: 'Microsoft YaHei', sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            margin: 0;
            padding: 20px;
            color: white;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        .card {
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            padding: 20px;
            width: 400px;
            box-shadow: 0 8px 32px 0 rgba(31, 38, 135, 0.37);
        }
        .header {
            display: flex;
            align-items: center;
            margin-bottom: 20px;
        }
        .avatar {
            width: 80px;
            height: 80px;
            border-radius: 50%;
            margin-right: 15px;
        }
        .user-info {
            flex-grow: 1;
        }
        .nickname {
            font-size: 24px;
            font-weight: bold;
            margin-bottom: 5px;
        }
        .rank {
            font-size: 16px;
            opacity: 0.8;
        }
        .rewards {
            background: rgba(255, 255, 255, 0.05);
            border-radius: 10px;
            padding: 15px;
            margin: 10px 0;
        }
        .reward-item {
            display: flex;
            justify-content: space-between;
            margin: 5px 0;
        }
        .hitokoto {
            font-style: italic;
            margin-top: 20px;
            padding: 10px;
            border-left: 3px solid rgba(255, 255, 255, 0.5);
        }
    </style>
</head>
<body>
    <div class="card">
        <div class="header">
            <img class="avatar" src="{{ avatar_url }}" alt="Avatar">
            <div class="user-info">
                <div class="nickname">{{ nickname }}</div>
                <div class="rank">第 {{ rank }} 名签到</div>
            </div>
        </div>
        <div class="rewards">
            <div class="reward-item">
                <span>今日好感度</span>
                <span>+{{ favor }}</span>
            </div>
            <div class="reward-item">
                <span>今日积分</span>
                <span>{{ points }}</span>
            </div>
            <div class="reward-item">
                <span>累计好感度</span>
                <span>{{ total_favor }}</span>
            </div>
            <div class="reward-item">
                <span>累计积分</span>
                <span>{{ total_points }}</span>
            </div>
            <div class="reward-item">
                <span>累计签到</span>
                <span>{{ total_days }}天</span>
            </div>
        </div>
        <div class="hitokoto">
            {{ hitokoto }}
        </div>
    </div>
</body>
</html>"""
            
            with open(template_path, "w", encoding="utf-8") as f:
                f.write(default_template)
            project_log(f"[签到系统]已创建默认模板文件: {template_path}")
            
        except Exception as e:
            project_log(f"[签到系统]创建默认模板失败: {e}", level=LOG_LEVELS.ERROR)
            raise e

    def toggle_mode(self):
        self.config["签到模式"] = "image" if self.config["签到模式"] == "text" else "text"
        config_path = os.path.join(self.config["数据存储路径"], "check_in_config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)
        return self.config["签到模式"]

    async def ensure_browser(self):
        try:
            from playwright.async_api import async_playwright
            if self.playwright is None or self.browser is None:
                if self.playwright:
                    await self.playwright.stop()
                self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.launch()
                self.page = await self.browser.new_page(viewport={"width": 800, "height": 600})
            elif self.page is None:
                self.page = await self.browser.new_page(viewport={"width": 800, "height": 600})
        except Exception as e:
            project_log(f"[签到系统]浏览器启动失败: {e}", level=LOG_LEVELS.ERROR)
            raise

    async def close_browser(self):
        try:
            if self.page:
                await self.page.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
        except Exception as e:
            project_log(f"[签到系统]关闭浏览器失败: {e}", level=LOG_LEVELS.ERROR)

    async def generate_image(self, user_id, nickname, rewards, hitokoto_text):
        try:
            import jinja2
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page(viewport={"width": 800, "height": 600})

                os.makedirs(self.config["数据存储路径"], exist_ok=True)
                img_path = os.path.abspath(os.path.join(self.config["数据存储路径"], f"sign_{user_id}.png"))
                if os.path.exists(img_path):
                    os.remove(img_path)
                template_path = os.path.abspath(os.path.join(self.config["数据存储路径"], self.config["模板文件"]))
                if not os.path.exists(template_path):
                    self._create_default_template(template_path)
                with open(template_path, "r", encoding="utf-8") as f:
                    template_content = f.read()
                template = jinja2.Template(template_content)
                html_content = template.render(
                    user_id=user_id,
                    nickname=nickname,
                    rank=rewards["rank"],
                    favor=rewards["favor"],
                    points=rewards["points"],
                    total_favor=rewards["total_favor"],
                    total_points=rewards["total_points"],
                    total_days=rewards["total_days"],
                    hitokoto=hitokoto_text,
                    avatar_url=f"http://q2.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640"
                )
                await page.set_content(html_content)
                await page.screenshot(path=img_path, full_page=True)
                await page.close()
                await browser.close()
                return img_path
        except Exception as e:
            project_log(f"[签到系统]生成图片失败: {e}", level=LOG_LEVELS.ERROR)
            raise Exception(f"生成签到图片失败: {str(e)}")
        finally:
            if self.browser_lock.locked():
                self.browser_lock.release()

    def clean_old_images(self):
        try:
            image_dir = self.config["数据存储路径"]
            current_time = datetime.now().timestamp()
            
            for filename in os.listdir(image_dir):
                if filename.startswith("sign_") and filename.endswith(".png"):
                    file_path = os.path.join(image_dir, filename)
                    file_time = os.path.getmtime(file_path)
                    if current_time - file_time > 3600:
                        try:
                            os.remove(file_path)
                            project_log(f"[签到系统]已清理过期图片: {filename}")
                        except Exception as e:
                            project_log(f"[签到系统]清理过期图片失败 {filename}: {e}", level=LOG_LEVELS.ERROR)
        except Exception as e:
            project_log(f"[签到系统]清理过期图片时出错: {e}", level=LOG_LEVELS.ERROR)

    def check_in(self, user_id: str) -> dict:
        user_id = str(user_id)
        today = datetime.now().strftime("%Y-%m-%d")
        user_data = self._load_user_data(user_id)

        if user_data.get("last_check") == today:
            return {"success": False, "message": "今天已经签到过了哦~"}

        rank = self._get_daily_rank()

        favor = random.randint(self.config["好感度"]["min"], self.config["好感度"]["max"])
        points = random.randint(self.config["积分"]["min"], self.config["积分"]["max"])

        user_data["total_days"] += 1
        user_data["好感度"] += favor
        user_data["积分"] += points
        user_data["last_check"] = today

        self._save_user_data(user_id, user_data)

        return {
            "success": True,
            "rewards": {
                "rank": rank,
                "favor": favor,
                "points": points,
                "total_days": user_data["total_days"],
                "total_favor": user_data["好感度"],
                "total_points": user_data["积分"]
            }
        }

    def _get_daily_rank(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        count = 0
        users_dir = os.path.join(self.config["数据存储路径"], "users")
        for filename in os.listdir(users_dir):
            if filename.endswith('.json'):
                with open(os.path.join(users_dir, filename), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if data.get("last_check") == today:
                        count += 1
        return count

check_in_manager = CheckInManager()

async def check_permission(event):
    user_id = str(event.user_id)
    return (user_id in Configurator.cm.get_cfg().others["ROOT_User"] or 
            user_id in open("./Super_User.ini", "r").read().splitlines() or 
            user_id in open("./Manage_User.ini", "r").read().splitlines())

async def on_message(event, actions, Manager, Segments):
    if not hasattr(event, 'message'):
        return False

    if random.random() < 0.01:
        check_in_manager.clean_old_images()

    message_content = str(event.message).strip()
    reminder = Configurator.cm.get_cfg().others['reminder']

    if message_content.startswith(f"{reminder}添加签到指令 "):
        if not await check_permission(event):
            await actions.send(
                group_id=event.group_id,
                message=Manager.Message(Segments.Text("你没有权限执行此操作"))
            )
            return True
        new_cmd = message_content.replace(f"{reminder}添加签到指令", "", 1).strip()
        if not new_cmd:
            await actions.send(
                group_id=event.group_id,
                message=Manager.Message(Segments.Text("请输入要添加的签到指令"))
            )
            return True
        if check_in_manager.add_command(new_cmd):
            await actions.send(
                group_id=event.group_id,
                message=Manager.Message(Segments.Text(f"已添加签到指令：{new_cmd}"))
            )
        else:
            await actions.send(
                group_id=event.group_id,
                message=Manager.Message(Segments.Text(f"签到指令已存在或无效"))
            )
        return True

    if message_content.startswith(f"{reminder}删除签到指令 "):
        if not await check_permission(event):
            await actions.send(
                group_id=event.group_id,
                message=Manager.Message(Segments.Text("你没有权限执行此操作"))
            )
            return True
        del_cmd = message_content.replace(f"{reminder}删除签到指令", "", 1).strip()
        if not del_cmd or del_cmd == "签到":
            await actions.send(
                group_id=event.group_id,
                message=Manager.Message(Segments.Text("不能删除默认签到指令"))
            )
            return True
        if check_in_manager.remove_command(del_cmd):
            await actions.send(
                group_id=event.group_id,
                message=Manager.Message(Segments.Text(f"已删除签到指令：{del_cmd}"))
            )
        else:
            await actions.send(
                group_id=event.group_id,
                message=Manager.Message(Segments.Text(f"签到指令不存在或无效"))
            )
        return True

    if message_content == f"{reminder}切换签到发送模式":
        if not await check_permission(event):
            await actions.send(
                group_id=event.group_id,
                message=Manager.Message(Segments.Text("你没有权限执行此操作"))
            )
            return True
        new_mode = check_in_manager.toggle_mode()
        await actions.send(
            group_id=event.group_id,
            message=Manager.Message(Segments.Text(f"已切换签到发送模式为：{new_mode}"))
        )
        return True

    if message_content not in check_in_manager.get_commands():
        return False

    try:
        try:
            group_member_info = await actions.get_group_member_info(event.group_id, event.user_id)
            user_nickname = group_member_info.data.raw.get("card") or group_member_info.data.raw.get("nickname")
        except Exception:
            stranger_info = await actions.get_stranger_info(event.user_id)
            user_nickname = stranger_info.data.raw.get("nickname", str(event.user_id))
        
        result = check_in_manager.check_in(str(event.user_id))
        
        if not result["success"]:
            await actions.send(
                group_id=event.group_id,
                message=Manager.Message([
                    Segments.At(event.user_id),
                    Segments.Text(result["message"])
                ])
            )
            return True

        try:
            async with httpx.AsyncClient() as client:
                hitokoto_response = await client.get("https://international.v1.hitokoto.cn/", timeout=5.0)
                hitokoto_data = hitokoto_response.json()
                hitokoto_text = f"{hitokoto_data['hitokoto']} —— {hitokoto_data.get('from_who', '未知')}, {hitokoto_data.get('from', '未知')}"
        except Exception as e:
            project_log(f"[签到系统]获取一言失败: {e}", level=LOG_LEVELS.ERROR)
            hitokoto_text = "一言获取失败..."

        rewards = result["rewards"]
        
        if check_in_manager.config["签到模式"] == "image":
            try:
                img_path = await check_in_manager.generate_image(
                    event.user_id, 
                    user_nickname, 
                    rewards, 
                    hitokoto_text
                )
                
                project_log(f"[签到系统]准备发送图片: {img_path}")
                
                await actions.send(
                    group_id=event.group_id,
                    message=Manager.Message([
                        Segments.At(event.user_id),
                        Segments.Image(f"file:///{img_path}")
                    ])
                )
                
                try:
                    if os.path.exists(img_path):
                        os.remove(img_path)
                        project_log(f"[签到系统]已清理临时文件: {img_path}")
                except Exception as e:
                    project_log(f"[签到系统]清理文件失败: {str(e)}", level=LOG_LEVELS.ERROR)
                    
            except Exception as e:
                project_log(f"[签到系统]发送图片失败: {str(e)}", level=LOG_LEVELS.ERROR)
                check_in_manager.config["签到模式"] = "text"
        
        if check_in_manager.config["签到模式"] == "text":
            message = f'''
签到成功，你是第{rewards["rank"]}名签到的小伙伴
好感度：+{rewards["favor"]}
奖励积分：{rewards["points"]}
累计好感：{rewards["total_favor"]}
累计总计：{rewards["total_points"]}
累计签到：{rewards["total_days"]}天
——————————
{hitokoto_text}'''
            await actions.send(
                group_id=event.group_id,
                message=Manager.Message([
                    Segments.Image(f"http://q2.qlogo.cn/headimg_dl?dst_uin={event.user_id}&spec=640"),
                    Segments.At(event.user_id),
                    Segments.Text(message)
                ])
            )
            
        return True
        
    except Exception as e:
        await actions.send(
            group_id=event.group_id,
            message=Manager.Message(Segments.Text(f"签到出错了: {e}"))
        )
        return True

project_log("[Xiaoyi_QQ]签到插件已加载")
project_log("Version: 1.2.3")
project_log("Author: Xiaoyi")