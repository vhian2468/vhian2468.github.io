"""Shared JSON writer protocol for collector, manager, merge and pipeline.

Locks are per resolved destination, reentrant in a thread, and process-wide on
disk. They are advisory: every writer must participate before live merge is
enabled. Compare-and-swap catches stale in-memory copies after the lock releases.
No database, backup or rejected edit is ever pruned by this module.
"""
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import tiktok_feed_authority as feed_state

_registry = {}
_registry_lock = threading.Lock()
_local = threading.local()


def sha(data):
    return hashlib.sha256(data).hexdigest()


class StaleDatabase(RuntimeError):
    def __init__(self, path, recovery):
        self.recovery = recovery
        super().__init__(f"Database đã thay đổi sau khi đọc; không ghi đè {path}. "
                         f"Bản chờ phục hồi: {recovery}")


@dataclass(frozen=True)
class Snapshot:
    path: Path
    data: bytes | None
    version: str | None


def snapshot(path):
    path = Path(path).resolve()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = None
    return Snapshot(path, data, sha(data) if data is not None else None)


@contextmanager
def database_lock(path, timeout=5):
    path = Path(path).resolve()
    key = os.path.normcase(str(path))
    with _registry_lock:
        mutex = _registry.setdefault(key, threading.RLock())
    if not mutex.acquire(timeout=timeout):
        raise TimeoutError(f"Database đang được chương trình khác lưu: {path}")
    held = getattr(_local, "held", None)
    if held is None:
        _local.held = held = {}
    handle = None
    locked = False
    try:
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = Path(str(path) + ".writer.lock").open("a+b")
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Database đang có writer khác: {path}") from error
                time.sleep(.05)
        held[key] = True
        yield
    finally:
        try:
            if locked:
                held.pop(key, None)
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                if handle is not None:
                    handle.close()
            finally:
                mutex.release()


def validate_bytes(data):
    value = json.loads(data.decode("utf-8-sig"))
    rows = list(value.values()) if isinstance(value, dict) else value
    if not isinstance(rows, list):
        raise ValueError("Database phải là List hoặc ID map")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not str(row.get("id") or "").strip():
            raise ValueError("Database có bản ghi thiếu ID")
        identifier = str(row["id"])
        if identifier in seen:
            raise ValueError("Database có ID trùng")
        seen.add(identifier)
    json.dumps(value, allow_nan=False)
    return value


def preserve(path, data, kind, metadata=None):
    """Immutable recovery artifact beside its target; never a live replacement."""
    directory = Path(path).resolve().parent / ".repost-recovery"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (Path(path).name + "." + uuid.uuid4().hex + "." + kind + ".json")
    with target.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if metadata is not None:
        with target.with_suffix(".receipt.json").open("x", encoding="utf-8") as stream:
            json.dump(metadata, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
    return target


def commit_bytes(path, data, expected_version, *, backup=False, rolling_backup=None, label="writer",
                 feed_authority=None):
    """Atomic CAS. A stale proposal is retained even when the live file vanished."""
    path = Path(path).resolve()
    validate_bytes(data)
    with database_lock(path):
        current = snapshot(path)
        if current.version != expected_version:
            recovery = preserve(path, data, "pending", dict(
                label=label, expected_sha256=expected_version, actual_sha256=current.version,
                proposed_sha256=sha(data), main_modified=False))
            raise StaleDatabase(path, recovery)
        # Resolve an interrupted feed commit BEFORE a metadata-only writer can
        # change its proving checksum. This preserves capture authority across
        # manual edits, thumbnail writes and process crashes.
        applied_authority = feed_state.recover(path, current.version)
        if current.data == data:
            if feed_authority is not None and feed_state.normalize(feed_authority) != applied_authority:
                feed_state.prepare(path, current.version, current.version, feed_authority)
                feed_state.finish(path, current.version)
            return current
        if backup and current.data is not None:
            preserve(path, current.data, "before", dict(label=label, sha256=current.version))
        if rolling_backup is not None and current.data is not None:
            rolling = Path(rolling_backup).resolve()
            if rolling == path or not rolling.name.startswith(path.name + ".") or rolling.parent != path.parent:
                raise ValueError("Rolling backup must be a distinct sibling of the target")
            # Keep a single explicit pre-save backup for frequent UI edits.
            # Rejected edits and explicit merge snapshots remain immutable.
            with database_lock(rolling):
                commit_bytes(rolling, current.data, snapshot(rolling).version, label=label + "-backup")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=path.name + ".",
                                             suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Checksums protect the proposal, not just the number of rows.
            if temporary.read_bytes() != data:
                raise IOError("File tạm khác bản đề xuất; không publish")
            if feed_authority is not None:
                feed_state.prepare(path, current.version, sha(data), feed_authority)
            os.replace(temporary, path)
            temporary = None
            if feed_authority is not None:
                feed_state.finish(path, sha(data))
        except Exception as error:
            if temporary is None:
                raise RuntimeError("Database đã ghi nhưng mốc Feed chưa chốt; "
                                   "giữ giao dịch dở để writer mới tự phục hồi, không dùng tool bản cũ") from error
            # Keep even a partially written proposal for diagnosis/recovery.
            # A disk-full or replace failure must not discard the user's edit.
            raise RuntimeError(f"Chưa ghi database; giữ file tạm tại {temporary}") from error
        return Snapshot(path, data, sha(data))


def commit_json(path, value, expected_version, **options):
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    return commit_bytes(path, data, expected_version, **options)


def local_authority_is_current(path):
    """Recognize an approved local edit even after a crash immediately after CAS."""
    path = Path(path).resolve()
    version = snapshot(path).version
    if version is None:
        return False
    marker = Path(str(path) + ".manager.state.json")
    if marker.is_file():
        state = json.loads(marker.read_text(encoding="utf-8"))
        if state.get("sha256") == version:
            return True
    directory = path.parent / ".repost-recovery"
    for plan in directory.glob(path.name + ".*.merge.json"):
        try:
            receipt = json.loads(plan.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A prepared plan is written before any replacement. An incomplete
            # plan alone cannot authorize a database overwrite.
            continue
        if (receipt.get("format") == "pc-merge-commit-v1"
                and os.path.normcase(receipt.get("target", "")) == os.path.normcase(str(path))
                and receipt.get("after_sha256") == version):
            return True
    return False
