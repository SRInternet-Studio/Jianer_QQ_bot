from Hyper import Segments
from Hyper.Events import *
from PIL import Image, ImageDraw, ImageFont
import os
from io import BytesIO
import httpx
from PIL import ImageFilter

def open_from_url(url: str):
    return Image.open(BytesIO(httpx.get(url).content))

def square_scale(image: Image, height: int):
    old_width, old_height = image.size
    x = height / old_height
    width = int(old_width * x)
    return image.resize((width, height))

def wrap_text(text, chars_per_line=13):
    lines = [text[i:i + chars_per_line] for i in range(0, len(text), chars_per_line)]
    return '\
'.join(lines)

async def get_image(quote, ava_url, name, uin):  
    mask_path = "assets/quote/mask.png" 
    mask = Image.open(mask_path).convert("RGBA")
    background = Image.new('RGBA', mask.size, (255, 255, 255, 255))
    head = open_from_url(ava_url).convert("RGBA")

    title_font = ImageFont.truetype(r"assets/t.ttf", size=36)
    desc_font = ImageFont.truetype(r"assets/n.ttf", size=30)
    digit_font = ImageFont.truetype(r"assets/sz.ttf", size=36)
    emoji_font = ImageFont.truetype(r"assets/e.ttf", size=36)  # 新增emoji字体

    background.paste(square_scale(head, 640), (0, 0))
    background.paste(mask, (0, 0), mask)

    draw = ImageDraw.Draw(background)
    text = wrap_text(quote)

    mask_circle = Image.new("L", head.size, 0)
    draw_circle = ImageDraw.Draw(mask_circle)
    draw_circle.ellipse((0, 0, head.size[0], head.size[1]), fill=255)
    head.putalpha(mask_circle)

    x_offset = 640
    y_offset = 165
    for i, char in enumerate(text):
        if char.isdigit() or char == '.':
            font = digit_font
            fill_color = (255, 0, 0)
        elif ord(char) in range(0x1F600, 0x1F64F):  # 检查是否为emoji
            font = emoji_font
            fill_color = (255, 255, 255)
        else:
            font = title_font
            fill_color = (255, 255, 255)

        char_width = font.getlength(char)
        if x_offset + char_width > mask.size[0]:
            x_offset = 640
            y_offset += 40

        draw.text((x_offset, y_offset), char, font=font, fill=fill_color)
        x_offset += char_width
        if char == '\
':
            x_offset = 640
            y_offset += 40

    draw.text((862 if len(name) >= 7 else 1000, 465), f"——{name}", font=desc_font, fill=(112, 112, 112))

    nbg = Image.new('RGB', mask.size, (0, 0, 0))
    nbg.paste(background, (0, 0))
    nbg.save("./temps/quote.png")

async def handle(message, actions, images = None) -> Segments.Image:
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
    text = str(message).replace("[图片]", "")
    if images != None:
        print("有图")
        await get_image(text, images, name, uin)  # 传递 uin 参数
    else:
        await get_image(text, f"http://q2.qlogo.cn/headimg_dl?dst_uin={uin}&spec=640", name, uin) # 传递 uin 参数

    return Segments.Image(f"file://{os.path.abspath('./temps/quote.png')}")
