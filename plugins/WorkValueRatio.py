from Hyper import Configurator
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

TRIGGHT_KEYWORD = "工作性价比计算"
HELP_MESSAGE = f"{Configurator.cm.get_cfg().others["reminder"]}工作性价比计算 —> 计算你的工作性价比💫"

async def on_message(event, actions, Manager, Segments, order):
    help_msg = f"""计算你的工作性价比💫
    
📌 请按照以下使用格式：
{Configurator.cm.get_cfg().others["reminder"]}工作性价比计算 【月薪】 【每月工作小时数】 【房租房贷】 【生活开支】
示例：{Configurator.cm.get_cfg().others["reminder"]}工作性价比计算 15000 160 5000 3000
（注：不接受"我梦里搬砖"或"喝西北风"等奇幻数值）"""
    r = ""

    parts = order.split()
    if len(parts) != 5 or parts[0] != "工作性价比计算":
        r = help_msg
    else:
        try:
            salary = float(parts[1].replace('元','').replace('万','e4'))
            hours = float(parts[2].replace('小时','').replace('时',''))
            rent = float(parts[3].replace('元','').replace('万','e4'))
            living = float(parts[4].replace('元','').replace('万','e4'))
            
            if any(x < 0 for x in [salary, hours, rent, living]):
                r = "负...负数？！Σ(°△°|||) 您是在黑洞里工作吗？"
            elif salary > 1e8:
                r = f"{salary:.0f}元？！您是把冥币汇率算进去了吧？( ﾟ∀。)"
            elif hours > 744:
                r = f"每月{hours}小时？您是需要脑科医生还是时光机？(；一_一)"
            elif (rent + living) > salary*10:
                r = "这开支...您是在迪拜养狮子吗？(´⊙ω⊙`)"
            elif hours == 0:
                r = "喂喂~杂鱼，你每个月工作0小时是在梦里打工吗？(￣▽￣*)ゞ"
            elif (rent + living) == 0:
                r = "哇哦~这位吃露水的，开支0元，你活在二次元不需要花钱的吗？(+_+)?"
            else:
                ratio = salary / (rent + living)
                
                r = f'''时薪：{salary/hours:.1f}元/h | 
生活成本：{(rent+living)/hours:.1f}元/h | 
性价比指数：{(salary/(rent+living)):.1f}x（>1.5为优，<1需警惕）

'''

                if ratio > 1.5:
                    r += "🌈 状态良好！当前收入覆盖生活成本有余"
                elif ratio > 1:
                    r += "⚠️ 收支平衡，建议关注职业发展机会"
                else:
                    r += "🚨 预警！考虑调整工作或缩减开支"
        
        except ValueError:
            r = "⚠️ 参数必须为数字\n" + help_msg
    
    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(r)))
    return True