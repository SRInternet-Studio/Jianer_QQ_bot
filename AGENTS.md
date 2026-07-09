# Repository Guidelines

## Project Structure & Module Organization
This repository is a Python-first QQ bot project with plugin-based extensions.

- Entry point: `main.py` — 事件注册与主运行循环；框架运行时来自 PyPI 包 `jianer-bot`。
- Business layer: `bot/` — 从 `main.py` 抽出的业务模块，按职责拆分：
  - `bot/plugin_state.py` — JianerCore `PluginManager` 的项目侧状态、加载、派发与插件管理视图。
  - `bot/plugin_ops.py` — 重载/启用/禁用插件的命令。
  - `bot/event_handlers.py` — 戳一戳、重启通知、入群/退群、入群邀请等非消息事件。
  - `bot/group_commands.py` — ping / 关于 / 群发黑名单菜单 / 插件视角 等轻量群命令。
  - `bot/admin_commands.py` — `管理 / 删除管理 / 让我访问`。
  - `bot/memory_commands.py` — 简儿记忆子命令。
  - `bot/misc_commands.py` — AI 菜单、后缀、黑名单、冷静、角色预设、头衔、群发等。
  - `bot/help_view.py` — 帮助文本/图片/转发三种呈现。
  - `bot/auth_store.py` / `feishu_bindings.py` / `help_mode.py` — 持久化层。
  - `bot/broadcast.py` — 定时群发与广播工具。
  - `bot/protocol.py` / `utils.py` — 协议判定与通用小工具。
  - `bot/__init__.py` 使用 PEP 562 懒加载，避免测试/静态分析场景拉起整条依赖链。
- Framework/runtime: `jianer-bot`（导入包名 `jianer`）、`AI_bot/`（AI 核心）、`Tools/`、`parser/`。
- Feature extensions: `plugins/` — 新式 JianerCore 插件；单文件插件直接放 `.py`，目录插件入口固定 `setup.py`。
- Runtime/static resources: `static/`, `prerequisites/`, `aiconfig/`。
- Tests: `tests/` — 根项目测试（目前覆盖 `plugin_state` 与 JianerCore 插件加载/派发）。
- Packaging subproject: `ARC_Spec_Python/` — 独立 Python 子包，自带工具链。

When adding new bot features, prefer `plugins/` for isolated capabilities. Shared business logic should go into `bot/`；只有真正跨进程/跨模块的工具才放 `Tools/` 或 `AI_bot/`。避免再往 `main.py` 里堆命令分支。

## Build, Test, and Development Commands
Use Python 3.11+ for the root project.

- `python -m pip install -r requirements.txt` — install root dependencies.
- `python main.py` — run the bot locally.
- `python -m pytest tests/ -q` — run root project unit tests.
- `docker compose up bot` — run bot service in container.
- `docker build -t jianer-bot .` — build container image.

For `ARC_Spec_Python/`:

- `cd ARC_Spec_Python && pip install -e .` — editable install.
- `cd ARC_Spec_Python && python -m pytest` — run tests.
- `cd ARC_Spec_Python && black . && flake8 .` — format and lint.

## Coding Style & Naming Conventions
- Use 4-space indentation and UTF-8 source files.
- Follow existing Python naming: `snake_case` for variables/functions, `PascalCase` for classes.
- Keep plugin folder names descriptive; directory plugin entry should remain `setup.py`.
- New plugins must define `__plugin_meta__ = PluginMetadata(...)` and expose `async def dispatch(event, actions)`.
- Use plugin IDs in the form `jianerbot-plugin-{name}`. Declare dependencies with full plugin IDs, for example `requires={"jianerbot-plugin-alconna"}`.
- `bot/` 下的命令函数：优先使用显式参数，不要依赖 `globals()` 反射注入。
- Comments 仅在必要处加简短说明；不要堆大量装饰性注释。
- In `ARC_Spec_Python/`, formatting is standardized by Black (line length 88) and import sorting by isort (`profile = "black"`).

## Testing Guidelines
- Primary framework: `pytest`（根项目 `tests/`，子项目见 `ARC_Spec_Python/pyproject.toml`）。
- Test file patterns: `test_*.py` 和 `*_test.py`；根项目 test path 为 `tests/`。
- 搬运/重构类改动，在提交前至少：
  - `python -c "import ast, glob; [ast.parse(open(f,encoding='utf-8').read()) for f in glob.glob('bot/*.py') + ['main.py']]"`（整体语法校验）。
  - `python -m pytest tests/ -q`。
  - 在 `ARC_Spec_Python/` 内运行 `pytest / black / flake8`。

## Plugin Architecture
- 插件加载和派发只通过 JianerCore `PluginManager`：`bot/plugin_state.py` 负责创建 manager、调用 `load_plugins("plugins")` 和 `dispatch(event, actions)`。
- 旧式关键词函数插件契约不再使用；不要新增旧式插件，也不要恢复项目自托管 loader/runner。
- 插件需要项目运行时状态时，从 `bot.plugin_state` 读取，例如 `current_stage()`、`current_order()`、`get_runtime()`、`websocket_url()`。
- `d_` 前缀仍作为禁用插件约定，文件和目录都适用。

## Commit & Pull Request Guidelines
Recent history uses `type(scope): summary` 风格（`feat:`, `fix:`, `refactor:`, `chore:`…）。约定：

- `type: short summary` (for example, `fix: prevent timing loop high CPU`)
- One logical change per commit; include config updates with related code changes.
- 请勿把本地配置/数据库/密钥文件加入提交。仓库 `.gitignore` 已忽略 `config.json`、`config12.json`、`aiconfig/gemini.ai.json`、`jianer_memory.db*`；新增同类文件时同步更新。

No repository-level PR template is present. For PRs, include:

- What changed and why
- Affected modules/paths (for example, `bot/misc_commands.py`)
- Local verification performed (commands and results)
- Screenshots/log snippets when behavior or UI output changes
