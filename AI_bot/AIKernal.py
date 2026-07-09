from __future__ import annotations
from Tools.GoogleAI import Context, Parts, Roles, Schema
from Tools.SearchOnline import network_gpt as SearchOnline
from Tools.deepseek import dsr114 as deepseek
from Tools.Sanitizer_Tools import sanitize_for_tts
from Tools.tools import replace_at_with_nickname, get_user_nickname, replace_scheme_with_http
from jianer import configurator as Configurator
from jianer import events as Events, common as Manager, segments as Segments
from jianer.events import *
from typing import Any, Union

MAX_MESSAGE_LENGTH = 3
if __name__ == "__main__":
    from main import ContextManager

class AIKernal:
    def __init__(self, actions: Any, config: Configurator.BotConfig,
                 bot_name: str, reminder: str = "") -> None:
        self.bot_name = bot_name
        self.actions = actions
        self.reminder = reminder
        self.config = config
        self.replace_at_with_nickname = replace_at_with_nickname
        self.url = ""
        self.sended = False
        self.sendedID = []
        self.messages_for_node = []
        self.enable_forward_msg_num = False
        self.result = ""
        self.reply_private_msg = False
        self.event = None

    async def generate_response(self, EnableNetwork: str, cmc: ContextManager, sys_prompt: str, user_lists: dict,
                                event: Union[Events.GroupMessageEvent, Events.PrivateMessageEvent], state_user_id=None):
        self.url = ""
        self.sended = False
        self.sendedID = []
        self.messages_for_node = []
        self.enable_forward_msg_num = False
        self.result = ""
        self.reply_private_msg = False
        self.event = event
        self.user_lists = user_lists
        state_user_id = event.user_id if state_user_id is None else state_user_id

        if isinstance(event, Events.PrivateMessageEvent):
            self.reply_private_msg = True
        
        match EnableNetwork:
            case "GoogleGemini":
                new = await self.build_message_content()
                response_stream = cmc.get_context(state_user_id, event.group_id, sys_prompt, sys_prompt, self.config).gen_content(
                    Roles.User(*new),
                    model_override=self.config.others.get("gemini_model", "gemini-2.0-flash-exp")
                )
                await self.handle_message_stream(response_stream, False)

            case "GPT-3.5" | "Net":
                model_name = "gpt-3.5-turbo-16k" if EnableNetwork == "GPT-3.5" else "gpt-4o-mini"
                msg = await self.process_reply_message("")
                msg += str(await self.replace_at_with_nickname(event.message, Manager, Segments, self.actions))
                search = SearchOnline(
                    sys_prompt, msg, self.user_lists, state_user_id,
                    model_name, self.bot_name, 
                    self.config.others["openai_key"]
                )
                await self.handle_message_stream(search.Response())

            case "Ds":
                msg = await self.process_reply_message("")
                msg += str(await self.replace_at_with_nickname(event.message, Manager, Segments, self.actions))
                search = deepseek(
                    sys_prompt, msg, self.user_lists, state_user_id,
                    "deepseek-chat", self.bot_name,
                    self.config.others["deepseek_key"]
                )
                await self.handle_message_stream(search.Response())

        self.result = self.result.rstrip()
        await self.finalize_messages()

        if not self.sended:
            await self.send_message([Segments.Text(self.result)], True)

        return cmc, self.user_lists, self.result

    async def process_reply_message(self, msg):
        # 优先处理引用消息
        if isinstance(self.event.message[0], Segments.Reply):
            content = await self.actions.get_msg(self.event.message[0].id)
            message = gen_message({"message": content.data["message"]})
            for i in message:
                if isinstance(i, Segments.Text):
                    msg += f"{i.text} "
                elif isinstance(i, Segments.At):
                    msg += f"@{await get_user_nickname(i.qq, Manager, self.actions)} "

        return msg

    async def build_message_content(self):
        new = []
        # 处理引用消息中的内容
        if isinstance(self.event.message[0], Segments.Reply):
            content = await self.actions.get_msg(self.event.message[0].id)
            message = gen_message({"message": content.data["message"]})
            for i in message:
                await self.handle_content_item(i, new)
                
        # 处理当前消息内容
        for i in self.event.message:
            await self.handle_content_item(i, new)
        return new

    async def handle_content_item(self, item, container: list):
        if isinstance(item, Segments.Text):
            container.append(Parts.Text(item.text.replace(self.reminder, "", 1)))
        elif isinstance(item, Segments.Image):
            url = item.file if item.file.startswith("http") else item.url
            print(f"AI: URL位置 {replace_scheme_with_http(url)}")
            container.append(Parts.File.upload_from_url(replace_scheme_with_http(url)))
            print("AI: 有图")
        elif isinstance(item, Segments.At):
            nickname = await get_user_nickname(item.qq, Manager, self.actions)
            container.append(Parts.Text(f"@{nickname}"))
        else:
            container.append(Parts.Text(str(item)))

    async def handle_message_stream(self, response_stream, is_openai=True):
        for partial, r_type in response_stream:
            if is_openai:
                if r_type != 'message':
                    self.user_lists = partial
                    continue

            message = Segments.Text(str(partial))
            if self.enable_forward_msg_num:
                self.messages_for_node.append(message)
            else:
                if not self.sended:
                    await self.send_message([message], True)
                else:
                    await self.send_message([message])
                self.messages_for_node.append(message)
            
            if len(self.messages_for_node) > MAX_MESSAGE_LENGTH - 1 and not self.enable_forward_msg_num and not self.reply_private_msg:
                self.enable_forward_msg_num = True

            if self.enable_forward_msg_num and len(self.messages_for_node) == MAX_MESSAGE_LENGTH + 1:
                self.sendedID.append(await self.send_message([Segments.Text(r"**[thinking]**")]))

            self.sended = True
            self.result += str(partial) + '\n'

    async def send_message(self, msg: list[Segments.Base], is_reply=False) -> Manager.Ret:
        if self.reply_private_msg:
            return await self.actions.send(
                user_id=self.event.user_id,
                message=Manager.Message(*msg)
            )
        else:
            if is_reply:
                return await self.actions.send(
                    group_id=self.event.group_id,
                    message=Manager.Message(Segments.Reply(self.event.message_id), *msg)
                )
            else:
                return await self.actions.send(
                    group_id=self.event.group_id,
                    message=Manager.Message(*msg)
                )

    async def finalize_messages(self):
        if self.enable_forward_msg_num:
            # 删除临时消息
            for msg_id in self.sendedID:
                await self.actions.del_message(msg_id.data.message_id) # 禁用消息连续撤回以防止QQ检测
            
            for m in range(len(self.messages_for_node)):
                self.messages_for_node[m] = Segments.CustomNode(
                    str(self.event.self_id),
                    self.bot_name,
                    Manager.Message(self.messages_for_node[m])
                )
            
            # 发送合并转发
            if len(self.messages_for_node) > MAX_MESSAGE_LENGTH:
                await self.actions.send_group_forward_msg(
                    group_id=self.event.group_id,
                    message=Manager.Message(*self.messages_for_node)
                )
