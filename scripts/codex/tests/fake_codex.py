#!/usr/bin/env python3
"""Fake `codex` for the codex_jobs tests.

Stands in for the real binary: `fake_codex.py app-server --listen unix://PATH [-c key=value ...]`
serves WebSocket framing on a unix socket (its own implementation, independent of
`codex_jobs.appserver`) and plays a scripted conversation. `--version` prints a version line.
Tests put an executable wrapper named `codex` first on PATH (see `FakeCodex` in conftest.py);
it exports the two environment variables below, because the launcher gives the server a
minimal environment.

Environment (both required):
  FAKE_CODEX_SCRIPT   path of the JSON script described below
  FAKE_CODEX_LOG      path of a JSON-lines log the fake appends to

Log records (one JSON object per line, all with "kind" and "t"):
  start     argv (list), env (dict), cwd, pid, pgid          first record, before anything else
  child     pid                                              the "spawn_child" process
  listen    path, bound                                      bound differs when the path was too long
  client    client (n), event ("open"|"close")      n counts clients whose handshake succeeded, from 1
  recv      client, msg (parsed JSON) or raw (text that was not JSON)
  send      client, msg
  pong      client, payload                                  a pong received from a client
  violation client, detail                                   e.g. an unmasked client frame

Script (JSON object; every key optional):
  thread_id           id of threads from thread/start            default "thr-fake-1"
  fork_thread_id      id of the thread thread/fork creates       default "<source id>-fork"
  responses           {method: result}  canned results; wins over the built-ins, so
                      "account/rateLimits/read" and any other method carry arbitrary JSON
                      (use the real v2 names, or snake case for a negative canary)
  errors              {method: {"code", "message", "data"?}}     canned JSON-RPC errors, win over all
  delays              {method: seconds}  the answer to that request comes this long after it arrived
  drop_reply          [method...]  the request takes effect (a turn/start starts its turn) but the
                      answer is never sent: the requesting client's connection is shut down instead
  turn                scripted turn played on turn/start (see below); without it turn/start
                      is answered from "responses" or -32601
  wire                {"fragment_parts": N, "ping_before_each_message": bool}
                      every server message is split into N frames; a ping goes before each
                      message (and between fragments); clients' pongs are logged
  handshake           "bad_accept" (wrong Sec-WebSocket-Accept) | "refuse" (HTTP 400)
  startup_fail        message written to stderr, then exit 3 before listening
  startup_delay       seconds to sleep before listening
  spawn_child         true: start a `sleep 300` child in the same process group
  ignore_sigterm      true: SIGTERM is ignored (SIGKILL escalation tests)
  notify_on           {method: {"method", "params"}}  a notification sent to the requesting client right
                      after the answer to each request of that method (the real server sends one per
                      thread/read with includeTurns)
  page_limit          largest page thread/turns/list and thread/items/list return, whatever "limit" asks
  history             {thread_id: [turn_id...]}  completed turns a thread/resume of that thread starts with

Built-in methods (a "responses" entry replaces the built-in result):
  initialize          {"userAgent", "codexHome", "platformFamily", "platformOs"}
  thread/start        {"thread": {"id": thread_id}, "model": params.model, "reasoningEffort":
                      params.config.model_reasoning_effort (null without it)}
  thread/resume       {"thread": {"id": params.threadId}, "model": params.model}; an unknown thread
                      is registered with its "history" turns, else none (the real server loads it
                      from its rollout)
  thread/fork         {"thread": {"id": fork_thread_id}, "model": params.model}; the new thread starts empty
  thread/read         {"thread": {"id", "status", "turns": [...]}} from the turns played so far, the
                      running turn last with status "inProgress" (turns only when params.includeTurns
                      is true, which also sends the requester a deprecationNotice as the real server
                      does); status is {"type": "active"} during a turn, else {"type": "idle"};
                      unknown thread -> -32600
  thread/turns/list   {"data": [turn...], "nextCursor", "backwardsCursor"}: the thread's turns, the running
                      one last in time, newest first unless params.sortDirection is "asc". params.itemsView
                      "notLoaded" gives `items: []`, "summary" (default) leaves out the agent messages,
                      "full" has every item. params.limit and params.cursor page it (the cursor is opaque
                      to clients); unknown thread -> -32600
  thread/items/list   {"data": [{"turnId", "item", "startedAtMs", "completedAtMs"}...], "nextCursor",
                      "backwardsCursor"}: the thread's finished items, oldest first unless params.sortDirection
                      is "desc"; params.turnId keeps one turn's items (an unknown turn gives no items);
                      params.limit and params.cursor page it; unknown thread -> -32600
  account/rateLimits/read   {"ordinaryUsageAllowed": true, "rateLimits": {"primary": {...}, ...}}
  turn/start          {"turn": {"id", "status": "inProgress", ...}} then plays the turn
  review/start        {"turn": {"id", "status": "inProgress", ...}, "reviewThreadId": params.threadId}
                      then plays the scripted turn as a native review (see below); params must be exactly
                      {threadId, target[, delivery]}, else -32600; needs a "turn" script key
  turn/steer          {"turnId"}; expectedTurnId must be the active turn, else -32600
  turn/interrupt      {} and ends the active turn with status "interrupted"
  fake/echo           the params, unchanged (any size)
  fake/close          close frame with params.code / params.reason, then the connection ends
  fake/exit           the whole process exits abruptly (os._exit(1))
  fake/never          a request that is never answered
  anything else       -32601 "Method not found"
Messages never carry a "jsonrpc" key. As measured on the real server (codex 0.167), turn/started,
item/* and turn/completed go only to the client that sent turn/start (a second client sees none of
them); when a turn ends every initialized client gets thread/status/changed {status: {type: "idle"}}.
A client that disconnects mid-turn does not stop the turn: it runs to its end and a later client's
thread/read returns the result. A server request goes to the client that sent turn/start. An abrupt
exit (fake/exit, exit_at) leaves the socket file in place, so a later connect gets ECONNREFUSED.

Turn (the "turn" script key):
  turn_id             default "turn-fake-<n>"
  applied_effort      the effort thread/settings/updated reports before turn/started (default: the
                      turn/start effort); a native review sends no thread/settings/updated, as the real server does
  events              [{"method", "params"}] extra notifications sent after turn/started
  approvals           [{"method", "params"}] server requests sent one at a time to the turn's
                      client, each awaited (threadId, turnId, itemId, startedAtMs are filled in)
  hang                true: after the approvals, wait for turn/interrupt (no scripted end)
  final_delay         seconds to wait before the final answer; a turn/interrupt ends the wait
  final_answer        text; sent as item/completed {item: agentMessage, phase "final_answer"}. A native
                      review (review/start) sends enteredReviewMode and exitedReviewMode items around it
                      and its agentMessage has phase null, as the real server's does
  status              "completed" (default) | "failed" | ...  turn/completed status
  error               turn/completed turn.error                default null
  exit_at             "after_turn_start_response" | "after_turn_started" | "after_approval" |
                      "after_final_answer" | "after_turn_completed": os._exit(1) at that point
  approval_timeout    seconds to wait for an approval answer   default 30
An interrupted turn sends turn/completed with status "interrupted" and no final answer.
"""

