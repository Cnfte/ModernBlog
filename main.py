import os
import json
import shutil
import subprocess
import stat
import uuid
import time
import sys
import threading
import queue
import re
from datetime import datetime
from functools import wraps
from urllib.parse import unquote

try:
    from flask import (Flask, render_template_string, request, jsonify,
                       redirect, url_for, send_from_directory, Response,
                       stream_with_context)
except ImportError:
    print("Flask 未安装，请运行: pip install flask --break-system-packages")
    sys.exit(1)

try:
    import frontmatter
except ImportError:
    print("frontmatter 未安装，请运行: pip install python-frontmatter")
    sys.exit(1)

try:
    import builder as _builder_mod
except ImportError:
    _builder_mod = None

try:
    import backup_manager as _bm
except ImportError:
    _bm = None

try:
    import audio_manager as _am
except ImportError:
    _am = None

# ─────────────────────────── App ───────────────────────────
app = Flask(__name__)
app.secret_key = os.urandom(24)

# ── 安全响应头 ──
@app.after_request
def _add_security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['X-XSS-Protection'] = '1; mode=block'
    resp.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    # CSP：允许本站脚本/样式 + 内联（后台面板重度依赖内联脚本）
    resp.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    return resp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'content', 'config.json')

# SSE 日志队列（多客户端广播）
_log_queues: list[queue.Queue] = []
_log_lock = threading.Lock()

# 构建/部署互斥锁：防止重复点击或并发请求导致 git/public 目录竞态
# （例如一次部署还在 rm -rf .git 时，另一次部署已经开始操作同一目录，
#  会导致 git 进程因 index.lock 冲突等原因瞬间返回非零但不产生任何输出）
_task_lock = threading.Lock()

DEFAULT_CONFIG = {
    "site_name": "SITE_NAME",
    "site_url": "https://example.com",
    "logo_text": "LOGO_TEXT",
    "hero_title": "WELCOME TO BACK",
    "hero_subtitle": "HERO_SUBTITLE",
    "site_keywords": "SEO_KEYWORDS",
    "site_description": "",
    "enable_indexnow": True,
    "indexnow_key": "",
    "start_date": "2024-01-01",
    "bg_url": "https://example.com/example.jpg",
    "post_bg_urls": "",
    "random_img_api": "https://www.dmoe.cc/random.php",
    "og_image": "",
    "posts_per_page": "9",
    "theme_color": "#ff99cc",
    "enable_particles": True,
    "enable_icon_spin": True,
    "enable_tag_bounce": True,
    "enable_hero_typing": False,
    "footer_custom": "FOOTER_CUSTOM",
    "footer_text": "",
    "site_notice": "",
    "show_notice_widget": False,
    "username": "USERNAME",
    "avatar_url": "",
    "bio": "Keep it simple. Keep it real.",
    "email": "",
    "github_url": "",
    "telegram_url": "",
    "bilibili_url": "",
    "twitter_url": "",
    "rss_url": "",
    "deploy_repo": "",
    "git_user_name": "",
    "git_user_email": "",
    "cf_zone_id": "",
    "cf_api_token": "",
    "cf_email": "",
    "monetag_tag_code": "",
    "anime_list": [],
    "friend_links": [],
    "contact_methods": [],
    "cloud_backup_url": "",
    "cloud_backup_key": "",
    "backup_retention": "10",
    "auto_backup_on_deploy": False,
    "enable_player": False,
    "player_default_mode": "order",  # order | random | single
}


# ─────────────────────────── 工具函数 ───────────────────────────
def _init_env():
    for d in ["content/posts/zh", "content/posts/en",
              "content/pages/zh", "content/pages/en", "content/attachments", "backups",
              "content/audio/files", "content/audio/lyrics"]:
        os.makedirs(os.path.join(BASE_DIR, d), exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, indent=4, ensure_ascii=False)


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    changed = False
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    if changed:
        _save_config(cfg)
    return cfg


def _save_config(cfg: dict):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)


def _ensure_indexnow_key(cfg: dict) -> str:
    """IndexNow 密钥懒生成：首次需要用到（开始构建 / 打开设置页）时自动补一个，
    写回 config.json，避免用户还要额外去手动生成再粘贴。"""
    key = str(cfg.get('indexnow_key', '') or '').strip()
    if not key:
        key = uuid.uuid4().hex
        cfg['indexnow_key'] = key
        _save_config(cfg)
    return key


def _get_list(cfg: dict, key: str) -> list:
    v = cfg.get(key, [])
    if isinstance(v, str):
        try:
            v = json.loads(v) if v.strip() else []
        except Exception:
            v = []
    return v if isinstance(v, list) else []


def _safe_path_component(name: str) -> str:
    """校验路径组件，防止路径穿越（../ 等）。
    只允许字母、数字、中文、下划线、连字符、点号，且不能包含 .. 或 / \\。"""
    if not name:
        raise ValueError('路径组件不能为空')
    if '..' in name or '/' in name or '\\' in name:
        raise ValueError(f'非法的路径组件: {name}')
    # 额外白名单：只允许安全字符
    if not re.match(r'^[\w\u4e00-\u9fff.\- ()]+$', name):
        raise ValueError(f'路径组件包含非法字符: {name}')
    return name


def _safe_filename_for_path(name: str) -> str:
    """对用作文件名的值做安全处理：取 basename，禁止路径穿越。"""
    name = os.path.basename(str(name or '').strip())
    if not name or '..' in name or '/' in name or '\\' in name:
        raise ValueError('非法的文件名')
    return name


def _broadcast_log(msg: str):
    ts = datetime.now().strftime('%H:%M:%S')
    line = f"[{ts}] {msg}"
    with _log_lock:
        for q in list(_log_queues):
            try:
                q.put_nowait(line)
            except queue.Full:
                pass


def _run_async(fn, *args, **kwargs):
    """在后台线程执行，输出广播到 SSE 日志。
    同一时间只允许一个构建/部署任务运行，避免 git/public 目录竞态。"""
    if not _task_lock.acquire(blocking=False):
        _broadcast_log("[WARN] 已有任务正在执行，请等待完成后再试")
        return
    def _wrap():
        try:
            fn(*args, **kwargs)
        except Exception as e:
            _broadcast_log(f"[ERR] 异常: {type(e).__name__}: {e}")
        finally:
            _task_lock.release()
    t = threading.Thread(target=_wrap, daemon=True)
    t.start()


# ─────────────────────────── HTML 模板 ───────────────────────────
# 单文件内嵌，无需 templates/ 目录
_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MODERN_BLOG // CONTROL PANEL</title>
<style>
:root{
  --bg:#090a10;--panel:#0f111a;--border:rgba(255,255,255,0.08);--border-focus:#38bdf8;
  --accent:#38bdf8;--accent-hover:#0284c7;--accent2:#f59e0b;--accent3:#818cf8;
  --green:#10b981;--red:#f43f5e;
  --text:#f1f5f9;--dim:#94a3b8;--input-bg:#0b0c14;
  --r:6px;--r-md:8px;
  --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans SC","PingFang SC",sans-serif;
  --mono:'JetBrains Mono','Fira Code',Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--font);background:var(--bg);color:var(--text);
  display:flex;flex-direction:column;height:100vh;font-size:13px;overflow:hidden}
a{color:var(--accent);text-decoration:none}

/* ── Top Bar ── */
.topbar{display:flex;align-items:center;gap:12px;padding:0 20px;
  height:48px;background:var(--panel);border-bottom:1px solid var(--border);flex-shrink:0}
.topbar .logo{font-family:var(--mono);font-size:.9rem;font-weight:700;
  color:var(--text);display:inline-flex;align-items:center;gap:8px;letter-spacing:.5px}
.topbar .logo svg{color:var(--accent)}
.topbar .clock{margin-left:auto;color:#38bdf8;font-family:var(--mono);font-size:.78rem;
  padding:3px 8px;background:rgba(56,189,248,0.08);border:1px solid rgba(56,189,248,0.2);border-radius:var(--r)}

/* ── Tab bar ── */
.tabbar{display:flex;gap:4px;padding:0 16px;background:var(--panel);
  border-bottom:1px solid var(--border);flex-shrink:0;overflow-x:auto}
.tab{display:inline-flex;align-items:center;gap:6px;padding:9px 14px;font-size:.78rem;font-weight:600;
  letter-spacing:.3px;color:var(--dim);cursor:pointer;border-bottom:2px solid transparent;
  white-space:nowrap;transition:color .14s linear,border-color .14s linear}
.tab svg{width:14px;height:14px;stroke-width:1.8;flex-shrink:0;opacity:.75}
.tab:hover{color:var(--text)}
.tab:hover svg{opacity:1}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab.active svg{opacity:1;color:var(--accent)}

/* ── Layout ── */
.workspace{flex:1;overflow:hidden;display:flex}
.pane{flex:1;overflow-y:auto;padding:20px 24px}

/* ── Buttons ── */
.btn{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;
  border:1px solid var(--border);border-radius:var(--r);background:var(--input-bg);
  color:var(--dim);font-family:var(--font);font-size:.78rem;font-weight:600;
  cursor:pointer;transition:color .12s linear,border-color .12s linear,background-color .12s linear,transform .1s linear;
  white-space:nowrap;line-height:1.2}
.btn svg{width:13px;height:13px;stroke-width:1.8;flex-shrink:0}
.btn:hover{border-color:rgba(255,255,255,0.2);color:var(--text)}
.btn:active{transform:translateY(1px)}
.btn.primary{border-color:rgba(56,189,248,0.4);color:var(--accent);background:rgba(56,189,248,0.06)}
.btn.primary:hover{border-color:var(--accent);background:var(--accent);color:#090a10}
.btn.green{border-color:rgba(16,185,129,0.4);color:var(--green);background:rgba(16,185,129,0.06)}
.btn.green:hover{border-color:var(--green);background:var(--green);color:#090a10}
.btn.red{border-color:rgba(244,63,94,0.4);color:var(--red);background:rgba(244,63,94,0.06)}
.btn.red:hover{border-color:var(--red);background:var(--red);color:#fff}
.btn.orange{border-color:rgba(245,158,11,0.4);color:var(--accent2);background:rgba(245,158,11,0.06)}
.btn.orange:hover{border-color:var(--accent2);background:var(--accent2);color:#090a10}
.btn.blue{border-color:rgba(129,140,248,0.4);color:var(--accent3);background:rgba(129,140,248,0.06)}
.btn.blue:hover{border-color:var(--accent3);background:var(--accent3);color:#090a10}
.btn:disabled{opacity:.35;cursor:not-allowed;transform:none}
.btn-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center}

/* ── Inputs ── */
input,textarea,select{width:100%;padding:7px 11px;
  background:var(--input-bg);border:1px solid var(--border);border-radius:var(--r);
  color:var(--text);font-family:var(--font);font-size:.82rem;outline:none;
  transition:border-color .12s linear,box-shadow .12s linear}
input:focus,textarea:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(56,189,248,0.15)}
textarea{resize:vertical;min-height:80px;line-height:1.6;font-family:var(--mono)}
label.field-label{display:block;color:var(--dim);font-size:.74rem;font-weight:500;
  margin-bottom:5px;margin-top:12px;letter-spacing:.3px}
.field-row{display:flex;gap:12px;align-items:flex-end}
.field-row>*{flex:1}
.char-counter{font-size:.68rem;margin-top:4px;font-family:var(--mono)}
.char-counter.cc-ok{color:var(--green)}
.char-counter.cc-bad{color:var(--red)}
.char-counter.cc-warn{color:var(--accent2)}
.char-counter.cc-dim{color:var(--dim)}

/* ── Sections ── */
.section-title{font-size:.72rem;color:var(--accent);font-weight:700;font-family:var(--mono);
  letter-spacing:1px;margin:22px 0 10px;padding-bottom:6px;text-transform:uppercase;
  border-bottom:1px solid var(--border);display:flex;align-items:center;gap:6px}
.card{background:var(--panel);border:1px solid var(--border);
  border-radius:var(--r-md);padding:16px 18px;margin-bottom:14px}

/* ── Split layout for content tab ── */
.split{display:flex;gap:0;height:100%;overflow:hidden}
.split-left{width:260px;flex-shrink:0;border-right:1px solid var(--border);
  padding:12px 0;overflow-y:auto;display:flex;flex-direction:column;background:rgba(11,12,20,0.5)}
.split-left .sl-toolbar{padding:8px 12px;display:flex;gap:6px;flex-wrap:wrap}
.split-right{flex:1;padding:16px 22px;overflow-y:auto;min-width:0}
.file-item{padding:9px 14px;font-size:.78rem;color:var(--dim);cursor:pointer;
  border-left:2px solid transparent;transition:background-color .12s linear,color .12s linear;word-break:break-all}
.file-item:hover{background:rgba(255,255,255,0.03);color:var(--text)}
.file-item.active{background:rgba(56,189,248,0.08);color:var(--accent);
  border-left-color:var(--accent)}
.file-item .fi-name{font-weight:600}
.file-item .fi-date{font-size:.68rem;color:var(--dim);margin-top:3px;font-family:var(--mono)}

/* ── Table ── */
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{background:var(--panel);color:var(--dim);font-size:.7rem;font-weight:600;letter-spacing:.5px;
  padding:9px 12px;text-align:left;border-bottom:1px solid var(--border);text-transform:uppercase;font-family:var(--mono)}
td{padding:9px 12px;border-bottom:1px solid rgba(255,255,255,0.04);
  vertical-align:middle;word-break:break-all}
tr:hover td{background:rgba(255,255,255,0.02)}
tr.selected td{background:rgba(56,189,248,0.08);color:var(--text)}

/* ── Log ── */
.log-wrap{background:#05060a;border:1px solid var(--border);
  border-radius:var(--r);padding:14px 16px;height:calc(100vh - 180px);
  overflow-y:auto;font-family:var(--mono);font-size:.74rem;line-height:1.8;color:#94a3b8}
.log-wrap .log-err{color:var(--red)}
.log-wrap .log-ok{color:var(--green)}

/* ── Asset grid ── */
.asset-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}
.asset-item{background:var(--panel);border:1px solid var(--border);
  border-radius:var(--r);padding:10px 12px;font-size:.75rem;word-break:break-all;
  display:flex;flex-direction:column;gap:6px;transition:border-color .12s linear}
.asset-item:hover{border-color:rgba(255,255,255,0.18)}
.asset-item .ai-name{color:var(--text);font-weight:600}
.asset-item .ai-size{color:var(--dim);font-family:var(--mono);font-size:.68rem}
.asset-link{display:block;background:var(--input-bg);border:1px solid var(--border);
  border-radius:var(--r);padding:5px 8px;font-size:.72rem;color:var(--accent3);
  margin-top:4px;cursor:pointer;word-break:break-all;font-family:var(--mono);transition:border-color .12s linear}
.asset-link:hover{border-color:var(--accent3)}

/* ── Modal (Linear Animation & Calibrated Blur) ── */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.65);backdrop-filter:blur(10px);
  -webkit-backdrop-filter:blur(10px);display:flex;align-items:center;justify-content:center;z-index:9000;
  opacity:0;pointer-events:none;transition:opacity .16s linear;will-change:opacity}
.modal-overlay.open{opacity:1;pointer-events:all}
.modal{background:var(--panel);border:1px solid rgba(255,255,255,0.12);border-radius:var(--r-md);
  padding:24px 26px;width:min(520px,94vw);max-height:88vh;overflow-y:auto;
  transform:scale(0.98);opacity:0;box-shadow:0 16px 36px rgba(0,0,0,0.6);
  transition:transform .16s linear,opacity .16s linear;will-change:transform,opacity}
.modal-overlay.open .modal{transform:scale(1);opacity:1}
.modal h3{color:var(--text);margin-bottom:16px;font-size:.95rem;font-weight:700;display:flex;align-items:center;gap:8px}
.modal-footer{display:flex;justify-content:flex-end;gap:8px;margin-top:20px}

/* ── Checkbox ── */
.ck-wrap{display:flex;align-items:center;gap:8px;margin-top:10px}
.ck-wrap input[type=checkbox]{width:16px;height:16px;accent-color:var(--accent);cursor:pointer}

/* ── Status bar ── */
.statusbar{padding:5px 18px;background:var(--panel);border-top:1px solid var(--border);
  font-size:.72rem;font-family:var(--mono);color:var(--dim);display:flex;align-items:center;gap:12px;flex-shrink:0}
.statusbar .st-msg{flex:1}
.statusbar .st-dot{width:6px;height:6px;border-radius:50%;background:var(--green);flex-shrink:0}

/* ── Scrollbar ── */
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,0.22)}

