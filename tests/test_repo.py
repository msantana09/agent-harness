"""Repo-wide checks: every name a file points at resolves inside the tree, Python imports only the
standard library or the repo, and the skill and plugin manifests are what their loaders accept."""

import ast
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTENT = ("docs", "hooks", "rules", "scripts", "skills", "tests")
SKILLS = ROOT / "skills"

SKILL_CALL = re.compile(r'Skill\("(?:agent-harness:)?([\w-]+)"\)')
# A slash command stands alone: after a line start, space, backtick or parenthesis, and before
# the end of a word that is not part of a longer path. Installed as a plugin, a skill's command
# takes the plugin's name as a prefix.
SLASH_COMMAND = re.compile(
    r"(?:^|(?<=[\s`(]))/(?:agent-harness:)?([a-z][a-z0-9-]*)(?=$|[\s`).,;:])", re.M
)
CLAUDE_CODE_COMMANDS = {"plugin"}
SECTION_CITATION = re.compile(r"([\w./<>-]+\.md)`? § ([^\n`*]+)")
SKILL_PATH = re.compile(
    r"(?:\$\{CLAUDE_SKILL_DIR\}/|(?<![\w./-]))((?:scripts|references)/[\w./-]*\w\.\w+)"
)
REPO_PATH = re.compile(r"(?<![\w./-])((?:docs|hooks|rules|scripts|skills)/[\w./-]*\w\.\w+)")
# The README links the repo's rules, scripts and skills into ~/.claude, so a path there names a
# file in the repo.
INSTALLED_PATH = re.compile(r"~/\.claude/((?:rules|scripts|skills)/[\w./-]*\w)")
PYTHON_COMMAND = re.compile(r"\b(python[\d.]*)\s+(?:-m\s+\w+|\S+\.py)\b")
SHEBANG = "#!/usr/bin/env python3"
DESCRIPTION_LIMIT = 1024


def files(suffix: str) -> Iterator[Path]:
    """Files with `suffix` in the README and the content folders, outside caches."""
    readme = ROOT / "README.md"
    if suffix == ".md" and readme.exists():
        yield readme
    for folder in CONTENT:
        for path in sorted((ROOT / folder).rglob(f"*{suffix}")):
            if "__pycache__" not in path.parts:
                yield path


def skill_folder(path: Path) -> Path | None:
    """The skill folder `path` sits in, or None outside skills/."""
    if SKILLS not in path.parents:
        return None
    return SKILLS / path.relative_to(SKILLS).parts[0]


def where(path: Path, text: str, offset: int) -> str:
    return f"{path.relative_to(ROOT)}:{text.count(chr(10), 0, offset) + 1}"


def headings(path: Path) -> list[str]:
    return [line.lstrip("#").strip() for line in path.read_text().splitlines() if line[:1] == "#"]


def test_every_skill_and_slash_command_named_is_a_skill_here() -> None:
    missing = []
    for path in files(".md"):
        text = path.read_text()
        for found in [*SKILL_CALL.finditer(text), *SLASH_COMMAND.finditer(text)]:
            if found.re is SLASH_COMMAND and found.group(1) in CLAUDE_CODE_COMMANDS:
                continue
            if not (SKILLS / found.group(1) / "SKILL.md").is_file():
                missing.append(f"{where(path, text, found.start())}: {found.group(0).strip()}")
    assert missing == []


def test_every_section_citation_names_a_heading_that_exists() -> None:
    dangling = []
    for path in files(".md"):
        text = path.read_text()
        for found in SECTION_CITATION.finditer(text):
            cited, rest = found.group(1), found.group(2).strip()
            target = next((p for p in (ROOT / cited, path.parent / cited) if p.is_file()), None)
            # The citation runs on into prose, so a heading matches when the text starts with it.
            ok = target is not None and any(
                rest.startswith(h) and not rest[len(h) : len(h) + 1].isalnum()
                for h in headings(target)
            )
            if not ok:
                dangling.append(f"{where(path, text, found.start())}: {found.group(0)}")
    assert dangling == []


def test_every_script_and_reference_path_named_resolves() -> None:
    missing = []
    for path in files(".md"):
        text = path.read_text()
        skill = skill_folder(path)
        pattern, base = (SKILL_PATH, skill) if skill else (REPO_PATH, ROOT)
        for found in pattern.finditer(text):
            if not (base / found.group(1)).is_file():
                missing.append(f"{where(path, text, found.start())}: {found.group(1)}")
        for found in INSTALLED_PATH.finditer(text):
            if not (ROOT / found.group(1)).is_file():
                missing.append(f"{where(path, text, found.start())}: {found.group(0)}")
    assert missing == []


