"""The `codex-jobs` command line.

Every stdout line is one of the machine-parseable forms below (logs go to stderr), plus JSON
for `status <id>`, `result` and `capabilities` and one tab-separated line per job for `status`:

    SUCCESS: <output-dir>            the job finished and its deliverable is in <output-dir>
    FAILURE: <reason>                the command did not do what was asked
    FAILURE: usage limit (resets <ISO>)   the usage gate refused the job or its turn ran out of
                                     usage (`wait` and `result` print it last)
    JOB: <job-id>                    a job was started (or an idempotency key found its owner)
    STATE: <job-id> <state>          a job's state after `wait`, `cancel`, `steer`, `interrupt`
    <path>                           `release`: the worktree whose lease the job dropped
    RUNNING: <job-id> <state>        `wait` ran out of time with the job still active

Exit codes: 0 done, 1 failure, 2 usage error, 75 `wait` timed out. Every path the command touches
arrives as a parameter of `main`; `__main__.py` supplies the real ones.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    admission,
    appserver,
    breadth,
    models,
    permissions,
    reconcile,
    records,
    review,
    runner,
    worktrees,
)

EXIT_OK, EXIT_FAILURE, EXIT_USAGE, EXIT_RUNNING = 0, 1, 2, 75
DEFAULT_MODEL = "sol"
DEFAULT_EFFORT = "high"
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_WAIT_SECONDS = 540
CANCEL_GRACE_SECONDS = 10.0
CANCEL_SETTLE_SECONDS = 10.0
SERVER_SETTLE_SECONDS = 10.0
POLL_SECONDS = 0.1
SERVER_CALL_TIMEOUT = 30.0


class CliError(Exception):
    """A failure to report as `FAILURE: <message>` with exit 1."""


class _Parser(argparse.ArgumentParser):
    """Usage errors keep the stdout contract: one `FAILURE:` line, exit 2."""

    def error(self, message: str) -> None:
        print(f"FAILURE: {message}", flush=True)
        raise SystemExit(EXIT_USAGE)


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer of seconds, got {text!r}")
    return value


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _say(line: str) -> None:
    print(line, flush=True)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="codex-jobs", description="Run Codex jobs through a local app-server.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    job_options = _Parser(add_help=False)
    job_options.add_argument(
        "--project-dir", help="project directory (default: git root, else cwd)"
    )
    job_options.add_argument("--output-dir", help="must resolve inside <project-dir>/.artifacts")
    job_options.add_argument(
        "--timeout",
        type=_positive_int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="seconds (default: 1800)",
    )
    job_options.add_argument(
        "--image", action="append", default=[], metavar="FILE", help="attach an image; repeatable"
    )
    job_options.add_argument(
        "--idempotency-key", help="a second start with the same key returns the first job"
    )
    job_options.add_argument(
        "--codex-bin", help="Codex executable (default: $CODEX_JOBS_BIN, else codex on PATH)"
    )
    mode = job_options.add_mutually_exclusive_group()
    mode.add_argument("--detach", action="store_true", help="print JOB: <id> and return")
    mode.add_argument("--wait", action="store_true", help="follow the job to its end (default)")

    def add_model_options(
        sub: argparse.ArgumentParser, *, required: bool, inherit: bool = False
    ) -> None:
        if required:
            sub.add_argument("--model", required=True, help="tier or catalog slug")
            sub.add_argument("--effort", required=True, help="reasoning effort")
        elif inherit:
            sub.add_argument(
                "--model",
                help=f"tier or catalog slug (default: the source job's, else {DEFAULT_MODEL})",
            )
            sub.add_argument(
                "--effort",
                help=f"reasoning effort (default: the source job's, else {DEFAULT_EFFORT})",
            )
        else:
            sub.add_argument(
                "--model", default=DEFAULT_MODEL, help="tier or catalog slug (default: sol)"
            )
            sub.add_argument(
                "--effort", default=DEFAULT_EFFORT, help="reasoning effort (default: high)"
            )

    consult = commands.add_parser("consult", parents=[job_options], help="start a consult job")
    add_model_options(consult, required=False)
    consult.add_argument("prompt_file")
    review_sub = commands.add_parser("review", parents=[job_options], help="start a review job")
    add_model_options(review_sub, required=False)
    review_sub.add_argument("prompt_file", nargs="?", help="required unless --native")
    review_sub.add_argument(
        "--native",
        action="store_true",
        help="run Codex's built-in reviewer: no prompt file, prose review.md only, no findings",
    )
    review_sub.add_argument("--target", help=f"what --native reviews: {review.TARGET_FORMS}")
    run = commands.add_parser(
        "run", parents=[job_options], help="start a job that edits files in a worktree"
    )
    add_model_options(run, required=True)
    run.add_argument("prompt_file")

    def add_placement(sub: argparse.ArgumentParser, *, required: bool) -> None:
        place = sub.add_mutually_exclusive_group(required=required)
        place.add_argument(
            "--branch",
            help="create a worktree on this new branch (needs --project-dir, any path in the repo)",
        )
        place.add_argument("--worktree", help="use this clean linked worktree of a repo")

    add_placement(run, required=True)
    run.add_argument(
        "--allow-commit", action="store_true", help="let the job commit to the worktree's branch"
    )
    for name, summary in (
        ("resume", "continue a thread in a new job (a run job continues in its worktree)"),
        ("fork", "branch a thread in a new job"),
    ):
        sub = commands.add_parser(name, parents=[job_options], help=summary)
        add_model_options(sub, required=False, inherit=True)
        sub.add_argument("source", metavar="job-or-thread-id")
        sub.add_argument("prompt_file")
        if name == "fork":
            add_placement(sub, required=False)

    status = commands.add_parser("status", help="list jobs, or print one job's record")
    status.add_argument("job_id", nargs="?")
    result = commands.add_parser("result", help="print a job's result.json")
    result.add_argument("job_id")
    wait = commands.add_parser("wait", help="follow a job for up to --max-seconds")
    wait.add_argument("job_id")
    wait.add_argument("--max-seconds", type=_positive_int, default=DEFAULT_WAIT_SECONDS)
    cancel = commands.add_parser("cancel", help="interrupt a job's turn and stop its app-server")
    cancel.add_argument("job_id")
    steer = commands.add_parser("steer", help="add input to a running turn")
    steer.add_argument("job_id")
    steer.add_argument("text")
    interrupt = commands.add_parser("interrupt", help="interrupt a job's turn")
    interrupt.add_argument("job_id")
    release = commands.add_parser(
        "release",
        help="drop a finished job's worktree lease and remove a worktree the service made",
    )
    release.add_argument("job_id")
    commands.add_parser("reconcile", help="settle every job whose runner died")
    capabilities = commands.add_parser(
        "capabilities", help="print kinds, tiers, models, efforts and live usage as JSON"
    )
    capabilities.add_argument(
        "--codex-bin", help="Codex executable (default: $CODEX_JOBS_BIN, else codex on PATH)"
    )
    commands.add_parser("mcp", help="serve these commands as MCP tools on stdio")
    return parser


def main(
    argv: Sequence[str],
    *,
    root: Path,
    catalog_path: Path,
    models_sh_path: Path,
    sessions_dir: Path,
) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    context = _Context(Path(root), Path(catalog_path), Path(models_sh_path), Path(sessions_dir))
    try:
        return _COMMANDS[args.command](context, args, parser)
    except (
        CliError,
        records.RecordError,
        models.ModelResolutionError,
        worktrees.WorktreeError,
        admission.AdmissionConfigError,
    ) as exc:
        _say(f"FAILURE: {exc}")
        return EXIT_FAILURE


class _Context:
    def __init__(self, root: Path, catalog_path: Path, models_sh_path: Path, sessions_dir: Path):
        self.root = root
        self.catalog_path = catalog_path
        self.models_sh_path = models_sh_path
        self.sessions_dir = sessions_dir

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id


def _project_dir(explicit: str | None) -> Path:
    if explicit:
        project = Path(os.path.abspath(Path(explicit).expanduser()))
        if not project.is_dir():
            raise CliError(f"project directory not found: {project}")
        return project
    cwd = os.getcwd()
    try:
        top = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except FileNotFoundError:
        top = ""
    return Path(top or cwd)


def _resolve_codex_bin(explicit: str | None) -> str:
    candidate = explicit or os.environ.get("CODEX_JOBS_BIN") or "codex"
    found = shutil.which(candidate)
    if not found:
        raise CliError("Codex executable not found or not executable")
    return str(Path(found).resolve())


def _checked_output_dir(project: Path, requested: str) -> Path:
    """The physical path of `requested`, which must lie strictly inside `<project>/.artifacts`."""
    path = Path(requested).expanduser()
    if not path.is_absolute():
        path = project / path
    artifacts = Path(os.path.realpath(project / ".artifacts"))
    resolved = Path(os.path.realpath(path))
    if resolved == artifacts or not resolved.is_relative_to(artifacts):
        raise CliError(f"refusing --output-dir outside {project / '.artifacts'}: {requested}")
    return resolved


def _new_auto_output_dir(project: Path) -> Path:
    """`.artifacts/codex/[<session8>/]analysis-<ts>-<random>`, allocated atomically."""
    base = project / ".artifacts" / "codex"
    session = os.environ.get("CLAUDE_SESSION_ID")
    if session:
        base = base / session[:8]
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"analysis-{datetime.now():%Y%m%d_%H%M%S}-", dir=base))


def _prepare_output_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".gitignore").write_text("*\n")


def _source_for(ctx: _Context, source: str) -> tuple[str, dict[str, Any] | None]:
    """(thread id, parent job record) for a `resume`/`fork` argument.

    The argument is a job id or a thread id.
    """
    if records.JOB_ID_RE.match(source):
        parent = records.load_job(ctx.root, source)
        if not parent["thread_id"]:
            raise CliError(f"job {source} has no thread yet")
        return parent["thread_id"], parent
    return source, None


def _review_target(args: argparse.Namespace) -> dict[str, str] | None:
    """The `review/start` target of a `review --native` start, or None for any other start.

    CliError for flags that do not fit.
    """
    if args.command != "review":
        return None
    if not args.native:
        if args.target is not None:
            raise CliError("--target needs --native")
        if args.prompt_file is None:
            raise CliError("review needs a prompt file (or --native --target ...)")
        return None
    if args.target is None:
        raise CliError(f"--native needs --target {review.TARGET_FORMS}")
    if args.prompt_file is not None or args.image:
        raise CliError(
            "--native takes no prompt file or --image: "
            "Codex's built-in reviewer is given the target only"
        )
    try:
        return review.parse_target(args.target)
    except ValueError as exc:
        raise CliError(str(exc)) from exc


def _fork_permission_class(args: argparse.Namespace, parent: dict[str, Any] | None) -> str:
    """The class a fork starts with.

    The parent's when the fork holds a worktree of its own, else `read`.
    """
    if not (args.branch or args.worktree):
        return permissions.READ
    if parent is None or permissions.permission_class_for(parent) == permissions.READ:
        raise CliError(
            "--branch and --worktree apply to a fork of a run job; "
            "this source has no worktree to inherit"
        )
    if args.branch:
        if not args.project_dir:
            raise CliError("fork --branch needs --project-dir: the repo to create the worktree in")
        own = worktrees.planned_worktree_path(
            worktrees.main_checkout(_project_dir(args.project_dir)), args.branch
        )
    else:
        own = worktrees.worktree_toplevel(Path(os.path.abspath(Path(args.worktree).expanduser())))
    fork_class = permissions.fork_permission_class(
        permissions.permission_class_for(parent),
        fork_worktree=own,
        parent_worktree=parent["worktree"],
    )
    if fork_class == permissions.READ and args.branch:
        raise CliError(f"{own} already exists; pick another branch name")
    if fork_class == permissions.READ:
        _log(f"{own} is the worktree of job {parent['job_id']}; the fork runs read-only")
    return fork_class


def _start_job(ctx: _Context, args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    review_target = _review_target(args)
    if args.idempotency_key is not None:
        owner = records.job_for_key(ctx.root, args.idempotency_key)
        if owner is not None:
            _say(f"JOB: {owner}")
            return EXIT_OK
    source_mode = source_thread_id = parent = None
    if args.command in ("resume", "fork"):
        source_mode = args.command
        source_thread_id, parent = _source_for(ctx, args.source)
    model_requested = args.model or (parent["model_slug"] if parent else DEFAULT_MODEL)
    effort_requested = args.effort or (parent["effort_requested"] if parent else DEFAULT_EFFORT)
    resolution = models.resolve_model(
        model_requested,
        effort_requested,
        catalog_path=ctx.catalog_path,
        models_sh_path=ctx.models_sh_path,
    )
    fork_class = (
        _fork_permission_class(args, parent) if args.command == "fork" else permissions.READ
    )
    resumes_run = (
        args.command == "resume"
        and parent is not None
        and permissions.permission_class_for(parent) != permissions.READ
    )
    writing = args.command == "run" or fork_class != permissions.READ or resumes_run
    if args.command == "run":
        allow_commit = args.allow_commit
    elif resumes_run:
        allow_commit = parent["allow_commit"]
    else:
        allow_commit = fork_class == permissions.WRITE_COMMIT
    places_worktree = writing and not resumes_run
    caller_worktree = (
        Path(os.path.abspath(Path(args.worktree).expanduser()))
        if places_worktree and args.worktree
        else None
    )
    if places_worktree and caller_worktree is None and not args.project_dir:
        raise CliError("run --branch needs --project-dir: the repo to create the worktree in")
    project = _project_dir(args.project_dir or (str(caller_worktree) if caller_worktree else None))
    codex_bin = _resolve_codex_bin(args.codex_bin)

    prompt = None
    if review_target is None:
        prompt = Path(args.prompt_file).expanduser()
        if not prompt.is_absolute():
            prompt = project / prompt
        if not prompt.is_file():
            raise CliError(f"prompt file not found: {prompt}")
    images = []
    for image in args.image:
        if not image:
            raise CliError("--image needs a file path, got an empty value")
        image_path = Path(image).expanduser()
        image_path = Path(
            os.path.abspath(image_path if image_path.is_absolute() else project / image_path)
        )
        if not image_path.is_file():
            raise CliError(f"image file not found: {image_path}")
        images.append(str(image_path))

    kind = "run" if writing else "review" if args.command == "review" else "consult"
    thread_cwd = project
    if parent is not None and not writing:
        thread_cwd = Path(parent["cwd"])
        if not thread_cwd.is_dir():
            raise CliError(
                f"job {parent['job_id']} ran in {thread_cwd}, which is gone; "
                f"{args.command} its thread id instead"
            )

    auto_dir = args.output_dir is None
    output_dir = (
        _new_auto_output_dir(project) if auto_dir else _checked_output_dir(project, args.output_dir)
    )

    job, created = records.create_job(
        ctx.root,
        kind=kind,
        cwd=str(thread_cwd),
        project_dir=str(project),
        model_requested=model_requested,
        model_slug=resolution.slug,
        catalog_fetched_at=resolution.fetched_at,
        effort_requested=effort_requested,
        sandbox="workspace-write" if writing else "read-only",
        allow_commit=allow_commit,
        timeout_seconds=args.timeout,
        output_dir=str(output_dir),
        idempotency_key=args.idempotency_key,
        parent_job_id=parent["job_id"] if parent else None,
        codex_bin=codex_bin,
        images=images,
        source_mode=source_mode,
        source_thread_id=source_thread_id,
        review_target=review_target,
        # this process owns the job through preparation, until its runner starts
        runner_pid=os.getpid(),
    )
    if not created:
        if auto_dir:
            shutil.rmtree(output_dir, ignore_errors=True)
        _say(f"JOB: {job['job_id']}")
        return EXIT_OK

    job_id = job["job_id"]
    job_dir = ctx.job_dir(job_id)
    if writing:
        try:
            if resumes_run:
                tree = worktrees.resume_worktree(ctx.root, job_id, _server_settled(ctx, parent))
            elif caller_worktree:
                _settle_holder(ctx, caller_worktree)
                tree = worktrees.use_worktree(ctx.root, job_id, caller_worktree)
            else:
                tree = worktrees.create_worktree(ctx.root, job_id, project, args.branch)
        except worktrees.WorktreeError as exc:
            records.transition(ctx.root, job_id, records.FAILED, error=str(exc), runner_pid=None)
            if auto_dir:
                shutil.rmtree(output_dir, ignore_errors=True)
            _say(f"FAILURE: {exc}")
            return EXIT_FAILURE
        records.update_job(
            ctx.root,
            job_id,
            cwd=tree.path,
            worktree=tree.path,
            branch=tree.branch,
            base_commit=tree.base_commit,
        )
    try:
        _prepare_output_dir(output_dir)
        if prompt is not None:
            shutil.copyfile(prompt, job_dir / "prompt.txt")
        prepared = records.load_job(ctx.root, job_id)
        if prepared["state"] not in records.TERMINAL_STATES:
            process = runner.spawn_runner(job_dir)
    except Exception as exc:
        records.transition(
            ctx.root, job_id, records.FAILED, error=f"could not start: {exc}", runner_pid=None
        )
        raise
    if (
        prepared["state"] in records.TERMINAL_STATES
    ):  # settled by someone else while this process prepared it
        records.update_job(ctx.root, job_id, runner_pid=None)
        if auto_dir:
            shutil.rmtree(output_dir, ignore_errors=True)
        _say(f"FAILURE: {_failure_reason(prepared)}")
        return EXIT_FAILURE

    _log(
        f"job {job_id} started "
        f"(model={resolution.slug}, effort={resolution.effort}, timeout={args.timeout}s)"
    )
    if args.detach:
        _say(f"JOB: {job_id}")
        return EXIT_OK

    final = _follow(ctx, job_id, process=process)
    if final["state"] != records.SUCCEEDED:
        _say(f"FAILURE: {_failure_reason(final)}")
        return EXIT_FAILURE
    task_failure = _task_failure(final, job_dir)
    if task_failure:
        _say(f"FAILURE: {task_failure}")
        return EXIT_FAILURE
    _say(f"SUCCESS: {final['output_dir']}")
    return EXIT_OK


def _usage_refusal(job: dict[str, Any]) -> str | None:
    """`usage limit (resets <ISO>)` for a job the usage gate refused or whose turn ran out of
    usage, else None.

    The reset time is the usage window's, from the limits read at the turn's start.
    """
    record = job["admission"]
    if job["state"] != records.FAILED or not isinstance(record, dict):
        return None
    if not admission.refused(record) and job["codex_error_info"] != runner.USAGE_LIMIT_ERROR:
        return None
    resets = record["resetsAt"]
    return "usage limit" if resets is None else f"usage limit (resets {admission.iso_utc(resets)})"


def _failure_reason(job: dict[str, Any]) -> str:
    refusal = _usage_refusal(job)
    if refusal:
        return refusal
    if job["state"] == records.TIMED_OUT:
        return f"timeout after {job['timeout_seconds']}s (job {job['job_id']})"
    detail = f": {job['error']}" if job.get("error") else ""
    return f"job {job['job_id']} {job['state']}{detail}"


def _review_failure(job_dir: Path) -> str | None:
    """Why the review did not complete, from its attestation's `failure: <reason>` status."""
    try:
        status = json.loads((job_dir / "attestation.json").read_text())["exit_status"]
    except (OSError, json.JSONDecodeError, KeyError):
        return None
    return status.removeprefix("failure: ") if status.startswith("failure: ") else None


