"""Read-only queue inspection with a separate explicit server retry dialog."""
import json
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from tiktok_manual_queue import queue_path

LABELS = {"prepared": "JSON chưa xác nhận", "committed": "Chờ gửi",
          "needs_review": "Cần đối chiếu JSON", "legacy_acked": "GAS từng xác nhận",
          "pending": "Đã tạo yêu cầu · chưa có ACK", "acked": "GAS từng xác nhận",
          "conflict": "GAS báo xung đột"}


def read_page(path, after=None, query="", pending_only=True, limit=100):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Invalid page limit")
    path = Path(path).resolve()
    file = queue_path(path)
    if not file.exists():
        return dict(items=[], next=None)
    position = tuple(after) if after is not None else (0, -1)
    if len(position) != 2 or any(type(n) is not int for n in position):
        raise ValueError("Invalid queue cursor")
    conflict_file = Path(str(path)+".manual_edits.conflicts.json")
    reports = {}
    if conflict_file.exists():
        for item in json.loads(conflict_file.read_text("utf-8")):
            reports.setdefault(str(item["video_id"]), []).append(item)
    items = []
    with closing(sqlite3.connect(file.as_uri()+"?mode=ro", uri=True, timeout=5)) as db:
        db.execute("BEGIN")
        if db.execute("SELECT target FROM queue_scope").fetchall() != [(os.path.normcase(str(path)),)]:
            raise ValueError("Queue thuộc database khác; không tự đổi đường dẫn")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for rowid, save_id, stamp, payload, status in db.execute(
                "SELECT rowid,op_id,created_at,payload,status FROM manual_saves WHERE rowid>=? ORDER BY rowid",
                (position[0],)):
            if pending_only and status == "legacy_acked":
                continue
            for index, change in enumerate(json.loads(payload)["changes"]):
                here = (rowid, index)
                if here <= position:
                    continue
                identifier = str(uuid.uuid5(uuid.UUID(save_id), change["video_id"]))
                upload = (db.execute("SELECT status,receipt FROM legacy_uploads WHERE op_id=?", (identifier,)).fetchone()
                          if "legacy_uploads" in tables else None)
                shown_status = upload[0] if upload else status
                if pending_only and shown_status == "acked":
                    continue
                if query.casefold() not in json.dumps(change, ensure_ascii=False).casefold():
                    continue
                if len(items) == limit:
                    return dict(items=items, next=items[-1]["position"])
                items.append(dict(position=here, save_id=save_id, operation_id=identifier,
                    created_at=stamp, video_id=change["video_id"], status=shown_status,
                    label=LABELS.get(shown_status, "Trạng thái lạ · cần kiểm tra"),
                    fields=change["fields"], base_values=change["base_values"],
                    receipt=json.loads(upload[1]) if upload and upload[1] else None,
                    prior_import_reports=reports.get(change["video_id"], [])))
    return dict(items=items, next=None)


def details(item):
    sections = [
        "Video: "+item["video_id"],
        "Trạng thái: "+item["label"],
        "Lưu local: "+item["created_at"],
        "ID mục hàng chờ: "+item["operation_id"],
        "\nGIÁ TRỊ LÚC BẮT ĐẦU SỬA",
        json.dumps(item["base_values"], ensure_ascii=False, indent=2),
        "\nNỘI DUNG ĐÃ LƯU TRONG HÀNG CHỜ",
        json.dumps(item["fields"], ensure_ascii=False, indent=2),
        "\nBIÊN NHẬN GAS ĐÃ LƯU (không phải truy vấn realtime)",
        json.dumps(item["receipt"], ensure_ascii=False, indent=2) if item["receipt"] else "Chưa có biên nhận.",
        "\nBÁO CÁO LẦN NHẬN TRƯỚC (có thể đã cũ)",
        json.dumps(item["prior_import_reports"], ensure_ascii=False, indent=2),
        "\nMàn hình chỉ đọc: không ACK, không xóa hàng chờ, không chọn bên thắng."
    ]
    return "\n".join(sections)


