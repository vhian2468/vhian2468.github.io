"""PC manual date/caption sync session. No credential files or Git operations."""
import argparse
import getpass
import os
import sys
import threading
from pathlib import Path
from tiktok_database_guard import database_lock
from tiktok_manual_edits import gas_request, profile_for, pull_annotations, saved_gas_url, validate_gas_url, import_records
from tiktok_manual_upload import push


def cycle(path, url, token, progress=lambda message: None):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".json":
        raise ValueError("Chọn database JSON, không chọn bản JS/GitHub export")
    profile_for(path)
    url = validate_gas_url(url)
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Thiếu token GAS ngày/caption")
    # Avoid two independent background sessions pulling/projecting concurrently.
    # Manager edits use the ordinary main database CAS; no main lock over HTTP.
    with database_lock(str(path)+".manual-sync-session", timeout=0.1):
        progress("Đang gửi hàng chờ ngày/caption PC…")
        sent = push(path, url, lambda request: gas_request(url, token, request), include_remote=True)
        # Integrity/authentication/upload failure aborts the cycle. Never pull a
        # potentially restored server over local data after a failed audit.
        progress("Đang nhận ngày/caption; giữ các trường còn chờ xác nhận…")
        if "remote_document" in sent:
            received = import_records(path, sent["remote_document"], remote=True)
        else:
            received = pull_annotations(path, url, token)
        return dict(acked=sent["acked"], remaining=sent["remaining"], blocked=sent["blocked"],
                    received_fields=received["changed_fields"],
                    conflicts=len(received["conflicts"]),
                    missing_ids=len(received["missing_ids"]))


def watch(run_cycle, stop, interval=30, notify=print):
    """Bounded backoff, cooperative stop; no independent worker thread."""
    if not 5 <= interval <= 3600:
        raise ValueError("Chu kỳ phải từ 5 đến 3600 giây")
    failures = 0
    while not stop.is_set():
        try:
            result = run_cycle()
            failures = 0
            notify("GAS xác nhận {acked} thao tác · còn {remaining} lần lưu · "
                   "nhận {received_fields} trường · xung đột {conflicts} · "
                   "video chưa có ở PC {missing_ids}".format(**result))
            delay = 5 if result["acked"] and result["remaining"] else interval
        except Exception as error:
            failures += 1
            delay = min(300, interval * 2**min(failures-1, 6))
            # Never echo arbitrary server responses (which may include secrets).
            notify(f"Chưa xác nhận đồng bộ ({type(error).__name__}); giữ dữ liệu và hàng chờ. Thử lại sau {delay}s.")
        if stop.wait(delay):
            break


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", nargs="?")
    parser.add_argument("--url", help="URL GAS ngày/caption, KHÔNG phải Hub media")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args(argv)
    if not 5 <= args.interval <= 3600:
        parser.error("--interval phải từ 5 đến 3600")
    try:
        path = Path(args.database or input("Đường dẫn data_<user>.json: ").strip().strip('"')).resolve(strict=True)
        url = validate_gas_url(args.url or os.environ.get("TIKTOK_GAS_URL") or
                               saved_gas_url(profile_for(path)) or input("URL GAS ngày/caption: ").strip())
        token = os.environ.get("TIKTOK_GAS_TOKEN") or getpass.getpass("Token riêng (chỉ giữ trong bộ nhớ phiên này): ")
        print("Đồng bộ NGÀY/CAPTION qua GAS hiện tại; không phải media/giờ/nhãn Hub.")
        print("Ctrl+C để dừng. Không cần commit/push GitHub. Không xóa queue hoặc lịch sử.")
        print("Nếu manager báo JSON đã đổi, mở lại file; bản chỉnh sửa bị từ chối vẫn có trong .repost-recovery.")
        run = lambda: cycle(path, url, token, print)
        if args.once:
            print(run())
        else:
            watch(run, threading.Event(), args.interval)
        return 0
    except KeyboardInterrupt:
        print("\nĐã dừng phiên đồng bộ. Hàng chờ local vẫn được giữ.")
        return 0
    except Exception as error:
        print(f"Không hoàn tất đồng bộ ({type(error).__name__}). Kiểm tra database, URL/token và hàng chờ; không xóa dữ liệu.")
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
