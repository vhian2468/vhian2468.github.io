import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import json
import os
import time
import webbrowser
from datetime import datetime
import tempfile
import shutil
import re
import html as html_lib
from urllib.parse import quote
import argparse
import glob
import hashlib
import calendar


def enrich_music_from_logs(data, log_paths):
    mapping = {str(v['id']): v for v in data}
    added = 0
    for path in log_paths:
        if not os.path.isfile(path):
            continue
        with open(path, encoding='utf-8-sig') as file:
            for line in file:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                payload = record.get('data', record) if isinstance(record, dict) else record
                items = payload.get('itemList', payload.get('data', [])) if isinstance(payload, dict) else payload
                if not isinstance(items, list):
                    continue
                for raw in items:
                    if not isinstance(raw, dict):
                        continue
                    current = mapping.get(str(raw.get('id', raw.get('aweme_id', ''))))
                    music = raw.get('music') or {}
                    if current is None or not isinstance(music, dict):
                        continue
                    music_id = str(music.get('id', music.get('mid', '')))
                    if music_id.isdigit() and not current.get('music_id'):
                        current['music_id'] = music_id
                        current['music_title'] = music.get('title') or current.get('music_title', '')
                        added += 1
    return added


def default_api_logs():
    folder = r'D:\tiktok\tiktok-repost\V4-2026'
    # Oldest first; never changes dates, order, captions or membership of database.
    return sorted(glob.glob(os.path.join(folder, 'api_log.jsonl.*')), reverse=True) + sorted(glob.glob(os.path.join(folder, 'api_log*.jsonl')))


def extract_reposts(text):
    """Accept offline DB, API response, JSONL capture, HAR and pasted HTTP bodies."""
    decoder = json.JSONDecoder()
    roots = []
    try:
        roots.append(json.loads(text))
    except ValueError:
        # HTTP headers/cURL text may surround a JSON response. Decode complete bodies.
        position = 0
        while position < len(text):
            match = re.compile(r'[\[{]').search(text, position)
            if not match:
                break
            position = match.start()
            try:
                value, consumed = decoder.raw_decode(text, position)
                roots.append(value)
                position = consumed
            except ValueError:
                position += 1
    result = {}

    def visit(value):
        if isinstance(value, str):
            try:
                visit(json.loads(value))
            except (ValueError, TypeError):
                pass
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            identifier = value.get('id', value.get('aweme_id'))
            if identifier and any(key in value for key in ('desc', 'video', 'uniqueId', 'author', 'repost_caption', 'aweme_desc')):
                item = normalize_repost(value)
                result[item['id']] = item
            else:
                for child in value.values():
                    visit(child)
    for root in roots:
        visit(root)
    if not result:
        raise ValueError('Không tìm thấy video. Request URL/header không chứa dữ liệu video; hãy dán response body JSON hoặc file API/offline.')
    return list(result.values())


def normalize_repost(item):
    result = dict(item)
    result['id'] = str(item.get('id', item.get('aweme_id', '')))
    if not result['id'].isdigit():
        raise ValueError('ID video phải là chuỗi số TikTok')
    if 'author' in item or 'video' in item:
        author = item.get('author') or {}
        video = item.get('video') or {}
        stats = item.get('stats') or item.get('statistics') or {}
        music = item.get('music') or {}
        result.update({
            'desc': item.get('desc', item.get('aweme_desc', '')),
            'uniqueId': author.get('uniqueId', author.get('unique_id', 'unknown')),
            'nickname': author.get('nickname', ''),
            'cover': video.get('cover', ''),
            'duration': video.get('duration', 0),
            'music_title': music.get('title', ''),
            'music_id': str(music.get('id', music.get('mid', ''))),
            'digg': stats.get('diggCount', stats.get('digg_count', 0)),
            'comment': stats.get('commentCount', stats.get('comment_count', 0)),
            'share': stats.get('shareCount', stats.get('share_count', 0)),
            'play': stats.get('playCount', stats.get('play_count', 0)),
        })
        if isinstance(result['cover'], dict):
            urls = result['cover'].get('url_list', [])
            result['cover'] = urls[0] if urls else ''
        # Store the original API object for fields not mapped by this version.
        result['api_raw'] = item
    timestamp = item.get('createTime', item.get('create_time'))
    try:
        result.setdefault('date', datetime.fromtimestamp(int(timestamp or (int(result['id']) >> 32))).strftime('%Y-%m-%d'))
    except (ValueError, OSError, OverflowError):
        result.setdefault('date', '')
    for key in ('desc', 'uniqueId', 'nickname', 'cover', 'music_title', 'music_id', 'repost_date', 'repost_caption'):
        result.setdefault(key, '')
    for key in ('digg', 'comment', 'share', 'play', 'duration', 'repost_order'):
        result.setdefault(key, 0)
    tags = item.get('textExtra', item.get('text_extra', [])) or []
    result.setdefault('hashtags', [x.get('hashtagName', x.get('hashtag_name', '')) for x in tags if isinstance(x, dict) and (x.get('hashtagName') or x.get('hashtag_name'))])
    return result