def show(parent, path):
    import tkinter as tk
    from tkinter import ttk
    dialog = tk.Toplevel(parent)
    dialog.title("Hàng chờ / xung đột ngày và caption PC")
    dialog.geometry("1000x720")
    tk.Label(dialog, text="Xem danh sách không sửa dữ liệu · nút Đối chiếu riêng sẽ hỏi GAS/token và xác nhận",
             fg="#946300").pack(anchor="w", padx=10, pady=8)
    bar = tk.Frame(dialog); bar.pack(fill="x", padx=10)
    term, history = tk.StringVar(), tk.BooleanVar(value=False)
    entry = tk.Entry(bar, textvariable=term); entry.pack(side="left", fill="x", expand=True)
    table = tk.Frame(dialog); table.pack(fill="x", padx=10, pady=8)
    tree = ttk.Treeview(table, columns=("video", "time", "state", "fields"), show="headings", height=9)
    for key, title, width in (("video", "Video ID", 200), ("time", "Thời điểm local", 220),
                              ("state", "Trạng thái", 260), ("fields", "Trường", 230)):
        tree.heading(key, text=title); tree.column(key, width=width)
    tree_scroll = ttk.Scrollbar(table, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=tree_scroll.set)
    tree_scroll.pack(side="right", fill="y")
    tree.pack(fill="x", expand=True)
    body = tk.Frame(dialog); body.pack(fill="both", expand=True, padx=10)
    text = tk.Text(body, wrap="word", state="disabled")
    scroll = tk.Scrollbar(body, command=text.yview); scroll.pack(side="right", fill="y")
    text.configure(yscrollcommand=scroll.set); text.pack(fill="both", expand=True)
    footer = tk.Frame(dialog); footer.pack(fill="x", padx=10, pady=8)
    status = tk.Label(footer); status.pack(side="left")
    cursors, data, following = [None], {}, [None]
    def put(value):
        text.configure(state="normal"); text.delete("1.0", "end")
        text.insert("1.0", value); text.configure(state="disabled")
    def load(reset=False):
        if reset: cursors[:] = [None]
        try:
            page = read_page(path, cursors[-1], term.get(), not history.get())
            data.clear()
            for row in tree.get_children(): tree.delete(row)
            for i, item in enumerate(page["items"]):
                key = str(i); data[key] = item
                tree.insert("", "end", iid=key, values=(item["video_id"], item["created_at"],
                                                       item["label"], ", ".join(item["fields"])))
            following[0] = page["next"]
            status.configure(text=f"Trang {len(cursors)} · {len(data)} mục")
            put("Chọn một dòng để xem đầy đủ ngày/caption và bằng chứng." if data else "Không có mục khớp bộ lọc.")
        except Exception:
            following[0] = None
            data.clear()
            for row in tree.get_children(): tree.delete(row)
            status.configure(text="Chưa đọc được dữ liệu")
            put("Chưa đọc được hàng chờ. Dữ liệu gốc được giữ nguyên; kiểm tra đúng database và quyền đọc.")
        next_button.configure(state="normal" if following[0] else "disabled")
        previous_button.configure(state="normal" if len(cursors)>1 else "disabled")
    def next_page():
        if following[0]: cursors.append(following[0]); load()
    def previous_page():
        if len(cursors)>1: cursors.pop(); load()
    previous_button = tk.Button(footer, text="Trang trước", command=previous_page); previous_button.pack(side="right")
    next_button = tk.Button(footer, text="Trang tiếp", command=next_page); next_button.pack(side="right")
    tk.Button(bar, text="Tìm / làm mới", command=lambda: load(True)).pack(side="left", padx=5)
    tk.Checkbutton(bar, text="Cả mục đã xác nhận", variable=history, command=lambda: load(True)).pack(side="left")
    def retry_selected():
        if not tree.selection():
            return
        from tiktok_manual_retry_ui import show as show_retry
        show_retry(dialog, path, data[tree.selection()[0]], on_staged=load)
    tk.Button(bar, text="Đối chiếu server / giữ PC", command=retry_selected).pack(side="left", padx=5)
    entry.bind("<Return>", lambda event: load(True))
    tree.bind("<<TreeviewSelect>>", lambda event: put(details(data[tree.selection()[0]])) if tree.selection() else None)
    load()
    return dialog
