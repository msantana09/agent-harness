"""Client and launcher for a per-job `codex app-server` on a unix socket.

The server speaks WebSocket over the socket: an HTTP/1.1 Upgrade, then one JSON-RPC message
per text frame, client frames masked. Messages carry no "jsonrpc" key. `WebSocketConnection`
is the framing, `AppServerClient` the request/notification layer on top of it, and
`launch_app_server` / `stop_app_server` own the server process (it keeps running with zero
clients, so whoever starts it stops it).
"""

import base64
import hashlib
import itertools
import json
import os
import queue
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The server's x-codex-websocket-max-unfragmented-message-bytes.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
DEFAULT_STARTUP_TIMEOUT = 30.0
DEFAULT_STOP_GRACE = 5.0
TOKEN_COMMAND_ENV = "CODEX_JOBS_GH_TOKEN_COMMAND"

_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HANDSHAKE_BYTES = 64 * 1024
_SUN_PATH_LIMIT = 104  # macOS sockaddr_un.sun_path, including the NUL
_ENV_PASSTHROUGH = ("USER", "LOGNAME", "TMPDIR", "LANG", "TERM", "SHELL")
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INTERNAL_ERROR = -32603


class ConnectionClosed(Exception):
    """The peer closed the connection (close frame or EOF). `code` is the close code, if any."""

    def __init__(self, code: int | None = None, reason: str = ""):
        super().__init__(f"connection closed (code={code}): {reason}")
        self.code = code
        self.reason = reason


class HandshakeError(ConnectionError):
    """The server did not complete the WebSocket upgrade."""


class ProtocolError(Exception):
    """The peer sent a frame or message that violates the WebSocket or JSON-RPC protocol."""


class MessageTooLarge(ProtocolError):
    """A message exceeded the connection's size bound."""


