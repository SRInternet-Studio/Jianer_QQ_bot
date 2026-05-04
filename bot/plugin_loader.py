"""插件加载器：扫描 plugins/ 目录加载文件/目录形式的插件。"""
import importlib.util
import os
import sys
import traceback
import uuid

PLUGIN_FOLDER = "plugins"


def load_plugins(config, logger, state: dict) -> list:
    """加载所有插件。

    state 为可变字典，函数会就地填充 loaded_plugins/disabled_plugins/failed_plugins/plugins_help。
    返回可调用的插件模块列表。
    """
    plugins = []
    plugins_help = ""
    protocol_now = str(config.protocol).lower()
    incompatible_in_feishu = {
        "CheckAccount",
        "CheckGroup",
        "LikePlugin",
        "AdvancedQuote",
        "SumUp_MySQL",
    }

    loaded_plugins = state.setdefault("loaded_plugins", [])
    disabled_plugins = state.setdefault("disabled_plugins", [])
    failed_plugins = state.setdefault("failed_plugins", [])
    loaded_plugins.clear()
    disabled_plugins.clear()
    failed_plugins.clear()

    if not os.path.exists(PLUGIN_FOLDER):
        os.makedirs(PLUGIN_FOLDER)

    for filename in os.listdir(PLUGIN_FOLDER):
        module_name = filename
        plugin_base_name = module_name[:-3] if module_name.endswith(".py") else module_name
        logger.debug(f"check file or directory: {filename}")

        if filename == "__pycache__":
            logger.debug("Directory __pycache__ not load.")
            continue

        if filename.startswith("d_"):
            disabled_plugins.append(module_name)
            continue

        if protocol_now == "feishu" and plugin_base_name in incompatible_in_feishu:
            disabled_plugins.append(plugin_base_name)
            logger.info(f"Feishu 模式跳过不兼容插件: {plugin_base_name}")
            continue

        plugin_path = os.path.join(PLUGIN_FOLDER, filename)
        if os.path.isdir(plugin_path):
            setup_file = os.path.join(plugin_path, "setup.py")
            if os.path.exists(setup_file):
                unique_module_name = f"{module_name}_{uuid.uuid4().hex}"
                try:
                    spec = importlib.util.spec_from_file_location(unique_module_name, setup_file)
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[unique_module_name] = module
                    spec.loader.exec_module(module)
                    logger.debug(f"Loaded setup.py from folder plugin: {module_name}")

                    if hasattr(module, 'TRIGGHT_KEYWORD') and hasattr(module, 'on_message'):
                        if isinstance(module.TRIGGHT_KEYWORD, str):
                            plugins.append(module)
                            loaded_plugins.append(unique_module_name)
                            if hasattr(module, 'HELP_MESSAGE') and isinstance(module.HELP_MESSAGE, str):
                                for help_message in [line.strip() for line in module.HELP_MESSAGE.splitlines() if line.strip()]:
                                    plugins_help += f"\n       {help_message}"
                            logger.info(f"已加载插件: {unique_module_name} (关键词: {module.TRIGGHT_KEYWORD})")
                        else:
                            failed_plugins.append(f"{module_name} (TRIGGHT_KEYWORD 必须是字符串)")
                    else:
                        failed_plugins.append(f"{module_name} (缺少 TRIGGHT_KEYWORD：触发标识符 或 on_message：触发函数后端)")

                except FileNotFoundError as e:
                    failed_plugins.append(f"{module_name} (文件未找到: {e})")
                    logger.error(f"加载插件 {unique_module_name} 失败，是因为: {e}")
                    if unique_module_name in sys.modules:
                        del sys.modules[unique_module_name]
                except ImportError as e:
                    failed_plugins.append(f"{module_name} (导入错误: {e})")
                    logger.error(f"加载插件 {unique_module_name} 失败，是因为: \n{traceback.format_exc()}\n")
                    if unique_module_name in sys.modules:
                        del sys.modules[unique_module_name]
                except Exception as e:
                    failed_plugins.append(f"{module_name} (其他错误: {str(e)})")
                    logger.error(f"加载插件 {unique_module_name} 失败: \n{traceback.format_exc()}\n")
                    if unique_module_name in sys.modules:
                        del sys.modules[unique_module_name]
            else:
                logger.warning(f"目录 {filename} 中缺少 setup.py 文件")
                failed_plugins.append(f"{filename} (入口错误: 缺少 setup.py 文件)")

        elif filename.endswith(".py") or filename.endswith(".pyw"):
            module_name = filename[:-3] if filename.endswith(".py") else filename[:-4]

            if filename.startswith("d_"):
                disabled_plugins.append(str(module_name)[3:])
                continue

            unique_module_name = f"{module_name}_{uuid.uuid4().hex}"
            try:
                if unique_module_name in sys.modules:
                    logger.warning(f"模块 {unique_module_name} 已经加载，跳过")
                    continue

                spec = importlib.util.spec_from_file_location(unique_module_name, os.path.join(PLUGIN_FOLDER, filename))
                module = importlib.util.module_from_spec(spec)
                sys.modules[unique_module_name] = module
                spec.loader.exec_module(module)

                if hasattr(module, 'TRIGGHT_KEYWORD') and hasattr(module, 'on_message'):
                    if isinstance(module.TRIGGHT_KEYWORD, str):
                        plugins.append(module)
                        loaded_plugins.append(unique_module_name)
                        if hasattr(module, 'HELP_MESSAGE') and isinstance(module.HELP_MESSAGE, str):
                            for help_message in [line.strip() for line in module.HELP_MESSAGE.splitlines() if line.strip()]:
                                plugins_help += f"\n       {help_message}"
                        logger.info(f"已加载插件: {unique_module_name} (关键词: {module.TRIGGHT_KEYWORD})")
                    else:
                        failed_plugins.append(f"{module_name} (TRIGGHT_KEYWORD 必须是字符串)")
                else:
                    failed_plugins.append(f"{module_name} (缺少 TRIGGHT_KEYWORD：触发标识符 或 on_message：触发函数后端)")

            except FileNotFoundError as e:
                failed_plugins.append(f"{module_name} (文件未找到: {e})")
                logger.error(f"加载插件 {unique_module_name} 失败，原因是: {e}")
                if unique_module_name in sys.modules:
                    del sys.modules[unique_module_name]
            except ImportError as e:
                failed_plugins.append(f"{module_name} (导入错误: {e})")
                logger.error(f"加载插件 {unique_module_name} 失败，原因是: \n{traceback.format_exc()}\n")
                if unique_module_name in sys.modules:
                    del sys.modules[unique_module_name]
            except Exception as e:
                failed_plugins.append(f"{module_name} (其他错误: {str(traceback.format_exc())})")
                logger.error(f"加载插件 {unique_module_name} 失败: \n{traceback.format_exc()}\n")
                if unique_module_name in sys.modules:
                    del sys.modules[unique_module_name]

        else:
            logger.debug(f"跳过非插件文件或目录: {filename}")

    state["plugins_help"] = plugins_help
    logger.info(f"成功加载 {len(loaded_plugins)} 个插件")
    return plugins
