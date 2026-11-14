"""Unit tests for Progressive SkillLoader."""

from pathlib import Path

import pytest
from src.skills_loader import SkillLoader


def test_list_and_load_skills():
    loader = SkillLoader(Path("skills"))
    skills = loader.list_skills()
    names = [s.name for s in skills]

    assert "triage-langfuse-trace" in names
    assert "triage-db-neon" in names
    assert "triage-eval-capture" in names

    content = loader.load_skill("triage-langfuse-trace")
    assert "PickForMe" in content
    assert "fetch_failed_traces" in content


def test_load_missing_skill_raises():
    loader = SkillLoader(Path("skills"))
    with pytest.raises(FileNotFoundError, match="Available skills"):
        loader.load_skill("non-existent-skill")


def test_format_index_for_prompt_is_compact():
    loader = SkillLoader(Path("skills"))
    index = loader.format_index_for_prompt()
    assert "triage-langfuse-trace" in index
    # Progressive disclosure: index should NOT dump the full SOP steps
    assert "Stage 1" not in index
