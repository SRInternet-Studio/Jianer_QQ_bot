from playwright.async_api import async_playwright
import os

async def capture_screenshot(url, output_path_base, extension):
    async with async_playwright() as p:
        print(f"capturing {url}")
        images_num = 0
        output_path = f"{os.path.abspath(output_path_base)}_{images_num}.{extension}"
        while os.path.exists(output_path):
            images_num += 1
            output_path = f"{os.path.abspath(output_path_base)}_{images_num}.{extension}"

        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        try:
            await page.goto(url, timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=5000)
            
            # 设置视口并截图
            await page.set_viewport_size({"width": 1920, "height": 1080})
            await page.screenshot(path=output_path, full_page=True)
            
            return output_path
        except Exception as e:
            if os.path.exists(output_path):
                os.remove(output_path)
            raise RuntimeError(f"Screenshot failed: {str(e)}")
        finally:
            await browser.close()