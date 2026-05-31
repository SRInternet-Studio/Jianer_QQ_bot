import os, json, datetime
from hyperot import configurator as Configurator
if not hasattr(Configurator, "cm") or not Configurator.cm:
    Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())
from hyperot import events as Events
from typing import Union, Optional, Tuple
# 初始化预设常量 

# 配置文件名
CONFIG_FILE = ".//prerequisites/current.json"
# 预设文件存放目录
PRESET_DIR = "prerequisites"
# 默认预设名称
NORMAL_PRESET = "Normal"
PLUGIN_FOLDER = "plugins"

current_preset = ""
if not os.path.exists(PLUGIN_FOLDER):
    os.makedirs(PLUGIN_FOLDER)

def read_presets():
    """读取 JSON 预设数据."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"错误：配置文件 '{CONFIG_FILE}' 未找到。")
        return {} 
    except json.JSONDecodeError as e:
        print(f"JSON 解码错误：{e}")
        return {} 

def write_presets(data):
    """写入 JSON 预设数据."""
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

def gen_presets(uid, bot_name, bot_name_en, event_user, lookup_uid=None):
    # 初始化统一预设读写变量 prerequisite_editor 和 prerequisite_readerq
    global current_preset
    if not os.path.exists(CONFIG_FILE) or os.stat(CONFIG_FILE).st_size == 0:
        write_presets({})  

    presets = read_presets()
    
    # 添加默认预设
    if NORMAL_PRESET not in presets:
        presets[NORMAL_PRESET] = {
            "name": "做我女朋友",
            "uid": [],
            "info": "老公～你来啦！ (♡>𖥦<)/♥",
            "path": f"{NORMAL_PRESET}.txt",
        }
        write_presets(presets)

    # 读取属于当前用户的预设
    sys_prompt = None
    lookup_uid = uid if lookup_uid is None else lookup_uid
    lookup_uid_str = str(lookup_uid)
    for preset_id, preset_data in presets.items():
        presets_uid_list = preset_data.get("uid", [])
        if lookup_uid_str in [str(x) for x in presets_uid_list]:
            preset_path = os.path.join(PRESET_DIR, preset_data["path"])
            with open(preset_path, "r", encoding="utf-8") as f:
                sys_prompt = f.read()
                current_preset = preset_data["name"]
                
                print(f"[{datetime.datetime.now()}] '{current_preset}' 已载入系统预设")
    
    if sys_prompt == None:
        preset_path = os.path.join(PRESET_DIR, presets[NORMAL_PRESET]["path"])
        with open(preset_path, "r", encoding="utf-8") as f:
            sys_prompt = f.read()
            current_preset = NORMAL_PRESET
            
    # 替换实时变量
    sys_prompt = sys_prompt.replace("{self.bot_name}",bot_name)
    sys_prompt = sys_prompt.replace("{self.bot_name_en}",bot_name_en)
    sys_prompt = sys_prompt.replace("{self.event_user}",event_user)
    sys_prompt = sys_prompt.replace("{self.event_user_id}",str(lookup_uid))

    return sys_prompt

def change_presets(presets: dict, order: str,
                   event: Union[Events.GroupMessageEvent, Events.PrivateMessageEvent],
                   target_user_id=None):
    selected_preset_id = None
    target_user_id = event.user_id if target_user_id is None else target_user_id
    target_user_id_str = str(target_user_id)
    for preset_id, preset_data in presets.items():
        # print(f"检查预设: {order} - {preset_data['name']}")
        if preset_data["name"] == order:
            selected_preset_id = preset_id
            break

    if selected_preset_id:
        # 将用户 ID 添加到所选预设的 uid 列表中
        if "uid" not in presets[selected_preset_id]:
            presets[selected_preset_id]["uid"] = []
        current_ids = [str(x) for x in presets[selected_preset_id]["uid"]]
        if target_user_id_str not in current_ids:
            presets[selected_preset_id]["uid"].append(target_user_id_str)

        # 从其他预设中移除用户 ID
        for preset_id, preset_data in presets.items():
            if preset_id != selected_preset_id and "uid" in preset_data:
                preset_data["uid"] = [x for x in preset_data["uid"] if str(x) != target_user_id_str]

        write_presets(presets)
        return presets, presets[selected_preset_id]["info"] if selected_preset_id else "", True
    
    return presets, "", False

def list_presets(presets: dict, current_preset: str, reminder: str):
    preset_list = "\n".join(
        [
            f"    {reminder}{data['name']}（当前） - {data['info']}"
            if data['name'] == current_preset
            else f"    {reminder}{data['name']} - {data['info']}"
            for data in presets.values()
        ]
    )
    return preset_list
