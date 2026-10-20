"""Crash-recoverable feed provenance beside a JSON DB, under its writer lock.

This module never changes video data. Call prepare/finish only while holding
the shared database lock. Preview reads are non-mutating. Raw/API clocks are
capture clocks, never download/mtime clocks. State lives in the private recovery
directory so it cannot be accidentally published with data_*.json.
"""
import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


def normalize(value):
    if not isinstance(value, dict) or set(value) != {
            "profile", "run_id", "device_id", "run_started", "items"}:
        raise ValueError("Feed authority has an invalid shape")
    result = dict(value)
    if not isinstance(result["profile"], str) or not re.fullmatch(r"[A-Za-z0-9_.]{1,64}", result["profile"]):
        raise ValueError("Feed authority profile is invalid")
    for field in ("run_id", "device_id"):
        if not isinstance(result[field], str) or str(uuid.UUID(result[field])) != result[field]:
            raise ValueError("Feed authority identity is invalid")
    stamp = datetime.fromisoformat(result["run_started"].replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Feed capture time must include timezone")
    result["run_started"] = stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")
    if isinstance(result["items"], bool) or not isinstance(result["items"], int) or result["items"] < 1:
        raise ValueError("Feed capture must contain at least one observed ID")
    return result


def is_current(incoming, previous):
    incoming = normalize(incoming)
    if previous is None:
        return True
    previous = normalize(previous)
    if incoming["profile"] != previous["profile"]:
        raise ValueError("Feed authority profile mismatch")
    if incoming["run_id"] == previous["run_id"]:
        if (incoming["device_id"], incoming["run_started"]) != (previous["device_id"], previous["run_started"]):
            raise ValueError("Feed run identity changed")
        return incoming["items"] >= previous["items"]
    return (incoming["run_started"], incoming["run_id"]) > (previous["run_started"], previous["run_id"])


def state_path(path):
    path = Path(path).resolve()
    return path.parent / ".repost-recovery" / (path.name + ".feed-state.json")


def inspect(path, version):
    """Return effective authority plus exact state fingerprint; no writes.

    Pending+after SHA proves the JSON replacement landed. Pending+before means
    it did not. Any third state requires review rather than guessing freshness.
    """
    path = Path(path).resolve()
    try:
        raw = state_path(path).read_bytes()
    except FileNotFoundError:
        return None, None
    state = json.loads(raw)
    if (state.get("format") != "repost-feed-state-v1"
            or os.path.normcase(state.get("target", "")) != os.path.normcase(str(path))):
        raise ValueError("Feed state belongs to another database or format")
    current = normalize(state["current"]) if state.get("current") is not None else None
    pending = state.get("pending")
    if pending is not None:
        candidate = normalize(pending["authority"])
        if current is not None and not is_current(candidate, current):
            raise ValueError("Pending Feed authority goes backwards")
        if version == pending["after_sha256"]:
            current = candidate
        elif version != pending["before_sha256"]:
            raise ValueError("Database đổi ngoài writer khi Feed còn giao dịch dở; cần đối chiếu, không ghi đè")
    return current, hashlib.sha256(raw).hexdigest()


def _write(path, state):
    target = state_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    with tempfile.NamedTemporaryFile("wb", dir=target.parent, prefix=target.name + ".",
                                     suffix=".pending", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    # A failed replace leaves a recoverable file; never remove an old state.
    os.replace(temporary, target)


def recover(path, version):
    current, token = inspect(path, version)
    if token is not None:
        state = json.loads(state_path(path).read_bytes())
        if state.get("pending") is not None:
            state["current"], state["pending"] = current, None
            _write(path, state)
    return current


def prepare(path, before, after, incoming):
    path = Path(path).resolve()
    incoming = normalize(incoming)
    current = recover(path, before)
    if not is_current(incoming, current):
        raise ValueError("Phiên Feed cũ hơn mốc đã lưu; không đảo lại thứ tự")
    state = dict(format="repost-feed-state-v1", target=str(path), current=current,
                 pending=dict(id=str(uuid.uuid4()), before_sha256=before,
                              after_sha256=after, authority=incoming))
    directory = state_path(path).parent
    directory.mkdir(parents=True, exist_ok=True)
    # Keep the intent independently of the small mutable checkpoint.
    with (directory / (path.name + "." + state["pending"]["id"] + ".feed-intent.json")).open(
            "x", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    _write(path, state)


def finish(path, version):
    return recover(path, version)
