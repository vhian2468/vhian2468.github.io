import os
import shutil
import subprocess
import signal
import sys
import json
import tempfile
import threading
from datetime import datetime
import traceback
import hashlib
import time
from contextlib import ExitStack
from tiktok_database_guard import database_lock, snapshot, commit_bytes, validate_bytes, local_authority_is_current
import tiktok_feed_authority as feed_state

# Reconciliation implementation lives with the collector project so the
# GitHub export folder stays free of private queue/runtime files.
_V4_MODULES = r"D:\tiktok\tiktok-repost\V4-2026"
if _V4_MODULES not in sys.path:
    sys.path.insert(0, _V4_MODULES)
import tiktok_publish_session as publish_sessions

_CURRENT_VERSION = object()

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

# ================= CẤU HÌNH ĐƯỜNG DẪN =================
DIR_GIT = r"C:\Users\Viet Hoang Nguyen\OneDrive\Documents\GitHub\vhian2468.github.io"
DIR_V4 = r"D:\tiktok\tiktok-repost\V4-2026"

FILE_JSON = "data_pe.siro_phan_all.json"
FILE_JSON_BACKUP1 = "data_pe.siro_phan_all.json.bak1"
FILE_JSON_BACKUP2 = "data_pe.siro_phan_all.json.bak2"

PATH_GIT_JSON = os.path.join(DIR_GIT, FILE_JSON)
PATH_GIT_BACKUP1 = os.path.join(DIR_GIT, FILE_JSON_BACKUP1)
PATH_GIT_BACKUP2 = os.path.join(DIR_GIT, FILE_JSON_BACKUP2)
PATH_V4_JSON = os.path.join(DIR_V4, FILE_JSON)
FILE_JSON_PIPELINE = FILE_JSON + ".pipeline.json"
PATH_GIT_PIPELINE = os.path.join(DIR_GIT, FILE_JSON_PIPELINE)
COLLECTOR_SCRIPT = "test_api_log - V2.py"
COLLECTOR_STATUS_PATH = os.path.join(DIR_V4, "collector_status.json")
RUN_LOG_DIR = os.path.join(DIR_V4, "run_logs")
PUBLISH_SESSIONS_DIR = os.path.join(DIR_V4, "publish_sessions")
PIPELINE_STATUS_PATH = os.path.join(DIR_V4, 'pipeline_status.json')
CURRENT_STEP = 'startup'


def mark_step(name, state='running', **details):
    global CURRENT_STEP
    CURRENT_STEP = name
    record = {'step': name, 'state': state, 'timestamp': datetime.now().isoformat(), 'pid': os.getpid(), **details}
    temporary = PIPELINE_STATUS_PATH + f'.{os.getpid()}.tmp'
    with open(temporary, 'w', encoding='utf-8') as file:
        json.dump(record, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, PIPELINE_STATUS_PATH)
    print(f'[{record["timestamp"]}] STEP={name} STATE={state}')


def run_logged_step(name, command, cwd):
    mark_step(name)
    environment = os.environ.copy()
    environment['PYTHONIOENCODING'] = 'utf-8'
    child = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding='utf-8', errors='replace', env=environment)
    print(f'{name}: PID={child.pid}')
    try:
        pump_child_output(child)
        code = child.wait()
    except BaseException:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=15)
        raise
    print(f'{name}: exit_code={code}')
    if code:
        raise RuntimeError(f'Bước {name} lỗi exit code={code}; xem traceback/output ngay trên run log')
    mark_step(name, 'complete', exit_code=code)

# ======================================================

class TeeWriter:
    """Ghi dong thoi ra console va file log, flush ngay de khong mat log khi crash."""

    def __init__(self, console, log_file, lock):
        self.console = console
        self.log_file = log_file
        self.lock = lock
        self.encoding = getattr(console, "encoding", "utf-8")

    def write(self, text):
        if not text:
            return 0
        with self.lock:
            self.log_file.write(text)
            self.log_file.flush()
            try:
                self.console.write(text)
                self.console.flush()
            except (OSError, ValueError):
                pass
        return len(text)

    def flush(self):
        with self.lock:
            self.console.flush()
            self.log_file.flush()

    def isatty(self):
        return self.console.isatty()


