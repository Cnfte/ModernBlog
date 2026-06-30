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

# ─────────────────────────── App ───────────────────────────
app = Flask(__name__)
app.secret_key = os.urandom(24)

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
    "start_date": "2024-01-01",
    "bg_url": "https://example.com/example.jpg",
    "post_bg_urls": "",
    "random_img_api": "https://www.dmoe.cc/random.php",
    "og_image": "",
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
}


# ─────────────────────────── 工具函数 ───────────────────────────
def _init_env():
    for d in ["content/posts/zh", "content/posts/en",
              "content/pages/zh", "content/pages/en", "content/attachments"]:
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


def _get_list(cfg: dict, key: str) -> list:
    v = cfg.get(key, [])
    if isinstance(v, str):
        try:
            v = json.loads(v) if v.strip() else []
        except Exception:
            v = []
    return v if isinstance(v, list) else []


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
        _broadcast_log("⚠️ 已有任务正在执行，请等待完成后再试")
        return
    def _wrap():
        try:
            fn(*args, **kwargs)
        except Exception as e:
            _broadcast_log(f"❌ 异常: {type(e).__name__}: {e}")
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
<title>INDEX // MOE_SYSTEM v8.0</title>
<style>
:root{
  --bg:#07070f;--panel:#0c0c18;--border:#1e1e30;--accent:#ff99cc;
  --accent2:#ffcc99;--accent3:#99ccff;--green:#00cc66;--red:#ff4466;
  --text:#e4e4ef;--dim:#666;--input-bg:#080812;
  --grad:linear-gradient(135deg,#ff99cc,#ffcc99);
  --r:10px;--font:'JetBrains Mono','Fira Code',monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--font);background:var(--bg);color:var(--text);
  display:flex;flex-direction:column;height:100vh;font-size:13px}
a{color:var(--accent);text-decoration:none}

/* ── Nav ── */
.topbar{display:flex;align-items:center;gap:10px;padding:0 18px;
  height:52px;background:var(--panel);border-bottom:1px solid var(--border);flex-shrink:0}
.topbar .logo{font-size:1rem;font-weight:bold;
  background:var(--grad);-webkit-background-clip:text;-webkit-text-fill-color:transparent;
  background-clip:text;margin-right:8px}
