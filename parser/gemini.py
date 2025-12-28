import logging
import asyncio
import base64
import os
import re
import mimetypes
from typing import Dict, Any, List, Optional, Tuple

try:
    import aiohttp
except ImportError as e:
    aiohttp = None

from .base import BaseParser
# from ..utils.history_manager import HistoryManager # 移除相对导入
from arcspec_ai.utils.history_manager import HistoryManager # 改为绝对导入

logger = logging.getLogger(__name__)


class GeminiParser(BaseParser):
    """Google Gemini 解析器（REST + aiohttp）
    
    支持文本、图片、音频的多模态理解；可自动从消息和参数中识别图片/音频并打包为inlineData。
    """

    PARSER_NAME = "gemini"
    PARSER_DESCRIPTION = "Google Gemini REST解析器，支持文本/图片/音频（aiohttp）"
    PARSER_ALIASES = ["google", "google_gemini", "gemini15", "gemini20"]

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        if aiohttp is None:
            raise ImportError("GeminiParser需要aiohttp，请先安装: pip install aiohttp")

        self.api_key = self.require_config_value("APIKey")
        self.model = self.require_config_value("Model")
        
        # 兼容 BaseUrl 和 BaseURL 大小写
        self.base_url = self.get_config_value("BaseUrl")
        if not self.base_url:
            self.base_url = self.get_config_value("BaseURL")
            
        self.temperature = self.get_config_value("Temperature", 0.5)
        self.max_tokens = self.get_config_value("MaxTokens", 1000)
        self.top_p = self.get_config_value("TopP", 1)
        self.personality = self.get_config_value("Personality", "")
        self.if_return_none = self.get_config_value("if_return_none", "模型没有返回任何内容")

        self.history_manager = HistoryManager(
            max_tokens=self.get_config_value("max_history_tokens", 3000),
            max_messages=self.get_config_value("max_history_messages", 20),
        )
        if self.personality:
            self.history_manager.set_system_message(self.personality)

        logger.info(f"Gemini解析器初始化完成，模型: {self.model}")

    def validate_config(self) -> None:
        # 基础必填验证
        for key in ["APIKey", "Model"]:
            if not self.get_config_value(key):
                raise ValueError(f"配置中缺少必需的键: {key}")

    def chat(self, message: str, history: Optional[List[Dict[str, str]]] = None, **kwargs) -> str:
        """同步聊天入口，内部使用async调用Gemini REST接口"""
        return asyncio.run(self._chat_async(message, history, **kwargs))

    async def _chat_async(self, message: str, history: Optional[List[Dict[str, str]]] = None, **kwargs) -> str:
        # 将消息加入历史（用户侧）
        self.history_manager.add_user_message(message)

        # 自动判别/收集图片与音频
        image_paths, image_urls = self._detect_media_from_message(message, kind="image")
        audio_paths, audio_urls = self._detect_media_from_message(message, kind="audio")

        # 兼容外部显式传入
        image_paths += kwargs.get("image_paths", [])
        image_urls += kwargs.get("image_urls", [])
        audio_paths += kwargs.get("audio_paths", [])
        audio_urls += kwargs.get("audio_urls", [])

        parts: List[Dict[str, Any]] = [{"text": message}]

        # 组装多模态 parts
        async with aiohttp.ClientSession() as session:
            parts += await self._gather_inline_parts(session, image_paths, image_urls, kind="image")
            parts += await self._gather_inline_parts(session, audio_paths, audio_urls, kind="audio")

            # 显式端点风格控制（默认使用Gemini官方格式）
            endpoint_style = str(self.get_config_value("EndpointStyle", "gemini")).lower()
            use_openai_style = endpoint_style == "openai"
            is_google = str(self.base_url).startswith("https://generativelanguage.googleapis.com")

            if use_openai_style:
                # OpenAI风格 chat.completions
                messages: List[Dict[str, Any]] = []
                if self.personality:
                    messages.append({"role": "system", "content": [{"type": "text", "text": self.personality}]})
                
                # 添加历史记录
                if history:
                    for msg in history:
                        role = msg.get("role", "user")
                        content = msg.get("content", "")
                        if role == "system": continue # 系统提示词已处理
                        messages.append({"role": role, "content": [{"type": "text", "text": content}]})

                user_content: List[Dict[str, Any]] = [{"type": "text", "text": message}]
                for p in parts[1:]:
                    if isinstance(p, dict) and p.get("inlineData"):
                        mime = p["inlineData"].get("mimeType", "")
                        b64 = p["inlineData"].get("data", "")
                        if mime.startswith("image/"):
                            data_url = f"data:{mime};base64,{b64}"
                            user_content.append({"type": "image_url", "image_url": {"url": data_url}})
                        elif mime.startswith("audio/"):
                            fmt = mime.split("/")[-1] or "wav"
                            user_content.append({"type": "input_audio", "input_audio": {"data": b64, "format": fmt}})
                
                messages.append({"role": "user", "content": user_content})

                payload = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": float(self.temperature),
                    "top_p": float(self.top_p),
                    "max_tokens": int(self.max_tokens),
                }

                url = f"{self.base_url.rstrip('/')}/v1/chat/completions" if self.base_url else "https://generativelanguage.googleapis.com/v1/chat/completions"
                headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

                logger.debug(f"POST {url} (openai style)")
                async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    data = await resp.json()
                    logger.debug(f"OpenAI风格响应: {str(data)[:500]}")
                    text = self._extract_text_from_openai(data)
                    if text and text != self.if_return_none:
                        self.history_manager.add_assistant_message(text)
                    return text or self.if_return_none

            else:
                # Gemini官方格式 generateContent
                contents = []
                
                # 添加历史记录
                if history:
                    for msg in history:
                        role = "model" if msg.get("role") == "assistant" else "user"
                        if msg.get("role") == "system": continue
                        contents.append({"role": role, "parts": [{"text": msg.get("content", "")}]})

                # 添加当前消息
                contents.append({"role": "user", "parts": parts})

                body = {
                    "contents": contents,
                    "generationConfig": {
                        "temperature": float(self.temperature),
                        "topP": float(self.top_p),
                        "maxOutputTokens": int(self.max_tokens),
                    },
                }
                if self.personality:
                    body["systemInstruction"] = {"role": "system", "parts": [{"text": self.personality}]}

                # Google官方与第三方聚合端点的差异：URL与鉴权
                base_url = self.base_url.rstrip('/') if self.base_url else "https://generativelanguage.googleapis.com"
                
                # if is_google:
                if True: # 强制使用 key 参数鉴权，因为很多第三方反代也是使用这种方式
                    if base_url.endswith("/v1beta"):
                         url = f"{base_url}/models/{self.model}:generateContent?key={self.api_key}"
                    else:
                         url = f"{base_url}/v1beta/models/{self.model}:generateContent?key={self.api_key}"
                    headers = None
                # else:
                #     # 如果 base_url 已经包含 /v1beta，则不再重复添加
                #     if base_url.endswith("/v1beta"):
                #         url = f"{base_url}/models/{self.model}:generateContent"
                #     else:
                #         url = f"{base_url}/v1beta/models/{self.model}:generateContent"
                #     headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

                logger.debug(f"POST {url} (gemini style)")
                async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    data = await resp.json()
                    logger.debug(f"Gemini风格响应: {str(data)[:500]}")
                    text = self._extract_text_from_response(data)
                    if text and text != self.if_return_none:
                        self.history_manager.add_assistant_message(text)
                    return text or self.if_return_none

    def _extract_text_from_response(self, data: Dict[str, Any]) -> str:
        """从Gemini响应中提取文本"""
        try:
            candidates = data.get("candidates") or []
            if not candidates:
                return ""
            content = candidates[0].get("content") or {}
            parts = content.get("parts") or []
            texts = []
            for p in parts:
                if "text" in p and isinstance(p["text"], str):
                    texts.append(p["text"])
            return "\n".join(texts).strip()
        except Exception:
            return ""

    def _extract_text_from_openai(self, data: Dict[str, Any]) -> str:
        """从OpenAI风格chat.completions响应中提取文本"""
        try:
            choices = data.get("choices") or []
            if not choices:
                return ""
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                texts = []
                for c in content:
                    if c.get("type") == "text" and isinstance(c.get("text"), str):
                        texts.append(c["text"])
                return "\n".join(texts).strip()
            return ""
        except Exception:
            return ""

    async def _gather_inline_parts(
        self,
        session: "aiohttp.ClientSession",
        paths: List[str],
        urls: List[str],
        kind: str,
    ) -> List[Dict[str, Any]]:
        """读取本地文件/URL为inlineData parts"""
        parts: List[Dict[str, Any]] = []

        # 本地文件
        for path in paths:
            try:
                if not os.path.exists(path):
                    logger.warning(f"文件不存在: {path}")
                    continue
                mime = self._guess_mime(path, kind)
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
                parts.append({"inlineData": {"mimeType": mime, "data": b64}})
            except Exception as e:
                logger.warning(f"读取文件失败 {path}: {e}")

        # 远程URL
        for url in urls:
            try:
                content_bytes, mime = await self._fetch_url_bytes(session, url, kind)
                if content_bytes:
                    b64 = base64.b64encode(content_bytes).decode("ascii")
                    parts.append({"inlineData": {"mimeType": mime, "data": b64}})
            except Exception as e:
                logger.warning(f"获取URL失败 {url}: {e}")

        return parts

    def _guess_mime(self, path: str, kind: str) -> str:
        mime, _ = mimetypes.guess_type(path)
        if not mime:
            mime = "image/png" if kind == "image" else "audio/wav"
        return mime

    async def _fetch_url_bytes(
        self, session: "aiohttp.ClientSession", url: str, kind: str
    ) -> Tuple[bytes, str]:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            resp.raise_for_status()
            data = await resp.read()
            mime = resp.headers.get("Content-Type") or (
                "image/png" if kind == "image" else "audio/wav"
            )
            return data, mime

    def _detect_media_from_message(self, message: str, kind: str) -> Tuple[List[str], List[str]]:
        """从消息文本中自动识别本地路径或URL的图片/音频"""
        # 简单规则：匹配以常见扩展结尾的路径/URL
        if kind == "image":
            exts = [".png", ".jpg", ".jpeg", ".webp", ".gif"]
        else:
            exts = [".wav", ".mp3", ".m4a", ".ogg", ".flac"]

        # Windows/相对路径匹配（含空格的情况用引号）
        path_pattern = r"(?:[A-Za-z]:\\[^\s\"']+|\./[^\s\"']+|\.[^\s\"']+)"
        url_pattern = r"https?://[^\s\"']+"

        candidate_paths = re.findall(path_pattern, message)
        candidate_urls = re.findall(url_pattern, message)

        def has_ext(s: str) -> bool:
            return any(s.lower().endswith(ext) for ext in exts)

        paths = [p for p in candidate_paths if has_ext(p)]
        urls = [u for u in candidate_urls if has_ext(u)]
        return paths, urls

    def clear_history(self) -> None:
        self.history_manager.clear_history()
        if self.personality:
            self.history_manager.set_system_message(self.personality)
        logger.info("Gemini历史记录已清空")

    def get_history_summary(self) -> Dict[str, Any]:
        return self.history_manager.get_history_summary()

    def get_model_info(self) -> Dict[str, Any]:
        info = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            "personality": self.personality,
            "base_url": self.base_url,
            "multimodal": True,
            "history_summary": self.get_history_summary(),
        }
        return info