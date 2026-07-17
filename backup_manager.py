# -*- coding: utf-8 -*-
"""
backup_manager.py
──────────────────
ModernBlogPanel 备份模块（轻量版）。

备份范围：content/config.json、content/posts/**、content/pages/**
不包含：content/attachments（附件）、构建产物 public/。

之所以只打包纯文本内容：Markdown 文章 + 配置通常只有几十 KB 到几百 KB，
整份备份基本能稳定控制在 5MB 以内，即使在网络不稳定（例如跨境访问）的环境下
也能快速、可靠地上传/下载，不会遇到大文件传输超时的问题。

功能：
  - create_backup()      本地打包为 zip
  - list_backups()       列出本地备份
  - delete_backup()      删除本地备份
  - restore_backup()     从本地 zip 恢复（恢复前自动做一次"恢复前快照"兜底）
  - prune_backups()      按保留份数清理旧备份
  - cloud_*              与云端 PHP 备份接口对接（上传 / 列表 / 拉取 / 删除）

安全说明：
  - zip 解压全程做路径穿越（zip slip）防护，只允许写入白名单前缀内的路径。
  - 云端交互全部走 HTTPS（由用户在配置中填写的 URL 决定），Key 通过自定义头
    X-Backup-Key 传递，不写入 URL query，避免出现在服务器访问日志中。
"""
import os
import io
import re
import json
import time
import zipfile
import mimetypes
import urllib.request
import urllib.error
import urllib.parse

# ── 打包白名单：仅这些路径会被纳入备份 ──
INCLUDE_FILES = ['content/config.json']
INCLUDE_DIRS = ['content/posts', 'content/pages']

# 单份备份体积超过该阈值时，create_backup() 返回结果中会带上 'oversized' 提示，
# 供上层 UI 提醒用户（不会阻止打包，只是提醒——正常情况下几乎不会触发）。
SIZE_WARN_BYTES = 5 * 1024 * 1024  # 5MB

_NAME_RE = re.compile(r'^backup_\d{8}_\d{6}(?:_[a-zA-Z0-9]+)?\.zip$')


def _backup_dir(base_dir: str) -> str:
    d = os.path.join(base_dir, 'backups')
    os.makedirs(d, exist_ok=True)
    return d


def _safe_name(name: str) -> str:
    """校验备份文件名合法（防目录穿越），非法则抛异常。"""
    name = os.path.basename(str(name or '').strip())
    if not name or not _NAME_RE.match(name):
        raise ValueError('非法的备份文件名')
    return name


def _human_size(n: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == 'B' else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# ─────────────────────────── 本地备份 ───────────────────────────

def create_backup(base_dir: str, label: str = '') -> dict:
    """打包 config.json + posts + pages 为 zip，返回 {name, size, size_h, mtime, oversized}"""
    bdir = _backup_dir(base_dir)
    ts = time.strftime('%Y%m%d_%H%M%S')
    safe_label = re.sub(r'[^a-zA-Z0-9]', '', label or '')[:16]
    # 始终附加短随机后缀，避免同一秒内连续创建（例如恢复前自动快照）时文件名冲突
    rand_suffix = os.urandom(3).hex()
    suffix = f"_{safe_label}{rand_suffix}" if safe_label else f"_{rand_suffix}"
    name = f"backup_{ts}{suffix}.zip"
    path = os.path.join(bdir, name)

    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
        cfg_path = os.path.join(base_dir, 'content', 'config.json')
        if os.path.exists(cfg_path):
            zf.write(cfg_path, arcname='content/config.json')
        for rel_dir in INCLUDE_DIRS:
            abs_dir = os.path.join(base_dir, rel_dir.replace('/', os.sep))
            if not os.path.exists(abs_dir):
                continue
            for root, _dirs, files in os.walk(abs_dir):
                for fn in files:
                    fp = os.path.join(root, fn)
                    arc = os.path.relpath(fp, base_dir).replace(os.sep, '/')
                    zf.write(fp, arcname=arc)
        # 备份清单，便于云端 / 人工核对内容
        manifest = {
            'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'includes': INCLUDE_FILES + INCLUDE_DIRS,
            'excludes': ['content/attachments', 'public/'],
        }
        zf.writestr('backup_manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))

    size = os.path.getsize(path)
    return {
        'name': name, 'size': size, 'size_h': _human_size(size),
        'mtime': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(path))),
        'oversized': size > SIZE_WARN_BYTES,
    }


