"""Durable PC manual-save intents; transport must send only committed entries.

These are explicit manager edits, NOT guessed annotation causal parents.
The Hub adapter must resolve their baseline before publishing annotations.
Never prune this journal or automatically ACK it from a JSON/GitHub import.
"""
import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tiktok_database_guard import database_lock, snapshot, commit_bytes, sha, validate_bytes

FIELDS = ("repost_date", "repost_caption")


def queue_path(path):
    return Path(str(Path(path).resolve()) + ".manual-queue.sqlite")


def queue_summary(path):
    file = queue_path(path)
    if not file.exists():
        return {}
    with closing(sqlite3.connect(file.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        if db.execute("SELECT target FROM queue_scope").fetchall() != [(os.path.normcase(str(Path(path).resolve())),)]:
            raise ValueError("Manual queue belongs to another database")
        return dict(db.execute("SELECT status,count(*) FROM manual_saves GROUP BY status"))


def status_title(path):
    counts = queue_summary(path)
    if not counts:
        return "TikTok HTML Generator & Data Manager"
    confirmed = counts.pop("legacy_acked", 0)
    if not counts:
        return "TikTok Manager · GAS từng xác nhận " + str(confirmed) + " lần lưu thủ công"
    return ("TikTok Manager · " + str(sum(counts.values())) +
            " lần lưu thủ công local · CHƯA gửi Drive" +
            (" · GAS từng xác nhận " + str(confirmed) if confirmed else "") +
            (" · cần đối chiếu " + str(counts["needs_review"]) if counts.get("needs_review") else ""))


def pending_fields(path):
    """Read-only protection set, including unproven/failed saves.

    Per-video ACKs release only that video's fields; a multi-video save may
    still have other videos awaiting confirmation.
    """
    file = queue_path(path)
    if not file.exists():
        return {}
    result = {}
    with closing(sqlite3.connect(file.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        if db.execute("SELECT target FROM queue_scope").fetchall() != [(os.path.normcase(str(Path(path).resolve())),)]:
            raise ValueError("Manual queue belongs to another database")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        acknowledged = (set(db.execute("SELECT save_id,video_id FROM legacy_uploads WHERE status='acked'"))
                        if "legacy_uploads" in tables else set())
        for identifier, payload, status in db.execute(
                "SELECT op_id,payload,status FROM manual_saves ORDER BY rowid"):
            if status == "legacy_acked":
                continue
            if status not in ("prepared", "committed", "needs_review"):
                raise ValueError("Unknown manual queue state; refuse incoming overwrite")
            for change in json.loads(payload)["changes"]:
                if status == "committed" and (identifier, change["video_id"]) in acknowledged:
                    continue
                for field, value in change["fields"].items():
                    result[(change["video_id"], field)] = dict(op_id=identifier, value=value, status=status)
    return result


def connect(path):
    db = sqlite3.connect(queue_path(path), timeout=5)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables - {"queue_scope", "manual_saves", "legacy_uploads", "legacy_endpoint", "legacy_upload_history"}:
            raise ValueError("Refusing unrelated manual queue database")
        db.execute("PRAGMA synchronous=FULL")
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS queue_scope(target TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS manual_saves(
                op_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                before_sha256 TEXT NOT NULL, after_sha256 TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL)""")
            target = os.path.normcase(str(Path(path).resolve()))
            scopes = db.execute("SELECT target FROM queue_scope").fetchall()
            if scopes and scopes != [(target,)]:
                raise ValueError("Manual queue belongs to another database")
            if not scopes:
                db.execute("INSERT INTO queue_scope VALUES(?)", (target,))
        return db
    except BaseException:
        db.close()
        raise


def changes(before, after):
    old = {str(v["id"]): v for v in (before.values() if isinstance(before, dict) else before)}
    edits = []
    for video in after:
        identifier = str(video["id"])
        previous = old.get(identifier, {})
        fields, base = {}, {}
        for field in FIELDS:
            value = str(video.get(field, "") or "")
            prior = str(previous.get(field, "") or "")
            if field in video.get("manual_fields", []) and value != prior:
                fields[field], base[field] = value, prior
        if fields:
            edits.append(dict(video_id=identifier, fields=fields, base_values=base,
                              existed_before=identifier in old))
    return edits


def recover(db, version):
    """Only an exact file hash proves completion; unknown state stays reviewable."""
    with db:
        for identifier, before, after in db.execute(
                "SELECT op_id,before_sha256,after_sha256 FROM manual_saves WHERE status='prepared'").fetchall():
            status = "committed" if version == after else "prepared" if version == before else "needs_review"
            db.execute("UPDATE manual_saves SET status=? WHERE op_id=?", (status, identifier))


def commit_manager_json(path, value, expected_version, **options):
    path = Path(path).resolve()
    proposed = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    validate_bytes(proposed)
    with database_lock(path):
        current = snapshot(path)
        if current.version != expected_version or current.data is None:
            # Existing CAS retains the rejected full proposal. Never queue stale
            # editor changes as accepted edits.
            return commit_bytes(path, proposed, expected_version, **options)
        edits = changes(json.loads(current.data.decode("utf-8-sig")), value)
        if not edits and not queue_path(path).exists():
            return commit_bytes(path, proposed, expected_version, **options)
        with closing(connect(path)) as db:
            recover(db, current.version)
            if not edits:
                return commit_bytes(path, proposed, expected_version, **options)
            payload = json.dumps(dict(format="pc-manual-save-v1", changes=edits),
                                 ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            after = sha(proposed)
            with db:
                db.execute("BEGIN IMMEDIATE")
                previous = db.execute("""SELECT op_id FROM manual_saves
                    WHERE before_sha256=? AND after_sha256=? AND payload=? AND status='prepared'""",
                    (current.version, after, payload)).fetchone()
                identifier = previous[0] if previous else str(uuid.uuid4())
                if not previous:
                    db.execute("INSERT INTO manual_saves VALUES(?,?,?,?,?,?)",
                               (identifier, datetime.now(timezone.utc).isoformat(),
                                current.version, after, payload, "prepared"))
            # Intent is durable BEFORE changing the main JSON. Any failure leaves
            # its original values intact for retry/recovery.
            committed = commit_bytes(path, proposed, expected_version, **options)
            with db:
                db.execute("UPDATE manual_saves SET status='committed' WHERE op_id=?", (identifier,))
            return committed
