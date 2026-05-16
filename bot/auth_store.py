"""Super_User / Manage_User 名单的读写。"""
import logging
import os

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


def write_user_groups(s: list, m: list) -> bool:
    s = [item for item in s if item]
    m = [item for item in m if item]
    su = "\n".join(s)
    ma = "\n".join(m)
    try:
        with open("Super_User.ini", "w", encoding="utf-8") as f:
            f.write(su)
        with open("Manage_User.ini", "w", encoding="utf-8") as f:
            f.write(ma)
        return True
    except Exception:
        _logger.exception("write user groups failed")
        return False
