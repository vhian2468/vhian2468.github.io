import json
import requests
import os
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

# Thử import Pillow để xử lý ảnh
try:
    from PIL import Image, ImageFile
    # Cho phép load ảnh bị lỗi nhẹ/thiếu dữ liệu
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ================= CORE LOGIC =================

# CẤU HÌNH NÉN ẢNH
MAX_WIDTH = 480       # Chiều ngang tối đa (px) - 480px là đủ đẹp cho web mobile/pc
JPEG_QUALITY = 75     # Chất lượng ảnh (0-100). 75 là cân bằng tốt nhất.

def process_image_data(image_data_or_path, output_path, is_file_path=False):
    """
    Hàm xử lý nén ảnh dùng chung.
    Input: BytesIO object hoặc đường dẫn file.
    Output: Lưu file nén tại output_path.
    """
    if not HAS_PIL:
        # Nếu không có Pillow, chỉ ghi file thô (nếu là bytes)
        if not is_file_path and image_data_or_path:
            with open(output_path, 'wb') as f:
                f.write(image_data_or_path.getbuffer())
        return

    try:
        img = None
        if is_file_path:
            img = Image.open(image_data_or_path)
        else:
            img = Image.open(image_data_or_path)

        # Convert sang RGB (để tránh lỗi khi nén PNG trong suốt sang JPEG)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # Resize nếu ảnh lớn hơn MAX_WIDTH
        width, height = img.size
        if width > MAX_WIDTH:
            # Tính tỷ lệ để giữ khung hình
            ratio = MAX_WIDTH / width
            new_height = int(height * ratio)
            img = img.resize((MAX_WIDTH, new_height), Image.Resampling.LANCZOS)
        
        # Lưu file với chuẩn JPEG tối ưu
        img.save(output_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
        
    except Exception as e:
        print(f"Lỗi nén ảnh {output_path}: {e}")
        # Nếu lỗi trong quá trình nén (vd file hỏng), cố gắng lưu bản gốc nếu là download mới
        if not is_file_path and image_data_or_path:
            with open(output_path, 'wb') as f:
                f.write(image_data_or_path.getbuffer())

def download_and_compress(url, video_id, thumb_dir, force_recompress=False):
    """
    Tải và nén ảnh.
    - url: Link ảnh
    - force_recompress: Nếu True, sẽ mở ảnh cũ ra nén lại (không tải mới nếu có rồi).
    """
    filename = f"{video_id}.jpeg" # Luôn lưu đuôi jpeg để đồng bộ
    file_path = os.path.join(thumb_dir, filename)
    rel_path = f"{os.path.basename(thumb_dir)}/{filename}"

    # 1. Kiểm tra file đã tồn tại
    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        if force_recompress and HAS_PIL:
            # Chế độ nén lại ảnh cũ (offline processing)
            # === SMART CHECK: Nếu ảnh đã nén rồi (đúng chuẩn) thì KHÔNG nén lại ===
            # Tránh làm giảm chất lượng ảnh (Generation Loss) và tốn thời gian
            try:
                with Image.open(file_path) as img:
                    # Nếu chiều ngang <= 480 VÀ định dạng đã là JPEG -> Coi như đã tối ưu
                    if img.size[0] <= MAX_WIDTH and img.format == 'JPEG':
                        return rel_path
            except Exception:
                # Nếu lỗi đọc ảnh check, cứ để process_image_data xử lý (nó có try-catch riêng)
                pass

            process_image_data(file_path, file_path, is_file_path=True)
            return rel_path
        else:
            # Bỏ qua nếu không yêu cầu nén lại -> Return path luôn
            return rel_path

    # 2. Nếu chưa có file hoặc force download -> Tải về
    if not url:
        return None

    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            image_bytes = BytesIO(response.content)
            # Nén và lưu
            process_image_data(image_bytes, file_path, is_file_path=False)
            return rel_path
    except Exception:
        pass
        
    return None

def process_single_item(key, item, thumb_dir, force_recompress):
    """Xử lý 1 video"""
    vid_id = item.get('id')
    unique_id = item.get('uniqueId')
    current_cover = item.get('cover')
    existing_offline_path = item.get('cover_off')

    if not vid_id:
        return key, None, None

    # === TỐI ƯU HÓA: SKIP NẾU ĐÃ CÓ ===
    # Nếu không bắt buộc nén lại AND trong JSON đã có đường dẫn offline
    if not force_recompress and existing_offline_path:
        # Kiểm tra xem file vật lý có thực sự tồn tại không
        # existing_offline_path thường dạng "thumbs/123.jpeg"
        # Chúng ta cần file name để check trong thumb_dir hiện tại
        check_filename = os.path.basename(existing_offline_path)
        check_full_path = os.path.join(thumb_dir, check_filename)
        
        if os.path.exists(check_full_path) and os.path.getsize(check_full_path) > 0:
            # File đã tồn tại và hợp lệ -> BỎ QUA HOÀN TOÀN
            # Trả về None để báo hiệu không có thay đổi gì
            return key, None, None

    # === HẾT PHẦN TỐI ƯU ===

    # Logic lấy link mới (oEmbed)
    new_cover_url = current_cover
    
    # Chỉ gọi oEmbed nếu cần tải mới (file chưa có) và không phải chế độ chỉ nén lại
    file_path_check = os.path.join(thumb_dir, f"{vid_id}.jpeg")
    need_download = not (os.path.exists(file_path_check) and os.path.getsize(file_path_check) > 0)

    if need_download and unique_id:
        try:
            # Chỉ thử lấy link tốt hơn nếu thực sự cần tải
            video_url = f"https://www.tiktok.com/@{unique_id}/video/{vid_id}"
            api_url = f"https://www.tiktok.com/oembed?url={video_url}"
            resp = requests.get(api_url, timeout=5)
            if resp.status_code == 200:
                info = resp.json()
                fetched = info.get('thumbnail_url')
                if fetched:
                    new_cover_url = fetched
        except:
            pass
    
    # Thực hiện tải/nén
    local_path = download_and_compress(new_cover_url, vid_id, thumb_dir, force_recompress)
    
    # Fallback
    if not local_path and current_cover and current_cover != new_cover_url:
         # Chỉ fallback download nếu file chưa có
         if need_download:
            local_path = download_and_compress(current_cover, vid_id, thumb_dir, force_recompress)

    return key, new_cover_url, local_path

# ================= GUI APP =================

class ThumbDownloaderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("TikTok Thumb Manager (Smart Skip)")
        self.root.geometry("650x550")
        
        # Variables
        self.input_path = tk.StringVar()
        self.thumb_folder_name = tk.StringVar(value="thumbs")
        self.status_var = tk.StringVar(value="Sẵn sàng")
        self.chk_recompress_var = tk.BooleanVar(value=False)
        self.is_running = False
        
        # Check Pillow
        if not HAS_PIL:
            messagebox.showwarning("Thiếu thư viện", "Chưa cài đặt Pillow!\nChức năng nén ảnh sẽ không hoạt động.\nHãy chạy: pip install Pillow")
        
        # UI Layout
        main_frame = tk.Frame(root, padx=20, pady=20)
        main_frame.pack(fill="both", expand=True)
        
        # 1. Chọn File Input
        tk.Label(main_frame, text="1. Chọn File JSON Dữ Liệu:", font=("Arial", 10, "bold")).pack(anchor="w")
        input_frame = tk.Frame(main_frame)
        input_frame.pack(fill="x", pady=5)
        tk.Entry(input_frame, textvariable=self.input_path, state="readonly").pack(side="left", fill="x", expand=True)
        tk.Button(input_frame, text="📂 Duyệt...", command=self.browse_input).pack(side="left", padx=5)
        
        # 2. Cấu hình Folder Thumbs
        tk.Label(main_frame, text="2. Tên thư mục chứa ảnh (sẽ tạo mới):", font=("Arial", 10, "bold")).pack(anchor="w", pady=(15, 0))
        tk.Label(main_frame, text="Nên đặt theo tên user (vd: thumbs_userA) để dễ quản lý.", fg="gray", font=("Arial", 9)).pack(anchor="w")
        tk.Entry(main_frame, textvariable=self.thumb_folder_name).pack(fill="x", pady=5)

        # 3. Tùy chọn Nén
        tk.Label(main_frame, text="3. Tùy chọn xử lý:", font=("Arial", 10, "bold")).pack(anchor="w", pady=(15, 0))
        
        opts_frame = tk.Frame(main_frame, bg="#f9f9f9", bd=1, relief="sunken", padx=10, pady=10)
        opts_frame.pack(fill="x", pady=5)
        
        tk.Label(opts_frame, text=f"Auto Resize: Max Width {MAX_WIDTH}px | Quality {JPEG_QUALITY}", fg="#4CAF50", bg="#f9f9f9").pack(anchor="w")
        
        self.chk_recompress = tk.Checkbutton(opts_frame, text="Nén lại ảnh cũ đã tải (Force Re-compress)", 
                                             variable=self.chk_recompress_var, bg="#f9f9f9", fg="red")
        self.chk_recompress.pack(anchor="w")
        tk.Label(opts_frame, text="   (Chế độ này sẽ chỉ nén các ảnh CHƯA ĐẠT CHUẨN, ảnh đã nén sẽ được bỏ qua)", 
                 font=("Arial", 8, "italic"), bg="#f9f9f9", fg="#666").pack(anchor="w")

        # 4. Progress
        self.progress_bar = ttk.Progressbar(main_frame, orient="horizontal", length=100, mode="determinate")
        self.progress_bar.pack(fill="x", pady=(20, 5))
        self.lbl_status = tk.Label(main_frame, textvariable=self.status_var, fg="blue")
        self.lbl_status.pack(pady=5)
        
        # 5. Action Buttons
        btn_frame = tk.Frame(main_frame)
        btn_frame.pack(pady=10)
        self.btn_start = tk.Button(btn_frame, text="🚀 Bắt đầu Xử lý", command=self.start_thread, bg="#2196F3", fg="white", font=("Arial", 12, "bold"), padx=20, pady=10)
        self.btn_start.pack()

        # Logs
        self.log_text = tk.Text(main_frame, height=8, state="disabled", bg="#f0f0f0", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

    def log(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state="disabled")

    def browse_input(self):
        filename = filedialog.askopenfilename(filetypes=[("JSON Files", "*.json")])
        if filename:
            self.input_path.set(filename)
            base = os.path.basename(filename)
            name_part = os.path.splitext(base)[0]
            if name_part.startswith("data_"):
                suggested_thumb = f"thumbs_{name_part[5:]}"
            else:
                suggested_thumb = f"thumbs_{name_part}"
            self.thumb_folder_name.set(suggested_thumb)

    def toggle_ui(self, enable):
        state = "normal" if enable else "disabled"
        self.btn_start.config(state=state)
        self.chk_recompress.config(state=state)
        
    def start_thread(self):
        if not self.input_path.get():
            messagebox.showwarning("Thiếu file", "Vui lòng chọn file JSON đầu vào!")
            return
        folder_name = self.thumb_folder_name.get().strip()
        if not folder_name:
            messagebox.showwarning("Thiếu tên folder", "Vui lòng nhập tên thư mục chứa ảnh!")
            return

        self.is_running = True
        self.toggle_ui(False)
        threading.Thread(target=self.run_process, args=(folder_name,), daemon=True).start()

    def run_process(self, thumb_folder):
        input_file = self.input_path.get()
        force_recompress = self.chk_recompress_var.get()
        
        try:
            self.status_var.set("Đang đọc dữ liệu...")
            self.log(f"Đọc file: {os.path.basename(input_file)}")
            
            with open(input_file, 'r', encoding='utf-8') as f:
                raw_data = json.load(f)

            # --- STANDARDIZE DATA TO DICT FOR PROCESSING ---
            # Chuyển đổi mọi định dạng đầu vào thành Dict {id: object} để dễ xử lý
            data_map = {}
            if isinstance(raw_data, list):
                # New Format (List) -> Convert to Dict map
                self.log("Phát hiện dữ liệu định dạng MỚI (List).")
                for item in raw_data:
                    if 'id' in item:
                        data_map[item['id']] = item
            elif isinstance(raw_data, dict):
                # Old Format (Dict) -> Use directly
                self.log("Phát hiện dữ liệu định dạng CŨ (Dict).")
                data_map = raw_data
            else:
                self.log("❌ Định dạng JSON không hợp lệ (không phải List hoặc Dict).")
                self.status_var.set("Lỗi định dạng file!")
                self.toggle_ui(True)
                return
            
            items_list = list(data_map.items())
            total = len(items_list)
            self.log(f"Tìm thấy {total} video.")
            
            base_dir = os.path.dirname(input_file)
            full_thumb_dir = os.path.join(base_dir, thumb_folder)
            
            if not os.path.exists(full_thumb_dir):
                os.makedirs(full_thumb_dir)
                self.log(f"Đã tạo thư mục: {full_thumb_dir}")
            else:
                self.log(f"Thư mục đã có: {full_thumb_dir}")
                if force_recompress:
                    self.log("⚠️ Chế độ Force Re-compress: Đang quét lại (Smart Skip ảnh đã nén)...")

            self.status_var.set("Đang xử lý ảnh...")
            self.progress_bar['maximum'] = total
            self.progress_bar['value'] = 0
            
            updated_count = 0
            skipped_count = 0
            done_count = 0
            
            # Tăng max_workers lên để xử lý ảnh nhanh hơn
            with ThreadPoolExecutor(max_workers=20) as executor:
                # Lưu ý: futures cần submit với data_map
                futures = {executor.submit(process_single_item, key, item, full_thumb_dir, force_recompress): key for key, item in items_list}
                
                for future in as_completed(futures):
                    key, fresh_url, local_path = future.result()
                    
                    if local_path is None and fresh_url is None:
                        skipped_count += 1
                    else:
                        if fresh_url:
                            data_map[key]['cover'] = fresh_url
                        
                        if local_path:
                            # Lưu path tương đối để HTML portable
                            # Đảm bảo dùng dấu / thay vì \ cho web
                            clean_path = local_path.replace("\\", "/")
                            data_map[key]['cover_off'] = clean_path
                            updated_count += 1
                        
                    done_count += 1
                    # Cập nhật thông báo chi tiết hơn
                    msg = f"Xong: {done_count}/{total} (Mới/Sửa: {updated_count}, Bỏ qua: {skipped_count})"
                    self.root.after(0, self.update_progress, done_count, total, msg)

            self.log(f"Hoàn tất! Cập nhật: {updated_count}, Skip: {skipped_count}.")
            
            # Lưu file - Truyền data_map vào để save
            self.status_var.set("Đang lưu file...")
            input_name = os.path.basename(input_file)
            name_part, ext = os.path.splitext(input_name)
            default_out = f"{name_part}_offline{ext}"
            
            self.root.after(0, lambda: self.save_file_dialog(data_map, default_out))

        except Exception as e:
            self.log(f"Lỗi: {e}")
            self.status_var.set("Có lỗi xảy ra!")
            self.toggle_ui(True)

    def update_progress(self, val, total, msg):
        self.progress_bar['value'] = val
        self.status_var.set(msg)

    def save_file_dialog(self, data_map, default_name):
        out_path = filedialog.asksaveasfilename(
            initialfile=default_name,
            defaultextension=".json",
            filetypes=[("JSON Files", "*.json")],
            title="Lưu file JSON kết quả"
        )
        
        if out_path:
            try:
                # --- QUAN TRỌNG: CHUYỂN VỀ LIST TRƯỚC KHI LƯU ---
                # Để đồng bộ với các tool khác
                save_data_list = list(data_map.values())
                
                with open(out_path, 'w', encoding='utf-8') as f:
                    json.dump(save_data_list, f, indent=4, ensure_ascii=False)
                
                messagebox.showinfo("Thành công", f"Đã lưu file (Format: LIST) tại:\n{out_path}")
                self.log(f"Đã lưu JSON: {out_path}")
            except Exception as e:
                messagebox.showerror("Lỗi lưu file", str(e))
        else:
            self.log("Đã huỷ lưu file JSON.")
            
        self.status_var.set("Hoàn thành!")
        self.toggle_ui(True)

if __name__ == "__main__":
    root = tk.Tk()
    app = ThumbDownloaderApp(root)
    root.mainloop()