/* ── Palette Swatches ── */
.swatch-chip{width:18px;height:18px;border-radius:50%;border:2px solid transparent;cursor:pointer;padding:0;transition:transform .12s linear,border-color .12s linear}
.swatch-chip:hover{transform:scale(1.28)}
.swatch-chip.active{border-color:#fff;transform:scale(1.18);box-shadow:0 0 6px rgba(255,255,255,0.45)}

@media(prefers-reduced-transparency:reduce){
  .modal-overlay{background:rgba(9,10,16,0.92);backdrop-filter:none}
}

/* ── Responsive ── */
@media(max-width:700px){
  .split-left{width:180px}
  .topbar .logo{font-size:.82rem}
}
</style>
</head>
<body>

<!-- Top Bar -->
<div class="topbar">
  <span class="logo">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="3" x2="9" y2="21"/></svg>
    MODERN_BLOG //
  </span>
  <span style="color:var(--dim);font-size:.74rem;font-family:var(--mono)">CONTROL PANEL</span>
  <span class="clock" id="clock">--:--:--</span>
</div>

<!-- Tab Bar -->
<div class="tabbar">
  <div class="tab active" data-tab="content">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
    内容
  </div>
  <div class="tab" data-tab="assets">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
    素材
  </div>
  <div class="tab" data-tab="settings">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
    配置
  </div>
  <div class="tab" data-tab="anime">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="2" y="7" width="20" height="15" rx="2" ry="2"/><polyline points="17 2 12 7 7 2"/></svg>
    追番
  </div>
  <div class="tab" data-tab="friends">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
    友链
  </div>
  <div class="tab" data-tab="audio">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
    音乐
  </div>
  <div class="tab" data-tab="backup">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
    备份
  </div>
  <div class="tab" data-tab="log">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
    日志
  </div>
</div>

<!-- Main Workspace -->
<div class="workspace">

  <!-- ═══════════════ TAB: 内容 ═══════════════ -->
  <div class="pane split" id="tab-content">
    <div class="split-left">
      <div class="sl-toolbar">
        <select id="lang-select" style="width:auto;flex:1">
          <option value="zh">ZH 中文</option>
          <option value="en">EN 英文</option>
        </select>
        <select id="mode-select" style="width:auto;flex:1">
          <option value="posts">文章</option>
          <option value="pages">页面</option>
        </select>
      </div>
      <div class="sl-toolbar" style="padding-top:0">
        <button class="btn green" onclick="newFile()" style="flex:1">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          新建
        </button>
      </div>
      <div id="file-list" style="flex:1;overflow-y:auto"></div>
    </div>
    <div class="split-right">
      <div class="btn-row">
        <button class="btn blue" onclick="saveFile()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
          保存
        </button>
        <button class="btn red" onclick="deleteFile()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
          删除
        </button>
        <button class="btn green" onclick="triggerBuild()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>
          构建
        </button>
        <button class="btn orange" onclick="triggerDeploy()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
          推送
        </button>
        <button class="btn" onclick="triggerCF()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/></svg>
          清理CF
        </button>
      </div>
      <div style="display:flex;gap:10px;margin-bottom:10px">
        <div style="flex:1">
          <label class="field-label">文章标题 *</label>
          <input id="f-title" placeholder="文章标题" oninput="updateTitleCounter()">
          <div class="char-counter cc-dim" id="cc-title">0 / 60 字符（SEO 推荐 30~60）</div>
        </div>
        <div style="width:140px">
          <label class="field-label">发布日期</label>
          <input id="f-date" placeholder="YYYY-MM-DD">
        </div>
      </div>
      <div style="display:flex;gap:10px;margin-bottom:10px">
        <div style="flex:1">
          <label class="field-label">分类（单选，如：技术/生活）</label>
          <input id="f-category" placeholder="分类名称">
        </div>
        <div style="flex:1">
          <label class="field-label">标签（英文逗号分隔）</label>
          <input id="f-tags" placeholder="Python, 算法, Web">
        </div>
        <div style="flex:1">
          <label class="field-label">封面图 URL</label>
          <div style="display:flex;gap:6px">
            <input id="f-cover" placeholder="/assets/cover.jpg" style="flex:1">
            <button type="button" class="btn blue" onclick="openCoverPicker()" style="flex-shrink:0;padding:0 10px" title="从素材库挑选封面">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
              素材库
            </button>
          </div>
        </div>
      </div>
      <div>
        <label class="field-label">文章摘要（meta description，建议 80~160 字符）</label>
        <textarea id="f-desc" style="min-height:55px" placeholder="留空则自动从正文前 150 字生成" oninput="updateDescCounter()"></textarea>
        <div class="char-counter cc-dim" id="cc-desc">0 / 160 字符（SEO 推荐 80~160）</div>
      </div>
      <div style="margin-top:10px">
        <label class="field-label">正文内容 (Markdown)</label>
        <textarea id="f-body" style="min-height:380px" placeholder="# 在这里写正文..."></textarea>
      </div>
      <div style="margin-top:8px;font-size:.72rem;color:var(--dim)" id="content-status"></div>
    </div>
  </div>

  <!-- ═══════════════ TAB: 素材 ═══════════════ -->
  <div class="pane" id="tab-assets" style="display:none">
    <div class="btn-row">
      <label class="btn green" style="cursor:pointer">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        上传文件 <input type="file" id="asset-upload" multiple style="display:none">
      </label>
      <input id="asset-link" readonly style="max-width:340px;display:inline-block" placeholder="点击文件后在此复制路径" onclick="copyAssetLink()">
      <button class="btn red" onclick="deleteSelectedAsset()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
        删除选中
      </button>
    </div>
    <div class="asset-grid" id="asset-grid"></div>
  </div>

  <!-- ═══════════════ TAB: 配置 ═══════════════ -->
  <div class="pane" id="tab-settings" style="display:none">
    <div class="section-title">站点基础信息</div>
    <div class="card">
      <div class="field-row">
        <div><label class="field-label">站点名称</label><input data-cfg="site_name" placeholder="我的个人博客"></div>
        <div><label class="field-label">站点域名（用于 RSS / 站点地图 / Canonical，需包含 https://）</label><input data-cfg="site_url" placeholder="https://example.com"></div>
      </div>
      <div class="field-row">
        <div><label class="field-label">SEO 关键词（逗号分隔）</label><input data-cfg="site_keywords" placeholder="技术, 博客, Python"></div>
        <div><label class="field-label">默认分享图（OG Image，完整 URL 或 /assets/ 路径）</label><input data-cfg="og_image" placeholder="https://example.com/assets/og.jpg"></div>
      </div>
      <div class="field-row">
        <div><label class="field-label">Logo 文字</label><input data-cfg="logo_text" placeholder="BLOG"></div>
        <div><label class="field-label">每页文章数</label><input data-cfg="posts_per_page" placeholder="8"></div>
        <div>
          <label class="field-label">主题调色盘（点击色块自由取色）</label>
          <div style="display:flex;align-items:center;gap:8px">
            <input type="color" id="cfg-color-picker" style="width:36px;height:32px;padding:2px;border:1px solid var(--border);border-radius:var(--r);background:var(--input-bg);cursor:pointer;flex-shrink:0" value="#ff99cc" aria-label="调色盘取色">
            <input data-cfg="theme_color" id="cfg-theme-color" placeholder="#ff99cc" style="font-family:var(--mono);text-transform:uppercase;flex:1" maxlength="7">
            <div id="cfg-color-preview" title="主色与色相衍射渐变预览" style="width:52px;height:32px;border-radius:var(--r);border:1px solid var(--border);flex-shrink:0;background:linear-gradient(135deg, #ff99cc 0%, #ffcc99 100%)"></div>
          </div>
        </div>
      </div>
      <div style="margin-top:8px;display:flex;align-items:center;gap:6px;flex-wrap:wrap">
        <span style="font-size:.7rem;color:var(--dim)">快捷推荐：</span>
        <button type="button" class="swatch-chip" data-color="#ff99cc" style="background:#ff99cc" title="Sakura 樱粉" aria-label="Sakura"></button>
        <button type="button" class="swatch-chip" data-color="#a855f7" style="background:#a855f7" title="Violet 紫罗兰" aria-label="Violet"></button>
        <button type="button" class="swatch-chip" data-color="#06b6d4" style="background:#06b6d4" title="Cyber 赛博青" aria-label="Cyber"></button>
        <button type="button" class="swatch-chip" data-color="#10b981" style="background:#10b981" title="Mint 薄荷绿" aria-label="Mint"></button>
        <button type="button" class="swatch-chip" data-color="#f59e0b" style="background:#f59e0b" title="Amber 暖金" aria-label="Amber"></button>
        <button type="button" class="swatch-chip" data-color="#f43f5e" style="background:#f43f5e" title="Rose 绯红" aria-label="Rose"></button>
        <button type="button" class="swatch-chip" data-color="#3b82f6" style="background:#3b82f6" title="Blue 霁蓝" aria-label="Blue"></button>
        <button type="button" class="swatch-chip" data-color="#64748b" style="background:#64748b" title="Slate 钛灰" aria-label="Slate"></button>
        <span style="font-size:.68rem;color:var(--dim);margin-left:4px">可直接点击色块取色或在输入框微调十六进制代码</span>
      </div>
      <div>
        <label class="field-label">全站背景图（为空则使用深色网格纯色底）</label>
        <input data-cfg="bg_url" placeholder="/assets/bg.jpg 或 https://...">
      </div>
      <div>
        <label class="field-label">随机背景图 API（填入后每次换页自动从该接口拉取一张随机壁纸）</label>
        <input data-cfg="random_img_api" placeholder="例如：https://api.example.com/random-wallpaper.php（留空使用全站背景）">
      </div>
      <div>
        <label class="field-label">文章页背景候选池（每行一个 URL，发布文章时随机选一个作为 post-hero 背景）</label>
        <textarea data-cfg="post_bg_urls" style="min-height:70px" placeholder="/assets/bg1.jpg&#10;/assets/bg2.jpg&#10;https://example.com/cover3.jpg"></textarea>
      </div>
      <div class="ck-wrap">
        <input type="checkbox" id="ck-hero-typing" data-cfg-bool="enable_hero_typing">
        <label for="ck-hero-typing" style="color:var(--dim);font-size:.8rem">启用首页 Hero 副标题逐字打字机效果</label>
      </div>
      <div class="ck-wrap">
        <input type="checkbox" id="ck-particles" data-cfg-bool="enable_particles">
        <label for="ck-particles" style="color:var(--dim);font-size:.8rem">启用鼠标跟随微光粒子动效（低配设备建议关闭）</label>
      </div>
      <div class="ck-wrap">
        <input type="checkbox" id="ck-icon-spin" data-cfg-bool="enable_icon_spin">
        <label for="ck-icon-spin" style="color:var(--dim);font-size:.8rem">启用前台装饰图标低速自转 / hover 旋转动效</label>
      </div>
      <div class="ck-wrap">
        <input type="checkbox" id="ck-tag-bounce" data-cfg-bool="enable_tag_bounce">
        <label for="ck-tag-bounce" style="color:var(--dim);font-size:.8rem">启用标签 / 分类 选中弹跳反馈动效</label>
      </div>
      <div>
        <label class="field-label">站点简介（meta description，全站默认，建议 80~160 字符）</label>
        <textarea id="cfg-site-desc" data-cfg="site_description" placeholder="一个记录技术、生活与思考的独立博客。" oninput="updateCounter(this, 'cc-site-desc', 160)"></textarea>
        <div class="char-counter cc-dim" id="cc-site-desc">0 / 160 字符（SEO 推荐 80~160）</div>
      </div>
      <div><label class="field-label">页脚文字</label><input data-cfg="footer_text" placeholder="© 2026 My Blog. Powered by ModernBlog."></div>
    </div>

    <div class="section-title">博主信息</div>
    <div class="card">
      <div class="field-row">
        <div><label class="field-label">博主昵称</label><input data-cfg="username" placeholder="站长昵称"></div>
        <div><label class="field-label">头像 URL</label><input data-cfg="avatar_url" placeholder="/assets/avatar.jpg"></div>
        <div><label class="field-label">建站日期（YYYY-MM-DD，用于计算运行天数）</label><input data-cfg="start_date" placeholder="2024-01-01"></div>
      </div>
      <div><label class="field-label">博主签名 / 一句话介绍</label><input data-cfg="bio" placeholder="Keep Coding, Stay Hungry."></div>
      <div class="field-row">
        <div><label class="field-label">首页大标题</label><input data-cfg="hero_title" placeholder="Hello, World."></div>
        <div><label class="field-label">首页副标题</label><input data-cfg="hero_subtitle" placeholder="欢迎来到我的数字花园。"></div>
      </div>
    </div>

    <div class="section-title">SEO & 搜索引擎收录（IndexNow 协议）</div>
    <div class="card">
      <p style="font-size:.74rem;color:var(--dim);margin-bottom:10px;line-height:1.6">
        启用后，每次执行「推送」时会自动向 IndexNow API 批量提交新增或修改的 URL，
        Bing、Yandex、Naver、Seznam 等搜索引擎会立即收到更新通知，收录速度从几天缩短到数小时。
      </p>
      <div class="ck-wrap" style="margin-top:0;margin-bottom:10px">
        <input type="checkbox" id="ck-indexnow" data-cfg-bool="enable_indexnow">
        <label for="ck-indexnow" style="color:var(--text);font-size:.8rem;font-weight:bold">启用 IndexNow 自动推送（需已配置「站点域名」）</label>
      </div>
      <div class="field-row">
        <div>
          <label class="field-label">IndexNow 密钥（Key）</label>
          <div style="display:flex;gap:8px">
            <input id="indexnow-key" readonly style="font-family:var(--mono);color:var(--accent)" placeholder="未生成（首次构建时会自动生成 32 位 hex key）">
            <button type="button" class="btn" onclick="regenIndexNowKey()" style="flex-shrink:0">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/></svg>
              重新生成
            </button>
          </div>
          <p style="font-size:.68rem;color:var(--dim);margin-top:4px">构建时会在 output 根目录自动生成 {key}.txt 验证文件供搜索引擎抓取核验。</p>
        </div>
      </div>
    </div>

    <div class="section-title">全站公告小部件</div>
    <div class="card">
      <div class="ck-wrap" style="margin-top:0;margin-bottom:8px">
        <input type="checkbox" id="ck-notice" data-cfg-bool="show_notice_widget">
        <label for="ck-notice" style="color:var(--dim);font-size:.8rem">在首页侧边栏显示公告卡片</label>
      </div>
      <div><label class="field-label">公告内容</label><input data-cfg="site_notice" placeholder="欢迎光临本站！"></div>
    </div>

    <div class="section-title">音乐播放器设置</div>
    <div class="card">
      <div class="ck-wrap" style="margin-top:0;margin-bottom:8px">
        <input type="checkbox" id="ck-player" data-cfg-bool="enable_player">
        <label for="ck-player" style="color:var(--dim);font-size:.8rem">前台显示音乐播放器（歌曲来自「音乐」Tab 上传的音频库）</label>
      </div>
      <div>
        <label class="field-label">默认播放模式</label>
        <select data-cfg="player_default_mode" style="max-width:240px">
          <option value="order">顺序播放</option>
          <option value="random">随机播放</option>
          <option value="single">单曲循环</option>
        </select>
      </div>
    </div>

    <div class="section-title">联系方式管理（支持自定义平台与链接）</div>
    <div class="card">
      <div class="btn-row" style="margin-bottom:10px">
        <button class="btn green" onclick="openContactModal()">+ 添加联系方式</button>
        <button class="btn" onclick="editContact()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
          编辑
        </button>
        <button class="btn red" onclick="deleteContact()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          删除
        </button>
        <button class="btn" onclick="moveContact(-1)">↑ 上移</button>
        <button class="btn" onclick="moveContact(1)">↓ 下移</button>
        <button class="btn blue" onclick="saveContactList()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
          保存联系方式
        </button>
      </div>
      <table id="contact-table">
        <thead><tr>
          <th>平台名称</th><th>FontAwesome 图标类名</th><th>链接 URL</th>
        </tr></thead>
        <tbody id="contact-tbody"></tbody>
      </table>
    </div>

    <div class="section-title">广告代码（Monetag 等流量变现平台）</div>
    <div class="card">
      <div>
        <label class="field-label">Monetag 广告标签代码（In-Page Push / Vignette 等）</label>
        <textarea data-cfg="monetag_tag_code" style="min-height:90px" placeholder="粘贴 Monetag 后台生成的广告 <script> 完整代码，将在前台所有页面 <head> 中自动加载。若无需变现请留空。"></textarea>
      </div>
      <div style="margin-top:10px">
        <label class="field-label">Service Worker 脚本（sw.js，用于推送通知等功能）</label>
        <div class="btn-row" style="margin-bottom:6px">
          <label class="btn blue" style="cursor:pointer">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
            上传 sw.js <input type="file" id="sw-upload" accept=".js" style="display:none">
          </label>
          <button class="btn red" onclick="deleteSwJs()">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
            删除 sw.js
          </button>
        </div>
        <p id="sw-status" style="font-size:.72rem;color:var(--dim)"></p>
      </div>
    </div>

    <div class="section-title">Git 部署与 Cloudflare 配置</div>
    <div class="card">
      <div><label class="field-label">Git 仓库地址（SSH 或带 Token 的 HTTPS，推送到此仓库的 main 分支）</label>
        <input data-cfg="deploy_repo" placeholder="git@github.com:user/repo.git"></div>
      <div class="field-row">
        <div><label class="field-label">Git 提交者昵称</label><input data-cfg="git_user_name" placeholder="Deployer"></div>
        <div><label class="field-label">Git 提交者邮箱</label><input data-cfg="git_user_email" placeholder="deploy@example.com"></div>
      </div>
      <div class="field-row">
        <div><label class="field-label">Cloudflare 账户邮箱</label><input data-cfg="cf_email" placeholder="user@example.com"></div>
        <div><label class="field-label">Cloudflare Zone ID</label><input data-cfg="cf_zone_id" placeholder="32位十六进制字符串"></div>
      </div>
      <div><label class="field-label">Cloudflare API Token（需具备 Purge Cache 权限）</label>
        <input data-cfg="cf_api_token" type="password" placeholder="CF API Token"></div>
    </div>

    <div class="btn-row" style="margin-top:16px">
      <button class="btn primary" onclick="saveConfig()" style="font-size:.9rem;padding:9px 24px">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
        保存所有配置
      </button>
    </div>
  </div>

  <!-- ═══════════════ TAB: 追番 ═══════════════ -->
  <div class="pane" id="tab-anime" style="display:none">
    <div class="section-title">正在追番管理</div>
    <div class="btn-row">
      <button class="btn green" onclick="openAnimeModal()">+ 添加番剧</button>
      <button class="btn" onclick="editAnime()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
        编辑
      </button>
      <button class="btn red" onclick="deleteAnime()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        删除
      </button>
      <button class="btn" onclick="moveAnime(-1)">↑ 上移</button>
      <button class="btn" onclick="moveAnime(1)">↓ 下移</button>
      <button class="btn blue" onclick="saveAnimeList()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
        保存到配置
      </button>
    </div>
    <table id="anime-table">
      <thead><tr>
        <th>标题</th><th>状态</th><th>当前集</th><th>总集数</th><th>封面URL</th><th>备注</th>
      </tr></thead>
      <tbody id="anime-tbody"></tbody>
    </table>
  </div>

  <!-- ═══════════════ TAB: 友链 ═══════════════ -->
  <div class="pane" id="tab-friends" style="display:none">
    <div class="section-title">友链管理</div>
    <div class="btn-row">
      <button class="btn green" onclick="openFriendModal()">+ 添加友链</button>
      <button class="btn" onclick="editFriend()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
        编辑
      </button>
      <button class="btn red" onclick="deleteFriend()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        删除
      </button>
      <button class="btn" onclick="moveFriend(-1)">↑ 上移</button>
      <button class="btn" onclick="moveFriend(1)">↓ 下移</button>
      <button class="btn blue" onclick="saveFriendList()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
        保存到配置
      </button>
    </div>
    <table id="friend-table">
      <thead><tr>
        <th>名称</th><th>链接</th><th>头像URL</th><th>描述</th>
      </tr></thead>
      <tbody id="friend-tbody"></tbody>
    </table>
  </div>

  <!-- ═══════════════ TAB: 音乐 ═══════════════ -->
  <div class="pane" id="tab-audio" style="display:none">
    <div class="section-title">音频库管理（仅支持 mp3 / flac，用于前台音乐播放器）</div>
    <div class="btn-row">
      <label class="btn green" style="cursor:pointer">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        上传音频 <input type="file" id="audio-upload" accept=".mp3,.flac" multiple style="display:none">
      </label>
      <button class="btn" onclick="editAudioTrack()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
        编辑信息
      </button>
      <label class="btn" style="cursor:pointer">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>
        上传 lrc 歌词 <input type="file" id="lrc-upload" accept=".lrc" style="display:none">
      </label>
      <button class="btn" onclick="removeAudioLrc()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        移除 lrc
      </button>
      <button class="btn red" onclick="deleteAudioTrack()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
        删除
      </button>
      <button class="btn" onclick="moveAudio(-1)">↑ 上移</button>
      <button class="btn" onclick="moveAudio(1)">↓ 下移</button>
      <button class="btn blue" onclick="saveAudioOrder()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
        保存顺序
      </button>
    </div>
    <table id="audio-table">
      <thead><tr>
        <th>标题</th><th>歌手</th><th>格式</th><th>时长</th><th>歌词来源</th>
      </tr></thead>
      <tbody id="audio-tbody"></tbody>
    </table>
    <p class="hint" style="font-size:.72rem;color:var(--dim);margin-top:12px;line-height:1.6">
      提示：上传音频时会自动提取 ID3 标签（标题、艺术家、时长、内嵌 USLT/SYLT 歌词）。前台播放器优先使用
      音频自带的内嵌歌词（如果解析到）或手动上传的 .lrc 逐行同步歌词；都没有则前台不显示歌词。
    </p>
  </div>

  <!-- ═══════════════ TAB: 备份 ═══════════════ -->
  <div class="pane" id="tab-backup" style="display:none">
    <div class="section-title">本地备份（配置 + 文章 + 页面，纯文本内容，体积小、传输快）</div>
    <div class="btn-row">
      <button class="btn green" onclick="createBackup()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>
        立即创建备份
      </button>
      <button class="btn" onclick="loadBackups()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/></svg>
        刷新列表
      </button>
      <label class="btn blue" style="cursor:pointer">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        上传 zip 并恢复 <input type="file" id="restore-upload" accept=".zip" style="display:none">
      </label>
    </div>
    <table id="backup-table">
      <thead><tr>
        <th>文件名</th><th>大小</th><th>创建时间</th><th style="width:280px">操作</th>
      </tr></thead>
      <tbody id="backup-tbody"></tbody>
    </table>

    <div class="section-title">云端备份（自建 PHP 备份接口）</div>
    <div class="card">
      <div class="field-row">
        <div><label class="field-label">云端 API 地址（api.php 完整 URL，需 https://）</label>
          <input data-cfg="cloud_backup_url" placeholder="https://your-domain.com/backup/api.php"></div>
        <div><label class="field-label">访问安全 Key（32位，大小写字母+数字，安装云端面板时生成）</label>
          <input data-cfg="cloud_backup_key" type="password" placeholder="32位高强度Key"></div>
      </div>
      <div class="field-row">
        <div style="max-width:220px">
          <label class="field-label">本地备份保留份数</label>
          <input data-cfg="backup_retention" placeholder="10">
        </div>
        <div style="display:flex;align-items:flex-end;padding-bottom:8px">
          <div class="ck-wrap" style="margin-top:0">
            <input type="checkbox" id="ck-auto-backup" data-cfg-bool="auto_backup_on_deploy">
            <label for="ck-auto-backup" style="color:var(--dim);font-size:.8rem">每次「推送」成功后自动创建一份备份</label>
          </div>
        </div>
      </div>
      <div class="btn-row" style="margin-top:6px">
        <button class="btn primary" onclick="saveConfig()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
          保存云端配置
        </button>
        <button class="btn blue" onclick="testCloudConnection()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="10"/><path d="m10 15 5-3-5-3v6z"/></svg>
          测试连接
        </button>
      </div>
      <p id="cloud-status" style="font-size:.72rem;color:var(--dim);margin-top:4px"></p>
      <p class="hint" style="font-size:.72rem;color:var(--dim);margin-top:6px">
        备份只包含 config.json / posts / pages 纯文本内容，不含附件，正常情况下体积在几十 KB 到
        几百 KB 之间，即使网络不稳定也能可靠、快速地上传下载。
      </p>
    </div>

    <div class="btn-row">
      <button class="btn" onclick="loadCloudBackups()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/></svg>
        刷新云端列表
      </button>
    </div>
    <table id="cloud-backup-table">
      <thead><tr>
        <th>文件名</th><th>大小</th><th>创建时间</th><th style="width:220px">操作</th>
      </tr></thead>
      <tbody id="cloud-backup-tbody"></tbody>
    </table>
  </div>

  <!-- ═══════════════ TAB: 日志 ═══════════════ -->
  <div class="pane" id="tab-log" style="display:none">
    <div class="btn-row">
      <button class="btn red" onclick="clearLog()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
        清空
      </button>
      <span style="font-size:.72rem;color:var(--dim);font-family:var(--mono)">实时推送（SSE）</span>
    </div>
    <div class="log-wrap" id="log-box"></div>
  </div>

</div>

<!-- Status Bar -->
<div class="statusbar">
  <div class="st-dot" id="st-dot"></div>
  <div class="st-msg" id="st-msg">就绪</div>
</div>

<!-- Modal: 番剧编辑 -->
<div class="modal-overlay" id="anime-modal">
  <div class="modal">
    <h3 id="anime-modal-title">添加番剧</h3>
    <input type="hidden" id="anime-edit-idx" value="-1">
    <label class="field-label">标题 *</label>
    <input id="am-title" placeholder="如：葬送的芙莉莲">
    <label class="field-label">封面图片 URL</label>
    <input id="am-cover" placeholder="https://... 或 /assets/...">
    <div class="field-row">
      <div><label class="field-label">当前集数</label><input id="am-ep" placeholder="12"></div>
      <div><label class="field-label">总集数</label><input id="am-total" placeholder="28"></div>
    </div>
    <label class="field-label">个人简评 / 备注</label>
    <input id="am-note" placeholder="如：神作，作画顶级">
    <label class="field-label">观看状态</label>
    <select id="am-status">
      <option value="airing">连载中 (airing)</option>
      <option value="done">已追完 (done)</option>
      <option value="plan">想看 (plan)</option>
    </select>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('anime-modal')">取消</button>
      <button class="btn primary" onclick="confirmAnime()">确定</button>
    </div>
  </div>
</div>

<!-- Modal: 封面图片挑选器 -->
<div class="modal-overlay" id="cover-modal">
  <div class="modal" style="width:min(680px,94vw)">
    <h3>从素材库挑选封面图</h3>
    <div id="cover-picker-grid" class="asset-grid" style="max-height:55vh;overflow-y:auto;margin-top:10px"></div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('cover-modal')">关闭</button>
    </div>
  </div>
</div>

<!-- Modal: 友链编辑 -->
<div class="modal-overlay" id="friend-modal">
  <div class="modal">
    <h3 id="friend-modal-title">添加友链</h3>
    <input type="hidden" id="friend-edit-idx" value="-1">
    <label class="field-label">站点名称 *</label>
    <input id="fm-name" placeholder="友人博客">
    <label class="field-label">站点链接 *</label>
    <input id="fm-url" placeholder="https://friend.com">
    <label class="field-label">头像 URL</label>
    <input id="fm-avatar" placeholder="https://friend.com/avatar.png">
    <label class="field-label">站点描述</label>
    <input id="fm-desc" placeholder="一个有趣的博客">
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('friend-modal')">取消</button>
      <button class="btn primary" onclick="confirmFriend()">确定</button>
    </div>
  </div>
</div>

<!-- Modal: 联系方式编辑 -->
<div class="modal-overlay" id="contact-modal">
  <div class="modal">
    <h3 id="contact-modal-title">添加联系方式</h3>
    <label class="field-label">平台名称 *（如：GitHub、Twitter、Telegram、Email）</label>
    <input id="cm-label" placeholder="GitHub">
    <label class="field-label">FontAwesome 图标类名（如：fab fa-github、fas fa-envelope、fab fa-telegram）</label>
    <input id="cm-icon" placeholder="fab fa-github">
    <label class="field-label">链接 URL *（如：https://github.com/username 或 mailto:user@example.com）</label>
    <input id="cm-url" placeholder="https://github.com/your-username">
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('contact-modal')">取消</button>
      <button class="btn primary" onclick="confirmContact()">确定</button>
    </div>
  </div>
</div>

<!-- Modal: 音轨信息编辑 -->
<div class="modal-overlay" id="audio-modal">
  <div class="modal">
    <h3>编辑音轨信息</h3>
    <input type="hidden" id="am2-id">
    <label class="field-label">曲目标题 *</label>
    <input id="am2-title" placeholder="歌曲名称">
    <label class="field-label">艺术家 / 歌手</label>
    <input id="am2-artist" placeholder="未知艺术家">
    <label class="field-label">歌词来源</label>
    <input id="am2-lyrics-source" readonly style="color:var(--dim);background:transparent;border-style:dashed">
    <p id="am2-lyrics-hint" style="font-size:.72rem;color:var(--dim);margin-top:6px;line-height:1.5"></p>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('audio-modal')">取消</button>
      <button class="btn primary" onclick="confirmAudioTrack()">确定</button>
    </div>
  </div>
</div>

<script>
let currentFile = null;
let currentTab = 'content';
let selectedAsset = null;
let animeList = [];
let selectedAnimeRow = -1;
let friendList = [];
let selectedFriendRow = -1;
let contactList = [];
let selectedContactRow = -1;
let audioList = [];
let selectedAudioRow = -1;
let logSource = null;

// 时钟
setInterval(() => {
  const d = new Date();
  document.getElementById('clock').textContent =
    String(d.getHours()).padStart(2,'0') + ':' +
    String(d.getMinutes()).padStart(2,'0') + ':' +
    String(d.getSeconds()).padStart(2,'0');
}, 1000);

function escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/[&<>"']/g, m => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[m]);
}

function setStatus(msg, ok=true) {
  const dot = document.getElementById('st-dot');
  const txt = document.getElementById('st-msg');
  dot.style.background = ok ? 'var(--green)' : 'var(--red)';
  txt.textContent = msg;
  txt.style.color = ok ? 'var(--text)' : 'var(--red)';
  if (ok) setTimeout(() => {
    dot.style.background = 'var(--green)';
    txt.textContent = '就绪';
    txt.style.color = 'var(--dim)';
  }, 4000);
}

// Tab 切换
document.querySelectorAll('.tab').forEach(t => {
  t.onclick = () => switchTab(t.dataset.tab);
});

function switchTab(name) {
  currentTab = name;
  document.querySelectorAll('.tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === name);
  });
  document.querySelectorAll('.pane').forEach(p => {
    p.style.display = (p.id === 'tab-' + name) ? (name === 'content' ? 'flex' : 'block') : 'none';
  });
  if (name === 'content') loadFileList();
  else if (name === 'assets') loadAssets();
  else if (name === 'settings') { loadSettings(); loadContacts(); checkSwStatus(); }
  else if (name === 'anime') loadAnime();
  else if (name === 'friends') loadFriends();
  else if (name === 'audio') loadAudio();
  else if (name === 'backup') loadBackups();
  else if (name === 'log') startLog();
}

