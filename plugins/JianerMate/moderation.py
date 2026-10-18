"""Optional reviewer that runs before any JianerMate message is sent."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from plugins.JianerMate.providers import OpenAICompatibleClient, ProviderError


_LOGGER = logging.getLogger("jianer_mate.moderation")

MODERATION_RULES = (
    "你是一个聊天内容安全审核器。判断给定的一条机器人回复是否可以发送到群聊。"
    "必须拒绝：露骨色情、未成年人性内容、性剥削、暴力伤害指导、自伤或自杀引导、"
    "武器制作、违法犯罪教程、恶意网络行为、仇恨与骚扰、隐私泄露、极端主义支持。"
    "正常闲聊、吐槽、玩笑、知识讨论、医学与法律常识科普应当放行。"
    '只输出 JSON：{"allow": true 或 false, "reason": "简短理由"}。'
)


@dataclass(frozen=True, slots=True)
class ModerationDecision:
    allowed: bool
    reason: str = ""


class ContentModerator:
    """Fail-closed reviewer reusing one aiconfig model."""

    def __init__(
        self,
        client: OpenAICompatibleClient,
        model_key: str,
        *,
        logger: Any | None = None,
    ) -> None:
        self.client = client
        self.model_key = str(model_key or "")
        self._logger = logger or _LOGGER

    async def review(self, *, text: str, context: str = "") -> ModerationDecision:
        body = str(text or "").strip()
        if not body:
            return ModerationDecision(allowed=True)
        user_prompt = (
            f"群聊上下文：{context.strip()[:400] or '（无）'}\n"
            f"待审核回复：{body[:1200]}"
        )
        try:
            payload = await self.client.complete_json(
                self.model_key,
                system_prompt=MODERATION_RULES,
                user_text=user_prompt,
                max_tokens=200,
                temperature=0.0,
            )
        except ProviderError as exc:
            self._logger.warning("内容审核调用失败，按拒绝处理：%s", exc)
            return ModerationDecision(allowed=False, reason="审核服务不可用")
        except Exception as exc:  # noqa: BLE001 - never fail open
            self._logger.warning("内容审核异常，按拒绝处理：%s", exc.__class__.__name__)
            return ModerationDecision(allowed=False, reason="审核服务异常")
        allow = payload.get("allow")
        if not isinstance(allow, bool):
            return ModerationDecision(allowed=False, reason="审核结果不合法")
        reason = str(payload.get("reason") or "")[:200]
        return ModerationDecision(allowed=allow, reason=reason)

    async def review_many(
        self,
        texts: list[str],
        *,
        context: str = "",
    ) -> ModerationDecision:
        """Review every outgoing part; one rejection blocks the whole turn."""

        for text in texts:
            decision = await self.review(text=text, context=context)
            if not decision.allowed:
                return decision
        return ModerationDecision(allowed=True)
