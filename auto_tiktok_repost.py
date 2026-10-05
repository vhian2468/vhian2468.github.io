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


def atomic_copy_json(source, destination, label="JSON"):
    """Copy qua file tam; destination chi thay doi sau khi ban copy hop le."""
    expected_count = validate_json_file(source, f"{label} nguồn")
    destination_dir = os.path.dirname(destination)
    os.makedirs(destination_dir, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination_dir,
            prefix=os.path.basename(destination) + ".",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
        shutil.copy2(source, temp_path)
        copied_count = validate_json_file(temp_path, f"{label} file tạm")
        if copied_count != expected_count:
            raise RuntimeError(
                f"{label} thay đổi số lượng khi copy: {expected_count} -> {copied_count}"
            )
        os.replace(temp_path, destination)
        temp_path = None
        return copied_count
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


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


def manager_edit_is_current():
    state_path = PATH_GIT_JSON + '.manager.state.json'
    if not os.path.isfile(state_path):
        return False
    with open(state_path, encoding='utf-8') as file:
        state = json.load(file)
    with open(PATH_GIT_JSON, 'rb') as file:
        digest = hashlib.sha256(file.read()).hexdigest()
    return state.get('sha256') == digest


def has_valid_save_receipt(run_id, minimum_count):
    try:
        with open(COLLECTOR_STATUS_PATH, "r", encoding="utf-8") as file:
            receipt = json.load(file)
        if receipt.get("run_id") != run_id or receipt.get("status") != "saved":
            return False
        receipt_count = int(receipt.get("count", -1))
        actual_count = validate_json_file(PATH_V4_JSON, "V4 JSON theo save receipt")
        return receipt_count == actual_count and actual_count >= minimum_count
    except (OSError, ValueError, TypeError, RuntimeError, json.JSONDecodeError):
        return False


def stop_child_safely(process):
    if process.poll() is not None:
        print(f"   ℹ️ Collector đã dừng trước tín hiệu, exit code={process.returncode}")
        return
    if os.name == "nt":
        print(f"   🛑 Gửi CTRL_BREAK_EVENT tới collector PID {process.pid}")
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        print(f"   🛑 Gửi SIGINT tới collector PID {process.pid}")
        process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        print("   ⚠️ Collector chưa thoát sau 30s; kết thúc tiến trình rồi kiểm tra save receipt.")
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
    if os.path.isfile(PIPELINE_STATUS_PATH):
        try:
            with open(PIPELINE_STATUS_PATH, encoding='utf-8') as file:
                previous = json.load(file)
            print(f'Phiên trước: bước={previous.get("step")} trạng thái={previous.get("state")} lúc={previous.get("timestamp")}')
        except (ValueError, OSError):
            pass
    mark_step('backup_and_sync')

    print("\n[Bước 1] Tiến hành Backup và Copy file JSON...")
    
    git_count_before = validate_json_file(PATH_GIT_JSON, "Git JSON hiện tại")
    print(f"   🔎 Git JSON hợp lệ: {git_count_before} videos")

    v4_count_before = None
    if os.path.exists(PATH_V4_JSON):
        try:
            v4_count_before = validate_json_file(PATH_V4_JSON, "V4 JSON hiện tại")
            print(f"   🔎 V4 JSON hợp lệ: {v4_count_before} videos")
        except RuntimeError as exc:
            invalid_backup = PATH_V4_JSON + datetime.now().strftime(".invalid-%Y%m%d_%H%M%S.bak")
            shutil.copy2(PATH_V4_JSON, invalid_backup)
            print(f"   ⚠️ {exc}")
            print(f"   ⚠️ Đã giữ file V4 lỗi tại: {invalid_backup}")

    if os.path.exists(PATH_GIT_BACKUP1):
        atomic_copy_json(PATH_GIT_BACKUP1, PATH_GIT_BACKUP2, "Backup bak2")
        print("   ✅ Đã backup: bak1 -> bak2")

    atomic_copy_json(PATH_GIT_JSON, PATH_GIT_BACKUP1, "Backup bak1")
    print("   ✅ Đã backup atomic: json hiện tại -> bak1")

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
    if should_resume_v4(git_count_before, v4_count_before) and not manager_edit_is_current():
        collection_base_count = v4_count_before
        print(
            f"   🛟 Phục hồi phiên trước: giữ V4 vì có thêm "
            f"{v4_count_before - git_count_before} videos chưa publish."
        )
    else:
        atomic_copy_json(PATH_GIT_JSON, PATH_V4_JSON, "Git -> V4")
        collection_base_count = git_count_before
        print("   ✅ Đã copy atomic: Git JSON -> V4 JSON thành công!")

    # BƯỚC 2: Chạy test_api_log
    print("\n[Bước 2] Chuẩn bị chạy 'test_api_log - V2.py'.")
    choice = input("   👉 Tự động dừng? (y/Enter = 150s, n = không, hoặc nhập số giây): ").strip().lower()
    timeout_sec = parse_timeout(choice)
    mark_step('collector')

    print("   ⚙️ Đang chạy 'test_api_log - V2.py'...")
    # Dùng CREATE_NEW_PROCESS_GROUP để có thể gửi tín hiệu ngắt (Ctrl+C) trên Windows an toàn
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    collector_run_id = f"auto-{os.getpid()}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    child_env = os.environ.copy()
    child_env["TIKTOK_RUN_ID"] = collector_run_id
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
        if timeout_sec:
            print(f"   ⏳ Sẽ tự dừng sau {timeout_sec} giây. Bạn có thể ấn Ctrl+C để dừng sớm.")
            p1.wait(timeout=timeout_sec)
            print("   ✅ Tool đã tự hoàn thành trước thời hạn.")
        else:
            print("   ⏳ Đang chạy... (Hãy ấn Ctrl+C vào cửa sổ này khi bạn muốn dừng tool).")
            p1.wait()
            
    except subprocess.TimeoutExpired:
        print(f"\n   ⏰ Đã hết {timeout_sec} giây! Đang yêu cầu tool lưu và dừng...")
        stop_child_safely(p1)
        print("   ✅ Đã tắt tool thành công.")
        
    except KeyboardInterrupt:
        print("\n   🛑 Bạn vừa ấn Ctrl+C thủ công. Đang dừng tool an toàn...")
        stop_child_safely(p1)
        print("   ✅ Đã tắt tool thành công.")

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
    atomic_copy_json(PATH_V4_JSON, PATH_GIT_JSON, "V4 -> Git")
    print("   ✅ Publish atomic thành công!")

    # BƯỚC 4: Chạy tiktok_thumb_manager
    print("\n[Bước 4] Đang chạy 'tiktok_thumb_manager.py' TỰ ĐỘNG...")
    atomic_copy_json(PATH_GIT_JSON, PATH_GIT_PIPELINE, "JSON staging cho thumbnail")
    cmd_thumb = [
        sys.executable, "tiktok_thumb_manager.py",
        "--auto",                                  # Cờ báo hiệu tự chạy tự đóng
        "--input", FILE_JSON_PIPELINE,
        "--output", FILE_JSON_PIPELINE,
        "--thumb-dir", "thumbs_pe.siro_phan"   # Tên thư mục bạn muốn lưu ảnh
    ]
    try:
        run_logged_step('thumbnail', cmd_thumb, DIR_GIT)
        pipeline_count = validate_json_file(PATH_GIT_PIPELINE, "JSON sau thumbnail manager")
        if pipeline_count < v4_count:
            raise RuntimeError(
                f"Thumbnail manager làm giảm database {v4_count} -> {pipeline_count}"
            )
        atomic_copy_json(PATH_GIT_PIPELINE, PATH_GIT_JSON, "Thumbnail staging -> Git")
    finally:
        if os.path.exists(PATH_GIT_PIPELINE):
            try:
                os.remove(PATH_GIT_PIPELINE)
            except OSError as exc:
                print(f"   ⚠️ Không xóa được file staging: {exc}")
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
    try:
        print(f"📝 Run log: {run_log_path}")
        main()
    except KeyboardInterrupt:
        exit_code = 130
        mark_step(CURRENT_STEP, 'interrupted')
        print("\n❌ QUY TRÌNH BỊ NGẮT TRƯỚC KHI COLLECTOR SẴN SÀNG.")
    except Exception as exc:
        exit_code = 1
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
