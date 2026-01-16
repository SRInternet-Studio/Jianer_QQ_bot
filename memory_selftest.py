import asyncio
import os
import time

from Tools.jianer_memory import JianerMemoryService


class TestMemoryService(JianerMemoryService):
    async def _call_memory_ai(self, scope, msg: str, sys_prompt: str) -> str:
        return '{"memories":[{"content":"用户提到自己的名字是小明","weight":0.9},{"content":"用户喜欢聊天","weight":0.2}]}'

    async def _call_memory_ai_text(self, uid: str, msg: str, sys_prompt: str) -> str:
        return '{"memories":[{"content":"项目用户中有人自称小明","weight":0.6}]}'


async def main() -> None:
    db_path = "tmp_jianer_memory_test.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    svc = TestMemoryService(db_path=db_path, memory_mode="test", min_new_rows_to_generate=3)
    svc.store.start()
    svc.update_persona("456", "你叫星语，是一个人工智能助手")

    now = int(time.time())
    for i in range(5):
        svc.store.enqueue_raw_message(
            group_id=123,
            user_id=456,
            message_id=str(i + 1),
            sender=456,
            content="我叫小明",
            timestamp=now + i,
            message_type="group",
        )

    await asyncio.sleep(0.2)

    ok = await svc.generate_now(group_id=123, user_id=456, is_private=False)
    print("generate_now:", ok)

    ctx = await svc.build_memory_context(123, 456, False, "你记得我叫什么吗", topk=3)
    print("memory_context:\n", ctx)

    st = await svc.get_status(123, 456, False)
    print("status:", st)

    svc.stop()
    if os.path.exists(db_path):
        os.remove(db_path)


if __name__ == "__main__":
    asyncio.run(main())

