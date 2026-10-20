"""Main-thread JSON refresh which never replaces an unsaved editor/model."""
import hashlib
import json
from pathlib import Path
from tiktok_database_guard import snapshot


def model_hash(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


class ManagerRefresh:
    def __init__(self):
        self.path = None
        self.signature = None
        self.baseline = None

    def reset(self, app):
        self.path = str(Path(app.current_file).resolve())
        self.signature = None
        self.baseline = model_hash(app.data_list)

    def dirty(self, app):
        if self.baseline is None or model_hash(app.data_list) != self.baseline:
            return True  # Includes failed saves that already mutated the model.
        selected = app.data.get(getattr(app, "selected_vid", None))
        if not selected:
            return False
        fields = dict(getattr(app, "extra_entries", {}))
        fields.update(repost_date=app.entry_repost_date, repost_caption=app.entry_repost_cap)
        # Do not strip: even unfinished whitespace is user input, not permission
        # to replace the form with a remote version.
        return any(entry.get() != str(selected.get(key, "") or "") for key, entry in fields.items())

    def poll(self, app, dialog_open=False):
        if not app.current_file or self.path != str(Path(app.current_file).resolve()):
            return "inactive"
        stat = Path(app.current_file).stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        if self.signature == signature:
            return "unchanged"
        current = snapshot(app.current_file)
        if current.version == app.loaded_version:
            self.signature = signature
            return "unchanged"
        if dialog_open or self.dirty(app):
            return "pending"  # Do not advance signature; reconsider once clean.
        selected = getattr(app, "selected_vid", None)
        scroll = app.tree.yview()
        old_version = app.loaded_version
        names = ("data", "data_list", "filtered_list", "current_file", "username", "loaded_version", "selected_vid")
        previous = {name: getattr(app, name) for name in names if hasattr(app, name)}
        previous_guard = (self.path, self.signature, self.baseline)
        loaded = app.load_path(app.current_file, notify=False, silent_errors=True)
        if loaded is False or app.loaded_version == old_version:
            for name, value in previous.items():
                setattr(app, name, value)
            self.path, self.signature, self.baseline = previous_guard
            return "failed"
        # load_path resets the baseline. Never mark a post-read file stat as
        # already seen: another writer may have replaced it during refresh.
        self.signature = None
        restored = False
        if selected in app.data:
            for item in app.tree.get_children():
                if app.tree.item(item, "tags")[0] == selected:
                    app.tree.selection_set(item)
                    app.on_select_item(None)
                    restored = True
                    break
        if not restored:
            app.entry_repost_date.delete(0, "end")
            app.entry_repost_cap.delete(0, "end")
            for entry in getattr(app, "extra_entries", {}).values():
                entry.delete(0, "end")
        if scroll:
            app.tree.yview_moveto(scroll[0])
        return "refreshed"