def atomic_write(path, content, backup=False):
    folder = os.path.dirname(os.path.abspath(path))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=folder, delete=False, suffix='.tmp') as file:
            temporary = file.name
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if backup and os.path.isfile(path):
            shutil.copy2(path, path + '.manager.bak')
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)

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
    <body class="locked">
    <style>
        body.locked #loadingMsg { display:none; }
        .music { display:block; margin-top:8px; font-size:.85em; color:#b7a6ff; text-decoration:none; }
        .music:hover { text-decoration:underline; }
    </style>
    
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
                    <input type="text" id="searchInput" placeholder="🔍 Username, nickname, ID, caption, hashtag, music..." style="width: 100%;">
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
                    <button id="captionFilter" aria-pressed="false" onclick="toggleCaptionFilter()">💬 Có caption</button>
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
        let captionOnly = false;
        let unlocked = false;
        let unlocking = false;

        function unlockApp() {
            if (unlocking || unlocked || jumpOrderInput.value !== '2211') return;
            unlocking = true;
            const script = document.createElement('script');
            script.src = 'data___USERNAME__.js?t=__TIMESTAMP__';
            script.onload = () => {
                unlocking = false;
                if (!Array.isArray(window.tiktokData)) { alert('File dữ liệu không hợp lệ'); return; }
                fullData = window.tiktokData;
                unlocked = true;
                jumpOrderInput.value = '';
                document.body.classList.remove('locked');
                startApp();
            };
            script.onerror = () => { unlocking = false; script.remove(); alert('Không tải được file dữ liệu JS'); };
            document.head.appendChild(script);
        }
        jumpOrderInput.addEventListener('input', () => { if (!unlocked) unlockApp(); });
        jumpOrderInput.addEventListener('keydown', e => { if (e.key === 'Enter') jumpToOrder(); });

        function toggleCaptionFilter() {
            if (!unlocked) return;
            captionOnly = !captionOnly;
            const button = document.getElementById('captionFilter');
            button.setAttribute('aria-pressed', String(captionOnly));
            button.classList.toggle('btn-primary', captionOnly);
            handleFilter();
        }

        function escapeHtml(value) {
            return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
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
            el.className = 'card' + (String(v.repost_caption || '').trim() ? ' has-repost-caption' : '');
            el.id = 'card-' + v.id;
            const videoUrl = 'https://www.tiktok.com/@' + encodeURIComponent(v.uniqueId || 'unknown') + '/video/' + encodeURIComponent(v.id);
            const musicId = String(v.music_id || v.music?.id || '');
            const musicTitle = v.music_title || v.music?.title || '';
            const musicUrl = /^\d+$/.test(musicId) ? 'https://www.tiktok.com/music/' + encodeURIComponent(musicTitle || 'original-sound') + '-' + musicId : '';
            const musicHtml = musicTitle ? (musicUrl
                ? `<a class="music" href="${escapeHtml(musicUrl)}" target="_blank" rel="noopener noreferrer">🎵 ${escapeHtml(musicTitle)}</a>`
                : `<span class="music" title="Chưa có music ID">🎵 ${escapeHtml(musicTitle)}</span>`) : '';
            el.innerHTML = `
                <a href="${escapeHtml(videoUrl)}" target="_blank" rel="noopener noreferrer" class="thumb-link">
                    <span class="cursor-badge">#${escapeHtml(v.feed_index)}</span>
                    ${v.repost_order ? `<span class="order-badge">Ord: ${escapeHtml(v.repost_order)}</span>` : ''}
                    <img class="thumb" loading="lazy" alt="Thumbnail">
                </a>
                <div class="info">
                    ${v.repost_date ? `<div class="date-repost">♻️ ${escapeHtml(v.repost_date)}</div>` : ''}
                    ${v.repost_caption ? `<div class="caption-repost">💬 ${escapeHtml(v.repost_caption)}</div>` : ''}
                    <div class="date-normal">📅 Tạo: ${escapeHtml(v.date)}</div>
                    <span class="nickname">${escapeHtml(v.nickname)}</span>
                    <span class="uid">@${escapeHtml(v.uniqueId)}</span>
                    <div class="desc" title="${escapeHtml(v.desc)}">${escapeHtml(v.desc)}</div>
                    <div class="metrics"><span>❤️ ${escapeHtml(v.digg || 0)}</span><span>💬 ${escapeHtml(v.comment || 0)}</span><span>🔄 ${escapeHtml(v.share || 0)}</span><span>▶ ${escapeHtml(v.play || 0)}</span></div>
                    ${musicHtml}
                </div>`;
            const img = el.querySelector('img');
            const safeImage = url => /^(https?:\/\/|[^:]+$)/i.test(String(url || '')) ? String(url || '') : '';
            const online = safeImage(v.cover);
            let triedOnline = !v.cover_off;
            img.onload = () => img.classList.add('loaded');
            img.onerror = () => {
                if (!triedOnline && online) { triedOnline = true; img.src = online; }
                else { img.onerror = null; img.classList.add('error'); }
            };
            img.src = safeImage(v.cover_off) || online;
            return el;
        }

        function legacyCreateCard(v) {
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
            if (!unlocked) { unlockApp(); return; }
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
                if (/^\d{4}-\d{2}-\d{2}$/.test(v.repost_date || '')) {
                    if (!repostMap[v.repost_date]) repostMap[v.repost_date] = [];
                    repostMap[v.repost_date].push(v);
                }
                if (/^\d{4}-\d{2}-\d{2}$/.test(v.date || '')) {
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
                if (searchInput.value !== '' || captionOnly) {
                    searchInput.value = '';
                    captionOnly = false;
                    document.getElementById('captionFilter').setAttribute('aria-pressed', 'false');
                    document.getElementById('captionFilter').classList.remove('btn-primary');
                    handleFilter(); 
                }
                jumpToId(targetVid.id);
            }
        };

        function handleFilter() {
            if (!unlocked) return;
            const term = searchInput.value.toLowerCase();
            const sortMode = sortSelect.value;
            resetRender();
            loadingMsg.style.display = 'block';
            
            displayData = fullData.filter(v => (!captionOnly || String(v.repost_caption || '').trim()) && (
                (v.nickname && v.nickname.toLowerCase().includes(term)) || 
                (v.date && v.date.includes(term)) ||
                (v.repost_date && v.repost_date.includes(term)) ||
                (v.repost_caption && v.repost_caption.toLowerCase().includes(term)) ||
                (v.desc && v.desc.toLowerCase().includes(term)) ||
                (v.uniqueId && v.uniqueId.toLowerCase().includes(term)) ||
                String(v.id || '').includes(term) ||
                String(v.music_title || '').toLowerCase().includes(term) ||
                String(v.hashtags || '').toLowerCase().includes(term)
            ));
            document.getElementById('totalCount').innerText = displayData.length + '/' + fullData.length;

            if (sortMode === 'feed') {
                displayData.sort((a, b) => (a.feed_index ?? 9e9) - (b.feed_index ?? 9e9));
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
    html_content = html_template.replace("__USERNAME__", html_lib.escape(username, quote=True))
    html_content = html_content.replace("__TIMESTAMP__", str(int(time.time())))
    return html_content

# ================= GUI APP =================
class TikTokManagerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("TikTok HTML Generator & Data Manager")
        self.root.geometry("1400x780")
        
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
        tk.Button(self.top_frame, text="➕ Thêm / nhập API", command=self.import_reposts).pack(side="left", padx=5)
        tk.Button(self.top_frame, text="🗑 Xóa đã chọn", command=self.delete_reposts).pack(side="left", padx=5)
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
        tk.Label(self.left_frame, text="🔍 Username / nickname, ID, mô tả, caption, hashtag, ngày, music:").pack(anchor="w", padx=5)
        tk.Entry(self.left_frame, textvariable=self.search_var).pack(fill="x", padx=5, pady=5)
        
        # Treeview (List)
        self.tree = ttk.Treeview(self.left_frame, columns=("feed_index", "date", "nickname", "repost_date", "order", "caption"), show="headings", selectmode="extended")
        self.tree.heading("feed_index", text="#")
        self.tree.heading("date", text="Ngày tạo")
        self.tree.heading("nickname", text="Username / Nickname")
        self.tree.heading("caption", text="Repost Caption")
        self.tree.heading("repost_date", text="Ngày Repost")
        self.tree.heading("order", text="Ord")
        
        self.tree.column("feed_index", width=50)
        self.tree.column("date", width=90)
        self.tree.column("nickname", width=220)
        self.tree.column("caption", width=240)
        self.tree.column("repost_date", width=90)
        self.tree.column("order", width=50)
        
        self.tree.pack(fill="both", expand=True)
        horizontal = ttk.Scrollbar(self.left_frame, orient='horizontal', command=self.tree.xview)
        horizontal.pack(fill='x')
        self.tree.configure(xscrollcommand=horizontal.set)
        self.tree.bind("<<TreeviewSelect>>", self.on_select_item)
        
        # --- RIGHT: EDITOR ---
        right_shell = tk.Frame(self.paned)
        self.paned.add(right_shell, width=430)
        editor_canvas = tk.Canvas(right_shell, bg='#f9f9f9', highlightthickness=0)
        editor_scroll = ttk.Scrollbar(right_shell, orient='vertical', command=editor_canvas.yview)
        editor_scroll.pack(side='right', fill='y')
        editor_canvas.pack(side='left', fill='both', expand=True)
        editor_canvas.configure(yscrollcommand=editor_scroll.set)
        self.right_frame = tk.Frame(editor_canvas, bg='#f9f9f9')
        editor_window = editor_canvas.create_window((0, 0), window=self.right_frame, anchor='nw')
        self.right_frame.bind('<Configure>', lambda event: editor_canvas.configure(scrollregion=editor_canvas.bbox('all')))
        editor_canvas.bind('<Configure>', lambda event: editor_canvas.itemconfigure(editor_window, width=event.width))
        
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
        tk.Button(self.form_frame, text='📅 Chọn ngày từ lịch', command=self.show_date_calendar).pack(anchor='e', pady=3)
        
        tk.Label(self.form_frame, text="Repost Caption:", bg="#f9f9f9").pack(anchor="w", pady=(10, 0))
        self.entry_repost_cap = tk.Entry(self.form_frame)
        self.entry_repost_cap.pack(fill="x", pady=2)

        self.extra_entries = {}
        for key, label in (('uniqueId', 'Username'), ('nickname', 'Nickname'), ('desc', 'Mô tả / hashtag'), ('music_title', 'Tên nhạc'), ('music_id', 'Music ID (để mở link nhạc)')):
            tk.Label(self.form_frame, text=label + ':', bg='#f9f9f9').pack(anchor='w')
            entry = tk.Entry(self.form_frame)
            entry.pack(fill='x', pady=2)
            self.extra_entries[key] = entry
        
        tk.Label(self.form_frame, text="Repost Order (Tự động):", bg="#f9f9f9").pack(anchor="w", pady=(10, 0))
        self.entry_repost_order = tk.Entry(self.form_frame, state="readonly")
        self.entry_repost_order.pack(fill="x", pady=2)

        tk.Button(self.form_frame, text="💾 LƯU THAY ĐỔI", command=self.save_current_item, bg="#2196F3", fg="white", font=("Arial", 10, "bold"), pady=10).pack(fill="x", pady=20)
        
        self.btn_open_web = tk.Button(self.form_frame, text="🌐 Mở trên Web", command=self.open_in_browser)
        self.btn_open_web.pack(fill="x")

        self.selected_vid = None

    def import_reposts(self):
        if not self.current_file:
            messagebox.showinfo('Chọn database', 'Hãy chọn file JSON database trước khi thêm repost.')
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('Thêm repost: API / JSON offline / thủ công')
        dialog.geometry('850x650')
        tk.Label(dialog, text='Dán response JSON, request có body JSON, JSONL/HAR hoặc chọn file. Request URL đơn thuần không chứa video.').pack(anchor='w', padx=10, pady=8)
        text = tk.Text(dialog, wrap='word')
        text.pack(fill='both', expand=True, padx=10)
        options = tk.Frame(dialog)
        options.pack(fill='x', padx=10, pady=8)
        tk.Label(options, text='Chèn mới tại Feed index:').pack(side='left')
        position_entry = tk.Entry(options, width=8)
        position_entry.insert(0, '0')
        position_entry.pack(side='left')
        update_existing = tk.BooleanVar(value=False)
        tk.Checkbutton(options, text='Cập nhật thông tin API cho ID đã tồn tại', variable=update_existing).pack(side='left', padx=10)

        def choose_file():
            path = filedialog.askopenfilename(filetypes=[('API / JSON / HAR', '*.json *.jsonl *.har *.txt'), ('Tất cả', '*.*')])
            if path:
                try:
                    with open(path, encoding='utf-8-sig') as file:
                        content = file.read()
                    text.delete('1.0', tk.END)
                    text.insert('1.0', content)
                except Exception as error:
                    messagebox.showerror('Không đọc được file', str(error))

        def manual_template():
            text.delete('1.0', tk.END)
            text.insert('1.0', json.dumps({'id': '', 'uniqueId': '', 'nickname': '', 'desc': '', 'cover': '', 'repost_date': '', 'repost_caption': '', 'music_title': '', 'music_id': ''}, ensure_ascii=False, indent=2))

        def apply_import():
            try:
                incoming = extract_reposts(text.get('1.0', tk.END))
                position = int(position_entry.get())
                if not 0 <= position <= len(self.data_list):
                    raise ValueError('Feed index chèn phải nằm trong database hiện tại')
                old_list = self.data_list
                new_list = [dict(v) for v in old_list]
                mapping = {v['id']: v for v in new_list}
                added, updated = [], 0
                protected = ('repost_date', 'repost_caption', 'repost_order', 'cover_off', 'feed_index')
                for item in incoming:
                    if item['id'] not in mapping:
                        added.append(item)
                        mapping[item['id']] = item
                    elif update_existing.get():
                        current = mapping[item['id']]
                        preserved = {key: current[key] for key in protected if key in current}
                        current.update(item)
                        current.update(preserved)
                        updated += 1
                new_list[position:position] = added
                for index, item in enumerate(new_list):
                    item['feed_index'] = index
                self.data_list = new_list
                self.data = {v['id']: v for v in new_list}
                if not self.save_json_file():
                    self.data_list = old_list
                    self.data = {v['id']: v for v in old_list}
                    return
                excluded_path = self.current_file + '.deleted_ids.json'
                if os.path.isfile(excluded_path):
                    with open(excluded_path, encoding='utf-8') as file:
                        excluded = set(json.load(file))
                    excluded.difference_update(v['id'] for v in incoming)
                    atomic_write(excluded_path, json.dumps(sorted(excluded)))
                self.refresh_tree()
                self.lbl_file.config(text=f'User: {self.username} | {len(new_list)} videos')
                messagebox.showinfo('Đã nhập', f'Thêm {len(added)}; cập nhật {updated}; bỏ qua {len(incoming)-len(added)-updated} ID cũ.\nRepost Order cũ được giữ; dùng Tạo Repost Order nếu cần tính lại.')
                dialog.destroy()
            except Exception as error:
                messagebox.showerror('Không nhập được', str(error))

        buttons = tk.Frame(dialog)
        buttons.pack(fill='x', padx=10, pady=10)
        tk.Button(buttons, text='📂 Chọn file', command=choose_file).pack(side='left')
        tk.Button(buttons, text='Mẫu nhập thủ công', command=manual_template).pack(side='left', padx=8)
        tk.Button(buttons, text='Thêm vào database', command=apply_import, bg='#ddffdd').pack(side='right')

    def delete_reposts(self):
        selections = self.tree.selection()
        if not selections:
            return
        ids = {self.tree.item(row, 'tags')[0] for row in selections}
        if not messagebox.askyesno('Xóa khỏi database', f'Xóa {len(ids)} repost khỏi database local?\nKhông tác động tài khoản TikTok. Bản trước khi xóa được lưu ở .manager.bak.'):
            return
        old_list = self.data_list
        excluded_path = self.current_file + '.deleted_ids.json'
        try:
            excluded = set()
            if os.path.isfile(excluded_path):
                with open(excluded_path, encoding='utf-8') as file:
                    excluded = set(json.load(file))
            atomic_write(excluded_path, json.dumps(sorted(excluded | ids)), backup=True)
        except Exception as error:
            messagebox.showerror('Không lưu được danh sách xóa', str(error))
            return
        self.data_list = [dict(v) for v in old_list if v['id'] not in ids]
        for index, item in enumerate(self.data_list):
            item['feed_index'] = index
        self.data = {v['id']: v for v in self.data_list}
        if not self.save_json_file():
            self.data_list = old_list
            self.data = {v['id']: v for v in old_list}
            atomic_write(excluded_path, json.dumps(sorted(excluded)))
            return
        self.selected_vid = None
        self.refresh_tree()
        self.lbl_file.config(text=f'User: {self.username} | {len(self.data_list)} videos')

    def load_file(self):
        filename = filedialog.askopenfilename(filetypes=[("JSON Files", "*.json")])
        if not filename: return
        self.load_path(filename)

    def load_path(self, filename, notify=True):
        
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
            self.data_list = [normalize_repost(v) for v in self.data_list]
            self.data = {v['id']: v for v in self.data_list}
            if len(self.data) != len(self.data_list):
                raise ValueError('Database có ID trùng; hãy sửa bản sao trước khi mở')
            
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
            self.selected_vid = None
            
            self.refresh_tree()
            if notify:
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
            for key in ('uniqueId', 'date', 'repost_caption', 'hashtags', 'music_title', 'music_id', 'repost_order', 'feed_index'):
                if term in str(v.get(key, '')).lower(): match = True
            
            if not term or match:
                self.filtered_list.append(v)
                
        # Insert to tree
        for v in self.filtered_list:
            self.tree.insert("", "end", values=(
                v.get('feed_index', ''),
                v.get('date', ''),
                '@' + v.get('uniqueId', '') + ' / ' + v.get('nickname', ''),
                v.get('repost_date', ''),
                v.get('repost_order', ''),
                v.get('repost_caption', '')
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
        for key, entry in self.extra_entries.items():
            entry.delete(0, tk.END)
            entry.insert(0, v.get(key, ''))
        
        self.entry_repost_order.config(state="normal")
        self.entry_repost_order.delete(0, tk.END)
        self.entry_repost_order.insert(0, v.get('repost_order', ''))
        self.entry_repost_order.config(state="readonly")

    def set_today(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self.entry_repost_date.delete(0, tk.END)
        self.entry_repost_date.insert(0, today)

    def show_date_calendar(self):
        try:
            chosen = datetime.strptime(self.entry_repost_date.get().strip(), '%Y-%m-%d')
        except ValueError:
            chosen = datetime.now()
        popup = tk.Toplevel(self.root)
        popup.title('Chọn ngày Repost')
        popup.transient(self.root)
        popup.resizable(False, False)
        year, month = tk.IntVar(value=chosen.year), tk.IntVar(value=chosen.month)
        navigation = tk.Frame(popup)
        navigation.pack(padx=10, pady=10)
        days = tk.Frame(popup)
        days.pack(padx=10, pady=(0, 10))

        def select_day(day):
            value = f'{year.get():04d}-{month.get():02d}-{day:02d}'
            self.entry_repost_date.delete(0, tk.END)
            self.entry_repost_date.insert(0, value)
            popup.destroy()

        def draw():
            try:
                y, m = year.get(), month.get()
                if not 1900 <= y <= 2100 or not 1 <= m <= 12:
                    return
            except (ValueError, tk.TclError):
                return
            for widget in days.winfo_children():
                widget.destroy()
            for column, label in enumerate(('T2', 'T3', 'T4', 'T5', 'T6', 'T7', 'CN')):
                tk.Label(days, text=label, width=4).grid(row=0, column=column)
            for row, week in enumerate(calendar.monthcalendar(y, m), start=1):
                for column, day in enumerate(week):
                    if day:
                        color = '#cce8ff' if (y, m, day) == (chosen.year, chosen.month, chosen.day) else '#f0f0f0'
                        tk.Button(days, text=str(day), width=4, bg=color, command=lambda d=day: select_day(d)).grid(row=row, column=column, padx=1, pady=1)

        def move(delta):
            try:
                total = year.get() * 12 + month.get() - 1 + delta
                y, m = divmod(total, 12)
                if 1900 <= y <= 2100:
                    year.set(y)
                    month.set(m + 1)
                    draw()
            except (ValueError, tk.TclError):
                pass

        tk.Button(navigation, text='◀', command=lambda: move(-1)).pack(side='left')
        tk.Spinbox(navigation, from_=1, to=12, width=3, textvariable=month, command=draw).pack(side='left', padx=6)
        tk.Spinbox(navigation, from_=1900, to=2100, width=5, textvariable=year, command=draw).pack(side='left')
        tk.Button(navigation, text='Xem', command=draw).pack(side='left', padx=6)
        tk.Button(navigation, text='▶', command=lambda: move(1)).pack(side='left')
        draw()
        popup.grab_set()

    def save_current_item(self):
        if not self.selected_vid or self.selected_vid not in self.data: return
        
        v = self.data[self.selected_vid]
        v['repost_date'] = self.entry_repost_date.get().strip()
        v['repost_caption'] = self.entry_repost_cap.get().strip()
        for key, entry in self.extra_entries.items():
            v[key] = entry.get().strip()
        
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
        
        output_dir = os.path.dirname(os.path.abspath(self.current_file))
        html_file = os.path.join(output_dir, f"view_{self.username}.html")
        js_file = os.path.join(output_dir, f"data_{self.username}.js")
        
        # Save JS
        enrich_music_from_logs(self.data_list, default_api_logs())
        export_data = [{key: value for key, value in v.items() if key != 'api_raw'} for v in self.data_list]
        js_content = f"window.tiktokData = {json.dumps(export_data, ensure_ascii=False)};"
        atomic_write(js_file, js_content)
            
        # Save HTML
        atomic_write(html_file, html)
            
        messagebox.showinfo("Xuất HTML", f"Đã tạo xong:\n- {html_file}\n- {js_file}")
        webbrowser.open(html_file)

    def save_json_file(self):
        if not self.current_file: return False
        try:
            # --- FIX: LUÔN LƯU DẠNG LIST (JSON MỚI) ---
            # Chuyển từ Dict map {ID: Obj} sang List [Obj, Obj] trước khi lưu
            save_data = self.data_list
            ids = [v['id'] for v in save_data]
            if len(ids) != len(set(ids)):
                raise ValueError('Database có ID trùng; không ghi file')
            atomic_write(self.current_file, json.dumps(save_data, ensure_ascii=False, indent=2), backup=True)
            with open(self.current_file, 'rb') as file:
                digest = hashlib.sha256(file.read()).hexdigest()
            atomic_write(self.current_file + '.manager.state.json', json.dumps({'sha256': digest, 'edited_at': datetime.now().isoformat()}))
            return True
                
            # print("Saved as LIST format.") # Debug
        except Exception as e:
            messagebox.showerror("Save Error", str(e))
            return False

def export_automatically(input_path, output_dir=None, api_logs=None):
    with open(input_path, encoding='utf-8-sig') as file:
        raw = json.load(file)
    data = [normalize_repost(v) for v in (raw.values() if isinstance(raw, dict) else raw)]
    ids = [v['id'] for v in data]
    if len(set(ids)) != len(ids):
        raise ValueError('Database có ID trùng; không xuất HTML')
    base = os.path.basename(input_path)
    username = base[5:-5] if base.startswith('data_') and base.endswith('.json') else os.path.splitext(base)[0]
    output_dir = output_dir or os.path.dirname(os.path.abspath(input_path))
    os.makedirs(output_dir, exist_ok=True)
    added = enrich_music_from_logs(data, default_api_logs() if api_logs is None else api_logs)
    export_data = [{key: value for key, value in v.items() if key != 'api_raw'} for v in data]
    js_path = os.path.join(output_dir, f'data_{username}.js')
    html_path = os.path.join(output_dir, f'view_{username}.html')
    atomic_write(js_path, 'window.tiktokData = ' + json.dumps(export_data, ensure_ascii=False) + ';')
    atomic_write(html_path, generate_static_html(username, data))
    print(f'EXPORTED: {len(data)} videos; bổ sung {added} music IDs từ API log; có link nhạc: {sum(bool(v.get("music_id")) for v in data)}')
    print(f'HTML: {html_path}')
    return html_path, js_path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='TikTok manager và HTML exporter')
    parser.add_argument('--auto', action='store_true', help='Xuất xong tự thoát, không mở GUI/browser')
    parser.add_argument('--input', default='data_pe.siro_phan_all.json')
    parser.add_argument('--output-dir')
    args = parser.parse_args()
    if args.auto:
        export_automatically(args.input, args.output_dir)
    else:
        root = tk.Tk()
        app = TikTokManagerApp(root)
        if os.path.isfile(args.input):
            app.load_path(args.input, notify=False)
        root.mainloop()
