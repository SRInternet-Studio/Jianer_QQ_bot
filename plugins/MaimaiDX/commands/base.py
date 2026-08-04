"""Base, OAuth, settings, fortune, recommendation, and ranking commands."""

import random
import re
from textwrap import dedent

from httpx import HTTPError as HTTPXError
from PIL import Image
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from jianer.plugins.builtin.alconna import Command

from .. import adapter
from ..config import log, lxnsconfig, maiconfig
from ..constants import FORTUNE, LEVEL_LIST
from ..core.clients.divingfish.client import DivingFishAPI
from ..core.clients.exceptions import HTTPError, UnknownError
from ..core.database.qq import update_user
from ..core.handler import (
    LxnsBindingPostPersistError,
    LxnsBindingTokenStorageError,
    bind_lxns,
    draw_chart_info,
    draw_rating_ranking,
    draw_rise_score_list,
    get_mai_what,
)
from ..core.image.tools import image_to_base64, song_chart
from ..core.lxns_oauth import (
    AuthorizationResponse,
    PendingBindingClaim,
    PendingBindingStore,
    build_authorize_url,
    extract_authorization_response,
    is_binding_channel_allowed,
    is_oob_redirect_uri,
)
from ..core.merge.models import ServiceName, Theme
from ..core.service import mai
from ..core.tool import qqhash
from ..message import MessageSegment
from ..resources import Root
from ..runtime import runtime
from .common import (
    command_argument,
    is_group,
    is_private,
    reject_unless_superuser,
    require_data,
    require_resources,
    require_user,
)


def _authorize_message(state: str) -> str:
    authorize_url = build_authorize_url(
        lxnsconfig.lx_client_id or "",
        lxnsconfig.redirect_uri or "",
        state=state,
    )
    if is_oob_redirect_uri(lxnsconfig.redirect_uri):
        return dedent(
            f"""\
    请完成落雪查分器账号绑定：

    1. 打开以下链接并允许「{maiconfig.bot_name} BOT」读取玩家数据
    =======================
    {authorize_url}
    =======================
    2. 授权完成后，复制页面显示的授权码
    3. 回到当前私聊，直接发送授权码，或发送「授权码：你的授权码」

    本次绑定有效期为 10 分钟，授权码只能使用一次；
    超时或失效后请重新发送「lxbind」获取授权链接。
    请在落雪查分器账号设置中开启允许读取成绩。"""
        ).strip()
    return dedent(
        f"""\
    请完成落雪查分器账号绑定：

    1. 打开以下链接并允许「{maiconfig.bot_name} BOT」访问您的落雪查分器数据
    =======================
    {authorize_url}
    =======================
    2. 授权完成后，复制浏览器地址栏中的完整回调链接
    3. 回到当前私聊，直接发送该链接（必须包含 code 和 state）

    本次绑定有效期为 10 分钟，授权码只能使用一次；
    超时或失效后请重新发送「lxbind」获取授权链接。
    请在落雪查分器账号设置中开启允许读取成绩。"""
    ).strip()


LXNS_ERROR = "BOT管理员尚未配置落雪查分器相关信息"
GROUP_BIND_GUIDE = (
    "BOT 管理员已将落雪查分器绑定设置为仅私聊。\n"
    "请添加 Bot 为好友后，在私聊中发送「lxbind」开始绑定。"
)
INVALID_CODE_MSG = "未识别到有效的落雪查分器 OAuth 授权码或回调链接。"
OAUTH_SESSION_REQUIRED_MSG = "请先发送「lxbind」创建新的落雪查分器绑定会话。"
OAUTH_STATE_MSG = (
    "落雪查分器绑定校验失败：回调链接缺少 state，或不属于当前绑定会话。"
    "请使用本次「lxbind」生成的授权链接重新授权。"
)
OAUTH_OOB_PRIVATE_MSG = "无回调地址授权只能在 Bot 私聊中提交授权码。"
OAUTH_IN_PROGRESS_MSG = "本次落雪查分器绑定正在处理中，请勿重复提交授权信息。"
OAUTH_FAILED_MSG = (
    "落雪查分器绑定失败：授权码可能已使用、已过期，或授权未成功。"
    "当前绑定会话仍有效，可以重新打开本次授权链接获取新的授权码。"
)
BINDING_TEMPORARY_FAILED_MSG = (
    "落雪查分器绑定暂时失败：网络、响应数据或本地数据库出现异常。"
    "当前绑定会话仍有效，可以稍后重试。"
)
OAUTH_TOKEN_STORAGE_FAILED_MSG = (
    "落雪查分器已签发授权令牌，但 BOT 未能安全保存；该授权码已经失效。"
    "请重新打开本次授权链接，获得新的授权码后再试。"
)
OAUTH_TOKEN_SAVED_MSG = (
    "落雪查分器授权令牌已保存，但好友码暂时未能同步。"
    "无需重复授权，可稍后直接使用查分命令重试。"
)
pending_bindings = PendingBindingStore()


