# Repository Guidelines

## Project Structure & Module Organization
- `main.py` is the QQ bot runtime entrypoint (plugin loading, event loop, message dispatch).
- `app.py` is the local WebUI/API service and installer helper.
- `AI_bot/` contains conversation core logic (context manager and AI kernel).
- `Tools/` holds shared utilities used across runtime and plugins.
- `plugins/` contains feature plugins. Both single-file plugins (`*.py`) and folder plugins (`<Plugin>/setup.py`) are supported.
- `assets/` stores static resources (HTML, fonts, images), and `prerequisites/` stores preset-related data.
- Root config files include `config.json`, `webui.json`, `appsettings.json`, and `instances.json`.

## Build, Test, and Development Commands
- Install dependencies:
  - `python -m pip install -r requirements.txt --disable-pip-version-check --no-warn-script-location`
- Start bot runtime:
  - `python main.py`
- Start WebUI/API service (debug mode):
  - `python app.py`
- Quick plugin development check:
  - run the bot, trigger your plugin keyword in a test group, and verify console output.

## Coding Style & Naming Conventions
- Use Python with 4-space indentation and UTF-8 source files.
- Follow existing style in touched files; do not introduce new formatting standards ad hoc.
- Plugin contract is strict: expose `TRIGGHT_KEYWORD` (string) and async `on_message(...)`.
- For directory plugins, keep entrypoint at `plugins/<Name>/setup.py`.
- Use clear, descriptive names for plugin modules and utility functions.

## Testing Guidelines
- No centralized `tests/` suite is currently enforced; validate changes with runtime checks.
- Minimum validation for behavior changes:
  - start `main.py` without import/load errors,
  - verify target plugin or feature path manually,
  - confirm no regressions in related command flows.
- If adding isolated logic, include a lightweight self-check entrypoint (`if __name__ == "__main__":`) consistent with existing utility modules.

## Commit & Pull Request Guidelines
- Current history uses short imperative subjects (for example, `Update requirements.txt`, `Fixed ...`) with occasional conventional format (`feat(release): ...`).
- Recommended commit style: `<type(optional-scope)>: <brief imperative summary>` or concise imperative summary.
- Keep commits focused by concern (runtime, plugin, config, assets).
- PRs should include: purpose, changed modules, manual verification steps, and screenshots/log snippets when behavior is visible in WebUI or chat output.

## Security & Configuration Tips
- Never commit real API keys, account credentials, or private group data.
- Keep environment-specific values in local config files and sanitize logs before sharing.