def _task_succeeded(job_dir: Path) -> bool | None:
    try:
        return json.loads((job_dir / "result.json").read_text()).get("task_succeeded")
    except (OSError, json.JSONDecodeError):
        return None


def _task_failure(job: dict[str, Any], job_dir: Path) -> str | None:
    """Why a `succeeded` job still failed its caller.

    Its result says Codex did not complete the task.

    A review names the reason its attestation records, when it records one.
    """
    if _task_succeeded(job_dir) is False:
        reason = _review_failure(job_dir) if job["kind"] == "review" else None
        return (
            f"{reason or 'codex reported the task did not complete'} "
            f"(job {job['job_id']}; see {job['output_dir']})"
        )
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _runner_dead(job: dict[str, Any], process: Any) -> bool:
    pid = job.get("runner_pid")
    if pid is None:
        return False
    if process is None or process.pid != pid:
        process = runner.spawned_runner(
            pid
        )  # a runner this process spawned, which no handle was passed for
    if process is not None:
        return (
            process.poll() is not None
        )  # polling reaps the child, so it never lingers as a zombie
    return not _pid_alive(pid)


def _follow(
    ctx: _Context, job_id: str, *, process: Any = None, max_seconds: float | None = None
) -> dict[str, Any]:
    """Poll the record until the job is terminal or `max_seconds` pass; reconcile a dead runner."""
    deadline = None if max_seconds is None else time.monotonic() + max_seconds
    while True:
        job = records.load_job(ctx.root, job_id)
        if job["state"] in records.TERMINAL_STATES:
            return job
        if _runner_dead(job, process):
            job = reconcile.reconcile_job(ctx.root, job_id, sessions_dir=ctx.sessions_dir)
            if job["state"] in records.TERMINAL_STATES:
                return job
        if deadline is not None and time.monotonic() >= deadline:
            return job
        time.sleep(POLL_SECONDS)