// ─── Content ───
document.getElementById('lang-select').onchange = loadFileList;
document.getElementById('mode-select').onchange = loadFileList;

async function loadFileList() {
  const lang = document.getElementById('lang-select').value;
  const mode = document.getElementById('mode-select').value;
  const res = await fetch(`/api/files?lang=${encodeURIComponent(lang)}&mode=${encodeURIComponent(mode)}`);
  const files = await res.json();
  const list = document.getElementById('file-list');
  list.innerHTML = '';
  files.forEach(f => {
    const item = document.createElement('div');
    item.className = 'file-item' + (currentFile === f.name ? ' active' : '');
    item.dataset.filename = f.name;
    item.innerHTML = `<div class="fi-name">${escapeHtml(f.title || f.name)}</div>
      <div class="fi-date">${escapeHtml(f.date || '')} ${escapeHtml(f.name)}</div>`;
    item.onclick = () => loadFile(f.name);
    list.appendChild(item);
  });
}

function clearEditor() {
  currentFile = null;
  document.getElementById('f-title').value = '';
  document.getElementById('f-date').value = new Date().toISOString().slice(0,10);
  document.getElementById('f-category').value = '';
  document.getElementById('f-tags').value = '';
  document.getElementById('f-cover').value = '';
  document.getElementById('f-desc').value = '';
  document.getElementById('f-body').value = '';
  updateTitleCounter();
  updateDescCounter();
  document.getElementById('content-status').textContent = '';
  document.querySelectorAll('.file-item').forEach(i => i.classList.remove('active'));
}

