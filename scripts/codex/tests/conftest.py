"""Fixtures for the codex_jobs tests: a fake `codex` executable and a way to launch it.

`fake_codex` writes `<tmp>/fake-bin/codex`, a wrapper that exports the script and log paths
(the launcher gives the server a minimal environment, so env vars set by a test never reach
it) and runs tests/fake_codex.py with this interpreter. The script format and log records are
documented at the top of fake_codex.py.
"""

import json
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_jobs import appserver, records

FAKE_CODEX = Path(__file__).resolve().parent / "fake_codex.py"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No token command and no real job root reach a test or the processes it starts."""
    monkeypatch.delenv(appserver.TOKEN_COMMAND_ENV, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


class FakeCodex:
    def __init__(self, root: Path):
        self.root = root
        self.script_path = root / "fake-script.json"
        self.log_path = root / "fake-log.jsonl"
        self.bin_dir = root / "fake-bin"
        self.bin = self.bin_dir / "codex"
        self._servers: list[appserver.AppServer] = []
        self.bin_dir.mkdir()
        self.bin.write_text(
            "#!/bin/sh\n"
            f"export FAKE_CODEX_SCRIPT={shlex.quote(str(self.script_path))}\n"
            f"export FAKE_CODEX_LOG={shlex.quote(str(self.log_path))}\n"
            f'exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_CODEX))} "$@"\n'
        )
        self.bin.chmod(0o755)
        self.set_script({})

    def set_script(self, script: dict[str, Any]) -> None:
        self.script_path.write_text(json.dumps(script))

    def launch(self, name: str = "job", **kwargs: Any) -> appserver.AppServer:
        """Start the fake through `launch_app_server`; stopped at teardown.

        No gh token unless asked.
        """
        job_dir = self.root / name
        job_dir.mkdir(exist_ok=True)
        kwargs.setdefault("token_resolver", lambda repo_dir: None)
        server = appserver.launch_app_server(job_dir, self.bin, self.root, **kwargs)
        self._servers.append(server)
        return server

    def records(self, kind: str | None = None) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        rows = []
        for line in self.log_path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # a line still being written
            if kind is None or row["kind"] == kind:
                rows.append(row)
        return rows

    def received(self, client: int | None = None) -> list[dict[str, Any]]:
        """Messages the fake received (parsed JSON), optionally from one client."""
        return [r["msg"] for r in self.records("recv") if client is None or r["client"] == client]

    def wait_for(
        self, predicate: Callable[[dict[str, Any]], bool], timeout: float = 5.0
    ) -> dict[str, Any]:
        """The first log record matching `predicate`, waiting for the fake to write it."""
        deadline = time.monotonic() + timeout
        while True:
            for row in self.records():
                if predicate(row):
                    return row
            if time.monotonic() > deadline:
                raise AssertionError(f"no matching fake log record in {timeout}s: {self.records()}")
            time.sleep(0.02)

    def shutdown(self) -> None:
        """Stop every fake app-server this run started: the ones from `launch` and the ones a
        runner launched."""
        pgids = {server.pgid for server in self._servers} | {
            row["pgid"] for row in self.records("start")
        }
        for pgid in pgids:
            appserver.stop_app_server(pgid, grace=1.0)
        for row in self.records("listen"):
            if row["bound"] != row["path"]:  # a killed fake leaves its short socket dir in /tmp
                shutil.rmtree(Path(row["bound"]).parent, ignore_errors=True)


GIT_IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]


def _commit_file(repo: Path, name: str, text: str = "x\n") -> None:
    (repo / name).write_text(text)
    for args in (["add", name], ["commit", "-q", "-m", name]):
        subprocess.run(
            ["git", "-C", str(repo), *GIT_IDENTITY, *args], check=True, capture_output=True
        )


@pytest.fixture
def commit_file() -> Callable[..., None]:
    """`commit_file(repo, name, text)`: add the file to the repo and commit it."""
    return _commit_file


@pytest.fixture
def review_repo(tmp_path: Path) -> Path:
    """A feature branch one commit ahead of `origin/trunk`, with `origin/HEAD` set: what a review
    is attested against."""
    origin = tmp_path / "review-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "trunk", str(origin)], check=True)
    repo = tmp_path / "review-repo"
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
    _commit_file(repo, "base.txt")
    for args in (
        ["remote", "add", "origin", str(origin)],
        ["push", "-q", "origin", "trunk"],
        ["remote", "set-head", "origin", "trunk"],
        ["checkout", "-q", "-b", "feature"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    _commit_file(repo, "change.txt")
    return repo.resolve()


@pytest.fixture
def fake_codex(tmp_path: Path) -> Iterator[FakeCodex]:
    fake = FakeCodex(tmp_path)
    yield fake
    fake.shutdown()


@pytest.fixture
def hold_slots(tmp_path: Path) -> Iterator[Callable[[Path, int], list[subprocess.Popen]]]:
    """`hold(root, n)`: n `admitted` jobs under `root` whose runner is a live `sleep`; returns the
    sleepers."""
    sleepers: list[subprocess.Popen] = []

    def hold(root: Path, count: int) -> list[subprocess.Popen]:
        held = []
        for _ in range(count):
            job, _created = records.create_job(
                root,
                kind="consult",
                cwd=str(tmp_path),
                project_dir=str(tmp_path),
                model_requested="luna",
                model_slug="gpt-6-luna",
                effort_requested="low",
                sandbox="read-only",
                timeout_seconds=60,
                output_dir=str(tmp_path / "held-out"),
                codex_bin="codex",
            )
            sleeper = subprocess.Popen(["sleep", "300"])
            sleepers.append(sleeper)
            held.append(sleeper)
            records.update_job(root, job["job_id"], runner_pid=sleeper.pid)
            records.transition(root, job["job_id"], records.ADMITTED)
        return held

    yield hold
    for sleeper in sleepers:
        sleeper.kill()
        sleeper.wait()
