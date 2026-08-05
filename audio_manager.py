# -*- coding: utf-8 -*-
"""
audio_manager.py
──────────────────
ModernBlogPanel 音乐播放器 · 音频库管理模块。

存储布局（全部位于 content/audio/ 下，与 content/attachments 完全分开，
不会被 backup_manager 的备份范围打包，也不会被静态构建复制到 public/）：
  content/audio/manifest.json      音频清单（顺序 / 标题 / 歌手 / 歌词来源等元数据）
  content/audio/files/<uuid>.<ext> 实际音频文件（仅允许 .mp3 / .flac）
  content/audio/lyrics/<uuid>.lrc  用户手动上传的 lrc 歌词（可选）

歌词来源（lyrics_source）三选一：
  'embedded' 使用上传时从音频文件标签中解析出的内嵌歌词（MP3 的 USLT 帧 /
             FLAC 的 Vorbis Comment LYRICS 字段），解析结果缓存进 manifest，
             之后不会再重新读取音频二进制。
  'lrc'      使用后台手动上传的独立 .lrc 逐行时间戳文件。
  'none'     两者都没有 / 用户选择不显示歌词。

安全说明：
  - 文件名一律使用服务端生成的 uuid，不直接使用用户上传的原始文件名，
    避免路径穿越 / 文件名冲突 / 特殊字符问题；原始文件名只保留展示用。
  - 上传文件的扩展名做白名单校验（仅 .mp3 / .flac），后台其余上传接口
    （素材库等）不受影响。
"""
import os
import io
import re
import json
import uuid
import struct

try:
    import mutagen
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3
    from mutagen.flac import FLAC
    from mutagen.mp3 import MP3
except ImportError:
    mutagen = None

ALLOWED_EXTS = {'.mp3', '.flac'}


def _audio_root(base_dir: str) -> str:
    d = os.path.join(base_dir, 'content', 'audio')
    os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(d, 'files'), exist_ok=True)
    os.makedirs(os.path.join(d, 'lyrics'), exist_ok=True)
    return d


def _files_dir(base_dir: str) -> str:
    return os.path.join(_audio_root(base_dir), 'files')


def _lyrics_dir(base_dir: str) -> str:
    return os.path.join(_audio_root(base_dir), 'lyrics')


def _manifest_path(base_dir: str) -> str:
    return os.path.join(_audio_root(base_dir), 'manifest.json')


def _load_manifest(base_dir: str) -> list:
    p = _manifest_path(base_dir)
    if not os.path.exists(p):
        return []
    try:
        with open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_manifest(base_dir: str, tracks: list) -> None:
    with open(_manifest_path(base_dir), 'w', encoding='utf-8') as f:
        json.dump(tracks, f, ensure_ascii=False, indent=2)


def _fmt_duration(seconds) -> str:
    try:
        seconds = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ''
    m, s = divmod(max(seconds, 0), 60)
    return f"{m}:{s:02d}"


