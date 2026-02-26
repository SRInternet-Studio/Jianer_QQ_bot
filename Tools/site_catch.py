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

    async def catch(self, url: str, size: tuple[int, int] = (0, 0), dark_mode: bool = False) -> str:
        self.page = await self.context.new_page()

        if dark_mode:
            await self.context.add_init_script("() => localStorage.setItem('theme', 'dark')")


            await self.page.add_style_tag(content="body { background-color: black; color: white; } * { color: white !important; filter: brightness(0.7);}")


        await self.page.goto(url, timeout=60000)  # 设置超时，防止页面加载时间过长

        # 等待页面加载完成
        await self.page.wait_for_load_state("networkidle", timeout=60000)

        title = await self.page.title()
        if title is None:  # 处理标题为空的情况
            title = "untitled"
        path = f"./temps/web_{''.join([str(ord(i)) for i in title][:12])}.png"
        opt = {"path": path}
        if size[0] == size[1] == 0:
            await self.page.set_viewport_size({"width": 1080, "height": 250})
            try:  # 处理获取高度失败的情况
                height = await self.page.evaluate("document.body.scrollHeight")
                await self.page.set_viewport_size({"width": 1080, "height": int(height)})
            except Exception as e:
                print(f"获取页面高度失败: {e}")
                await self.page.set_viewport_size({"width": 1080, "height": 720}) # 设置默认高度
        else:
            await self.page.set_viewport_size({"width": size[0], "height": size[1]})

        await self.page.screenshot(**opt)

        await self.page.close()
        return path

    async def quit(self) -> None:
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        await self.playwright.stop()