def list_backups(base_dir: str) -> list:
    bdir = _backup_dir(base_dir)
    out = []
    for fn in os.listdir(bdir):
        if not fn.endswith('.zip'):
            continue
        fp = os.path.join(bdir, fn)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        out.append({
            'name': fn,
            'size': st.st_size,
            'size_h': _human_size(st.st_size),
            'mtime': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime)),
            'is_snapshot': fn.startswith('prerestore_'),
        })
    out.sort(key=lambda x: x['mtime'], reverse=True)
    return out


def delete_backup(base_dir: str, name: str) -> None:
    name = _safe_name(name) if not name.startswith('prerestore_') else os.path.basename(name)
    fp = os.path.join(_backup_dir(base_dir), name)
    if os.path.exists(fp):
        os.remove(fp)


def prune_backups(base_dir: str, keep: int) -> int:
    """按保留份数清理旧备份（恢复前自动快照 prerestore_* 不计入清理，单独保留最近 3 份）"""
    try:
        keep = int(keep)
    except (TypeError, ValueError):
        return 0
    if keep <= 0:
        return 0
    items = [b for b in list_backups(base_dir) if not b['is_snapshot']]
    removed = 0
    for b in items[keep:]:
        try:
            delete_backup(base_dir, b['name'])
            removed += 1
        except Exception:
            pass
    # 恢复前快照只保留最近 3 份，防止无限堆积
    snaps = [b for b in list_backups(base_dir) if b['is_snapshot']]
    for b in snaps[3:]:
        try:
            os.remove(os.path.join(_backup_dir(base_dir), b['name']))
        except Exception:
            pass
    return removed


def _extract_safely(zf: zipfile.ZipFile, dest_base: str) -> list:
    """仅允许解压 content/config.json、content/posts/**、content/pages/** 下的条目，
    并防止 zip slip（.. 路径穿越）。返回被写入的相对路径列表。"""
    allowed_prefixes = tuple(d + '/' for d in INCLUDE_DIRS)
    written = []
    dest_base = os.path.abspath(dest_base)
    for info in zf.infolist():
        rel = info.filename.replace('\\', '/')
        if rel in ('backup_manifest.json',) or rel.endswith('/'):
            continue
        if not (rel in INCLUDE_FILES or rel.startswith(allowed_prefixes)):
            continue  # 忽略白名单以外的条目（例如误打包的附件/构建产物）
        target = os.path.abspath(os.path.join(dest_base, rel))
        if not (target == dest_base or target.startswith(dest_base + os.sep)):
            continue  # 路径穿越，丢弃
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info) as src, open(target, 'wb') as dst:
            dst.write(src.read())
        written.append(rel)
    return written


def restore_backup(base_dir: str, name: str, auto_snapshot: bool = True) -> dict:
    """从本地备份 zip 恢复。默认恢复前自动创建一份 prerestore_ 快照兜底。"""
    name = _safe_name(name) if not name.startswith('prerestore_') else os.path.basename(name)
    path = os.path.join(_backup_dir(base_dir), name)
    if not os.path.exists(path):
        raise FileNotFoundError('备份文件不存在')

    snapshot_name = None
    if auto_snapshot:
        snap = create_backup(base_dir, label='')
        old = os.path.join(_backup_dir(base_dir), snap['name'])
        snapshot_name = 'prerestore_' + snap['name'][len('backup_'):]
        os.replace(old, os.path.join(_backup_dir(base_dir), snapshot_name))

    with zipfile.ZipFile(path, 'r') as zf:
        written = _extract_safely(zf, base_dir)

    return {'restored_files': len(written), 'snapshot': snapshot_name}


def restore_uploaded(base_dir: str, file_bytes: bytes, auto_snapshot: bool = True) -> dict:
    """从上传的 zip 二进制内容直接恢复（不落地为 backups/ 下的文件，先临时校验）。"""
    buf = io.BytesIO(file_bytes)
    try:
        zf = zipfile.ZipFile(buf, 'r')
    except zipfile.BadZipFile:
        raise ValueError('上传的文件不是有效的 zip 备份')

    snapshot_name = None
    if auto_snapshot:
        snap = create_backup(base_dir, label='')
        old = os.path.join(_backup_dir(base_dir), snap['name'])
        snapshot_name = 'prerestore_' + snap['name'][len('backup_'):]
        os.replace(old, os.path.join(_backup_dir(base_dir), snapshot_name))

    written = _extract_safely(zf, base_dir)
    zf.close()
    return {'restored_files': len(written), 'snapshot': snapshot_name}


# ─────────────────────────── 云端接口 ───────────────────────────

