"""Explicit field-level reconciliation of an open PC form with newer JSON."""
import copy
import json
from tiktok_database_guard import snapshot, validate_bytes
from tiktok_manual_queue import commit_manager_json
from tiktok_manual_edits import validate_patch, atomic_json

FIELDS = {"repost_date", "repost_caption", "uniqueId", "nickname", "desc", "music_title", "music_id"}


def prepare(path, video_id, opening, proposed):
    if set(proposed)-FIELDS or set(proposed)-set(opening) or not all(isinstance(v, str) for v in proposed.values()):
        raise ValueError("Thiếu bản gốc của trường đang sửa")
    validate_patch({k:v for k,v in proposed.items() if k in ("repost_date", "repost_caption")})
    current = snapshot(path)
    if current.data is None:
        raise ValueError("Database đã biến mất; giữ nguyên phần đang nhập")
    validate_bytes(current.data)
    data = json.loads(current.data.decode("utf-8-sig"))
    data = list(data.values()) if isinstance(data, dict) else data
    target = next((v for v in data if str(v["id"]) == video_id), None)
    if target is None:
        raise ValueError("Video không còn trong database; không tự thêm lại. Phần đang nhập vẫn giữ.")
    changes, conflicts = {}, {}
    for field, value in proposed.items():
        before, remote = str(opening[field] or ""), str(target.get(field, "") or "")
        if value == before:
            continue  # User did not edit this field: preserve disk/API updates.
        if remote == before or remote == value:
            changes[field] = value
        else:
            conflicts[field] = dict(base=before, current=remote, local=value)
    return dict(path=str(current.path), version=current.version, data=data,
                video_id=video_id, changes=changes, conflicts=conflicts)


def commit(plan, choices):
    if set(choices) != set(plan["conflicts"]) or any(v not in ("local", "current") for v in choices.values()):
        raise ValueError("Cần chọn rõ từng trường xung đột trước khi lưu")
    data = copy.deepcopy(plan["data"])
    target = next(v for v in data if str(v["id"]) == plan["video_id"])
    changes = dict(plan["changes"])
    for field, choice in choices.items():
        if choice == "local":
            changes[field] = plan["conflicts"][field]["local"]
    manual = set(target.get("manual_fields", []))
    for field, value in changes.items():
        if str(target.get(field, "") or "") != value:
            target[field] = value
            manual.add(field)
    if manual:
        target["manual_fields"] = sorted(manual)
    result = commit_manager_json(plan["path"], data, plan["version"],
                                 rolling_backup=plan["path"]+".manager.bak", label="manager-form-rebase")
    atomic_json(plan["path"]+".manager.state.json", {"sha256": result.version})
    return result


def choose_conflict(root, field, values):
    import tkinter as tk
    dialog = tk.Toplevel(root)
    dialog.title("Đối chiếu trường: "+field)
    dialog.geometry("760x580")
    answer = [None]
    tk.Label(dialog, text="Chỉ lựa chọn trường này; không thay Feed hay các trường khác.").pack(pady=8)
    for key, label in (("base", "Bản lúc mở"), ("current", "Bản mới trong database"), ("local", "Nội dung đang nhập")):
        frame = tk.LabelFrame(dialog, text=label)
        frame.pack(fill="both", expand=True, padx=10, pady=4)
        text = tk.Text(frame, height=5, wrap="word")
        bar = tk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        text.insert("1.0", values[key] if values[key] else "(Trống)")
        text.configure(state="disabled")
    def finish(choice):
        answer[0] = choice
        dialog.destroy()
    buttons = tk.Frame(dialog)
    buttons.pack(pady=10)
    for label, value in (("Giữ nội dung đang nhập", "local"), ("Giữ bản database mới", "current"), ("Hủy, chưa lưu", None)):
        tk.Button(buttons, text=label, command=lambda v=value: finish(v)).pack(side="left", padx=5)
    dialog.protocol("WM_DELETE_WINDOW", lambda: finish(None))
    dialog.transient(root); dialog.grab_set()
    root.wait_window(dialog)
    return answer[0]