async def complete_lxns_binding(user, code):
    try:
        result = await bind_lxns(user, code)
    except LxnsBindingPostPersistError as error:
        cause = error.__cause__ or error
        log.warning(f"落雪查分器 OAuth 好友码同步失败：{type(cause).__name__}")
        return OAUTH_TOKEN_SAVED_MSG, True
    except LxnsBindingTokenStorageError as error:
        cause = error.__cause__ or error
        log.warning(f"落雪查分器 OAuth 令牌保存失败：{type(cause).__name__}")
        return OAUTH_TOKEN_STORAGE_FAILED_MSG, False
    except HTTPError as error:
        log.warning(f"落雪查分器 OAuth 绑定失败：{type(error).__name__}")
        return OAUTH_FAILED_MSG, False
    except (HTTPXError, UnknownError, ValidationError, SQLAlchemyError) as error:
        log.warning(f"落雪查分器 OAuth 绑定暂时失败：{type(error).__name__}")
        return BINDING_TEMPORARY_FAILED_MSG, False
    return result, result == "授权完成。"


async def _complete_claimed_lxns_binding(
    response: AuthorizationResponse,
    claim: PendingBindingClaim,
    event,
    actions,
) -> bool:
    try:
        if not await require_data(event, actions):
            pending_bindings.release(claim)
            return True
        user = await require_user(event, actions, allow_mention=False)
        if user is None:
            pending_bindings.release(claim)
            return True
        result, succeeded = await complete_lxns_binding(user, response.code)
    except BaseException:
        pending_bindings.release(claim)
        raise

    if succeeded:
        pending_bindings.complete(claim)
    else:
        pending_bindings.release(claim)
    await adapter.send(event, actions, result)
    return True


async def _claim_and_complete_lxns_binding(
    response: AuthorizationResponse,
    event,
    actions,
) -> bool:
    oob = is_oob_redirect_uri(lxnsconfig.redirect_uri)
    if oob and response.state is None:
        if not is_private(event):
            await adapter.send(event, actions, OAUTH_OOB_PRIVATE_MSG)
            return True
        claim = pending_bindings.claim_without_state(event.self_id, event.user_id)
    else:
        claim = pending_bindings.claim(
            event.self_id,
            event.user_id,
            response.state,
        )
    if claim is None:
        message = (
            OAUTH_IN_PROGRESS_MSG
            if pending_bindings.is_in_flight(event.self_id, event.user_id)
            else OAUTH_STATE_MSG
        )
        await adapter.send(event, actions, message)
        return True
    return await _complete_claimed_lxns_binding(response, claim, event, actions)


@Command("更新maimai数据").handle()
async def handle_update_data(event=None, actions=None):
    if await reject_unless_superuser(event, actions):
        return True
    if not is_private(event):
        await adapter.send(event, actions, "请私聊 BOT 使用该命令。")
        return True
    ok = await runtime.initialize(force=True)
    await adapter.send(event, actions, "maimai数据更新完成" if ok else "maimai数据更新失败")
    return True


@Command("帮助maimaiDX").handle()
@Command("帮助maimaidx").handle()
async def handle_help(event=None, actions=None):
    if not await require_resources(event, actions):
        return True
    await adapter.send(
        event,
        actions,
        MessageSegment.image(image_to_base64(Image.open(Root / "maimaidxhelp.png"))),
    )
    return True


@Command("项目地址maimaiDX").handle()
@Command("项目地址maimaidx").handle()
async def handle_repository(event=None, actions=None):
    await adapter.send(
        event,
        actions,
        "项目地址：https://github.com/Yuri-YuzuChaN/nonebot-plugin-maimaidx\n"
        "JianerCore 移植版固定基线：v3.0.13 / 83a1bee",
    )
    return True


