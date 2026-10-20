"""Upload explicit PC saves through the EXISTING date/caption GAS protocol.

No Hub annotation cutover is implied. Canonical requests are durable before
network I/O; ACK receipts and original queues are retained for later migration.
"""
import hashlib
import json
import uuid
from contextlib import closing
from tiktok_database_guard import database_lock
from tiktok_manual_queue import connect, queue_path
from tiktok_manual_edits import profile_for


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def verify_history(db, current, boundary):
    """Refuse server rollback or same-revision mutation before any new writes.

    A later revision is not proof of full historical continuity; the legacy
    pull API exposes only latest states. Full export remains required at cutover.
    """
    latest = {}
    for (payload,) in db.execute("SELECT receipt FROM legacy_uploads WHERE status='acked'"):
        receipt = json.loads(payload)
        state = receipt["state"]
        video = state["video_id"]
        if type(state.get("server_seq")) is not int or state["server_seq"] > boundary:
            raise ValueError("GAS history is behind an acknowledged PC edit; stop and restore/review server history")
        if state["revision"] > latest.get(video, {}).get("revision", 0):
            latest[video] = state
    for video, known in latest.items():
        remote = current.get(video)
        if (not remote or remote["revision"] < known["revision"] or
                remote["server_seq"] < known["server_seq"] or
                not set(known["fields"]).issubset(remote["fields"])):
            raise ValueError("GAS is missing acknowledged manual history; local queue and receipts retained")
        if remote["revision"] == known["revision"] and encode(remote) != encode(known):
            raise ValueError("GAS changed an acknowledged revision; do not overwrite either copy")


def read_remote(profile, rpc):
    current, cursor = {}, 0
    for page in range(10000):
        response = rpc(dict(action="pull", profile=profile, cursor=cursor))
        if response.get("ok") is not True:
            raise RuntimeError("GAS pull not confirmed; local queue retained")
        next_cursor = response.get("cursor")
        if type(next_cursor) is not int or next_cursor < cursor:
            raise ValueError("Invalid GAS cursor")
        if type(response.get("has_more")) is not bool or not isinstance(response.get("changes"), list):
            raise ValueError("Incomplete GAS page")
        for state in response["changes"]:
            if (state.get("profile") != profile or type(state.get("revision")) is not int or state["revision"] < 1 or
                    type(state.get("server_seq")) is not int or not cursor < state["server_seq"] <= next_cursor or
                    not isinstance(state.get("fields"), dict) or state.get("video_id") in current):
                raise ValueError("Invalid GAS state")
            current[state["video_id"]] = state
        if not response["has_more"]:
            return current, next_cursor
        if next_cursor <= cursor:
            raise ValueError("GAS cursor did not advance")
        cursor = next_cursor
    raise ValueError("GAS pagination limit reached")


