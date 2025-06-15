import time
import traceback
from Tools.AI_tools import *
from openai import OpenAI


class OtherAI():
    def __init__(self, prompt, message, user_lists, uid, mode, bn, key, ai_url) -> None:
        self.prompt = prompt
        self.message = message
        self.user_lists = user_lists
        self.uid = uid
        self.bn = bn
        self.mode = mode
        self.key = key
        self.ai_url = ai_url
        self.client = OpenAI(
            api_key=self.key,
            base_url=self.ai_url,
            timeout=30
        )

    def Response(self):
        try:
            mode = self.mode
            input_data = self.message
            user_lists = self.user_lists
            uid = str(self.uid)
            system_message = {"role": "system", "content": self.prompt}

            # 检查 uid 是否存在于 user_lists 中，如果不存在则初始化一个空列表
            if uid not in user_lists:
                user_lists[uid] = []

            user_input = user_lists[uid]

            # 检查第一条消息是否为系统消息
            if len(user_input) > 0 and user_input[0]["role"] != "system":
                # 过滤掉除第一条消息外的所有系统消息
                filtered_messages = [msg for msg in user_input if msg['role'] != 'system' or msg == user_input[0]]
                user_input = filtered_messages

            if user_input and user_input[0]["role"] != "system":
                user_input.insert(0, system_message)
            else:
                user_input = [system_message] + user_input

            history_limit = 15
            if len(user_input) > history_limit:
                # 只保留最近的 history_limit 条消息
                user_input = user_input[-history_limit:]

            user_input.append({"role": "user", "content": input_data})
            print(f"{self.uid} 的上下文：{len(user_input)}")

            try:
                chat_completion = self.client.chat.completions.create(
                    model=mode,
                    messages=user_input,
                    stream=True,
                )

                splitter = StreamSplitter()
                for message, _ in splitter.split_stream(chat_completion, 'openai'):
                    yield message, 'message'

                user_input.append({"role": "assistant", "content": splitter.full_content})
                user_lists[uid] = user_input
                yield user_lists, 'user_lists'

            except self.client.NotFoundError as e:
                print(f"OpenAI API Error: {e}")
                yield (f"模型 '{mode}' 无法找到. 请检查模型名称是否正确，以及你的API KEY是否有权限访问该模型。"
                       f"{self.bn}发生错误，不能回复你的消息了，请稍候再试吧 ε(┬┬﹏┬┬)3", 'message')
                user_input.append({"role": "system", "content": f"Last request failed: {time.ctime()}"})
                user_lists[uid] = user_input

            except self.client.PermissionDeniedError as e:
                error_response = str(e)
                if 'insufficient_user_quota' in error_response:
                    yield (f"无效的 API KEY 是因为配额已用尽 。"
                           f"{self.bn}发生错误，不能回复你的消息了，请稍候再试吧 ε(┬┬﹏┬┬)3", 'message')
                    user_input.append({"role": "system", "content": f"Last request failed: {time.ctime()}"})
                    user_lists[uid] = user_input
                else:
                    raise

            except self.client.BadRequestError as e:
                print(f"OtherAI bad request Error: {e}")
                yield (f"与你自定义的AI通信出现问题: {e}。\n"
                       f"{self.bn}发生错误，不能回复你的消息了，请稍候再试吧ε(┬┬﹏┬┬)3 ", 'message')
                user_input.append({"role": "system", "content": f"Last request failed: {time.ctime()}"})
                user_lists[uid] = user_input

        except Exception as e:
            print(traceback.format_exc())
            yield (f"{type(e).__name__}\n{self.bn}发生错误，不能回复你的消息了，请稍候再试吧 ε(┬┬﹏┬┬)3", 'message')
            if 'user_input' in locals():
                user_input.append({"role": "system", "content": f"Last request failed: {time.ctime()}"})
                user_lists[uid] = user_input



