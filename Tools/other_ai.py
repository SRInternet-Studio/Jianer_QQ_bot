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
        print(self.key)
        print(self.ai_url)

    def Response(self):
        try:
            mode = self.mode
            input_data = self.message
            user_lists = self.user_lists
            uid = str(self.uid) 
            system_message = {"role": "system", "content": self.prompt} 

            if uid not in user_lists:
                user_lists[uid] = [system_message]
            else:
                user_input: list = user_lists[uid]
                if len(user_input) > 0 and user_input[0]["role"] != "system":
                    user_input = [msg for msg in user_input if msg['role'] != 'system']
                    user_input.insert(0,system_message) # Insert at the beginning

            user_input: list = user_lists[uid]
            history_limit = 7 
            if len(user_input) > history_limit + 1: 
                num_to_remove = len(user_input) - (history_limit + 1)
                user_input = [user_input[0]] + user_input[num_to_remove + 1:] 

            user_input.append({"role": "user", "content": input_data})
            print(str(self.uid) + " 的上下文：" + str(len(user_input)))

            client = OpenAI(

                api_key=self.key,
                base_url=self.ai_url,
            )


            try:
                chat_completion = client.chat.completions.create(
                    model=mode,
                    messages=user_input,
                    stream=True,
                )

                splitter = StreamSplitter()
                for message, _ in splitter.split_stream(chat_completion, 'openai'):
                    # print(f"[{time.time()}] RESPONSE: {repr(message)}")
                    yield message, 'message'


                user_input.append({"role": "assistant", "content": splitter.full_content})
                user_lists[uid] = user_input 
                yield user_lists, 'user_lists'

            except OpenAI.NotFoundError as e:
                print(f"OpenAI API Error: {e}")
                yield f"模型 '{mode}' 无法找到. 请检查模型名称是否正确，以及你的API KEY是否有权限访问该模型。\
{self.bn}发生错误，不能回复你的消息了，请稍候再试吧 ε(┬┬﹏┬┬)3", 'message'

            except OpenAI.PermissionDeniedError as e:
                error_response = str(e)
                if 'insufficient_user_quota' in error_response:
                    yield f"无效的 API KEY 是因为 配额已用尽 。\
{self.bn}发生错误，不能回复你的消息了，请稍候再试吧 ε(┬┬﹏┬┬)3", 'message'
                else:
                    raise 

            except OpenAI.BadRequestError as e:
                print(f"OtherAI bad request Error: {e}")
                yield f"与 你自定义的ai 通信出现问题: {e}。\
{self.bn}发生错误，不能回复你的消息了，请稍候再试吧 ε(┬┬﹏┬┬)3", 'message'

        except Exception as e:
            print(traceback.format_exc())
            yield f"{type(e)}\n{self.bn}发生错误，不能回复你的消息了，请稍候再试吧 ε(┬┬﹏┬┬)3", 'message'