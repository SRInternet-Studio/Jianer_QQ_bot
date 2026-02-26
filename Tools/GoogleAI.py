from typing import Union
import requests
import base64
import json
import traceback
from pydantic import BaseModel
from Tools.AI_tools import *
import time, datetime
import os

try:
    from google import genai as _genai
    from google.genai import types as _genai_types
except ImportError:
    _genai = None
    _genai_types = None

class Schema(BaseModel):
    messages: list[str]

class Parts:
    @staticmethod
    class File:
        def __init__(self, mime_type: str, data: str):
            self.mime_type = mime_type
            self.data = data

        @classmethod
        def upload_from_file(cls, path: str):
            with open(path, "rb") as f:
                file_data = f.read()
            
            if "png" in path.lower():
                mime_type = 'image/png'
            elif "jpg" in path.lower() or "jpeg" in path.lower():
                mime_type = 'image/jpeg'
            else:
                mime_type = 'image/jpeg'  # 默认
            
            encoded_data = base64.b64encode(file_data).decode('utf-8')
            return cls(mime_type, encoded_data)

        @classmethod
        def upload_from_url(cls, url: str):
            print(url)
            response = requests.get(url)
            
            # 创建临时目录
            temp_dir = "./temps"
            if not os.path.exists(temp_dir):
                os.makedirs(temp_dir)
            
            path = f"./temps/google_{len(response.content)}_{len(url)}"
            with open(path, "wb") as f:
                f.write(response.content)

            if "png" in url.lower():
                print("png in file")
                mime_type = 'image/png'
            else:
                print("jpg in file")
                mime_type = 'image/jpeg'
            
            encoded_data = base64.b64encode(response.content).decode('utf-8')
            
            # 删除临时文件
            try:
                if os.path.exists(path):
                    os.remove(path)
                    print(f"已删除临时文件: {path}")
            except Exception as e:
                print(f"删除临时文件失败: {e}")
            
            return cls(mime_type, encoded_data)

        def to_raw(self) -> dict:
            return {
                "inline_data": {
                    "mime_type": self.mime_type,
                    "data": self.data
                }
            }

    @staticmethod
    class Text:
        def __init__(self, text: str):
            self.text = text

        def to_raw(self) -> dict:
            return {"text": self.text}


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
    
    @staticmethod
    class Developer(BaseRole):
        def __init__(self, *args: Union[Parts.File, Parts.Text]):
            super().__init__(*args)
            self.tag = "developer"


class Context:
    def __init__(self, api_key: str, model: str, base_url: str, tools: list = None, 
                 system_instruction: str = "", generation_config: dict = None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.tools = tools or []
        self.system_instruction = system_instruction
        self.generation_config = generation_config or {}
        self.history = []
        self._client = None
        
        # 安全设置映射
        self.safety_settings = [
            {
                "category": "HARM_CATEGORY_HATE_SPEECH",
                "threshold": "BLOCK_ONLY_HIGH"
            },
            {
                "category": "HARM_CATEGORY_HARASSMENT", 
                "threshold": "BLOCK_ONLY_HIGH"
            },
            {
                "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "threshold": "BLOCK_MEDIUM_AND_ABOVE"
            },
            {
                "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                "threshold": "BLOCK_MEDIUM_AND_ABOVE"
            }
        ]

    def _normalize_base_url(self, base_url: str) -> str:
        if not base_url:
            return base_url
        normalized = base_url.rstrip("/")
        if normalized.endswith("/v1beta"):
            normalized = normalized[: -len("/v1beta")]
        return normalized

    def _get_client(self):
        if self._client is not None:
            return self._client
        if _genai is None:
            raise RuntimeError("缺少依赖：google-genai。请安装后再使用 Gemini 功能。")
        normalized_base_url = self._normalize_base_url(self.base_url)
        self._client = _genai.Client(
            api_key=self.api_key,
            http_options={"base_url": normalized_base_url},
        )
        return self._client

    def __gen_content(self, new: Roles.User) -> list:
        content = []
        
        # 如果有系统指令且历史记录为空，先添加developer消息
        if self.system_instruction and len(self.history) == 0:
            developer_msg = Roles.Developer(Parts.Text(self.system_instruction))
            content.append(developer_msg)
        
        content += self.history
        if len(content) > 0 and isinstance(content[-1], Roles.User):
            raise ValueError("还在思考上一条消息呢，稍等一下再问啦 <( ￣^￣)...")

        content.append(new)
        self.history = content
        return [i.res() for i in content]

    def _convert_contents_for_sdk(self, contents: list[dict]) -> list[dict]:
        converted = []
        for c in contents:
            role = c.get("role", "user")
            if role not in ("user", "model"):
                role = "user"
            converted.append(
                {
                    "role": role,
                    "parts": c.get("parts", []),
                }
            )
        return converted

    def _build_sdk_config(self):
        if _genai_types is None:
            return None

        safety_settings = []
        for s in self.safety_settings:
            try:
                safety_settings.append(
                    _genai_types.SafetySetting(
                        category=s["category"],
                        threshold=s["threshold"],
                    )
                )
            except Exception:
                safety_settings.append(s)

        config_kwargs = {
            "system_instruction": self.system_instruction or None,
            "safety_settings": safety_settings or None,
            "tools": self.tools or None,
        }

        if self.generation_config:
            allowed_fields = getattr(_genai_types.GenerateContentConfig, "model_fields", {}) or {}
            for k, v in self.generation_config.items():
                if k in allowed_fields:
                    config_kwargs[k] = v

        try:
            return _genai_types.GenerateContentConfig(**config_kwargs)
        except Exception:
            return config_kwargs

    def gen_content(self, content: Roles.User, model_override: str = None, stream: bool = True):
        try:
            new = self.__gen_content(content)

            model_name = model_override or self.model
            sdk_contents = self._convert_contents_for_sdk(new)
            client = self._get_client()
            config = self._build_sdk_config()

            if stream:
                splitter = StreamSplitter()
                response_stream = client.models.generate_content_stream(
                    model=model_name,
                    contents=sdk_contents,
                    config=config,
                )
                for message, enable_forward in splitter.split_stream(response_stream, type="gemini"):
                    if message.strip():
                        yield message.rstrip(), enable_forward
                self.history.append(Roles.Model(Parts.Text(splitter.full_content)))
            else:
                response = client.models.generate_content(
                    model=model_name,
                    contents=sdk_contents,
                    config=config,
                )
                full_response = getattr(response, "text", None) or ""
                print(f"[{datetime.datetime.now()}] RESPONSE: {repr(full_response)}")
                yield full_response, 1
                self.history.append(Roles.Model(Parts.Text(full_response)))
                
        except Exception as e:
            self.history = self.history[:len(self.history) - 1]
            print(f"GoogleAI error: {e}")
            if any(keyword in str(traceback.format_exc()) for keyword in ["finish_reason: SAFETY", "safety_ratings"]):
                yield "你发送的消息违规啦！快住嘴 (⓿_⓿)", 0
            elif "Resource exhausted" in str(e):
                yield r"你问的太快了，都回复不过来了，等会再试试吧 {{{(>_<)}}}", 0
            else:
                yield str(e), 0
