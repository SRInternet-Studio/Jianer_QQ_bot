from typing import Union
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold, FunctionDeclaration
import httpx, traceback
from pydantic import BaseModel
from Tools.AI_tools import *
import time, datetime

class Schema(BaseModel):
  messages: list[str]

class Parts:
    @staticmethod
    class File:
        def __init__(self, file: genai.types.file_types.File):
            self.file = file

        @classmethod
        def upload_from_file(cls, path: str):
            if "png" in path:
                file = genai.upload_file(path, mime_type='image/png')
            else:
                file = genai.upload_file(path, mime_type='image/jpeg')
            return cls(file)

        @classmethod
        def upload_from_url(cls, url: str):
            print(url)
            response = httpx.get(url)
            path = f"./temps/google_{len(response.content)}_{len(url)}"
            with open(path, "wb") as f:
                f.write(response.content)

            if "png" in url:
                print("png in file")
                file = genai.upload_file(path, mime_type='image/png')
            else:
                print("jpg in file")
                file = genai.upload_file(path, mime_type='image/jpeg')
            return cls(file)

        def to_raw(self) -> genai.types.file_types.File:
            return self.file

    @staticmethod
    class Text:
        def __init__(self, text: str):
            self.text = text

        def to_raw(self) -> str:
            return self.text


class BaseRole:
    def __init__(self, *args: Union[Parts.File, Parts.Text]):
        self.content = list(args)
        self.tag = "none"

    def res(self) -> dict:
        return {
            "role": self.tag,
            "parts": [
                i.to_raw() for i in self.content
            ]
        }


class Roles:
    @staticmethod
    class User(BaseRole):
        def __init__(self, *args: Union[Parts.File, Parts.Text]):
            super().__init__(*args)
            self.tag = "user"

    @staticmethod
    class Model(BaseRole):
        def __init__(self, *args: Union[Parts.File, Parts.Text]):
            super().__init__(*args)
            self.tag = "model"


class Context:
    def __init__(self, api_key: str, model: genai.GenerativeModel, tools: list = None):
        genai.configure(api_key=api_key)
        self.model = model
        
        self.safety = {
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_ONLY_HIGH,
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE
        }

        # self.safety = {
        #     HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        #     HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        #     HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        #     HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE
        # }

        self.tools = tools
        self.history = []

    def __gen_content(self, new: Roles.User) -> list:
        content = []
        content += self.history
        if len(content) > 0 and isinstance(content[-1], Roles.User):
            raise ValueError("消息角色重复")

        content.append(new)
        self.history = content
        return [i.res() for i in content]

    def gen_content(self, content: Roles.User):
        try:
            new = self.__gen_content(content)
            # g_c = self.model.start_chat(history=new, enable_automatic_function_calling=True)
            
            
            #  config={'response_mime_type': 'application/json', 'response_schema': list[Schema],}
            # print(content.res())
            # res = g_c.send_message(content.res(), safety_settings=self.safety)
            # del g_c
            
            res = self.model.generate_content(contents=new, safety_settings=self.safety, stream=True)
            splitter = StreamSplitter()
            
            for message, enable_forward_msg_num in splitter.split_stream(res):
                print(f"[{datetime.datetime.now()}] RESPONSE: {repr(message)}")
                yield message, enable_forward_msg_num 
            
            # 添加到历史记录
            self.history.append(Roles.Model(Parts.Text(splitter.full_content)))
            # print(splitter.full_content)
            
        except Exception as e:
            self.history = self.history[:len(self.history) - 1]
            print(f"GoogleAI error: {e}")
            if any(keyword in str(traceback.format_exc()) for keyword in ["finish_reason: SAFETY", "safety_ratings"]):
                yield "你发送的消息违规啦！快住嘴 (⓿_⓿)", 0
            else:
                yield e, 0