async function loadFile(name) {
  const lang = document.getElementById('lang-select').value;
  const mode = document.getElementById('mode-select').value;
  try {
    const res = await fetch(`/api/file?lang=${encodeURIComponent(lang)}&mode=${encodeURIComponent(mode)}&name=${encodeURIComponent(name)}`);
    const d = await res.json();
    if (!res.ok || d.error) {
      setStatus('[ERR] 读取文件失败: ' + (d.error || 'not found'), false);
      return;
    }
    currentFile = name;
    document.getElementById('f-title').value = d.title || '';
    document.getElementById('f-date').value = d.date || '';
    document.getElementById('f-category').value = d.category || '';

    // 安全处理 tags：可能是数组，也可能是已拼接的字符串
    if (Array.isArray(d.tags)) {
      document.getElementById('f-tags').value = d.tags.join(', ');
    } else {
      document.getElementById('f-tags').value = d.tags_str || d.tags || '';
    }

    document.getElementById('f-cover').value = d.cover || '';
    document.getElementById('f-desc').value = d.description || '';

    // 正文内容优先读取 body，兜底 content
    const bodyVal = (d.body !== undefined && d.body !== null) ? d.body : (d.content || '');
    document.getElementById('f-body').value = bodyVal;

    updateTitleCounter();
    updateDescCounter();
    document.getElementById('content-status').textContent = `已载入: ${name} (UID:${d.uid || '无'})`;
    document.querySelectorAll('.file-item').forEach(i => {
      i.classList.toggle('active', i.dataset.filename === name || i.textContent.includes(name));
    });
  } catch(err) {
    console.error('loadFile error:', err);
    setStatus('[ERR] 载入异常: ' + err.message, false);
  }
}

function newFile() {
  clearEditor();
  document.getElementById('f-title').focus();
  setStatus('[OK] 已新建空白文章/页面');
}

function updateTitleCounter() {
  const v = document.getElementById('f-title').value;
  const c = document.getElementById('cc-title');
  const len = v.length;
  c.textContent = `${len} / 60 字符（SEO 推荐 30~60）`;
  c.className = 'char-counter ' + (len === 0 ? 'cc-dim' : len < 30 ? 'cc-warn' : len <= 60 ? 'cc-ok' : 'cc-bad');
}

function updateDescCounter() {
  const v = document.getElementById('f-desc').value;
  const c = document.getElementById('cc-desc');
  const len = v.length;
  c.textContent = `${len} / 160 字符（SEO 推荐 80~160）`;
  c.className = 'char-counter ' + (len === 0 ? 'cc-dim' : len < 80 ? 'cc-warn' : len <= 160 ? 'cc-ok' : 'cc-bad');
}

async function saveFile() {
  const lang = document.getElementById('lang-select').value;
  const mode = document.getElementById('mode-select').value;
  const title = document.getElementById('f-title').value.trim();
  if (!title) { alert('文章标题不能为空'); return; }
  const tags = document.getElementById('f-tags').value.split(',').map(s=>s.trim()).filter(Boolean);
  const body = document.getElementById('f-body').value;
  const payload = {
    lang, mode,
    name: currentFile,
    original_name: currentFile,
    title, date: document.getElementById('f-date').value,
    category: document.getElementById('f-category').value.trim(),
    tags, cover: document.getElementById('f-cover').value.trim(),
    description: document.getElementById('f-desc').value.trim(),
    body: body,
    content: body,
  };
  const res = await fetch('/api/file', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  const d = await res.json();
  if (d.ok) {
    currentFile = d.name;
    setStatus('[OK] 保存成功: ' + d.name);
    document.getElementById('content-status').textContent = `[OK] 已保存 UID:${d.uid}`;
    loadFileList();
  } else {
    alert('保存失败: ' + d.error);
    setStatus('[ERR] 保存失败', false);
  }
}

async function deleteFile() {
  if (!currentFile) { alert('未选中任何文件'); return; }
  if (!confirm(`确认删除「${currentFile}」？此操作不可撤销。`)) return;
  const lang = document.getElementById('lang-select').value;
  const mode = document.getElementById('mode-select').value;
  const res = await fetch('/api/file', {method:'DELETE', headers:{'Content-Type':'application/json'}, body:JSON.stringify({lang, mode, name:currentFile})});
  const d = await res.json();
  if (d.ok) {
    clearEditor();
    setStatus('[OK] 已删除');
    loadFileList();
  } else {
    alert('删除失败: ' + (d.error || '未知错误'));
  }
}

async function triggerBuild() {
  setStatus('[...] 构建中...');
  switchTab('log');
  const res = await fetch('/api/build', {method:'POST'});
  const d = await res.json();
  if (!d.ok) setStatus('[ERR] 构建已在进行中', false);
}

async function triggerDeploy() {
  setStatus('[...] 推送中...');
  switchTab('log');
  const res = await fetch('/api/deploy', {method:'POST'});
  const d = await res.json();
  if (!d.ok) setStatus('[ERR] 推送已在进行中', false);
}

async function triggerCF() {
  setStatus('[...] 清理 CF 缓存...');
  const res = await fetch('/api/purge_cf', {method:'POST'});
  const d = await res.json();
  if (d.ok) setStatus('[OK] CF 缓存已清除');
  else { alert('CF 清除失败: ' + d.error); setStatus('[ERR] 清除失败', false); }
}

// ─── Assets ───
async function loadAssets() {
  const res = await fetch('/api/assets');
  const files = await res.json();
  const grid = document.getElementById('asset-grid');
  grid.innerHTML = '';
  selectedAsset = null;
  files.forEach(f => {
    const item = document.createElement('div');
    item.className = 'asset-item';
    const isImg = /\.(png|jpe?g|gif|webp|avif|svg|ico|bmp)$/i.test(f.name);
    let preview = isImg ? `<div style="height:90px;background:#05060a;border-radius:4px;overflow:hidden;display:flex;align-items:center;justify-content:center"><img src="/assets/${encodeURIComponent(f.name)}" style="max-height:100%;max-width:100%;object-fit:cover" loading="lazy" onerror="this.onerror=null;this.src='/attachments/${encodeURIComponent(f.name)}'"></div>` : `<div style="height:90px;background:rgba(255,255,255,0.03);border-radius:4px;display:flex;align-items:center;justify-content:center;color:var(--dim)"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg></div>`;
    item.innerHTML = preview + `
      <div class="ai-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</div>
      <div class="ai-size">${escapeHtml(f.size)}</div>
      <div class="asset-link" onclick="event.stopPropagation();selectAsset('${escapeHtml(f.name)}');copyAssetLink()">/assets/${escapeHtml(f.name)}</div>`;
    item.onclick = () => selectAsset(f.name, item);
    grid.appendChild(item);
  });
}

function selectAsset(name, el) {
  selectedAsset = name;
  document.getElementById('asset-link').value = `/assets/${name}`;
  document.querySelectorAll('.asset-item').forEach(i => i.style.borderColor = 'var(--border)');
  if (el) el.style.borderColor = 'var(--accent)';
}

function copyAssetLink() {
  const v = document.getElementById('asset-link').value;
  if (v) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(v).then(() => setStatus('[OK] 已复制: ' + v)).catch(() => {
        fallbackCopyText(v);
      });
    } else {
      fallbackCopyText(v);
    }
  }
}

function fallbackCopyText(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand('copy');
    setStatus('[OK] 已复制: ' + text);
  } catch(e) {
    prompt('请手动复制链接:', text);
  }
  document.body.removeChild(ta);
}

async function deleteSelectedAsset() {
  if (!selectedAsset) { alert('请先点击选中一个素材文件'); return; }
  if (!confirm(`确认删除素材「${selectedAsset}」？`)) return;
  const res = await fetch('/api/asset', {method:'DELETE', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:selectedAsset})});
  const d = await res.json();
  if (d.ok) { setStatus('[OK] 素材已删除'); loadAssets(); }
  else alert('删除失败: ' + d.error);
}

// 挑选封面 Modal
async function openCoverPicker() {
  const res = await fetch('/api/assets');
  const files = await res.json();
  const grid = document.getElementById('cover-picker-grid');
  grid.innerHTML = '';
  const imgFiles = files.filter(f => /\.(png|jpe?g|gif|webp|avif|svg|ico|bmp)$/i.test(f.name));
  if (!imgFiles.length) {
    grid.innerHTML = '<div style="color:var(--dim);font-size:.8rem;padding:20px;grid-column:1/-1">素材库中暂无图片，请在「素材」tab 上传。</div>';
  } else {
    imgFiles.forEach(f => {
      const item = document.createElement('div');
      item.className = 'asset-item';
      item.style.cursor = 'pointer';
      item.innerHTML = `
        <div style="height:90px;background:#05060a;border-radius:4px;overflow:hidden;display:flex;align-items:center;justify-content:center">
          <img src="/assets/${encodeURIComponent(f.name)}" style="max-height:100%;max-width:100%;object-fit:cover" loading="lazy" onerror="this.onerror=null;this.src='/attachments/${encodeURIComponent(f.name)}'">
        </div>
        <div class="ai-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</div>
        <div class="ai-size">${escapeHtml(f.size)}</div>`;
      item.onclick = () => {
        document.getElementById('f-cover').value = `/assets/${f.name}`;
        closeModal('cover-modal');
        setStatus('[OK] 已设为封面: ' + f.name);
      };
      grid.appendChild(item);
    });
  }
  document.getElementById('cover-modal').classList.add('open');
}

// 上传素材
document.getElementById('asset-upload').onchange = async (e) => {
  const files = e.target.files;
  if (!files.length) return;
  const fd = new FormData();
  for (let f of files) fd.append('files', f);
  const res = await fetch('/api/upload_asset', {method:'POST', body:fd});
  const d = await res.json();
  if (d.ok) {
    setStatus(`[OK] 上传了 ${d.names.length} 个文件`);
    loadAssets();
  } else alert('上传失败: ' + d.error);
  e.target.value = '';
};

// ─── Theme Color Palette Helper ───
const LEGACY_COLOR_PRESETS = {
  'sakura': '#ff99cc', 'violet': '#a855f7', 'cyber': '#06b6d4',
  'gold': '#f59e0b', 'mint': '#10b981', 'dark': '#1e293b'
};

function shiftHueClient(hex, deg) {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex || '');
  if (!m) return hex;
  let r = parseInt(m[1], 16)/255, g = parseInt(m[2], 16)/255, b = parseInt(m[3], 16)/255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  let h, s, l = (max + min) / 2;
  if (max === min) { h = s = 0; } else {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    switch (max) {
      case r: h = (g - b) / d + (g < b ? 6 : 0); break;
      case g: h = (b - r) / d + 2; break;
      default: h = (r - g) / d + 4;
    }
    h /= 6;
  }
  h = (h + deg / 360) % 1;
  function hue2rgb(p, q, t) {
    if (t < 0) t += 1; if (t > 1) t -= 1;
    if (t < 1 / 6) return p + (q - p) * 6 * t;
    if (t < 1 / 2) return q;
    if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
    return p;
  }
  let r2, g2, b2;
  if (s === 0) { r2 = g2 = b2 = l; } else {
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    r2 = hue2rgb(p, q, h + 1 / 3); g2 = hue2rgb(p, q, h); b2 = hue2rgb(p, q, h - 1 / 3);
  }
  return '#' + [r2, g2, b2].map(x => Math.round(x * 255).toString(16).padStart(2, '0')).join('');
}

function syncThemeColorUI(val) {
  if (!val) val = '#ff99cc';
  val = String(val).trim().toLowerCase();
  if (val in LEGACY_COLOR_PRESETS) val = LEGACY_COLOR_PRESETS[val];
  if (!val.startsWith('#') && /^[0-9a-f]{6}$/i.test(val)) val = '#' + val;
  const textInput = document.getElementById('cfg-theme-color');
  const picker = document.getElementById('cfg-color-picker');
  const prev = document.getElementById('cfg-color-preview');
  if (textInput && textInput.value.toLowerCase() !== val) textInput.value = val;
  if (/^#[0-9a-f]{6}$/i.test(val)) {
    if (picker) picker.value = val;
    if (prev) prev.style.background = `linear-gradient(135deg, ${val} 0%, ${shiftHueClient(val, 30)} 100%)`;
  }
  document.querySelectorAll('.swatch-chip').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.color.toLowerCase() === val);
  });
}