def _extract_metadata(filepath: str, ext: str) -> dict:
    """尽力从音频文件里解析标题 / 歌手 / 时长 / 内嵌歌词，解析失败时静默降级为空值。

    注意：部分 “.flac” 文件实际是用 ID3v2（TIT2/TPE1/USLT）打的标签，而不是
    FLAC 原生的 Vorbis Comment（常见于一些音乐下载/转码工具导出的文件，虽然
    不规范，但 PotPlayer 等播放器两种都认）。如果只按 Vorbis Comment 读取，
    这类文件会出现标题/歌手/歌词全部读空、只能靠文件名兜底的情况，
    所以这里在原生标签读不到时，额外用 ID3 再兜底读一次。
    """
    out = {'title': '', 'artist': '', 'duration': 0, 'duration_h': '', 'embedded_lyrics': ''}
    if mutagen is None:
        return out

    # 1) 优先按标准方式读：easy 接口（FLAC 走 Vorbis Comment，MP3 走 ID3 easy 映射）
    try:
        easy = MutagenFile(filepath, easy=True)
        if easy is not None:
            if easy.get('title'):
                out['title'] = str(easy['title'][0])
            if easy.get('artist'):
                out['artist'] = str(easy['artist'][0])
            if getattr(easy, 'info', None) and getattr(easy.info, 'length', None):
                out['duration'] = easy.info.length
                out['duration_h'] = _fmt_duration(easy.info.length)
    except Exception:
        pass

    audio = None
    try:
        if ext == '.mp3':
            audio = MP3(filepath)
        elif ext == '.flac':
            audio = FLAC(filepath)
    except Exception:
        audio = None

    # 时长兜底：easy 接口没拿到时，直接用原生对象的 info.length 再取一次
    if not out['duration'] and audio is not None:
        try:
            length = getattr(audio.info, 'length', None)
            if length:
                out['duration'] = length
                out['duration_h'] = _fmt_duration(length)
        except Exception:
            pass

    try:
        if ext == '.mp3' and audio is not None and audio.tags:
            uslt_frames = audio.tags.getall('USLT')
            if uslt_frames:
                out['embedded_lyrics'] = uslt_frames[0].text or ''
        elif ext == '.flac' and audio is not None:
            tags = audio.tags
            if tags:
                for key in ('lyrics', 'LYRICS', 'unsyncedlyrics', 'UNSYNCEDLYRICS'):
                    if key in tags and tags[key]:
                        out['embedded_lyrics'] = tags[key][0]
                        break
    except Exception:
        pass

    # 2) 兜底：FLAC 文件如果是用 ID3v2 打的标签（不规范但常见），上面原生
    #    Vorbis Comment 读取会全部落空，这里按 ID3 再补一次。
    #    mutagen.id3.ID3() 对非 mp3 文件同样有效，只要文件里存在 ID3v2 header。
    if ext == '.flac' and not (out['title'] and out['artist'] and out['embedded_lyrics']):
        try:
            id3 = ID3(filepath)
            if not out['title']:
                frames = id3.getall('TIT2')
                if frames:
                    out['title'] = str(frames[0])
            if not out['artist']:
                frames = id3.getall('TPE1')
                if frames:
                    out['artist'] = str(frames[0])
            if not out['embedded_lyrics']:
                uslt_frames = id3.getall('USLT')
                if uslt_frames:
                    out['embedded_lyrics'] = uslt_frames[0].text or ''
        except Exception:
            pass

    return out


def _public_track(t: dict) -> dict:
    """裁剪出前端 / 面板需要的字段（不暴露服务端内部路径）。"""
    return {
        'id': t.get('id'),
        'title': t.get('title') or t.get('original_name') or '未命名',
        'artist': t.get('artist') or '',
        'ext': t.get('ext'),
        'duration': t.get('duration', 0),
        'duration_h': t.get('duration_h', ''),
        'lyrics_source': t.get('lyrics_source', 'none'),
        'has_embedded_lyrics': bool(t.get('embedded_lyrics')),
        'has_lrc': bool(t.get('lrc_filename')),
        'order': t.get('order', 0),
        'url': f"/api/audio/stream/{t.get('id')}{t.get('ext')}",
    }


def list_tracks(base_dir: str) -> list:
    tracks = _load_manifest(base_dir)
    tracks.sort(key=lambda x: x.get('order', 0))
    return [_public_track(t) for t in tracks]


def add_track(base_dir: str, file_bytes: bytes, original_name: str) -> dict:
    ext = os.path.splitext(original_name or '')[1].lower()
    if ext not in ALLOWED_EXTS:
        raise ValueError('仅支持 .mp3 / .flac 格式的音频文件')
    if not file_bytes:
        raise ValueError('文件内容为空')

    tid = uuid.uuid4().hex
    dest = os.path.join(_files_dir(base_dir), f"{tid}{ext}")
    with open(dest, 'wb') as f:
        f.write(file_bytes)

    meta = _extract_metadata(dest, ext)
    tracks = _load_manifest(base_dir)
    max_order = max([t.get('order', 0) for t in tracks], default=-1)
    entry = {
        'id': tid,
        'ext': ext,
        'original_name': os.path.basename(original_name),
        'title': meta['title'] or os.path.splitext(os.path.basename(original_name))[0],
        'artist': meta['artist'],
        'duration': meta['duration'],
        'duration_h': meta['duration_h'],
        'embedded_lyrics': meta['embedded_lyrics'],
        'lrc_filename': '',
        'lyrics_source': 'embedded' if meta['embedded_lyrics'] else 'none',
        'order': max_order + 1,
    }
    tracks.append(entry)
    _save_manifest(base_dir, tracks)
    return _public_track(entry)


