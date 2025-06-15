from Hyper import Segments
from Hyper.Events import *
from PIL import Image, ImageDraw, ImageFont
import os
from io import BytesIO
import httpx
from PIL import ImageFilter
from urllib.parse import urlparse, urlunparse
import emoji

# 替换 https 为 http 的函数
def replace_scheme_with_http(url: str) -> str:
    parsed_url = urlparse(url)
    if parsed_url.scheme == 'https':
        parsed_url = parsed_url._replace(scheme='http')
    return urlunparse(parsed_url)

# 从 URL 打开图像的函数
def open_from_url(url: str):
    print(url)
    return Image.open(BytesIO(httpx.get(replace_scheme_with_http(url)).content))

# 判断是否是 Emoji 的函数
def is_emoji(char):
    return char in emoji.EMOJI_DATA

# 调整图像大小的函数
def square_scale(image: Image, height: int):
    old_width, old_height = image.size
    x = height / old_height
    width = int(old_width * x)
    return image.resize((width, height))

# 包装文字（自动换行）的函数
def wrap_text(text, chars_per_line=13):
    lines = [text[i:i + chars_per_line] for i in range(0, len(text), chars_per_line)]
    return '\n'.join(lines)

# 包装名字（自动换行）的函数
def wrap_name(name, chars_per_line=7):
    lines = [name[i:i + chars_per_line] for i in range(0, len(name), chars_per_line)]
    return '\n'.join(lines)

# 本地渲染 Emoji（彩色）的函数
def render_emoji(char, font):
    # 创建一个透明背景的图像，用于绘制 Emoji
    emoji_img = Image.new("RGBA", (36, 36), (0, 0, 0, 0))  # 创建一个透明图像
    draw = ImageDraw.Draw(emoji_img)
    draw.text((0, 0), char, font=font, fill=(255,255, 255))  # 使用黑色绘制，因为我们将直接粘贴到背景上
    return emoji_img

# 生成图像的主要函数
async def get_image(quote, ava_url, name, uin):
    if str(uin) == "1348472639":
        print("3803")
        mask_path = "assets/quote/maskrbc.png"
    else:
        mask_path = "assets/quote/mask.png"
    mask = Image.open(mask_path).convert("RGBA")
    background = Image.new('RGBA', mask.size, (255, 255, 255, 255))
    head = open_from_url(ava_url).convert("RGBA")

    title_font = ImageFont.truetype(r"assets/t.ttf", size=36)
    desc_font = ImageFont.truetype(r"assets/n.ttf", size=30)
    digit_font = ImageFont.truetype(r"assets/sz.ttf", size=36)

    # 加载本地彩色 Emoji 字体
    emoji_font = ImageFont.truetype("assets/e.ttf", size=30)  # 替换为你的字体文件路径

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
    for char in text:
        font = title_font
        fill_color = (255, 255, 255)  # 默认白色

        if char.isdigit() or char == '.':
            font = digit_font
            fill_color = (255, 0, 0)
        elif is_emoji(char):  # 使用本地渲染的彩色 emoji
            emoji_img = render_emoji(char, emoji_font)
            background.paste(emoji_img, (int(x_offset), int(y_offset)), emoji_img)  # 转换为整数
            x_offset += emoji_img.width
            continue

        char_width = font.getlength(char)
        if x_offset + char_width > mask.size[0]:
            x_offset = 640
            y_offset += 40

        draw.text((int(x_offset), int(y_offset)), char, font=font, fill=fill_color) # 转换为整数
        x_offset += char_width
        if char == '\n':
            x_offset = 640
            y_offset += 40

    # 处理右下角名字的自动换行
    name_text = wrap_name(name)
    draw.text((862 if len(name_text) >= 7 else 1000, 465), f"——{name_text}", font=desc_font, fill=(112, 112, 112))

    nbg = Image.new('RGB', mask.size, (0, 0, 0))
    nbg.paste(background, (0, 0))
    nbg.save("./temps/quote.png")

# 处理消息的函数
async def handle(message, actions, images=None) -> Segments.Image:
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
    if images is not None:
        print("有图")
        await get_image(text, images, name, uin)  # 传递 uin 参数
    else:
        await get_image(text, f"http://q2.qlogo.cn/headimg_dl?dst_uin={uin}&spec=640", name, uin)  # 传递 uin 参数

    return Segments.Image(f"file://{os.path.abspath('./temps/quote.png')}")