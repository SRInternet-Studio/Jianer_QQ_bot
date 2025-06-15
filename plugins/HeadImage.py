from Hyper import Configurator
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

TRIGGHT_KEYWORD = "大头照"
HELP_MESSAGE = f"{Configurator.cm.get_cfg().others["reminder"]}大头照 【@一个用户】—> {Configurator.cm.get_cfg().others["bot_name"]}给他拍张大头照"

async def on_message(event, actions, Manager, Segments):
    if str(event.user_id):
        uin = ""
    
        for i in event.message:
                print(type(i))
                print(str(i))
                if isinstance(i, Segments.At):
                    print("At in loading...")
                    uin = i.qq

        if uin == "":
            uin = event.user_id
            
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(f"http://q2.qlogo.cn/headimg_dl?dst_uin={uin}&spec=640")))
    
    return True