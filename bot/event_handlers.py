"""非消息事件处理：戳一戳、入群欢迎、退群提示、入群邀请审批、重启完成通知。"""
import os
import random
import traceback


async def handle_notify_poke(actions, Manager, Segments, event, config, logger):
    """处理戳一戳。"""
    if not (str(event.sub_type) == "poke" and int(event.target_id) == int(event.self_id)):
        return
    logger.info(f"({event.user_id}) POKED")
    try:
        if event.group_id:
            poke_result = await actions.custom.group_poke(group_id=event.group_id, user_id=event.user_id)
            poke_result = Manager.Ret.fetch(poke_result).data.raw
            if poke_result.get("status", "error") != "ok":
                logger.warning(f"sys: 戳一戳失败 {poke_result}")
            await actions.send(group_id=event.group_id, message=Manager.Message(
                Segments.Text(random.choice(config.others["poke_rejection_phrases"]))))
        elif event.user_id:
            poke_result = await actions.custom.friend_poke(user_id=event.user_id)
            poke_result = Manager.Ret.fetch(poke_result).data.raw
            if poke_result.get("status", "error") != "ok":
                logger.warning(f"sys: 戳一戳失败 {poke_result}")
            await actions.send(user_id=event.user_id, message=Manager.Message(
                Segments.Text(random.choice(config.others["poke_rejection_phrases"]))))
    except KeyError:
        logger.warning("不接受戳一戳")


async def handle_listener_start_notify(actions, Manager, Segments, event,
                                       bot_name, bot_name_en, ONE_SLOGAN, reminder, ROOT_User):
    """处理重启完成通知（如有 restart.temp 则向源群发送恢复消息）。"""
    if not os.path.exists("restart.temp"):
        return
    with open("restart.temp", "r", encoding="utf-7") as f:
        group_id = f.read()
    os.remove("restart.temp")
    r_admin = f'''在 {event.time_str} QQ机器人已手动重启成功'''
    await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin)))
    await actions.send(group_id=group_id, message=Manager.Message(Segments.Text(
        f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
欢迎! {bot_name} 已经重启成功！ 现在你可以发送 {reminder}帮助 来知道更多。''')))


async def handle_member_increase(actions, Manager, Segments, event, bot_name, reminder):
    """处理新成员加入：发送欢迎消息。"""
    user = event.user_id
    welcome = f''' 加入{bot_name}的大家庭，{bot_name}是你最忠实可爱的女朋友噢o(*≧▽≦)ツ
随时和{bot_name}交流，你只需要在问题的前面加上 {reminder} 就可以啦！( •̀ ω •́ )✧
@{bot_name} 可以看看{bot_name}会做什么有趣的事情哦~o((>ω< ))o
祝你在{bot_name}的大家庭里生活愉快！♪(≧∀≦)ゞ☆'''
    await actions.send(group_id=event.group_id, message=Manager.Message(
        Segments.Image(f"http://q2.qlogo.cn/headimg_dl?dst_uin={user}&spec=640"),
        Segments.Text("欢迎"), Segments.At(user), Segments.Text(welcome)))


async def handle_member_decrease(actions, Manager, Segments, event, bot_name, logger, get_user_nickname):
    """处理成员退群：发送告别消息。"""
    user_nick = await get_user_nickname(event.user_id, Manager, actions)
    if user_nick:
        user_nick = f"@{user_nick} "
    else:
        user_nick = "有人又"
    text = f'''{user_nick}离开了{bot_name}的大家庭，{bot_name}好伤心o(TヘTo)……
大家一定要记得多来陪{bot_name}玩玩ヾ(•ω•`)o'''
    logger.info(f"group: {event.user_id} 已离开群聊 {event.group_id}")
    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(text)))


async def handle_group_add_invite(actions, Manager, Segments, event, config, logger,
                                  bot_name, reminder, get_user_nickname, set_wait_flag):
    """处理入群邀请：按关键词自动审批。审批通过时调用 set_wait_flag(True) 通知主流程跳过下一次欢迎。"""
    keywords: list = config.others["Auto_approval"]
    cleaned_text = event.comment.strip().lower()

    for keyword in keywords:
        processed_keyword = keyword.strip().lower()
        if processed_keyword in cleaned_text:
            try:
                user = event.user_id
                nick = await get_user_nickname(user, Manager, actions)
                logger.info(f"group: {nick} 的入群回答 {processed_keyword} 符合正确答案，已准许入群 {event.group_id}")
                await actions.set_group_add_request(flag=event.flag, sub_type=event.sub_type, approve=True, reason="")
                set_wait_flag(True)
                welcome = f'''{nick} 的答案正确，欢迎加入{bot_name}的大家庭！o(*≧▽≦)ツ
随时和{bot_name}交流，只需在问题的前面加上 {reminder} 就可以啦！( •̀ ω •́ )✧
@{bot_name} 可以看看{bot_name}会做什么有趣的事情哦~o((>ω< ))o
祝你在{bot_name}的大家庭里生活愉快！♪(≧∀≦)ゞ☆'''
                await actions.send(group_id=event.group_id, message=Manager.Message(
                    Segments.Image(f"http://q2.qlogo.cn/headimg_dl?dst_uin={user}&spec=640"),
                    Segments.Text(welcome)))
                break
            except Exception:
                logger.error(traceback.format_exc())