def _reconciled(ctx: _Context, job_id: str) -> dict[str, Any]:
    return reconcile.reconcile_job(ctx.root, job_id, sessions_dir=ctx.sessions_dir)


def _state_line(job: dict[str, Any]) -> str:
    return f"STATE: {job['job_id']} {job['state']}"


def _cmd_status(ctx: _Context, args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.job_id:
        _say(json.dumps(_reconciled(ctx, args.job_id), indent=2))
        return EXIT_OK
    reconcile.reconcile_all(ctx.root, sessions_dir=ctx.sessions_dir)
    for job in records.list_jobs(ctx.root):
        model = job["model_used"] or job["model_requested"]
        _say("\t".join([job["job_id"], job["kind"], job["state"], model, job["updated_at"]]))
    return EXIT_OK


def _cmd_result(ctx: _Context, args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    job = _reconciled(ctx, args.job_id)
    path = ctx.job_dir(args.job_id) / "result.json"
    if not path.exists():
        raise CliError(f"job {args.job_id} has no result (state {job['state']})")
    _say(json.dumps(json.loads(path.read_text()), indent=2))
    refusal = _usage_refusal(job)
    if refusal:
        _say(f"FAILURE: {refusal}")
        return EXIT_FAILURE
    return EXIT_OK


def _cmd_wait(ctx: _Context, args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    _reconciled(ctx, args.job_id)
    job = _follow(ctx, args.job_id, max_seconds=args.max_seconds)
    if job["state"] not in records.TERMINAL_STATES:
        _say(f"RUNNING: {job['job_id']} {job['state']}")
        return EXIT_RUNNING
    _say(_state_line(job))
    if job["state"] != records.SUCCEEDED:
        refusal = _usage_refusal(job)
        if refusal:
            _say(f"FAILURE: {refusal}")
        return EXIT_FAILURE
    task_failure = _task_failure(job, ctx.job_dir(job["job_id"]))
    if task_failure:
        _say(f"FAILURE: {task_failure}")
        return EXIT_FAILURE
    return EXIT_OK


def _server_settled(ctx: _Context, job: dict[str, Any]) -> dict[str, Any]:
    """`job` reloaded once its app-server has stopped, or after `SERVER_SETTLE_SECONDS`.

    A job turns terminal just before its runner stops the app-server; this gives that stop time
    to land.
    """
    deadline = time.monotonic() + SERVER_SETTLE_SECONDS
    while (
        job["state"] in records.TERMINAL_STATES
        and worktrees.appserver_running(job)
        and time.monotonic() < deadline
    ):
        time.sleep(POLL_SECONDS)
        job = records.load_job(ctx.root, job["job_id"])
    return job


def _settle_holder(ctx: _Context, worktree: Path) -> None:
    """Give the app-server of a finished job still holding `worktree` time to stop, so a claim
    made right after that job ends is not refused while its runner shuts the server down."""
    holder = worktrees.lease_holder(ctx.root, worktree)
    if holder is not None:
        _server_settled(ctx, records.load_job(ctx.root, holder))


def _cmd_release(ctx: _Context, args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    _server_settled(
        ctx, _reconciled(ctx, args.job_id)
    )  # a job whose runner died is settled first, which stops its app-server
    _say(worktrees.release_worktree(ctx.root, args.job_id)["worktree"])
    return EXIT_OK


def _cmd_reconcile(ctx: _Context, args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    for job in reconcile.reconcile_all(ctx.root, sessions_dir=ctx.sessions_dir):
        _say(_state_line(job))
    return EXIT_OK


def _server_call(ctx: _Context, job: dict[str, Any], method: str, params: dict[str, Any]) -> Any:
    socket_path = ctx.job_dir(job["job_id"]) / "app.sock"
    try:
        with appserver.AppServerClient(socket_path) as client:
            client.initialize(timeout=SERVER_CALL_TIMEOUT)
            return client.call(method, params, timeout=SERVER_CALL_TIMEOUT)
    except (
        OSError,
        TimeoutError,
        appserver.ConnectionClosed,
        appserver.RpcError,
        appserver.ProtocolError,
    ) as exc:
        raise CliError(f"{method} failed for job {job['job_id']}: {exc!r}") from exc


def _active_turn(ctx: _Context, job_id: str) -> dict[str, Any]:
    job = _reconciled(ctx, job_id)
    if job["state"] in records.TERMINAL_STATES:
        raise CliError(f"job {job_id} is already {job['state']}")
    if not job["thread_id"] or not job["turn_id"]:
        raise CliError(f"job {job_id} has no running turn yet (state {job['state']})")
    return job


def _cmd_steer(ctx: _Context, args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    job = _active_turn(ctx, args.job_id)
    _server_call(
        ctx,
        job,
        "turn/steer",
        {
            "threadId": job["thread_id"],
            "expectedTurnId": job["turn_id"],
            "input": [{"type": "text", "text": args.text}],
        },
    )
    records.append_event(
        ctx.job_dir(args.job_id), "steered", {"turn_id": job["turn_id"], "text": args.text}
    )
    _say(_state_line(records.load_job(ctx.root, args.job_id)))
    return EXIT_OK


def _cmd_interrupt(ctx: _Context, args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    job = _active_turn(ctx, args.job_id)
    _server_call(
        ctx, job, "turn/interrupt", {"threadId": job["thread_id"], "turnId": job["turn_id"]}
    )
    records.append_event(
        ctx.job_dir(args.job_id), "interrupted_by_client", {"turn_id": job["turn_id"]}
    )
    _say(_state_line(records.load_job(ctx.root, args.job_id)))
    return EXIT_OK


def _cmd_cancel(ctx: _Context, args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    job = _reconciled(ctx, args.job_id)
    job_id = job["job_id"]
    if job["state"] not in records.TERMINAL_STATES:
        # Stamp the intent first: the runner reads it when the interrupted turn completes.
        records.update_job(ctx.root, job_id, cancel_requested_at=_now())
        if job["thread_id"] and job["turn_id"]:
            try:
                _server_call(
                    ctx,
                    job,
                    "turn/interrupt",
                    {"threadId": job["thread_id"], "turnId": job["turn_id"]},
                )
            except CliError as exc:
                _log(f"{exc}; stopping the app-server instead")
        job = _follow(ctx, job_id, max_seconds=CANCEL_GRACE_SECONDS)
        if job["state"] not in records.TERMINAL_STATES:
            runner.stop_server(ctx.job_dir(job_id))
            job = _follow(ctx, job_id, max_seconds=CANCEL_SETTLE_SECONDS)
        if (
            job["state"] not in records.TERMINAL_STATES
            and not job["turn_id"]
            and _runner_dead(job, None)
        ):
            job = records.transition(
                ctx.root, job_id, records.CANCELLED, error="cancelled before dispatch"
            )
    _say(_state_line(job))
    return EXIT_OK if job["state"] == records.CANCELLED else EXIT_FAILURE


def _live_usage(codex_bin: str, config: admission.AdmissionConfig) -> admission.UsageDecision:
    """Read the account's limits through a throwaway app-server."""
    with tempfile.TemporaryDirectory(prefix="cj-usage-") as scratch:
        try:
            server = appserver.launch_app_server(Path(scratch), codex_bin, Path(scratch))
        except appserver.AppServerStartError as exc:
            raise CliError(f"{admission.RATE_LIMITS_METHOD} failed: {exc}") from exc
        try:
            with appserver.AppServerClient(server.socket_path) as client:
                client.initialize(timeout=SERVER_CALL_TIMEOUT)
                return admission.read_usage(client, config)
        except (
            OSError,
            TimeoutError,
            appserver.ConnectionClosed,
            appserver.RpcError,
            appserver.ProtocolError,
            admission.MalformedRateLimits,
        ) as exc:
            raise CliError(f"{admission.RATE_LIMITS_METHOD} failed: {exc!r}") from exc
        finally:
            appserver.stop_app_server(server.pgid)


def _cmd_capabilities(
    ctx: _Context, args: argparse.Namespace, parser: argparse.ArgumentParser
) -> int:
    tiers = models.load_tiers(ctx.models_sh_path)
    catalog, fetched_at = models.load_catalog(ctx.catalog_path)
    subcommands = sorted(
        next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices
    )
    config = admission.load_config(admission.DEFAULT_CONFIG_PATH)
    usage = _live_usage(_resolve_codex_bin(args.codex_bin), config).record
    _say(
        json.dumps(
            {
                "kinds": list(records.KINDS),
                "subcommands": subcommands,
                "tiers": tiers,
                "models": {slug: list(efforts) for slug, efforts in catalog.items()},
                "catalog_fetched_at": fetched_at,
                "breadth": [
                    {"name": item["name"], "summary": item["summary"], "tests": list(item["tests"])}
                    for item in breadth.CAPABILITIES
                ],
                "admission": {
                    "usedPercent": usage["usedPercent"],
                    "ordinaryUsageAllowed": usage["ordinaryUsageAllowed"],
                    "slot_holders": len(admission.slot_holders(ctx.root)),
                    "max_concurrent_jobs": config.max_concurrent_jobs,
                },
            },
            indent=2,
        )
    )
    return EXIT_OK


def _cmd_mcp(ctx: _Context, args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from . import mcp_server  # imported here: mcp_server imports this module

    return mcp_server.serve(
        root=ctx.root,
        catalog_path=ctx.catalog_path,
        models_sh_path=ctx.models_sh_path,
        sessions_dir=ctx.sessions_dir,
    )


_COMMANDS = {
    "consult": _start_job,
    "review": _start_job,
    "run": _start_job,
    "resume": _start_job,
    "fork": _start_job,
    "status": _cmd_status,
    "result": _cmd_result,
    "wait": _cmd_wait,
    "cancel": _cmd_cancel,
    "steer": _cmd_steer,
    "interrupt": _cmd_interrupt,
    "release": _cmd_release,
    "reconcile": _cmd_reconcile,
    "capabilities": _cmd_capabilities,
    "mcp": _cmd_mcp,
}
