"""PC manager adapter for the per-field manual journal (no network/cutover).

Date/caption retain the public JSON + legacy queue path. Time, labels and notes
stay in the adjacent SQLite ledger; only the converged current time/labels are
eligible for an explicit HTML export. Notes, revisions and conflicts remain
private. Hub transport is an explicit, separate --manual-field-journal option
requiring upgraded server/clients.
"""
import copy
import json
import sqlite3
import sys
from pathlib import Path

from tiktok_database_guard import database_lock, snapshot
from tiktok_manual_edits import profile_for


def implementation():
    directory = Path(__file__).resolve().parent / 'mobile_capture'
    if not (directory / 'manual_fields.py').is_file():
        directory = Path('D:/tiktok/tiktok-repost/V4-2026/mobile_capture')
    if not (directory / 'manual_fields.py').is_file():
        raise ValueError('Thiếu module manual_fields.py; không lưu thiếu journal. Cài đủ bộ PC mới.')
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    import manual_fields
    return manual_fields


class ManagerJournal:
    def __init__(self, database, version):
        self.database = Path(database).resolve()
        self.path = Path(str(self.database) + '.manual-fields.sqlite')
        module = implementation()
        self.ledger = module.Journal(self.path, profile_for(self.database), 'main')
        try:
            with database_lock(self.database):
                if snapshot(self.database).version != version:
                    raise ValueError('Database đổi trong lúc mở; mở lại trước khi sửa.')
                self.ledger.recover_json_intents(self.database, version)
                # One scan, not five queries per video in a 43k-row database.
                events = self.ledger.export()['events']
                replaced = {parent for event in events for parent in event['base_revision']}
                self.bases = {}
                for event in events:
                    if event['op_id'] not in replaced:
                        self.bases.setdefault((event['video_id'], event['field']), []).append(event['op_id'])
                self.bases = {key: sorted(value) for key, value in self.bases.items()}
        except BaseException:
            self.close()
            raise

    def close(self):
        self.ledger.close()

    def selected_bases(self, video_id):
        # Revisions captured when the displayed JSON was loaded, not whatever
        # happened to arrive while the user was typing in an older form.
        return {field: list(self.bases.get((str(video_id), field), [])) for field in implementation().FIELDS}

    def conflicts(self, video_id=None):
        return self.ledger.conflicts(video_id)

    def display_values(self, fields=('repost_time', 'tags', 'notes')):
        """Build one read-only display index for the PC manager.

        The 43k-row manager must not execute three SQLite history queries for
        every visible video.  Scan the immutable event payloads once, retain
        only causal heads, and expose converged values plus explicit conflict
        markers.  Public export may explicitly select only converged current
        repost_time/tags; notes and all revision metadata remain private.
        """
        module = implementation()
        wanted = tuple(fields)
        if (not wanted or len(set(wanted)) != len(wanted) or
                any(field not in module.FIELDS for field in wanted)):
            raise ValueError('Invalid manual display fields')
        grouped = {}
        replaced = set()
        for (payload,) in self.ledger.db.execute(
                'SELECT payload FROM field_events ORDER BY rowid'):
            event = json.loads(payload)
            if event['field'] in wanted:
                grouped.setdefault((event['video_id'], event['field']), []).append(event)
            replaced.update(event['base_revision'])
        result = {}
        for (video_id, field), events in grouped.items():
            heads = [event for event in events if event['op_id'] not in replaced]
            encoded = {module.canonical(event['value']) for event in heads}
            item = result.setdefault(video_id, {})
            item[field] = copy.deepcopy(heads[0]['value']) if len(encoded) == 1 else None
            item[field + '_heads'] = [event['op_id'] for event in heads]
            if len(encoded) > 1:
                item.setdefault('manual_conflicts', []).append(field)
        for item in result.values():
            item['manual_conflicts'] = sorted(item.get('manual_conflicts', []))
        return result

    def resolve(self, video_id, field, value, observed_heads, version=None, **options):
        """Resolve only the exact alternatives displayed by the PC dialog."""
        module = implementation(); video_id = str(video_id); receipt = {}
        if field in module.PUBLIC_FIELDS:
            if version is None:
                raise ValueError('Public conflict resolution needs the loaded MAIN version')
            committed = module.resolve_manager_conflict(
                self.database, version, self.ledger, video_id, field, value,
                list(observed_heads), manual_receipt=receipt, **options)
        elif field in module.PRIVATE_FIELDS:
            operation = self.ledger.resolve(video_id, field, value, list(observed_heads))
            receipt['events'] = ([] if operation is None else
                                 [next(event for event in self.ledger.state(video_id, field)['versions']
                                       if event['op_id'] == operation)])
            committed = None
        else:
            raise ValueError('Unknown manual field')
        for event in receipt.get('events', []):
            self.bases[(event['video_id'], event['field'])] = [event['op_id']]
        return dict(snapshot=committed, events=receipt.get('events', []),
                    idempotent=bool(receipt.get('idempotent')))

    def save(self, videos, version, *, source='pc_manager', private_updates=None, **options):
        module = implementation()
        rows = list(videos.values()) if isinstance(videos, dict) else videos
        keys = {(str(row['id']), field) for row in rows for field in module.PUBLIC_FIELDS
                if field in row.get('manual_fields', [])}
        keys.update((private_updates or {}).keys())
        bases = {key: list(self.bases.get(key, [])) for key in keys}
        receipt = {}
        committed = module.commit_manager_fields(self.database, videos, version, self.ledger, bases,
                    source=source, private_updates=private_updates, manual_receipt=receipt, **options)
        # Advance only versions produced by THIS successful save. A remote
        # concurrent branch remains unseen, so later saves cannot erase it.
        for event in receipt.get('events', []):
            self.bases[(event['video_id'], event['field'])] = [event['op_id']]
        return committed