import base64
import hashlib
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_UNFRAGMENTED = 16 * 1024 * 1024
SUN_PATH_LIMIT = 104
NOT_FOUND = -32601
BAD_REQUEST = -32600
DEPRECATED_HYDRATION = (
    "Full-history hydration is deprecated for paginated threads; omit `includeTurns` or set it to `false`, "
    "then page with `thread/turns/list` and `thread/items/list`."
)


def now_ms() -> int:
    return int(time.time() * 1000)


def apply_mask(data: bytes, mask: bytes) -> bytes:
    if not data:
        return b""
    key = (mask * (len(data) // 4 + 1))[: len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(len(data), "big")


class Log:
    def __init__(self, path: str):
        self._file = open(path, "a")
        self._lock = threading.Lock()

    def write(self, kind: str, **fields: Any) -> None:
        with self._lock:
            self._file.write(json.dumps({"kind": kind, "t": time.time(), **fields}) + "\n")
            self._file.flush()


class Client:
    def __init__(self, server: Server, conn: socket.socket):
        self.server = server
        self.conn = conn
        self.number = 0  # assigned once the handshake succeeded
        self.rfile = conn.makefile("rb")
        self.message_lock = threading.Lock()
        self.frame_lock = threading.Lock()
        self.initialized = False

    def read_exact(self, n: int) -> bytes:
        data = self.rfile.read(n)
        if len(data) < n:
            raise EOFError
        return data

    def send_frame(self, opcode: int, payload: bytes, fin: bool = True) -> None:
        n = len(payload)
        header = bytearray([(0x80 if fin else 0) | opcode])
        if n < 126:
            header.append(n)
        elif n < 65536:
            header += bytes([126]) + struct.pack(">H", n)
        else:
            header += bytes([127]) + struct.pack(">Q", n)
        with self.frame_lock:
            self.conn.sendall(bytes(header) + payload)

    def send_json(self, message: dict[str, Any]) -> None:
        self.server.log.write("send", client=self.number, msg=message)
        data = json.dumps(message).encode()
        wire = self.server.script.get("wire", {})
        ping = wire.get("ping_before_each_message", False)
        parts = max(1, int(wire.get("fragment_parts", 1)))
        step = max(1, -(-len(data) // parts))
        pieces = [data[i : i + step] for i in range(0, len(data), step)] or [b""]
        with self.message_lock:
            if ping:
                self.send_frame(0x9, b"hb")
            for i, piece in enumerate(pieces):
                if ping and i > 0:
                    self.send_frame(0x9, b"hb")
                last = i == len(pieces) - 1
                self.send_frame(0x1 if i == 0 else 0x0, piece, fin=last)

    def read_message(self) -> str | None:
        """The next client text message, or None when the client closed or broke the protocol."""
        parts: list[bytes] = []
        while True:
            b0, b1 = self.read_exact(2)
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self.read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self.read_exact(8))[0]
            if not b1 & 0x80:
                self.server.log.write(
                    "violation", client=self.number, detail="unmasked client frame"
                )
                self.send_frame(0x8, struct.pack(">H", 1002))
                return None
            mask = self.read_exact(4)
            payload = apply_mask(self.read_exact(length), mask)
            if opcode == 0x8:
                self.send_frame(0x8, payload[:2])
                return None
            if opcode == 0x9:
                self.send_frame(0xA, payload)
            elif opcode == 0xA:
                self.server.log.write(
                    "pong", client=self.number, payload=payload.decode(errors="replace")
                )
            elif opcode in (0x0, 0x1):
                parts.append(payload)
                if fin:
                    return b"".join(parts).decode()
            else:
                self.server.log.write("violation", client=self.number, detail=f"opcode {opcode:#x}")
                return None

    def handshake(self) -> bool:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.conn.recv(4096)
            if not chunk:
                return False
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        headers = {}
        for line in head.decode("latin-1").split("\r\n")[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        mode = self.server.script.get("handshake")
        if mode == "refuse":
            self.conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            return False
        key = headers["sec-websocket-key"].encode()
        accept = base64.b64encode(hashlib.sha1(key + GUID).digest()).decode()
        if mode == "bad_accept":
            accept = base64.b64encode(b"wrong").decode()
        self.conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                f"x-codex-websocket-max-unfragmented-message-bytes: {MAX_UNFRAGMENTED}\r\n\r\n"
            ).encode()
        )
        # Bytes after the header (none from a well-behaved client) go back in front of the stream.
        if rest:
            self.rfile = _Prefixed(rest, self.rfile)
        return True


class _Prefixed:
    def __init__(self, prefix: bytes, rfile: Any):
        self.prefix = prefix
        self.rfile = rfile

    def read(self, n: int) -> bytes:
        take, self.prefix = self.prefix[:n], self.prefix[n:]
        return take + (self.rfile.read(n - len(take)) if len(take) < n else b"")


class Turn:
    def __init__(self, thread_id: str, turn_id: str, client: Client):
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.client = client
        self.interrupted = threading.Event()
        self.review = False
        self.effort: str | None = None


class Server:
    def __init__(self, script: dict[str, Any], log: Log, cleanup: Any):
        self.script = script
        self.log = log
        self.cleanup = cleanup
        self.lock = threading.Lock()
        self.clients: list[Client] = []
        self.client_count = 0
        self.threads: dict[str, list[dict[str, Any]]] = {}
        self.active: Turn | None = None
        self.turn_count = 0
        self.server_request_ids = iter(range(1000, 10**9))
        self.waiting: dict[int, tuple[threading.Event, list[Any]]] = {}

    def die(self) -> None:
        os._exit(
            1
        )  # no cleanup: a killed Codex leaves its socket file behind (connect then gets ECONNREFUSED)

    def emit(self, targets: list[Client], message: dict[str, Any]) -> None:
        message = {**message, "emittedAtMs": now_ms()}
        for client in targets:
            try:
                client.send_json(message)
            except OSError:
                pass  # a client that died mid-turn: the turn runs on, as on the real server

    def emit_to_turn_client(self, turn: Turn, message: dict[str, Any]) -> None:
        self.emit([turn.client], message)

    def emit_to_all(self, message: dict[str, Any]) -> None:
        with self.lock:
            targets = [c for c in self.clients if c.initialized]
        self.emit(targets, message)

    def serve(self, listener: socket.socket) -> None:
        while True:
            conn, _ = listener.accept()
            client = Client(self, conn)
            threading.Thread(target=self.client_loop, args=(client,), daemon=True).start()

    def client_loop(self, client: Client) -> None:
        try:
            if not client.handshake():
                return  # a probe that connects and leaves, or a refused handshake
            with self.lock:
                self.client_count += 1
                client.number = self.client_count
                self.clients.append(client)
            self.log.write("client", client=client.number, event="open")
            while True:
                text = client.read_message()
                if text is None:
                    break
                self.handle_text(client, text)
        except (EOFError, OSError):
            pass
        finally:
            with self.lock:
                if client in self.clients:
                    self.clients.remove(client)
            if client.number:
                self.log.write("client", client=client.number, event="close")
            try:
                client.conn.close()
            except OSError:
                pass

    def handle_text(self, client: Client, text: str) -> None:
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            self.log.write("recv", client=client.number, raw=text)
            return
        self.log.write("recv", client=client.number, msg=message)
        method = message.get("method")
        request_id = message.get("id")
        if method is None:
            with self.lock:
                waiter = self.waiting.get(request_id)
            if waiter is not None:
                waiter[1].append(message)
                waiter[0].set()
            return
        if request_id is None or method == "fake/never":
            return  # a notification such as `initialized`; fake/never is a request nobody answers
        params = message.get("params") or {}
        script = self.script
        if method in script.get("errors", {}):
            client.send_json({"id": request_id, "error": script["errors"][method]})
            return
        time.sleep(float(script.get("delays", {}).get(method, 0)))
        try:
            result = self.result_for(client, method, params)
        except Failure as failure:
            client.send_json({"id": request_id, "error": failure.error})
            return
        if method == "initialize":
            client.initialized = True
        if method == "fake/close":
            client.send_frame(
                0x8,
                struct.pack(">H", int(params.get("code", 1000)))
                + str(params.get("reason", "")).encode(),
            )
            client.conn.shutdown(socket.SHUT_RDWR)
            return
        if method in script.get("drop_reply", []):
            client.conn.shutdown(socket.SHUT_RDWR)
        else:
            client.send_json({"id": request_id, "result": result})
        notice = script.get("notify_on", {}).get(method)
        if method == "thread/read" and params.get("includeTurns"):
            notice = notice or {
                "method": "deprecationNotice",
                "params": {"summary": DEPRECATED_HYDRATION, "details": None},
            }
        if notice is not None:
            self.emit([client], notice)
        if method in ("turn/start", "review/start") and "turn" in script:
            threading.Thread(target=self.play_turn, args=(self.active,), daemon=True).start()

    def result_for(self, client: Client, method: str, params: dict[str, Any]) -> Any:
        script = self.script
        if method == "fake/exit":
            self.die()
        if method == "fake/close":
            return None
        if method == "fake/echo":
            return params
        if method == "turn/start" and "turn" in script:
            return self.start_turn(client, params)
        if method == "review/start" and "turn" in script:
            return self.start_review(client, params)
        if method in script.get("responses", {}):
            return script["responses"][method]
        if method == "initialize":
            return {
                "userAgent": "fake-codex/0.0.0",
                "codexHome": os.path.expanduser("~/.codex"),
                "platformFamily": "unix",
                "platformOs": "macos",
            }
        if method == "thread/start":
            thread_id = script.get("thread_id", "thr-fake-1")
            with self.lock:
                self.threads.setdefault(thread_id, [])
            return {
                "thread": {"id": thread_id},
                "model": params.get("model"),
                "reasoningEffort": (params.get("config") or {}).get("model_reasoning_effort"),
            }
        if method == "thread/resume":
            thread_id = params.get("threadId")
            earlier = [
                turn_object(turn_id, "completed")
                for turn_id in script.get("history", {}).get(thread_id, [])
            ]
            with self.lock:
                self.threads.setdefault(thread_id, earlier)
            return {"thread": {"id": thread_id}, "model": params.get("model")}
        if method == "thread/fork":
            source = params.get("threadId")
            thread_id = script.get("fork_thread_id", f"{source}-fork")
            with self.lock:
                self.threads.setdefault(thread_id, [])
            return {"thread": {"id": thread_id}, "model": params.get("model")}
        if method == "account/rateLimits/read":
            return {
                "ordinaryUsageAllowed": True,
                "rateLimits": {
                    "primary": {
                        "usedPercent": 12.0,
                        "windowDurationMins": 10080,
                        "resetsAt": 1794800000,
                    },
                    "rateLimitReachedType": None,
                },
            }
        if method == "thread/read":
            thread_id = params.get("threadId")
            with self.lock:
                if thread_id not in self.threads:
                    raise Failure(BAD_REQUEST, f"thread not found: {thread_id}")
                running = self.active is not None and self.active.thread_id == thread_id
                thread: dict[str, Any] = {
                    "id": thread_id,
                    "status": {"type": "active", "activeFlags": []}
                    if running
                    else {"type": "idle"},
                }
                if params.get("includeTurns"):
                    turns = list(self.threads[thread_id])
                    if running:
                        turns.append(turn_object(self.active.turn_id, "inProgress"))
                    thread["turns"] = turns
            return {"thread": thread}
        if method == "thread/turns/list":
            return self.list_turns(params)
        if method == "thread/items/list":
            return self.list_items(params)
        if method == "turn/steer":
            with self.lock:
                active = self.active
            if active is None or params.get("expectedTurnId") != active.turn_id:
                raise Failure(BAD_REQUEST, "expectedTurnId does not match the active turn")
            return {"turnId": active.turn_id}
        if method == "turn/interrupt":
            with self.lock:
                active = self.active
            if active is None or params.get("turnId") != active.turn_id:
                raise Failure(BAD_REQUEST, "turnId does not match the active turn")
            active.interrupted.set()
            return {}
        raise Failure(NOT_FOUND, "Method not found")

    def history(self, thread_id: str | None) -> list[dict[str, Any]]:
        """The thread's turns oldest first, the running one last."""
        with self.lock:
            if thread_id not in self.threads:
                raise Failure(BAD_REQUEST, f"thread not loaded: {thread_id}")
            turns = list(self.threads[thread_id])
            if self.active is not None and self.active.thread_id == thread_id:
                turns.append(turn_object(self.active.turn_id, "inProgress"))
        return turns

    def page(
        self, entries: list[Any], params: dict[str, Any], newest_first: bool
    ) -> dict[str, Any]:
        """One page of `entries` (oldest first): sorted per params.sortDirection, cut at params.limit and params.page_limit."""
        direction = params.get("sortDirection") or ("desc" if newest_first else "asc")
        ordered = entries[::-1] if direction == "desc" else list(entries)
        start = int(params.get("cursor") or 0)
        size = min(params.get("limit") or len(ordered), self.script.get("page_limit", len(ordered)))
        end = start + max(size, 1)
        return {
            "data": ordered[start:end],
            "nextCursor": str(end) if end < len(ordered) else None,
            "backwardsCursor": str(start) if ordered[start:end] else None,
        }

    def list_turns(self, params: dict[str, Any]) -> dict[str, Any]:
        view = params.get("itemsView") or "summary"
        shaped = []
        for turn in self.history(params.get("threadId")):
            items = {
                "notLoaded": [],
                "summary": [i for i in turn["items"] if i["type"] != "agentMessage"],
                "full": turn["items"],
            }[view]
            shaped.append({**turn, "items": items, "itemsView": view})
        return self.page(shaped, params, newest_first=True)

    def list_items(self, params: dict[str, Any]) -> dict[str, Any]:
        entries = [
            {
                "turnId": turn["id"],
                "item": item,
                "startedAtMs": item.get("startedAtMs"),
                "completedAtMs": item.get("completedAtMs"),
            }
            for turn in self.history(params.get("threadId"))
            if params.get("turnId") in (None, turn["id"])
            for item in turn["items"]
        ]
        return self.page(entries, params, newest_first=False)

    def start_turn(self, client: Client, params: dict[str, Any]) -> Any:
        thread_id = params.get("threadId")
        with self.lock:
            if thread_id not in self.threads:
                raise Failure(BAD_REQUEST, f"thread not found: {thread_id}")
            if self.active is not None:
                raise Failure(BAD_REQUEST, "a turn is already running")
            self.turn_count += 1
            turn_id = self.script["turn"].get("turn_id", f"turn-fake-{self.turn_count}")
            self.active = Turn(thread_id, turn_id, client)
            self.active.effort = params.get("effort")
        return {"turn": turn_object(turn_id, "inProgress")}

    def start_review(self, client: Client, params: dict[str, Any]) -> Any:
        if not {"threadId", "target"} <= set(params) <= {"threadId", "target", "delivery"}:
            raise Failure(BAD_REQUEST, f"invalid review/start params: {sorted(params)}")
        reply = self.start_turn(client, params)
        self.active.review = True
        return {**reply, "reviewThreadId": params["threadId"]}

    def play_turn(self, turn: Turn) -> None:
        spec = self.script["turn"]
        exit_at = spec.get("exit_at")

        def point(name: str) -> None:
            if exit_at == name:
                self.die()

        point("after_turn_start_response")
        ids = {"threadId": turn.thread_id, "turnId": turn.turn_id}
        if turn.review:
            self.emit_review_item(
                turn,
                {
                    "type": "enteredReviewMode",
                    "id": f"enter-{turn.turn_id}",
                    "review": "current changes",
                },
            )
        if not turn.review:
            settings = {
                "threadId": turn.thread_id,
                "threadSettings": {"effort": spec.get("applied_effort", turn.effort)},
            }
            self.emit_to_turn_client(
                turn, {"method": "thread/settings/updated", "params": settings}
            )
        self.emit_to_turn_client(
            turn,
            {
                "method": "turn/started",
                "params": {
                    "threadId": turn.thread_id,
                    "turn": turn_object(turn.turn_id, "inProgress"),
                },
            },
        )
        point("after_turn_started")
        for event in spec.get("events", []):
            self.emit_to_turn_client(turn, event)
        for approval in spec.get("approvals", []):
            self.ask_approval(turn, approval, ids, float(spec.get("approval_timeout", 30)))
            point("after_approval")
        if spec.get("hang"):
            turn.interrupted.wait()
        elif "final_delay" in spec:
            turn.interrupted.wait(float(spec["final_delay"]))
        items: list[dict[str, Any]] = []
        status = spec.get("status", "completed")
        if turn.interrupted.is_set():
            status = "interrupted"
        elif "final_answer" in spec:
            item = {
                "type": "agentMessage",
                "id": f"msg-fake-{turn.turn_id}",
                "text": spec["final_answer"],
                "phase": None if turn.review else "final_answer",
                "startedAtMs": now_ms(),
                "completedAtMs": now_ms(),
            }
            if turn.review:
                self.emit_review_item(
                    turn,
                    {
                        "type": "exitedReviewMode",
                        "id": f"exit-{turn.turn_id}",
                        "review": spec["final_answer"],
                    },
                )
            items.append(item)
            self.emit_to_turn_client(
                turn,
                {
                    "method": "item/completed",
                    "params": {"item": item, **ids, "completedAtMs": now_ms()},
                },
            )
            point("after_final_answer")
        done = turn_object(turn.turn_id, status, spec.get("error"), items)
        with self.lock:
            self.threads[turn.thread_id].append(done)
            self.active = None
        self.emit_to_turn_client(
            turn, {"method": "turn/completed", "params": {"threadId": turn.thread_id, "turn": done}}
        )
        self.emit_to_all(
            {
                "method": "thread/status/changed",
                "params": {"threadId": turn.thread_id, "status": {"type": "idle"}},
            }
        )
        point("after_turn_completed")

    def emit_review_item(self, turn: Turn, item: dict[str, Any]) -> None:
        ids = {"threadId": turn.thread_id, "turnId": turn.turn_id}
        self.emit_to_turn_client(
            turn,
            {"method": "item/started", "params": {"item": item, **ids, "startedAtMs": now_ms()}},
        )
        self.emit_to_turn_client(
            turn,
            {
                "method": "item/completed",
                "params": {"item": item, **ids, "completedAtMs": now_ms()},
            },
        )

    def ask_approval(
        self, turn: Turn, approval: dict[str, Any], ids: dict[str, str], timeout: float
    ) -> None:
        request_id = next(self.server_request_ids)
        waiter = (threading.Event(), [])
        with self.lock:
            self.waiting[request_id] = waiter
        params = {
            **ids,
            "itemId": "item-fake-1",
            "startedAtMs": now_ms(),
            **approval.get("params", {}),
        }
        try:
            turn.client.send_json(
                {"id": request_id, "method": approval["method"], "params": params}
            )
        except OSError:
            return
        waiter[0].wait(timeout)


class Failure(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.error = {"code": code, "message": message}


def turn_object(
    turn_id: str, status: str, error: Any = None, items: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    done = status != "inProgress"
    return {
        "id": turn_id,
        "rootTurnId": turn_id,
        "items": items or [],
        "itemsView": "summary" if items else "notLoaded",
        "status": status,
        "error": error,
        "startedAt": int(time.time()),
        "completedAt": int(time.time()) if done else None,
        "durationMs": 1 if done else None,
    }


def listen(path: str, log: Log) -> tuple[socket.socket, Any]:
    """Bind `path`; a path over the sun_path limit is bound short and symlinked, as Codex does."""
    if os.path.lexists(path):
        os.unlink(path)
    bound = path
    short_dir = None
    if len(os.fsencode(path)) >= SUN_PATH_LIMIT:
        short_dir = tempfile.mkdtemp(prefix="fc-", dir="/tmp")
        bound = os.path.join(short_dir, "s")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(bound)
    listener.listen(16)
    if short_dir is not None:
        os.symlink(bound, path)

    def cleanup() -> None:
        for leftover in (path, bound):
            try:
                os.unlink(leftover)
            except OSError:
                pass
        if short_dir is not None:
            shutil.rmtree(short_dir, ignore_errors=True)

    log.write("listen", path=path, bound=bound)
    return listener, cleanup


def main(argv: list[str]) -> int:
    if argv[:1] == ["--version"]:
        print("codex-cli 0.0.0-fake")
        return 0
    if argv[:1] != ["app-server"]:
        print(f"fake codex: unsupported arguments {argv}", file=sys.stderr)
        return 2
    listen_url = None
    i = 1
    while i < len(argv):
        if argv[i] == "--listen":
            listen_url = argv[i + 1]
            i += 2
        elif argv[i] == "-c":
            i += 2
        else:
            print(f"fake codex: unsupported argument {argv[i]}", file=sys.stderr)
            return 2
    if listen_url is None or not listen_url.startswith("unix://"):
        print("fake codex: app-server needs --listen unix://PATH", file=sys.stderr)
        return 2
    with open(os.environ["FAKE_CODEX_SCRIPT"]) as f:
        script = json.load(f)
    log = Log(os.environ["FAKE_CODEX_LOG"])
    log.write(
        "start",
        argv=argv,
        env=dict(os.environ),
        cwd=os.getcwd(),
        pid=os.getpid(),
        pgid=os.getpgrp(),
    )
    cleanups: list[Any] = []

    def on_term(signum: int, frame: Any) -> None:
        for cleanup in cleanups:
            cleanup()
        os._exit(0)

    signal.signal(signal.SIGTERM, signal.SIG_IGN if script.get("ignore_sigterm") else on_term)
    if "startup_fail" in script:
        print(f"fake codex: {script['startup_fail']}", file=sys.stderr, flush=True)
        return 3
    time.sleep(float(script.get("startup_delay", 0)))
    if script.get("spawn_child"):
        child = subprocess.Popen(["sleep", "300"])
        log.write("child", pid=child.pid)
    listener, cleanup = listen(listen_url[len("unix://") :], log)
    cleanups.append(cleanup)
    print(f"fake codex app-server listening on {listen_url}", file=sys.stderr, flush=True)
    Server(script, log, cleanup).serve(listener)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
