import os

PLUGIN_FOLDER = "plugins" # 发布时替换为链接到 main.py 的 PLUGIN_FOLDER
def get_all_plugin_names() -> list:
    global PLUGIN_FOLDER
    plugin_names = []
    plugin_folder = PLUGIN_FOLDER

    for filename in os.listdir(plugin_folder):
        if filename == "__pycache__":
            continue

        plugin_name = filename

        # 处理目录形式插件
        plugin_path = os.path.join(plugin_folder, filename)
        if os.path.isdir(plugin_path):
            pass  # 目录名即插件名，已经赋值

        # 处理文件形式插件
        elif filename.endswith(".py") or filename.endswith(".pyw"):
            plugin_name = filename[:-3] if filename.endswith(".py") else filename[:-4]

        else:
            continue # 跳过非插件文件

        # 检查是否禁用
        if plugin_name.startswith("d_"):
            plugin_name = plugin_name[2:] #移除d_

        plugin_names.append(plugin_name)

    return plugin_names