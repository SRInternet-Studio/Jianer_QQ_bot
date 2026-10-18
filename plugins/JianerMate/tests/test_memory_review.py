import asyncio

from plugins.JianerMate.memory import LongTermMemory
from plugins.JianerMate.memory_review import MemoryAction, MemoryReviewer
from plugins.JianerMate.store import MateStore


class _StubClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def complete_json(self, model_key, **kwargs):
        self.calls.append({"model_key": model_key, **kwargs})
        return self.payload


def test_memory_action_parsing_rules():
    created = MemoryAction.from_payload(
        {
            "action": "create",
            "scope": "person",
            "canonical_fact": "用户喜欢猫",
            "memory_text": "我记得他喜欢猫。",
            "importance": 2,
            "confidence": -1,
        }
    )
    assert created is not None
    assert created.importance == 1.0 and created.confidence == 0.0
    assert MemoryAction.from_payload(
        {"action": "update", "scope": "person", "memory_text": "x"}
    ) is None
    assert MemoryAction.from_payload(
        {"action": "create", "scope": "everyone", "memory_text": "x"}
    ) is None
    assert MemoryAction.from_payload(
        {"action": "create", "scope": "group", "memory_text": "一起打本"}
    ).scope == "group"


def test_reviewer_parses_actions_and_prompt():
    client = _StubClient(
        {
            "memories": [
                {
                    "action": "create",
                    "scope": "person",
                    "canonical_fact": "用户喜欢猫",
                    "memory_text": "我记得他喜欢猫。",
                },
                {"action": "update", "scope": "person", "memory_text": "缺少 id"},
            ]
        }
    )
    reviewer = MemoryReviewer(client)
    actions = asyncio.run(
        reviewer.review(
            model_key="planner",
            transcript="小明: 我超喜欢猫",
            persona_text="你是简儿",
            known_memories=[
                {"id": "7", "scope": "person", "canonical_fact": "用户养了一只猫"}
            ],
        )
    )
    assert len(actions) == 1
    assert actions[0].canonical_fact == "用户喜欢猫"
    call = client.calls[0]
    assert call["model_key"] == "planner"
    assert "我超喜欢猫" in call["user_text"]
    assert "id=7" in call["user_text"]


def test_memory_jobs_are_idempotent_and_persisted(tmp_path):
    store = MateStore(tmp_path / "mate.db")
    conversation = store.ensure_conversation(
        "milky", "10001", "group", "222", now=1
    )
    assert store.enqueue_memory_job(
        conversation.ref,
        exchange_key="222:1000",
        canonical_user_id="qq:5",
        now=10,
    )
    assert not store.enqueue_memory_job(
        conversation.ref,
        exchange_key="222:1000",
        canonical_user_id="qq:5",
        now=11,
    )
    assert store.count_memory_jobs() == 1
    jobs = store.due_memory_jobs(now=12, limit=5)
    assert jobs and jobs[0]["canonical_user_id"] == "qq:5"
    store.finish_memory_job(
        jobs[0]["id"], status="pending", attempts=1, next_attempt_at=100, now=12
    )
    assert store.due_memory_jobs(now=50, limit=5) == ()
    assert len(store.due_memory_jobs(now=100, limit=5)) == 1
    store.close()


def test_long_term_memory_writes_through_legacy_backend(tmp_path):
    memory = LongTermMemory(tmp_path / "memory.db", backend="legacy")
    assert memory.available, memory.error
    memory.ensure_identity(protocol="qq", self_id="", external_id="5")
    memory.create_scoped_memory(
        scope="person",
        canonical_user_id="qq:5",
        preset="简儿",
        content="我记得他喜欢猫。",
        canonical_fact="用户喜欢猫",
    )
    records = memory.list_scoped_memories(
        scope="person", canonical_user_id="qq:5", preset="简儿"
    )
    assert any("喜欢猫" in str(getattr(item, "content", "")) for item in records)
    memory.close()
