"""Super_User / Manage_User 名单的读写。

ROOT_User 是只读组（来自 config.json），永远不应被写入到 Super_User.ini 或
Manage_User.ini。写入前会主动剥离掉传入名单中所有 ROOT_User，避免任何调用方
（包括未来的新命令分支）意外把 ROOT 降级或重复入组。
"""
import logging
import os
from typing import Iterable

_logger = logging.getLogger(__name__)


def _load_user_list(filename: str) -> list:
    if not os.path.exists(filename):
        with open(filename, "w", encoding="utf-8"):
            pass
    with open(filename, "r", encoding="utf-8") as f:
        seen, out = set(), []
        for line in f:
            s = line.strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out


def read_user_groups() -> tuple[list, list]:
    """返回 (Super_User, Manage_User)。"""
    return _load_user_list("Super_User.ini"), _load_user_list("Manage_User.ini")


def write_user_groups(s: list, m: list, root_users: Iterable = ()) -> bool:
    """写入两个名单。

    - 剔除空串、去重保序
    - 剔除任何出现在 root_users 中的条目（ROOT_User 不进 Super/Manage 文件）
    """
    root_set = {str(r).strip() for r in (root_users or ()) if str(r).strip()}

    def _sanitize(seq):
        seen, out = set(), []
        for item in seq or ():
            v = str(item).strip()
            if not v or v in root_set or v in seen:
                continue
            seen.add(v)
            out.append(v)
        return out

    s_clean = _sanitize(s)
    m_clean = _sanitize(m)
    try:
        with open("Super_User.ini", "w", encoding="utf-8") as f:
            f.write("\n".join(s_clean))
        with open("Manage_User.ini", "w", encoding="utf-8") as f:
            f.write("\n".join(m_clean))
        return True
    except Exception:
        _logger.exception("write user groups failed")
        return False

