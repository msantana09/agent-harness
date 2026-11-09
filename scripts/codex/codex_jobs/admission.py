"""Usage admission: should this job's turn start now?

Two gates, both read from live facts:

* the account's usage, from the app-server's `account/rateLimits/read` (`check_usage`);
* this machine's concurrency cap, counted from the job records under the job root and claimed
  atomically (`admit`).

Limits live in the tracked `scripts/codex/admission.json`; `load_config` raises when the file or
a key is missing, there is no default in code. The wire reply is read with its real camelCase
keys only: a reply that spells them differently raises `MalformedRateLimits` instead of being
read as "plenty of headroom".
"""

import fcntl
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import records
from .appserver import AppServerClient
from .worktrees import appserver_running

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "admission.json"
RECHECK_SECONDS = 15
LOCK_NAME = "admission.lock"
RATE_LIMITS_METHOD = "account/rateLimits/read"

REASON_USAGE_NOT_ALLOWED = "usage not allowed"


class AdmissionConfigError(Exception):
    """The admission config file or one of its keys is missing or invalid."""

    def __init__(self, path: Path | str, key: str | None, detail: str):
        self.path, self.key, self.detail = Path(path), key, detail
        where = f"{self.path}" if key is None else f"{self.path}: key {key!r}"
        super().__init__(f"{where}: {detail}")


class MalformedRateLimits(Exception):
    """The `account/rateLimits/read` reply lacks a key the server is documented to send."""


@dataclass(frozen=True)
class AdmissionConfig:
    max_concurrent_jobs: int
    reserve_percent: int


@dataclass(frozen=True)
class UsageDecision:
    """Outcome of the usage check.

    `record` is the block stored in the job's `admission` field: `allowed` is `true`, `false`
    or `"unknown"` (the server reported `ordinaryUsageAllowed: null`), next to the wire values
    that were read. `resets_at` is an ISO-8601 UTC string when the server sent one.
    """

    allowed: bool
    reason: str | None
    resets_at: str | None
    record: dict[str, Any]


def load_config(path: Path | str) -> AdmissionConfig:
    path = Path(path)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise AdmissionConfigError(path, None, "file not found") from None
    except json.JSONDecodeError as exc:
        raise AdmissionConfigError(path, None, f"not valid JSON ({exc})") from None
    if not isinstance(raw, dict):
        raise AdmissionConfigError(path, None, "top level must be a JSON object")

    def read(key: str, minimum: int, maximum: int | None) -> int:
        if key not in raw:
            raise AdmissionConfigError(path, key, "key missing")
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise AdmissionConfigError(path, key, f"must be an integer, got {value!r}")
        if value < minimum or (maximum is not None and value > maximum):
            bounds = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
            raise AdmissionConfigError(path, key, f"must be {bounds}, got {value}")
        return value

    return AdmissionConfig(
        max_concurrent_jobs=read("max_concurrent_jobs", 1, None),
        reserve_percent=read("reserve_percent", 0, 100),
    )


