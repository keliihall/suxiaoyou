"""System prompt assembly — integration tests against real Agent prompts.

These tests exercise ``assemble`` against real agent definitions from
``AgentRegistry`` and the real skill registry, with file-system project
instructions where applicable. Pure-function tests with pinned inputs live
in ``test_prompt_assembler.py``.

Per ADR-0009 (PromptAssembler extraction).
"""

from datetime import datetime
from pathlib import Path

from app.dependencies import set_skill_registry
from app.skill.registry import SkillRegistry
from app.agent.agent import AgentRegistry
from app.session.system_prompt import (
    active_skills_from_registry,
    assemble,
    load_project_instructions,
    render_skills_section,
)


_PINNED = {
    "now": datetime(2026, 5, 4, 15, 30, 0),
    "tz_name": "PDT",
    "platform_name": "Darwin",
    "cwd": "/test/cwd",
}


def _resolve_io(directory: str | None = None) -> dict:
    """Mirror SessionPrompt._build_system_prompt_parts I/O resolution."""
    return {
        "project_instructions": load_project_instructions(directory),
        "skills_summary": render_skills_section(active_skills_from_registry()),
    }


class TestSystemPrompt:
    def test_build_agent_has_prompt(self):
        ar = AgentRegistry()
        build = ar.get("build")
        parts = assemble(build, **_resolve_io(), **_PINNED)
        prompt = parts.as_plain_text()
        assert "software engineering" in prompt.lower() or "tool" in prompt.lower()
        assert "Visible process language" in prompt
        assert "reasoning trace" in prompt
        assert "苏小有 (suyo)" in prompt
        assert "Yakyak" not in prompt
        assert "Never call it a former name" in prompt
        assert "latest genuine user-authored message" in prompt
        assert "request/UI locale" in prompt
        assert "Do not use the surrounding UI language to choose the final" in prompt
        assert "final response in Simplified Chinese" not in prompt

    def test_includes_environment(self):
        ar = AgentRegistry()
        build = ar.get("build")
        parts = assemble(build, **_resolve_io(), **_PINNED)
        prompt = parts.as_plain_text()
        assert "Working directory" in prompt
        assert "Platform" in prompt
        assert "date" in prompt

    def test_plan_agent_prompt(self):
        ar = AgentRegistry()
        plan = ar.get("plan")
        parts = assemble(plan, **_resolve_io(), **_PINNED)
        prompt = parts.as_plain_text()
        assert "PLAN MODE" in prompt or "read-only" in prompt.lower()

    def test_builtin_primary_and_subagents_share_fixed_product_identity(self):
        ar = AgentRegistry()

        for name in ("build", "plan", "explore", "general"):
            agent = ar.get(name)
            parts = assemble(agent, **_resolve_io(), **_PINNED)
            prompt = parts.as_plain_text()
            assert "You are 苏小有 (suyo)" in prompt, name
            assert prompt.index("<product_identity>") < prompt.index(
                "<response_language>"
            ), name
            assert prompt.rstrip().endswith("</response_language>"), name
            assert "Yakyak" not in prompt, name

    def test_compaction_prompt_preserves_current_product_identity(self):
        ar = AgentRegistry()
        compaction = ar.get("compaction")

        assert "苏小有 (suyo) is the product's fixed assistant identity" in (
            compaction.system_prompt or ""
        )
        assert "Yakyak" not in (compaction.system_prompt or "")
        assert "incorrect assistant response from an older app version" in (
            compaction.system_prompt or ""
        )
        assert "Never call it a former name" in (compaction.system_prompt or "")
        assert "latest genuine user-authored message" in (
            compaction.system_prompt or ""
        )
        assert "## Continuation Handoff" in (compaction.system_prompt or "")

    def test_with_project_instructions(self, tmp_path: Path):
        instructions = tmp_path / "AGENTS.md"
        instructions.write_text("# Custom Instructions\nDo X and Y.")

        ar = AgentRegistry()
        build = ar.get("build")
        pinned = {**_PINNED, "cwd": str(tmp_path)}
        parts = assemble(build, **_resolve_io(str(tmp_path)), **pinned)
        prompt = parts.as_plain_text()
        assert "Custom Instructions" in prompt
        assert "Do X and Y" in prompt

    def test_without_project_instructions(self, tmp_path: Path):
        ar = AgentRegistry()
        build = ar.get("build")
        pinned = {**_PINNED, "cwd": str(tmp_path)}
        parts = assemble(build, **_resolve_io(str(tmp_path)), **pinned)
        prompt = parts.as_plain_text()
        assert "Project Instructions" not in prompt

    def test_cached_parts_separate_static_from_dynamic(self):
        ar = AgentRegistry()
        build = ar.get("build")
        parts = assemble(build, **_resolve_io(), **_PINNED)
        # Agent base prompt is in cached section
        assert "苏小有 (suyo)" in parts.cached
        assert "Yakyak" not in parts.cached
        # Environment info is in dynamic section
        assert "Working directory" in parts.dynamic

    def test_as_cached_blocks_format(self):
        ar = AgentRegistry()
        build = ar.get("build")
        parts = assemble(build, **_resolve_io(), **_PINNED)
        blocks = parts.as_cached_blocks()
        assert len(blocks) == 2
        # First block (cached) has cache_control
        assert blocks[0]["type"] == "text"
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        # Second block (dynamic) has no cache_control
        assert blocks[1]["type"] == "text"
        assert "cache_control" not in blocks[1]

    def test_includes_skill_routing_when_skills_available(self, tmp_path: Path):
        skills_dir = tmp_path / ".suxiaoyou" / "skills" / "sheet-helper"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            "---\nname: sheet-helper\ndescription: Helps with spreadsheet workflows.\n---\nUse for sheets.",
            encoding="utf-8",
        )

        registry = SkillRegistry(project_dir=str(tmp_path))
        registry.scan(project_dir=str(tmp_path))
        set_skill_registry(registry)

        ar = AgentRegistry()
        build = ar.get("build")
        parts = assemble(build, **_resolve_io(), **_PINNED)

        assert "Skill Routing" in parts.dynamic
        assert "sheet-helper" in parts.dynamic