def push(path, endpoint, rpc, limit=50, *, include_remote=False):
    """rpc receives no credentials here; caller owns token and URL validation."""
    if not queue_path(path).exists():
        return dict(acked=0, blocked=0, remaining=0)
    profile = profile_for(path)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Invalid upload limit")
    # Serializes uploaders, not manager saves. No SQLite lock across network I/O.
    with database_lock(queue_path(path)), closing(connect(path)) as db:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS legacy_endpoint(identity TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS legacy_uploads(
                op_id TEXT PRIMARY KEY,save_id TEXT NOT NULL,video_id TEXT NOT NULL,
                request TEXT NOT NULL,status TEXT NOT NULL,receipt TEXT)""")
            identity = hashlib.sha256((endpoint+"|"+profile).encode()).hexdigest()
            existing = db.execute("SELECT identity FROM legacy_endpoint").fetchall()
            if existing and existing != [(identity,)]:
                raise ValueError("GAS endpoint changed; retain queue and review before reconnecting")
            if not existing:
                db.execute("INSERT INTO legacy_endpoint VALUES(?)", (identity,))
        # Read current server state. Never infer revision from a file timestamp.
        current, next_cursor = read_remote(profile, rpc)
        verify_history(db, current, next_cursor)
        acked, blocked, attempted = 0, set(), 0
        rows = [(a,b,json.loads(c)["changes"],d) for a,b,c,d in db.execute(
            "SELECT op_id,created_at,payload,status FROM manual_saves WHERE status!='legacy_acked' ORDER BY rowid")]
        while attempted < limit:
            batch, visited, batch_keys = [], set(), {}
            for save_id, created_at, changes, status in rows:
                if status != "committed":
                    blocked.update(change["video_id"] for change in changes)
                    continue
                for change in changes:
                    video = change["video_id"]
                    identifier = str(uuid.uuid5(uuid.UUID(save_id), video))
                    saved = db.execute("SELECT request,status FROM legacy_uploads WHERE op_id=?", (identifier,)).fetchone()
                    if saved and saved[1] == "acked":
                        continue
                    if video in blocked or video in visited:
                        continue
                    # A video's second edit cannot leap over an earlier edit,
                    # even when the earlier one does not fit the byte budget.
                    visited.add(video)
                    if len(batch) >= min(20, limit-attempted):
                        continue
                    remote = current.get(video, dict(profile=profile, video_id=video, revision=0, fields={}))
                    if saved:
                        operation = json.loads(saved[0])  # Exact lost-ACK retry.
                    else:
                        fields = remote.get("fields", {})
                        if any(key in fields and fields[key] != change["base_values"][key]
                               for key in change["fields"]):
                            blocked.add(video)
                            continue
                        operation = dict(op_id=identifier, profile=profile, video_id=video,
                                         base_revision=remote["revision"], after_op="",
                                         patch=change["fields"], base_values=change["base_values"],
                                         created_at=created_at)
                    candidate = dict(action="sync", profile=profile, cursor=0, ops=batch+[operation])
                    # gas_request uses JSON ASCII escaping. Budget that encoding,
                    # not the smaller unescaped Unicode payload; leave token room.
                    if len(json.dumps(candidate).encode("utf-8")) > 160000:
                        if not batch:
                            raise ValueError("Manual operation too large; keep local queue for review")
                        continue
                    if not saved:
                        with db:
                            db.execute("INSERT INTO legacy_uploads VALUES(?,?,?,?,?,NULL)",
                                       (identifier, save_id, video, encode(operation), "pending"))
                    batch.append(operation)
                    batch_keys[operation["op_id"]] = identifier
            if not batch:
                break
            attempted += len(batch)
            reply = rpc(dict(action="sync", profile=profile, cursor=0, ops=batch))
            if reply.get("ok") is not True:
                raise RuntimeError("GAS sync not confirmed; original requests retained")
            receipts = reply.get("acks")
            expected = {op["op_id"] for op in batch}
            if (not isinstance(receipts, list) or len(receipts) != len(expected) or
                    {a.get("op_id") for a in receipts} != expected):
                raise ValueError("Missing/duplicate GAS acknowledgement; queue retained")
            by_id = {a["op_id"]: a for a in receipts}
            updates = []
            for operation in batch:
                identifier, video = operation["op_id"], operation["video_id"]
                receipt = by_id[identifier]
                if receipt.get("status") == "conflict":
                    updates.append(("conflict", receipt, operation))
                    continue
                state = receipt.get("state", {})
                if (receipt.get("status") != "ok" or state.get("profile") != profile or
                    state.get("video_id") != video or type(state.get("revision")) is not int or
                    type(state.get("server_seq")) is not int or state["server_seq"] < 1 or
                    state["revision"] != operation["base_revision"]+1 or
                    receipt.get("revision") != state["revision"] or
                    any(state.get("fields", {}).get(k) != v for k,v in operation["patch"].items())):
                    raise ValueError("Invalid GAS receipt; queue retained")
                updates.append(("acked", receipt, operation))
            # Validate the entire response before acknowledging any local entry.
            with db:
                db.execute("BEGIN IMMEDIATE")
                for status, receipt, operation in updates:
                    db.execute("UPDATE legacy_uploads SET status=?,receipt=? WHERE op_id=?",
                               (status, encode(receipt), batch_keys[operation["op_id"]]))
            for status, receipt, operation in updates:
                video = operation["video_id"]
                if status == "conflict":
                    blocked.add(video)
                else:
                    state = receipt["state"]
                    if state["revision"] >= current.get(video, {}).get("revision", 0):
                        current[video] = state
                    acked += 1
        for save_id, _, changes, status in rows:
            if status == "committed" and all(db.execute(
                    "SELECT status FROM legacy_uploads WHERE op_id=?",
                    (str(uuid.uuid5(uuid.UUID(save_id), change["video_id"])),)).fetchone() == ("acked",)
                    for change in changes):
                with db:
                    db.execute("UPDATE manual_saves SET status='legacy_acked' WHERE op_id=?", (save_id,))
        remaining = db.execute("SELECT count(*) FROM manual_saves WHERE status!='legacy_acked'").fetchone()[0]
        result = dict(acked=acked, blocked=len(blocked), remaining=remaining)
        if include_remote:
            # The complete audited pull plus confirmed own ACKs can feed local
            # projection directly. Concurrent later edits arrive next cycle.
            # Keep this opt-in: normal UI/status responses must not expose text.
            result["remote_document"] = dict(profile=profile, changes=list(current.values()))
        return result
