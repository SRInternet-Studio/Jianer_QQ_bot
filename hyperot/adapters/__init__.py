import sys
from ..utils.hypetyping import Any


def replace(new: Any, old: str):
    try:
        del sys.modules[old]
    except KeyError:
        pass
    finally:
        sys.modules[old] = new


def replace_listener(new: "hyperot.adapters.listener"):
    replace(new, "hyperot.adapters.listener")


def replace_common(new: "hyperot.adapters.common"):
    replace(new, "hyperot.adapters.common")


def replace_res(new: "hyperot.adapters.res"):
    replace(new, "hyperot.adapters.res")


class Adapter:
    def __init__(self, name: str, version: str, listener: Any, common: Any, res: Any):
        self.name = name
        self.version = version
        self.listener = listener
        self.common = common
        self.res = res

    def load(self):
        replace_res(self.res)
        replace_common(self.common)
        replace_listener(self.listener)
