import subprocess, gc
from plugins.RunCommand.execute_command import execute_command
from plugins.RunCommand.DANGEROUS_PATTERNS import DANGEROUS_PATTERNS
from Hyper import Configurator
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

TRIGGHT_KEYWORD = "runcommand"
HELP_MESSAGE = f"{Configurator.cm.get_cfg().others["reminder"]}runcommand (命令，必填) —> 通过命令实现更多功能（需要SU）"

async def on_message(event, actions, Manager, Segments, re, order, Super_User, ROOT_User, bot_name, CONFUSED_WORD):
    if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User:
        # 提取命令
        command = order.removeprefix("runcommand").strip()
        command_lower = command.lower()
        
        # 日志记录
        print(f"检查并执行命令: {command}")
        r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 执行了以下命令: \n {command}'''
        await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin))) #管理员操作通知ROOT用户
        
        is_dangerous = False
        for pattern in DANGEROUS_PATTERNS:
            try:
                re.compile(pattern)
                if re.search(pattern, command_lower):
                    is_dangerous = True
                    print(f"检测到危险命令: {pattern}")
                    break
            except re.error as e:
                print(f"✗ 无效屏蔽词条: {pattern}\n   错误: {e}")
        
        if is_dangerous:
            await actions.send(group_id=event.group_id, 
                            message=Manager.Message(Segments.Text(
                                                    f"""命令执行结果:
❌ ERROR 危险命令，已屏蔽。
ℹ️ INFO 不被允许的命令: {command}""")))
            return True
        
        command114514 = execute_command(command, subprocess)
        if command114514["returncode"] == 0:
            if str(command_lower.split(" ")[0]) == "echo":
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"""{str(command114514["stdout"]).strip('\n')}""")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"""命令执行结果:
ℹ️ INFO 执行成功
ℹ️ INFO {command114514["stdout"]}.""")))
                
        elif command114514["stderr"]:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"""命令执行结果:
❌ ERROR 执行失败,代码命令可能有误
ℹ️ INFO {command114514["stderr"]}.""")))
        else:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"""命令执行结果:
❌ ERROR 执行失败,代码命令可能有误
ℹ️ INFO {command114514["stderr"]}.
❌ERROR 返回码:{command114514['returncode']}.""")))
                
    else:
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
        
    return True

async def get_user_info(uid, Manager, actions):
    try:
        gc.collect()
        info = Manager.Ret.fetch(await actions.custom.get_stranger_info(user_id=uid, no_cache=True))
        if 'nickname' not in info.data.raw:
            raise ValueError(f"{uid} is not a valid user ID.")
        return True, info.data.raw
    except Exception as e:
        print(f"tools: 获取用户 {uid} 信息失败: {e}")
        return False, str(uid)
    
async def get_user_nickname(uid, Manager, actions) -> str:
    s, user_info = await get_user_info(uid, Manager, actions)
    if s:
        return f"@{user_info['nickname']}({uid})"
    else:
        return str(uid)