async def _bind(argument, event, actions, command):
    private = is_private(event)
    oob = is_oob_redirect_uri(lxnsconfig.redirect_uri)
    if oob and not private:
        await adapter.send(event, actions, GROUP_BIND_GUIDE)
        return True
    if not is_binding_channel_allowed(
        private_only=lxnsconfig.lxns_bind_private_only,
        is_private=private,
    ):
        await adapter.send(event, actions, GROUP_BIND_GUIDE)
        return True
    if not all((lxnsconfig.lx_client_id, lxnsconfig.lx_client_secret, lxnsconfig.redirect_uri)):
        await adapter.send(event, actions, LXNS_ERROR + "，无法进行绑定授权。")
        return True
    text = command_argument(event, command, argument)
    if not text:
        state = pending_bindings.start(event.self_id, event.user_id)
        if oob:
            guide = "请在当前私聊发送页面显示的授权码。"
        elif private:
            guide = "请在当前私聊发送完整回调链接。"
        else:
            guide = "建议在 Bot 私聊中发送完整回调链接。"
        await adapter.send(event, actions, f"{_authorize_message(state)}\n\n{guide}")
        return True
    if not pending_bindings.is_active(event.self_id, event.user_id):
        await adapter.send(event, actions, OAUTH_SESSION_REQUIRED_MSG)
        return True
    response = extract_authorization_response(text)
    if response.code is None:
        await adapter.send(event, actions, INVALID_CODE_MSG)
        return True
    return await _claim_and_complete_lxns_binding(response, event, actions)


for _bind_command in ("lxbind", "绑定落雪", "绑定lx"):
    Command(_bind_command).handle()(
        lambda event=None, actions=None, _command=_bind_command: _bind(
            "", event, actions, _command
        )
    )
    Command(f"{_bind_command} <argument>").handle()(
        lambda argument="", event=None, actions=None, _command=_bind_command: _bind(
            argument, event, actions, _command
        )
    )


async def handle_pending_oauth(event, actions):
    if not is_binding_channel_allowed(
        private_only=lxnsconfig.lxns_bind_private_only,
        is_private=is_private(event),
    ):
        return False
    if not pending_bindings.is_active(event.self_id, event.user_id):
        return False
    response = extract_authorization_response(str(getattr(event, "msg_str", "")))
    if response.code is None:
        return False
    return await _claim_and_complete_lxns_binding(response, event, actions)


async def _source(argument, event, actions):
    if not await require_data(event, actions):
        return True
    user = await require_user(event, actions, allow_mention=False)
    if user is None:
        return True
    source = ServiceName.get_by_index(command_argument(event, "数据源", argument))
    if source is None:
        await adapter.send(event, actions, f"未找到该数据源：\n{ServiceName.get_help()}")
        return True
    if (
        source == ServiceName.LXNS
        and lxnsconfig.lxns_dev_token is None
        and (lxnsconfig.lx_client_id is None or lxnsconfig.redirect_uri is None)
    ):
        await update_user(user.qqid, service=ServiceName.DIVINGFISH)
        await adapter.send(
            event,
            actions,
            LXNS_ERROR + "。为防止无法查询成绩，已切换为水鱼查分器。",
        )
        return True
    await update_user(user.qqid, service=source)
    await adapter.send(event, actions, f"数据源已切换为：「{source.display_name}」")
    return True


@Command("数据源").handle()
@Command("数据源 <argument>").handle()
async def handle_source(argument: str = "", event=None, actions=None):
    return await _source(argument, event, actions)


@Command("主题").handle()
@Command("主题 <argument>").handle()
async def handle_theme(argument: str = "", event=None, actions=None):
    if not await require_data(event, actions):
        return True
    user = await require_user(event, actions, allow_mention=False)
    if user is None:
        return True
    theme = Theme.get_by_index(command_argument(event, "主题", argument))
    if theme is None:
        await adapter.send(event, actions, f"未找到该主题：\n{Theme.get_help()}")
        return True
    await update_user(user.qqid, theme=theme)
    await adapter.send(event, actions, f"主题已切换为：「{theme.value}」")
    return True


