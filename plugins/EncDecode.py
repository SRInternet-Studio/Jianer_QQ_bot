from Hyper import Configurator
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

TRIGGHT_KEYWORD = "enc解密"
HELP_MESSAGE = f"{Configurator.cm.get_cfg().others["reminder"]}enc解密 (解密内容) —> 尝试解密被enc加密的内容✅"

async def on_message(event, actions, Manager, Segments, order, bot_name, base64, urllib):

    try:
        start_index = order.find("enc解密")
        if start_index != -1:
            encoded_part = order[start_index + len("enc解密"):].strip()

            if not encoded_part:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("请提供加密的内容啦 ❗")))
                return True
        
            base64_decoded = base64.b64decode(encoded_part).decode('utf-8')
            url_decoded = urllib.parse.unquote(base64_decoded)
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"解密结果: \n{str(url_decoded)}")))
        else:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("请提供加密的内容啦 ❗")))
        return True
    except Exception as e:
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}解密失败了 >_<: \n{str(e)}")))
        return True