class RpcError(Exception):
    """A JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class AppServerStartError(Exception):
    """`codex app-server` did not come up listening."""


def _apply_mask(data: bytes, mask: bytes) -> bytes:
    if not data:
        return b""
    key = (mask * (len(data) // 4 + 1))[: len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(len(data), "big")


def _connectable_path(path: str | os.PathLike[str]) -> str:
    """`path`, or the symlink target when `path` is too long for sun_path (Codex symlinks long
    ones)."""
    text = os.fspath(path)
    if len(os.fsencode(text)) < _SUN_PATH_LIMIT:
        return text
    resolved = os.path.realpath(text)
    return resolved if len(os.fsencode(resolved)) < _SUN_PATH_LIMIT else text


class WebSocketConnection:
    """Client side of a WebSocket on a connected stream socket.

    `send_text` is thread-safe. `recv_text` is for a single reader thread: it answers pings,
    reassembles fragments, raises `ConnectionClosed` on a close frame or EOF and
    `ProtocolError` / `MessageTooLarge` on violations (after sending a close frame).
    """

    def __init__(
        self,
        sock: socket.socket,
        *,
        leftover: bytes = b"",
        max_message_bytes: int = MAX_MESSAGE_BYTES,
    ):
        self._sock = sock
        self._buf = bytearray(leftover)
        self._max = max_message_bytes
        self._send_lock = threading.Lock()
        self._closed = False

    @classmethod
    def connect(
        cls,
        path: str | os.PathLike[str],
        *,
        timeout: float = 10.0,
        max_message_bytes: int = MAX_MESSAGE_BYTES,
    ) -> WebSocketConnection:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect(_connectable_path(path))
            key = base64.b64encode(os.urandom(16))
            sock.sendall(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Key: " + key + b"\r\nSec-WebSocket-Version: 13\r\n\r\n"
            )
            head, leftover = _read_http_head(sock)
            _check_upgrade(head, key)
            sock.settimeout(None)
        except BaseException:
            sock.close()
            raise
        return cls(sock, leftover=leftover, max_message_bytes=max_message_bytes)

    def send_text(self, text: str) -> None:
        data = text.encode()
        if len(data) > self._max:
            raise ValueError(f"message of {len(data)} bytes exceeds the {self._max} byte bound")
        self._send_frame(0x1, data)

    def recv_text(self) -> str:
        parts: list[bytes] = []
        total = 0
        in_message = False
        while True:
            b0, b1 = self._recv_exact(2)
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            length = b1 & 0x7F
            if b0 & 0x70:
                self._fail(1002, "reserved bits set")
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            if opcode >= 0x8:
                if not fin or length > 125:
                    self._fail(1002, "malformed control frame")
            elif total + length > self._max:
                self._fail(1009, f"message exceeds {self._max} bytes", MessageTooLarge)
            mask = self._recv_exact(4) if b1 & 0x80 else None
            payload = self._recv_exact(length)
            if mask:
                payload = _apply_mask(payload, mask)
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                self._on_close_frame(payload)
            if opcode == 0x1 and not in_message:
                in_message = True
            elif opcode == 0x0 and in_message:
                pass
            elif opcode == 0x2:
                self._fail(1003, "binary frames are not supported")
            else:
                self._fail(1002, f"unexpected opcode {opcode:#x}")
            parts.append(payload)
            total += length
            if fin:
                try:
                    return b"".join(parts).decode()
                except UnicodeDecodeError:
                    self._fail(1007, "text message is not valid UTF-8")

    def close(self) -> None:
        with self._send_lock:
            already = self._closed
            self._closed = True
        if already:
            return
        try:
            self._sock.sendall(self._frame(0x8, struct.pack(">H", 1000)))
        except OSError:
            pass
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()

    def _frame(self, opcode: int, payload: bytes) -> bytes:
        n = len(payload)
        header = bytearray([0x80 | opcode])
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        return bytes(header) + mask + _apply_mask(payload, mask)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        frame = self._frame(opcode, payload)
        with self._send_lock:
            if self._closed:
                raise ConnectionClosed(None, "connection already closed")
            try:
                self._sock.sendall(frame)
            except OSError as exc:
                raise ConnectionClosed(None, f"send failed: {exc}") from exc

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            try:
                chunk = self._sock.recv(1 << 20)
            except OSError as exc:
                raise ConnectionClosed(None, f"receive failed: {exc}") from exc
            if not chunk:
                raise ConnectionClosed(None, "connection closed by peer")
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _on_close_frame(self, payload: bytes) -> None:
        code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else None
        reason = payload[2:].decode(errors="replace")
        try:
            self._send_frame(0x8, payload[:2])
        except ConnectionClosed:
            pass
        self.close()
        raise ConnectionClosed(code, reason)

    def _fail(self, code: int, reason: str, exc_type: type[ProtocolError] = ProtocolError) -> None:
        try:
            self._send_frame(0x8, struct.pack(">H", code) + reason.encode()[:100])
        except ConnectionClosed:
            pass
        self.close()
        raise exc_type(reason)


def _read_http_head(sock: socket.socket) -> tuple[bytes, bytes]:
    buf = b""
    while b"\r\n\r\n" not in buf:
        if len(buf) > _MAX_HANDSHAKE_BYTES:
            raise HandshakeError("handshake response header exceeds the size bound")
        chunk = sock.recv(4096)
        if not chunk:
            raise HandshakeError(f"connection closed during handshake: {buf!r}")
        buf += chunk
    head, leftover = buf.split(b"\r\n\r\n", 1)
    return head, leftover


def _check_upgrade(head: bytes, key: bytes) -> None:
    lines = head.decode("latin-1").split("\r\n")
    status = lines[0].split(" ", 2)
    if len(status) < 2 or status[1] != "101":
        raise HandshakeError(f"expected 101 Switching Protocols, got {lines[0]!r}")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    expected = base64.b64encode(hashlib.sha1(key + _WS_GUID).digest()).decode()
    if headers.get("sec-websocket-accept") != expected:
        raise HandshakeError(
            f"Sec-WebSocket-Accept mismatch: got {headers.get('sec-websocket-accept')!r}"
        )


_CLOSED = object()


class AppServerClient:
    """JSON-RPC client for one connection to an app-server socket.

    Responses are routed to `call` by request id. Notifications queue up for
    `next_notification` / `iter_notifications`. A message with `id` and `method` is a server
    request: `request_handler(method, params)` runs on its own thread and its return value is
    sent as the `result` (raise `RpcError` to send an error); with no handler the server gets
    -32601. When the connection ends, pending calls raise `ConnectionClosed` and the
    notification stream ends.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        client_name: str = "codex-jobs",
        client_version: str = "0.1.0",
        request_handler: Callable[[str, dict[str, Any]], Any] | None = None,
        connect_timeout: float = 10.0,
        max_message_bytes: int = MAX_MESSAGE_BYTES,
    ):
        self._client_info = {"name": client_name, "version": client_version}
        self._request_handler = request_handler
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._pending: dict[Any, queue.Queue[Any]] = {}
        self._notifications: queue.Queue[Any] = queue.Queue()
        self._close_error: ConnectionClosed | None = None
        self._conn = WebSocketConnection.connect(
            path, timeout=connect_timeout, max_message_bytes=max_message_bytes
        )
        self._reader = threading.Thread(target=self._pump, name="appserver-reader", daemon=True)
        self._reader.start()

    def __enter__(self) -> AppServerClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def closed(self) -> bool:
        return self._close_error is not None

    @property
    def close_reason(self) -> ConnectionClosed | None:
        return self._close_error

    def initialize(self, timeout: float = 30.0) -> Any:
        """`initialize` then the `initialized` notification; returns the initialize result."""
        result = self.call(
            "initialize",
            {"clientInfo": self._client_info, "capabilities": {"experimentalApi": True}},
            timeout,
        )
        self.notify("initialized")
        return result

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 60.0) -> Any:
        """Send a request and return its `result`; raise `RpcError`, `TimeoutError` or
        `ConnectionClosed`."""
        request_id = next(self._ids)
        answer: queue.Queue[Any] = queue.Queue(maxsize=1)
        with self._lock:
            if self._close_error is not None:
                raise self._close_error
            self._pending[request_id] = answer
        try:
            self._send(
                {"id": request_id, "method": method, "params": {} if params is None else params}
            )
            try:
                reply = answer.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError(f"{method} got no response within {timeout}s") from None
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
        if isinstance(reply, Exception):
            raise reply
        if "error" in reply:
            error = reply["error"]
            raise RpcError(error.get("code"), error.get("message"), error.get("data"))
        return reply["result"]

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"method": method, "params": {} if params is None else params})

    def respond(self, request_id: Any, result: Any) -> None:
        self._send({"id": request_id, "result": result})

    def respond_error(self, request_id: Any, code: int, message: str, data: Any = None) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self._send({"id": request_id, "error": error})

    def next_notification(self, timeout: float | None = None) -> dict[str, Any] | None:
        """The next notification, None on timeout; raises `ConnectionClosed` once the stream has
        ended."""
        try:
            item = self._notifications.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is _CLOSED:
            self._notifications.put(_CLOSED)  # keep waking other consumers
            raise self._close_error or ConnectionClosed(None, "connection closed")
        return item

    def iter_notifications(self) -> Iterator[dict[str, Any]]:
        """Yield notifications until the connection ends."""
        while True:
            try:
                note = self.next_notification()
            except ConnectionClosed:
                return
            if note is not None:
                yield note

    def close(self) -> None:
        self._conn.close()
        self._reader.join(timeout=2.0)

    def _send(self, message: dict[str, Any]) -> None:
        self._conn.send_text(json.dumps(message))

    def _pump(self) -> None:
        error: ConnectionClosed
        try:
            while True:
                try:
                    message = json.loads(self._conn.recv_text())
                except json.JSONDecodeError as exc:
                    raise ProtocolError(f"invalid JSON: {exc}") from exc
                self._dispatch(message)
        except ConnectionClosed as exc:
            error = exc
        except ProtocolError as exc:
            error = ConnectionClosed(None, f"protocol error: {exc}")
        except Exception as exc:  # the reader must wake every waiter whatever kills it
            error = ConnectionClosed(None, f"reader failed: {exc!r}")
        with self._lock:
            self._close_error = error
            waiting = list(self._pending.values())
            self._pending.clear()
        for answer in waiting:
            answer.put(error)
        self._notifications.put(_CLOSED)

    def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict):
            raise ProtocolError("JSON-RPC message is not an object")
        method = message.get("method")
        if "id" in message and method is None:
            if "result" not in message and "error" not in message:
                raise ProtocolError("response has neither result nor error")
            with self._lock:
                answer = self._pending.pop(message["id"], None)
            if answer is not None:
                answer.put(message)
        elif "id" in message:
            threading.Thread(
                target=self._answer_server_request,
                args=(message["id"], method, message.get("params") or {}),
                name="appserver-request",
                daemon=True,
            ).start()
        elif method is not None:
            self._notifications.put(message)
        else:
            raise ProtocolError("message has neither id nor method")

    def _answer_server_request(self, request_id: Any, method: str, params: dict[str, Any]) -> None:
        try:
            if self._request_handler is None:
                self.respond_error(
                    request_id, _JSONRPC_METHOD_NOT_FOUND, f"no handler for server request {method}"
                )
                return
            try:
                result = self._request_handler(method, params)
            except RpcError as exc:
                self.respond_error(request_id, exc.code, exc.message, exc.data)
                return
            except Exception as exc:  # an unanswered server request would hang the turn
                self.respond_error(request_id, _JSONRPC_INTERNAL_ERROR, f"handler failed: {exc!r}")
                return
            self.respond(request_id, result)
        except ConnectionClosed:
            pass


