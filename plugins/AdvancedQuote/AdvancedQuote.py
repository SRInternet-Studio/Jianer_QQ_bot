import os
from hyperot import segments as Segments
from hyperot.events import *
from Tools.site_catch import Catcher
from Tools import tools as t

# 生成图像的主要函数
async def get_image(quote, ava_url, name, uin):
    catcher = await Catcher.init()
    with open("./assets/quote.html", "r", encoding="utf-8") as f:
        html = f.read()

    html = html.replace("{ava_url}", ava_url)
    html = html.replace("{quote}", quote)
    html = html.replace("{name}", name)

    with open(f"./temps/quote_{uin}.html", "w", encoding="utf-8") as f:
        f.write(html)
    # res = await html2img(f"file://{os.path.abspath(f'./temps/quote_{uin}.html')}")
    res = await catcher.catch(f"file://{os.path.abspath(f'./temps/quote_{uin}.html')}", (1280, 640))
    os.remove(f"./temps/quote_{uin}.html")
    await catcher.quit()
    return res

# 处理消息的函数
async def handle(message, actions, images=None, Manager=None, Segments=None) -> Segments.Image:
    if isinstance(message[0], Segments.Reply):
        msg_id = message[0].id
    else:
        return

    content = await actions.get_msg(msg_id)
    name = content.data["sender"]["nickname"] if not content.data["sender"].get("card") else \
        content.data["sender"]["card"]
    uin = content.data["sender"]["user_id"]
    message = content.data["message"]
    message = gen_message({"message": message})
    if hasattr(t, "replace_at_with_nickname"):
        text = await t.replace_at_with_nickname(message, Manager, Segments, actions)
    text = str(text).replace("[图片]", "")
    if images is not None:
        print("有图")
        await get_image(text, images, name, uin)  # 传递 uin 参数
    else:
        await get_image(text, f"http://q2.qlogo.cn/headimg_dl?dst_uin={uin}&spec=640", name, uin)  # 传递 uin 参数

    return Segments.Image(f"file://{os.path.abspath('./temps/web_.png')}")