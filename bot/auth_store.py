"""Super_User / Manage_User 名单的读写。"""
import os


def _load_user_list(filename: str) -> list:
    if not os.path.exists(filename):
        with open(filename, "w"):
            pass
    with open(filename, "r") as f:
        return list({line.strip() for line in f if line.strip()})


def read_user_groups() -> tuple[list, list]:
    """返回 (Super_User, Manage_User)。"""
    return _load_user_list("Super_User.ini"), _load_user_list("Manage_User.ini")


def write_user_groups(s: list, m: list) -> bool:
    s = [item for item in s if item]
    m = [item for item in m if item]
    su = "\n".join(s)
    ma = "\n".join(m)
    try:
        with open("Super_User.ini", "w") as f:
            f.write(su)
        with open("Manage_User.ini", "w") as f:
            f.write(ma)
        return True
    except Exception:
        return False
