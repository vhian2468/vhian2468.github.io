import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import json
import os
import time
import webbrowser
from datetime import datetime

# ================= HTML GENERATOR LOGIC (UPDATED FOR OFFLINE THUMBS) =================
def generate_static_html(username, data_list):
    html_template = r"""
    <!DOCTYPE html>
    <html lang="vi">
    <head>
        <meta charset="UTF-8">
        <title>Repo: __USERNAME__</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <style>
            :root { 
                --primary: #fe2c55; 
                --bg: #121212; 
                --card-bg: #1e1e1e; 
                --sidebar-bg: #000; 
                --text: #fff; 
                --text-sub: #888; 
                --sidebar-width: 280px; 
                
                /* MÀU MỚI */
                --neon-green: #39ff14;
                --neon-yellow: #fff01f;
            }
            body { background-color: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 0; padding: 0; display: flex; height: 100vh; overflow: hidden; }
            
            /* === SIDEBAR === */
            .sidebar { 
                width: var(--sidebar-width); 
                background: var(--sidebar-bg); 
                border-right: 1px solid #333; 
                display: flex; flex-direction: column; 
                transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1); 
                position: fixed; top: 0; bottom: 0; left: 0; 
                z-index: 2000; 
                transform: translateX(-100%); 
                box-shadow: 2px 0 10px rgba(0,0,0,0.5);
            }
            .sidebar.expanded { transform: translateX(0); }
            
            .sidebar-toggle-arrow {
                position: absolute; top: 70px; right: -32px;
                width: 32px; height: 40px;
                background: var(--primary); color: #fff;
                border: none; border-radius: 0 6px 6px 0;
                cursor: pointer; display: flex; align-items: center; justify-content: center;
                font-size: 18px; box-shadow: 2px 2px 5px rgba(0,0,0,0.3); z-index: 2001;
            }
            .sidebar-toggle-arrow:hover { filter: brightness(1.1); }

            .sidebar-header { padding: 15px; border-bottom: 1px solid #333; display: flex; justify-content: space-between; align-items: center; background: #111; }
            .sidebar-title { font-weight: bold; color: var(--primary); font-size: 1.1em; }
            
            .date-list { flex-grow: 1; overflow-y: auto; padding: 0; -webkit-overflow-scrolling: touch; }
            
            /* Sidebar Section Header */
            .sb-section {
                background: #222;
                color: #aaa;
                font-size: 0.85em;
                font-weight: bold;
                padding: 8px 12px;
                border-bottom: 1px solid #333;
                border-top: 1px solid #333;
                text-transform: uppercase;
                position: sticky; top: 0;
            }
            .sb-section.repost-sec { color: var(--neon-green); border-left: 4px solid var(--neon-green); }
            .sb-section.create-sec { color: var(--primary); border-left: 4px solid var(--primary); margin-top: 10px;}

            .date-item { padding: 10px 15px; cursor: pointer; border-bottom: 1px solid #222; font-size: 0.95em; display: flex; justify-content: space-between; color: #ccc; }
            .date-item:hover, .date-item:active { background: #333; color: #fff; }
            .date-count { background: #444; padding: 2px 8px; border-radius: 10px; font-size: 0.8em; }

            /* MAIN CONTENT */
            .main-container { 
                flex-grow: 1; display: flex; flex-direction: column; height: 100%; margin-left: 0; width: 100%; transition: margin-left 0.3s ease; 
            }
            
            .controls { background: #181818; padding: 10px; border-bottom: 1px solid #333; z-index: 100; display: flex; flex-direction: column; gap: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
            .top-bar { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
            .stats { font-size: 0.9em; color: #aaa; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
            
            .nav-bar { display: flex; gap: 8px; overflow-x: auto; padding-bottom: 2px; }
            .nav-group { display: flex; gap: 5px; background: #222; padding: 4px; border-radius: 6px; align-items: center; }
            
            input, select, button { padding: 8px; border-radius: 6px; border: 1px solid #444; background: #2f2f2f; color: white; font-size: 14px; outline: none; }
            input:focus, select:focus { border-color: var(--primary); }
            button { cursor: pointer; background: #444; font-weight: bold; }
            
            .btn-primary { background: var(--primary); border: none; }
            .btn-go { width: 40px; text-align: center; padding: 8px 0; }
            .btn-action { min-width: 60px; }
            .btn-danger { background: #822; border: none; }

            .scroll-area { flex-grow: 1; overflow-y: auto; padding: 10px; -webkit-overflow-scrolling: touch; background: #000; }
            .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 15px; }
            
            /* CARD DESIGN UPDATED */
            .card { background: var(--card-bg); border-radius: 8px; overflow: hidden; border: 1px solid #333; position: relative; display: flex; flex-direction: column; transition: 0.2s; }
            .card:hover { transform: translateY(-3px); border-color: #666; }
            
            /* REPOST HIGHLIGHT */
            .card.has-repost-caption { border: 2px solid var(--neon-green) !important; box-shadow: 0 0 10px rgba(57, 255, 20, 0.2); }

            .card.highlight { border: 2px solid #ffee00 !important; box-shadow: 0 0 15px rgba(255, 238, 0, 0.6); z-index: 10; }

            .thumb-link { display: block; position: relative; padding-top: 140%; background: #111; }
            .thumb { position: absolute; top: 0; left: 0; width: 100%; height: 100%; object-fit: cover; transition: opacity 0.3s; opacity: 0; }
            .thumb.loaded { opacity: 1; }
            /* Thêm fallback background nếu ảnh lỗi */
            .thumb.error { opacity: 0.5; object-fit: contain; padding: 20px; box-sizing: border-box; }
            
            .info { padding: 12px; flex-grow: 1; display: flex; flex-direction: column; justify-content: space-between; }
            
            /* DATE STYLES */
            .date-repost { color: var(--neon-green); font-size: 0.9em; font-weight: bold; margin-bottom: 4px; display: flex; align-items: center; gap: 5px; text-shadow: 0 0 5px rgba(57,255,20,0.4); }
            .caption-repost { color: var(--neon-yellow); font-size: 0.95em; font-weight: bold; margin-bottom: 8px; font-style: italic; border-left: 2px solid var(--neon-yellow); padding-left: 8px; line-height: 1.3; }
            .date-normal { color: var(--primary); font-size: 0.85em; font-weight: normal; margin-bottom: 4px; opacity: 0.9; }

            .nickname { font-weight: bold; font-size: 1rem; display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px;}
            .uid { font-size: 0.85em; color: var(--text-sub); display: block; margin-bottom: 4px; }
            .desc { font-size: 0.9em; color: #ddd; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; height: 2.8em; margin: 6px 0; line-height: 1.4; }
            
            .metrics { font-size: 0.8em; color: var(--text-sub); display: flex; gap: 12px; border-top: 1px solid #333; padding-top: 8px; margin-top: auto; }
            .cursor-badge { position: absolute; top: 8px; left: 8px; background: rgba(0,0,0,0.7); color: #fff; padding: 4px 8px; font-size: 12px; border-radius: 4px; z-index: 10; font-weight: bold; pointer-events: none; }
            .order-badge { position: absolute; top: 8px; right: 8px; background: var(--neon-green); color: #000; padding: 2px 6px; font-size: 11px; border-radius: 4px; z-index: 10; font-weight: bold; pointer-events: none; }

            #loadingMsg { text-align: center; padding: 20px; color: #666; width: 100%; grid-column: 1 / -1; }

            @media (max-width: 768px) {
                .grid { grid-template-columns: repeat(2, 1fr); gap: 10px; }
                .controls { gap: 8px; }
                .nav-bar { flex-wrap: wrap; }
                .nickname { font-size: 0.9rem; }
                .metrics { font-size: 0.75em; gap: 8px; }
                .sidebar-toggle-arrow { height: 50px; width: 36px; font-size: 20px; top: 60px; }
            }
        </style>
    </head>
    <body>
    
    <div id="sidebar" class="sidebar">
        <button class="sidebar-toggle-arrow" id="sidebarToggle" onclick="toggleSidebar()">▶</button>
        <div class="sidebar-header">
            <span class="sidebar-title">🗂 DATA MANAGER</span>
        </div>
        <div id="dateList" class="date-list"></div>
    </div>
    
    <div id="mainContainer" class="main-container">
        <div class="controls">
            <div class="top-bar">
                <div class="stats">
                    <span style="color:#fe2c55; font-weight:bold;">__USERNAME__</span> 
                    (<span id="totalCount">0</span> items)
                </div>
            </div>
            
            <div class="nav-bar">
                <div class="nav-group" style="flex-grow: 2;">
                    <input type="text" id="searchInput" placeholder="🔍 Tìm: caption, date, repost..." style="width: 100%;">
                </div>
                <div class="nav-group" style="flex-grow: 1;">
                    <select id="sortSelect" style="width: 100%;">
                        <option value="feed">Thứ tự Feed</option>
                        <option value="repost_order">Thứ tự Repost (1->New)</option>
                        <option value="repost_newest">Repost mới nhất</option>
                        <option value="newest_create">Ngày tạo mới nhất</option>
                    </select>
                </div>
            </div>
            
            <div class="nav-bar">
                <div class="nav-group">
                    <!-- NEW: Repost Order Search -->
                    <input type="number" id="jumpOrderInput" placeholder="Ord" style="width: 50px;">
                    <button onclick="jumpToOrder()" class="btn-primary btn-go">Go</button>
                    
                    <span style="border-left:1px solid #555; height:20px; margin:0 4px;"></span>

                    <!-- EXISTING: Feed Index Search -->
                    <input type="number" id="jumpInput" placeholder="Feed" style="width: 50px;">
                    <button onclick="jumpTo()" class="btn-primary btn-go">Go</button>
                </div>
                <div class="nav-group">
                    <button onclick="scrollOffset(-300)" class="btn-action" title="Lên">-300</button>
                    <button onclick="scrollOffset(300)" class="btn-action" title="Xuống">+300</button>
                </div>
                <div class="nav-group">
                    <button onclick="renderAll()" class="btn-danger">All</button>
                    <button onclick="scrollToTop()">⬆️ Top</button>
                    <button onclick="scrollToEnd()">⬇️ End</button> <!-- NEW: End Button -->
                </div>
            </div>
        </div>
        
        <div class="scroll-area" id="scrollArea">
            <div class="grid" id="videoGrid"></div>
            <div id="loadingMsg">Đang tải data...</div>
        </div>
    </div>

    <!-- SỬ DỤNG TIMESTAMP ĐỂ TRÁNH CACHE JS -->
    <script src="data___USERNAME__.js?t=__TIMESTAMP__"></script>
    
    <script>
        const grid = document.getElementById('videoGrid');
        const scrollArea = document.getElementById('scrollArea');
        const searchInput = document.getElementById('searchInput');
        const sortSelect = document.getElementById('sortSelect');
        const dateListEl = document.getElementById('dateList');
        const loadingMsg = document.getElementById('loadingMsg');
        
        const jumpInput = document.getElementById('jumpInput');
        const jumpOrderInput = document.getElementById('jumpOrderInput');
        
        const sidebar = document.getElementById('sidebar');
        const sidebarToggle = document.getElementById('sidebarToggle');
        
        let fullData = [];
        let displayData = [];
        let renderedStart = 0;
        let renderedEnd = 0;
        const BATCH_SIZE = 40; 

        if (window.tiktokData && Array.isArray(window.tiktokData)) {
            fullData = window.tiktokData;
            startApp();
        } else {
            loadingMsg.innerText = "Không tìm thấy dữ liệu JS!";
        }

        function startApp() {
            document.getElementById('totalCount').innerText = fullData.length;
            displayData = [...fullData];
            renderSidebar();
            resetRender();
            renderDown(); 
            scrollArea.addEventListener('scroll', onScroll);
        }

        function toggleSidebar() {
            sidebar.classList.toggle('expanded');
            sidebarToggle.innerHTML = sidebar.classList.contains('expanded') ? '◀' : '▶';
        }

        function resetRender() {
            grid.innerHTML = '';
            renderedStart = 0;
            renderedEnd = 0;
        }

        function createCard(v) {
            const el = document.createElement('div');
            let extraClass = '';
            if (v.repost_caption) extraClass = 'has-repost-caption';
            
            el.className = `card ${extraClass}`;
            el.id = 'card-' + v.id;
            
            const descText = (v.desc || '').replace(/"/g, '&quot;');
            let topHtml = '';
            
            if (v.repost_date) topHtml += `<div class="date-repost">♻️ ${v.repost_date}</div>`;
            if (v.repost_caption) topHtml += `<div class="caption-repost">💬 "${v.repost_caption}"</div>`;
            
            const normalDateHtml = `<div class="date-normal">📅 Tạo: ${v.date}</div>`;
            
            // Show Repost Order if exists
            let orderHtml = '';
            if (v.repost_order) orderHtml = `<span class="order-badge">Ord: ${v.repost_order}</span>`;

            // === LOGIC XỬ LÝ ẢNH OFFLINE (Tích hợp) ===
            // 1. Ưu tiên cover_off (đường dẫn local) nếu có và không rỗng
            // 2. Fallback sang cover (link online)
            const thumbSrc = (v.cover_off && v.cover_off.trim() !== "") ? v.cover_off : v.cover;
            
            // Script xử lý lỗi trong HTML:
            // Nếu ảnh hiện tại (thumbSrc) lỗi và nó chứa đường dẫn offline -> thử load lại bằng link online.
            // Nếu vẫn lỗi -> hiện placeholder error.
            const onErrorScript = `
                this.onerror=null; 
                if(this.src.indexOf('${v.cover_off}') !== -1 && '${v.cover}' !== 'undefined') { 
                    this.src='${v.cover}'; 
                } else { 
                    this.classList.add('error'); 
                }
            `;

            el.innerHTML = `
                <a href="https://www.tiktok.com/@${v.uniqueId}/video/${v.id}" target="_blank" class="thumb-link">
                    <span class="cursor-badge">#${v.feed_index}</span>
                    ${orderHtml}
                    <img src="${thumbSrc}" class="thumb" 
                         onload="this.classList.add('loaded')" 
                         onerror="${onErrorScript.replace(/\n/g, '')}"
                         loading="lazy">
                </a>
                <div class="info">
                    <div class="meta-top">${topHtml}${normalDateHtml}</div>
                    <span class="nickname">${v.nickname}</span>
                    <span class="uid">@${v.uniqueId}</span>
                    <div class="desc" title="${descText}">${v.desc || ''}</div>
                    <div class="metrics">
                        <span>❤️ ${v.digg}</span><span>💬 ${v.comment}</span><span>🔄 ${v.share}</span>
                    </div>
                </div>
            `;
            return el;
        }

        function onScroll() {
            const st = scrollArea.scrollTop;
            const sh = scrollArea.scrollHeight;
            const ch = scrollArea.clientHeight;
            if (st + ch >= sh - 600) renderDown();
            if (st <= 200 && renderedStart > 0) renderUp();
        }

        function renderDown(forceCount = -1) {
            if (renderedEnd >= displayData.length) { loadingMsg.style.display = 'none'; return; }
            let count = (forceCount > -1) ? forceCount : BATCH_SIZE;
            let end = Math.min(renderedEnd + count, displayData.length);
            const batch = displayData.slice(renderedEnd, end);
            const fragment = document.createDocumentFragment();
            batch.forEach(v => fragment.appendChild(createCard(v)));
            grid.appendChild(fragment);
            renderedEnd = end;
            loadingMsg.style.display = (renderedEnd >= displayData.length) ? 'none' : 'block';
        }

        function renderUp() {
            if (renderedStart <= 0) return;
            const oldScrollHeight = scrollArea.scrollHeight;
            const oldScrollTop = scrollArea.scrollTop;
            let count = BATCH_SIZE;
            let start = Math.max(0, renderedStart - count);
            const batch = displayData.slice(start, renderedStart);
            const fragment = document.createDocumentFragment();
            batch.forEach(v => fragment.appendChild(createCard(v)));
            grid.insertBefore(fragment, grid.firstChild);
            renderedStart = start;
            scrollArea.scrollTop = oldScrollTop + (scrollArea.scrollHeight - oldScrollHeight);
        }

        function renderAll() {
            if (!confirm("⚠️ CẢNH BÁO: Hiển thị tất cả có thể gây lag!")) return;
            loadingMsg.innerText = "Rendering all...";
            setTimeout(() => { renderDown(displayData.length - renderedEnd); }, 50);
        }

        function scrollOffset(amount) {
            const cardHeight = 400; 
            if (amount > 0) {
                renderDown(amount);
                setTimeout(() => scrollArea.scrollBy({ top: (amount/2)*cardHeight, behavior: 'smooth' }), 50);
            } else {
                scrollArea.scrollBy({ top: -((Math.abs(amount)/2)*cardHeight), behavior: 'smooth' });
            }
        }

        function jumpToId(vid) {
            const index = displayData.findIndex(v => v.id === vid);
            if (index === -1) { alert("Video không có trong danh sách lọc hiện tại!"); return; }
            grid.innerHTML = '';
            renderedStart = Math.max(0, index - 20); 
            renderedEnd = renderedStart; 
            renderDown(50);
            setTimeout(() => {
                const el = document.getElementById('card-' + vid);
                if (el) {
                    el.scrollIntoView({ behavior: 'auto', block: 'center' });
                    el.classList.add('highlight');
                    setTimeout(() => el.classList.remove('highlight'), 2000);
                }
            }, 50);
        }

        // --- NEW FUNCTIONS: Order Jump & End Scroll ---

        function jumpToOrder() {
            const targetOrder = parseInt(jumpOrderInput.value);
            if (isNaN(targetOrder)) return;
            
            const targetVid = displayData.find(v => v.repost_order == targetOrder);
            if (targetVid) {
                jumpToId(targetVid.id);
            } else {
                alert("Không tìm thấy Order: " + targetOrder);
            }
        }

        function jumpTo() {
            const targetIndex = parseInt(jumpInput.value);
            if (isNaN(targetIndex)) return;
            const targetVid = displayData.find(v => v.feed_index == targetIndex);
            if (targetVid) jumpToId(targetVid.id);
            else alert("Không tìm thấy Feed Index: " + targetIndex);
        }

        function scrollToTop() {
            resetRender();
            renderDown();
            scrollArea.scrollTop = 0;
        }

        function scrollToEnd() {
            resetRender();
            
            // Force render the last batch
            renderedEnd = displayData.length;
            renderedStart = Math.max(0, renderedEnd - 50); // Render 50 item cuối
            
            const batch = displayData.slice(renderedStart, renderedEnd);
            const fragment = document.createDocumentFragment();
            batch.forEach(v => fragment.appendChild(createCard(v)));
            grid.appendChild(fragment);
            
            loadingMsg.style.display = 'none';
            
            setTimeout(() => {
                scrollArea.scrollTop = scrollArea.scrollHeight;
            }, 50);
        }

        function renderSidebar() {
            const repostMap = {};
            const createMap = {};

            fullData.forEach(v => {
                if (v.repost_date) {
                    if (!repostMap[v.repost_date]) repostMap[v.repost_date] = [];
                    repostMap[v.repost_date].push(v);
                }
                if (v.date) {
                    if (!createMap[v.date]) createMap[v.date] = [];
                    createMap[v.date].push(v);
                }
            });

            const sortedRepost = Object.keys(repostMap).sort((a, b) => b.localeCompare(a));
            const sortedCreate = Object.keys(createMap).sort((a, b) => b.localeCompare(a));

            let html = '';
            if (sortedRepost.length > 0) {
                html += `<div class="sb-section repost-sec">♻️ NGÀY REPOST (${sortedRepost.length})</div>`;
                html += sortedRepost.map(date => 
                    `<div class="date-item" onclick="scrollToDate('${date}', 'repost')">
                        <span>${date}</span><span class="date-count" style="color:#39ff14">${repostMap[date].length}</span>
                    </div>`
                ).join('');
            }
            html += `<div class="sb-section create-sec">📅 NGÀY ĐĂNG (${sortedCreate.length})</div>`;
            html += sortedCreate.map(date => 
                `<div class="date-item" onclick="scrollToDate('${date}', 'create')">
                    <span>${date}</span><span class="date-count">${createMap[date].length}</span>
                </div>`
            ).join('');

            dateListEl.innerHTML = html;
        }

        window.scrollToDate = function(dateStr, type) {
            if (window.innerWidth < 768) toggleSidebar();
            let targetVid = null;
            
            if (type === 'repost') {
                const candidates = fullData.filter(v => v.repost_date === dateStr);
                if (candidates.length > 0) targetVid = candidates[0];
            } else {
                const candidates = fullData.filter(v => v.date === dateStr);
                if (candidates.length > 0) {
                    candidates.sort((a, b) => (BigInt(b.id) > BigInt(a.id)) ? 1 : -1);
                    targetVid = candidates[0];
                }
            }

            if (targetVid) {
                if (searchInput.value !== '') {
                    searchInput.value = '';
                    handleFilter(); 
                }
                jumpToId(targetVid.id);
            }
        };

        function handleFilter() {
            const term = searchInput.value.toLowerCase();
            const sortMode = sortSelect.value;
            resetRender();
            loadingMsg.style.display = 'block';
            
            displayData = fullData.filter(v => 
                (v.nickname && v.nickname.toLowerCase().includes(term)) || 
                (v.date && v.date.includes(term)) ||
                (v.repost_date && v.repost_date.includes(term)) ||
                (v.repost_caption && v.repost_caption.toLowerCase().includes(term)) ||
                (v.desc && v.desc.toLowerCase().includes(term)) ||
                (v.uniqueId && v.uniqueId.toLowerCase().includes(term))
            );

            if (sortMode === 'feed') {
                displayData.sort((a, b) => (a.feed_index || 9e9) - (b.feed_index || 9e9));
            } else if (sortMode === 'repost_order') {
                // Sort by repost_order descending (Newest Repost first)
                displayData.sort((a, b) => (b.repost_order || 0) - (a.repost_order || 0));
            } else if (sortMode === 'repost_newest') {
                displayData.sort((a, b) => {
                    if (a.repost_date && !b.repost_date) return -1;
                    if (!a.repost_date && b.repost_date) return 1;
                    if (a.repost_date && b.repost_date) return b.repost_date.localeCompare(a.repost_date);
                    return 0;
                });
            } else if (sortMode === 'newest_create') {
                displayData.sort((a, b) => (BigInt(b.id) > BigInt(a.id)) ? 1 : -1);
            }
            
            renderDown();
        }
        
        searchInput.addEventListener('input', () => { clearTimeout(window.searchTimer); window.searchTimer = setTimeout(handleFilter, 300); });
        sortSelect.addEventListener('change', handleFilter);
    </script>
    </body>
    </html>
    """
    html_content = html_template.replace("__USERNAME__", username)
    html_content = html_content.replace("__TIMESTAMP__", str(int(time.time())))
    return html_content