def setup_run_logging():
    os.makedirs(RUN_LOG_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(RUN_LOG_DIR, f"auto_tiktok_repost_{timestamp}.log")
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    lock = threading.RLock()
    sys.stdout = TeeWriter(original_stdout, log_file, lock)
    sys.stderr = TeeWriter(original_stderr, log_file, lock)
    return log_path, log_file, original_stdout, original_stderr


def pump_child_output(process):
    """Chuyen output collector ra console va run log theo thoi gian thuc."""
    if process.stdout is None:
        return
    try:
        while True:
            chunk = process.stdout.readline()
            if chunk == "":
                break
            sys.stdout.write(chunk)
    finally:
        process.stdout.close()

def validate_json_file(path, label="JSON"):
    """Doc va kiem tra file truoc khi no duoc dung de ghi de noi khac."""
    if not os.path.isfile(path):
        raise RuntimeError(f"{label} không tồn tại: {path}")

    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except Exception as exc:
        raise RuntimeError(f"{label} không phải JSON hợp lệ: {path} ({exc})") from exc

    if not isinstance(data, list):
        raise RuntimeError(f"{label} phải có format List, nhận được {type(data).__name__}")

    seen_ids = set()
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise RuntimeError(f"{label}[{index}] không phải Object")
        video_id = str(item.get("id", "")).strip()
        if not video_id:
            raise RuntimeError(f"{label}[{index}] thiếu id")
        if video_id in seen_ids:
            raise RuntimeError(f"{label} có ID trùng: {video_id}")
        seen_ids.add(video_id)
    return len(data)


def atomic_copy_json(source, destination, label="JSON", *,
                     expected_destination=_CURRENT_VERSION, expected_source=_CURRENT_VERSION,
                     backup=False):
    """Copy one coherent source; CAS protects a destination edited during a step."""
    paths = sorted({os.path.normcase(os.path.realpath(p)) for p in (source, destination)})
    with ExitStack() as locks:
        for path in paths:
            locks.enter_context(database_lock(path))
        captured = snapshot(source)
        if captured.data is None:
            raise RuntimeError(f"{label} nguồn không tồn tại: {source}")
        if expected_source is not _CURRENT_VERSION and captured.version != expected_source:
            raise RuntimeError(f"{label}: nguồn đã thay đổi sau kiểm tra; dừng, không ghi đè đích")
        try:
            rows = validate_bytes(captured.data)
            if not isinstance(rows, list):
                raise ValueError("JSON phải là List")
        except ValueError as error:
            raise RuntimeError(f"{label} nguồn không hợp lệ") from error
        target = snapshot(destination)
        authority, _ = feed_state.inspect(source, captured.version)
        target_authority, _ = feed_state.inspect(destination, target.version)
        if authority is not None and not feed_state.is_current(authority, target_authority):
            raise RuntimeError(f"{label}: nguồn thuộc phiên Feed cũ hơn đích; đối chiếu trước, không ghi đè")
        if authority is None and target_authority is not None and target.data is not None:
            old_rows = validate_bytes(target.data)
            old_rows = list(old_rows.values()) if isinstance(old_rows, dict) else old_rows
            if [str(row["id"]) for row in old_rows] != [str(row["id"]) for row in rows]:
                raise RuntimeError(f"{label}: nguồn không có mốc phiên Feed và khác thứ tự đích đã xác minh; cần hợp nhất")
        expected = target.version if expected_destination is _CURRENT_VERSION else expected_destination
        commit_bytes(destination, captured.data, expected,
                     rolling_backup=str(destination) + ".pipeline.bak" if backup else None, label=label,
                     feed_authority=authority)
        return len(rows)


def rotate_main_backups_after_preflight():
    """Rotate rolling MAIN backups only when MAIN has new bytes.

    The caller must first create the durable publish-session.  Source versions
    are pinned here as an additional fail-closed check against a concurrent
    writer between preflight and backup rotation.
    """
    with database_lock(PATH_GIT_JSON):
        current = snapshot(PATH_GIT_JSON)
    if current.data is None or current.version is None:
        raise RuntimeError("MAIN không tồn tại sau preflight; không chạy collector")

    first = snapshot(PATH_GIT_BACKUP1)
    if first.version == current.version:
        print("   ℹ️ MAIN không đổi; giữ nguyên bak1/bak2.")
        return False
    if first.data is not None:
        atomic_copy_json(
            PATH_GIT_BACKUP1,
            PATH_GIT_BACKUP2,
            "Backup bak2",
            expected_source=first.version,
            backup=True,
        )
        print("   ✅ Đã backup: bak1 -> bak2")
    atomic_copy_json(
        PATH_GIT_JSON,
        PATH_GIT_BACKUP1,
        "Backup bak1",
        expected_source=current.version,
        backup=True,
    )
    if snapshot(PATH_GIT_BACKUP1).version != current.version:
        raise RuntimeError("Backup bak1 đọc lại không khớp MAIN; không chạy collector")
    print("   ✅ Đã backup atomic: json hiện tại -> bak1")
    return True


def preflight_exact_collection_seed():
    """Read-only proof that collector starts from exact MAIN bytes/Feed state."""
    paths = sorted(
        {os.path.normcase(os.path.realpath(path)) for path in (PATH_GIT_JSON, PATH_V4_JSON)}
    )
    with ExitStack() as locks:
        for path in paths:
            locks.enter_context(database_lock(path))
        main = snapshot(PATH_GIT_JSON)
        collector = snapshot(PATH_V4_JSON)
        if main.data is None or collector.data is None or main.data != collector.data:
            raise RuntimeError("Collector seed không phải exact MAIN; không xoay backup")
        # Validate bytes before any backup generation can be consumed.
        for label, value in (("MAIN", main.data), ("V4", collector.data)):
            rows = validate_bytes(value)
            if not isinstance(rows, list):
                raise RuntimeError(f"{label} seed phải là List")
        main_authority, _ = feed_state.inspect(PATH_GIT_JSON, main.version)
        collector_authority, _ = feed_state.inspect(PATH_V4_JSON, collector.version)
        if main_authority != collector_authority:
            raise RuntimeError("Collector seed có Feed authority khác MAIN")
        return main.version


def prepare_publish_session(run_id):
    """Preflight, rotate, then durably pin the session before collector Popen."""
    preflight_exact_collection_seed()
    # No PREPARED exists yet. A rotation failure is therefore retryable without
    # leaving an unfinished session that blocks the next startup.
    rotate_main_backups_after_preflight()
    return publish_sessions.prepare_session(
        sessions_root=PUBLISH_SESSIONS_DIR,
        run_id=run_id,
        destination_path=PATH_GIT_JSON,
        collector_path=PATH_V4_JSON,
        require_database_sha256=True,
    )


def unresolved_publish_run_ids():
    """Return PREPARED sessions which do not have a durable COMPLETE receipt."""
    unfinished = publish_sessions.discover_unfinished_sessions(
        sessions_root=PUBLISH_SESSIONS_DIR,
    )
    return [item["run_id"] for item in unfinished]


def _require_status_database_sha256(status):
    expected = str(status.get("database_sha256", "")).lower()
    with database_lock(PATH_V4_JSON):
        actual = snapshot(PATH_V4_JSON).version
    if len(expected) != 64 or expected != actual:
        raise RuntimeError(
            "collector_status thiếu/sai database_sha256; giữ nguyên cả hai, không publish"
        )


def _process_is_alive(pid):
    """Read-only liveness check; permission errors fail closed as alive."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise RuntimeError("collector_status thiếu PID hợp lệ; không đóng session")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = open_process(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            error = ctypes.get_last_error()
            if error in (87, 1168):  # invalid PID / element not found
                return False
            if error == 5:  # access denied: process may exist, so fail closed
                return True
            raise OSError(error, "Không xác minh được collector PID")
        try:
            code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(code)):
                raise OSError(ctypes.get_last_error(),
                              "Không đọc được trạng thái collector PID")
            return code.value == 259  # STILL_ACTIVE
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def resume_saved_publish_at_startup(run_id):
    """Finish a proven saved session before allowing a new collector run."""
    try:
        with open(COLLECTOR_STATUS_PATH, "rb") as file:
            status_bytes = file.read()
        status = json.loads(status_bytes.decode("utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise RuntimeError(
            "V4 có dữ liệu chưa publish nhưng thiếu collector_status hợp lệ; "
            "giữ nguyên cả hai, không chạy collector mới"
        ) from error
    status_run_id = str(status.get("run_id", "")).strip()
    if status_run_id != run_id:
        raise RuntimeError(
            "Publish-session dở không khớp collector receipt saved; "
            "giữ nguyên cả hai, không chạy collector mới"
        )
    if status.get("status") != "saved":
        try:
            state = publish_sessions.session_state(
                sessions_root=PUBLISH_SESSIONS_DIR,
                run_id=run_id,
            )
            if state["state"] == "aborted_no_change":
                return state
            if state["state"] != "prepared":
                raise publish_sessions.PublishSessionError(
                    "Chỉ PREPARED mới được đóng no-change")
            collector_pid = status.get("pid")
            if _process_is_alive(collector_pid):
                raise RuntimeError(
                    "Collector của publish-session vẫn đang chạy; không mở phiên mới"
                )
            result = publish_sessions.abort_no_change_session(
                sessions_root=PUBLISH_SESSIONS_DIR,
                run_id=run_id,
                collector_status_path=COLLECTOR_STATUS_PATH,
                expected_status_sha256=hashlib.sha256(status_bytes).hexdigest(),
                expected_collector_pid=collector_pid,
            )
        except publish_sessions.PublishSessionError as error:
            raise RuntimeError(
                "Collector chưa có receipt saved và V4/Feed không chứng minh exact base; "
                "giữ nguyên cả hai, không chạy collector mới"
            ) from error
        except RuntimeError:
            raise
        print(
            "   ✅ Đã đóng publish-session collector chết trước save: "
            f"{run_id} · V4 exact base · không publish dữ liệu"
        )
        return result
    _require_status_database_sha256(status)
    try:
        state = publish_sessions.session_state(
            sessions_root=PUBLISH_SESSIONS_DIR,
            run_id=run_id,
        )
        if state["state"] == "prepared":
            publish_sessions.record_saved(
                sessions_root=PUBLISH_SESSIONS_DIR,
                run_id=run_id,
                collector_status_path=COLLECTOR_STATUS_PATH,
            )
        result = publish_sessions.publish_session(
            sessions_root=PUBLISH_SESSIONS_DIR,
            run_id=run_id,
            collector_status_path=COLLECTOR_STATUS_PATH,
        )
    except publish_sessions.PublishSessionError as error:
        raise RuntimeError(
            "Không chứng minh được phiên V4 dở dang thuộc exact MAIN; "
            "giữ nguyên cả hai, không chạy collector mới"
        ) from error
    print(
        "   ✅ Đã phục hồi publish-session trước khi chạy phiên mới: "
        f"{result['count']} videos · Feed={result['feed_source']}"
    )
    return result


def resume_unresolved_publish_sessions_at_startup():
    """Resolve the sole proven interrupted run before any base selection."""
    run_ids = unresolved_publish_run_ids()
    if not run_ids:
        return None
    if len(run_ids) != 1:
        raise RuntimeError(
            f"Có {len(run_ids)} publish-session dở; cần đối chiếu, không chạy collector mới"
        )
    return resume_saved_publish_at_startup(run_ids[0])


def seed_collector_from_main(*, git_count, v4_count, git_version, v4_version,
                             git_authority, v4_authority):
    """Seed V4 from verified MAIN after all unfinished sessions are resolved."""
    if (v4_version is not None and v4_version != git_version
            and (git_authority is None or v4_authority is None)):
        raise RuntimeError(
            "MAIN/V4 khác SHA nhưng thiếu Feed authority ở ít nhất một bên; "
            "giữ nguyên cả hai để đối chiếu"
        )
    # atomic_copy_json enforces Feed authority. It accepts equal authority and
    # a newer MAIN, but rejects a MAIN whose Feed proof is older than V4.
    return atomic_copy_json(
        PATH_GIT_JSON,
        PATH_V4_JSON,
        "Git -> V4",
        expected_destination=v4_version,
        expected_source=git_version,
        backup=True,
    )


def parse_timeout(choice):
    if choice in ("", "y"):
        return 150
    if choice == "n":
        return None
    if choice.isdigit() and int(choice) > 0:
        return int(choice)
    raise ValueError("Chỉ chấp nhận y, n hoặc số giây lớn hơn 0")


def should_resume_v4(git_count, v4_count):
    return v4_count is not None and v4_count > git_count


def choose_collection_base(git_count, v4_count, local_edit):
    if should_resume_v4(git_count, v4_count) and local_edit:
        raise RuntimeError("Cả Git JSON (có sửa/hợp nhất local) và V4 (có thêm video chưa publish) đều có thay đổi. "
                           "Không copy đè một bên; cần đối chiếu hai bản trước. Cả hai database được giữ nguyên.")
    return "v4" if should_resume_v4(git_count, v4_count) else "git"


def wait_until_collecting(process, run_id):
    """Do not charge browser setup/manual story wait to repost collection time."""
    print('   ⏳ Chờ collector reload và bước thao tác post/story. Chưa tính thời gian bắt repost.')
    while process.poll() is None:
        try:
            with open(COLLECTOR_STATUS_PATH, encoding='utf-8') as file:
                state = json.load(file)
            if state.get('run_id') == run_id and state.get('status') in ('collecting', 'saving', 'saved'):
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.2)


def manager_edit_is_current():
    with database_lock(PATH_GIT_JSON):
        return local_authority_is_current(PATH_GIT_JSON)


def has_valid_save_receipt(run_id, minimum_count):
    try:
        with open(COLLECTOR_STATUS_PATH, "r", encoding="utf-8") as file:
            receipt = json.load(file)
        if receipt.get("run_id") != run_id or receipt.get("status") != "saved":
            return False
        receipt_count = int(receipt.get("count", -1))
        with database_lock(PATH_V4_JSON):
            actual_count = validate_json_file(PATH_V4_JSON, "V4 JSON theo save receipt")
            actual_sha256 = snapshot(PATH_V4_JSON).version
        receipt_sha256 = str(receipt.get("database_sha256", "")).lower()
        return (
            receipt_count == actual_count
            and actual_count >= minimum_count
            and len(receipt_sha256) == 64
            and receipt_sha256 == actual_sha256
        )
    except (OSError, ValueError, TypeError, RuntimeError, json.JSONDecodeError):
        return False


def stop_child_safely(process, stop_request_path=None):
    if process.poll() is not None:
        print(f"   ℹ️ Collector đã dừng trước tín hiệu, exit code={process.returncode}")
        return
    if stop_request_path:
        # Per-run file avoids interrupting Playwright's greenlet dispatcher.
        with open(stop_request_path, "w", encoding="utf-8") as file:
            file.write(datetime.now().isoformat())
            file.flush()
            os.fsync(file.fileno())
        print(f"   🛑 Đã gửi yêu cầu lưu/dừng tới collector PID {process.pid}")
    elif os.name == "nt":
        print(f"   🛑 Gửi CTRL_BREAK_EVENT tới collector PID {process.pid}")
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        print(f"   🛑 Gửi SIGINT tới collector PID {process.pid}")
        process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        print("   ⚠️ Collector chưa thoát sau 60s; xem stack trong run log. Kết thúc tiến trình rồi kiểm tra save receipt.")
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print("   ❌ Collector không phản hồi terminate; chuyển sang kill.")
            process.kill()
            process.wait(timeout=10)

def main():
    print("=" * 60)
    print("🚀 BẮT ĐẦU QUY TRÌNH TỰ ĐỘNG HÓA TIKTOK REPOST".center(60))
    print("=" * 60)
    print(f"PID điều phối: {os.getpid()}")
    auto_html = input('Tự động xuất HTML sau khi thu thập? (y = có, Enter/n = mở manager để chỉnh tay): ').strip().lower() == 'y'
    sync_stories = input('Đồng bộ story user hiện tại từ D:\\tiktok\\tiktok-story? (y = có, Enter/n = không): ').strip().lower() == 'y'
    if os.path.isfile(PIPELINE_STATUS_PATH):
        try:
            with open(PIPELINE_STATUS_PATH, encoding='utf-8') as file:
                previous = json.load(file)
            print(f'Phiên trước: bước={previous.get("step")} trạng thái={previous.get("state")} lúc={previous.get("timestamp")}')
        except (ValueError, OSError):
            pass
    mark_step('backup_and_sync')

    print("\n[Bước 1] Tiến hành Backup và Copy file JSON...")
    # A crash may leave equal row counts, MAIN committed but V4 not mirrored,
    # or other hash-only divergence. Resolve durable sessions before looking at
    # counts/hashes for the next run.
    resume_unresolved_publish_sessions_at_startup()
    
    with database_lock(PATH_GIT_JSON):
        git_count_before = validate_json_file(PATH_GIT_JSON, "Git JSON hiện tại")
        git_snapshot_before = snapshot(PATH_GIT_JSON)
        git_version_before = git_snapshot_before.version
        git_authority_before = feed_state.recover(PATH_GIT_JSON, git_version_before)
    print(f"   🔎 Git JSON hợp lệ: {git_count_before} videos")

    v4_count_before = None
    v4_version_before = None
    v4_authority_before = None
    if os.path.exists(PATH_V4_JSON):
        try:
            with database_lock(PATH_V4_JSON):
                v4_version_before = snapshot(PATH_V4_JSON).version
                v4_count_before = validate_json_file(PATH_V4_JSON, "V4 JSON hiện tại")
                v4_authority_before, _ = feed_state.inspect(PATH_V4_JSON, v4_version_before)
            print(f"   🔎 V4 JSON hợp lệ: {v4_count_before} videos")
        except RuntimeError as exc:
            invalid_backup = PATH_V4_JSON + datetime.now().strftime(".invalid-%Y%m%d_%H%M%S.bak")
            shutil.copy2(PATH_V4_JSON, invalid_backup)
            print(f"   ⚠️ {exc}")
            print(f"   ⚠️ Đã giữ file V4 lỗi tại: {invalid_backup}")

    # Khong de Git cu ghi de mot ban autosave V4 moi hon sau lan dung dot ngot.
    git_excluded = PATH_GIT_JSON + '.deleted_ids.json'
    if os.path.isfile(git_excluded):
        with open(git_excluded, encoding='utf-8') as file:
            excluded = json.load(file)
        if not isinstance(excluded, list):
            raise RuntimeError('Danh sách ID đã xóa phải là List')
        temp_excluded = PATH_V4_JSON + '.deleted_ids.json.tmp'
        shutil.copy2(git_excluded, temp_excluded)
        os.replace(temp_excluded, PATH_V4_JSON + '.deleted_ids.json')
    # With no unresolved session, verified MAIN may seed V4. This also covers
    # thumbnail/auto-HTML writes which intentionally have no manager-state file.
    seed_collector_from_main(
        git_count=git_count_before,
        v4_count=v4_count_before,
        git_version=git_version_before,
        v4_version=v4_version_before,
        git_authority=git_authority_before,
        v4_authority=v4_authority_before,
    )
    collection_base_count = git_count_before
    print("   ✅ Đã copy atomic: Git JSON -> V4 JSON thành công!")

    # BƯỚC 2: Chạy test_api_log
    print("\n[Bước 2] Chuẩn bị chạy 'test_api_log - V2.py'.")
    choice = input("   👉 Tự động dừng? (y/Enter = 150s, n = không, hoặc nhập số giây): ").strip().lower()
    timeout_sec = parse_timeout(choice)
    allow_deleted = False
    deleted_history_path = PATH_V4_JSON + '.deleted_ids.json'
    if os.path.isfile(deleted_history_path):
        with open(deleted_history_path, encoding='utf-8') as file:
            deleted_history = json.load(file)
        if deleted_history:
            answer = input(f'   👉 Có {len(deleted_history)} ID từng xóa. Cập nhật lại nếu API trả về? (Enter/y = có, n = bỏ qua phiên này): ').strip().lower()
            allow_deleted = answer != 'n'
    while True:
        wait_choice = input('   👉 Chờ post/story sau reload bao nhiêu giây? (Enter = 60s; 0 = chờ đến khi bạn nhấn Enter): ').strip()
        if not wait_choice or wait_choice.isdigit():
            story_wait_seconds = int(wait_choice or '60')
            break
        print('   Hãy nhập số giây không âm.')
    collector_run_id = f"auto-{os.getpid()}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    mark_step('publish_preflight', run_id=collector_run_id)
    prepare_publish_session(collector_run_id)
    mark_step('collector')

    print("   ⚙️ Đang chạy 'test_api_log - V2.py'...")
    # Dùng CREATE_NEW_PROCESS_GROUP để có thể gửi tín hiệu ngắt (Ctrl+C) trên Windows an toàn
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    child_env = os.environ.copy()
    child_env["TIKTOK_RUN_ID"] = collector_run_id
    stop_request_path = os.path.join(RUN_LOG_DIR, collector_run_id + ".stop")
    child_env["TIKTOK_STOP_FILE"] = stop_request_path
    child_env['TIKTOK_STORY_WAIT_SECONDS'] = str(story_wait_seconds)
    child_env['TIKTOK_INCLUDE_DELETED'] = '1' if allow_deleted else '0'
    p1 = subprocess.Popen(
        [sys.executable, "-u", COLLECTOR_SCRIPT],
        cwd=DIR_V4,
        creationflags=creationflags,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=child_env,
    )
    print(f"   🔎 Collector PID: {p1.pid}")
    output_thread = threading.Thread(
        target=pump_child_output,
        args=(p1,),
        name="collector-log-pump",
        daemon=True,
    )
    output_thread.start()

    try:
        wait_until_collecting(p1, collector_run_id)
        if timeout_sec:
            print(f"   ⏳ Sẽ tự dừng sau {timeout_sec} giây. Bạn có thể ấn Ctrl+C để dừng sớm.")
            p1.wait(timeout=timeout_sec)
            print("   ✅ Tool đã tự hoàn thành trước thời hạn.")
        else:
            print("   ⏳ Đang chạy... (Hãy ấn Ctrl+C vào cửa sổ này khi bạn muốn dừng tool).")
            p1.wait()
            
    except subprocess.TimeoutExpired:
        print(f"\n   ⏰ Đã hết {timeout_sec} giây! Đang yêu cầu tool lưu và dừng...")
        stop_child_safely(p1, stop_request_path)
        print("   ✅ Đã tắt tool thành công.")
        
    except KeyboardInterrupt:
        print("\n   🛑 Bạn vừa ấn Ctrl+C thủ công. Đang dừng tool an toàn...")
        stop_child_safely(p1, stop_request_path)
        print("   ✅ Đã tắt tool thành công.")

    if p1.poll() is not None and os.path.isfile(stop_request_path):
        os.remove(stop_request_path)
    output_thread.join(timeout=15)
    if output_thread.is_alive():
        print("   ⚠️ Luồng ghi log collector chưa đóng sau 15 giây.")
    print(f"   🔎 Collector exit code: {p1.returncode}")

    save_receipt_valid = has_valid_save_receipt(collector_run_id, collection_base_count)
    print(f"   🔎 Save receipt hợp lệ: {save_receipt_valid}")
    if not save_receipt_valid:
        raise RuntimeError(f"Collector kết thúc với exit code {p1.returncode}; không publish JSON")
    if p1.returncode != 0 and save_receipt_valid:
        print("   ⚠️ Cleanup collector lỗi nhưng database đã save/validate; tiếp tục publish an toàn.")

    v4_count = validate_json_file(PATH_V4_JSON, "V4 JSON sau collector")
    if v4_count < collection_base_count:
        raise RuntimeError(
            f"Database giảm bất thường {collection_base_count} -> {v4_count}; không ghi đè Git"
        )
    print(f"   🔎 V4 JSON hợp lệ: {v4_count} videos")

    # BƯỚC 3: V4 -> Git
    print("\n[Bước 3] Copy trả lại file JSON từ V4 về Git...")
    mark_step('publish_json')
    publish_sessions.record_saved(
        sessions_root=PUBLISH_SESSIONS_DIR,
        run_id=collector_run_id,
        collector_status_path=COLLECTOR_STATUS_PATH,
    )
    publish = publish_sessions.publish_session(
        sessions_root=PUBLISH_SESSIONS_DIR,
        run_id=collector_run_id,
        collector_status_path=COLLECTOR_STATUS_PATH,
    )
    print(
        "   ✅ Publish hợp nhất ba chiều thành công: "
        f"{publish['count']} videos · Feed={publish['feed_source']} · "
        f"xung đột cần đối chiếu={publish['conflicts']}"
    )
    # COMPLETE receipts without conflicts omit this optional field.
    conflict_receipt = publish.get('conflict_receipt')
    if conflict_receipt:
        print("   ⚠️ Đã giữ đủ hai phương án tại: " + conflict_receipt)
    if not publish['mirrored_to_collector']:
        print("   ⚠️ MAIN đã publish nhưng chưa mirror về V4; lần chạy sau sẽ lấy MAIN làm nền. "
              "Không xóa recovery artifact.")
    if sync_stories:
        sync_command = [sys.executable, '-u', 'tiktok_html_generator.py', '--sync-stories', '--input', FILE_JSON]
        if allow_deleted:
            sync_command.append('--include-deleted')
        run_logged_step('story_sync', sync_command, DIR_GIT)

    # Only import explicit manual fields, never API metadata/order. Opt-in network sync.
    if os.path.isfile(PATH_GIT_JSON + '.manual_edits.json'):
        run_logged_step('manual_overlay', [sys.executable, '-u', 'tiktok_html_generator.py', '--apply-manual-overlay', '--input', FILE_JSON], DIR_GIT)
    if os.environ.get('TIKTOK_MANUAL_SYNC', '').strip() == '1':
        run_logged_step('manual_gas_sync', [sys.executable, '-u', 'tiktok_html_generator.py', '--sync-manual', '--input', FILE_JSON], DIR_GIT)

    # BƯỚC 4: Chạy tiktok_thumb_manager
    print("\n[Bước 4] Đang chạy 'tiktok_thumb_manager.py' TỰ ĐỘNG...")
    with database_lock(PATH_GIT_JSON):
        thumb_base_version = snapshot(PATH_GIT_JSON).version
        atomic_copy_json(PATH_GIT_JSON, PATH_GIT_PIPELINE, "JSON staging cho thumbnail", backup=True)
    cmd_thumb = [
        sys.executable, "tiktok_thumb_manager.py",
        "--auto",                                  # Cờ báo hiệu tự chạy tự đóng
        "--input", FILE_JSON_PIPELINE,
        "--output", FILE_JSON_PIPELINE,
        "--thumb-dir", "thumbs_pe.siro_phan"   # Tên thư mục bạn muốn lưu ảnh
    ]
    thumbnail_published = False
    try:
        run_logged_step('thumbnail', cmd_thumb, DIR_GIT)
        pipeline_count = validate_json_file(PATH_GIT_PIPELINE, "JSON sau thumbnail manager")
        if pipeline_count < v4_count:
            raise RuntimeError(
                f"Thumbnail manager làm giảm database {v4_count} -> {pipeline_count}"
            )
        atomic_copy_json(PATH_GIT_PIPELINE, PATH_GIT_JSON, "Thumbnail staging -> Git",
                         expected_destination=thumb_base_version, backup=True)
        thumbnail_published = True
    finally:
        if thumbnail_published and os.path.exists(PATH_GIT_PIPELINE):
            try:
                os.remove(PATH_GIT_PIPELINE)
            except OSError as exc:
                print(f"   ⚠️ Không xóa được file staging: {exc}")
        elif os.path.exists(PATH_GIT_PIPELINE):
            print(f"   🛟 Chưa publish; giữ nguyên staging để phục hồi: {PATH_GIT_PIPELINE}")
    print("   ✅ Xong Bước 4!")

    # BƯỚC 5: Chạy tiktok_html_generator
    print("\n[Bước 5] Đang chạy 'tiktok_html_generator.py'...")
    generator_command = [sys.executable, '-u', 'tiktok_html_generator.py', '--input', FILE_JSON]
    if auto_html:
        generator_command.append('--auto')
        run_logged_step('html_export', generator_command, DIR_GIT)
    else:
        print('Mở manager với database hiện tại. Bạn chỉnh ngày/caption và tự chọn Xuất HTML khi sẵn sàng; đóng manager để kết thúc auto.')
        run_logged_step('html_manager', generator_command, DIR_GIT)
    final_count = validate_json_file(PATH_GIT_JSON, "Git JSON cuối quy trình")
    print(f"   🔎 Kiểm tra cuối: {final_count} videos, JSON hợp lệ")
    print("   ✅ Xong!")

    # BƯỚC 6: Kết thúc
    print("\n" + "=" * 60)
    print("🎉 HOÀN THÀNH TOÀN BỘ QUY TRÌNH!".center(60))
    print("=" * 60)
    mark_step('pipeline', 'complete')

if __name__ == "__main__":
    run_log_path, run_log_file, original_stdout, original_stderr = setup_run_logging()
    exit_code = 0
    pipeline_owned = False
    try:
        print(f"📝 Run log: {run_log_path}")
        with database_lock(PIPELINE_STATUS_PATH + ".run", timeout=.1):
            pipeline_owned = True
            main()
    except KeyboardInterrupt:
        exit_code = 130
        if pipeline_owned:
            mark_step(CURRENT_STEP, 'interrupted')
        print("\n❌ QUY TRÌNH BỊ NGẮT TRƯỚC KHI COLLECTOR SẴN SÀNG.")
    except Exception as exc:
        exit_code = 1
        if pipeline_owned:
            mark_step(CURRENT_STEP, 'failed', error=str(exc))
        print(f"\n❌ QUY TRÌNH DỪNG AN TOÀN: {exc}")
        traceback.print_exc()
    finally:
        print(f"\n📝 Đã đóng run log: {run_log_path} | exit_code={exit_code}")
        if sys.stdin.isatty():
            try:
                if exit_code == 0:
                    print('\n✅ ĐÃ HOÀN TẤT QUY TRÌNH. Kết quả từng bước được ghi ở trên.')
                    print('HTML: ' + os.path.join(DIR_GIT, 'view_pe.siro_phan_all.html'))
                else:
                    print('\n❌ DỪNG DO LỖI ở bước ' + CURRENT_STEP + f' (exit code {exit_code}).')
                print('Log chi tiết: ' + run_log_path)
                input('Cửa sổ được giữ để bạn xem kết quả. Nhấn Enter khi muốn đóng...')
            except (EOFError, KeyboardInterrupt):
                pass
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        run_log_file.close()
    sys.exit(exit_code)
