import os, json, datetime
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

def gen_presets(uid, bot_name, event_user):
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
    for preset_id, preset_data in presets.items():
        presets_uid_list = preset_data.get("uid", [])
        if uid in presets_uid_list:
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
    sys_prompt = sys_prompt.replace("{self.event_user}",event_user)

    return sys_prompt
