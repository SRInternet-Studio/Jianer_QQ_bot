import asyncio

from plugins.JianerAI.memorix.core.utils.person_profile_service import PersonProfileService


def _empty_buckets():
    return {
        "identity_settings": [],
        "relationship_settings": [],
        "stable_facts": [],
        "interaction_preferences": [],
        "recent_interactions": [],
        "uncertain_notes": [],
    }


def test_profile_bucket_recognizes_identity_and_relationship_statements():
    assert PersonProfileService._guess_profile_bucket("系统是 AuroraLinux-Android") == "identity_settings"
    assert PersonProfileService._guess_profile_bucket("主机是 ASUS TUF Gaming F15") == "identity_settings"
    assert PersonProfileService._guess_profile_bucket("现在是学生") == "identity_settings"
    assert PersonProfileService._guess_profile_bucket("桃子和我是同型号的机娘") == "relationship_settings"
    assert PersonProfileService._guess_profile_bucket("桃子是我的朋友") == "relationship_settings"


def test_unconfirmed_memory_facts_are_projected_with_a_visible_marker():
    service = object.__new__(PersonProfileService)
    buckets = service._merge_fact_claim_buckets(
        _empty_buckets(),
        [
            {
                "profile_section": "uncertain_notes",
                "value_text": (
                    "桃子和我是同型号的机娘。系统是 AuroraLinux-Android；"
                    "主机是 ASUS TUF Gaming F15。"
                ),
                "authority": "summary_derived",
                "stability": "uncertain",
            }
        ],
    )

    assert buckets["relationship_settings"] == ["待确认：桃子和我是同型号的机娘。"]
    assert buckets["identity_settings"] == [
        "待确认：系统是 AuroraLinux-Android；",
        "待确认：主机是 ASUS TUF Gaming F15。",
    ]
    assert buckets["uncertain_notes"] == []


def test_trusted_identity_claim_is_not_marked_as_pending():
    service = object.__new__(PersonProfileService)
    buckets = service._merge_fact_claim_buckets(
        _empty_buckets(),
        [
            {
                "profile_section": "stable_facts",
                "value_text": "职业是画师",
                "authority": "direct_user",
                "stability": "stable",
            }
        ],
    )

    assert buckets["identity_settings"] == ["职业是画师"]


def test_untrusted_relation_classification_stays_visible_in_relationship_section():
    service = object.__new__(PersonProfileService)
    buckets = service._confine_untrusted_profile_buckets(
        {
            **_empty_buckets(),
            "relationship_settings": ["桃子与星语是同型号机娘"],
        }
    )

    assert buckets["relationship_settings"] == ["待确认：桃子与星语是同型号机娘"]
    assert buckets["stable_facts"] == []


def test_explicit_person_fact_can_be_backfilled_into_graph_relation():
    class Metadata:
        def compute_relation_hash(self, subject, predicate, obj):
            return f"{subject}|{predicate}|{obj}"

        def get_relation(self, hash_value, include_inactive=False):
            return None

        def get_fact_evidence(self, claim_id):
            return [{"evidence_type": "paragraph", "evidence_id": "paragraph-1"}]

    class RelationWriter:
        def __init__(self):
            self.calls = []

        async def upsert_relation_with_vector(self, **kwargs):
            self.calls.append(kwargs)

    async def scenario():
        service = object.__new__(PersonProfileService)
        service.metadata_store = Metadata()
        service.plugin_config = {}
        writer = RelationWriter()
        service.relation_write_service = writer
        await service._backfill_explicit_fact_relations(
            person_id="qq:42",
            primary_name="user-42",
            fact_claims=[
                {
                    "claim_id": "claim-1",
                    "value_text": "桃子和我是同型号的机娘。",
                    "authority": "summary_derived",
                    "confidence": 0.9,
                }
            ],
        )
        assert writer.calls == [
            {
                "subject": "桃子",
                "predicate": "同型号的机娘",
                "obj": "user-42",
                "confidence": 0.75,
                "source_paragraph": "paragraph-1",
                "metadata": {
                    "source_type": "person_fact_relation",
                    "relation_origin": "automatic_profile_backfill",
                    "person_id": "qq:42",
                    "person_ids": ["qq:42"],
                    "fact_claim_id": "claim-1",
                    "authority": "summary_derived",
                },
                "write_vector": False,
            }
        ]

    asyncio.run(scenario())
