from abc import ABC

message_types = {}


class SegmentBase(ABC):
    def __init__(self, *args, **kwargs):
        ...

    def __init_subclass__(cls, **kwargs):
        ...

    def to_json(self) -> dict:
        ...

    def __str__(self) -> str: return "__not_set__"

    def __eq__(self, other) -> bool:
        ...

    def __ne__(self, other) -> bool:
        ...