def public_display_values(database, fields=('repost_time', 'tags')):
    """Read the converged web-safe projection without taking a writer lock.

    Export is a snapshot reader: it must not recover intents, create tables or
    leave a ``.writer.lock`` beside MAIN. SQLite's read-only connection observes
    one consistent database/WAL snapshot while a writer may continue normally.
    """
    database = Path(database).resolve()
    path = Path(str(database) + '.manual-fields.sqlite')
    if not path.is_file():
        return {}
    module = implementation()
    wanted = tuple(fields)
    if (not wanted or len(set(wanted)) != len(wanted) or
            any(field not in ('repost_time', 'tags') for field in wanted)):
        raise ValueError('Invalid public manual fields')
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        connection.execute('PRAGMA query_only=ON')
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        allowed = {'field_scope', 'field_events', 'field_drafts', 'field_json_intents'}
        if not {'field_scope', 'field_events'} <= tables or tables - allowed:
            raise ValueError('Not a manual-field journal; public export stopped')
        scopes = connection.execute(
            'SELECT profile,workspace,device_id FROM field_scope').fetchall()
        if len(scopes) != 1 or scopes[0][:2] != (profile_for(database), 'main'):
            raise ValueError('Manual journal belongs to another public-export scope')

        grouped = {}
        replaced = set()
        for (payload,) in connection.execute(
                'SELECT payload FROM field_events ORDER BY rowid'):
            event = json.loads(payload)
            module.validate(event, scopes[0][0], scopes[0][1])
            if event['field'] in wanted:
                grouped.setdefault((event['video_id'], event['field']), []).append(event)
                replaced.update(event['base_revision'])
        result = {}
        for (video_id, field), events in grouped.items():
            heads = [event for event in events if event['op_id'] not in replaced]
            encoded = {module.canonical(event['value']) for event in heads}
            item = result.setdefault(video_id, {})
            item[field] = copy.deepcopy(heads[0]['value']) if len(encoded) == 1 else None
            if len(encoded) > 1:
                item.setdefault('manual_conflicts', []).append(field)
        for item in result.values():
            item['manual_conflicts'] = sorted(item.get('manual_conflicts', []))
        return result
    finally:
        connection.close()


def install_session(app, database, version):
    old = getattr(app, 'manual_field_session', None)
    app.manual_field_session = None
    if old is not None:
        old.close()
    # If the new file or ledger cannot open, the old file's writer must never
    # stay attached to the new UI model. Save stays disabled until a good load.
    app.manual_field_session = ManagerJournal(database, version)


