"""Agent instruction files stay in sync across tools (AGENTS.md §8)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / ".claude" / "skills"
MIRROR = ROOT / ".kilo" / "skill"


def test_kilo_skills_mirror_claude_skills():
    canonical = {p.relative_to(CANONICAL): p.read_bytes() for p in CANONICAL.rglob("*.md")}
    mirror = {p.relative_to(MIRROR): p.read_bytes() for p in MIRROR.rglob("*.md")}
    assert canonical, "no skills found under .claude/skills"
    assert canonical == mirror, "skills drifted - run: powershell -File scripts/sync_agent_skills.ps1"


def test_claude_md_imports_agents_md():
    assert (ROOT / "CLAUDE.md").read_text(encoding="utf-8").strip() == "@AGENTS.md"