function initThemeColorPicker() {
  const picker = document.getElementById('cfg-color-picker');
  const textInput = document.getElementById('cfg-theme-color');
  if (picker) {
    picker.addEventListener('input', () => syncThemeColorUI(picker.value));
  }
  if (textInput) {
    textInput.addEventListener('input', () => {
      let v = textInput.value.trim();
      if (!v.startsWith('#') && v.length === 6) v = '#' + v;
      if (/^#[0-9a-f]{6}$/i.test(v)) syncThemeColorUI(v);
    });
  }
  document.querySelectorAll('.swatch-chip').forEach(btn => {
    btn.addEventListener('click', () => syncThemeColorUI(btn.dataset.color));
  });
}

// ─── Settings ───
async function loadSettings() {
  const res = await fetch('/api/config');
  const cfg = await res.json();
  document.querySelectorAll('[data-cfg]').forEach(el => {
    const k = el.dataset.cfg;
    if (k in cfg) {
      if (Array.isArray(cfg[k])) el.value = cfg[k].join('\n');
      else el.value = cfg[k] || '';
    }
  });
  document.querySelectorAll('[data-cfg-bool]').forEach(el => {
    const k = el.dataset.cfgBool;
    if (k in cfg) el.checked = Boolean(cfg[k]);
  });
  syncThemeColorUI(cfg.theme_color || '#ff99cc');
  const kEl = document.getElementById('indexnow-key');
  if (kEl) kEl.value = cfg.indexnow_key || '';
  const dEl = document.getElementById('cfg-site-desc');
  if (dEl) updateCounter(dEl, 'cc-site-desc', 160);
}

async function regenIndexNowKey() {
  if (!confirm('重新生成 IndexNow Key 后，原密钥将失效，下次「构建」时会自动生成新的 {key}.txt 验证文件。\n\n确认重新生成？')) return;
  const res = await fetch('/api/indexnow/regen_key', {method:'POST'});
  const d = await res.json();
  if (d.ok) { document.getElementById('indexnow-key').value = d.key; setStatus('[OK] IndexNow 密钥已重新生成'); }
  else alert('重新生成失败: ' + d.error);
}

function updateCounter(inputEl, counterId, targetMax) {
  const c = document.getElementById(counterId);
  if (!c) return;
  const len = inputEl.value.length;
  c.textContent = `${len} / ${targetMax} 字符（SEO 推荐 80~${targetMax}）`;
  c.className = 'char-counter ' + (len === 0 ? 'cc-dim' : len < 80 ? 'cc-warn' : len <= targetMax ? 'cc-ok' : 'cc-bad');
}

async function saveConfig() {
  const cfg = {};
  document.querySelectorAll('[data-cfg]').forEach(el => {
    const k = el.dataset.cfg;
    if (k === 'post_bg_urls') {
      cfg[k] = el.value.split('\n').map(s=>s.trim()).filter(Boolean);
    } else if (k === 'posts_per_page' || k === 'backup_retention') {
      cfg[k] = parseInt(el.value, 10) || 8;
    } else {
      cfg[k] = el.value;
    }
  });
  document.querySelectorAll('[data-cfg-bool]').forEach(el => {
    cfg[el.dataset.cfgBool] = el.checked;
  });
  const res = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(cfg)});
  const d = await res.json();
  if (d.ok) { setStatus('[OK] 配置保存成功'); alert('配置保存成功！重新构建后生效。'); }
  else alert('保存失败: ' + d.error);
}

// sw.js 管理
async function checkSwStatus() {
  const res = await fetch('/api/sw_status');
  const d = await res.json();
  const el = document.getElementById('sw-status');
  if (el) el.textContent = d.exists ? '[OK] sw.js 已上传' : '[WARN] 未检测到 sw.js';
}

document.getElementById('sw-upload').onchange = async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  const res = await fetch('/api/upload_sw', {method:'POST', body:fd});
  const d = await res.json();
  if (d.ok) { checkSwStatus(); setStatus('[OK] sw.js 上传成功'); }
  else alert('上传失败: ' + d.error);
  e.target.value = '';
};

async function deleteSwJs() {
  if (!confirm('确认删除 sw.js？')) return;
  const res = await fetch('/api/delete_sw', {method:'DELETE'});
  const d = await res.json();
  if (d.ok) { checkSwStatus(); setStatus('[OK] sw.js 已删除'); }
  else alert('删除失败: ' + d.error);
}

// ─── Anime ───
async function loadAnime() {
  const res = await fetch('/api/anime');
  animeList = await res.json();
  renderAnimeTable();
}

function renderAnimeTable() {
  const tb = document.getElementById('anime-tbody');
  tb.innerHTML = '';
  animeList.forEach((a, i) => {
    const tr = document.createElement('tr');
    if (i === selectedAnimeRow) tr.classList.add('selected');
    tr.innerHTML = `<td>${escapeHtml(a.title)}</td>
      <td><span style="font-size:.72rem;padding:2px 6px;border-radius:4px;background:rgba(255,255,255,.06);font-family:var(--mono)">${escapeHtml(a.status||'airing')}</span></td>
      <td style="font-family:var(--mono)">${escapeHtml(String(a.ep||''))}</td>
      <td style="font-family:var(--mono)">${escapeHtml(String(a.total||''))}</td>
      <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;font-family:var(--mono)">${escapeHtml(a.cover||'')}</td>
      <td>${escapeHtml(a.note||'')}</td>`;
    tr.onclick = () => { selectedAnimeRow = i; renderAnimeTable(); };
    tb.appendChild(tr);
  });
}

function openAnimeModal(data={}, idx=-1) {
  document.getElementById('anime-modal-title').textContent = idx>=0 ? '编辑番剧' : '添加番剧';
  document.getElementById('anime-edit-idx').value = idx;
  document.getElementById('am-title').value = data.title||'';
  document.getElementById('am-cover').value = data.cover||'';
  document.getElementById('am-ep').value = data.ep||'';
  document.getElementById('am-total').value = data.total||'';
  document.getElementById('am-note').value = data.note||'';
  document.getElementById('am-status').value = data.status||'airing';
  document.getElementById('anime-modal').classList.add('open');
}

function editAnime() {
  if (selectedAnimeRow < 0) { alert('请先选中一行'); return; }
  openAnimeModal(animeList[selectedAnimeRow], selectedAnimeRow);
}

function confirmAnime() {
  const title = document.getElementById('am-title').value.trim();
  if (!title) { alert('标题不能为空'); return; }
  const data = {
    title, cover: document.getElementById('am-cover').value.trim(),
    ep: document.getElementById('am-ep').value.trim(),
    total: document.getElementById('am-total').value.trim(),
    note: document.getElementById('am-note').value.trim(),
    status: document.getElementById('am-status').value,
  };
  const idx = parseInt(document.getElementById('anime-edit-idx').value);
  if (idx >= 0) animeList[idx] = data; else animeList.push(data);
  renderAnimeTable();
  closeModal('anime-modal');
}

function deleteAnime() {
  if (selectedAnimeRow < 0) { alert('请先选中一行'); return; }
  if (!confirm(`删除「${animeList[selectedAnimeRow].title}」？`)) return;
  animeList.splice(selectedAnimeRow, 1);
  selectedAnimeRow = -1;
  renderAnimeTable();
}

function moveAnime(delta) {
  const n = selectedAnimeRow + delta;
  if (n < 0 || n >= animeList.length) return;
  [animeList[selectedAnimeRow], animeList[n]] = [animeList[n], animeList[selectedAnimeRow]];
  selectedAnimeRow = n;
  renderAnimeTable();
}

async function saveAnimeList() {
  await fetch('/api/anime', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(animeList)});
  setStatus('[OK] 追番配置已保存'); alert('追番列表已保存！重新构建后生效。');
}

// ─── Friends ───
async function loadFriends() {
  const res = await fetch('/api/friends');
  friendList = await res.json();
  renderFriendTable();
}

function renderFriendTable() {
  const tb = document.getElementById('friend-tbody');
  tb.innerHTML = '';
  friendList.forEach((f, i) => {
    const tr = document.createElement('tr');
    if (i === selectedFriendRow) tr.classList.add('selected');
    tr.innerHTML = `<td>${escapeHtml(f.name||'')}</td>
      <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;font-family:var(--mono)">${escapeHtml(f.url||'')}</td>
      <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;font-family:var(--mono)">${escapeHtml(f.avatar||'')}</td>
      <td>${escapeHtml(f.desc||'')}</td>`;
    tr.onclick = () => { selectedFriendRow = i; renderFriendTable(); };
    tb.appendChild(tr);
  });
}

function openFriendModal(data={}, idx=-1) {
  document.getElementById('friend-modal-title').textContent = idx>=0 ? '编辑友链' : '添加友链';
  document.getElementById('friend-edit-idx').value = idx;
  document.getElementById('fm-name').value = data.name||'';
  document.getElementById('fm-url').value = data.url||'';
  document.getElementById('fm-avatar').value = data.avatar||'';
  document.getElementById('fm-desc').value = data.desc||'';
  document.getElementById('friend-modal').classList.add('open');
}

function editFriend() {
  if (selectedFriendRow < 0) { alert('请先选中一行'); return; }
  openFriendModal(friendList[selectedFriendRow], selectedFriendRow);
}

function confirmFriend() {
  const name = document.getElementById('fm-name').value.trim();
  const url = document.getElementById('fm-url').value.trim();
  if (!name || !url) { alert('名称和链接不能为空'); return; }
  const data = {
    name, url,
    avatar: document.getElementById('fm-avatar').value.trim(),
    desc: document.getElementById('fm-desc').value.trim(),
  };
  const idx = parseInt(document.getElementById('friend-edit-idx').value);
  if (idx >= 0) friendList[idx] = data; else friendList.push(data);
  renderFriendTable();
  closeModal('friend-modal');
}

function deleteFriend() {
  if (selectedFriendRow < 0) { alert('请先选中一行'); return; }
  if (!confirm(`删除「${friendList[selectedFriendRow].name}」？`)) return;
  friendList.splice(selectedFriendRow, 1);
  selectedFriendRow = -1;
  renderFriendTable();
}

function moveFriend(delta) {
  const n = selectedFriendRow + delta;
  if (n < 0 || n >= friendList.length) return;
  [friendList[selectedFriendRow], friendList[n]] = [friendList[n], friendList[selectedFriendRow]];
  selectedFriendRow = n;
  renderFriendTable();
}

async function saveFriendList() {
  await fetch('/api/friends', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(friendList)});
  setStatus('[OK] 友链配置已保存'); alert('友链已保存！重新构建后生效。');
}

// ─── Contact Methods ───
async function loadContacts() {
  const res = await fetch('/api/contact_methods');
  contactList = await res.json();
  renderContactTable();
}

function renderContactTable() {
  const tb = document.getElementById('contact-tbody');
  tb.innerHTML = '';
  contactList.forEach((c, i) => {
    const tr = document.createElement('tr');
    if (i === selectedContactRow) tr.classList.add('selected');
    tr.innerHTML = `<td>${escapeHtml(c.label || '')}</td>
      <td><code style="font-size:.74rem;color:var(--accent3)">${escapeHtml(c.icon || '')}</code></td>
      <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;font-family:var(--mono)">${escapeHtml(c.url || '')}</td>`;
    tr.onclick = () => { selectedContactRow = i; renderContactTable(); };
    tb.appendChild(tr);
  });
}

function openContactModal(data=null, idx=-1) {
  document.getElementById('contact-modal-title').textContent = (idx >= 0) ? '编辑联系方式' : '添加联系方式';
  document.getElementById('cm-label').value = data ? data.label || '' : '';
  document.getElementById('cm-icon').value = data ? data.icon || '' : '';
  document.getElementById('cm-url').value = data ? data.url || '' : '';
  document.getElementById('contact-modal').dataset.editIdx = (idx >= 0) ? idx : -1;
  document.getElementById('contact-modal').classList.add('open');
}

function editContact() {
  if (selectedContactRow < 0) { alert('请先选中一行'); return; }
  openContactModal(contactList[selectedContactRow], selectedContactRow);
}

function confirmContact() {
  const idx = parseInt(document.getElementById('contact-modal').dataset.editIdx, 10);
  const data = {
    label: document.getElementById('cm-label').value.trim(),
    icon: document.getElementById('cm-icon').value.trim() || 'fas fa-link',
    url: document.getElementById('cm-url').value.trim(),
  };
  if (!data.label || !data.url) { alert('名称和链接不能为空'); return; }
  if (idx >= 0) contactList[idx] = data; else contactList.push(data);
  closeModal('contact-modal');
  renderContactTable();
}

function deleteContact() {
  if (selectedContactRow < 0) { alert('请先选中一行'); return; }
  if (!confirm(`删除「${contactList[selectedContactRow].label}」？`)) return;
  contactList.splice(selectedContactRow, 1);
  selectedContactRow = -1;
  renderContactTable();
}

function moveContact(delta) {
  const n = selectedContactRow + delta;
  if (selectedContactRow < 0 || n < 0 || n >= contactList.length) return;
  [contactList[selectedContactRow], contactList[n]] = [contactList[n], contactList[selectedContactRow]];
  selectedContactRow = n;
  renderContactTable();
}

async function saveContactList() {
  await fetch('/api/contact_methods', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(contactList)});
  setStatus('[OK] 联系方式已保存'); alert('联系方式已保存！重新构建后生效。');
}

// ─── Audio ───
const LYRICS_SOURCE_LABEL = {embedded: '内嵌歌词', lrc: '.lrc 文件', none: '无'};

async function loadAudio() {
  const res = await fetch('/api/audio');
  audioList = await res.json();
  renderAudioTable();
}

function renderAudioTable() {
  const tb = document.getElementById('audio-tbody');
  tb.innerHTML = '';
  if (!audioList.length) {
    tb.innerHTML = '<tr><td colspan="5" style="color:var(--dim);text-align:center;padding:24px">音频库为空，点击「上传音频」添加</td></tr>';
    return;
  }
  audioList.forEach((a, i) => {
    const tr = document.createElement('tr');
    if (i === selectedAudioRow) tr.classList.add('selected');
    const durStr = a.duration ? `${Math.floor(a.duration/60)}:${String(Math.floor(a.duration%60)).padStart(2,'0')}` : '--:--';
    const lrcBadge = a.lyrics_source === 'none'
      ? `<span style="opacity:.45;font-size:.72rem">无歌词</span>`
      : `<span style="color:var(--green);font-size:.72rem;padding:2px 6px;border-radius:4px;background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.2)">${LYRICS_SOURCE_LABEL[a.lyrics_source]||a.lyrics_source}</span>`;
    tr.innerHTML = `<td><strong>${escapeHtml(a.title||'')}</strong></td>
      <td>${escapeHtml(a.artist||'')}</td>
      <td><span style="font-size:.72rem;font-family:var(--mono);color:var(--dim)">${escapeHtml((a.format||'').toUpperCase())}</span></td>
      <td style="font-family:var(--mono);color:var(--dim)">${durStr}</td>
      <td>${lrcBadge}</td>`;
    tr.onclick = () => { selectedAudioRow = i; renderAudioTable(); };
    tb.appendChild(tr);
  });
}