def test_python_imports_only_the_standard_library_or_the_repo() -> None:
    outside = []
    for path in files(".py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                # A script imports the scripts in its own folder, or another skill's that it puts
                # on its path. A package's modules and tests import the package, which sits in
                # their folder or the one above. A hook imports the lib_shared package or a
                # module in it.
                sibling = (
                    (path.parent / f"{top}.py").is_file()
                    or any((folder / top / "__init__.py").is_file() for folder in path.parents[:2])
                    or any(SKILLS.glob(f"*/scripts/{top}.py"))
                    or (ROOT / "hooks" / top / "__init__.py").is_file()
                    or (ROOT / "hooks" / "lib_shared" / f"{top}.py").is_file()
                )
                runner = top == "pytest" and "tests" in path.relative_to(ROOT).parts
                if top not in sys.stdlib_module_names and not sibling and not runner:
                    outside.append(f"{path.relative_to(ROOT)}:{node.lineno}: {name}")
    assert outside == []


def frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text().splitlines()
    assert lines[0] == "---", f"{path.relative_to(ROOT)} has no frontmatter"
    end = lines.index("---", 1)
    fields = (line.split(":", 1) for line in lines[1:end] if ":" in line and line[:1] != " ")
    return {key.strip(): value.strip() for key, value in fields}


def test_every_skill_is_named_for_its_folder_with_a_short_enough_description() -> None:
    manifests = sorted(SKILLS.glob("*/SKILL.md"))
    assert manifests
    for manifest in manifests:
        fields = frontmatter(manifest)
        assert fields.get("name") == manifest.parent.name, manifest
        assert 0 < len(fields.get("description", "")) < DESCRIPTION_LIMIT, manifest


def test_both_plugin_files_parse_and_name_the_repo() -> None:
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    repo = plugin["repository"].rstrip("/").rsplit("/", 1)[-1]
    assert plugin["name"] == repo
    assert marketplace["name"] == repo
    assert [entry["name"] for entry in marketplace["plugins"]] == [repo]


def test_shebangs_and_command_lines_name_python3() -> None:
    wrong = []
    for path in files(".py"):
        first = path.read_text().split("\n", 1)[0]
        if first.startswith("#!") and first != SHEBANG:
            wrong.append(f"{path.relative_to(ROOT)}:1: {first}")
    for path in [*files(".py"), *files(".md")]:
        text = path.read_text()
        for found in PYTHON_COMMAND.finditer(text):
            if found.group(1) != "python3":
                wrong.append(f"{where(path, text, found.start())}: {found.group(0)}")
    assert wrong == []


def registrations() -> list[tuple[str, dict[str, Any]]]:
    """Every (event, hook entry) in the root settings.json, which holds the `hooks` key and
    nothing else."""
    settings = json.loads((ROOT / "settings.json").read_text())
    assert list(settings) == ["hooks"]
    return [
        (event, hook)
        for event, groups in settings["hooks"].items()
        for group in groups
        for hook in group["hooks"]
    ]


# A command runs the hook from where the README installs it, ~/.claude/hooks/. A fail-open hook's
# command exits 0 when the file is missing: Python exits 2 on a file it cannot open, and Claude
# Code reads exit 2 from a PreToolUse, UserPromptSubmit or Stop hook as a block.
LAUNCH = re.compile(r'python3 "\$HOME/\.claude/(hooks/(\w+)/\w+/main\.py)"')
GUARDED_LAUNCH = re.compile(
    r'f="\$HOME/\.claude/(hooks/(\w+)/\w+/main\.py)"; \[ -f "\$f" \] \|\| exit 0; python3 "\$f"'
)
# These run before every tool call, prompt or reply, so a broken one must never lock a session.
# Every other PreToolUse hook is a guard that blocks the call when it cannot run.
FAIL_OPEN_HOOKS = {
    "hooks/pre_tool_use/stop_gate/main.py",
    "hooks/user_prompt_submit/stop_flag/main.py",
    "hooks/stop/prose_question_blocker/main.py",
    "hooks/stop/peer_reply_guard/main.py",
}


def test_every_hook_is_registered_once_under_its_folders_event() -> None:
    named = []
    for event, hook in registrations():
        found = LAUNCH.fullmatch(hook["command"]) or GUARDED_LAUNCH.fullmatch(hook["command"])
        assert found, hook["command"]
        assert (ROOT / found.group(1)).is_file(), hook["command"]
        assert event == found.group(2).title().replace("_", ""), hook["command"]
        named.append(found.group(1))
    hooks = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "hooks").glob("*/*/main.py")
        if path.parent.parent.name != "lib_shared"
    )
    assert sorted(named) == hooks


def test_fail_open_hooks_launch_guarded_and_every_other_guard_blocks_when_it_cannot_run() -> None:
    for event, hook in registrations():
        guarded = GUARDED_LAUNCH.fullmatch(hook["command"])
        found = guarded or LAUNCH.fullmatch(hook["command"])
        assert found, hook["command"]
        path = found.group(1)
        if path in FAIL_OPEN_HOOKS:
            assert guarded and "onFailure" not in hook, hook["command"]
        else:
            assert not guarded, hook["command"]
            if event == "PreToolUse":
                assert hook.get("onFailure") == "block", hook["command"]


def test_a_fail_open_hook_whose_file_is_missing_lets_the_call_through(tmp_path: Path) -> None:
    for _, hook in registrations():
        if GUARDED_LAUNCH.fullmatch(hook["command"]):
            done = subprocess.run(
                ["sh", "-c", hook["command"]],
                input="{}",
                capture_output=True,
                text=True,
                env={**os.environ, "HOME": str(tmp_path)},
            )
            assert done.returncode == 0, (hook["command"], done.stderr)