def private_editor(app):
    """Open a separate private editor; cancellation/typing is never published."""
    import tkinter as tk
    from tkinter import ttk, messagebox
    from datetime import datetime, timezone, timedelta
    if not getattr(app, 'selected_vid', None):
        messagebox.showinfo('Chọn video', 'Chọn một video trước khi sửa giờ, nhãn hoặc ghi chú.'); return
    session = getattr(app, 'manual_field_session', None)
    if session is None:
        messagebox.showerror('Chưa mở journal', 'Mở lại database trước khi sửa.'); return
    video_id = str(app.selected_vid)
    states = {field: session.ledger.state(video_id, field) for field in ('repost_time', 'tags', 'notes')}
    if any(state['conflict'] for state in states.values()):
        messagebox.showwarning('Cần đối chiếu', 'Có nhiều phiên bản nhập tay. Không tự ghi đè; giữ journal để đối chiếu.'); return
    # Modal captures each exact displayed revision; background receipt changes
    # never silently refresh this baseline.
    observed = {field: list(state['base_revision']) for field, state in states.items()}
    dialog = tk.Toplevel(app.root); dialog.title('Giờ / nhãn xuất web · ghi chú riêng · '+video_id)
    dialog.transient(app.root); dialog.geometry('720x650'); dialog.grab_set()
    ttk.Label(dialog, text='Lưu vào journal local. Giờ/nhãn hiện hành sẽ xuất web khi tạo HTML; ghi chú, lịch sử và xung đột không xuất GitHub.\nChưa phải xác nhận Drive đã nhận.\n'+str(session.path), wraplength=680).pack(anchor='w', padx=14, pady=10)
    stamp = states['repost_time']['value'] or {}
    mode = tk.StringVar(value=stamp.get('kind', 'none'))
    ttk.Label(dialog, text='Giờ repost: xuất web khi đã hội tụ; không đổi ngày nhật ký').pack(anchor='w', padx=14)
    ttk.Combobox(dialog, textvariable=mode, values=('none', 'exact', 'range'), state='readonly').pack(fill='x', padx=14)
    now = datetime.now(timezone(timedelta(hours=7))).isoformat(timespec='seconds')
    lower = tk.StringVar(value=stamp.get('at', stamp.get('lower', now)))
    upper = tk.StringVar(value=stamp.get('upper', now))
    ttk.Label(dialog, text='Mốc chính / đầu khoảng (ví dụ 2026-10-21T01:02:03+07:00)').pack(anchor='w', padx=14, pady=(8,0))
    ttk.Entry(dialog, textvariable=lower).pack(fill='x', padx=14)
    ttk.Label(dialog, text='Cuối khoảng (chỉ dùng khi chọn range)').pack(anchor='w', padx=14, pady=(8,0))
    ttk.Entry(dialog, textvariable=upper).pack(fill='x', padx=14)
    ttk.Label(dialog, text='Nhãn — ngăn cách bằng dấu phẩy').pack(anchor='w', padx=14, pady=(10,0))
    tags = tk.StringVar(value=', '.join(states['tags']['value'] or []))
    ttk.Entry(dialog, textvariable=tags).pack(fill='x', padx=14)
    ttk.Label(dialog, text='Ghi chú riêng').pack(anchor='w', padx=14, pady=(10,0))
    notes = tk.Text(dialog, wrap='word', height=9); notes.insert('1.0', states['notes']['value'] or '')
    notes.pack(fill='both', expand=True, padx=14)
    def save():
        try:
            # Refuse an older main model; no implicit rebase of unrelated edits.
            if snapshot(app.current_file).version != app.loaded_version:
                raise ValueError('Database đã đổi; giữ nội dung form và mở lại database trước khi lưu.')
            value = None
            if mode.get() == 'exact': value = dict(kind='exact', zone='Asia/Bangkok', at=lower.get().strip())
            elif mode.get() == 'range': value = dict(kind='range', zone='Asia/Bangkok', lower=lower.get().strip(), upper=upper.get().strip())
            updates = {(video_id, 'repost_time'): value, (video_id, 'tags'): sorted({tag.strip() for tag in tags.get().split(',') if tag.strip()}),
                       (video_id, 'notes'): notes.get('1.0', 'end-1c')}
            module = implementation()
            bases = {(video_id, field): observed[field] for field in observed}
            # All three private values share one durable intent. The public
            # JSON bytes are loaded directly and never carry these values.
            current = snapshot(app.current_file)
            public = json.loads(current.data.decode('utf-8-sig'))
            receipt = {}
            result = module.commit_manager_fields(app.current_file, public, current.version, session.ledger, bases,
                       source='pc_editor', private_updates=updates, manual_receipt=receipt)
            for event in receipt.get('events', []):
                session.bases[(event['video_id'], event['field'])] = [event['op_id']]
            app.loaded_version = result.version
            dialog.destroy()
            messagebox.showinfo('Đã lưu journal local', 'Đã giữ giờ/nhãn/ghi chú trong journal. Giờ/nhãn hiện hành sẽ vào lần xuất HTML kế tiếp; ghi chú vẫn riêng tư. Ngày repost không đổi.\nChưa xác nhận Drive; transport mới chỉ bật sau khi nâng Hub và tất cả APK.', parent=app.root)
        except Exception as error:
            messagebox.showerror('Chưa lưu — giữ nội dung form', str(error), parent=dialog)
    ttk.Button(dialog, text='Lưu vào journal PC', command=save).pack(pady=12)
    return dialog


