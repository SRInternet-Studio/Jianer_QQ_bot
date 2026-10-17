"""今天是什么小猪 Plus 的业务模块（JianerCore 原生实现）。

上游 ``nonebot-plugin-rollpig-plus`` 的领域逻辑（数据层、渲染、流程、文案）
保留在这里，框架相关的部分统一走 ``plugins.RollPigPlus.host``：

- ``host.logger`` / ``host.localstore`` / ``host.get_plugin_config``
- ``host.Bot``：把 JianerCore ``actions`` 包装成 OneBot 调用
- ``host.scheduler`` / ``host.on_startup`` / ``host.on_shutdown`` / ``host.on_bot_connect``

命令注册位于 ``handlers/``，由 ``plugins/RollPigPlus/setup.py`` 导入。
"""

from .config import Config, plugin_config

__all__ = ["Config", "plugin_config"]
