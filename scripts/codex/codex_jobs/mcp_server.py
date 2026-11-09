"""`codex-jobs mcp`: the job commands as MCP tools over newline-delimited JSON-RPC on stdio.

Each tool is a thin client of the code the command line runs. Starting and controlling jobs
calls `cli.main` in-process with that command's argv and reads its stdout lines; reading jobs
goes to `records` and `reconcile` directly. Nothing shells out.

Two handshakes, picked by the first request:

  `server/discover`  the 2026-07-28 protocol, which Claude Code's main loop speaks. It carries
                     `io.modelcontextprotocol/clientCapabilities` in `_meta` on every request.
  `initialize`       the classic handshake, for any other client.

Whether a call may be answered as a task is read per request from its own `_meta` capabilities
(a subagent's calls carry no tasks extension even though the main loop's do), falling back to
the capabilities an `initialize` declared. A client with the extension gets a task from
`codex_wait` (and from a submit tool given `wait: true`): the task id is the job id, so
`tasks/get` is a read of the job record and survives a restart of this server. Any other client
gets the same answer as a plain blocking result.
"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, TextIO

from . import cli, reconcile, records, runner

PROTOCOL_VERSION = "2026-07-28"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
SERVER_INFO = {"name": "codex-jobs", "version": "0.1.0"}
TASK_TTL_MS = 3_600_000
TASK_POLL_MS = 2000
WAIT_POLL_SECONDS = 0.25
SHUTDOWN_JOIN_SECONDS = 5.0

ERR_PARSE, ERR_METHOD_NOT_FOUND, ERR_INVALID_PARAMS, ERR_INTERNAL = -32700, -32601, -32602, -32603

_STRING = {"type": "string"}
_JOB_ID = {"type": "string", "description": "Job id (cj-yyyymmdd-hhmmss-xxxx)."}
_JOB_FLAGS: dict[str, Any] = {
    "prompt": {"type": "string", "description": "Prompt text. Give this or prompt_file."},
    "prompt_file": {
        "type": "string",
        "description": "Path of a prompt file (relative paths resolve against project_dir).",
    },
    "model": {
        "type": "string",
        "description": "Tier (luna, terra, sol, astra) or catalog slug. Default sol (resume, fork: the source job's).",
    },
    "effort": {
        "type": "string",
        "description": "Reasoning effort the model lists. Default high (resume, fork: the source job's).",
    },
    "project_dir": {
        "type": "string",
        "description": "Project directory. Default: git root of the server's cwd, else the cwd; pass it.",
    },
    "output_dir": {
        "type": "string",
        "description": "Deliverable directory; must resolve inside <project_dir>/.artifacts.",
    },
    "timeout": {"type": "integer", "description": "Job timeout in seconds. Default 1800."},
    "images": {"type": "array", "items": _STRING, "description": "Image file paths to attach."},
    "idempotency_key": {
        "type": "string",
        "description": "A second submit with the same key returns the first job.",
    },
    "wait": {
        "type": "boolean",
        "description": "Follow the job to its end and return its result (a task when the client supports tasks). Default false: return the job id at once.",
    },
}
_PLACEMENT_FLAGS: dict[str, Any] = {
    "branch": {
        "type": "string",
        "description": "Create a worktree on this new branch of project_dir's repo (project_dir is required with it).",
    },
    "worktree": {"type": "string", "description": "Use this clean linked worktree."},
}
_RUN_FLAGS: dict[str, Any] = {
    **_PLACEMENT_FLAGS,
    "allow_commit": {
        "type": "boolean",
        "description": "Let the job commit to the worktree's branch. Default false: the caller commits.",
    },
}
_REVIEW_FLAGS: dict[str, Any] = {
    "native": {
        "type": "boolean",
        "description": "Run Codex's built-in reviewer: give no prompt or prompt_file; review.md is prose and there is no findings.json. Needs target.",
    },
    "target": {
        "type": "string",
        "description": "What a native review reads: uncommitted, base:<branch> or commit:<sha>.",
    },
}
_FLAG_OPTIONS = (
    ("model", "--model"),
    ("effort", "--effort"),
    ("project_dir", "--project-dir"),
    ("output_dir", "--output-dir"),
    ("timeout", "--timeout"),
    ("idempotency_key", "--idempotency-key"),
    ("branch", "--branch"),
    ("worktree", "--worktree"),
    ("target", "--target"),
)


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: Sequence[str] = (),
    *,
    read_only: bool = False,
) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
    }
    if read_only:
        tool["annotations"] = {"readOnlyHint": True}
    return tool


TOOLS: list[dict[str, Any]] = [
    _tool(
        "codex_capabilities",
        "Kinds enabled, tiers, models and the efforts each lists, as JSON.",
        {},
        read_only=True,
    ),
    _tool(
        "codex_review",
        "Start a Codex review job. Returns the job id at once.",
        {**_JOB_FLAGS, **_REVIEW_FLAGS},
    ),
    _tool(
        "codex_consult",
        "Start a read-only Codex consultation job. Returns the job id at once.",
        _JOB_FLAGS,
    ),
    _tool(
        "codex_run",
        "Start a Codex task job that writes in a worktree; model and effort are required, and exactly one of branch and worktree. Returns the job id at once.",
        {**_JOB_FLAGS, **_RUN_FLAGS},
    ),
    _tool(
        "codex_resume",
        "Continue a thread (job id or thread id) in a new job. A job id supplies the source's working directory, model and effort unless model or effort is given. Resuming a finished run job continues it with its write access in the same worktree, which the new job takes over; refused while the run job is live, after its worktree was released, or once that worktree is off the job's branch.",
        {
            "source": {"type": "string", "description": "Job id or thread id to continue."},
            **_JOB_FLAGS,
        },
        ["source"],
    ),
    _tool(
        "codex_fork",
        "Branch a thread (job id or thread id) in a new job. A job id supplies the source's working directory, model and effort unless model or effort is given. The fork is read-only; a fork of a run job that gets its own branch or worktree keeps the run job's write access. Pointed at the source's own worktree it silently stays read-only: the job record's sandbox says which.",
        {
            "source": {"type": "string", "description": "Job id or thread id to branch."},
            **_JOB_FLAGS,
            **_PLACEMENT_FLAGS,
        },
        ["source"],
    ),
    _tool(
        "codex_status",
        "One job's record, or without job_id a list of every job.",
        {"job_id": _JOB_ID},
        read_only=True,
    ),
    _tool(
        "codex_result",
        "A finished job's result envelope.",
        {"job_id": _JOB_ID},
        ["job_id"],
        read_only=True,
    ),
    _tool(
        "codex_wait",
        "Wait for a job to end and return its result envelope. A client that supports tasks gets a task that completes with the envelope; otherwise the call blocks until the job ends or max_seconds pass, then returns the envelope or the running state.",
        {
            "job_id": _JOB_ID,
            "max_seconds": {
                "type": "integer",
                "description": "Longest blocking wait. Default: until the job ends. Ignored for a task.",
            },
        },
        ["job_id"],
        read_only=True,
    ),
    _tool(
        "codex_cancel",
        "Interrupt a job's turn and stop its app-server.",
        {"job_id": _JOB_ID},
        ["job_id"],
    ),
    _tool(
        "codex_steer",
        "Add input to a job's running turn.",
        {"job_id": _JOB_ID, "text": {"type": "string", "description": "Text to add to the turn."}},
        ["job_id", "text"],
    ),
    _tool("codex_interrupt", "Interrupt a job's running turn.", {"job_id": _JOB_ID}, ["job_id"]),
]
_TOOL_SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOLS}


class ToolError(Exception):
    """A tool call that fails: reported as `isError: true` content, not a protocol error."""


class MethodNotFound(Exception):
    """A JSON-RPC method this server does not implement."""


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "resultType": "complete",
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def _json_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    return _text_result(json.dumps(payload, indent=2), is_error=is_error)


def _check_type(value: Any, spec: dict[str, Any]) -> bool:
    kind = spec["type"]
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list) and all(_check_type(item, spec["items"]) for item in value)
    return False


def validate_arguments(name: str, arguments: dict[str, Any]) -> None:
    schema = _TOOL_SCHEMAS[name]
    properties = schema["properties"]
    for key in schema["required"]:
        if key not in arguments:
            raise ToolError(f"{name}: missing required argument {key!r}")
    for key, value in arguments.items():
        if key not in properties:
            raise ToolError(f"{name}: unknown argument {key!r}; accepted: {sorted(properties)}")
        if not _check_type(value, properties[key]):
            raise ToolError(f"{name}: argument {key!r} must be of type {properties[key]['type']}")


class _StdoutRouter:
    """Stands in for `sys.stdout` so `cli.main`'s prints can be captured per thread.

    The JSON-RPC stream is a separate handle, so a stray print from anywhere else lands on
    stderr instead of corrupting the protocol.
    """

    def __init__(self, fallback: TextIO):
        self._local = threading.local()
        self._fallback = fallback

    @contextlib.contextmanager
    def capture(self) -> Iterator[io.StringIO]:
        buffer = io.StringIO()
        self._local.buffer = buffer
        try:
            yield buffer
        finally:
            self._local.buffer = None

    def write(self, text: str) -> int:
        target = getattr(self._local, "buffer", None) or self._fallback
        return target.write(text)

    def flush(self) -> None:
        target = getattr(self._local, "buffer", None) or self._fallback
        target.flush()


class Server:
    """The tools and handshakes. Use as a context manager: it routes `sys.stdout` while open."""

    def __init__(self, *, root: Path, catalog_path: Path, models_sh_path: Path, sessions_dir: Path):
        self.root = Path(root)
        self.catalog_path = Path(catalog_path)
        self.models_sh_path = Path(models_sh_path)
        self.sessions_dir = Path(sessions_dir)
        self.closing = threading.Event()
        self._session_capabilities: dict[str, Any] = {}
        self._router = _StdoutRouter(sys.stderr)
        self._router_lock = threading.Lock()
        self._saved_stdout: TextIO | None = None
        self._tools: dict[str, Callable[[dict[str, Any], bool], dict[str, Any]]] = {
            "codex_capabilities": lambda a, t: self._cli_text(["capabilities"]),
            "codex_review": lambda a, t: self._submit("review", a, t),
            "codex_consult": lambda a, t: self._submit("consult", a, t),
            "codex_run": lambda a, t: self._submit("run", a, t),
            "codex_resume": lambda a, t: self._submit("resume", a, t),
            "codex_fork": lambda a, t: self._submit("fork", a, t),
            "codex_status": lambda a, t: self._status(a),
            "codex_result": lambda a, t: self._result(a),
            "codex_wait": lambda a, t: self._wait(a, t),
            "codex_cancel": lambda a, t: self._cli_text(["cancel", self._job_id(a)]),
            "codex_steer": lambda a, t: self._cli_text(["steer", "--", self._job_id(a), a["text"]]),
            "codex_interrupt": lambda a, t: self._cli_text(["interrupt", self._job_id(a)]),
        }

    def __enter__(self) -> Server:
        self._route_stdout()
        return self

    def __exit__(self, *exc: object) -> None:
        self.closing.set()
        if self._saved_stdout is not None and sys.stdout is self._router:
            sys.stdout = self._saved_stdout

    def _route_stdout(self) -> None:
        """Make `sys.stdout` the router, again if something (a test harness) swapped it out."""
        with self._router_lock:
            if sys.stdout is not self._router:
                self._saved_stdout = sys.stdout
                sys.stdout = self._router

    def dispatch(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """The response to one JSON-RPC message, or None for a notification.

        Each message first reaps the runners this server spawned that have since exited.
        """
        runner.reap_spawned()
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if request_id is None:
            return None
        try:
            result = self._handle(method, params)
        except ToolError as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": ERR_INVALID_PARAMS, "message": str(exc)},
            }
        except MethodNotFound as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": ERR_METHOD_NOT_FOUND, "message": str(exc)},
            }
        except Exception as exc:  # an unanswered id would hang the client
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": ERR_INTERNAL, "message": f"{type(exc).__name__}: {exc}"},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _handle(self, method: str | None, params: dict[str, Any]) -> dict[str, Any]:
        server_meta = {"_meta": {META_SERVER_INFO: SERVER_INFO}}
        if method == "server/discover":
            return {
                "resultType": "complete",
                "supportedVersions": [PROTOCOL_VERSION],
                "capabilities": {"tools": {}, "extensions": {TASKS_EXTENSION: {}}},
                **server_meta,
            }
        if method == "initialize":
            declared = params.get("capabilities") or {}
            self._session_capabilities = declared
            capabilities: dict[str, Any] = {"tools": {}}
            if TASKS_EXTENSION in (declared.get("extensions") or {}):
                capabilities["extensions"] = {TASKS_EXTENSION: {}}
            return {
                "protocolVersion": params.get("protocolVersion"),
                "capabilities": capabilities,
                "serverInfo": SERVER_INFO,
            }
        if method == "ping":
            return {"resultType": "complete"}
        if method == "tools/list":
            return {"resultType": "complete", "tools": TOOLS, **server_meta}
        if method == "tools/call":
            return {**self._call_tool(params), **server_meta}
        if method == "tasks/get":
            return {
                "resultType": "complete",
                **self._task_view(self._task_job(params)),
                **server_meta,
            }
        if method == "tasks/cancel":
            job = self._task_job(params)
            self._run_cli(["cancel", job["job_id"]])
            return {
                "resultType": "complete",
                **self._task_view(self._task_job(params)),
                **server_meta,
            }
        raise MethodNotFound(f"unknown method {method}")

    def _tasks_supported(self, params: dict[str, Any]) -> bool:
        meta = params.get("_meta") or {}
        capabilities = meta.get(META_CAPABILITIES)
        if capabilities is None:
            capabilities = self._session_capabilities
        return TASKS_EXTENSION in ((capabilities or {}).get("extensions") or {})

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if name not in self._tools:
            raise ToolError(f"unknown tool {name!r}")
        arguments = params.get("arguments") or {}
        try:
            validate_arguments(name, arguments)
            return self._tools[name](arguments, self._tasks_supported(params))
        except (ToolError, records.RecordError) as exc:
            return _text_result(f"FAILURE: {exc}", is_error=True)
        except Exception as exc:  # a tool never takes the server down
            return _text_result(
                f"FAILURE: {name} raised {type(exc).__name__}: {exc}", is_error=True
            )

    def _run_cli(self, argv: list[str]) -> tuple[int, list[str]]:
        self._route_stdout()
        with self._router.capture() as buffer:
            code = cli.main(
                argv,
                root=self.root,
                catalog_path=self.catalog_path,
                models_sh_path=self.models_sh_path,
                sessions_dir=self.sessions_dir,
            )
        return code, buffer.getvalue().splitlines()

    def _cli_text(self, argv: list[str]) -> dict[str, Any]:
        code, lines = self._run_cli(argv)
        return _text_result("\n".join(lines), is_error=code != cli.EXIT_OK)

    @staticmethod
    def _job_id(arguments: dict[str, Any]) -> str:
        job_id = arguments["job_id"]
        if not records.JOB_ID_RE.match(job_id):
            raise ToolError(f"not a job id: {job_id!r}")
        return job_id

    def _submit(self, command: str, arguments: dict[str, Any], tasks: bool) -> dict[str, Any]:
        has_text, has_file = "prompt" in arguments, "prompt_file" in arguments
        native = bool(arguments.get("native"))
        if native and (has_text or has_file):
            raise ToolError("a native review takes no prompt or prompt_file")
        if not native and has_text == has_file:
            raise ToolError("give exactly one of prompt and prompt_file")
        scratch = Path(tempfile.mkdtemp(prefix="codex-jobs-mcp-"))
        try:
            prompt_path = None
            if has_text:
                prompt_path = scratch / "prompt.txt"
                prompt_path.write_text(arguments["prompt"])
            elif has_file:
                prompt_path = Path(arguments["prompt_file"])
            # Option values ride as `--flag=value` and operands follow `--`, so an argument that
            # starts with `-` is read as data, never as a flag.
            argv = [command, "--detach"]
            if native:
                argv.append("--native")
            for key, flag in _FLAG_OPTIONS:
                if key in arguments:
                    argv.append(f"{flag}={arguments[key]}")
            argv += [f"--image={image}" for image in arguments.get("images", [])]
            if arguments.get("allow_commit"):
                argv.append("--allow-commit")
            argv.append("--")
            if command in ("resume", "fork"):
                argv.append(arguments["source"])
            if prompt_path is not None:
                argv.append(str(prompt_path))
            code, lines = self._run_cli(argv)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        job_line = next((line for line in lines if line.startswith("JOB: ")), None)
        if code != cli.EXIT_OK or job_line is None:
            return _text_result(
                "\n".join(lines) or f"FAILURE: {command} exited {code} without a job", is_error=True
            )
        job = records.load_job(self.root, job_line.removeprefix("JOB: "))
        if arguments.get("wait"):
            return self._wait_result(job["job_id"], None, tasks)
        return _json_result({"job_id": job["job_id"], "state": job["state"]})

    def _status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if "job_id" in arguments:
            return _json_result(
                reconcile.reconcile_job(
                    self.root, self._job_id(arguments), sessions_dir=self.sessions_dir
                )
            )
        reconcile.reconcile_all(self.root, sessions_dir=self.sessions_dir)
        rows = [
            {
                "job_id": job["job_id"],
                "kind": job["kind"],
                "state": job["state"],
                "model": job["model_used"] or job["model_requested"],
                "updated_at": job["updated_at"],
            }
            for job in records.list_jobs(self.root)
        ]
        return _json_result(rows)

    def _terminal_view(self, job: dict[str, Any]) -> dict[str, Any]:
        """The tool result for a terminal job: its `result.json`, else the state and error.

        An error result unless the job succeeded and Codex reported the task done.
        """
        path = self.root / job["job_id"] / "result.json"
        try:
            payload: dict[str, Any] = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            payload = {"job_id": job["job_id"], "state": job["state"], "error": job["error"]}
            return _json_result(payload, is_error=True)
        return _json_result(
            payload,
            is_error=job["state"] != records.SUCCEEDED or payload["task_succeeded"] is not True,
        )

    def _result(self, arguments: dict[str, Any]) -> dict[str, Any]:
        job = reconcile.reconcile_job(
            self.root, self._job_id(arguments), sessions_dir=self.sessions_dir
        )
        path = self.root / job["job_id"] / "result.json"
        if not path.exists():
            raise ToolError(f"job {job['job_id']} has no result (state {job['state']})")
        return _json_result(json.loads(path.read_text()))

    def _wait(self, arguments: dict[str, Any], tasks: bool) -> dict[str, Any]:
        job_id = self._job_id(arguments)
        return self._wait_result(job_id, arguments.get("max_seconds"), tasks)

    def _wait_result(self, job_id: str, max_seconds: int | None, tasks: bool) -> dict[str, Any]:
        if tasks:
            job = reconcile.reconcile_job(self.root, job_id, sessions_dir=self.sessions_dir)
            return {**self._task_view(job), "resultType": "task"}
        deadline = None if max_seconds is None else time.monotonic() + max_seconds
        while True:
            job = reconcile.reconcile_job(self.root, job_id, sessions_dir=self.sessions_dir)
            if job["state"] in records.TERMINAL_STATES:
                return self._terminal_view(job)
            if self.closing.is_set() or (deadline is not None and time.monotonic() >= deadline):
                return _json_result({"job_id": job_id, "state": job["state"], "terminal": False})
            time.sleep(WAIT_POLL_SECONDS)

    def _task_job(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = params.get("taskId")
        try:
            return reconcile.reconcile_job(self.root, str(task_id), sessions_dir=self.sessions_dir)
        except records.RecordError as exc:
            raise ToolError(f"Failed to retrieve task: Task not found ({exc})") from exc

    @staticmethod
    def _task_fields(job: dict[str, Any]) -> dict[str, Any]:
        state = job["state"]
        status = "working"
        if state == records.CANCELLED:
            status = "cancelled"
        elif state in records.TERMINAL_STATES:
            status = "completed"
        return {
            "taskId": job["job_id"],
            "status": status,
            "createdAt": job["created_at"],
            "lastUpdatedAt": job["updated_at"],
            "ttlMs": TASK_TTL_MS,
            "pollIntervalMs": TASK_POLL_MS,
        }

    def _task_view(self, job: dict[str, Any]) -> dict[str, Any]:
        view = self._task_fields(job)
        if view["status"] == "completed":
            view["result"] = {
                key: value for key, value in self._terminal_view(job).items() if key != "resultType"
            }
        return view


def serve_stdio(server: Server, stdin: TextIO, stdout: TextIO) -> None:
    """Read one JSON-RPC message per line until EOF; answer each request on its own thread.

    A blocking `codex_wait` from one client (a subagent) must not stop the next `tasks/get` from
    another, so requests never queue behind each other.
    """
    write_lock = threading.Lock()
    threads: list[threading.Thread] = []

    def send(payload: dict[str, Any]) -> None:
        with write_lock:
            stdout.write(json.dumps(payload) + "\n")
            stdout.flush()

    def answer(message: dict[str, Any]) -> None:
        response = server.dispatch(message)
        if response is not None:
            send(response)

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": ERR_PARSE, "message": "parse error"},
                }
            )
            continue
        if not isinstance(message, dict):
            send(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": ERR_PARSE, "message": "expected a JSON object"},
                }
            )
            continue
        thread = threading.Thread(target=answer, args=(message,), daemon=True)
        thread.start()
        threads.append(thread)
    server.closing.set()
    for thread in threads:
        thread.join(SHUTDOWN_JOIN_SECONDS)


def serve(*, root: Path, catalog_path: Path, models_sh_path: Path, sessions_dir: Path) -> int:
    protocol_out = sys.stdout
    with Server(
        root=root,
        catalog_path=catalog_path,
        models_sh_path=models_sh_path,
        sessions_dir=sessions_dir,
    ) as server:
        serve_stdio(server, sys.stdin, protocol_out)
    return cli.EXIT_OK
