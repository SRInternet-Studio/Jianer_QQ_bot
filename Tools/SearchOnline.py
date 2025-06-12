import openai, time
import traceback
from Tools.AI_tools import *

class network_gpt():
    def __init__(self, prompt, message, user_lists, uid, mode, bn, key) -> None:
        self.prompt = prompt
        self.message = message
        self.user_lists = user_lists
        self.uid = uid
        self.mode = mode
        self.bn = bn
        self.key = key

    def Response(self):
        try:
            mode = self.mode #"gpt-3.5-turbo-16k"
            input_data = self.message
            user_lists = self.user_lists
            image_url = ""

            if str(self.uid) not in user_lists:
                user_lists[str(self.uid)] = []
                
            user_input: list = user_lists[str(self.uid)]

            if len(user_input) >= 15:
                user_input.pop(0)
                user_input.pop(0)
                user_input.pop(0)

            user_input.append({"role": "system","content": self.prompt})
            user_input.append({"role": "user", "content": input_data})

            print(str(self.uid) + " 的上下文：" + str(len(user_input)))

            # client = OpenAI(
            #     # This is the default and can be omitted
            #     api_key = "sk-TczjyYwyuUP7KP7t619f6658C85e43A1905b77465b2e9aDf",
            #     base_url = "https://free.v36.cm",
            #     default_headers = {"x-foo": "true"},
            # )

            # optional; defaults to `os.environ['OPENAI_API_KEY']`
            openai.api_key = self.key #旧的可用4不可用3.5"sk-TczjyYwyuUP7KP7t619f6658C85e43A1905b77465b2e9aDf"

            # all client options can be configured just like the `OpenAI` instantiation counterpart
            openai.base_url = "https://free.v36.cm/v1/"
            openai.default_headers = {"x-foo": "true"}  

           # print(f"\n{user_input}\n")

            try:
                chat_completion = openai.chat.completions.create(
                    messages=user_input,
                    model=mode,
                    stream=True,
                )

                splitter = StreamSplitter()
                for message, _ in splitter.split_stream(chat_completion, 'openai'):
                    print(f"[{time.time()}] YIELD: {repr(message)}")
                    yield message, 'message'
                    
                user_input.append({"role": "assistant", "content": splitter.full_content})
                user_lists[str(self.uid)] = user_input
                yield user_lists, 'user_lists'

            except openai.PermissionDeniedError as e:
                error_response = str(e)
                if 'insufficient_user_quota' in error_response:
                    yield f'''无效的 API KEY 是因为 配额已用尽 。
{self.bn}发生错误，不能回复你的消息了，请稍候再试吧 ε(┬┬﹏┬┬)3''', 'message'
                else:
                    raise 
                
        except Exception as e:
            print(traceback.format_exc())
            yield self.user_lists, f"{type(e)}\n{self.bn}发生错误，不能回复你的消息了，请稍候再试吧 ε(┬┬﹏┬┬)3", 'message'

        
