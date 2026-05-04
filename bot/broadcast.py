"""定时群发：后台线程循环读取 timing_message.ini，在指定分钟向所有群广播。"""
import asyncio
import datetime
import os
import random
import time

from .utils import load_blacklist


async def send_msg_all_groups(text, actions, Manager, Segments, suffix_manager, logger, message=None):
    echo = await actions.custom.get_group_list()
    result = Manager.Ret.fetch(echo)
    blacklist = load_blacklist()
    logger.info(f"sys: 群发 {result.data.raw}")
    processed_text = suffix_manager.process_text(text, 0)
    for group in result.data.raw:
        group_id = str(group['group_id'])
        if group_id not in blacklist:
            await actions.send(group_id=group['group_id'], message=Manager.Message(Segments.Text(processed_text)))
            time.sleep(random.random() * 3)
        else:
            logger.warning(f"群聊 {group_id} 在黑名单内，取消发送")


def timing_message_loop(actions, Manager, Segments, suffix_manager, logger):
    """阻塞循环：后台线程入口。每分钟检查一次 timing_message.ini。"""
    while True:
        if not os.path.isfile("timing_message.ini"):
            now1 = datetime.datetime.now()
            logger.debug(f"Current: {now1.hour:02}:{now1.minute:02}")
            time.sleep(60 - now1.second)
            continue

        with open("timing_message.ini", "r", encoding="utf-8") as f:
            content = f.read().strip()

        if "⊕" in content:
            first_newline_pos = content.find("\n")
            if first_newline_pos != -1:
                first_line = content[:first_newline_pos]
                remaining_lines = content[first_newline_pos:]
                if "⊕" in first_line:
                    time_part, message_part = first_line.split("⊕", 1)
                    full_message = message_part + remaining_lines
                else:
                    full_message = content
            else:
                time_part, full_message = content.split("⊕", 1)
        else:
            full_message = content
            time_part = ""

        now = datetime.datetime.now()
        logger.debug(f"Current: {now.hour:02}:{now.minute:02}, target: {time_part}")
        if time_part and f"{now.hour:02}:{now.minute:02}" == time_part:
            logger.info("send timing messages")
            asyncio.run(send_msg_all_groups(full_message, actions, Manager, Segments, suffix_manager, logger))

        time.sleep(60 - now.second)
