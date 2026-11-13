"""Tests for scripts/triage_run.sh: the Haiku digest step and the optional run recorder.

A fake `claude` on PATH records the digest input and answers with canned JSON, so the input
window (whole output when small; grep-matched failure lines + head + tail when large) and the
failure report (the API error from claude's JSON, a sidecar with stdout and stderr) are pinned
without a model call. A stub recorder named by TRIAGE_RUN_RECORDER logs its calls and keeps a
copy of the output file it is handed, so the capture-file lines are pinned too.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "triage_run.sh"
DIGEST = {
    "summary": "1 failed",
    "items": [{"name": "test_mid", "location": "t.py:3", "message": "boom"}],
}


def _fake_claude(bin_dir: Path, stdout: str, exit_code: int = 0, stderr: str = "") -> Path:
    capture = bin_dir / "stdin.txt"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "claude"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f"cat > '{capture}'\n"
        f"printf '%s' {json.dumps(stdout)}\n"
        f"printf '%s' {json.dumps(stderr)} >&2\n"
        f"exit {exit_code}\n"
    )
    fake.chmod(0o755)
    return capture


def _stub_recorder(folder: Path) -> Path:
    """A recorder that logs each call to calls.log; a record is `rec.<n>` with `.out` and `.rc`."""
    folder.mkdir()
    recorder = folder / "recorder"
    recorder.write_text(
        "#!/bin/sh\n"
        f"cd '{folder}'\n"
        'case "$1" in\n'
        '  start) echo "start $TRIAGE_RUN_PID $*" >> calls.log; id="$PWD/rec.$$"; : > "$id";'
        ' echo "$id" ;;\n'
        '  end) echo "end $2 $3" >> calls.log; cp "$4" "$2.out"; echo "$3" > "$2.rc" ;;\n'
        "esac\n"
    )
    recorder.chmod(0o755)
    return recorder


def _env(bin_dir: Path, **extra: str) -> dict[str, str]:
    """The caller's environment with `bin_dir` first on PATH and no recorder unless given."""
    env = {k: v for k, v in os.environ.items() if k != "TRIAGE_RUN_RECORDER"}
    env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    env.update(extra)
    return env


def _records(folder: Path) -> list[tuple[int, str]]:
    """(exit code, recorded output) of every closed record, in no particular order."""
    return [
        (int(rc.read_text()), rc.with_suffix(".out").read_text()) for rc in folder.glob("rec.*.rc")
    ]


def _run(
    tmp_path: Path, output: str, bin_dir: Path, cwd: Path | None = None
) -> subprocess.CompletedProcess:
    src = tmp_path / "src.txt"
    src.write_text(output)
    return subprocess.run(
        [
            "bash",
            str(SCRIPT),
            str(tmp_path / "out" / "run.txt"),
            "--",
            "bash",
            "-c",
            f"cat '{src}'; exit 1",
        ],
        capture_output=True,
        text=True,
        env=_env(bin_dir),
        cwd=cwd,
        timeout=60,
    )


def _ok_json() -> str:
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "structured_output": DIGEST}
    )


def test_small_output_is_sent_whole(tmp_path: Path) -> None:
    capture = _fake_claude(tmp_path / "bin", _ok_json())
    output = "collected 3 items\nFAILED t.py::test_mid - boom\n1 failed, 2 passed\n"
    res = _run(tmp_path, output, tmp_path / "bin")
    assert res.returncode == 1
    assert capture.read_text() == output
    assert "- test_mid  t.py:3" in res.stdout


def test_the_digest_ignores_modules_in_the_callers_folder(tmp_path: Path) -> None:
    _fake_claude(tmp_path / "bin", _ok_json())
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "json.py").write_text("raise SystemExit(7)\n")
    res = _run(tmp_path, "FAILED t.py::test_mid - boom\n", tmp_path / "bin", cwd=repo)
    assert "- test_mid  t.py:3" in res.stdout


def test_large_output_keeps_failure_lines_outside_head_and_tail(tmp_path: Path) -> None:
    capture = _fake_claude(tmp_path / "bin", _ok_json())
    filler = "".join(f"<div class='dom-dump'>row {i}</div>\n" for i in range(9000))
    output = (
        filler[:30_000]
        + "\n FAIL  src/widget.test.tsx > renders the total\n"
        + filler
        + "Tests 1 failed\n"
    )
    res = _run(tmp_path, output, tmp_path / "bin")
    sent = capture.read_text()
    assert "renders the total" in sent
    assert sent.endswith(output[-120_000:])
    assert len(sent.encode()) <= 145_000
    assert "- test_mid" in res.stdout