.topbar .clock{margin-left:auto;color:#00ffaa;font-size:.8rem}

/* ── Tab bar ── */
.tabbar{display:flex;gap:2px;padding:0 18px;background:var(--panel);
  border-bottom:1px solid var(--border);flex-shrink:0;overflow-x:auto}
.tab{padding:10px 18px;font-size:.78rem;font-weight:bold;letter-spacing:.5px;
  color:var(--dim);cursor:pointer;border-bottom:2px solid transparent;
  white-space:nowrap;transition:color .15s,border-color .15s}
.tab:hover{color:var(--text)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}

/* ── Layout ── */
.workspace{flex:1;overflow:hidden;display:flex}
.pane{flex:1;overflow-y:auto;padding:20px 24px}

/* ── Buttons ── */
.btn{display:inline-flex;align-items:center;gap:6px;padding:7px 16px;
  border:1px solid var(--border);border-radius:var(--r);background:var(--input-bg);
  color:var(--dim);font-family:var(--font);font-size:.8rem;font-weight:bold;
  cursor:pointer;transition:.15s;white-space:nowrap}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.primary{border-color:var(--accent);color:var(--accent)}
.btn.primary:hover{background:var(--accent);color:#000}
.btn.green{border-color:var(--green);color:var(--green)}
.btn.green:hover{background:var(--green);color:#000}
.btn.red{border-color:var(--red);color:var(--red)}
.btn.red:hover{background:var(--red);color:#000}
.btn.orange{border-color:var(--accent2);color:var(--accent2)}
.btn.orange:hover{background:var(--accent2);color:#000}
.btn.blue{border-color:var(--accent3);color:var(--accent3)}
.btn.blue:hover{background:var(--accent3);color:#000}
.btn:disabled{opacity:.35;cursor:not-allowed}
.btn-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}

/* ── Inputs ── */
input,textarea,select{width:100%;padding:8px 12px;
  background:var(--input-bg);border:1px solid var(--border);border-radius:var(--r);
  color:var(--text);font-family:var(--font);font-size:.82rem;outline:none;
  transition:border-color .15s}
input:focus,textarea:focus,select:focus{border-color:#ff99cc55}
textarea{resize:vertical;min-height:80px;line-height:1.65}
label.field-label{display:block;color:var(--dim);font-size:.75rem;
  margin-bottom:5px;margin-top:12px;letter-spacing:.4px}
.field-row{display:flex;gap:10px;align-items:flex-end}
.field-row>*{flex:1}

/* ── Sections ── */
.section-title{font-size:.7rem;color:var(--accent);font-weight:bold;
  letter-spacing:1.5px;margin:22px 0 10px;padding-bottom:6px;
  border-bottom:1px solid var(--border)}
.card{background:var(--panel);border:1px solid var(--border);
  border-radius:var(--r);padding:16px 18px;margin-bottom:14px}

/* ── Split layout for content tab ── */
.split{display:flex;gap:0;height:100%;overflow:hidden}
.split-left{width:240px;flex-shrink:0;border-right:1px solid var(--border);
  padding:12px 0;overflow-y:auto;display:flex;flex-direction:column}
.split-left .sl-toolbar{padding:8px 12px;display:flex;gap:6px;flex-wrap:wrap}
.split-right{flex:1;padding:16px 20px;overflow-y:auto;min-width:0}
.file-item{padding:9px 14px;font-size:.78rem;color:var(--dim);cursor:pointer;
  border-left:2px solid transparent;transition:.12s;word-break:break-all}
.file-item:hover{background:rgba(255,153,204,.06);color:var(--text)}
.file-item.active{background:rgba(255,153,204,.1);color:var(--accent);
  border-left-color:var(--accent)}
.file-item .fi-name{font-weight:bold}
.file-item .fi-date{font-size:.65rem;color:var(--dim);margin-top:2px}

/* ── Table ── */
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{background:var(--panel);color:var(--dim);font-size:.7rem;letter-spacing:.5px;
  padding:8px 10px;text-align:left;border-bottom:1px solid var(--border)}
td{padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.04);
  vertical-align:middle;word-break:break-all}
tr:hover td{background:rgba(255,153,204,.04)}
tr.selected td{background:rgba(255,153,204,.1);color:var(--accent)}

/* ── Log ── */
.log-wrap{background:#03030a;border:1px solid rgba(0,255,120,.12);
  border-radius:var(--r);padding:14px 16px;height:calc(100vh - 180px);
  overflow-y:auto;font-size:.75rem;line-height:1.9;color:#00ffcc}
.log-wrap .log-err{color:var(--red)}
.log-wrap .log-ok{color:var(--green)}

/* ── Asset grid ── */
.asset-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}
.asset-item{background:var(--panel);border:1px solid var(--border);
  border-radius:var(--r);padding:10px 12px;font-size:.75rem;word-break:break-all;
  display:flex;flex-direction:column;gap:6px}
.asset-item .ai-name{color:var(--text);font-weight:bold}
.asset-item .ai-size{color:var(--dim)}
.asset-link{display:block;background:var(--input-bg);border:1px solid var(--border);
  border-radius:6px;padding:5px 10px;font-size:.72rem;color:var(--accent3);
  margin-top:4px;cursor:pointer;word-break:break-all}

/* ── Modal ── */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.75);backdrop-filter:blur(6px);
  display:flex;align-items:center;justify-content:center;z-index:9000;
  opacity:0;pointer-events:none;transition:opacity .35s var(--ease-out);will-change:opacity}
.modal-overlay.open{opacity:1;pointer-events:all}
.modal{background:var(--panel);border:1px solid var(--border);border-radius:14px;
  padding:28px 30px;width:min(520px,92vw);max-height:90vh;overflow-y:auto;
  transform:scale(0.85) translateY(24px);opacity:0;
  transition:transform .45s var(--ease),opacity .35s var(--ease-out);will-change:transform,opacity}
.modal-overlay.open .modal{transform:scale(1) translateY(0);opacity:1}
.modal h3{color:var(--accent);margin-bottom:16px;font-size:1rem}
.modal-footer{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}

/* ── Checkbox ── */
.ck-wrap{display:flex;align-items:center;gap:8px;margin-top:10px}
.ck-wrap input[type=checkbox]{width:auto;accent-color:var(--accent)}

/* ── Status bar ── */
.statusbar{padding:4px 18px;background:var(--panel);border-top:1px solid var(--border);
  font-size:.72rem;color:var(--dim);display:flex;align-items:center;gap:12px;flex-shrink:0}
.statusbar .st-msg{flex:1}
.statusbar .st-dot{width:7px;height:7px;border-radius:50%;background:var(--green)}

/* ── Scrollbar ── */
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-thumb{background:#252538;border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:#ff99cc44}

/* ── Responsive ── */
@media(max-width:700px){
  .split-left{width:180px}
  .topbar .logo{font-size:.85rem}
}
</style>
</head>
<body>

<!-- Top Bar -->
<div class="topbar">
  <span class="logo">INDEX // </span>
  <span style="color:var(--dim);font-size:.75rem">MOE_SYSTEM v8.0</span>
  <span class="clock" id="clock">--:--:--</span>
</div>

<!-- Tab Bar -->
<div class="tabbar">
  <div class="tab active" data-tab="content">✍ 内容</div>
  <div class="tab" data-tab="assets">🖼 素材</div>
  <div class="tab" data-tab="settings">⚙ 配置</div>
  <div class="tab" data-tab="anime">📺 追番</div>
  <div class="tab" data-tab="friends">🔗 友链</div>
  <div class="tab" data-tab="log">📋 日志</div>
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
        <button class="btn green" onclick="newFile()" style="flex:1">+ 新建</button>
      </div>
      <div id="file-list" style="flex:1;overflow-y:auto"></div>
    </div>
    <div class="split-right">
      <div class="btn-row">
        <button class="btn blue" onclick="saveFile()">💾 保存</button>
        <button class="btn red" onclick="deleteFile()">🗑 删除</button>
        <button class="btn green" onclick="triggerBuild()">🔨 构建</button>
        <button class="btn orange" onclick="triggerDeploy()">🚀 推送</button>
        <button class="btn" onclick="triggerCF()">☁ 清理CF</button>
      </div>
      <div style="display:flex;gap:10px;margin-bottom:10px">
        <div style="flex:1">
          <label class="field-label">文章标题 *</label>
          <input id="f-title" placeholder="输入标题...">
        </div>
        <div style="width:160px">
          <label class="field-label">发布日期</label>
          <input id="f-date" placeholder="YYYY-MM-DD">
        </div>
      </div>
      <label class="field-label">标签（逗号分隔）</label>
      <input id="f-tags" placeholder="anime, tech, life" style="margin-bottom:10px">
      <label class="field-label">正文 (Markdown)</label>
      <textarea id="f-body" style="height:calc(100vh - 340px);font-size:.8rem"
        placeholder="在此输入 Markdown 内容..."></textarea>
      <div id="content-status" style="margin-top:6px;font-size:.72rem;color:var(--dim)"></div>
    </div>
  </div>

  <!-- ═══════════════ TAB: 素材 ═══════════════ -->
  <div class="pane" id="tab-assets" style="display:none">
    <div class="section-title">素材库管理</div>
    <div class="btn-row">
      <label class="btn green" style="cursor:pointer">
        📁 上传文件 <input type="file" id="asset-upload" multiple style="display:none">
      </label>
      <button class="btn red" onclick="deleteSelectedAsset()">🗑 删除选中</button>
    </div>
    <div style="margin-bottom:12px">
      <label class="field-label">生成的 Markdown / URL 链接</label>
      <div style="display:flex;gap:8px">
        <input id="asset-link" readonly placeholder="上传后自动填充...">
        <button class="btn blue" onclick="copyAssetLink()">复制</button>
      </div>
    </div>
    <div id="asset-grid" class="asset-grid"></div>
  </div>

  <!-- ═══════════════ TAB: 配置 ═══════════════ -->
  <div class="pane" id="tab-settings" style="display:none">
    <div class="section-title">基础站点</div>
    <div class="card">
      <div class="field-row">
        <div><label class="field-label">站点标题</label><input data-cfg="site_name"></div>
        <div><label class="field-label">站点 URL（含 https://）</label><input data-cfg="site_url"></div>
      </div>
      <div class="field-row">
        <div><label class="field-label">Logo 文字</label><input data-cfg="logo_text"></div>
        <div><label class="field-label">建站日期（YYYY-MM-DD）</label><input data-cfg="start_date"></div>
      </div>
      <div class="field-row">
        <div><label class="field-label">Hero 大标题</label><input data-cfg="hero_title"></div>
        <div><label class="field-label">Hero 副标题</label><input data-cfg="hero_subtitle"></div>
      </div>
      <div class="field-row">
        <div><label class="field-label">SEO 关键词</label><input data-cfg="site_keywords"></div>
        <div><label class="field-label">SEO 描述</label><input data-cfg="site_description"></div>
      </div>
      <div class="field-row">
        <div><label class="field-label">背景图 URL</label><input data-cfg="bg_url"></div>
        <div><label class="field-label">OG 封面图 URL</label><input data-cfg="og_image"></div>
      </div>
      <div class="field-row">
        <div style="flex:1 1 100%"><label class="field-label">文章封面图池（每行一个 URL，构建时为每篇文章随机分配一张；留空则使用下方随机图 API）</label><textarea data-cfg="post_bg_urls" rows="3" style="width:100%"></textarea></div>
      </div>
      <div class="field-row">
        <div style="flex:1 1 100%"><label class="field-label">随机图 API 地址（图池留空时生效，每篇文章会自动带上各自唯一的 uid 参数，前端各自请求，不会撞图，也不占用构建时间）</label><input data-cfg="random_img_api" placeholder="https://www.dmoe.cc/random.php"></div>
      </div>
      <p style="font-size:.7rem;color:var(--dim);margin-top:-6px">
        单篇文章在编辑时填写「封面 URL」可覆盖以上全局设置，优先级最高。
      </p>
    </div>

    <div class="section-title">页脚内容</div>
    <div class="card">
      <label class="field-label">页脚文字（支持 HTML，链接用 href=，独立于公告栏）</label>
      <textarea data-cfg="footer_text" style="height:80px" placeholder="本站由 Cloudflare 和 GitHub 强力驱动&#10;如有问题请联系：me@example.com"></textarea>
      <p style="font-size:.7rem;color:var(--dim);margin-top:6px">留空则沿用旧版 footer_custom 内容（兼容旧配置）</p>
    </div>

    <div class="section-title">弹窗公告 &amp; 通知</div>
    <div class="card">
      <label class="field-label">公告内容（支持 HTML，链接请用 href= 而非 src=）</label>
      <textarea data-cfg="site_notice" style="height:80px"></textarea>
      <div class="ck-wrap">
        <input type="checkbox" id="ck-notice" data-cfg-bool="show_notice_widget">
        <label for="ck-notice" style="color:var(--dim);font-size:.8rem">侧边栏也显示公告 Widget</label>
      </div>
    </div>

    <div class="section-title">博主信息</div>
    <div class="card">
      <div class="field-row">
        <div><label class="field-label">博主 ID</label><input data-cfg="username"></div>
        <div><label class="field-label">头像 URL</label><input data-cfg="avatar_url"></div>
      </div>
      <label class="field-label">个人简介</label>
      <input data-cfg="bio">
      <div class="field-row">
        <div><label class="field-label">邮箱</label><input data-cfg="email"></div>
        <div><label class="field-label">GitHub</label><input data-cfg="github_url"></div>
      </div>
      <div class="field-row">
        <div><label class="field-label">Telegram</label><input data-cfg="telegram_url"></div>
        <div><label class="field-label">Bilibili</label><input data-cfg="bilibili_url"></div>
      </div>
      <div class="field-row">
        <div><label class="field-label">Twitter/X</label><input data-cfg="twitter_url"></div>
        <div><label class="field-label">RSS</label><input data-cfg="rss_url"></div>
      </div>
    </div>

    <div class="section-title">部署 &amp; Cloudflare</div>
    <div class="card">
      <div class="field-row">
        <div><label class="field-label">GitHub 仓库 SSH</label><input data-cfg="deploy_repo"></div>
        <div><label class="field-label">CF Zone ID</label><input data-cfg="cf_zone_id"></div>
      </div>
      <div class="field-row">
        <div><label class="field-label">Git 用户名（git config user.name）</label><input data-cfg="git_user_name" placeholder="Your Name"></div>
        <div><label class="field-label">Git 邮箱（git config user.email）</label><input data-cfg="git_user_email" placeholder="you@example.com"></div>
      </div>
      <div class="field-row">
        <div><label class="field-label">CF API Token</label><input data-cfg="cf_api_token" type="password"></div>
        <div><label class="field-label">CF 账号 Email（可选）</label><input data-cfg="cf_email"></div>
      </div>
    </div>

    <div class="section-title">广告代码</div>
    <div class="card">
      <label class="field-label">Monetag / 其他 &lt;script&gt; 标签（注入所有页面 &lt;head&gt; 末尾）</label>
      <textarea data-cfg="monetag_tag_code" style="height:90px"
        placeholder="<!-- 粘贴完整的 <script> 标签 -->"></textarea>
      <div style="margin-top:10px;display:flex;align-items:center;gap:10px">
        <label class="btn green" style="cursor:pointer">
          📂 上传 sw.js <input type="file" id="sw-upload" accept=".js" style="display:none">
        </label>
        <button class="btn red" onclick="deleteSwJs()">🗑 删除 sw.js</button>
        <span id="sw-status" style="font-size:.72rem;color:var(--dim)">检测中...</span>
      </div>
    </div>

    <div style="margin-top:20px;text-align:center">
      <button class="btn primary" style="padding:12px 48px;font-size:.9rem" onclick="saveConfig()">
        💾 保存所有配置
      </button>
    </div>
  </div>

  <!-- ═══════════════ TAB: 追番 ═══════════════ -->
  <div class="pane" id="tab-anime" style="display:none">
    <div class="section-title">正在追番管理</div>
    <div class="btn-row">
      <button class="btn green" onclick="openAnimeModal()">+ 添加番剧</button>
      <button class="btn" onclick="editAnime()">✏ 编辑</button>
      <button class="btn red" onclick="deleteAnime()">✕ 删除</button>
      <button class="btn" onclick="moveAnime(-1)">↑ 上移</button>
      <button class="btn" onclick="moveAnime(1)">↓ 下移</button>
      <button class="btn blue" onclick="saveAnimeList()">💾 保存到配置</button>
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
      <button class="btn" onclick="editFriend()">✏ 编辑</button>
      <button class="btn red" onclick="deleteFriend()">✕ 删除</button>
      <button class="btn" onclick="moveFriend(-1)">↑ 上移</button>
      <button class="btn" onclick="moveFriend(1)">↓ 下移</button>
      <button class="btn blue" onclick="saveFriendList()">💾 保存到配置</button>
    </div>
    <table id="friend-table">
      <thead><tr>
        <th>名称</th><th>链接</th><th>头像URL</th><th>描述</th>
      </tr></thead>
      <tbody id="friend-tbody"></tbody>
    </table>
  </div>

  <!-- ═══════════════ TAB: 日志 ═══════════════ -->
  <div class="pane" id="tab-log" style="display:none">
    <div class="btn-row">
      <button class="btn red" onclick="clearLog()">🗑 清空</button>
      <span style="font-size:.72rem;color:var(--dim)">实时推送（SSE）</span>
    </div>
    <div class="log-wrap" id="log-box"></div>
  </div>

</div><!-- /workspace -->

<!-- Status Bar -->
<div class="statusbar">
  <div class="st-dot" id="st-dot"></div>
  <div class="st-msg" id="st-msg">SYSTEM READY</div>
</div>

<!-- ── 番剧 Modal ── -->
<div class="modal-overlay" id="anime-modal">
  <div class="modal">
    <h3 id="anime-modal-title">添加番剧</h3>
    <input type="hidden" id="anime-edit-idx" value="-1">
    <label class="field-label">标题 *</label><input id="am-title">
    <label class="field-label">封面 URL</label><input id="am-cover" placeholder="https://...">
    <div class="field-row">
      <div><label class="field-label">当前集</label><input id="am-ep" placeholder="12"></div>
      <div><label class="field-label">总集数</label><input id="am-total" placeholder="24"></div>
    </div>
    <label class="field-label">备注</label><input id="am-note" placeholder="推荐理由等">
    <label class="field-label">状态</label>
    <select id="am-status">
      <option value="airing">连载中</option>
      <option value="ended">已完结</option>
    </select>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('anime-modal')">取消</button>
      <button class="btn green" onclick="confirmAnime()">确定</button>
    </div>
  </div>
</div>

<!-- ── 友链 Modal ── -->
<div class="modal-overlay" id="friend-modal">
  <div class="modal">
    <h3 id="friend-modal-title">添加友链</h3>
    <input type="hidden" id="friend-edit-idx" value="-1">
    <label class="field-label">名称 *</label><input id="fm-name">
    <label class="field-label">链接 *</label><input id="fm-url" placeholder="https://example.com">
    <label class="field-label">头像 URL</label><input id="fm-avatar" placeholder="https://...">
    <label class="field-label">描述</label><input id="fm-desc" placeholder="一句话介绍">
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('friend-modal')">取消</button>
      <button class="btn green" onclick="confirmFriend()">确定</button>
    </div>
  </div>
</div>

<script>
'use strict';
// ─── State ───
let currentFile = null;
let animeList = [];
let friendList = [];
let selectedAnimeRow = -1;
let selectedFriendRow = -1;
let selectedAsset = null;
let logSource = null;

// ─── Clock ───
setInterval(() => {
  document.getElementById('clock').textContent =
    new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
}, 1000);

// ─── Status ───
function setStatus(msg, ok=true) {
  document.getElementById('st-msg').textContent = msg;
  document.getElementById('st-dot').style.background = ok ? 'var(--green)' : 'var(--red)';
}

// ─── Tab ───
function switchTab(tabName) {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  const tabEl = document.querySelector(`[data-tab="${tabName}"]`);
  if (tabEl) tabEl.classList.add('active');
  document.querySelectorAll('.pane').forEach(p => {
    p.style.display = 'none';
    p.classList.remove('pane-enter');
  });
  const id = 'tab-' + tabName;
  const pane = document.getElementById(id);
  if (!pane) return;
  pane.style.display = (id === 'tab-content') ? 'flex' : 'block';
  // 触发弹性入场动画
  requestAnimationFrame(() => {
    pane.style.opacity = '0';
    pane.style.transform = 'translateY(18px) scale(0.98)';
    pane.style.transition = 'none';
    requestAnimationFrame(() => {
      pane.style.transition = 'opacity .4s var(--ease-out), transform .45s var(--ease)';
      pane.style.opacity = '1';
      pane.style.transform = 'translateY(0) scale(1)';
    });
  });
  if (tabName === 'assets') loadAssets();
  if (tabName === 'settings') loadSettings();
  if (tabName === 'anime') loadAnime();
  if (tabName === 'friends') loadFriends();
  if (tabName === 'log') startLog();
}
document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => switchTab(t.dataset.tab));
});
// Init
loadFileList();
checkSwStatus();

// ─── File List ───
async function loadFileList() {
  const lang = document.getElementById('lang-select').value;
  const mode = document.getElementById('mode-select').value;
  const res = await fetch(`/api/files?lang=${lang}&mode=${mode}`);
  const files = await res.json();
  const el = document.getElementById('file-list');
  el.innerHTML = '';
  files.forEach(f => {
    const d = document.createElement('div');
    d.className = 'file-item' + (currentFile===f.name?' active':'');
    d.innerHTML = `<div class="fi-name">${f.name}</div><div class="fi-date">${f.date||''}</div>`;
    d.onclick = () => loadFile(f.name);
    el.appendChild(d);
  });
}
document.getElementById('lang-select').onchange = () => { currentFile=null; loadFileList(); clearEditor(); };
document.getElementById('mode-select').onchange = () => { currentFile=null; loadFileList(); clearEditor(); };

function clearEditor() {
  document.getElementById('f-title').value='';
  document.getElementById('f-date').value='';
  document.getElementById('f-tags').value='';
  document.getElementById('f-body').value='';
  document.getElementById('content-status').textContent='';
  currentFile = null;
}

async function loadFile(name) {
  const lang = document.getElementById('lang-select').value;
  const mode = document.getElementById('mode-select').value;
  const res = await fetch(`/api/file?lang=${lang}&mode=${mode}&name=${encodeURIComponent(name)}`);
  if (!res.ok) return;
  const d = await res.json();
  document.getElementById('f-title').value = d.title||'';
  document.getElementById('f-date').value = d.date||'';
  document.getElementById('f-tags').value = Array.isArray(d.tags)?d.tags.join(', '):(d.tags||'');
  document.getElementById('f-body').value = d.content||'';
  document.getElementById('content-status').textContent = `已加载: ${name}`;
  currentFile = name;
  document.querySelectorAll('.file-item').forEach(el => {
    el.classList.toggle('active', el.querySelector('.fi-name').textContent===name);
  });
}

function newFile() {
  clearEditor();
  document.getElementById('f-date').value = new Date().toISOString().slice(0,10);
  document.getElementById('content-status').textContent = '新建模式';
}

async function saveFile() {
  const title = document.getElementById('f-title').value.trim();
  if (!title) { alert('标题不能为空！'); return; }
  const body = {
    lang: document.getElementById('lang-select').value,
    mode: document.getElementById('mode-select').value,
    name: currentFile,
    title,
    date: document.getElementById('f-date').value || new Date().toISOString().slice(0,10),
    tags: document.getElementById('f-tags').value,
    content: document.getElementById('f-body').value,
  };
  const res = await fetch('/api/file', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const d = await res.json();
  if (d.ok) {
    currentFile = d.name;
    document.getElementById('content-status').textContent = `✅ 已保存 UID:${d.uid}`;
    setStatus('文件保存成功');
    loadFileList();
  } else {
    alert('保存失败: ' + d.error);
  }
}

async function deleteFile() {
  if (!currentFile) { alert('未选择文件'); return; }
  if (!confirm(`删除 ${currentFile}？`)) return;
  const lang = document.getElementById('lang-select').value;
  const mode = document.getElementById('mode-select').value;
  await fetch(`/api/file?lang=${lang}&mode=${mode}&name=${encodeURIComponent(currentFile)}`, {method:'DELETE'});
  clearEditor(); loadFileList(); setStatus('文件已删除');
}

// ─── Build / Deploy ───
function triggerBuild() {
  if (!confirm('执行构建？')) return;
  fetch('/api/build', {method:'POST'});
  setStatus('⏳ 构建中...');
  // 自动切换到日志 tab
  document.querySelector('[data-tab=log]').click();
}

function triggerDeploy() {
  if (!confirm('强制推送 GitHub 远程 main 分支？')) return;
  fetch('/api/deploy', {method:'POST'});
  setStatus('⏳ 推送中...');
  document.querySelector('[data-tab=log]').click();
}

function triggerCF() {
  fetch('/api/purge_cf', {method:'POST'});
  setStatus('⏳ 清理 CF 缓存...');
  document.querySelector('[data-tab=log]').click();
}

// ─── Assets ───
async function loadAssets() {
  const res = await fetch('/api/assets');
  const files = await res.json();
  const grid = document.getElementById('asset-grid');
  grid.innerHTML = '';
  files.forEach(f => {
    const d = document.createElement('div');
    d.className = 'asset-item';
    const isImg = /\.(jpg|jpeg|png|gif|webp|avif|svg)$/i.test(f.name);
    const link = isImg ? `![${f.name}](/attachments/${f.name})` : `[${f.name}](/attachments/${f.name})`;
    d.innerHTML = `<div class="ai-name">${f.name}</div>
      <div class="ai-size">${f.size}</div>
      <div class="asset-link" onclick="selectAsset('${link}',this)">${link}</div>`;
    grid.appendChild(d);
  });
}

function selectAsset(link, el) {
  document.getElementById('asset-link').value = link;
  selectedAsset = el.closest('.asset-item').querySelector('.ai-name').textContent;
  document.querySelectorAll('.asset-item').forEach(x => x.style.borderColor='');
  el.closest('.asset-item').style.borderColor = 'var(--accent)';
}

function copyAssetLink() {
  const v = document.getElementById('asset-link').value;
  if (v) { navigator.clipboard.writeText(v); setStatus('✅ 已复制'); }
}

async function deleteSelectedAsset() {
  if (!selectedAsset) { alert('请先点击选中一项素材'); return; }
  if (!confirm(`删除 ${selectedAsset}？`)) return;
  await fetch(`/api/asset?name=${encodeURIComponent(selectedAsset)}`, {method:'DELETE'});
  selectedAsset = null;
  document.getElementById('asset-link').value='';
  loadAssets(); setStatus('素材已删除');
}

document.getElementById('asset-upload').onchange = async (e) => {
  const files = e.target.files;
  if (!files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  const res = await fetch('/api/upload', {method:'POST', body:fd});
  const d = await res.json();
  if (d.ok) {
    setStatus(`✅ 上传了 ${d.names.length} 个文件`);
    const lastName = d.names[d.names.length-1];
    const isImg = /\.(jpg|jpeg|png|gif|webp|avif|svg)$/i.test(lastName);
    document.getElementById('asset-link').value = isImg
      ? `![${lastName}](/attachments/${lastName})`
      : `[${lastName}](/attachments/${lastName})`;
    loadAssets();
  }
  e.target.value='';
};

// ─── Settings ───
async function loadSettings() {
  const res = await fetch('/api/config');
  const cfg = await res.json();
  document.querySelectorAll('[data-cfg]').forEach(el => {
    el.value = cfg[el.dataset.cfg] ?? '';
  });
  document.querySelectorAll('[data-cfg-bool]').forEach(el => {
    el.checked = !!cfg[el.dataset.cfgBool];
  });
  checkSwStatus();
}

async function saveConfig() {
  const cfg = {};
  document.querySelectorAll('[data-cfg]').forEach(el => { cfg[el.dataset.cfg] = el.value; });
  document.querySelectorAll('[data-cfg-bool]').forEach(el => { cfg[el.dataset.cfgBool] = el.checked; });
  const res = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(cfg)});
  const d = await res.json();
  if (d.ok) { setStatus('✅ 配置保存成功'); alert('配置保存成功！重新构建后生效。'); }
  else alert('保存失败: ' + d.error);
}

async function checkSwStatus() {
  const res = await fetch('/api/sw_status');
  const d = await res.json();
  const el = document.getElementById('sw-status');
  if (el) el.textContent = d.exists ? '✅ sw.js 已上传' : '⚠ 未检测到 sw.js';
  if (el) el.style.color = d.exists ? 'var(--green)' : 'var(--accent2)';
}

document.getElementById('sw-upload').onchange = async (e) => {
  const f = e.target.files[0]; if (!f) return;
  const fd = new FormData(); fd.append('file', f);
  await fetch('/api/upload_sw', {method:'POST', body:fd});
  checkSwStatus(); setStatus('✅ sw.js 上传成功');
  e.target.value='';
};

async function deleteSwJs() {
  if (!confirm('删除 content/sw.js？')) return;
  await fetch('/api/sw', {method:'DELETE'});
  checkSwStatus(); setStatus('sw.js 已删除');
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
    tr.innerHTML = `<td>${a.title||''}</td><td>${a.status==='airing'?'连载中':'完结'}</td>
      <td>${a.ep||''}</td><td>${a.total||''}</td>
      <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis">${a.cover||''}</td>
      <td>${a.note||''}</td>`;
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
  setStatus('✅ 追番配置已保存'); alert('追番列表已保存！重新构建后生效。');
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
    tr.innerHTML = `<td>${f.name||''}</td>
      <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">${f.url||''}</td>
      <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis">${f.avatar||''}</td>
      <td>${f.desc||''}</td>`;
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
  const data = { name, url, avatar: document.getElementById('fm-avatar').value.trim(), desc: document.getElementById('fm-desc').value.trim() };
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
  setStatus('✅ 友链配置已保存'); alert('友链已保存！重新构建后生效。');
}

// ─── Modal ───
function closeModal(id) { document.getElementById(id).classList.remove('open'); }
document.querySelectorAll('.modal-overlay').forEach(o => {
  o.addEventListener('click', e => { if (e.target===o) o.classList.remove('open'); });
});

// ─── Log SSE ───
function startLog() {
  if (logSource) return;
  logSource = new EventSource('/api/log_stream');
  const box = document.getElementById('log-box');
  logSource.onmessage = (e) => {
    if (e.data === ':ping') return;
    const line = document.createElement('div');
    const msg = e.data;
    if (msg.includes('❌') || msg.includes('FAIL') || msg.includes('错误')) line.className='log-err';
    else if (msg.includes('✅') || msg.includes('完成') || msg.includes('成功')) line.className='log-ok';
    line.textContent = msg;
    box.appendChild(line);
    box.scrollTop = box.scrollHeight;
  };
}

function clearLog() {
  document.getElementById('log-box').innerHTML='';
}
</script>
</body>
</html>
"""


# ─────────────────────────── Routes ───────────────────────────
@app.route('/')
def index():
    return render_template_string(_HTML)


@app.route('/api/files')
def api_files():
    lang = request.args.get('lang', 'zh')
    mode = request.args.get('mode', 'posts')
    path = os.path.join(BASE_DIR, 'content', mode, lang)
    result = []
    if os.path.exists(path):
        for fn in sorted(os.listdir(path), reverse=True):
            if not fn.endswith('.md'):
                continue
            try:
                p = frontmatter.load(os.path.join(path, fn))
                result.append({'name': fn, 'date': str(p.get('date', ''))})
            except Exception:
                result.append({'name': fn, 'date': ''})
    return jsonify(result)


@app.route('/api/file', methods=['GET', 'POST', 'DELETE'])
def api_file():
    if request.method == 'GET':
        lang = request.args.get('lang', 'zh')
        mode = request.args.get('mode', 'posts')
        name = request.args.get('name', '')
        fp = os.path.join(BASE_DIR, 'content', mode, lang, name)
        if not os.path.exists(fp):
            return jsonify({'error': 'not found'}), 404
        p = frontmatter.load(fp)
        tags = p.get('tags', [])
        if isinstance(tags, list):
            tags = ', '.join(tags)
        return jsonify({'title': p.get('title', ''), 'date': str(p.get('date', '')),
                        'tags': tags, 'content': p.content})

    if request.method == 'DELETE':
        lang = request.args.get('lang', 'zh')
        mode = request.args.get('mode', 'posts')
        name = request.args.get('name', '')
        fp = os.path.join(BASE_DIR, 'content', mode, lang, name)
        if os.path.exists(fp):
            os.remove(fp)
        return jsonify({'ok': True})

    # POST — save
    data = request.json
    lang = data.get('lang', 'zh')
    mode = data.get('mode', 'posts')
    title = data.get('title', '').strip()
    date = data.get('date', datetime.now().strftime('%Y-%m-%d'))
    tags_raw = data.get('tags', '')
    tags = [x.strip() for x in tags_raw.split(',') if x.strip()] if tags_raw else []
    content = data.get('content', '')
    name = data.get('name') or f"{int(time.time())}.md"

    dir_path = os.path.join(BASE_DIR, 'content', mode, lang)
    os.makedirs(dir_path, exist_ok=True)
    fp = os.path.join(dir_path, name)

    uid = str(int(time.time())) + str(int(uuid.uuid4().int % 100))
    if os.path.exists(fp):
        old = frontmatter.load(fp)
        uid = str(old.get('uid', uid))
        date = str(old.get('date', date))

    meta = {'title': title, 'uid': uid, 'date': date}
    if tags:
        meta['tags'] = tags
    post = frontmatter.Post(content, **meta)
    # FIX: python-frontmatter>=1.x 的 dump() 只接受文本句柄（内部 fd.write(str)），
    # 用 'wb' 二进制模式打开会导致 "a bytes-like object is required, not 'str'"
    with open(fp, 'w', encoding='utf-8') as f:
        frontmatter.dump(post, f)

    _broadcast_log(f"SAVE: {name}  UID={uid}")
    return jsonify({'ok': True, 'name': name, 'uid': uid})


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
    name = request.args.get('name', '')
    fp = os.path.join(BASE_DIR, 'content', 'attachments', os.path.basename(name))
    if os.path.exists(fp):
        os.remove(fp)
    _broadcast_log(f"ASSET DELETE: {name}")
    return jsonify({'ok': True})


@app.route('/api/upload', methods=['POST'])
def api_upload():
    files = request.files.getlist('files')
    ap = os.path.join(BASE_DIR, 'content', 'attachments')
    os.makedirs(ap, exist_ok=True)
    names = []
    for f in files:
        fn = os.path.basename(f.filename)
        if fn:
            f.save(os.path.join(ap, fn))
            names.append(fn)
            _broadcast_log(f"UPLOAD: {fn} → content/attachments/")
    return jsonify({'ok': True, 'names': names})


@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'GET':
        return jsonify(_load_config())
    data = request.json
    cfg = _load_config()
    for k, v in data.items():
        cfg[k] = v
    _save_config(cfg)
    _broadcast_log("CONFIG SAVED")
    return jsonify({'ok': True})


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
    cfg['anime_list'] = request.json
    _save_config(cfg)
    _broadcast_log("ANIME LIST SAVED")
    return jsonify({'ok': True})


@app.route('/api/friends', methods=['GET', 'POST'])
def api_friends():
    if request.method == 'GET':
        cfg = _load_config()
        return jsonify(_get_list(cfg, 'friend_links'))
    cfg = _load_config()
    cfg['friend_links'] = request.json
    _save_config(cfg)
    _broadcast_log("FRIEND LINKS SAVED")
    return jsonify({'ok': True})


def _do_build():
    _broadcast_log("⏳ 开始构建...")
    if _builder_mod is None:
        _broadcast_log("❌ 找不到 builder.py")
        return
    import io, contextlib
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            _builder_mod.build()
        for line in buf.getvalue().splitlines():
            _broadcast_log(line)
        _broadcast_log("✅ 构建完成")
    except Exception as e:
        _broadcast_log(f"❌ 构建失败: {e}")


def _do_deploy():
    cfg = _load_config()
    repo = cfg.get('deploy_repo', '').strip()
    if not repo:
        _broadcast_log("❌ 请先在配置中填写 GitHub 仓库 SSH 地址")
        return
    pub = os.path.join(BASE_DIR, 'public')
    if not os.path.exists(pub):
        _broadcast_log("❌ public/ 目录不存在，请先构建")
        return
    gd = os.path.join(pub, '.git')
    if os.path.exists(gd):
        shutil.rmtree(gd, onerror=lambda f, p, _: (os.chmod(p, stat.S_IWRITE), f(p)))
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    git_name = cfg.get('git_user_name', '').strip() or 'CNFTE Deploy'
    git_email = cfg.get('git_user_email', '').strip() or 'deploy@cnfte.local'
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
            _broadcast_log("❌ 部署失败：命令执行超时（120s），请检查网络连通性")
            return
        except Exception as e:
            _broadcast_log(f"$ {' '.join(cmd)}  [FAIL exception]")
            _broadcast_log(f"❌ 部署失败：{type(e).__name__}: {e}")
            return
        out = (r.stdout + r.stderr).strip()
        st = '[OK]' if r.returncode == 0 else f'[FAIL rc={r.returncode}]'
        _broadcast_log(f"$ {' '.join(cmd)}  {st}" + (f"\n{out}" if out else ''))
        if r.returncode != 0:
            if not out:
                _broadcast_log(
                    "❌ 部署失败（命令无任何输出却返回失败，常见原因：与另一次构建/部署"
                    "并发冲突导致 git 锁文件冲突，或进程被意外中断）。请稍后重试一次。"
                )
            else:
                _broadcast_log("❌ 部署失败")
            return
    _broadcast_log("✅ 🚀 GitHub 推送完成")


def _do_purge_cf():
    cfg = _load_config()
    z = cfg.get('cf_zone_id', '').strip()
    tok = cfg.get('cf_api_token', '').strip()
    em = cfg.get('cf_email', '').strip()
    if not z or not tok:
        _broadcast_log("❌ 请先配置 CF Zone ID 和 API Token")
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
            _broadcast_log("✅ ☁️ CF 缓存清理成功")
        else:
            _broadcast_log(f"❌ CF 报错: {d}")
    except Exception as e:
        _broadcast_log(f"❌ CF 请求失败: {e}")


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