@Command("今日舞萌").handle()
async def handle_fortune(event=None, actions=None):
    if not await require_resources(event, actions):
        return True
    user = await require_user(event, actions)
    if user is None:
        return True
    fortune_hash = qqhash(user.qqid)
    daily_random = random.Random(fortune_hash)
    rp = fortune_hash % 100
    flags = []
    value = fortune_hash
    for _ in range(11):
        flags.append(value & 3)
        value >>= 2
    lines = [f"今日人品值：{rp}"]
    for index, flag in enumerate(flags):
        if flag == 3:
            lines.append(f"宜 {FORTUNE[index]}")
        elif flag == 0:
            lines.append(f"忌 {FORTUNE[index]}")
    song = daily_random.choice(mai.total_list.root)
    levels = "/".join(str(item.level_value) for item in song.difficulties)
    message = MessageSegment.text(
        "\n".join(lines)
        + f"\n{maiconfig.bot_name} Bot提醒您：打机时不要大力拍打或滑动哦"
        + f"\n今日推荐歌曲：ID.{song.song_id} - {song.song_name}"
    )
    message += MessageSegment.image(image_to_base64(Image.open(song_chart(song.song_id))))
    message += MessageSegment.text(levels)
    await adapter.send(event, actions, message)
    return True


@Command("查看排名").handle()
@Command("查看排名 <argument>").handle()
async def handle_rating_ranking(argument: str = "", event=None, actions=None):
    if not await require_resources(event, actions):
        return True
    args = command_argument(event, "查看排名", argument)
    name, page = ("", int(args)) if args.isdigit() else (args.lower(), 1)
    await adapter.send(event, actions, await draw_rating_ranking(name, page))
    return True


@Command("我的排名").handle()
async def handle_my_rating_ranking(event=None, actions=None):
    if not await require_data(event, actions):
        return True
    user = await require_user(event, actions)
    if user is None:
        return True
    api = DivingFishAPI(qqid=user.qqid)
    info = await api.query_user_b50()
    rank_data = await api.rating_ranking()
    for number, rank in enumerate(rank_data, 1):
        if rank.username == info.username:
            await adapter.send(
                event,
                actions,
                f"您的Rating为「{rank.ra}」，排名第「{number}」名",
            )
            return True
    await adapter.send(event, actions, "未在查分器排行榜中找到您的记录。")
    return True


async def handle_natural_patterns(event, actions):
    text = str(getattr(event, "msg_str", "") or "").strip()
    match = re.fullmatch(r".*mai.*什么(.+)?", text)
    if match:
        if not await require_resources(event, actions):
            return True
        user = await require_user(event, actions, check_auth=True, silent_auth_error=True)
        song = mai.total_list.random()
        point = match.group(1)
        if point and any(word in point for word in ("推分", "上分", "加分")) and user:
            recommended = await get_mai_what(user)
            if recommended is not None:
                song = recommended
        await adapter.send(event, actions, await draw_chart_info(song, user))
        return True

    match = re.fullmatch(
        r"[随来给]个((?:dx|sd|标准))?([绿黄红紫白]?)([0-9]+\+?).*",
        text,
        re.IGNORECASE,
    )
    if match:
        if not await require_resources(event, actions):
            return True
        user = await require_user(event, actions, check_auth=True, silent_auth_error=True)
        chart_type = (match.group(1) or "").lower()
        types = ["DX"] if chart_type == "dx" else ["SD"] if chart_type in {"sd", "标准"} else ["SD", "DX"]
        level, color = match.group(3), match.group(2)
        songs = mai.total_list.filter(level=level, type=types)
        if color:
            index = "绿黄红紫白".index(color)
            songs = [
                song
                for song in songs
                if len(song.difficulties) > index and song.difficulties[index].level == level
            ]
        result = "没有这样的乐曲哦。" if not songs else await draw_chart_info(random.choice(songs), user)
        await adapter.send(event, actions, result)
        return True

    match = re.fullmatch(r"我要在?([0-9]+\+?)?[上加\+]([0-9]+)?分\s?(.+)?", text)
    if match:
        if not await require_resources(event, actions):
            return True
        user = await require_user(event, actions, check_auth=True)
        if user is None:
            return True
        level, score = match.group(1), match.group(2)
        if level and level not in LEVEL_LIST:
            await adapter.send(event, actions, "无此等级")
            return True
        await adapter.send(
            event,
            actions,
            await draw_rise_score_list(user, level, int(score) if score else None),
        )
        return True
    return False