def test_the_digest_call_drops_the_plugin_dirs_variable(tmp_path: Path) -> None:
    """Plugins listed in CLAUDE_CODE_PLUGIN_DIRS load even with empty setting sources."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    seen = bin_dir / "plugin-dirs.txt"
    fake = bin_dir / "claude"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "cat > /dev/null\n"
        f"printf '%s' \"${{CLAUDE_CODE_PLUGIN_DIRS-unset}}\" > '{seen}'\n"
        f"printf '%s' {json.dumps(_ok_json())}\n"
    )
    fake.chmod(0o755)
    src = tmp_path / "src.txt"
    src.write_text("1 failed\n")
    subprocess.run(
        [
            "bash",
            str(SCRIPT),
            str(tmp_path / "out" / "run.txt"),
            "--",
            "bash",
            "-c",
            f"cat '{src}'; exit 1",
        ],
        capture_output=True,
        text=True,
        env=_env(bin_dir, CLAUDE_CODE_PLUGIN_DIRS="/some/plugins"),
        timeout=60,
    )
    assert seen.read_text() == "unset"


def test_claude_error_is_reported_with_a_sidecar(tmp_path: Path) -> None:
    error = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "api_error_status": 529,
        "result": "Overloaded",
    }
    _fake_claude(tmp_path / "bin", json.dumps(error), exit_code=1, stderr="api 529")
    res = _run(tmp_path, "FAILED t.py::test_mid - boom\n", tmp_path / "bin")
    assert res.returncode == 1
    assert "digest unavailable: 529: Overloaded" in res.stdout
    sidecar = tmp_path / "out" / "run.txt.digest-err"
    assert "api 529" in sidecar.read_text() and "Overloaded" in sidecar.read_text()


def test_without_a_recorder_a_run_prints_one_line_and_leaves_no_capture_file(
    tmp_path: Path,
) -> None:
    out = tmp_path / "out.txt"
    result = subprocess.run(
        ["bash", str(SCRIPT), str(out), "--", sys.executable, "-c", "print('hi')"],
        cwd=tmp_path,
        env=_env(tmp_path / "bin"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        f"ok (exit 0, 1 lines) full output: {out}\n",
        "",
    )
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.txt"]


@pytest.mark.parametrize(
    "code, printed", [("print('3 passed')", "3 passed"), ("print('1 failed'); exit(1)", "1 failed")]
)
def test_a_recorder_gets_the_scripts_pid_the_exit_code_and_the_runs_own_output(
    tmp_path: Path, code: str, printed: str
) -> None:
    _fake_claude(tmp_path / "bin", "", exit_code=1)
    folder = tmp_path / "records"
    out = tmp_path / "run.txt"
    proc = subprocess.Popen(
        ["bash", str(SCRIPT), str(out), "--", sys.executable, "-c", code],
        cwd=tmp_path,
        env=_env(tmp_path / "bin", TRIAGE_RUN_RECORDER=str(_stub_recorder(folder))),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    rc = proc.wait(timeout=30)
    start, end = (folder / "calls.log").read_text().splitlines()
    assert start == f"start {proc.pid} start {out} -- {sys.executable} -c {code}"
    assert end.split()[0] == "end" and end.split()[2] == str(rc)
    assert _records(folder) == [(rc, f"{printed}\n")]
    assert not list(tmp_path.glob(".triage-run.*"))


def test_an_out_file_that_is_a_directory_fails_without_running(tmp_path: Path) -> None:
    """The script fails the way a plain `>` redirect does, and leaves no capture file inside."""
    _fake_claude(tmp_path / "bin", "", exit_code=1)
    out_dir = tmp_path / "outdir"
    out_dir.mkdir()
    marker = tmp_path / "ran"
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            str(out_dir),
            "--",
            sys.executable,
            "-c",
            f"open({str(marker)!r}, 'w').close()",
        ],
        cwd=tmp_path,
        env=_env(tmp_path / "bin", TRIAGE_RUN_RECORDER=str(_stub_recorder(tmp_path / "records"))),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert result.stdout.startswith("FAILED (exit 1,")
    assert not marker.exists()
    assert list(out_dir.iterdir()) == []


@pytest.mark.parametrize("stage", ["linking", "running"])
def test_a_terminated_run_removes_its_capture_file(tmp_path: Path, stage: str) -> None:
    """SIGTERM while the capture file is being linked into place, or while the command runs,
    leaves no `.triage-run.*` link beside <out-file>. Each stage touches a marker when it
    starts, so the signal lands inside it."""
    started = tmp_path / "started"
    bin_dir = tmp_path / "bin"
    _fake_claude(bin_dir, "", exit_code=1)
    command = [sys.executable, "-c", "import time; time.sleep(5)"]
    if stage == "linking":
        slow_ln = bin_dir / "ln"
        slow_ln.write_text(f'#!/bin/sh\ntouch "{started}"\nsleep 2\nexec /bin/ln "$@"\n')
        slow_ln.chmod(0o755)
    else:
        command = [
            sys.executable,
            "-c",
            f"import pathlib, time; pathlib.Path({str(started)!r}).touch(); time.sleep(5)",
        ]
    proc = subprocess.Popen(
        ["bash", str(SCRIPT), str(tmp_path / "run.txt"), "--", *command],
        cwd=tmp_path,
        env=_env(bin_dir, TRIAGE_RUN_RECORDER=str(_stub_recorder(tmp_path / "records"))),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not started.exists():
            time.sleep(0.02)
        assert list(tmp_path.glob(".triage-run.*")), "capture file was never created"
        proc.terminate()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    assert not list(tmp_path.glob(".triage-run.*"))


def test_a_run_sharing_its_out_file_with_a_concurrent_run_records_its_own_output(
    tmp_path: Path,
) -> None:
    _fake_claude(tmp_path / "bin", "", exit_code=1)
    folder = tmp_path / "records"
    env = _env(tmp_path / "bin", TRIAGE_RUN_RECORDER=str(_stub_recorder(folder)))
    out = tmp_path / "run.txt"
    later_run = ["bash", str(SCRIPT), str(out), "--", sys.executable, "-c", "print('600 passed')"]
    # This run prints its own failing summary, then a second run on the same <out-file> starts
    # and finishes green before this one exits.
    this_run = (
        "import subprocess, sys; print('1 failed, 5 passed', flush=True); "
        f"subprocess.run({later_run!r}, capture_output=True); sys.exit(1)"
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), str(out), "--", sys.executable, "-c", this_run],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert sorted(_records(folder)) == [(0, "600 passed\n"), (1, "1 failed, 5 passed\n")]
    assert not list(tmp_path.glob(".triage-run.*"))
