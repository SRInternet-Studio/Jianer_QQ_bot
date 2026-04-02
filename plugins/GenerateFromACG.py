import requests
from hyperot import configurator as Configurator
Configurator.ensure_config_manager(file="config.json")

reminder = Configurator.cm.get_cfg().others["reminder"]
bot_name = Configurator.cm.get_cfg().others["bot_name"]
TRIGGHT_KEYWORD = "生图 ACG "
HELP_MESSAGE = f'''{reminder}生图 ACG (任意类型，必填) —> {bot_name}制作精美二次元壁纸
{reminder}生图 ACG 帮助 —> 查看{bot_name}的生图帮助菜单'''


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
                return True
        else:
            selfID = await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}正在制作超级好看的二次元壁纸 ヾ(≧▽≦*)o")))

            # 完全使用LoliAPI
            if "随机" in result:
                api = "https://www.loliapi.com/acg/"
                
                try:
                    print(f"使用 LoliAPI: {api}")
                    image_id = await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(api), Segments.Text(f"{result}生成 结束！✧*。٩(>ω<*)و✧*。")))
                    await actions.del_message(selfID.data.message_id)
                    cooldowns[user_id] = current_time
                except Exception as e:
                    await actions.del_message(selfID.data.message_id)
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''因为 {type(e)} 
            {bot_name}不能生成图片了，请稍候在尝试吧 o(TヘTo)''')))
                                
                return True
            elif "电脑壁纸" in result:
                api = "https://www.loliapi.com/acg/pc/"
                
                try:
                    print(f"使用 LoliAPI: {api}")
                    image_id = await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(api), Segments.Text(f"{result}生成 结束！✧*。٩(>ω<*)و✧*。")))
                    await actions.del_message(selfID.data.message_id)
                    cooldowns[user_id] = current_time
                except Exception as e:
                    await actions.del_message(selfID.data.message_id)
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''因为 {type(e)} 
            {bot_name}不能生成图片了，请稍候在尝试吧 o(TヘTo)''')))
                                
                return True
            elif "手机壁纸" in result:
                api = "https://www.loliapi.com/acg/pe/"
                
                try:
                    print(f"使用 LoliAPI: {api}")
                    image_id = await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(api), Segments.Text(f"{result}生成 结束！✧*。٩(>ω<*)و✧*。")))
                    await actions.del_message(selfID.data.message_id)
                    cooldowns[user_id] = current_time
                except Exception as e:
                    await actions.del_message(selfID.data.message_id)
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''因为 {type(e)} 
            {bot_name}不能生成图片了，请稍候在尝试吧 o(TヘTo)''')))
                                
                return True
            elif "头像" in result:
                api = "https://www.loliapi.com/acg/pp/"
                
                try:
                    print(f"使用 LoliAPI: {api}")
                    image_id = await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(api), Segments.Text(f"{result}生成 结束！✧*。٩(>ω<*)و✧*。")))
                    await actions.del_message(selfID.data.message_id)
                    cooldowns[user_id] = current_time
                except Exception as e:
                    await actions.del_message(selfID.data.message_id)
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''因为 {type(e)} 
            {bot_name}不能生成图片了，请稍候在尝试吧 o(TヘTo)''')))
                                
                return True
            elif "背景" in result:
                api = "https://www.loliapi.com/bg/"
                
                try:
                    print(f"使用 LoliAPI: {api}")
                    image_id = await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(api), Segments.Text(f"{result}生成 结束！✧*。٩(>ω<*)و✧*。")))
                    await actions.del_message(selfID.data.message_id)
                    cooldowns[user_id] = current_time
                except Exception as e:
                    await actions.del_message(selfID.data.message_id)
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''因为 {type(e)} 
            {bot_name}不能生成图片了，请稍候在尝试吧 o(TヘTo)''')))
                                
                return True
            elif "帮助" in result:
                h = f'''{bot_name}可生成精美 ACG 壁纸噢~ヾ(≧∪≦*)ノ〃
{reminder}生图 ACG 随机-> 根据设备自动适配
{reminder}生图 ACG 电脑壁纸 -> 电脑端高清壁纸 
{reminder}生图 ACG 手机壁纸 -> 移动端适配壁纸 
{reminder}生图 ACG 头像 -> 适合做头像的图片 
{reminder}生图 ACG 背景 -> 随机二次元背景

举个🍐子：{reminder}生图 ACG 随机 -> {bot_name}生成自适应二次元壁纸
快来试试吧Ｏ(≧▽≦)Ｏ '''
                await actions.del_message(selfID.data.message_id)
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(h)))
                return True
            else:
                await actions.del_message(selfID.data.message_id)
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("指定的类型不存在")))
                h = f'''{bot_name}可生成精美 ACG 壁纸噢~ヾ(≧∪≦*)ノ〃
{reminder}生图 ACG 随机 -> 根据设备自动适配
{reminder}生图 ACG 电脑壁纸 -> 电脑端高清壁纸 
{reminder}生图 ACG 手机壁纸 -> 移动端适配壁纸 
{reminder}生图 ACG 头像 -> 适合做头像的图片 
{reminder}生图 ACG 背景 -> 随机二次元背景

举个🍐子：{reminder}生图 ACG 随机 -> {bot_name}生成自适应二次元壁纸
快来试试吧Ｏ(≧▽≦)Ｏ '''
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(h)))
                return True



    
