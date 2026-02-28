from Tools.log_helper import project_log
import requests
from Hyper import Configurator
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

reminder = Configurator.cm.get_cfg().others["reminder"]
bot_name = Configurator.cm.get_cfg().others["bot_name"]
TRIGGHT_KEYWORD = "生图 ACG "
HELP_MESSAGE = f"{reminder}生图 ACG (任意类型，必填) —> {bot_name}制作精美二次元壁纸"

async def on_message(event, actions, Manager, Segments, order, time, cooldowns, 
                     Super_User, Manage_User, ROOT_User, bot_name):
    
    global reminder
    start_index = order.find("生图 ACG ") 
    if start_index != -1:
        result = order[start_index + len("生图 ACG "):].strip()
        api = ""
        user_id = event.user_id
        current_time = time.time()
        if user_id in cooldowns and current_time - cooldowns[user_id] < 18:
            if not (str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User):
                time_remaining = 18 - (current_time - cooldowns[user_id])
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"18秒个人cd，请等待 {time_remaining:.1f} 秒后重试")))
                return
        else:
            selfID = await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}正在制作超级好看的二次元壁纸 ヾ(≧▽≦*)o")))

            if "随机" in result:
                api = "https://api.iw233.cn/api.php?sort=random"
                project_log("0")
            elif "精选" in result:
                api = "https://api.iw233.cn/api.php?sort=top"
                project_log("1")
            elif "白毛" in result:
                api = "https://api.iw233.cn/api.php?sort=yin"
                project_log("2")
            elif "星空" in result:
                api = "https://api.iw233.cn/api.php?sort=xing"
                project_log("3")
            elif "兽娘" in result:
                api = "https://api.iw233.cn/api.php?sort=cat"
                project_log("4")
            elif "电脑壁纸" in result:
                api = "https://api.iw233.cn/api.php?sort=pc"
                project_log("5")
            elif "手机壁纸" in result:
                api = "https://api.iw233.cn/api.php?sort=mp"
                project_log("6")
            elif "头像" in result:
                api = "https://www.loliapi.com/acg/pp/"
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(api), Segments.Text(f"{result}生成 结束！✧*。٩(>ω<*)و✧*。")))
                await actions.del_message(selfID.data.message_id)
                cooldowns[user_id] = current_time
                project_log("7")
                return

            if api == "":
                h = f'''{bot_name}可生成精美 ACG 壁纸噢~ヾ(≧∪≦*)ノ〃
1. 按内容生成，发送
{reminder}生图 ACG 随机/精选/白毛/星空/兽娘/头像
2. 按尺寸生成，发送
{reminder}生图 ACG 电脑壁纸/手机壁纸
举个🍐子：{reminder}生图 ACG 白毛 -> {bot_name}生成白毛二次元壁纸
快来试试吧Ｏ(≧▽≦)Ｏ '''
                await actions.del_message(selfID.data.message_id)
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(h)))
            else:
                parameters = {
                        "type": "json",
                        'num': "1",
                        }

                try:
                    response = requests.get(api, params=parameters)
                    project_log(parameters)
                    outputurl = response.json()
                    output = outputurl["pic"][0]
                    project_log(output)

                    image_id = await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(output), Segments.Text(f"{result}生成 结束！✧*。٩(>ω<*)و✧*。")))
                    await actions.del_message(selfID.data.message_id)
                    cooldowns[user_id] = current_time
                except Exception as e:
                    await actions.del_message(selfID.data.message_id)
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''因为 {type(e)} 
{bot_name}不能生成图片了，请稍候在尝试吧 o(TヘTo)''')))
                
        return True