document.getElementById('audio-upload').onchange = async (e) => {
  const files = e.target.files;
  if (!files || !files.length) return;
  const fd = new FormData();
  for (let f of files) fd.append('files', f);
  setStatus('[...] 正在上传并解析音频...');
  try {
    const res = await fetch('/api/audio/upload', {method:'POST', body:fd});
    const d = await res.json();
    if (d.ok) {
      setStatus(`[OK] 成功上传 ${d.added.length} 首` + (d.errors.length ? `，${d.errors.length} 个失败` : ''));
      loadAudio();
    } else {
      alert('上传失败: ' + d.error);
      setStatus('[ERR] 上传失败', false);
    }
  } catch(err) {
    alert('网络或服务端异常: ' + err);
    setStatus('[ERR] 上传异常', false);
  }
  e.target.value = '';
};

function editAudioTrack() {
  if (selectedAudioRow < 0) { alert('请先选中一行音频'); return; }
  const a = audioList[selectedAudioRow];
  document.getElementById('am2-id').value = a.id;
  document.getElementById('am2-title').value = a.title || '';
  document.getElementById('am2-artist').value = a.artist || '';
  document.getElementById('am2-lyrics-source').value = LYRICS_SOURCE_LABEL[a.lyrics_source] || a.lyrics_source;
  const hints = [];
  hints.push(a.has_embedded_lyrics ? '[OK] 检测到内嵌歌词' : '[-] 无内嵌歌词');
  hints.push(a.has_lrc ? '[OK] 已上传 lrc' : '[-] 未上传 lrc');
  document.getElementById('am2-lyrics-hint').textContent = hints.join(' · ');
  document.getElementById('audio-modal').classList.add('open');
}

async function confirmAudioTrack() {
  const id = document.getElementById('am2-id').value;
  const title = document.getElementById('am2-title').value.trim();
  const artist = document.getElementById('am2-artist').value.trim();
  if (!title) { alert('曲目标题不能为空'); return; }
  const res = await fetch('/api/audio/update', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id, title, artist})
  });
  const d = await res.json();
  if (d.ok) { setStatus('[OK] 音轨信息已更新'); loadAudio(); }
  else alert('更新失败: ' + d.error);
  closeModal('audio-modal');
}

function deleteAudioTrack() {
  if (selectedAudioRow < 0) { alert('请先选中一行音频'); return; }
  const a = audioList[selectedAudioRow];
  if (!confirm(`确认删除歌曲「${a.title}」？相关音频文件与歌词都将被移除。`)) return;
  fetch('/api/audio/delete', {
    method:'DELETE', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id: a.id})
  }).then(r => r.json())
    .then(() => { setStatus('[OK] 音轨已删除'); loadAudio(); });
}

function moveAudio(delta) {
  const n = selectedAudioRow + delta;
  if (selectedAudioRow < 0 || n < 0 || n >= audioList.length) return;
  [audioList[selectedAudioRow], audioList[n]] = [audioList[n], audioList[selectedAudioRow]];
  selectedAudioRow = n;
  renderAudioTable();
}

async function saveAudioOrder() {
  const ids = audioList.map(a => a.id);
  const res = await fetch('/api/audio/order', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({order: ids})
  });
  const d = await res.json();
  if (d.ok) setStatus('[OK] 播放顺序已保存');
  else alert('保存顺序失败: ' + d.error);
}

document.getElementById('lrc-upload').onchange = async (e) => {
  if (selectedAudioRow < 0) { alert('请先在表格中选中要关联歌词的音轨'); e.target.value = ''; return; }
  const file = e.target.files[0];
  if (!file) return;
  const a = audioList[selectedAudioRow];
  const fd = new FormData();
  fd.append('id', a.id);
  fd.append('file', file);
  const res = await fetch('/api/audio/upload_lrc', {method:'POST', body:fd});
  const d = await res.json();
  if (d.ok) { setStatus('[OK] lrc 歌词已上传'); loadAudio(); }
  else alert('上传歌词失败: ' + d.error);
  e.target.value = '';
};

async function removeAudioLrc() {
  if (selectedAudioRow < 0) { alert('请先选中一行音频'); return; }
  const a = audioList[selectedAudioRow];
  if (!a.has_lrc) { alert('该曲目当前没有关联的手动上传 lrc 文件'); return; }
  if (!confirm(`移除「${a.title}」的手动上传 lrc 歌词？（如果曲目有内嵌歌词将自动回退到内嵌）`)) return;
  const res = await fetch('/api/audio/remove_lrc', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id: a.id})
  });
  const d = await res.json();
  if (d.ok) { setStatus('[OK] lrc 已移除'); loadAudio(); }
  else alert('移除失败: ' + d.error);
}

// ─── Backup ───
async function loadBackups() {
  const res = await fetch('/api/backup/list');
  const backups = await res.json();
  const tb = document.getElementById('backup-tbody');
  tb.innerHTML = '';
  if (!backups.length) {
    tb.innerHTML = '<tr><td colspan="4" style="color:var(--dim);text-align:center;padding:20px">暂无备份</td></tr>';
    return;
  }
  backups.forEach(b => {
    const tr = document.createElement('tr');
    const nameTd = document.createElement('td');
    nameTd.style.fontFamily = 'var(--mono)';
    nameTd.textContent = b.name;
    const sizeTd = document.createElement('td');
    sizeTd.style.fontFamily = 'var(--mono)';
    sizeTd.textContent = b.size_h;
    const mtimeTd = document.createElement('td');
    mtimeTd.style.fontFamily = 'var(--mono)';
    mtimeTd.textContent = b.mtime;
    const actTd = document.createElement('td');
    actTd.style.display = 'flex';
    actTd.style.gap = '6px';

    const btnDownload = document.createElement('button');
    btnDownload.className = 'btn blue';
    btnDownload.style.cssText = 'padding:3px 8px;font-size:.72rem';
    btnDownload.textContent = '下载';
    btnDownload.onclick = () => downloadBackup(b.name);

    const btnUpload = document.createElement('button');
    btnUpload.className = 'btn orange';
    btnUpload.style.cssText = 'padding:3px 8px;font-size:.72rem';
    btnUpload.textContent = '上传云端';
    btnUpload.onclick = () => uploadBackupToCloud(b.name);

    const btnRestore = document.createElement('button');
    btnRestore.className = 'btn green';
    btnRestore.style.cssText = 'padding:3px 8px;font-size:.72rem';
    btnRestore.textContent = '恢复';
    btnRestore.onclick = () => restoreBackup(b.name);

    const btnDelete = document.createElement('button');
    btnDelete.className = 'btn red';
    btnDelete.style.cssText = 'padding:3px 8px;font-size:.72rem';
    btnDelete.textContent = '删除';
    btnDelete.onclick = () => deleteBackupLocal(b.name);

    actTd.appendChild(btnDownload);
    actTd.appendChild(btnUpload);
    actTd.appendChild(btnRestore);
    actTd.appendChild(btnDelete);

    tr.appendChild(nameTd);
    tr.appendChild(sizeTd);
    tr.appendChild(mtimeTd);
    tr.appendChild(actTd);
    tb.appendChild(tr);
  });
}

async function createBackup() {
  setStatus('[...] 正在创建备份...');
  const res = await fetch('/api/backup/create', {method: 'POST'});
  const d = await res.json();
  if (d.ok) {
    let msg = '[OK] 备份创建成功: ' + d.backup.name + ' (' + d.backup.size_h + ')';
    setStatus(msg);
    loadBackups();
  } else {
    alert('创建备份失败: ' + d.error); setStatus('[ERR] 备份创建失败', false);
  }
}

function downloadBackup(name) {
  window.location.href = '/api/backup/download/' + encodeURIComponent(name);
}

