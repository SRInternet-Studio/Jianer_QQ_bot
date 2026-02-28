from Tools.log_helper import project_log, LOG_LEVELS
import os
import requests
import shutil
import re
from Hyper import Configurator,Listener
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())
TRIGGHT_KEYWORD = "hub"
HELP_MESSAGE = f"{Configurator.cm.get_cfg().others["reminder"]}hub hub的内容【引用一条消息】 —> {Configurator.cm.get_cfg().others["bot_name"]}将消息载入HUB"
async def on_message(event, actions: Listener.Actions, Manager, Segments,reminder,Events,gen_message):
    if not isinstance(event, Events.GroupMessageEvent):
        return False
    imageurl = None
    if isinstance(event.message[0], Segments.Reply):
        user_message = event.message[1:]
        hub_content = None
        for segment in user_message:
            if isinstance(segment, Segments.Text):
                hub_match = re.search(r'-hub\s+(\S+)', segment.text)
                if hub_match:
                    hub_content = hub_match.group(1)
                    project_log(f"提取到hub内容: {hub_content}")
                    break
        
        if hub_content:
            content = await actions.get_msg(event.message[0].id)
            project_log(content.data)
            message = gen_message({"message": content.data["message"]})
            # 处理图片部分
            imageurl = None
            for i in message:
                if isinstance(i, Segments.Image):
                    project_log("应该有图")
                    if i.file.startswith("http"):
                        imageurl = i.file
                    else:
                        imageurl = i.url
                    project_log(imageurl)
            
            if imageurl:
                # 添加到HUB
                download_rename_image(imageurl,hub_content,"/opt/1panel/apps/openresty/openresty/www/sites/hub.srinternet.top/index/img")
                await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Text(f"Success 当前图片已成功添加到Hub！请前往hub查看")))
                return True
            else:
                await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Reply(event.message_id),Segments.Text("被引用的消息中没有找到图片内容")))
                return True
        else:
            await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Reply(event.message_id),Segments.Text(f"请在消息中使用'{reminder}hub xxx'指定hub内容")))
            return True
    else:
        await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Reply(event.message_id),Segments.Text(f"请先引用一条消息，并在回复中使用'{reminder}hub xxx'")))
        return True

def download_rename_image(image_url, new_name_without_ext, target_dir):
        try:
            os.makedirs(target_dir, exist_ok=True)
            original_filename = os.path.basename(image_url)
            original_name, original_ext = os.path.splitext(original_filename)
            if not original_ext:
                response = requests.head(image_url)
                content_type = response.headers.get('Content-Type', '')
                if content_type.startswith('image/'):
                    original_ext = '.' + content_type.split('/')[1]
                else:
                    original_ext = '.jpg' #获取不到后缀名TM用jpg得了！
            new_filename = f"{new_name_without_ext}{original_ext}"
            # 下载img
            project_log(f"正在下载图片: {image_url}")
            response = requests.get(image_url, stream=True)
            response.raise_for_status()
            # 保存img
            new_path = os.path.join(target_dir, new_filename)
            with open(new_path, 'wb') as out_file:
                shutil.copyfileobj(response.raw, out_file)
            project_log(f"图片已成功保存为: {new_path}")
            return new_path
        except Exception as e:
            project_log(f"操作失败: {e}", level=LOG_LEVELS.ERROR)
            return None