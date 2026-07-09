from Tools.GoogleAI import Context
from jianer import configurator as Configurator

tools = [] #["google_search"]
generation_config = {
    "temperature": 1,
    "top_p": 0.95,
    "max_output_tokens": 8192,
}

user_lists = {}
class ContextManager:
    def __init__(self):
        self.groups: dict[int, dict[int, Context]] = {}

    def get_context(self, uin: int, gid: int, system_instruction: str = "", sys_prompt: str = "",
                    config: Configurator.BotConfig = None):
        global tools, generation_config
        if config is None:
            config = Configurator.BotConfig.get("jianer-bot")
        try:
            key = config.others["gemini_key"]
            model = config.others.get("gemini_model", "gpt-4o-mini")
            context = self.groups[gid][uin]
            # 如果传入了新的system_instruction，更新context
            if system_instruction and context.system_instruction != system_instruction:
                context.system_instruction = system_instruction
            return context
        except KeyError:
            if self.groups.get(gid):
                self.groups[gid][uin] = Context(
                    api_key=key, 
                    model=model, 
                    base_url=config.others["gemini_base_url"], 
                    tools=tools,
                    system_instruction=system_instruction or sys_prompt,
                    generation_config=generation_config
                )
                return self.groups[gid][uin]
            else:
                self.groups[gid] = {}
                self.groups[gid][uin] = Context(
                    api_key=key, 
                    model=model, 
                    base_url=config.others["gemini_base_url"], 
                    tools=tools,
                    system_instruction=system_instruction or sys_prompt,
                    generation_config=generation_config
                )
                return self.groups[gid][uin]
            
    def del_context(self, uin: int, gid: int):
        global user_lists
        if gid in self.groups and uin in self.groups[gid]:
            del self.groups[gid][uin]
        if str(uin) in user_lists:
            del user_lists[str(uin)]

