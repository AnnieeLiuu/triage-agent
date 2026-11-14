"""Progressive Skill Loader for reading troubleshooting SOPs on demand."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillSummary:
    """Lightweight metadata exposed in the system prompt before full loading."""

    name: str
    description: str
    path: Path


class SkillLoader:
    """Discovers and loads Markdown SOP skills from a directory."""

    def __init__(self, skills_dir: Path | str = Path("skills")) -> None:
        self.skills_dir = Path(skills_dir)

    def list_skills(self) -> list[SkillSummary]:
        """Return name and description for every valid skill file."""
        if not self.skills_dir.exists():
            return []

        summaries: list[SkillSummary] = []
        for path in sorted(self.skills_dir.glob("*.md")):
            name, description = self._parse_frontmatter(path)
            summaries.append(SkillSummary(name=name, description=description, path=path))
        return summaries

    def load_skill(self, name: str) -> str:
        """Load the full Markdown SOP for the requested skill name."""
        normalized = name.removesuffix(".md").strip()
        for summary in self.list_skills():
            if summary.name == normalized or summary.path.stem == normalized:
                return summary.path.read_text(encoding="utf-8")

        available = ", ".join(s.name for s in self.list_skills()) or "(none)"
        raise FileNotFoundError(f"Skill '{name}' not found. Available skills: {available}")

    def format_index_for_prompt(self) -> str:
        """Format a compact skill catalog for progressive disclosure."""
        skills = self.list_skills()
        if not skills:
            return "目前沒有可用的 troubleshooting skills。"
        lines = [f"- `{s.name}`: {s.description}" for s in skills]
        return "\n".join(lines)

    @staticmethod
    def _parse_frontmatter(path: Path) -> tuple[str, str]:
        text = path.read_text(encoding="utf-8")
        name = path.stem
        description = ""

        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                for raw_line in parts[1].splitlines():
                    line = raw_line.strip()
                    if line.startswith("name:"):
                        name = line.split(":", 1)[1].strip().strip("\"'")
                    elif line.startswith("description:"):
                        description = line.split(":", 1)[1].strip().strip("\"'")
        return name, description or f"SOP in {path.name}"