class CloudError(Exception):
    """云端接口返回的错误。log 字段保留云端返回的详细执行日志（如果有）。"""
    def __init__(self, message: str, log=None):
        super().__init__(message)
        self.log = log or []


def _cloud_call(url: str, key: str, action: str, method: str = 'GET',
                 fields: dict = None, file_field: str = None, file_name: str = None,
                 file_bytes: bytes = None, timeout: int = 30, raw: bool = False):
    if not url or not key:
        raise CloudError('未配置云端备份地址或访问 Key')
    url = url.rstrip('/')
    fields = fields or {}

    if file_bytes is not None:
        boundary = '----ModernBlogPanelBoundary' + str(int(time.time() * 1000))
        body = io.BytesIO()

        def _wf(s):
            body.write(s.encode('utf-8') if isinstance(s, str) else s)

        for k, v in fields.items():
            _wf(f'--{boundary}\r\n')
            _wf(f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n')
        ctype = mimetypes.guess_type(file_name or 'backup.zip')[0] or 'application/zip'
        _wf(f'--{boundary}\r\n')
        _wf(f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n')
        _wf(f'Content-Type: {ctype}\r\n\r\n')
        _wf(file_bytes)
        _wf(f'\r\n--{boundary}--\r\n')
        data = body.getvalue()
        headers = {
            'X-Backup-Key': key,
            'Content-Type': f'multipart/form-data; boundary={boundary}',
            'Content-Length': str(len(data)),
        }
        req = urllib.request.Request(f"{url}?action={action}", data=data, headers=headers, method='POST')
    elif method == 'POST':
        data = urllib.parse.urlencode(fields).encode() if fields else b''
        headers = {'X-Backup-Key': key, 'Content-Type': 'application/x-www-form-urlencoded'}
        req = urllib.request.Request(f"{url}?action={action}", data=data, headers=headers, method='POST')
    else:
        qs = ('&' + urllib.parse.urlencode(fields)) if fields else ''
        headers = {'X-Backup-Key': key}
        req = urllib.request.Request(f"{url}?action={action}{qs}", headers=headers, method='GET')

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_data = resp.read()
    except urllib.error.HTTPError as e:
        log_lines = []
        try:
            err_body = json.loads(e.read().decode('utf-8', 'ignore'))
            msg = err_body.get('error', str(e))
            log_lines = err_body.get('log', []) or []
        except Exception:
            msg = f'HTTP {e.code}'
        raise CloudError(msg, log=log_lines)
    except urllib.error.URLError as e:
        raise CloudError(f'网络请求失败: {e.reason}')

    if raw:
        return raw_data
    try:
        return json.loads(raw_data.decode('utf-8'))
    except Exception:
        raise CloudError('云端返回了非预期的数据格式')


def cloud_ping(url: str, key: str) -> dict:
    return _cloud_call(url, key, 'ping', method='GET')


def cloud_list(url: str, key: str) -> list:
    d = _cloud_call(url, key, 'list', method='GET')
    if not d.get('ok'):
        raise CloudError(d.get('error', '获取云端列表失败'))
    return d.get('backups', [])


def cloud_upload(base_dir: str, url: str, key: str, name: str) -> dict:
    name = _safe_name(name) if not name.startswith('prerestore_') else os.path.basename(name)
    fp = os.path.join(_backup_dir(base_dir), name)
    if not os.path.exists(fp):
        raise FileNotFoundError('本地备份文件不存在')
    with open(fp, 'rb') as f:
        content = f.read()
    d = _cloud_call(url, key, 'upload', file_field='file', file_name=name, file_bytes=content)
    if not d.get('ok'):
        raise CloudError(d.get('error', '上传到云端失败'))
    return d


def cloud_pull(base_dir: str, url: str, key: str, name: str) -> dict:
    name = os.path.basename(str(name or '').strip())
    if not name.endswith('.zip'):
        raise ValueError('非法的云端备份文件名')
    raw_data = _cloud_call(url, key, 'download', method='GET', fields={'file': name}, raw=True)
    dest = os.path.join(_backup_dir(base_dir), name)
    with open(dest, 'wb') as f:
        f.write(raw_data)
    return {'name': name, 'size': len(raw_data)}


def cloud_delete(url: str, key: str, name: str) -> dict:
    name = os.path.basename(str(name or '').strip())
    d = _cloud_call(url, key, 'delete', method='POST', fields={'file': name})
    if not d.get('ok'):
        raise CloudError(d.get('error', '云端删除失败'))
    return d