def update_track(base_dir: str, track_id: str, fields: dict) -> dict:
    tracks = _load_manifest(base_dir)
    for t in tracks:
        if t.get('id') == track_id:
            if 'title' in fields:
                t['title'] = str(fields['title'] or t['title'])
            if 'artist' in fields:
                t['artist'] = str(fields['artist'] or '')
            if 'lyrics_source' in fields and fields['lyrics_source'] in ('embedded', 'lrc', 'none'):
                src = fields['lyrics_source']
                # 只允许切换到确实存在数据的来源，避免选中一个空歌词源
                if src == 'embedded' and not t.get('embedded_lyrics'):
                    pass
                elif src == 'lrc' and not t.get('lrc_filename'):
                    pass
                else:
                    t['lyrics_source'] = src
            _save_manifest(base_dir, tracks)
            return _public_track(t)
    raise FileNotFoundError('音轨不存在')


def delete_track(base_dir: str, track_id: str) -> None:
    tracks = _load_manifest(base_dir)
    keep = []
    for t in tracks:
        if t.get('id') == track_id:
            fp = os.path.join(_files_dir(base_dir), f"{t.get('id')}{t.get('ext')}")
            if os.path.exists(fp):
                os.remove(fp)
            lrc = os.path.join(_lyrics_dir(base_dir), f"{t.get('id')}.lrc")
            if os.path.exists(lrc):
                os.remove(lrc)
        else:
            keep.append(t)
    _save_manifest(base_dir, keep)


def reorder_tracks(base_dir: str, ordered_ids: list) -> None:
    tracks = _load_manifest(base_dir)
    order_map = {tid: i for i, tid in enumerate(ordered_ids)}
    for t in tracks:
        if t.get('id') in order_map:
            t['order'] = order_map[t['id']]
    _save_manifest(base_dir, tracks)


def attach_lrc(base_dir: str, track_id: str, lrc_bytes: bytes) -> dict:
    tracks = _load_manifest(base_dir)
    for t in tracks:
        if t.get('id') == track_id:
            text = lrc_bytes.decode('utf-8', errors='ignore')
            with open(os.path.join(_lyrics_dir(base_dir), f"{track_id}.lrc"), 'w', encoding='utf-8') as f:
                f.write(text)
            t['lrc_filename'] = f"{track_id}.lrc"
            t['lyrics_source'] = 'lrc'
            _save_manifest(base_dir, tracks)
            return _public_track(t)
    raise FileNotFoundError('音轨不存在')


def remove_lrc(base_dir: str, track_id: str) -> dict:
    tracks = _load_manifest(base_dir)
    for t in tracks:
        if t.get('id') == track_id:
            fp = os.path.join(_lyrics_dir(base_dir), f"{track_id}.lrc")
            if os.path.exists(fp):
                os.remove(fp)
            t['lrc_filename'] = ''
            t['lyrics_source'] = 'embedded' if t.get('embedded_lyrics') else 'none'
            _save_manifest(base_dir, tracks)
            return _public_track(t)
    raise FileNotFoundError('音轨不存在')


_LRC_LINE_RE = re.compile(r'^\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\](.*)$')


