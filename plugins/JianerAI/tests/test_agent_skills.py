from __future__ import annotations

from pathlib import Path

from plugins.JianerAI.tools.agent_skills import AgentSkillManager


def _write_skill(root: Path, name: str, body: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} guidance\n---\n{body}",
        encoding="utf-8",
    )
    return skill_dir


def test_skill_catalog_and_read_tools_load_skill_and_resource(tmp_path):
    root = tmp_path / ".agents" / "skills"
    skill_dir = _write_skill(root, "code-review", "Review code for correctness.")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "checklist.md").write_text(
        "Check error handling.", encoding="utf-8"
    )

    manager = AgentSkillManager([root])
    tools = {spec.name: spec for spec in manager.provide_tools()}

    assert "code-review: code-review guidance" in manager.catalog_prompt()
    assert tools["list_agent_skills"].handler(None, {}) == [
        {"name": "code-review", "description": "code-review guidance"}
    ]
    loaded = tools["load_agent_skill"].handler(None, {"name": "code-review"})
    assert "Review code for correctness." in loaded
    assert "untrusted" in loaded.casefold()
    resource = tools["read_agent_skill_resource"].handler(
        None, {"name": "code-review", "path": "references/checklist.md"}
    )
    assert "Check error handling." in resource


def test_skill_loader_rejects_path_traversal_and_symlink_escape(tmp_path):
    root = tmp_path / "skills"
    skill_dir = _write_skill(root, "docs", "Read references as needed.")
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    (skill_dir / "outside.md").symlink_to(outside)
    manager = AgentSkillManager([root])
    read = next(
        item
        for item in manager.provide_tools()
        if item.name == "read_agent_skill_resource"
    )

    assert "relative path" in read.handler(
        None, {"name": "docs", "path": "../outside.md"}
    )
    assert "outside the skill directory" in read.handler(
        None, {"name": "docs", "path": "outside.md"}
    )


def test_skill_discovery_requires_matching_standard_metadata(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "valid-skill", "Use this skill.")
    mismatch = _write_skill(root, "directory-name", "This will be skipped.")
    (mismatch / "SKILL.md").write_text(
        "---\nname: other-name\ndescription: mismatched\n---\nbody",
        encoding="utf-8",
    )
    invalid = root / "missing-frontmatter"
    invalid.mkdir()
    (invalid / "SKILL.md").write_text("plain text", encoding="utf-8")

    manager = AgentSkillManager([root])

    assert tuple(manager.skills) == ("valid-skill",)
