#!/usr/bin/env python3
"""Write invented Claude Code sessions for the container gate to run self-optimize on.

    python3 fixtures.py CONFIG_DIR [PROJECT_DIR]

Inputs: CONFIG_DIR, the Claude Code config folder the run uses (created when missing), and
PROJECT_DIR, the folder the sessions were invented for.
Outputs: CONFIG_DIR/CLAUDE.md with a vague testing rule, and ten sessions under
CONFIG_DIR/projects/-home-dev-shop/, dated 2 to 14 days before now so a first run, which reads
the 30 days through yesterday, sees them all. In five sessions the user corrects the same habit
(the agent runs the whole test suite after a one-file change). In three more the agent does the
same and the user lets it pass: the instances an eval suite is written from, since its cases never
come from the sessions the diagnosis quotes. The other two hold an approval and a one-off
correction. Each file's modification time is its last message's. With PROJECT_DIR, a
`tests/test_<module>.py` there for each module the sessions touch, so a run that looks for a test
file a session names finds it in the project; a file already there is left as it is.

Exit codes: 0 written; 2 invalid arguments.
"""

import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

CWD = "/home/dev/shop"
PROJECT = "-home-dev-shop"
MODEL = "claude-sonnet-5-5"
RULES = "# Shop\n\nRun the tests before you finish a change.\n"
# The modules the sessions touch; each has a test file a session names or implies.
MODULES = ("orders", "tax", "api", "shipping", "export", "pricing", "cart")
TEST_FILE = """import unittest


class {name}(unittest.TestCase):
    def test_{module}_module(self):
        self.assertTrue("{module}")
"""

# (days ago, the request, the command the agent ran, what it said, the user's reply)
SESSIONS: list[tuple[int, str, str, str, str]] = [
    (
        14,
        "Add a discount field to the Order model and test it.",
        "pytest -q",
        "Added `discount` to Order with a default of 0 and ran the tests: 912 passed.",
        "Don't run the whole suite for a one-file change, just run tests/test_orders.py.",
    ),
    (
        13,
        "Fix the typo in the 404 page template.",
        "pytest -q",
        "Fixed 'recieved' in templates/404.html and ran the tests: 912 passed.",
        "Ok.",
    ),
    (
        11,
        "Fix the rounding in tax.py, totals are off by a cent.",
        "pytest",
        "Rounded with Decimal.quantize and ran the tests: 912 passed in 4m 10s.",
        "Again the full suite. Run only the tests for the module you touched.",
    ),
    (
        9,
        "Write a setup section for the README.",
        "",
        "Added a Setup section: clone, create a virtualenv, install requirements, run the app.",
        "Thanks, that reads well.",
    ),
    (
        8,
        "Rename get_user to fetch_user in api.py and its callers.",
        "pytest -q tests/",
        "Renamed it in api.py and the two callers; all 912 tests pass.",
        "Stop running everything, tests/test_api.py covers this.",
    ),
    (
        7,
        "Make the shipping fee 4.99 in shipping.py; tests/test_shipping.py checks it.",
        "pytest",
        "Changed SHIPPING_FEE to 4.99 and ran the tests: 912 passed in 4m 05s.",
        "Thanks.",
    ),
    (
        6,
        "Bump requests in requirements.txt.",
        "",
        "Set requests to >=2.32 in requirements.txt.",
        "Pin it exactly, requests==2.32.3. We pin every dependency.",
    ),
    (
        4,
        "Change the CSV export header to use Title Case.",
        "make test",
        "Updated the header row and ran make test: all 912 tests pass.",
        "That runs all 912 tests for a header change. Only tests/test_export.py next time.",
    ),
    (
        3,
        "Add a docstring to discount() in pricing.py. Its tests are in tests/test_pricing.py.",
        "make test",
        "Added the docstring and ran make test: all 912 tests pass.",
        "Good.",
    ),
    (
        2,
        "Add type hints to cart.py.",
        "pytest -q",
        "Added hints to every function in cart.py; mypy is clean and 912 tests pass.",
        "Please, only the cart tests. The full run takes four minutes every time.",
    ),
]


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def session_records(index: int, start: datetime) -> list[dict[str, Any]]:
    """The records of one session, in the shape Claude Code writes them."""
    _, request, command, reply, correction = SESSIONS[index]
    session = str(uuid.uuid5(uuid.NAMESPACE_URL, f"container-gate/{index}"))
    records: list[dict[str, Any]] = []

    def add(kind: str, seconds: int, message: dict[str, Any]) -> None:
        records.append(
            {
                "type": kind,
                "uuid": f"{session[:8]}-{len(records):02d}",
                "sessionId": session,
                "timestamp": stamp(start + timedelta(seconds=seconds)),
                "cwd": CWD,
                "gitBranch": "main",
                "message": message,
            }
        )

    def say(seconds: int, request_id: str, content: list[dict[str, Any]]) -> None:
        add(
            "assistant",
            seconds,
            {
                "id": f"msg-{index}-{request_id}",
                "model": MODEL,
                "role": "assistant",
                "content": content,
                "usage": {
                    "input_tokens": 1200,
                    "cache_read_input_tokens": 8000,
                    "output_tokens": 150,
                },
            },
        )

    add("user", 0, {"role": "user", "content": request})
    if command:
        tool = f"toolu_{index}_1"
        say(
            20,
            "1",
            [
                {
                    "type": "tool_use",
                    "id": tool,
                    "name": "Bash",
                    "input": {"command": command, "description": "Run the tests"},
                }
            ],
        )
        add(
            "user",
            270,
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool, "content": "912 passed in 250.4s"}
                ],
            },
        )
    say(280, "2", [{"type": "text", "text": reply}])
    add("user", 330, {"role": "user", "content": correction})
    say(340, "3", [{"type": "text", "text": "Understood."}])
    return records


def write(config_dir: Path, now: datetime) -> list[Path]:
    """Write CLAUDE.md and the sessions under *config_dir*; returns the session files."""
    folder = config_dir / "projects" / PROJECT
    folder.mkdir(parents=True, exist_ok=True)
    (config_dir / "CLAUDE.md").write_text(RULES)
    written = []
    for index, (days_ago, *_) in enumerate(SESSIONS):
        start = (now - timedelta(days=days_ago)).replace(hour=10, minute=0, second=0, microsecond=0)
        records = session_records(index, start)
        path = folder / f"{records[0]['sessionId']}.jsonl"
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        last = datetime.fromisoformat(records[-1]["timestamp"].replace("Z", "+00:00")).timestamp()
        os.utime(path, (last, last))
        written.append(path)
    return written


def write_project(project_dir: Path) -> list[Path]:
    """Write the test file of each module in MODULES under *project_dir*/tests, keeping any that
    exists; returns the files written."""
    folder = project_dir / "tests"
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for module in MODULES:
        path = folder / f"test_{module}.py"
        if not path.exists():
            path.write_text(TEST_FILE.format(name=module.title(), module=module))
            written.append(path)
    return written


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        print("usage: fixtures.py CONFIG_DIR [PROJECT_DIR]", file=sys.stderr)
        return 2
    write(Path(argv[0]), datetime.now(UTC))
    if len(argv) == 2:
        write_project(Path(argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