def _resolution_text(field, value):
    if field in ('repost_time', 'tags'):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return value or ''


def _parse_resolution(field, text):
    module = implementation()
    if field == 'repost_time':
        value = None if not text.strip() or text.strip() == 'null' else json.loads(text)
    elif field == 'tags':
        value = json.loads(text)
    else:
        value = text
    module.validate_value(field, value)
    return value


def conflict_editor(app):
    """Readable, explicit resolver for public and private per-field branches."""
    import tkinter as tk
    from tkinter import ttk, messagebox
    session = getattr(app, 'manual_field_session', None)
    if session is None:
        messagebox.showerror('Chưa mở journal', 'Mở lại database trước khi đối chiếu.'); return
    selected_video = str(app.selected_vid) if getattr(app, 'selected_vid', None) else None
    only_selected = tk.BooleanVar(value=bool(selected_video))
    dialog = tk.Toplevel(app.root); dialog.title('Đối chiếu xung đột nhập tay')
    dialog.transient(app.root); dialog.geometry('980x700'); dialog.grab_set()
    ttk.Label(dialog, text=('Chọn rõ một phương án hoặc nhập giá trị hợp nhất. '
                            'Không tự chọn theo thời gian. Nếu có bản mới đến, thao tác sẽ bị từ chối.'),
              wraplength=930).pack(anchor='w', padx=12, pady=10)
    body = ttk.Panedwindow(dialog, orient='horizontal'); body.pack(fill='both', expand=True, padx=12)
    left = ttk.Frame(body); right = ttk.Frame(body); body.add(left, weight=2); body.add(right, weight=3)
    tree = ttk.Treeview(left, columns=('video', 'field', 'heads'), show='headings', height=18)
    for key, title, width in (('video','Video',180),('field','Trường',120),('heads','Phương án',75)):
        tree.heading(key, text=title); tree.column(key, width=width, anchor='w')
    tree.pack(fill='both', expand=True)
    details = tk.Text(right, wrap='word', height=15, state='disabled'); details.pack(fill='both', expand=True)
    choice = tk.StringVar()
    combo = ttk.Combobox(right, textvariable=choice, state='readonly'); combo.pack(fill='x', pady=(8,4))
    editor = tk.Text(right, wrap='word', height=8); editor.pack(fill='both', expand=True)
    scope = ttk.Label(dialog, text=''); scope.pack(anchor='w', padx=12, pady=(6,0))
    loaded = {}

    def select(_event=None):
        selected = tree.selection()
        if not selected: return
        conflict = loaded[selected[0]]
        lines = []
        labels = []
        for index, event in enumerate(conflict['alternatives'], 1):
            source = event['source']
            labels.append(f"{index}. {source['kind']} · {event['created_at']} · {event['op_id'][:8]}")
            lines.extend((f"PHƯƠNG ÁN {index}", f"Giá trị: {_resolution_text(conflict['field'], event['value'])}",
                          f"Nguồn: {source['kind']} · thiết bị {source['device_id']}",
                          f"Op: {event['op_id']}", f"Base value: {json.dumps(event['base_value'], ensure_ascii=False)}",
                          f"Base revisions: {', '.join(event['base_revision']) or '(gốc)'}", ''))
        details.config(state='normal'); details.delete('1.0','end'); details.insert('1.0','\n'.join(lines)); details.config(state='disabled')
        combo.config(values=labels); choice.set(labels[0] if labels else '')
        editor.delete('1.0','end')
        if conflict['alternatives']:
            editor.insert('1.0', _resolution_text(conflict['field'], conflict['alternatives'][0]['value']))

    def use_choice(_event=None):
        selected = tree.selection()
        if not selected or not choice.get(): return
        conflict = loaded[selected[0]]
        try: index = list(combo['values']).index(choice.get())
        except ValueError: return
        editor.delete('1.0','end')
        editor.insert('1.0', _resolution_text(conflict['field'], conflict['alternatives'][index]['value']))

    combo.bind('<<ComboboxSelected>>', use_choice)
    tree.bind('<<TreeviewSelect>>', select)

    def refresh():
        loaded.clear()
        for item in tree.get_children(): tree.delete(item)
        scope_video = selected_video if only_selected.get() else None
        conflicts = session.conflicts(scope_video)
        for index, conflict in enumerate(conflicts):
            key = 'conflict-' + str(index); loaded[key] = conflict
            tree.insert('', 'end', iid=key, values=(conflict['video_id'], conflict['field'], len(conflict['alternatives'])))
        scope.config(text=(f"{len(conflicts)} xung đột" +
                          (f" của video {selected_video}" if scope_video else ' trong toàn bộ journal')))
        if conflicts:
            first = next(iter(loaded)); tree.selection_set(first); tree.focus(first); select()
        else:
            details.config(state='normal'); details.delete('1.0','end'); details.insert('1.0','Không còn xung đột trong phạm vi này.'); details.config(state='disabled')

    def resolve():
        selected = tree.selection()
        if not selected: return
        conflict = loaded[selected[0]]; text = editor.get('1.0','end-1c')
        try:
            value = _parse_resolution(conflict['field'], text)
            if not messagebox.askyesno('Xác nhận đối chiếu',
                    f"Hợp nhất đủ {len(conflict['base_revision'])} phương án của {conflict['field']}?\n"
                    'Lịch sử vẫn được giữ; thao tác không chọn theo timestamp.', parent=dialog):
                return
            result = session.resolve(conflict['video_id'], conflict['field'], value,
                                     list(conflict['base_revision']), getattr(app, 'loaded_version', None),
                                     rolling_backup=str(session.database)+'.manager.bak', label='manual-resolution')
            committed = result['snapshot']
            if committed is not None:
                raw = json.loads(committed.data.decode('utf-8-sig'))
                app.data_list = list(raw.values()) if isinstance(raw, dict) else raw
                app.data = {str(video['id']): video for video in app.data_list}
                app.loaded_version = committed.version
                if hasattr(app, 'refresh_tree'): app.refresh_tree()
                if getattr(app, 'selected_vid', None) == conflict['video_id'] and hasattr(app, 'on_select_item'):
                    # Keep selection/model coherent; form refresh is optional in
                    # lightweight tests and older manager builds.
                    try: app.on_select_item(None)
                    except (KeyError, IndexError): pass
            messagebox.showinfo('Đã đối chiếu', 'Đã tạo explicit_resolution nối đủ các đầu đang hiển thị. Lịch sử cũ vẫn còn.', parent=dialog)
            refresh()
        except Exception as error:
            messagebox.showerror('Chưa hợp nhất — hãy làm mới', str(error), parent=dialog)

    controls = ttk.Frame(dialog); controls.pack(fill='x', padx=12, pady=10)
    ttk.Checkbutton(controls, text='Chỉ video đang chọn', variable=only_selected,
                    command=refresh, state=('normal' if selected_video else 'disabled')).pack(side='left', padx=(0,10))
    ttk.Button(controls, text='Làm mới danh sách', command=refresh).pack(side='left')
    ttk.Button(controls, text='Xác nhận hợp nhất', command=resolve).pack(side='right')
    refresh()
    return dialog
