"""插件派发器：按关键词匹配调用插件的 on_message，参数按名反射注入。"""
import inspect
import traceback


async def execute_plugins(plugins, reminder, logger, isAny: bool, **main_context) -> bool:
    has_plugin = False
    user_message = main_context["order"] if "order" in main_context else ""

    for plugin_module in plugins:
        if not (
            (not isAny and f"{reminder}{plugin_module.TRIGGHT_KEYWORD}" in f"{reminder}{user_message}")
            or (isAny and plugin_module.TRIGGHT_KEYWORD == "Any")
        ):
            continue

        # 参数绑定阶段的错误视为配置问题：记录并跳过，不视为"已处理"
        kwargs = None
        try:
            on_message_params = inspect.signature(plugin_module.on_message).parameters
            kwargs = {}
            missing = None
            for param_name, param in on_message_params.items():
                if param_name in main_context:
                    kwargs[param_name] = main_context[param_name]
                elif param.default is not inspect.Parameter.empty:
                    pass
                else:
                    missing = param_name
                    break
            if missing is not None:
                logger.error(
                    f"插件 {plugin_module.__name__} 未提供参数 {missing}："
                    "无法在所有上下文中找到该标识符且无默认值。详见 "
                    "https://github.com/SRInternet-Studio/Jianer_QQ_bot/wiki"
                )
                kwargs = None
        except Exception:
            logger.error(f"插件 {plugin_module.__name__} 参数解析失败:\n{traceback.format_exc()}")
            kwargs = None

        if kwargs is None:
            continue

        # 插件 on_message 执行阶段的错误：若为精确匹配则视为"已处理"避免落到 AI
        try:
            response = await plugin_module.on_message(**kwargs)
            if response is True:
                has_plugin = True
                break
        except Exception:
            logger.error(f"\n插件 {plugin_module.__name__} 执行出错，是因为: \n{traceback.format_exc()}")
            if not isAny:
                has_plugin = True

    return has_plugin