def _parse_lrc(text: str) -> list:
    """把 lrc 文本解析成 [{time: 秒, text: 一行歌词}, ...]，按时间升序，忽略元信息行（ti/ar/al 等）。"""
    out = []
    for line in text.splitlines():
        line = line.rstrip('\r\n')
        m = _LRC_LINE_RE.match(line.strip())
        if not m:
            continue
        mm, ss, ms, content = m.groups()
        t = int(mm) * 60 + int(ss) + (int(ms.ljust(3, '0')) / 1000 if ms else 0)
        content = content.strip()
        if content:
            out.append({'time': round(t, 3), 'text': content})
    out.sort(key=lambda x: x['time'])
    return out


def _parse_json_lyrics(text: str):
    """尝试解析部分客户端（如网易云）导出的逐字 JSON 歌词格式，形如：
    [{"t":11376,"c":[{"tx":"Just take it "}]}, {"t":13800,"c":[{"tx":"..."}]}]
    t 是毫秒时间戳，c 是这一行按字/词切分的片段数组，拼接其中的 tx 即为整行文本。
    不是这个格式（或解析不出有效行）时返回 None，交给上层继续尝试别的格式。"""
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, list) or not data:
        return None
    out = []
    for item in data:
        if not isinstance(item, dict) or 't' not in item:
            return None
        try:
            t = float(item.get('t')) / 1000.0
        except (TypeError, ValueError):
            return None
        parts = item.get('c')
        if isinstance(parts, list):
            line_text = ''.join(str(p.get('tx', '')) for p in parts if isinstance(p, dict))
        else:
            line_text = str(item.get('tx', ''))
        line_text = line_text.strip()
        if line_text:
            out.append({'time': round(t, 3), 'text': line_text})
    if not out:
        return None
    out.sort(key=lambda x: x['time'])
    return out


