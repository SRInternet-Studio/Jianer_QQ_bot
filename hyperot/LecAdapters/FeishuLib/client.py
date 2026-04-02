import base64
import json
import os
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
