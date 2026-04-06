import base64
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import time

import httpx


class FeishuClient:
    def __init__(self, config):
        feishu_cfg = {}
        if isinstance(config.others, dict):
            feishu_cfg = config.others.get("feishu", {}) or {}
        conn_cfg = config.get_connection("Feishu") if hasattr(config, "get_connection") else None

        def pick(key: str, default=None):
            conn_val = getattr(conn_cfg, key, None) if conn_cfg is not None else None
            if conn_val not in (None, ""):
                return conn_val
            return feishu_cfg.get(key, default)

        self.event_mode = str(pick("event_mode", "webhook") or "webhook").lower()
        self.base_url = str(pick("base_url", "https://open.feishu.cn") or "https://open.feishu.cn").rstrip("/")
        self.app_id = str(pick("app_id", "") or "")
        self.app_secret = str(pick("app_secret", "") or "")
        self.verification_token = str(pick("verification_token", "") or "")
        self.encrypt_key = str(pick("encrypt_key", "") or "")
        self.callback_path = str(pick("callback_path", "/feishu/callback") or "/feishu/callback")
        self.bot_open_id = str(pick("bot_open_id", self.app_id) or self.app_id or "")
        self.token_refresh_skew_seconds = int(pick("token_refresh_skew_seconds", 300) or 300)
        self.timeout = httpx.Timeout(30.0, connect=10.0)
        self._tenant_access_token = ""
        self._tenant_token_expire_at = 0

    def _request(self, method: str, path: str, params=None, json_body=None, data=None, files=None, auth: bool = True):
        url = f"{self.base_url}{path}"
        headers = {}
        if auth:
            headers["Authorization"] = f"Bearer {self.get_tenant_access_token()}"
        response = httpx.request(
            method=method.upper(),
            url=url,
            params=params,
            json=json_body,
            data=data,
            files=files,
            headers=headers,
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            body = {}
            try:
                body = response.json()
            except Exception:
                body = {"raw": response.text}
            err_msg = body.get("msg") or body.get("message") or body.get("error", {}).get("message") or str(body)
            err_code = body.get("code")
            raise RuntimeError(f"Feishu API HTTP {response.status_code} code={err_code} msg={err_msg}")
        if not response.text:
            return {}
        payload = response.json()
        code = payload.get("code", 0)
        if code not in (0, "0", None):
            raise RuntimeError(payload.get("msg") or payload.get("message") or str(payload))
        return payload

    def get_tenant_access_token(self) -> str:
        if self._tenant_access_token and time.time() < self._tenant_token_expire_at - self.token_refresh_skew_seconds:
            return self._tenant_access_token
        payload = self._request(
            "POST",
            "/open-apis/auth/v3/tenant_access_token/internal",
            json_body={
                "app_id": self.app_id,
                "app_secret": self.app_secret,
            },
            auth=False,
        )
        expire = int(payload.get("expire", 7200) or 7200)
        self._tenant_access_token = payload.get("tenant_access_token") or ""
        self._tenant_token_expire_at = int(time.time()) + expire
        return self._tenant_access_token

    def send_message(self, receive_id_type: str, receive_id: str, msg_type: str, content: dict | str):
        if isinstance(content, dict):
            content = json.dumps(content, ensure_ascii=False)
        payload = self._request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            json_body={
                "receive_id": str(receive_id),
                "msg_type": msg_type,
                "content": content,
            },
        )
        return payload.get("data", {})

    def reply_message(self, message_id: str, msg_type: str, content: dict | str):
        if isinstance(content, dict):
            content = json.dumps(content, ensure_ascii=False)
        payload = self._request(
            "POST",
            f"/open-apis/im/v1/messages/{message_id}/reply",
            json_body={
                "msg_type": msg_type,
                "content": content,
            },
        )
        return payload.get("data", {})

    def delete_message(self, message_id: str):
        return self._request("DELETE", f"/open-apis/im/v1/messages/{message_id}")

    def get_message(self, message_id: str):
        payload = self._request("GET", f"/open-apis/im/v1/messages/{message_id}")
        data = payload.get("data", {})
        items = data.get("items")
        if isinstance(items, list) and items:
            return items[0]
        return data

    def get_user(self, open_id: str):
        payload = self._request(
            "GET",
            f"/open-apis/contact/v3/users/{open_id}",
            params={"user_id_type": "open_id"},
        )
        data = payload.get("data", {})
        return data.get("user", data)

    def get_chat(self, chat_id: str):
        payload = self._request("GET", f"/open-apis/im/v1/chats/{chat_id}")
        data = payload.get("data", {})
        return data.get("chat", data)

    def upload_image(self, source: str) -> str:
        raw = self._read_source_bytes(source)
        files = {
            "image": ("image.jpg", raw, "image/jpeg"),
        }
        payload = self._request("POST", "/open-apis/im/v1/images", data={"image_type": "message"}, files=files)
        return payload.get("data", {}).get("image_key", "")

    def upload_file(self, source: str, file_type: str, file_name: str = "", duration: int | None = None) -> str:
        raw = self._read_source_bytes(source)
        source_path = str(source or "")
        if source_path.startswith("file://"):
            source_path = source_path[7:]
        if not file_name:
            file_name = os.path.basename(source_path) or f"upload_{int(time.time() * 1000)}.{file_type}"
        content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        data = {"file_type": str(file_type), "file_name": str(file_name)}
        if duration not in (None, 0, "0", ""):
            data["duration"] = str(int(duration))
        files = {
            "file": (str(file_name), raw, content_type),
        }
        payload = self._request("POST", "/open-apis/im/v1/files", data=data, files=files)
        return payload.get("data", {}).get("file_key", "")

    def upload_audio(self, source: str, duration: int | None = None) -> str:
        source_path = str(source or "")
        if source_path.startswith("file://"):
            source_path = source_path[7:]
        ext = os.path.splitext(source_path)[1].lower()
        upload_source = source
        converted_path = ""
        if ext != ".opus":
            ffmpeg_bin = shutil.which("ffmpeg")
            if not ffmpeg_bin:
                raise RuntimeError(f"Feishu audio requires .opus file, got '{ext or 'unknown'}' and ffmpeg is not available")
            if not os.path.isfile(source_path):
                raise RuntimeError("Feishu audio conversion supports local files only")
            fd, converted_path = tempfile.mkstemp(suffix=".opus")
            os.close(fd)
            proc = subprocess.run(
                [ffmpeg_bin, "-y", "-i", source_path, "-acodec", "libopus", "-ac", "1", "-ar", "16000", converted_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if proc.returncode != 0:
                if os.path.exists(converted_path):
                    os.remove(converted_path)
                err = (proc.stderr or proc.stdout or "").strip()
                if len(err) > 400:
                    err = err[-400:]
                raise RuntimeError(f"ffmpeg convert to opus failed: {err}")
            upload_source = converted_path
            source_path = converted_path
        file_name = os.path.basename(source_path) or f"audio_{int(time.time() * 1000)}.opus"
        try:
            return self.upload_file(source=upload_source, file_type="opus", file_name=file_name, duration=duration)
        finally:
            if converted_path and os.path.exists(converted_path):
                os.remove(converted_path)

    def _read_source_bytes(self, source: str) -> bytes:
        source = str(source or "")
        if source.startswith("base64:"):
            return base64.b64decode(source.split("base64:", 1)[1])
        if source.startswith("file://"):
            source = source[7:]
        if source.startswith("http://") or source.startswith("https://"):
            response = httpx.get(source, timeout=self.timeout)
            response.raise_for_status()
            return response.content
        if os.path.isfile(source):
            with open(source, "rb") as fp:
                return fp.read()
        raise FileNotFoundError(source)
