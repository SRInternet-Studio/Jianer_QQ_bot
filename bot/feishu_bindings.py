"""飞书账号 <-> QQ 号绑定的持久化。"""
import json
import os

FEISHU_BIND_FILE = "feishu_bindings.json"


def load_feishu_bindings() -> dict:
    if not os.path.exists(FEISHU_BIND_FILE):
        return {}
    try:
        with open(FEISHU_BIND_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if k and v}
    except Exception:
        pass
    return {}


def save_feishu_bindings(bindings: dict) -> bool:
    try:
        with open(FEISHU_BIND_FILE, "w", encoding="utf-8") as f:
            json.dump(bindings, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def bind_feishu_user(open_id: str, qq_id: str) -> bool:
    open_id = str(open_id or "").strip()
    qq_id = str(qq_id or "").strip()
    if not open_id or not qq_id:
        return False
    bindings = load_feishu_bindings()
    bindings[open_id] = qq_id
    return save_feishu_bindings(bindings)


def get_bound_qq(open_id: str) -> str | None:
    bindings = load_feishu_bindings()
    return bindings.get(str(open_id))
