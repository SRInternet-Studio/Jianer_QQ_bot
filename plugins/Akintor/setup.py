from plugins.Akintor.GameSession import *
from asyncio import sleep
from datetime import datetime, timedelta
import traceback
from akinator_python import Akinator
aki = Akinator(lang="cn")

from Hyper import Configurator
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

TRIGGHT_KEYWORD = "Any"
HELP_MESSAGE = f"{Configurator.cm.get_cfg().others["reminder"]}猜人 —> 启动 Akintor 游戏"

active_games = {}

def turn_on(gid, uid, game_data):
    active_games[gid] = GameSession(uid, gid, game_data)

def turn_off(gid):
    if gid in active_games:
        del active_games[gid]

def get_game_status(gid):
    return active_games.get(gid)

async def handle_timeout(event, actions, Manager, Segments):
    gid = event.group_id
    game_session = get_game_status(gid)
    
    await sleep(30)
    while get_game_status(gid):
        if datetime.now() < game_session.timeout:
            await sleep(30)
            continue
        else:
            await actions.send(group_id=gid, message=Manager.Message(Segments.At(game_session.uid),Segments.Text(f"由于超时，已为您自动结束游戏")))
            turn_off(gid)
            break

async def on_message(event, Events, actions, Manager, Segments, reminder):
    global aki
    if not isinstance(event, Events.GroupMessageEvent):
        return False
    
    if str(event.message) == f"{reminder}猜人":
        uid = event.user_id
        gid = event.group_id
        game_session = get_game_status(gid)
        if game_session:
            if uid == game_session.uid:
                game_session.timeout = datetime.now() + timedelta(seconds=30)
                await actions.send(
                    group_id=gid,
                    message=Manager.Message(Segments.Text(f"您已经开始游戏啦 ~"))
                )
            else:
                await actions.send(
                    group_id=gid,
                    message=Manager.Message(Segments.Text(
                        f"本群 {Segments.At(game_session.uid)} 正在进行中，请耐心等待~"))
                )
        else:
            try:
                aki = Akinator(lang="cn")
                game_data = aki.start_game()
                q = game_data  
                turn_on(gid, uid, aki)
                await actions.send(group_id=gid,message=Manager.Message(Segments.At(event.user_id), Segments.Text(f" 欢迎 ☆*: .｡. o(≧▽≦)o .｡.:*☆\n初始条件设置：{q}\n是(y)\n不是(n)\n我不知道(idk)\n或许是(p)\n或许不是(pn)\n上一题(b)\n退出(exit)")))
                await handle_timeout(event, actions, Manager, Segments)  
            except Exception as e:
                await actions.send(group_id=gid,message=Manager.Message(Segments.Text(f'服务器出问题了，一会再来玩吧\n{e}')))
                print(f"Akintor error:\n{traceback.format_exc()}")
        return True
    
    else:
        game_session = get_game_status(event.group_id)
        if not game_session:
            return False
        
        uid = event.user_id
        if uid != game_session.uid:
            return False
        
        reply = str(event.message)
        if reply in yes:
            r = aki.post_answer('y')
        elif reply in no:
            r = aki.post_answer('n')
        elif reply in idk:
            r = aki.post_answer('idk')
        elif reply in probably:
            r = aki.post_answer('p')
        elif reply in probablyn:
            r = aki.post_answer('pn')
        elif reply in back:
            r = aki.go_back()
        elif reply in exit114:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text('游戏已成功结束 ~~~ヾ(＾∇＾) \n(若仍有问题，无需理会)')))
            turn_off(event.group_id)
            return True
        else:
            return False

        game_session.question_count += 1

        if aki.answer_id:
            try:
                msg = f"是 {aki.name} ({aki.description})! 我猜对了么?"
                await actions.send(group_id=event.group_id,message=Manager.Message(Segments.At(event.user_id),Segments.Text(msg),Segments.Image(aki.photo)))
                turn_off(event.user_id)
            except Exception as e:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'抱歉，获取答案时出错了。\n{e}')))
            
            turn_off(event.group_id)
            return True
        else:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.At(event.user_id), Segments.Text(f"\n{aki.question}\n是(y)\n不是(n)\n我不知道(idk)\n或许是(p)\n或许不是(pn)\n上一题(b)\n退出(exit)")))
            game_session.timeout = datetime.now() + timedelta(seconds=60)  # 重置超时时间
            return True
