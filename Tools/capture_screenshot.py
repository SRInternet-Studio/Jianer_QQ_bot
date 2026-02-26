from playwright.async_api import async_playwright
import os

async def capture_screenshot(url, output_path_base, extension="png", timeout=5000):
    if not url:
        return ""
    
    async with async_playwright() as p:
        print(f"capturing {url}......")
        images_num = 0
        output_path = f"{os.path.abspath(output_path_base)}_{images_num}.{extension}"
        while os.path.exists(output_path):
            images_num += 1
            output_path = f"{os.path.abspath(output_path_base)}_{images_num}.{extension}"

        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        try:
            await page.goto(url, timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=timeout)
            
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
            
async def capture_full_page_screenshot(url, output_path_base, extension="png", timeout=30000, max_height=-1):
    if not url:
        return ""
    
    async with async_playwright() as p:
        print(f"capturing {url} (full page)......")
        images_num = 0
        output_path = f"{os.path.abspath(output_path_base)}_{images_num}.{extension}"
        while os.path.exists(output_path):
            images_num += 1
            output_path = f"{os.path.abspath(output_path_base)}_{images_num}.{extension}"

        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        try:
            await page.set_viewport_size({"width": 1920, "height": 1080})
            await page.goto(url, timeout=timeout, wait_until="networkidle")
            
            # 获取页面完整高度
            full_height = await page.evaluate("""() => {
                return Math.max(
                    document.body.scrollHeight,
                    document.body.offsetHeight,
                    document.documentElement.clientHeight,
                    document.documentElement.scrollHeight,
                    document.documentElement.offsetHeight
                );
            }""")
            
            await page.set_viewport_size({"width": 1920, "height": full_height})
            await page.wait_for_timeout(timeout/6)
            
            # 设置视口并截图
            await page.screenshot(
                path=output_path,
                full_page=True, 
                clip={
                    "x": 0,
                    "y": 0,
                    "width": 1920,
                    "height": min(max_height, full_height) if max_height > 0 else full_height
                }, 
                type='png' if extension.lower() == 'png' else 'jpeg',
                quality=90 if extension.lower() in ('jpg', 'jpeg') else None
            )
            
            return output_path
        except Exception as e:
            if os.path.exists(output_path):
                os.remove(output_path)
            raise RuntimeError(f"Screenshot failed: {str(e)}")
        finally:
            await browser.close()