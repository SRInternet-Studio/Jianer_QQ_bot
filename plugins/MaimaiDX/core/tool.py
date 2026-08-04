import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import aiofiles
from playwright.async_api import async_playwright

from ..resources import pie_html_file

SNAPSHOT_JS = (
    "echarts.getInstanceByDom(document.querySelector('div[_echarts_instance_]'))."
    "getDataURL({type: 'PNG', pixelRatio: 2, excludeComponents: ['toolbox']})"
)


def qqhash(qq: int) -> int:
    days = (
        int(time.strftime("%d", time.localtime(time.time())))
        + 31 * int(time.strftime("%m", time.localtime(time.time())))
        + 77
    )
    return (days * qq) >> 8


async def openfile(file: Path) -> dict | list:
    async with aiofiles.open(file, "r", encoding="utf-8") as f:
        data = json.loads(await f.read())
    return data


def _atomic_write_text(file: Path, content: str) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=file.parent,
            prefix=f".{file.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, file)
        temporary = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


async def writefile(file: Path, data: Any) -> bool:
    content = json.dumps(data, ensure_ascii=False, indent=4)
    await asyncio.to_thread(_atomic_write_text, Path(file), content)
    return True


async def run_chrome_to_base64() -> str:
    async with async_playwright() as p:
        browers = await p.chromium.launch(headless=True)
        page = await browers.new_page(java_script_enabled=True)
        await page.goto("file://" + str(pie_html_file))
        await asyncio.sleep(2)

        content: str = await page.evaluate(SNAPSHOT_JS)
        await browers.close()

    content_array = content.split(",")
    if len(content_array) != 2:
        raise OSError(content_array)

    return "base64://" + content_array[-1]