@dataclass(frozen=True)
class AppServer:
    pid: int
    pgid: int
    socket_path: Path


_launched: dict[int, subprocess.Popen[bytes]] = {}
_launched_lock = threading.Lock()


def _origin_owner(repo_dir: Path) -> str:
    """Lowercased owner of the github.com origin remote of `repo_dir`, or ''."""
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    url = result.stdout.strip() if result.returncode == 0 else ""
    if url.startswith("git@github.com:"):
        path = url[len("git@github.com:") :]
    elif "://github.com/" in url or "@github.com/" in url:
        path = url.split("github.com/", 1)[1]
    else:
        return ""
    return path.split("/", 1)[0].lower()


def resolve_gh_token(repo_dir: Path) -> str | None:
    """The GH_TOKEN a job in `repo_dir` runs with, or None for no token.

    With `CODEX_JOBS_GH_TOKEN_COMMAND` set, it runs `<command> <repo_dir>`: exit 0 prints the
    token on stdout (empty output means no token), and a nonzero exit refuses the job with the
    command's stderr, so a repo the command will not serve never starts. Unset, the token is the
    one of the gh account named after the lowercased owner of `repo_dir`'s github.com origin
    (`gh auth token -u <owner>`, gh found on PATH), else None. Either way the lookup runs outside
    any sandbox, in the caller's environment minus GH_TOKEN and GITHUB_TOKEN.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
    command = os.environ.get(TOKEN_COMMAND_ENV)
    if command:
        try:
            result = subprocess.run(
                [command, str(repo_dir)], capture_output=True, text=True, env=env, timeout=30
            )
        except OSError as exc:
            raise AppServerStartError(f"{TOKEN_COMMAND_ENV} {command} did not run: {exc}") from exc
        if result.returncode != 0:
            raise AppServerStartError(
                f"{TOKEN_COMMAND_ENV} refused {repo_dir} (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )
        return result.stdout.strip() or None
    owner = _origin_owner(repo_dir)
    gh = shutil.which("gh")
    if not owner or gh is None:
        return None
    result = subprocess.run(
        [gh, "auth", "token", "-u", owner], capture_output=True, text=True, env=env, timeout=30
    )
    token = result.stdout.strip()
    return token if result.returncode == 0 and token else None


def build_app_server_env(gh_token: str | None) -> dict[str, str]:
    """The app-server's whole environment: HOME, the caller's PATH and a few basics, plus GH_TOKEN.

    `shell_environment_policy.ignore_default_excludes=true` (set when there is a token) passes every
    KEY/TOKEN/SECRET variable through to commands Codex runs, so nothing else of the caller's is
    inherited.
    """
    env = {"HOME": os.environ["HOME"], "PATH": os.environ["PATH"]}
    for name in _ENV_PASSTHROUGH:
        if name in os.environ:
            env[name] = os.environ[name]
    if gh_token:
        env["GH_TOKEN"] = gh_token
    return env


def _log_tail(log_path: Path, lines: int = 20) -> str:
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 8192))
            text = f.read().decode(errors="replace")
    except OSError as exc:
        return f"(no log: {exc})"
    return "\n".join(text.splitlines()[-lines:])


def _wait_until_listening(
    proc: subprocess.Popen[bytes], socket_path: Path, timeout: float, log_path: Path
) -> None:
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while True:
        returncode = proc.poll()
        if returncode is not None:
            raise AppServerStartError(
                f"codex app-server exited with status {returncode} before "
                f"listening\n{_log_tail(log_path)}"
            )
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(1.0)
            probe.connect(_connectable_path(socket_path))
            return
        except OSError as exc:
            last_error = exc
        finally:
            probe.close()
        if time.monotonic() >= deadline:
            raise AppServerStartError(
                f"codex app-server not accepting connections on {socket_path} after "
                f"{timeout}s ({last_error})\n{_log_tail(log_path)}"
            )
        time.sleep(0.05)


def launch_app_server(
    job_dir: Path,
    codex_bin: str | os.PathLike[str],
    repo_dir: Path,
    extra_config: Sequence[str] = (),
    *,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    token_resolver: Callable[[Path], str | None] = resolve_gh_token,
    on_spawn: Callable[[int, int], None] | None = None,
) -> AppServer:
    """Start `codex app-server --listen unix://<job_dir>/app.sock` in its own session and wait for
    the socket.

    Commands always run in a non-login shell (`-c allow_login_shell=false`). `extra_config` is a
    list of `key=value` strings, each passed as `-c key=value`. The environment is
    `build_app_server_env`; `token_resolver(repo_dir)` supplies GH_TOKEN, and when it returns one
    the launcher also sets `shell_environment_policy.ignore_default_excludes=true`. Output goes to
    `<job_dir>/app-server.log`. `on_spawn(pid, pgid)` is called as soon as the process exists,
    before the wait for its socket, so a caller can record what to stop if it dies during that wait.
    On failure to listen, or when `on_spawn` raises, the process group is stopped; a failure to
    listen raises `AppServerStartError` with the log tail.
    """
    job_dir = Path(job_dir)
    socket_path = job_dir / "app.sock"
    log_path = job_dir / "app-server.log"
    token = token_resolver(Path(repo_dir))
    argv = [
        str(codex_bin),
        "app-server",
        "--listen",
        f"unix://{socket_path}",
        "-c",
        "allow_login_shell=false",
    ]
    if token:
        argv += ["-c", "shell_environment_policy.ignore_default_excludes=true"]
    for item in extra_config:
        argv += ["-c", item]
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            argv,
            cwd=job_dir,
            env=build_app_server_env(token),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pgid = proc.pid  # setsid made the child its own session and group leader
    with _launched_lock:
        _launched[pgid] = proc
    try:
        if on_spawn is not None:
            on_spawn(proc.pid, pgid)
        _wait_until_listening(proc, socket_path, startup_timeout, log_path)
    except BaseException:
        stop_app_server(pgid, grace=1.0)
        raise
    return AppServer(pid=proc.pid, pgid=pgid, socket_path=socket_path)


def _group_alive(pgid: int) -> bool:
    with _launched_lock:
        proc = _launched.get(pgid)
        if proc is not None and proc.poll() is not None:
            del _launched[pgid]  # reaped the leader; its group may still hold other members
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def stop_app_server(pgid: int, *, grace: float = DEFAULT_STOP_GRACE) -> bool:
    """SIGTERM the process group, SIGKILL it after `grace` seconds; True once the group is gone."""
    if pgid <= 1 or pgid == os.getpgrp():
        raise ValueError(f"refusing to signal process group {pgid}")
    if not _group_alive(pgid):
        return True
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return True
        time.sleep(0.02)
    _signal_group(pgid, signal.SIGKILL)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return True
        time.sleep(0.02)
    return False
