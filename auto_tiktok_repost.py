import os
import shutil
import subprocess
import signal

# ================= CẤU HÌNH ĐƯỜNG DẪN =================
DIR_GIT = r"D:\OneDrive\Documents\GitHub\vhian2468.github.io"
DIR_V4 = r"D:\tiktok\tiktok-repost\V4-2026"

FILE_JSON = "data_pe.siro_phan_all.json"
FILE_JSON_BACKUP1 = "data_pe.siro_phan_all.json.bak1"
FILE_JSON_BACKUP2 = "data_pe.siro_phan_all.json.bak2"

PATH_GIT_JSON = os.path.join(DIR_GIT, FILE_JSON)
PATH_GIT_BACKUP1 = os.path.join(DIR_GIT, FILE_JSON_BACKUP1)
PATH_GIT_BACKUP2 = os.path.join(DIR_GIT, FILE_JSON_BACKUP2)
PATH_V4_JSON = os.path.join(DIR_V4, FILE_JSON)

# ======================================================

def main():
    print("=" * 60)
    print("🚀 BẮT ĐẦU QUY TRÌNH TỰ ĐỘNG HÓA TIKTOK REPOST".center(60))
    print("=" * 60)

    print("\n[Bước 1] Tiến hành Backup và Copy file JSON...")
    
    if os.path.exists(PATH_GIT_BACKUP1):
        shutil.copy2(PATH_GIT_BACKUP1, PATH_GIT_BACKUP2)
        print("   ✅ Đã backup: bak1 -> bak2")

    if os.path.exists(PATH_GIT_JSON):
        shutil.copy2(PATH_GIT_JSON, PATH_GIT_BACKUP1)
        print("   ✅ Đã backup: json hiện tại -> bak1")

    # 3. Copy file json gốc từ Git về V4 (Ghi đè file ở V4)
    if os.path.exists(PATH_GIT_JSON):
        shutil.copy2(PATH_GIT_JSON, PATH_V4_JSON)
        print("   ✅ Đã copy: Git JSON -> V4 JSON thành công!")
    else:
        print(f"   ❌ LỖI: Không tìm thấy file JSON gốc tại Git: {PATH_GIT_JSON}")

    # BƯỚC 2: Chạy test_api_log
    print("\n[Bước 2] Chuẩn bị chạy 'test_api_log - V2.py'.")
    choice = input("   👉 Bạn có muốn tool tự động tắt không? (y = 150s / n = Không / hoặc nhập số giây tùy chỉnh - mặc định là y): ").strip().lower()

    if choice == 'y':
        timeout_sec = 150
    elif choice.isdigit():
        timeout_sec = int(choice)
    else:
        timeout_sec = 100  # Áp dụng cho 'n', để trống, hoặc nhập sai định dạng

    print("   ⚙️ Đang chạy 'test_api_log - V2.py'...")
    # Dùng CREATE_NEW_PROCESS_GROUP để có thể gửi tín hiệu ngắt (Ctrl+C) trên Windows an toàn
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    p1 = subprocess.Popen(["python", "test_api_log - V2.py"], cwd=DIR_V4, creationflags=creationflags)

    try:
        if timeout_sec:
            print("   ⏳ Đang đếm ngược 5 phút. (Bạn vẫn có thể ấn Ctrl+C ở đây để dừng sớm).")
            p1.wait(timeout=timeout_sec)
            print("   ✅ Tool đã hoàn thành trước thời hạn 5 phút.")
        else:
            print("   ⏳ Đang chạy... (Hãy ấn Ctrl+C vào cửa sổ này khi bạn muốn dừng tool).")
            p1.wait()
            
    except subprocess.TimeoutExpired:
        print("\n   ⏰ Đã hết 5 phút! Đang gửi tín hiệu dừng (Ctrl+C)...")
        if os.name == 'nt':
            p1.send_signal(signal.CTRL_BREAK_EVENT) # Gửi break event tương đương Ctrl+C
        else:
            p1.terminate()
        p1.wait()
        print("   ✅ Đã tắt tool thành công.")
        
    except KeyboardInterrupt:
        print("\n   🛑 Bạn vừa ấn Ctrl+C thủ công. Đang dừng tool an toàn...")
        if os.name == 'nt':
            p1.send_signal(signal.CTRL_BREAK_EVENT)
        p1.wait()
        print("   ✅ Đã tắt tool thành công.")

    # BƯỚC 3: V4 -> Git
    print("\n[Bước 3] Copy trả lại file JSON từ V4 về Git...")
    shutil.copy2(PATH_V4_JSON, PATH_GIT_JSON)
    print("   ✅ Thành công!")

    # BƯỚC 4: Chạy tiktok_thumb_manager
    print("\n[Bước 4] Đang chạy 'tiktok_thumb_manager.py' TỰ ĐỘNG...")
    cmd_thumb = [
        "python", "tiktok_thumb_manager.py",
        "--auto",                                  # Cờ báo hiệu tự chạy tự đóng
        "--input", FILE_JSON,                      # File đầu vào (nó sẽ đọc ở thư mục Git)
        "--output", FILE_JSON,                     # Lưu đè luôn lên file đầu vào đó
        "--thumb-dir", "thumbs_pe.siro_phan"   # Tên thư mục bạn muốn lưu ảnh
    ]
    subprocess.run(cmd_thumb, cwd=DIR_GIT)
    print("   ✅ Xong Bước 4!")

    # BƯỚC 5: Chạy tiktok_html_generator
    print("\n[Bước 5] Đang chạy 'tiktok_html_generator.py'...")
    subprocess.run(["python", "tiktok_html_generator.py"], cwd=DIR_GIT)
    print("   ✅ Xong!")

    # BƯỚC 6: Kết thúc
    print("\n" + "=" * 60)
    print("🎉 HOÀN THÀNH TOÀN BỘ QUY TRÌNH!".center(60))
    print("=" * 60)

if __name__ == "__main__":
    main()