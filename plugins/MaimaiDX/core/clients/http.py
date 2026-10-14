from abc import ABC, abstractmethod
from io import BytesIO

import httpx


class ApiClient(ABC):
    def __init__(
        self,
        *,
        base_url: str,
        headers: dict | None = None,
        timeout: int = 180,
    ):
        self.base_url = base_url
        self.headers = headers or {}
        self.timeout = timeout

    async def _request(self, method: str, endpoint: str, **kwargs) -> dict | list:
        async with httpx.AsyncClient(proxy=None, timeout=self.timeout) as client:
            request_headers = dict(self.headers)
            auth_snapshot = self._request_auth_snapshot(request_headers)
            resp = await client.request(
                method,
                self.base_url + endpoint,
                headers=request_headers,
                **kwargs,
            )

            if resp.status_code == 401:
                handled = await self._on_unauthorized(auth_snapshot)
                if handled:
                    resp = await client.request(
                        method,
                        self.base_url + endpoint,
                        headers=dict(self.headers),
                        **kwargs,
                    )

            self._handle_error(resp)
            return resp.json()

    def _request_auth_snapshot(self, headers: dict) -> object | None:
        return headers.get("Authorization")

    async def _on_unauthorized(self, auth_snapshot: object | None = None) -> bool:
        return False

    @abstractmethod
    def _request_data(self, method: str, endpoint: str, **kwargs):
        raise NotImplementedError

    @abstractmethod
    def _handle_error(self, resp: httpx.Response) -> None:
        raise NotImplementedError


async def qqlogo(qqid: int | None = None, icon: str | None = None) -> bytes | None:
    """获取QQ头像"""
    if qqid is None and icon is None:
        return None
    async with httpx.AsyncClient(timeout=30) as session:
        if qqid is not None:
            params = {"b": "qq", "nk": qqid, "s": 100}
            res = await session.request("GET", "https://q1.qlogo.cn/g", params=params)
        else:
            res = await session.request("GET", icon)
        return res.content


async def online_assets(endpoint: str) -> BytesIO | None:
    """获取资源文件"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://www.yuzuchan.moe/assets/maimaidx{endpoint}"
            )
            resp.raise_for_status()
            return BytesIO(resp.content)
    except Exception:
        return None