def _parse_jsonl_lyrics(text: str):
    """兜底：部分客户端不是导出一整个 JSON 数组，而是每行各自一个
    JSON 对象（JSON Lines）。逐行尝试解析，只要能凑出至少一行有效的
    {t, c/tx} 就按 synced 处理；只要有任意一行不是合法 JSON 对象就整体放弃。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    out = []
    for ln in lines:
        if not (ln.startswith('{') and ln.endswith('}')):
            return None
        try:
            item = json.loads(ln)
        except Exception:
            return None
        if not isinstance(item, dict) or 't' not in item:
            return None
        try:
            t = float(item.get('t')) / 1000.0
        except (TypeError, ValueError):
            return None
        parts = item.get('c')
        if isinstance(parts, list):
            line_text = ''.join(str(p.get('tx', '')) for p in parts if isinstance(p, dict))
        else:
            line_text = str(item.get('tx', ''))
        line_text = line_text.strip()
        if line_text:
            out.append({'time': round(t, 3), 'text': line_text})
    if not out:
        return None
    out.sort(key=lambda x: x['time'])
    return out


def _normalize_embedded_lyrics(raw_text: str) -> dict:
    """内嵌歌词标签里实际存的内容五花八门：可能是真正的无时间戳纯文本，
    也可能是某些打标签工具直接把整份 LRC（带 [mm:ss.xx] 时间戳）塞进了
    LYRICS / USLT 字段，还可能是网易云等客户端导出的逐字 JSON 歌词。
    这里统一识别归一化成 {mode, lines, text}，避免把时间戳前缀或原始 JSON
    结构直接展示给用户（对应'时间点别在前端显示''还会出现JSON原歌词'的问题），
    并且只要能识别出时间信息就归类为 synced，让前端能真正按播放进度对齐。"""
    text = (raw_text or '').strip()
    if not text:
        return {'mode': 'none', 'lines': [], 'text': ''}

    if text[0] == '[' and text[-1] == ']':
        json_lines = _parse_json_lyrics(text)
        if json_lines:
            return {'mode': 'synced', 'lines': json_lines, 'text': ''}
    elif text[0] == '{':
        jsonl_lines = _parse_jsonl_lyrics(text)
        if jsonl_lines:
            return {'mode': 'synced', 'lines': jsonl_lines, 'text': ''}

    lrc_lines = _parse_lrc(text)
    if lrc_lines:
        return {'mode': 'synced', 'lines': lrc_lines, 'text': ''}

    return {'mode': 'plain', 'lines': [], 'text': text}


def get_lyrics(base_dir: str, track_id: str) -> dict:
    """返回 {mode: 'synced'|'plain'|'none', lines: [...], text: str}
    'synced' 用于精确同步滚动（lrc 上传 / 内嵌 LRC / 内嵌逐字 JSON 都归入此类）；
    'plain' 仅用于真正没有时间信息的整段歌词，前端按整体轮播展示。"""
    tracks = _load_manifest(base_dir)
    for t in tracks:
        if t.get('id') == track_id:
            src = t.get('lyrics_source', 'none')
            if src == 'lrc' and t.get('lrc_filename'):
                fp = os.path.join(_lyrics_dir(base_dir), t['lrc_filename'])
                if os.path.exists(fp):
                    with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                        text = f.read()
                    lines = _parse_lrc(text)
                    if lines:
                        return {'mode': 'synced', 'lines': lines, 'text': ''}
            if src == 'embedded' and t.get('embedded_lyrics'):
                return _normalize_embedded_lyrics(t['embedded_lyrics'])
            return {'mode': 'none', 'lines': [], 'text': ''}
    raise FileNotFoundError('音轨不存在')


def stream_path(base_dir: str, track_id: str, ext_hint: str = '') -> tuple:
    """返回 (目录, 文件名)，供 Flask send_from_directory 使用（支持 Range 断点续传/拖动进度条）。"""
    tracks = _load_manifest(base_dir)
    for t in tracks:
        if t.get('id') == track_id:
            return _files_dir(base_dir), f"{t['id']}{t['ext']}"
    raise FileNotFoundError('音轨不存在')


def export_for_build(base_dir: str) -> list:
    """给 builder.py 静态构建用：产出前端可以直接消费的音轨列表（歌词已解析好，
    URL 是相对站点根目录的静态路径，不依赖任何运行时接口——因为构建产物是纯静态
    文件，发布到 GitHub Pages / Cloudflare 后台面板的 Flask 服务并不会一起上线）。
    """
    tracks = _load_manifest(base_dir)
    tracks.sort(key=lambda x: x.get('order', 0))
    out = []
    for t in tracks:
        lyrics = {'mode': 'none', 'lines': [], 'text': ''}
        src = t.get('lyrics_source', 'none')
        if src == 'lrc' and t.get('lrc_filename'):
            fp = os.path.join(_lyrics_dir(base_dir), t['lrc_filename'])
            if os.path.exists(fp):
                with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = _parse_lrc(f.read())
                if lines:
                    lyrics = {'mode': 'synced', 'lines': lines, 'text': ''}
        elif src == 'embedded' and t.get('embedded_lyrics'):
            lyrics = _normalize_embedded_lyrics(t['embedded_lyrics'])
        out.append({
            'id': t.get('id'),
            'title': t.get('title') or t.get('original_name') or '未命名',
            'artist': t.get('artist') or '',
            'duration': t.get('duration', 0),
            'url': f"/audio/files/{t.get('id')}{t.get('ext')}",
            'lyrics': lyrics,
        })
    return out


def copy_audio_files_to_build(base_dir: str, output_dir: str) -> int:
    """把 content/audio/files 下的实际音频文件复制到构建输出目录（public/audio/files）。
    只复制一份到站点根目录（不像 attachments 那样每个语言目录各存一份）——
    因为播放器 URL 用的是站点根绝对路径 /audio/..., 中英文页面共用同一份即可，
    音频文件体积通常远大于图片，重复拷贝没有必要也浪费构建产物体积。"""
    src = _files_dir(base_dir)
    if not os.path.isdir(src):
        return 0
    files = [f for f in os.listdir(src) if os.path.isfile(os.path.join(src, f))]
    if not files:
        return 0
    dest = os.path.join(output_dir, 'audio', 'files')
    os.makedirs(dest, exist_ok=True)
    import shutil as _shutil
    for fn in files:
        _shutil.copy2(os.path.join(src, fn), os.path.join(dest, fn))
    return len(files)