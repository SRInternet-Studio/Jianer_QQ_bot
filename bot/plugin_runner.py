"""插件派发器：按关键词匹配调用插件的 on_message，参数按名反射注入。"""
import inspect
import traceback


async def execute_plugins(plugins, reminder, logger, isAny: bool, **main_context) -> bool:
    has_plugin = False
    user_message = main_context["order"] if "order" in main_context else ""

    for plugin_module in plugins:
        if (not isAny and f"{reminder}{plugin_module.TRIGGHT_KEYWORD}" in f"{reminder}{user_message}") or (isAny and plugin_module.TRIGGHT_KEYWORD == "Any"):
            try:
                on_message_params = inspect.signature(plugin_module.on_message).parameters
                kwargs = {}
                for param_name, param in on_message_params.items():
                    if param_name in main_context:
                        kwargs[param_name] = main_context[param_name]
                    elif param.default is not inspect.Parameter.empty:
                        pass
                    else:
                        raise ValueError(f'''插件 {plugin_module.__name__} 未提供参数 {param_name} ：
无法在所有上下文中找到具有该标识符的变量且该标识符不具有默认值，这样的变量可能在定义前被使用或本就没有定义。
如果您是开发者，请在 main.py 中提供此值。如果您是用户，请忽略此消息并通知管理员及时地修复。
详见 https://github.com/SRInternet-Studio/Jianer_QQ_bot/wiki''')

                response = await plugin_module.on_message(**kwargs)

                if response is not None:
                    if response == True:
                        has_plugin = True
                        break

            except Exception:
                logger.error(f"\n插件 {plugin_module.__name__} 执行出错，是因为: \n{traceback.format_exc()}")
                if not isAny:
                    has_plugin = True

    return has_plugin