def iso_utc(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require(container: Any, key: str, where: str) -> Any:
    if not isinstance(container, dict) or key not in container:
        raise MalformedRateLimits(f"{where} has no {key!r} key: {container!r}")
    return container[key]


def _int_or_none(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedRateLimits(f"{name} must be an integer or null, got {value!r}")
    return value


def over_reserve(used_percent: float, reserve_percent: int) -> bool:
    return used_percent > 100 - reserve_percent


def refused(record: dict[str, Any]) -> bool:
    """Whether a stored `UsageDecision.record` is one the usage gate refused."""
    used = record["usedPercent"]
    return record["ordinaryUsageAllowed"] is False or (
        isinstance(used, int | float) and over_reserve(used, record["reserve_percent"])
    )


def check_usage(rate_limits_result: dict[str, Any], config: AdmissionConfig) -> UsageDecision:
    """Decide from an `account/rateLimits/read` result whether a turn may start.

    Order: `ordinaryUsageAllowed is False` refuses outright (the window is read only if the
    server sent one, for `resets_at`); otherwise `primary.usedPercent > 100 - reserve_percent`
    refuses. `ordinaryUsageAllowed: null` is recorded as `allowed: "unknown"` and the percent
    rule decides. `primary: null` (no window reported) never refuses on percent: it records
    `usedPercent: "unknown"`, because missing data must not block a job. A reply without the
    `rateLimits` or `primary` key, or whose `primary` lacks `usedPercent`, is protocol drift and
    raises `MalformedRateLimits`.
    """
    if not isinstance(rate_limits_result, dict):
        raise MalformedRateLimits(f"reply is not an object: {rate_limits_result!r}")
    ordinary = rate_limits_result.get("ordinaryUsageAllowed")
    if ordinary is not None and not isinstance(ordinary, bool):
        raise MalformedRateLimits(
            f"ordinaryUsageAllowed must be a boolean or null, got {ordinary!r}"
        )
    snapshot = _require(rate_limits_result, "rateLimits", "reply")
    primary = _require(snapshot, "primary", "rateLimits")  # null is valid: no usage window reported

    record: dict[str, Any] = {
        "allowed": "unknown" if ordinary is None else ordinary,
        "ordinaryUsageAllowed": ordinary,
        "usedPercent": "unknown" if primary is None else None,
        "windowDurationMins": None,
        "resetsAt": None,
        "rateLimitReachedType": snapshot.get("rateLimitReachedType"),
        "reserve_percent": config.reserve_percent,
    }

    if ordinary is False:
        resets_epoch = None
        if isinstance(primary, dict):
            resets_epoch = _int_or_none(primary.get("resetsAt"), "primary.resetsAt")
            record["usedPercent"] = _int_or_none(primary.get("usedPercent"), "primary.usedPercent")
            record["windowDurationMins"] = _int_or_none(
                primary.get("windowDurationMins"), "primary.windowDurationMins"
            )
            record["resetsAt"] = resets_epoch
        return UsageDecision(
            allowed=False,
            reason=REASON_USAGE_NOT_ALLOWED,
            resets_at=None if resets_epoch is None else iso_utc(resets_epoch),
            record=record,
        )

    if primary is None:
        return UsageDecision(allowed=True, reason=None, resets_at=None, record=record)

    used = _require(primary, "usedPercent", "rateLimits.primary")
    if isinstance(used, bool) or not isinstance(used, int | float):
        raise MalformedRateLimits(f"primary.usedPercent must be a number, got {used!r}")
    resets_epoch = _int_or_none(primary.get("resetsAt"), "primary.resetsAt")
    record.update(
        usedPercent=used,
        windowDurationMins=_int_or_none(
            primary.get("windowDurationMins"), "primary.windowDurationMins"
        ),
        resetsAt=resets_epoch,
    )
    resets_at = None if resets_epoch is None else iso_utc(resets_epoch)

    if over_reserve(used, config.reserve_percent):
        return UsageDecision(
            allowed=False,
            reason=f"usage reserve reached: {used}% used, {config.reserve_percent}% reserved",
            resets_at=resets_at,
            record=record,
        )
    return UsageDecision(allowed=True, reason=None, resets_at=resets_at, record=record)


def read_usage(client: AppServerClient, config: AdmissionConfig) -> UsageDecision:
    """Read the limits through an initialized app-server client and decide."""
    return check_usage(client.call(RATE_LIMITS_METHOD), config)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


SLOT_STATES = frozenset({records.ADMITTED, records.STARTING, records.DISPATCHED, records.RUNNING})


def slot_holders(root: Path, *, exclude: str | None = None) -> list[dict[str, Any]]:
    """Jobs holding a slot on this machine: a slot state with a live runner, or a live app-server.

    A job in a slot state holds its slot while its runner pid is alive. Any job, whatever its
    state, also holds one for as long as its detached app-server process group is alive and not
    stamped stopped: a turn may still be running there (a dead runner's turn that reconcile
    adopts, or an `outcome_unknown` or `timed_out` turn the runner has not stopped yet).
    `queued` jobs hold nothing, and a dead runner with no live app-server does not count,
    whatever the record says. Oldest first.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    holders = []
    for entry in sorted(root.iterdir()):
        if not records.JOB_ID_RE.match(entry.name) or entry.name == exclude:
            continue
        try:
            job = records.load_job(root, entry.name)
        except records.JobNotFound:  # mid-creation, or removed by an idempotency loser
            continue
        pid = job.get("runner_pid")
        runner_alive = pid is not None and _pid_alive(pid)
        if (job["state"] in SLOT_STATES and runner_alive) or appserver_running(job):
            holders.append(job)
    return sorted(holders, key=lambda job: (job["created_at"], job["job_id"]))


def _try_admit(root: Path, job_id: str, config: AdmissionConfig) -> list[str]:
    """Under the root-wide lock: admit `job_id` if a slot is free, else return the holders' ids."""
    fd = os.open(Path(root) / LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        holders = [job["job_id"] for job in slot_holders(root, exclude=job_id)]
        if len(holders) < config.max_concurrent_jobs:
            records.transition(root, job_id, records.ADMITTED)
            return []
        return holders
    finally:
        os.close(fd)


def admit(
    root: Path,
    job_id: str,
    config: AdmissionConfig,
    *,
    is_cancelled: Callable[[], bool],
    recheck_seconds: float = RECHECK_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Move `job_id` from `queued` to `admitted` once a slot is free; False if cancelled first.

    Precondition: the caller is the job's runner and has already recorded its own
    `os.getpid()` as `runner_pid`. Other runners count a job while that pid is alive (or its
    app-server group is), so a runner that has not recorded it holds no slot in their eyes.

    The count and the transition happen under an exclusive `flock` on `<root>/admission.lock`,
    so concurrent callers cannot all see a free slot: at most `max_concurrent_jobs` jobs are
    ever in a slot state with a live runner or app-server. `queued` jobs are never counted, so
    any number of waiters can poll without blocking each other. While at the cap, each change in the
    set of
    holders is journaled on the job as a `queued_behind` event, then it sleeps
    `recheck_seconds` and tries again. `root` must exist.
    """
    job_dir = Path(root) / job_id
    last_reported: list[str] | None = None
    while True:
        if is_cancelled():
            return False
        behind = _try_admit(root, job_id, config)
        if not behind:
            return True
        if behind != last_reported:
            records.append_event(job_dir, "queued_behind", {"queued_behind": behind})
            last_reported = behind
        sleep(recheck_seconds)
