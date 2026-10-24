from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from plugins.JianerAI.sticker_store import StickerOptions, StickerStore
from plugins.JianerAI.tools.stickers import sticker_tools


def _image(color: str) -> bytes:
    image = Image.new("RGB", (64, 64), color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_sticker_store_add_search_deduplicate_and_delete(
    tmp_path: Path,
) -> None:
    store = StickerStore(
        StickerOptions(directory=tmp_path / "stickers", max_entries=10)
    )
    first = store.add(
        _image("white"),
        "一只白猫抬头，表情震惊无语，适合表达无法理解",
        "白猫 震惊 无语",
        "user-1",
    )
    duplicate = store.add(_image("white"), "重复图片", "重复")

    assert first.status == "added"
    assert first.entry is not None
    assert duplicate.status == "duplicate"
    assert duplicate.entry is not None
    assert duplicate.entry.id == first.entry.id
    found = store.search("无语白猫", 5)
    assert found
    assert found[0][0].id == first.entry.id
    assert Path(first.entry.file).is_file()

    removed = store.delete(first.entry.id)
    assert removed is not None
    assert store.count() == 0
    assert not Path(first.entry.file).exists()


def test_sticker_store_trims_old_entries_and_tools_register(
    tmp_path: Path,
) -> None:
    store = StickerStore(
        StickerOptions(directory=tmp_path / "stickers", max_entries=1)
    )
    first = store.add(_image("red"), "红色笑脸", "开心")
    second = store.add(_image("blue"), "蓝色哭脸", "难过")

    assert first.entry is not None
    assert second.entry is not None
    assert store.get(first.entry.id) is None
    assert store.get(second.entry.id) is not None

    specs = sticker_tools(store, lambda _raw, _hint: _description())
    assert {spec.name for spec in specs} == {
        "sticker_add",
        "sticker_delete",
        "sticker_list",
        "sticker_search",
        "sticker_send",
    }


async def _description() -> str:
    return "测试描述"
