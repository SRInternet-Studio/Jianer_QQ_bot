import asyncio
from playwright.async_api import async_playwright

class Catcher:
    browser = None
    context = None
    page = None 

    @classmethod
    async def init(cls, headless: bool = True) -> "Catcher":
        c = cls()
        c.playwright = await async_playwright().start()
        c.browser = await c.playwright.chromium.launch(headless=headless)
        c.context = await c.browser.new_context()
        return c

    async def catch(self, url: str, size: tuple[int, int] = (0, 0)) -> str:
        self.page = await self.context.new_page()
        await self.page.goto(url)
        title = await self.page.title()
        path = f"./temps/web_{''.join([str(ord(i)) for i in title][:12])}.png"
        opt = {"path": path}
        if size[0] == size[1] == 0:
            await self.page.set_viewport_size({"width": 1080, "height": 250})
            height = await self.page.evaluate("document.body.scrollHeight")
            await self.page.set_viewport_size({"width": 1080, "height": int(height)})
        else:
            await self.page.set_viewport_size({"width": size[0], "height": size[1]})

        await self.page.screenshot(**opt) 
        # await self.page.close() 
        return path

    async def quit(self) -> None:
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        await self.playwright.stop()