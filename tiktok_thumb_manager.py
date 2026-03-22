import json
import requests
import os
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
import argparse # THÊM THƯ VIỆN ĐỂ NHẬN THAM SỐ

# Thử import Pillow để xử lý ảnh
try:
    from PIL import Image, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ================= CORE LOGIC =================

MAX_WIDTH = 480       
JPEG_QUALITY = 75     

def process_image_data(image_data_or_path, output_path, is_file_path=False):
    if not HAS_PIL:
        if not is_file_path and image_data_or_path:
            with open(output_path, 'wb') as f:
                f.write(image_data_or_path.getbuffer())
        return

    try:
        img = Image.open(image_data_or_path) if is_file_path else Image.open(image_data_or_path)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        width, height = img.size
        if width > MAX_WIDTH:
            ratio = MAX_WIDTH / width
            new_height = int(height * ratio)
            img = img.resize((MAX_WIDTH, new_height), Image.Resampling.LANCZOS)
        img.save(output_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
    except Exception as e:
        print(f"Lỗi nén ảnh {output_path}: {e}")
        if not is_file_path and image_data_or_path:
            with open(output_path, 'wb') as f:
                f.write(image_data_or_path.getbuffer())

def download_and_compress(url, video_id, thumb_dir, force_recompress=False):
    filename = f"{video_id}.jpeg"
    file_path = os.path.join(thumb_dir, filename)
    rel_path = f"{os.path.basename(thumb_dir)}/{filename}"

    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        if force_recompress and HAS_PIL:
            try:
                with Image.open(file_path) as img:
                    if img.size[0] <= MAX_WIDTH and img.format == 'JPEG':
                        return rel_path
            except Exception:
                pass
            process_image_data(file_path, file_path, is_file_path=True)
            return rel_path
        else:
            return rel_path

    if not url:
        return None

    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            image_bytes = BytesIO(response.content)
            process_image_data(image_bytes, file_path, is_file_path=False)
            return rel_path
    except Exception:
        pass
        
    return None

def process_single_item(key, item, thumb_dir, force_recompress):
    vid_id = item.get('id')
    unique_id = item.get('uniqueId')
    current_cover = item.get('cover')
    existing_offline_path = item.get('cover_off')

    if not vid_id:
        return key, None, None

    if not force_recompress and existing_offline_path:
        check_filename = os.path.basename(existing_offline_path)
        check_full_path = os.path.join(thumb_dir, check_filename)
        if os.path.exists(check_full_path) and os.path.getsize(check_full_path) > 0:
            return key, None, None

    new_cover_url = current_cover
    file_path_check = os.path.join(thumb_dir, f"{vid_id}.jpeg")
    need_download = not (os.path.exists(file_path_check) and os.path.getsize(file_path_check) > 0)

    if need_download and unique_id:
        try:
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
    
    local_path = download_and_compress(new_cover_url, vid_id, thumb_dir, force_recompress)
    
    if not local_path and current_cover and current_cover != new_cover_url:
         if need_download:
            local_path = download_and_compress(current_cover, vid_id, thumb_dir, force_recompress)

    return key, new_cover_url, local_path

# ================= GUI APP =================

class ThumbDownloaderApp:
    def __init__(self, root, auto_run=False, in_path=None, out_path=None, thumb_dir=None, force=False):
        self.root = root
        self.auto_run = auto_run
        self.auto_out_path = out_path
        
        self.root.title("TikTok Thumb Manager (Smart Skip)")
        self.root.geometry("650x550")
        
        # Variables
        default_in = in_path if in_path else "D:/OneDrive/Documents/GitHub/vhian2468.github.io/data_pe.siro_phan.json"
        default_thumb = thumb_dir if thumb_dir else "thumbs_pe.siro_phan"
        
        self.input_path = tk.StringVar(value=default_in)
        self.thumb_folder_name = tk.StringVar(value=default_thumb)
        self.status_var = tk.StringVar(value="Sẵn sàng")
        self.chk_recompress_var = tk.BooleanVar(value=force)
        self.is_running = False
        
        if not HAS_PIL and not self.auto_run:
            messagebox.showwarning("Thiếu thư viện", "Chưa cài đặt Pillow!\nChức năng nén ảnh sẽ không hoạt động.\nHãy chạy: pip install Pillow")
        
        # UI Layout
        main_frame = tk.Frame(root, padx=20, pady=20)
        main_frame.pack(fill="both", expand=True)
        
        tk.Label(main_frame, text="1. Chọn File JSON Dữ Liệu:", font=("Arial", 10, "bold")).pack(anchor="w")
        input_frame = tk.Frame(main_frame)
        input_frame.pack(fill="x", pady=5)
        tk.Entry(input_frame, textvariable=self.input_path, state="readonly").pack(side="left", fill="x", expand=True)
        tk.Button(input_frame, text="📂 Duyệt...", command=self.browse_input).pack(side="left", padx=5)
        
        tk.Label(main_frame, text="2. Tên thư mục chứa ảnh:", font=("Arial", 10, "bold")).pack(anchor="w", pady=(15, 0))
        tk.Entry(main_frame, textvariable=self.thumb_folder_name).pack(fill="x", pady=5)

        tk.Label(main_frame, text="3. Tùy chọn xử lý:", font=("Arial", 10, "bold")).pack(anchor="w", pady=(15, 0))
        opts_frame = tk.Frame(main_frame, bg="#f9f9f9", bd=1, relief="sunken", padx=10, pady=10)
        opts_frame.pack(fill="x", pady=5)
        tk.Label(opts_frame, text=f"Auto Resize: Max Width {MAX_WIDTH}px | Quality {JPEG_QUALITY}", fg="#4CAF50", bg="#f9f9f9").pack(anchor="w")
        
        self.chk_recompress = tk.Checkbutton(opts_frame, text="Nén lại ảnh cũ đã tải (Force Re-compress)", variable=self.chk_recompress_var, bg="#f9f9f9", fg="red")
        self.chk_recompress.pack(anchor="w")

        self.progress_bar = ttk.Progressbar(main_frame, orient="horizontal", length=100, mode="determinate")
        self.progress_bar.pack(fill="x", pady=(20, 5))
        self.lbl_status = tk.Label(main_frame, textvariable=self.status_var, fg="blue")
        self.lbl_status.pack(pady=5)
        
        btn_frame = tk.Frame(main_frame)
        btn_frame.pack(pady=10)
        self.btn_start = tk.Button(btn_frame, text="🚀 Bắt đầu Xử lý", command=self.start_thread, bg="#2196F3", fg="white", font=("Arial", 12, "bold"), padx=20, pady=10)
        self.btn_start.pack()

        self.log_text = tk.Text(main_frame, height=8, state="disabled", bg="#f0f0f0", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

        # NẾU CÓ CỜ AUTO THÌ TỰ CHẠY SAU 0.5 GIÂY
        if self.auto_run:
            self.root.after(500, self.start_thread)

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
                self.thumb_folder_name.set(f"thumbs_{name_part[5:]}")
            else:
                self.thumb_folder_name.set(f"thumbs_{name_part}")

    def toggle_ui(self, enable):
        state = "normal" if enable else "disabled"
        self.btn_start.config(state=state)
        self.chk_recompress.config(state=state)
        
    def start_thread(self):
        if not self.input_path.get():
            if not self.auto_run: messagebox.showwarning("Thiếu file", "Vui lòng chọn file JSON!")
            return
        folder_name = self.thumb_folder_name.get().strip()
        if not folder_name:
            if not self.auto_run: messagebox.showwarning("Thiếu folder", "Vui lòng nhập tên thư mục!")
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

            data_map = {}
            if isinstance(raw_data, list):
                for item in raw_data:
                    if 'id' in item: data_map[item['id']] = item
            elif isinstance(raw_data, dict):
                data_map = raw_data
            else:
                self.log("❌ Định dạng JSON không hợp lệ.")
                if self.auto_run: self.root.after(0, self.root.destroy)
                return
            
            items_list = list(data_map.items())
            total = len(items_list)
            
            base_dir = os.path.dirname(input_file)
            full_thumb_dir = os.path.join(base_dir, thumb_folder)
            
            if not os.path.exists(full_thumb_dir):
                os.makedirs(full_thumb_dir)

            self.status_var.set("Đang xử lý ảnh...")
            self.progress_bar['maximum'] = total
            self.progress_bar['value'] = 0
            
            updated_count = 0
            skipped_count = 0
            done_count = 0
            
            with ThreadPoolExecutor(max_workers=40) as executor:
                futures = {executor.submit(process_single_item, key, item, full_thumb_dir, force_recompress): key for key, item in items_list}
                
                for future in as_completed(futures):
                    key, fresh_url, local_path = future.result()
                    
                    if local_path is None and fresh_url is None:
                        skipped_count += 1
                    else:
                        if fresh_url: data_map[key]['cover'] = fresh_url
                        if local_path:
                            data_map[key]['cover_off'] = local_path.replace("\\", "/")
                            updated_count += 1
                        
                    done_count += 1
                    msg = f"Xong: {done_count}/{total} (Mới/Sửa: {updated_count}, Bỏ qua: {skipped_count})"
                    self.root.after(0, self.update_progress, done_count, total, msg)

            self.status_var.set("Đang lưu file...")
            input_name = os.path.basename(input_file)
            name_part, ext = os.path.splitext(input_name)
            default_out = f"{name_part}_offline{ext}"
            
            self.root.after(0, lambda: self.save_file_dialog(data_map, default_out))

        except Exception as e:
            self.log(f"Lỗi: {e}")
            self.status_var.set("Có lỗi xảy ra!")
            self.toggle_ui(True)
            if self.auto_run: self.root.after(0, self.root.destroy)

    def update_progress(self, val, total, msg):
        self.progress_bar['value'] = val
        self.status_var.set(msg)

    def save_file_dialog(self, data_map, default_name):
        # NẾU CÓ CỜ AUTO -> BỎ QUA POPUP CHỌN FILE, LƯU THẲNG
        if self.auto_run and self.auto_out_path:
            out_path = self.auto_out_path
        else:
            out_path = filedialog.asksaveasfilename(
                initialfile=default_name,
                defaultextension=".json",
                filetypes=[("JSON Files", "*.json")],
                title="Lưu file JSON kết quả"
            )
        
        if out_path:
            try:
                save_data_list = list(data_map.values())
                with open(out_path, 'w', encoding='utf-8') as f:
                    json.dump(save_data_list, f, indent=4, ensure_ascii=False)
                
                if not self.auto_run:
                    messagebox.showinfo("Thành công", f"Đã lưu file tại:\n{out_path}")
                self.log(f"Đã lưu JSON: {out_path}")
            except Exception as e:
                if not self.auto_run:
                    messagebox.showerror("Lỗi lưu file", str(e))
        
        self.status_var.set("Hoàn thành!")
        self.toggle_ui(True)
        
        # NẾU CÓ CỜ AUTO -> TỰ ĐÓNG APP SAU KHI LƯU XONG
        if self.auto_run:
            self.root.after(1000, self.root.destroy)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TikTok Thumb Manager")
    parser.add_argument("--auto", action="store_true", help="Chạy tự động (không chờ người dùng bấm)")
    parser.add_argument("--input", type=str, help="Đường dẫn file JSON input")
    parser.add_argument("--output", type=str, help="Đường dẫn file JSON output")
    parser.add_argument("--thumb-dir", type=str, help="Tên thư mục chứa ảnh (vd: thumbs_pe.siro_phan)")
    parser.add_argument("--force", action="store_true", help="Bắt buộc nén lại ảnh")
    
    args = parser.parse_args()

    root = tk.Tk()
    app = ThumbDownloaderApp(
        root, 
        auto_run=args.auto, 
        in_path=args.input, 
        out_path=args.output, 
        thumb_dir=args.thumb_dir, 
        force=args.force
    )
    root.mainloop()