async function deleteBackupLocal(name) {
  if (!confirm(`删除本地备份 ${name}？此操作不可撤销。`)) return;
  const res = await fetch('/api/backup/delete', {method:'DELETE', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
  const d = await res.json();
  if (d.ok) { setStatus('[OK] 备份已删除'); loadBackups(); }
  else alert('删除失败: ' + d.error);
}

async function restoreBackup(name) {
  if (!confirm(`确定要从「${name}」恢复吗？\n\n这将覆盖当前的 config.json / posts / pages 内容！\n（系统会先自动为当前状态创建一份"恢复前快照"，如有需要可再次恢复回去）`)) return;
  setStatus('[...] 正在恢复...');
  const res = await fetch('/api/backup/restore', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
  const d = await res.json();
  if (d.ok) {
    setStatus('[OK] 恢复完成，请重新构建');
    alert('恢复完成！建议在「内容」Tab 检查，或点击「构建」生成静态页面。');
    loadBackups();
  } else {
    alert('恢复失败: ' + d.error); setStatus('[ERR] 恢复失败', false);
  }
}

document.getElementById('restore-upload').onchange = async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  if (!confirm(`确定要上传并恢复「${file.name}」吗？\n\n这将覆盖当前的 config.json / posts / pages 内容！`)) {
    e.target.value = ''; return;
  }
  const fd = new FormData();
  fd.append('file', file);
  setStatus('[...] 正在上传并恢复...');
  const res = await fetch('/api/backup/upload_restore', {method:'POST', body:fd});
  const d = await res.json();
  if (d.ok) {
    setStatus('[OK] 恢复完成，请重新构建');
    alert('恢复完成！');
    loadBackups();
  } else {
    alert('上传恢复失败: ' + d.error); setStatus('[ERR] 恢复失败', false);
  }
  e.target.value = '';
};

// ─── Cloud Backup ───
async function testCloudConnection() {
  const el = document.getElementById('cloud-status');
  el.textContent = '[...] 测试中...';
  el.style.color = 'var(--dim)';
  const res = await fetch('/api/backup/cloud/test', {method:'POST'});
  const d = await res.json();
  if (d.ok) { el.textContent = '[OK] 云端连接正常：' + (d.server || ''); el.style.color = 'var(--green)'; }
  else { el.textContent = '[ERR] 连接失败: ' + d.error; el.style.color = 'var(--red)'; }
}

async function loadCloudBackups() {
  const res = await fetch('/api/backup/cloud/list');
  const d = await res.json();
  const tb = document.getElementById('cloud-backup-tbody');
  tb.innerHTML = '';
  if (!d.ok) {
    tb.innerHTML = `<tr><td colspan="4" style="color:var(--red);text-align:center;padding:16px">获取云端列表失败: ${escapeHtml(d.error||'')}</td></tr>`;
    return;
  }
  const list = d.backups || [];
  if (!list.length) {
    tb.innerHTML = '<tr><td colspan="4" style="color:var(--dim);text-align:center;padding:16px">云端暂无备份</td></tr>';
    return;
  }
  list.forEach(b => {
    const tr = document.createElement('tr');
    const nameTd = document.createElement('td');
    nameTd.style.fontFamily = 'var(--mono)';
    nameTd.textContent = b.name;
    const sizeTd = document.createElement('td');
    sizeTd.style.fontFamily = 'var(--mono)';
    sizeTd.textContent = b.size_h||b.size;
    const mtimeTd = document.createElement('td');
    mtimeTd.style.fontFamily = 'var(--mono)';
    mtimeTd.textContent = b.mtime||'';
    const actTd = document.createElement('td');
    actTd.style.display = 'flex';
    actTd.style.gap = '6px';

    const btnPull = document.createElement('button');
    btnPull.className = 'btn blue';
    btnPull.style.cssText = 'padding:3px 8px;font-size:.72rem';
    btnPull.textContent = '拉取到本地';
    btnPull.onclick = () => pullFromCloud(b.name);

    const btnDelete = document.createElement('button');
    btnDelete.className = 'btn red';
    btnDelete.style.cssText = 'padding:3px 8px;font-size:.72rem';
    btnDelete.textContent = '删除';
    btnDelete.onclick = () => deleteCloudBackup(b.name);

    actTd.appendChild(btnPull);
    actTd.appendChild(btnDelete);

    tr.appendChild(nameTd);
    tr.appendChild(sizeTd);
    tr.appendChild(mtimeTd);
    tr.appendChild(actTd);
    tb.appendChild(tr);
  });
}

async function uploadBackupToCloud(name) {
  if (!confirm(`将本地备份「${name}」上传到云端？`)) return;
  setStatus('[...] 上传云端中...');
  const res = await fetch('/api/backup/cloud/upload', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
  const d = await res.json();
  if (d.ok) { setStatus('[OK] 已上传到云端'); }
  else { alert('上传失败: ' + d.error); setStatus('[ERR] 上传失败', false); }
}

async function pullFromCloud(name) {
  setStatus('[...] 正在从云端拉取...');
  const res = await fetch('/api/backup/cloud/pull', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
  const d = await res.json();
  if (d.ok) { setStatus('[OK] 已拉取到本地备份列表'); loadBackups(); }
  else { alert('拉取失败: ' + d.error); setStatus('[ERR] 拉取失败', false); }
}

async function deleteCloudBackup(name) {
  if (!confirm(`删除云端备份「${name}」？此操作不可撤销。`)) return;
  const res = await fetch('/api/backup/cloud/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
  const d = await res.json();
  if (d.ok) { setStatus('[OK] 云端备份已删除'); loadCloudBackups(); }
  else alert('删除失败: ' + d.error);
}

// ─── Modal ───
function closeModal(id) {
  document.getElementById(id).classList.remove('open');
}

document.querySelectorAll('.modal-overlay').forEach(ov => {
  ov.onclick = (e) => { if (e.target === ov) ov.classList.remove('open'); };
});

window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-overlay.open').forEach(ov => ov.classList.remove('open'));
  }
});

// ─── Log Stream (SSE) ───
function startLog() {
  if (logSource) return;
  logSource = new EventSource('/api/log_stream');
  const box = document.getElementById('log-box');
  logSource.onmessage = (e) => {
    if (e.data === ':ping') return;
    const line = document.createElement('div');
    const msg = e.data;
    if (msg.includes('[ERR]') || msg.includes('FAIL') || msg.includes('错误')) line.className='log-err';
    else if (msg.includes('[OK]') || msg.includes('完成') || msg.includes('成功')) line.className='log-ok';
    line.textContent = msg;
    box.appendChild(line);
    box.scrollTop = box.scrollHeight;
  };
}

function clearLog() {
  document.getElementById('log-box').innerHTML='';
}

// 默认载入
loadFileList();
initThemeColorPicker();
</script>
</body>
</html>"""


# ─────────────────────────── Routes ───────────────────────────
@app.route('/')
def index():
    return render_template_string(_HTML)


@app.route('/api/files')
def api_files():
    try:
        lang = _safe_path_component(request.args.get('lang', 'zh'))
        mode = _safe_path_component(request.args.get('mode', 'posts'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    path = os.path.join(BASE_DIR, 'content', mode, lang)
    result = []
    if os.path.exists(path):
        for fn in sorted(os.listdir(path), reverse=True):
            if not fn.endswith('.md'):
                continue
            try:
                p = frontmatter.load(os.path.join(path, fn))
                title = p.get('title', '') or fn.replace('.md', '')
                result.append({
                    'name': fn,
                    'title': title,
                    'date': str(p.get('date', '')),
                    'uid': str(p.get('uid', ''))
                })
            except Exception:
                result.append({'name': fn, 'title': fn.replace('.md', ''), 'date': '', 'uid': ''})
    if result:
        result.sort(key=lambda x: (str(x.get('date', '')), str(x.get('name', ''))), reverse=True)
    return jsonify(result)


@app.route('/api/file', methods=['GET', 'POST', 'DELETE'])
def api_file():
    if request.method == 'GET':
        try:
            lang = _safe_path_component(request.args.get('lang', 'zh'))
            mode = _safe_path_component(request.args.get('mode', 'posts'))
            name = _safe_filename_for_path(request.args.get('name', ''))
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        fp = os.path.join(BASE_DIR, 'content', mode, lang, name)
        if not os.path.exists(fp):
            return jsonify({'error': 'not found'}), 404
        try:
            p = frontmatter.load(fp)
            content_body = p.content if p.content is not None else ''
            tags = p.get('tags', [])
            if isinstance(tags, list):
                tags_list = [str(x).strip() for x in tags if str(x).strip()]
                tags_str = ', '.join(tags_list)
            elif isinstance(tags, str):
                tags_list = [x.strip() for x in tags.split(',') if x.strip()]
                tags_str = tags
            else:
                tags_list = []
                tags_str = ''
            return jsonify({
                'title': p.get('title', '') or name.replace('.md', ''),
                'uid': str(p.get('uid', '')),
                'date': str(p.get('date', '')),
                'category': p.get('category', ''),
                'tags': tags_list,
                'tags_str': tags_str,
                'cover': p.get('cover', ''),
                'description': p.get('description', ''),
                'content': content_body,
                'body': content_body
            })
        except Exception as e:
            try:
                with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                    raw_text = f.read()
                return jsonify({
                    'title': name.replace('.md', ''),
                    'uid': '',
                    'date': '',
                    'category': '',
                    'tags': [],
                    'tags_str': '',
                    'cover': '',
                    'description': '',
                    'content': raw_text,
                    'body': raw_text
                })
            except Exception as e2:
                return jsonify({'error': str(e2)}), 500

    if request.method == 'DELETE':
        data = request.get_json(silent=True) or {}
        lang_raw = request.args.get('lang') or data.get('lang') or 'zh'
        mode_raw = request.args.get('mode') or data.get('mode') or 'posts'
        name_raw = request.args.get('name') or data.get('name') or ''
        try:
            lang = _safe_path_component(lang_raw)
            mode = _safe_path_component(mode_raw)
            name = _safe_filename_for_path(name_raw)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        fp = os.path.join(BASE_DIR, 'content', mode, lang, name)
        if os.path.exists(fp):
            os.remove(fp)
            _broadcast_log(f"DELETE: {name}")
        return jsonify({'ok': True})

    # POST — save
    data = request.get_json(silent=True) or {}
    try:
        lang = _safe_path_component(data.get('lang', 'zh'))
        mode = _safe_path_component(data.get('mode', 'posts'))
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    title = str(data.get('title', '')).strip()
    date = str(data.get('date', '')).strip() or datetime.now().strftime('%Y-%m-%d')
    category = str(data.get('category', '')).strip()
    cover = str(data.get('cover', '')).strip()
    description = str(data.get('description', '')).strip()

    tags_raw = data.get('tags', '')
    if isinstance(tags_raw, list):
        tags = [str(x).strip() for x in tags_raw if str(x).strip()]
    elif isinstance(tags_raw, str):
        tags = [x.strip() for x in tags_raw.split(',') if x.strip()]
    else:
        tags = []

    content = data.get('body')
    if content is None:
        content = data.get('content', '')
    if not isinstance(content, str):
        content = str(content)

    name = data.get('name') or data.get('original_name') or ''
    if not name:
        name = f"{int(time.time())}.md"
    else:
        name = os.path.basename(str(name).strip())
        if not name.endswith('.md'):
            name = f"{name}.md"

    # 文件名安全校验：只允许 .md 结尾，且不含路径穿越字符
    if not name.endswith('.md') or '..' in name or '/' in name or '\\' in name:
        return jsonify({'ok': False, 'error': '非法的文件名（仅允许 .md 结尾的纯文件名）'}), 400

    dir_path = os.path.join(BASE_DIR, 'content', mode, lang)
    os.makedirs(dir_path, exist_ok=True)
    fp = os.path.join(dir_path, name)

    uid = str(int(time.time())) + str(int(uuid.uuid4().int % 100))
    if os.path.exists(fp):
        try:
            old = frontmatter.load(fp)
            uid = str(old.get('uid', uid))
            if not data.get('date'):
                date = str(old.get('date', date))
        except Exception:
            pass

    meta = {'title': title, 'uid': uid, 'date': date}
    if category:
        meta['category'] = category
    if cover:
        meta['cover'] = cover
    if description:
        meta['description'] = description
    if tags:
        meta['tags'] = tags
    post = frontmatter.Post(content, **meta)
    with open(fp, 'w', encoding='utf-8') as f:
        frontmatter.dump(post, f)

    _broadcast_log(f"SAVE: {name}  UID={uid}")
    return jsonify({'ok': True, 'name': name, 'uid': uid})


@app.route('/attachments/<path:name>')
@app.route('/assets/<path:name>')
def serve_attachment(name):
    # 路径穿越防护：检查规范化路径是否仍然在 attachments 目录下
    ap = os.path.join(BASE_DIR, 'content', 'attachments')
    raw_name = unquote(name)
    safe_name = os.path.normpath(raw_name).lstrip(os.sep)
    if '..' in safe_name.split(os.sep):
        return ('', 404)
    target = os.path.normpath(os.path.join(ap, safe_name))
    if not target.startswith(os.path.normpath(ap) + os.sep) and target != os.path.normpath(ap):
        return ('', 404)
    if not os.path.isfile(target):
        pub_target = os.path.normpath(os.path.join(BASE_DIR, 'public', 'attachments', safe_name))
        if os.path.isfile(pub_target):
            return send_from_directory(os.path.join(BASE_DIR, 'public', 'attachments'), safe_name)
        return ('', 404)
    return send_from_directory(ap, safe_name)


@app.route('/api/assets')
def api_assets():
    ap = os.path.join(BASE_DIR, 'content', 'attachments')
    result = []
    if os.path.exists(ap):
        for fn in sorted(os.listdir(ap)):
            fp = os.path.join(ap, fn)
            if os.path.isfile(fp):
                size = os.path.getsize(fp)
                size_str = f"{size/1024:.1f} KiB" if size < 1024*1024 else f"{size/1024/1024:.1f} MiB"
                result.append({'name': fn, 'size': size_str})
    return jsonify(result)


@app.route('/api/asset', methods=['DELETE'])
def api_asset_delete():
    data = request.get_json(silent=True) or {}
    name = request.args.get('name') or data.get('name') or ''
    safe_name = os.path.basename(str(name).strip())
    if not safe_name or '..' in safe_name or '/' in safe_name or '\\' in safe_name:
        return jsonify({'ok': False, 'error': '非法的文件名'}), 400
    fp = os.path.join(BASE_DIR, 'content', 'attachments', safe_name)
    if os.path.exists(fp):
        os.remove(fp)
    _broadcast_log(f"ASSET DELETE: {safe_name}")
    return jsonify({'ok': True})


@app.route('/api/upload', methods=['POST'])
@app.route('/api/upload_asset', methods=['POST'])
def api_upload():
    files = request.files.getlist('files')
    ap = os.path.join(BASE_DIR, 'content', 'attachments')
    os.makedirs(ap, exist_ok=True)
    # 素材库只允许常见图片/文档/压缩包类型，防止上传可执行文件
    ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif', '.svg',
                          '.ico', '.bmp', '.tiff', '.pdf', '.zip', '.tar', '.gz',
                          '.mp4', '.webm', '.ogg', '.mp3', '.wav', '.flac',
                          '.txt', '.md', '.csv', '.json', '.xml', '.yml', '.yaml',
                          '.css', '.js', '.ttf', '.woff', '.woff2', '.otf'}
    names = []
    for f in files:
        fn = os.path.basename(f.filename)
        if not fn:
            continue
        # 路径穿越防护
        if '..' in fn or '/' in fn or '\\' in fn:
            continue
        ext = os.path.splitext(fn)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            _broadcast_log(f"[WARN] 拒绝上传（不安全的文件类型）: {fn}")
            continue
        # 文件名只保留安全字符
        safe_fn = re.sub(r'[^\w.\-() \u4e00-\u9fff]', '_', fn)
        f.save(os.path.join(ap, safe_fn))
        names.append(safe_fn)
        _broadcast_log(f"UPLOAD: {safe_fn} → content/attachments/")
    return jsonify({'ok': True, 'names': names})


@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'GET':
        cfg = _load_config()
        _ensure_indexnow_key(cfg)
        return jsonify(cfg)
    data = request.get_json(silent=True) or {}
    cfg = _load_config()
    for k, v in data.items():
        # indexnow_key 不接受前端写入（只读展示，重新生成走专门的接口），
        # 避免手滑改成非法值导致密钥校验文件和实际提交的 key 对不上
        if k == 'indexnow_key':
            continue
        cfg[k] = v
    _save_config(cfg)
    _broadcast_log("CONFIG SAVED")
    return jsonify({'ok': True})


@app.route('/api/indexnow/regenerate', methods=['POST'])
@app.route('/api/indexnow/regen_key', methods=['POST'])
def api_indexnow_regenerate():
    cfg = _load_config()
    key = uuid.uuid4().hex
    cfg['indexnow_key'] = key
    _save_config(cfg)
    _broadcast_log("INDEXNOW: 密钥已重新生成")
    return jsonify({'ok': True, 'key': key})


@app.route('/api/sw_status')
def api_sw_status():
    return jsonify({'exists': os.path.exists(os.path.join(BASE_DIR, 'content', 'sw.js'))})


@app.route('/api/upload_sw', methods=['POST'])
def api_upload_sw():
    f = request.files.get('file')
    if not f:
        return jsonify({'ok': False, 'error': 'no file'})
    dest = os.path.join(BASE_DIR, 'content', 'sw.js')
    f.save(dest)
    _broadcast_log(f"SW.JS UPLOAD → {dest}")
    return jsonify({'ok': True})


@app.route('/api/sw', methods=['DELETE'])
@app.route('/api/delete_sw', methods=['DELETE', 'POST'])
def api_sw_delete():
    dest = os.path.join(BASE_DIR, 'content', 'sw.js')
    if os.path.exists(dest):
        os.remove(dest)
    _broadcast_log("SW.JS DELETED")
    return jsonify({'ok': True})


@app.route('/api/anime', methods=['GET', 'POST'])
def api_anime():
    if request.method == 'GET':
        cfg = _load_config()
        return jsonify(_get_list(cfg, 'anime_list'))
    cfg = _load_config()
    data = request.get_json(silent=True)
    if data is not None and isinstance(data, list):
        cfg['anime_list'] = data
        _save_config(cfg)
        _broadcast_log("ANIME LIST SAVED")
    return jsonify({'ok': True})


@app.route('/api/friends', methods=['GET', 'POST'])
def api_friends():
    if request.method == 'GET':
        cfg = _load_config()
        return jsonify(_get_list(cfg, 'friend_links'))
    cfg = _load_config()
    data = request.get_json(silent=True)
    if data is not None and isinstance(data, list):
        cfg['friend_links'] = data
        _save_config(cfg)
        _broadcast_log("FRIEND LINKS SAVED")
    return jsonify({'ok': True})


@app.route('/api/contact_methods', methods=['GET', 'POST'])
def api_contact_methods():
    if request.method == 'GET':
        cfg = _load_config()
        return jsonify(_get_list(cfg, 'contact_methods'))
    cfg = _load_config()
    data = request.get_json(silent=True)
    if data is not None and isinstance(data, list):
        cfg['contact_methods'] = data
        _save_config(cfg)
        _broadcast_log("CONTACT METHODS SAVED")
    return jsonify({'ok': True})


def _am_or_error():
    if _am is None:
        return jsonify({'ok': False, 'error': '未找到 audio_manager.py 模块'}), 500
    return None


@app.route('/api/audio/list')
@app.route('/api/audio', methods=['GET'])
def api_audio_list():
    err = _am_or_error()
    if err:
        return err
    return jsonify(_am.list_tracks(BASE_DIR))


@app.route('/api/audio/upload', methods=['POST'])
def api_audio_upload():
    err = _am_or_error()
    if err:
        return err
    files = request.files.getlist('files')
    if not files:
        return jsonify({'ok': False, 'error': '未选择文件'})
    added = []
    errors = []
    for f in files:
        try:
            track = _am.add_track(BASE_DIR, f.read(), f.filename)
            added.append(track)
            _broadcast_log(f"[INFO] 音频上传: {f.filename} → {track['title']}")
        except Exception as e:
            errors.append(f"{f.filename}: {e}")
    return jsonify({'ok': True, 'added': added, 'errors': errors})


@app.route('/api/audio/update', methods=['POST'])
def api_audio_update():
    err = _am_or_error()
    if err:
        return err
    data = request.json or {}
    tid = data.get('id')
    if not tid:
        return jsonify({'ok': False, 'error': '缺少 id'})
    try:
        track = _am.update_track(BASE_DIR, tid, data)
        _broadcast_log(f"[INFO] 音轨信息已更新: {track['title']}")
        return jsonify({'ok': True, 'track': track})
    except FileNotFoundError as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/audio', methods=['DELETE'])
@app.route('/api/audio/delete', methods=['DELETE', 'POST'])
def api_audio_delete():
    err = _am_or_error()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    tid = request.args.get('id') or data.get('id') or ''
    if not tid:
        return jsonify({'ok': False, 'error': '缺少音轨 ID'}), 400
    try:
        _am.delete_track(BASE_DIR, tid)
        _broadcast_log(f"[INFO] 音轨已删除: {tid}")
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/audio/reorder', methods=['POST'])
@app.route('/api/audio/order', methods=['POST'])
def api_audio_reorder():
    err = _am_or_error()
    if err:
        return err
    data = request.get_json(silent=True) or []
    if isinstance(data, list):
        ids = data
    elif isinstance(data, dict):
        ids = data.get('order') or data.get('ids') or []
    else:
        ids = []
    _am.reorder_tracks(BASE_DIR, ids)
    _broadcast_log("[INFO] 播放顺序已保存")
    return jsonify({'ok': True})


@app.route('/api/audio/lrc', methods=['POST'])
@app.route('/api/audio/upload_lrc', methods=['POST'])
def api_audio_lrc_upload():
    err = _am_or_error()
    if err:
        return err
    tid = request.form.get('id') or request.args.get('id') or ''
    f = request.files.get('file')
    if not tid or not f:
        return jsonify({'ok': False, 'error': '缺少 id 或文件'})
    try:
        track = _am.attach_lrc(BASE_DIR, tid, f.read())
        _broadcast_log(f"[INFO] 已为《{track['title']}》上传 lrc 歌词")
        return jsonify({'ok': True, 'track': track})
    except FileNotFoundError as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/audio/lrc', methods=['DELETE'])
@app.route('/api/audio/remove_lrc', methods=['DELETE', 'POST'])
def api_audio_lrc_delete():
    err = _am_or_error()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    tid = request.args.get('id') or data.get('id') or ''
    if not tid:
        return jsonify({'ok': False, 'error': '缺少音轨 ID'}), 400
    try:
        track = _am.remove_lrc(BASE_DIR, tid)
        _broadcast_log(f"[INFO] 已移除《{track['title']}》的 lrc 歌词")
        return jsonify({'ok': True, 'track': track})
    except FileNotFoundError as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/audio/lyrics/<track_id>')
def api_audio_lyrics(track_id):
    err = _am_or_error()
    if err:
        return err
    try:
        return jsonify(_am.get_lyrics(BASE_DIR, track_id))
    except FileNotFoundError as e:
        return jsonify({'mode': 'none', 'lines': [], 'text': '', 'error': str(e)})


@app.route('/api/audio/stream/<path:fname>')
def api_audio_stream(fname):
    err = _am_or_error()
    if err:
        return err
    # 安全：取 basename，防路径穿越
    safe_fname = os.path.basename(fname)
    if '..' in safe_fname or '/' in safe_fname or '\\' in safe_fname:
        return ('', 404)
    tid, ext = os.path.splitext(safe_fname)
    try:
        d, real_name = _am.stream_path(BASE_DIR, tid)
    except FileNotFoundError:
        return ('', 404)
    # send_from_directory 默认支持 Range 请求头（拖动进度条 / 断点续传），无需额外处理
    return send_from_directory(d, real_name, conditional=True)


@app.route('/audio-accel-sw.js')
def audio_accel_sw():
    # 面板本地预览也走一遍并行分片加速 Service Worker，跟构建产物用的是同一份
    # 源文件（themes/default/audio-accel-sw.js），只是这里直接从主题目录读出来
    # 返回，不需要真的复制一份到面板的静态目录里。
    theme_dir = os.path.join(BASE_DIR, 'themes', 'default')
    sw_path = os.path.join(theme_dir, 'audio-accel-sw.js')
    if not os.path.exists(sw_path):
        return ('', 404)
    resp = send_from_directory(theme_dir, 'audio-accel-sw.js')
    resp.headers['Content-Type'] = 'text/javascript; charset=utf-8'
    # Service Worker 脚本按规范必须允许在其请求路径的作用域内被注册；
    # 显式给个宽松一点的 Service-Worker-Allowed，面板预览路由跟真实站点的
    # 目录结构未必一致，避免作用域被浏览器判定超出脚本所在目录而注册失败
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp


def _do_build():
    _broadcast_log("[BUILD] 开始构建...")
    if _builder_mod is None:
        _broadcast_log("[ERR] 找不到 builder.py")
        return
    cfg = _load_config()
    if cfg.get('enable_indexnow', True):
        _ensure_indexnow_key(cfg)
    import io, contextlib
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            _builder_mod.build()
        for line in buf.getvalue().splitlines():
            _broadcast_log(line)
        _broadcast_log("[OK] 构建完成")
    except Exception as e:
        _broadcast_log(f"[ERR] 构建失败: {e}")


def _do_indexnow_ping(cfg: dict):
    """推送成功后，把 builder.py 在构建时缓存的完整 URL 列表批量提交给 IndexNow
    （Bing / Yandex 等支持该协议的搜索引擎会更快去抓取），失败不影响部署本身。"""
    key = str(cfg.get('indexnow_key', '')).strip()
    site_url = cfg.get('site_url', '').rstrip('/')
    cache_path = os.path.join(BASE_DIR, '.indexnow_urls.json')
    if not key or not site_url or not os.path.exists(cache_path):
        return
    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            url_list = json.load(f)
        if not url_list:
            return
        host = site_url.replace('https://', '').replace('http://', '').split('/')[0]
        body = json.dumps({
            'host': host,
            'key': key,
            'keyLocation': f"{site_url}/{key}.txt",
            'urlList': url_list,
        }).encode('utf-8')
        import urllib.request
        req = urllib.request.Request(
            'https://api.indexnow.org/indexnow',
            data=body, headers={'Content-Type': 'application/json; charset=utf-8'},
            method='POST')
        with urllib.request.urlopen(req, timeout=15) as resp:
            code = resp.status
        _broadcast_log(f"[INFO] IndexNow 提交完成（{len(url_list)} 个 URL，HTTP {code}）")
    except Exception as e:
        _broadcast_log(f"[WARN] IndexNow 提交失败（不影响本次部署）: {e}")


def _do_deploy():
    cfg = _load_config()
    repo = cfg.get('deploy_repo', '').strip()
    if not repo:
        _broadcast_log("[ERR] 请先在配置中填写 GitHub 仓库 SSH 地址")
        return
    pub = os.path.join(BASE_DIR, 'public')
    if not os.path.exists(pub):
        _broadcast_log("[ERR] public/ 目录不存在，请先构建")
        return
    gd = os.path.join(pub, '.git')
    if os.path.exists(gd):
        shutil.rmtree(gd, onerror=lambda f, p, _: (os.chmod(p, stat.S_IWRITE), f(p)))
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    # git 用户名/邮箱仅允许安全字符（字母、数字、空格、@、.、-、_），
    # 防止配置被篡改后注入 git 配置中的非法值
    git_name = re.sub(r'[^\w\s.\-@]', '', cfg.get('git_user_name', '').strip()) or 'Blog Deploy Bot'
    git_email = re.sub(r'[^\w.\-@+]', '', cfg.get('git_user_email', '').strip()) or 'deploy@localhost'
    cmds = [
        ['git', 'init'],
        ['git', 'config', 'user.name', git_name],
        ['git', 'config', 'user.email', git_email],
        ['git', 'add', '.'],
        ['git', 'commit', '-m', f"update: {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
        ['git', 'branch', '-M', 'main'],
        ['git', 'remote', 'add', 'origin', repo],
        ['git', 'push', '-f', 'origin', 'main'],
    ]
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, cwd=pub, capture_output=True, text=True, env=env, timeout=120)
        except subprocess.TimeoutExpired:
            _broadcast_log(f"$ {' '.join(cmd)}  [FAIL timeout]")
            _broadcast_log("[ERR] 部署失败：命令执行超时（120s），请检查网络连通性")
            return
        except Exception as e:
            _broadcast_log(f"$ {' '.join(cmd)}  [FAIL exception]")
            _broadcast_log(f"[ERR] 部署失败：{type(e).__name__}: {e}")
            return
        out = (r.stdout + r.stderr).strip()
        st = '[OK]' if r.returncode == 0 else f'[FAIL rc={r.returncode}]'
        _broadcast_log(f"$ {' '.join(cmd)}  {st}" + (f"\n{out}" if out else ''))
        if r.returncode != 0:
            if not out:
                _broadcast_log(
                    "[ERR] 部署失败（命令无任何输出却返回失败，常见原因：与另一次构建/部署"
                    "并发冲突导致 git 锁文件冲突，或进程被意外中断）。请稍后重试一次。"
                )
            else:
                _broadcast_log("[ERR] 部署失败")
            return
    _broadcast_log("[OK] GitHub 推送完成")

    if cfg.get('enable_indexnow', True) and str(cfg.get('indexnow_key', '')).strip():
        _do_indexnow_ping(cfg)

    if str(cfg.get('auto_backup_on_deploy', False)).lower() in ('true', '1'):
        if _bm is None:
            _broadcast_log("[WARN] 自动备份已开启，但未找到 backup_manager.py")
        else:
            try:
                info = _bm.create_backup(BASE_DIR)
                removed = _bm.prune_backups(BASE_DIR, cfg.get('backup_retention', '10'))
                msg = f"[OK] 自动备份完成: {info['name']} ({info['size_h']})"
                if removed:
                    msg += f"，已清理 {removed} 份过期备份"
                _broadcast_log(msg)
            except Exception as e:
                _broadcast_log(f"[WARN] 自动备份失败: {e}")


def _do_purge_cf():
    cfg = _load_config()
    z = cfg.get('cf_zone_id', '').strip()
    tok = cfg.get('cf_api_token', '').strip()
    em = cfg.get('cf_email', '').strip()
    if not z or not tok:
        _broadcast_log("[ERR] 请先配置 CF Zone ID 和 API Token")
        return
    try:
        import urllib.request
        headers = {'Content-Type': 'application/json'}
        if em:
            headers['X-Auth-Email'] = em
            headers['X-Auth-Key'] = tok
        else:
            headers['Authorization'] = f'Bearer {tok}'
        import urllib.parse
        body = json.dumps({'purge_everything': True}).encode()
        req = urllib.request.Request(
            f'https://api.cloudflare.com/client/v4/zones/{z}/purge_cache',
            data=body, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read())
        if d.get('success'):
            _broadcast_log("[OK] CF 缓存清理成功")
        else:
            _broadcast_log(f"[ERR] CF 报错: {d}")
    except Exception as e:
        _broadcast_log(f"[ERR] CF 请求失败: {e}")


@app.route('/api/build', methods=['POST'])
def api_build():
    _run_async(_do_build)
    return jsonify({'ok': True})


@app.route('/api/deploy', methods=['POST'])
def api_deploy():
    _run_async(_do_deploy)
    return jsonify({'ok': True})


@app.route('/api/purge_cf', methods=['POST'])
def api_purge_cf():
    _run_async(_do_purge_cf)
    return jsonify({'ok': True})


def _bm_or_error():
    if _bm is None:
        return jsonify({'ok': False, 'error': '找不到 backup_manager.py'}), 500
    return None


@app.route('/api/backup/list')
def api_backup_list():
    if _bm is None:
        return jsonify([])
    return jsonify(_bm.list_backups(BASE_DIR))


@app.route('/api/backup/create', methods=['POST'])
def api_backup_create():
    err = _bm_or_error()
    if err:
        return err
    try:
        info = _bm.create_backup(BASE_DIR)
        cfg = _load_config()
        removed = _bm.prune_backups(BASE_DIR, cfg.get('backup_retention', '10'))
        _broadcast_log(f"[OK] 手动备份创建成功: {info['name']} ({info['size_h']})" +
                       (f"，清理了 {removed} 份过期备份" if removed else ""))
        return jsonify({'ok': True, 'backup': info})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/backup/delete', methods=['DELETE', 'POST'])
def api_backup_delete():
    err = _bm_or_error()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = request.args.get('name') or data.get('name', '')
    if not name:
        return jsonify({'ok': False, 'error': '缺少备份名称'}), 400
    try:
        _bm.delete_backup(BASE_DIR, name)
        _broadcast_log(f"[INFO] 本地备份已删除: {name}")
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/backup/download/<path:name>')
def api_backup_download(name):
    bdir = os.path.join(BASE_DIR, 'backups')
    safe_name = os.path.basename(name)
    if '..' in safe_name or '/' in safe_name or '\\' in safe_name:
        return jsonify({'ok': False, 'error': '非法的文件名'}), 400
    fp = os.path.join(bdir, safe_name)
    if not os.path.exists(fp):
        return jsonify({'ok': False, 'error': '文件不存在'}), 404
    return send_from_directory(bdir, safe_name, as_attachment=True)


@app.route('/api/backup/restore', methods=['POST'])
def api_backup_restore():
    err = _bm_or_error()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = request.args.get('name') or data.get('name', '')
    if not name:
        return jsonify({'ok': False, 'error': '缺少备份名称'}), 400
    try:
        result = _bm.restore_backup(BASE_DIR, name, auto_snapshot=True)
        _broadcast_log(f"[OK] 已从备份恢复: {name}（写入 {result['restored_files']} 个文件，"
                       f"恢复前快照: {result['snapshot']}）")
        return jsonify({'ok': True, **result})
    except Exception as e:
        _broadcast_log(f"[ERR] 恢复失败: {e}")
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/backup/restore_upload', methods=['POST'])
@app.route('/api/backup/upload_restore', methods=['POST'])
def api_backup_restore_upload():
    err = _bm_or_error()
    if err:
        return err
    f = request.files.get('file')
    if not f:
        return jsonify({'ok': False, 'error': '未收到文件'})
    try:
        result = _bm.restore_uploaded(BASE_DIR, f.read(), auto_snapshot=True)
        _broadcast_log(f"[OK] 已从上传文件恢复: {f.filename}（写入 {result['restored_files']} 个文件，"
                       f"恢复前快照: {result['snapshot']}）")
        return jsonify({'ok': True, **result})
    except Exception as e:
        _broadcast_log(f"[ERR] 恢复失败: {e}")
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/backup/cloud/ping', methods=['GET', 'POST'])
@app.route('/api/backup/cloud/test', methods=['GET', 'POST'])
def api_backup_cloud_ping():
    if _bm is None:
        return jsonify({'ok': False, 'error': '找不到 backup_manager.py'})
    cfg = _load_config()
    try:
        d = _bm.cloud_ping(cfg.get('cloud_backup_url', ''), cfg.get('cloud_backup_key', ''))
        return jsonify({'ok': True, 'server': d.get('server', 'unknown')})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/backup/cloud/list')
def api_backup_cloud_list():
    if _bm is None:
        return jsonify({'ok': False, 'error': '找不到 backup_manager.py'})
    cfg = _load_config()
    try:
        backups = _bm.cloud_list(cfg.get('cloud_backup_url', ''), cfg.get('cloud_backup_key', ''))
        return jsonify({'ok': True, 'backups': backups})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/backup/cloud/upload', methods=['POST'])
def api_backup_cloud_upload():
    if _bm is None:
        return jsonify({'ok': False, 'error': '找不到 backup_manager.py'})
    cfg = _load_config()
    data = request.get_json(silent=True) or {}
    name = request.args.get('name') or data.get('name', '')
    if not name:
        return jsonify({'ok': False, 'error': '缺少备份名称'}), 400
    try:
        _bm.cloud_upload(BASE_DIR, cfg.get('cloud_backup_url', ''), cfg.get('cloud_backup_key', ''), name)
        _broadcast_log(f"[OK] 已上传到云端: {name}")
        return jsonify({'ok': True})
    except Exception as e:
        _broadcast_log(f"[ERR] 云端上传失败: {e}")
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/backup/cloud/pull', methods=['POST'])
def api_backup_cloud_pull():
    if _bm is None:
        return jsonify({'ok': False, 'error': '找不到 backup_manager.py'})
    cfg = _load_config()
    data = request.get_json(silent=True) or {}
    name = request.args.get('name') or data.get('name', '')
    if not name:
        return jsonify({'ok': False, 'error': '缺少备份名称'}), 400
    try:
        info = _bm.cloud_pull(BASE_DIR, cfg.get('cloud_backup_url', ''), cfg.get('cloud_backup_key', ''), name)
        _broadcast_log(f"[OK] 已从云端拉取到本地: {info['name']}")
        return jsonify({'ok': True, **info})
    except Exception as e:
        _broadcast_log(f"[ERR] 云端拉取失败: {e}")
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/backup/cloud/delete', methods=['POST', 'DELETE'])
def api_backup_cloud_delete():
    if _bm is None:
        return jsonify({'ok': False, 'error': '找不到 backup_manager.py'})
    cfg = _load_config()
    data = request.get_json(silent=True) or {}
    name = request.args.get('name') or data.get('name', '')
    if not name:
        return jsonify({'ok': False, 'error': '缺少备份名称'}), 400
    try:
        _bm.cloud_delete(cfg.get('cloud_backup_url', ''), cfg.get('cloud_backup_key', ''), name)
        _broadcast_log(f"[INFO] 已删除云端备份: {name}")
        return jsonify({'ok': True})
    except Exception as e:
        _broadcast_log(f"[ERR] 云端删除失败: {e}")
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/log_stream')
def api_log_stream():
    q = queue.Queue(maxsize=200)
    with _log_lock:
        _log_queues.append(q)

    def generate():
        try:
            while True:
                try:
                    msg = q.get(timeout=20)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield "data: :ping\n\n"
        except GeneratorExit:
            with _log_lock:
                if q in _log_queues:
                    _log_queues.remove(q)

    return Response(stream_with_context(generate()),
                    mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ─────────────────────────── Entry ───────────────────────────
if __name__ == '__main__':
    _init_env()
    print("=" * 55)
    print("  INDEX // MOE_SYSTEM v8.0.0")
    print("  Flask WebUI Edition")
    print(f"  访问地址: http://0.0.0.0:32323")
    print(f"  本机访问: http://127.0.0.1:32323")
    print("=" * 55)
    app.run(host='0.0.0.0', port=32323, debug=False, threaded=True)