# ================= GUI APP =================
class TikTokManagerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("TikTok HTML Generator & Data Manager")
        self.root.geometry("1000x600")
        
        self.data = {}
        self.data_list = []
        self.current_file = ""
        self.username = ""
        self.filtered_list = []
        
        # === LAYOUT ===
        # Top Frame: Actions
        self.top_frame = tk.Frame(root, bg="#eee", padx=10, pady=10)
        self.top_frame.pack(fill="x")
        
        tk.Button(self.top_frame, text="📂 Chọn File JSON", command=self.load_file, bg="white").pack(side="left", padx=5)
        self.lbl_file = tk.Label(self.top_frame, text="Chưa chọn file", bg="#eee", fg="#555")
        self.lbl_file.pack(side="left", padx=5)
        
        tk.Button(self.top_frame, text="🛠 Tạo Repost Order", command=self.calc_repost_order, bg="#ffdddd").pack(side="right", padx=5)
        
        # NEW: AUTO FILL BUTTON
        tk.Button(self.top_frame, text="⚡ Điền Ngày Repost", command=self.auto_fill_repost_dates, bg="#fff0dd").pack(side="right", padx=5)
        
        tk.Button(self.top_frame, text="🌐 Xuất HTML", command=self.export_html, bg="#ddffdd").pack(side="right", padx=5)

        # Main PanedWindow (Split Left/Right)
        self.paned = tk.PanedWindow(root, orient="horizontal")
        self.paned.pack(fill="both", expand=True, padx=10, pady=10)
        
        # --- LEFT: LIST & SEARCH ---
        self.left_frame = tk.Frame(self.paned)
        self.paned.add(self.left_frame, width=600)
        
        # Search Bar (Fixed: No 'placeholder' arg)
        self.search_var = tk.StringVar()
        self.search_var.trace("w", self.on_search)
        
        # Dùng Label thay cho placeholder
        tk.Label(self.left_frame, text="🔍 Tìm kiếm (ID, Caption, Date):").pack(anchor="w", padx=5)
        tk.Entry(self.left_frame, textvariable=self.search_var).pack(fill="x", padx=5, pady=5)
        
        # Treeview (List)
        self.tree = ttk.Treeview(self.left_frame, columns=("feed_index", "date", "nickname", "repost_date", "order"), show="headings")
        self.tree.heading("feed_index", text="#")
        self.tree.heading("date", text="Ngày tạo")
        self.tree.heading("nickname", text="Nickname")
        self.tree.heading("repost_date", text="Ngày Repost")
        self.tree.heading("order", text="Ord")
        
        self.tree.column("feed_index", width=50)
        self.tree.column("date", width=90)
        self.tree.column("nickname", width=120)
        self.tree.column("repost_date", width=90)
        self.tree.column("order", width=50)
        
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_select_item)
        
        # --- RIGHT: EDITOR ---
        self.right_frame = tk.Frame(self.paned, bg="#f9f9f9", bd=1, relief="sunken")
        self.paned.add(self.right_frame)
        
        tk.Label(self.right_frame, text="✏️ CHỈNH SỬA", font=("Arial", 12, "bold"), bg="#f9f9f9").pack(pady=10)
        
        # Form
        self.form_frame = tk.Frame(self.right_frame, bg="#f9f9f9", padx=10)
        self.form_frame.pack(fill="x")
        
        self.lbl_info = tk.Label(self.form_frame, text="Chọn 1 video bên trái...", bg="#f9f9f9", justify="left")
        self.lbl_info.pack(anchor="w", pady=5)
        
        tk.Label(self.form_frame, text="Ngày Repost (YYYY-MM-DD):", bg="#f9f9f9").pack(anchor="w")
        self.entry_repost_date = tk.Entry(self.form_frame)
        self.entry_repost_date.pack(fill="x", pady=2)
        # SỬA LỖI Ở ĐÂY: thay text_color thành fg
        tk.Button(self.form_frame, text="Hôm nay", command=self.set_today, fg="blue", height=1).pack(anchor="e")
        
        tk.Label(self.form_frame, text="Repost Caption:", bg="#f9f9f9").pack(anchor="w", pady=(10, 0))
        self.entry_repost_cap = tk.Entry(self.form_frame)
        self.entry_repost_cap.pack(fill="x", pady=2)
        
        tk.Label(self.form_frame, text="Repost Order (Tự động):", bg="#f9f9f9").pack(anchor="w", pady=(10, 0))
        self.entry_repost_order = tk.Entry(self.form_frame, state="readonly")
        self.entry_repost_order.pack(fill="x", pady=2)

        tk.Button(self.form_frame, text="💾 LƯU THAY ĐỔI", command=self.save_current_item, bg="#2196F3", fg="white", font=("Arial", 10, "bold"), pady=10).pack(fill="x", pady=20)
        
        self.btn_open_web = tk.Button(self.form_frame, text="🌐 Mở trên Web", command=self.open_in_browser)
        self.btn_open_web.pack(fill="x")

        self.selected_vid = None

    def load_file(self):
        filename = filedialog.askopenfilename(filetypes=[("JSON Files", "*.json")])
        if not filename: return
        
        try:
            with open(filename, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            
            # --- TỰ ĐỘNG CHUẨN HÓA SANG LIST ---
            if isinstance(raw_data, dict):
                # Format cũ (Dict) -> Chuyển thành List
                self.data_list = list(raw_data.values())
            elif isinstance(raw_data, list):
                # Format mới (List) -> Giữ nguyên
                self.data_list = raw_data
            else:
                self.data_list = []

            # Tạo Map Dict nội bộ để tìm kiếm nhanh theo ID
            self.data = {v['id']: v for v in self.data_list if 'id' in v}
            
            # Extract username
            base = os.path.basename(filename)
            if base.startswith("data_") and base.endswith(".json"):
                self.username = base[5:-5]
            else:
                self.username = "unknown"
                
            self.current_file = filename
            self.lbl_file.config(text=f"User: {self.username} | {len(self.data_list)} videos")
            
            # Sort mặc định
            self.data_list.sort(key=lambda x: x.get('feed_index', 999999))
            
            self.refresh_tree()
            messagebox.showinfo("OK", "Đã tải dữ liệu thành công! (Tự động convert về List)")
            
        except Exception as e:
            messagebox.showerror("Lỗi", f"Không đọc được file: {e}")

    def refresh_tree(self):
        # Clear tree
        for item in self.tree.get_children():
            self.tree.delete(item)
            
        term = self.search_var.get().lower()
        
        self.filtered_list = []
        for v in self.data_list:
            # Search filter
            match = False
            if term in str(v.get('id', '')).lower(): match = True
            if term in str(v.get('desc', '')).lower(): match = True
            if term in str(v.get('nickname', '')).lower(): match = True
            if term in str(v.get('repost_date', '')).lower(): match = True
            
            if not term or match:
                self.filtered_list.append(v)
                
        # Insert to tree
        for v in self.filtered_list:
            self.tree.insert("", "end", values=(
                v.get('feed_index', ''),
                v.get('date', ''),
                v.get('nickname', ''),
                v.get('repost_date', ''),
                v.get('repost_order', '')
            ), tags=(v['id'],))

    def on_search(self, *args):
        self.refresh_tree()

    def on_select_item(self, event):
        sel = self.tree.selection()
        if not sel: return
        
        # Get ID from tags
        item_id = self.tree.item(sel[0], "tags")[0]
        self.selected_vid = item_id
        
        v = self.data.get(item_id)
        if not v: return
        
        # Fill Form
        self.lbl_info.config(text=f"ID: {v['id']}\nNick: {v['nickname']}\nDate: {v['date']}\nDesc: {v['desc'][:50]}...")
        
        self.entry_repost_date.delete(0, tk.END)
        self.entry_repost_date.insert(0, v.get('repost_date', ''))
        
        self.entry_repost_cap.delete(0, tk.END)
        self.entry_repost_cap.insert(0, v.get('repost_caption', ''))
        
        self.entry_repost_order.config(state="normal")
        self.entry_repost_order.delete(0, tk.END)
        self.entry_repost_order.insert(0, v.get('repost_order', ''))
        self.entry_repost_order.config(state="readonly")

    def set_today(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self.entry_repost_date.delete(0, tk.END)
        self.entry_repost_date.insert(0, today)

    def save_current_item(self):
        if not self.selected_vid or self.selected_vid not in self.data: return
        
        v = self.data[self.selected_vid]
        v['repost_date'] = self.entry_repost_date.get().strip()
        v['repost_caption'] = self.entry_repost_cap.get().strip()
        
        # Update UI List
        self.refresh_tree()
        self.save_json_file()

    def open_in_browser(self):
        if not self.selected_vid: return
        v = self.data[self.selected_vid]
        url = f"https://www.tiktok.com/@{v['uniqueId']}/video/{v['id']}"
        webbrowser.open(url)

    def calc_repost_order(self):
        if not self.data_list: return
        
        confirm = messagebox.askyesno("Xác nhận", "Tính toán lại Repost Order?\n\nLuật: Feed Index Cao Nhất (Video Cũ Nhất trong Feed) => Order = 1.")
        if not confirm: return
        
        # 1. Sort list by feed_index DESC (Max to Min)
        sorted_items = sorted(self.data_list, key=lambda x: x.get('feed_index', 0), reverse=True)
        
        # 2. Assign Order
        for idx, item in enumerate(sorted_items):
            item['repost_order'] = idx + 1
            # Update main dict to ensure sync
            self.data[item['id']]['repost_order'] = idx + 1
            
        self.refresh_tree()
        self.save_json_file()
        messagebox.showinfo("Thành công", f"Đã cập nhật thứ tự cho {len(sorted_items)} video!")
    
    def auto_fill_repost_dates(self):
        """
        Tính năng: Tự động điền ngày repost cho các video nằm giữa 2 video có cùng ngày repost.
        Dựa trên thứ tự Feed Index (từ 0 -> N).
        """
        if not self.data_list: return
        
        if not messagebox.askyesno("Xác nhận", "Tính năng này sẽ tìm các cặp video có cùng Ngày Repost\nvà tự động điền ngày đó cho TẤT CẢ video nằm giữa chúng.\n\nBạn có muốn tiếp tục?"):
            return

        # 1. Sắp xếp danh sách theo Feed Index (Tăng dần) để đảm bảo tính liền mạch
        self.data_list.sort(key=lambda x: x.get('feed_index', 0))
        
        updates_count = 0
        
        # 2. Lấy danh sách chỉ mục (index) của các video ĐÃ CÓ repost_date
        # indices_with_date chứa các index trong data_list
        indices_with_date = [i for i, x in enumerate(self.data_list) if x.get('repost_date', '').strip()]

        if len(indices_with_date) < 2:
            messagebox.showinfo("Thông báo", "Cần ít nhất 2 video đã có ngày Repost để thực hiện tính năng này.")
            return

        # 3. Duyệt qua từng cặp anchor liền kề
        for i in range(len(indices_with_date) - 1):
            start_idx = indices_with_date[i]
            end_idx = indices_with_date[i+1]

            vid_start = self.data_list[start_idx]
            vid_end = self.data_list[end_idx]
            
            date_start = vid_start.get('repost_date')
            date_end = vid_end.get('repost_date')

            # Nếu 2 mốc này có cùng ngày -> Điền cho tất cả video ở giữa
            if date_start == date_end and date_start:
                # Duyệt các video nằm giữa start_idx và end_idx
                for k in range(start_idx + 1, end_idx):
                    # Chỉ điền nếu chưa có ngày (hoặc ghi đè luôn để đảm bảo đồng bộ - ở đây chọn ghi đè)
                    if self.data_list[k].get('repost_date') != date_start:
                        self.data_list[k]['repost_date'] = date_start
                        # Cập nhật cả vào self.data map
                        vid_id = self.data_list[k]['id']
                        if vid_id in self.data:
                            self.data[vid_id]['repost_date'] = date_start
                        updates_count += 1

        if updates_count > 0:
            self.refresh_tree()
            self.save_json_file()
            messagebox.showinfo("Hoàn tất", f"Đã tự động điền ngày cho {updates_count} video!")
        else:
            messagebox.showinfo("Thông báo", "Không tìm thấy khoảng trống nào giữa 2 video cùng ngày để điền.")

    def export_html(self):
        if not self.username: return
        html = generate_static_html(self.username, self.data_list)
        
        html_file = f"view_{self.username}.html"
        js_file = f"data_{self.username}.js"
        
        # Save JS
        js_content = f"window.tiktokData = {json.dumps(self.data_list, ensure_ascii=False)};"
        with open(js_file, "w", encoding="utf-8") as f:
            f.write(js_content)
            
        # Save HTML
        with open(html_file, "w", encoding="utf-8") as f:
            f.write(html)
            
        messagebox.showinfo("Xuất HTML", f"Đã tạo xong:\n- {html_file}\n- {js_file}")
        webbrowser.open(html_file)

    def save_json_file(self):
        if not self.current_file: return
        try:
            # --- FIX: LUÔN LƯU DẠNG LIST (JSON MỚI) ---
            # Chuyển từ Dict map {ID: Obj} sang List [Obj, Obj] trước khi lưu
            save_data = list(self.data.values())
            
            with open(self.current_file, "w", encoding="utf-8") as f:
                json.dump(save_data, f, ensure_ascii=False, indent=2)
                
            # print("Saved as LIST format.") # Debug
        except Exception as e:
            messagebox.showerror("Save Error", str(e))

if __name__ == "__main__":
    root = tk.Tk()
    app = TikTokManagerApp(root)
    root.mainloop()