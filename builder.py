import os
import shutil
import markdown
import frontmatter
import json
import math
import datetime
import stat
import re
import random
import time
import colorsys
import hashlib
import threading
import urllib.request
import urllib.parse
from html.parser import HTMLParser
from html import escape as _html_escape
from concurrent.futures import ThreadPoolExecutor, as_completed
from jinja2 import Environment, FileSystemLoader, select_autoescape

try:
    import audio_manager as _am
except ImportError:
    _am = None

I18N = {
    "zh": {"home": "首页", "lang_name": "EN", "lang_link": "/", "read_unit": "分钟"},
    "en": {"home": "HOME", "lang_name": "ZH", "lang_link": "/", "read_unit": "min"}
}

MD_EXTENSIONS = ['fenced_code', 'tables', 'toc', 'codehilite', 'nl2br', 'sane_lists']

_CJK_RE = re.compile(r'[\u3400-\u9fff\uf900-\ufaff]')
_SKIP_TRANS_TAGS = frozenset({'script', 'style', 'code', 'pre', 'kbd', 'svg', 'math', 'noscript', 'textarea'})
_SKIP_TRANS_CLASSES = frozenset({
    'post-math-block', 'post-math-inline', 'mb-mermaid', 'mb-terminal',
    'mb-diff', 'codehilite', 'mb-kbd-keys'
})


class _AutoTranslator:
    """构建期中→英自动翻译器（带 content/.i18n_cache.json 本地持久化缓存，零外部依赖）。"""

    def __init__(self, cache_path: str, enabled: bool = True):
        self.cache_path = cache_path
        self.enabled = enabled
        self._lock = threading.Lock()
        self._dirty = False
        self._cache: dict[str, str] = {}
        self._net_failed = 0
        if os.path.exists(cache_path):
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._cache = data
            except Exception:
                self._cache = {}

    def _key(self, text: str) -> str:
        return hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]

    def save(self):
        if not self._dirty:
            return
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with self._lock:
                tmp = self.cache_path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(self._cache, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.cache_path)
                self._dirty = False
        except Exception as e:
            print(f"[WARN] 保存翻译缓存失败: {e}")

    def _call_google(self, text: str) -> str | None:
        url = (
            'https://translate.googleapis.com/translate_a/single'
            '?client=gtx&sl=zh-CN&tl=en&dt=t&q=' + urllib.parse.quote(text)
        )
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            if isinstance(data, list) and data and isinstance(data[0], list):
                out = ''.join(seg[0] for seg in data[0] if isinstance(seg, list) and seg and seg[0])
                return out if out else None
        except Exception:
            return None
        return None

    def translate_text(self, text: str) -> str:
        if not text or not self.enabled or not _CJK_RE.search(text):
            return text
        stripped = text.strip()
        if not stripped:
            return text
        k = self._key(stripped)
        with self._lock:
            cached = self._cache.get(k)
        if cached is not None:
            lead = text[:len(text) - len(text.lstrip())]
            trail = text[len(text.rstrip()):]
            return lead + cached + trail
        if self._net_failed >= 6:
            return text
        res = self._call_google(stripped)
        if res is not None:
            with self._lock:
                self._cache[k] = res
                self._dirty = True
            lead = text[:len(text) - len(text.lstrip())]
            trail = text[len(text.rstrip()):]
            return lead + res + trail
        with self._lock:
            self._net_failed += 1
        return text

    def translate_many(self, texts: list[str]) -> list[str]:
        """批量合并请求以大幅加速首次构建，同时逐条写入缓存。"""
        if not self.enabled or not texts:
            return list(texts)
        results = list(texts)
        missing_idx: list[int] = []
        missing_clean: list[str] = []
        for idx, raw in enumerate(texts):
            if not raw or not _CJK_RE.search(raw):
                continue
            s = raw.strip()
            if not s:
                continue
            k = self._key(s)
            with self._lock:
                c = self._cache.get(k)
            if c is not None:
                lead = raw[:len(raw) - len(raw.lstrip())]
                trail = raw[len(raw.rstrip()):]
                results[idx] = lead + c + trail
            else:
                missing_idx.append(idx)
                missing_clean.append(s)

        if not missing_idx or self._net_failed >= 6:
            return results

        # 按字符数分批，每批 <= 1400 字符，用不易被翻译改写的数字分隔符拼接
        batch_start = 0
        while batch_start < len(missing_idx):
            if self._net_failed >= 6:
                break
            batch_i = []
            batch_s = []
            total_len = 0
            while batch_start < len(missing_idx):
                s = missing_clean[batch_start]
                if batch_s and total_len + len(s) + 16 > 1400:
                    break
                batch_i.append(missing_idx[batch_start])
                batch_s.append(s)
                total_len += len(s) + 16
                batch_start += 1

            if len(batch_s) == 1:
                tr = self.translate_text(texts[batch_i[0]])
                results[batch_i[0]] = tr
                continue

            sep = '\n[[[MBSEP]]]\n'
            joined = sep.join(batch_s)
            translated = self._call_google(joined)
            parts = None
            if translated is not None:
                parts = re.split(r'\s*\[\[\[\s*MBSEP\s*\]\]\]\s*', translated)
            if parts is not None and len(parts) == len(batch_s):
                with self._lock:
                    for bi, orig_s, en_s in zip(batch_i, batch_s, parts):
                        en_clean = en_s.strip()
                        if en_clean:
                            self._cache[self._key(orig_s)] = en_clean
                            self._dirty = True
                            raw = texts[bi]
                            lead = raw[:len(raw) - len(raw.lstrip())]
                            trail = raw[len(raw.rstrip()):]
                            results[bi] = lead + en_clean + trail
            else:
                # 若分隔符被改写，退回逐条翻译（仍然写入缓存）
                for bi in batch_i:
                    results[bi] = self.translate_text(texts[bi])
        return results

    def translate_html(self, html_str: str) -> str:
        """解析 HTML，仅翻译非代码/非数学/非图表元素的中文文本节点，100% 保留标签与属性。"""
        if not html_str or not self.enabled or not _CJK_RE.search(html_str):
            return html_str

        tokens: list[tuple[str, str]] = []  # ('raw', ...) or ('text', ...)
        skip_stack: list[bool] = []
        void_tags = frozenset({
            'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
            'link', 'meta', 'param', 'source', 'track', 'wbr'
        })

        class _Parser(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=False)

            def handle_starttag(self, tag, attrs):
                t = tag.lower()
                raw_tag = self.get_starttag_text() or f'<{tag}>'
                tokens.append(('raw', raw_tag))
                if t in void_tags:
                    return
                cls_val = ''
                for k, v in attrs:
                    if k.lower() == 'class' and v:
                        cls_val = v
                cls_set = set(cls_val.split())
                should_skip = (t in _SKIP_TRANS_TAGS) or bool(cls_set & _SKIP_TRANS_CLASSES)
                parent_skip = skip_stack[-1] if skip_stack else False
                skip_stack.append(parent_skip or should_skip)

            def handle_startendtag(self, tag, attrs):
                raw_tag = self.get_starttag_text() or f'<{tag}/>'
                tokens.append(('raw', raw_tag))

            def handle_endtag(self, tag):
                tokens.append(('raw', f'</{tag}>'))
                if skip_stack:
                    skip_stack.pop()

            def handle_data(self, data):
                if (skip_stack and skip_stack[-1]) or not _CJK_RE.search(data):
                    tokens.append(('raw', data))
                else:
                    tokens.append(('text', data))

            def handle_entityref(self, name):
                tokens.append(('raw', f'&{name};'))

            def handle_charref(self, name):
                tokens.append(('raw', f'&#{name};'))

            def handle_comment(self, data):
                tokens.append(('raw', f'<!--{data}-->'))

        try:
            parser = _Parser()
            parser.feed(html_str)
            parser.close()
        except Exception:
            return html_str

        text_positions = [i for i, (kind, _) in enumerate(tokens) if kind == 'text']
        if not text_positions:
            return html_str

        originals = [tokens[i][1] for i in text_positions]
        translated = self.translate_many(originals)
        for pos, en_val in zip(text_positions, translated):
            tokens[pos] = ('raw', _html_escape(en_val, quote=False))
        return ''.join(val for _, val in tokens)


def _derive_theme_colors(hex_color: str) -> dict:
    """
    根据后台配置的主题色（十六进制）推导出全套 CSS 变量值，写死进 <head> 的 :root，
    保证「刚打开网站、还没手动在前台调色盘调过颜色」的第一眼观感就已经是后台设置的
    主题色，而不是永远显示主题内置的默认粉色（旧版本的 bug：主题色只有前台 JS 手动
    改过、存进 localStorage 才会生效，后台配置形同虚设，页脚这类静态渲染的元素
    自然也就跟着一起没反应）。

    算法和前台 JS 里的 shiftHue() 保持一致（同样的色相旋转角度），确保「后台默认值」
    和「前台手动调色」两条路径算出来的颜色观感统一——但主题默认的粉色是设计师手调的
    粉/橙/蓝三色搭配，不是单纯色相旋转出来的，所以配置值等于默认粉色时直接沿用原始
    手调色板，避免大多数「没特意改过颜色」的用户观感发生变化。
    """
    DEFAULT_HEX = '#ff99cc'
    hex_color = (hex_color or DEFAULT_HEX).strip().lower()
    legacy_map = {
        'sakura': '#ff99cc', 'violet': '#a855f7', 'cyber': '#06b6d4',
        'gold': '#f59e0b', 'mint': '#10b981', 'dark': '#1e293b'
    }
    if hex_color in legacy_map:
        hex_color = legacy_map[hex_color]
    m = re.match(r'^#?([0-9a-fA-F]{6})$', hex_color)
    if not m:
        hex_color = DEFAULT_HEX
    else:
        hex_color = '#' + m.group(1)

    if hex_color == DEFAULT_HEX:
        return {
            'accent': '#ff99cc', 'accent2': '#ffcc99', 'accent3': '#99ccff',
            'grad': 'linear-gradient(135deg, #ff99cc 0%, #ffcc99 100%)',
            'grad2': 'linear-gradient(135deg, #99ccff 0%, #cc99ff 100%)',
            'border_accent': 'rgba(255, 153, 204, 0.45)',
        }

    hexs = hex_color[1:]
    r, g, b = int(hexs[0:2], 16) / 255, int(hexs[2:4], 16) / 255, int(hexs[4:6], 16) / 255
    h, l, s = colorsys.rgb_to_hls(r, g, b)

    def shift(deg):
        nh = (h + deg / 360.0) % 1.0
        nr, ng, nb = colorsys.hls_to_rgb(nh, l, s)
        return '#{:02x}{:02x}{:02x}'.format(round(nr * 255), round(ng * 255), round(nb * 255))

    accent2 = shift(30)
    accent3 = shift(200)
    accent4 = shift(260)
    return {
        'accent': hex_color,
        'accent2': accent2,
        'accent3': accent3,
        'grad': f'linear-gradient(135deg, {hex_color} 0%, {accent2} 100%)',
        'grad2': f'linear-gradient(135deg, {accent3} 0%, {accent4} 100%)',
        'border_accent': hex_color + '73',
    }


def _toc_slugify(value, separator='-'):
    import unicodedata
    # 规范化 Unicode
    value = unicodedata.normalize('NFKC', str(value))
    # 转小写
    value = value.lower()
    # 把空格和各种分隔符统一为连字符
    value = re.sub(r'[\s\u3000/\\|]+', '-', value)
    # 去除所有 CSS/URL 不合法的字符（保留中文、字母、数字、连字符、下划线）
    value = re.sub(r'[^\w\u4e00-\u9fff\u3040-\u30ff\-]', '', value)
    # 合并多个连字符
    value = re.sub(r'-{2,}', '-', value)
    # 去掉首尾连字符
    value = value.strip('-')
    # 不能为空
    if not value:
        value = 'section'
    # 不能以数字开头（CSS selector 规则）
    if value and value[0].isdigit():
        value = 'h-' + value
    return value


MD_EXT_CONFIGS = {
    'codehilite': {'css_class': 'codehilite', 'guess_lang': False},
    'toc': {'permalink': True, 'slugify': _toc_slugify}
}


def _safe_remove(path):
    if not os.path.exists(path):
        return

    def _handler(func, p, _):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    shutil.rmtree(path, onerror=_handler)


def _get_meta_desc(content: str, custom: str, length=155) -> str:
    if custom and custom.strip():
        return custom.strip()
    text = re.sub(r'```[\s\S]*?```', '', content)
    text = re.sub(r'`[^`]+`', '', text)
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text)
    text = re.sub(r'[#*>`\-_~|]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return (text[:length] + "…") if len(text) > length else text


def _render_md(content: str) -> tuple[str, str]:
    # 统一换行符，防止 Windows CRLF 导致块级正则匹配失败
    content = (content or '').replace('\r\n', '\n').replace('\r', '\n')

    # 保护代码块、已封装的自定义 mb-* HTML 块与公式不被 Markdown 误解析
    protected: list[str] = []
    def _stash(raw: str) -> str:
        idx = len(protected)
        protected.append(raw)
        return f"@@MDSTASH{idx}@@"

    # 1. 保护围栏代码块与行内代码
    tmp = re.sub(r'```[\s\S]*?```', lambda m: _stash(m.group(0)), content)
    tmp = re.sub(r'`[^`\n]+`', lambda m: _stash(m.group(0)), tmp)

    # 2. 保护块级数学公式（同时支持多行 $$\n...\n$$ 与单行 $$...$$）
    math_blocks: list[str] = []
    def _stash_math(m):
        idx = len(math_blocks)
        math_blocks.append(m.group(1).strip())
        return f"\n\n@@MATHBLK{idx}@@\n\n"
    tmp = re.sub(r'(?m)^\$\$\s*\n([\s\S]*?)\n\$\$\s*$', _stash_math, tmp)
    tmp = re.sub(r'(?m)^\$\$([^\n$]+?)\$\$\s*$', _stash_math, tmp)

    # 3. 保护行内数学公式 $...$（避免内部 _ 或 ^ 被 Markdown 解析成斜体）
    inline_maths: list[str] = []
    def _stash_inline_math(m):
        idx = len(inline_maths)
        inline_maths.append(m.group(1))
        return f"@@INMATH{idx}@@"
    tmp = re.sub(r'(?<!\$)\$(?!\s)([^\n$]+?)(?<!\s)\$(?!\$)', _stash_inline_math, tmp)

    # 4. 支持 GFM 删除线 ~~text~~、高亮 ==text==、下划线 ++text++
    tmp = re.sub(r'~~(?=\S)(.+?)(?<=\S)~~', r'<del>\1</del>', tmp)
    tmp = re.sub(r'==(?=\S)(.+?)(?<=\S)==', r'<mark>\1</mark>', tmp)
    tmp = re.sub(r'\+\+(?=\S)(.+?)(?<=\S)\+\+', r'<u>\1</u>', tmp)

    # 还原被保护的代码块以便 Python-Markdown 正常高亮
    for idx, raw in enumerate(protected):
        tmp = tmp.replace(f"@@MDSTASH{idx}@@", raw)

    md = markdown.Markdown(extensions=MD_EXTENSIONS, extension_configs=MD_EXT_CONFIGS)
    html = md.convert(tmp)

    # 还原块级数学公式容器（带 data-math 属性便于前端 KaTeX 精准渲染）
    for idx, expr in enumerate(math_blocks):
        safe_expr = expr.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        block_html = f'<div class="post-math-block" data-math="{safe_expr.replace(chr(34), "&quot;")}">$$\n{safe_expr}\n$$</div>'
        html = html.replace(f'<p>@@MATHBLK{idx}@@</p>', block_html).replace(f'@@MATHBLK{idx}@@', block_html)

    # 还原行内数学公式
    for idx, expr in enumerate(inline_maths):
        safe_expr = expr.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        html = html.replace(f'@@INMATH{idx}@@', f'<span class="post-math-inline">${safe_expr}$</span>')

    # 5. 支持 GFM 待办清单 - [x] / - [ ]（兼容紧凑列表与松散列表 <li><p>...</p></li>）
    html = re.sub(
        r'<li>\s*<p>\s*\[([ xX])\]\s+([\s\S]*?)</p>\s*</li>',
        lambda m: f'<li class="task-item"><input type="checkbox" disabled{" checked" if m.group(1).lower()=="x" else ""}> {m.group(2)}</li>',
        html
    )
    html = re.sub(
        r'<li>\s*\[([ xX])\]\s+',
        lambda m: f'<li class="task-item"><input type="checkbox" disabled{" checked" if m.group(1).lower()=="x" else ""}> ',
        html
    )

    # 为文章正文图片自动添加 lazy loading + async decoding
    html = re.sub(
        r'<img(?![^>]*loading=)([^>]*)>',
        r'<img\1 loading="lazy" decoding="async">',
        html
    )
    return html, getattr(md, 'toc', '')


def _inject_uid(file_path: str, p) -> str:
    if 'uid' in p.metadata and str(p.metadata.get('uid', '')).strip():
        return str(p.metadata['uid']).strip()
    uid = str(int(time.time())) + str(random.randint(10, 99))
    p.metadata['uid'] = uid
    # FIX: python-frontmatter>=1.x 的 dump() 只接受文本句柄（内部 fd.write(str)），
    # 用 'wb' 二进制模式打开会导致 "a bytes-like object is required, not 'str'"
    with open(file_path, 'w', encoding='utf-8') as f:
        frontmatter.dump(p, f)
    return uid


def _copy_tree_fast(src: str, dst: str):
    os.makedirs(dst, exist_ok=True)
    for fn in os.listdir(src):
        sf = os.path.join(src, fn)
        df = os.path.join(dst, fn)
        if os.path.isfile(sf):
            shutil.copy2(sf, df)


def _fix_notice_links(html: str) -> str:
    """
    FIX crawlable-anchors: 将公告内容中错误的 <a src="..."> 修复为 <a href="...">
    用户配置的 site_notice 若包含 src= 写法，统一转换，确保搜索引擎可抓取。
    """
    return re.sub(r'<a\s+src=(["\'])(.*?)\1', r'<a href=\1\2\1', html, flags=re.IGNORECASE)


def _resolve_cover_url(meta: dict, uid: str, post_bg_urls: list, random_img_api: str) -> None:
    """
    为文章/页面计算封面图兜底 URL，写入 meta['bg_url']（原地修改）。
    优先级：frontmatter 手动 cover（已在 meta 中，不覆盖）
          > 固定图池随机抽取
          > 随机图 API + uid（前端各自请求，零构建期网络开销，且因 uid 各不相同天然不会撞图）
          > 都没配置：meta['use_default_icon'] = True，模板会渲染一个默认的毛笔图标占位，
            而不是留一个空白/破图的 <img>。
    """
    if meta.get('cover'):
        meta['use_default_icon'] = False
        return
    if post_bg_urls:
        meta['bg_url'] = random.choice(post_bg_urls)
        meta['use_default_icon'] = False
    elif random_img_api:
        sep = '&' if '?' in random_img_api else '?'
        meta['bg_url'] = f"{random_img_api}{sep}v={uid}"
        meta['use_default_icon'] = False
    else:
        meta['use_default_icon'] = True


def _extract_category(p) -> str:
    """从 frontmatter 中提取单个分类（category 字段），兼容误填为列表的情况（取第一个）。"""
    cat = p.get('category', '')
    if isinstance(cat, list):
        cat = cat[0] if cat else ''
    return str(cat).strip()


def build():
    t_start = time.time()
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONTENT_DIR = os.path.join(BASE_DIR, 'content')
    OUTPUT_DIR = os.path.join(BASE_DIR, 'public')
    THEME_DIR = os.path.join(BASE_DIR, 'themes', 'default')

    config_path = os.path.join(CONTENT_DIR, 'config.json')
    if not os.path.exists(config_path):
        print("[ERR] 找不到 config.json，请先运行 main.py 初始化。")
        return
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    # ── 修复公告中的 src= 链接 ──
    if config.get('site_notice'):
        config['site_notice'] = _fix_notice_links(config['site_notice'])

    # ── 根据后台配置的主题色，推导出完整一套 CSS 变量（写死进 <head>，
    #    让「还没手动在前台调过色」的默认状态也能体现后台设置的主题色）──
    theme_colors = _derive_theme_colors(config.get('theme_color', '#ff99cc'))

    anime_raw = config.get('anime_list', [])
    if isinstance(anime_raw, str):
        try:
            config['anime_list'] = json.loads(anime_raw) if anime_raw.strip() else []
        except Exception:
            config['anime_list'] = []

    fl_raw = config.get('friend_links', [])
    if isinstance(fl_raw, str):
        try:
            config['friend_links'] = json.loads(fl_raw) if fl_raw.strip() else []
        except Exception:
            config['friend_links'] = []

    # ── 文章封面图：固定图池（留空则用下方随机图 API） ──
    pb_raw = config.get('post_bg_urls', [])
    if isinstance(pb_raw, str):
        config['post_bg_urls'] = [u.strip() for u in re.split(r'[\n,]+', pb_raw) if u.strip()]
    elif not isinstance(pb_raw, list):
        config['post_bg_urls'] = []
    post_bg_urls = config['post_bg_urls']

    # ── 随机图 API（前端模式）：URL 拼上每篇文章唯一的 uid 作为防撞图参数，
    #    不在构建期发起任何请求，由浏览器在访问时各自请求，天然零构建开销且互不重复 ──
    random_img_api = str(config.get('random_img_api', '') or '').strip()
    config['random_img_api'] = random_img_api

    snw = config.get('show_notice_widget', True)
    if isinstance(snw, str):
        config['show_notice_widget'] = snw.lower() not in ('false', '0', 'no', '')

    _safe_remove(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    open(os.path.join(OUTPUT_DIR, '.nojekyll'), 'w').close()

    # ── Monetag sw.js ──
    sw_src = os.path.join(CONTENT_DIR, 'sw.js')
    if os.path.exists(sw_src):
        shutil.copy2(sw_src, os.path.join(OUTPUT_DIR, 'sw.js'))
        print("[INFO] sw.js 已复制到输出目录（Monetag Push Ads）")
    elif config.get('monetag_tag_code', '').strip():
        print("[WARN] 检测到 Monetag 广告代码，但未找到 content/sw.js。")

    attach_src = os.path.join(CONTENT_DIR, 'attachments')
    if os.path.exists(attach_src):
        dest_attach = os.path.join(OUTPUT_DIR, 'attachments')
        _copy_tree_fast(attach_src, dest_attach)
        dest_assets = os.path.join(OUTPUT_DIR, 'assets')
        _copy_tree_fast(attach_src, dest_assets)

    # ── Favicon 兜底 ──
    fav_content = os.path.join(CONTENT_DIR, 'favicon.ico')
    fav_theme = os.path.join(THEME_DIR, 'favicon.ico')
    if os.path.exists(fav_content):
        shutil.copy2(fav_content, os.path.join(OUTPUT_DIR, 'favicon.ico'))
    elif os.path.exists(fav_theme):
        shutil.copy2(fav_theme, os.path.join(OUTPUT_DIR, 'favicon.ico'))

    # ── 音乐播放器：音轨 + 歌词数据打进静态页面，音频文件复制到 public/audio/files ──
    # （构建产物是纯静态文件，后台面板的 Flask 服务不会跟着一起发布，所以前端播放器
    #  不能依赖任何运行时接口，音轨列表和歌词都要在构建期就准备成静态数据）
    audio_tracks = []
    if config.get('enable_player') and _am is not None:
        try:
            audio_tracks = _am.export_for_build(BASE_DIR)
            n = _am.copy_audio_files_to_build(BASE_DIR, OUTPUT_DIR)
            if n:
                print(f"[INFO] 已复制 {n} 个音频文件到 public/audio/files/")
            audio_dir = os.path.join(OUTPUT_DIR, 'audio')
            os.makedirs(audio_dir, exist_ok=True)
            with open(os.path.join(audio_dir, 'tracks.json'), 'w', encoding='utf-8') as tf:
                json.dump(audio_tracks, tf, ensure_ascii=False, separators=(',', ':'))
        except Exception as e:
            print(f"[WARN] 音频库导出失败，播放器将不显示任何歌曲: {e}")
            audio_tracks = []

        # ── 音频并行分片加速 Service Worker：注册域限定在 /audio/，不会跟
        #    上面的 Monetag sw.js（scope 通常是站点根 /）互相冲突 ──
        accel_sw_src = os.path.join(THEME_DIR, 'audio-accel-sw.js')
        if os.path.exists(accel_sw_src):
            shutil.copy2(accel_sw_src, os.path.join(OUTPUT_DIR, 'audio-accel-sw.js'))
            print("[INFO] audio-accel-sw.js 已复制到输出目录（音频并行分片加速）")
        else:
            print("[WARN] 未找到 themes/default/audio-accel-sw.js，播放器将退回单流原生加载")

    env = Environment(
        loader=FileSystemLoader(THEME_DIR),
        autoescape=select_autoescape(disabled_extensions=['html']),
        keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True,
    )
    env.globals['datetime'] = datetime.datetime
    env.globals['random_seed'] = lambda: random.randint(1, 999999)

    try:
        idx_tpl = env.get_template('index.html')
        post_tpl = env.get_template('post.html')
    except Exception as e:
        print(f"[ERR] 模板加载失败: {e}")
        return

    # ── 404 页面（GitHub Pages 约定：仓库根目录放一个 404.html，
    #    访问不存在的路径时会展示它，同时仍然返回 HTTP 404 状态码）──
    # 主题里没有 404.html 也不影响正常构建，直接跳过即可。
    try:
        err_tpl = env.get_template('404.html')
        with open(os.path.join(OUTPUT_DIR, '404.html'), 'w', encoding='utf-8') as f:
            f.write(err_tpl.render(config=config, theme_colors=theme_colors))
    except Exception:
        pass

    all_links = []
    noindex_urls = set()

    auto_translate = bool(config.get('auto_translate_en', True))
    translator = _AutoTranslator(os.path.join(CONTENT_DIR, '.i18n_cache.json'), enabled=auto_translate)

    # 预加载手工英文版（如用户在 content/posts/en/ 或 content/pages/en/ 写了手动翻译，优先采用）
    manual_en_posts = {}
    en_post_dir = os.path.join(CONTENT_DIR, 'posts', 'en')
    if os.path.exists(en_post_dir):
        for fn in os.listdir(en_post_dir):
            if fn.endswith('.md'):
                try:
                    ep = frontmatter.load(os.path.join(en_post_dir, fn))
                    e_uid = str(ep.get('uid', '') or '').strip()
                    if e_uid:
                        manual_en_posts[e_uid] = ep
                    manual_en_posts[fn] = ep
                except Exception:
                    pass

    manual_en_pages = {}
    en_page_dir = os.path.join(CONTENT_DIR, 'pages', 'en')
    if os.path.exists(en_page_dir):
        for fn in os.listdir(en_page_dir):
            if fn.endswith('.md'):
                try:
                    manual_en_pages[fn] = frontmatter.load(os.path.join(en_page_dir, fn))
                except Exception:
                    pass

    # 翻译站点级文本（bio / site_name / site_notice / footer / anime_list）
    raw_footer = str(config.get('footer_text') or config.get('footer_custom') or '')
    anime_en_list = []
    for _an in (config.get('anime_list') or []):
        if isinstance(_an, dict):
            _t_en, _n_en = translator.translate_many([
                str(_an.get('title', '') or ''),
                str(_an.get('note', '') or ''),
            ])
            anime_en_list.append({'title': _t_en, 'note': _n_en})
        else:
            anime_en_list.append({'title': '', 'note': ''})

    config_en = {
        'site_name': translator.translate_text(str(config.get('site_name', '') or '')),
        'bio': translator.translate_text(str(config.get('bio', '') or '')),
        'site_notice': translator.translate_html(str(config.get('site_notice', '') or '')),
        'footer_html': translator.translate_html(raw_footer),
        'anime_list': anime_en_list,
    }

    lang = 'zh'
    lang_out = OUTPUT_DIR
    url_prefix = '/'
    os.makedirs(lang_out, exist_ok=True)
    search_index = []

    nav_pages = []
    page_src = os.path.join(CONTENT_DIR, 'pages', 'zh')
    if os.path.exists(page_src):
        for fn in sorted(f for f in os.listdir(page_src) if f.endswith('.md')):
            p = frontmatter.load(os.path.join(page_src, fn))
            url = (url_prefix + fn.replace('.md', '.html')).replace('//', '/')
            zh_title = str(p.get('title', fn))
            mp = manual_en_pages.get(fn)
            en_title = str(mp.get('title', '')) if (mp and mp.get('title')) else translator.translate_text(zh_title)
            nav_pages.append({'title': zh_title, 'title_en': en_title, 'url': url})

    def render_page(fn, src_dir, out_dir, u_prefix):
        fp = os.path.join(src_dir, fn)
        p = frontmatter.load(fp)
        uid = _inject_uid(fp, p)
        html, toc = _render_md(p.content)
        mp = manual_en_pages.get(fn)
        if mp is not None:
            html_en, toc_en = _render_md(mp.content)
            title_en = str(mp.get('title', '') or translator.translate_text(str(p.get('title', fn))))
        else:
            html_en = translator.translate_html(html)
            toc_en = translator.translate_html(toc)
            title_en = translator.translate_text(str(p.get('title', fn)))
        final_url = (u_prefix + fn.replace('.md', '.html')).replace('//', '/')
        _noindex = bool(p.get('noindex', False)) or 'noindex' in str(p.get('robots', ''))
        raw_date = p.get('date', '')
        date_str = str(raw_date) if raw_date else ''
        meta = {**p.metadata,
                'url': final_url,
                'uid': uid,
                'title_en': title_en,
                'content': html,
                'content_en': html_en,
                'toc': toc,
                'toc_en': toc_en,
                'date': date_str,
                'read_time': max(1, math.ceil(len(p.content) / 450)),
                'is_page': True,
                'noindex': _noindex,
                'description': _get_meta_desc(p.content, p.get('description', ''))}
        _resolve_cover_url(meta, uid, post_bg_urls, random_img_api)
        out = os.path.join(out_dir, fn.replace('.md', '.html'))
        with open(out, 'w', encoding='utf-8') as f:
            f.write(post_tpl.render(post=meta, i18n=I18N[lang], lang=lang,
                                    config=config, config_en=config_en, nav_pages=nav_pages,
                                    theme_colors=theme_colors, audio_tracks=audio_tracks))
        return final_url, meta.get('title', fn), title_en, _noindex

    if os.path.exists(page_src):
        fns = [f for f in os.listdir(page_src) if f.endswith('.md')]
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(render_page, fn, page_src, lang_out, url_prefix): fn for fn in fns}
            for fut in as_completed(futs):
                try:
                    url, title, title_en, _ni = fut.result()
                    all_links.append(url)
                    if _ni:
                        noindex_urls.add(url)
                    search_index.append({'title': title, 'title_en': title_en, 'url': url})
                except Exception as e:
                    print(f"[WARN] 页面渲染错误: {e}")

    def render_post(fn, src_dir, out_base, u_prefix):
        fp = os.path.join(src_dir, fn)
        p = frontmatter.load(fp)
        uid = _inject_uid(fp, p)
        html, toc = _render_md(p.content)
        post_dir = os.path.join(out_base, 'archive', uid)
        os.makedirs(post_dir, exist_ok=True)
        final_url = (u_prefix + f'archive/{uid}/').replace('//', '/')
        summary = _get_meta_desc(p.content, p.get('description', ''))
        _noindex = bool(p.get('noindex', False)) or 'noindex' in str(p.get('robots', ''))
        tags = p.get('tags', [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(',') if t.strip()]
        category = _extract_category(p)
        raw_date = p.get('date', '')
        date_str = str(raw_date) if raw_date else ''

        # 优先使用手工英文版，否则构建期自动翻译（命中本地缓存毫秒级）
        mp = manual_en_posts.get(uid) or manual_en_posts.get(fn)
        zh_title = str(p.get('title', '') or '')
        if mp is not None:
            html_en, toc_en = _render_md(mp.content)
            title_en = str(mp.get('title', '') or translator.translate_text(zh_title))
            summary_en = _get_meta_desc(mp.content, mp.get('description', ''))
        else:
            title_en, summary_en = translator.translate_many([zh_title, summary])
            html_en = translator.translate_html(html)
            toc_en = translator.translate_html(toc)

        category_en = translator.translate_text(category) if category else ''
        tags_en = translator.translate_many([str(t) for t in tags]) if tags else []

        meta = {**p.metadata,
                'url': final_url,
                'uid': uid,
                'title_en': title_en,
                'content': html,
                'content_en': html_en,
                'toc': toc,
                'toc_en': toc_en,
                'summary': summary,
                'summary_en': summary_en,
                'description': summary,
                'read_time': max(1, math.ceil(len(p.content) / 450)),
                'tags': tags,
                'tags_en': tags_en,
                'category': category,
                'category_en': category_en,
                'date': date_str,
                'is_page': False,
                'noindex': _noindex}
        _resolve_cover_url(meta, uid, post_bg_urls, random_img_api)
        with open(os.path.join(post_dir, 'index.html'), 'w', encoding='utf-8') as f:
            f.write(post_tpl.render(post=meta, i18n=I18N[lang], lang=lang,
                                    config=config, config_en=config_en, nav_pages=nav_pages,
                                    theme_colors=theme_colors, audio_tracks=audio_tracks))
        return final_url, meta, _noindex

    posts_data = []
    post_src = os.path.join(CONTENT_DIR, 'posts', 'zh')
    if os.path.exists(post_src):
        fns = [f for f in os.listdir(post_src) if f.endswith('.md')]
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(render_post, fn, post_src, lang_out, url_prefix): fn for fn in fns}
            for fut in as_completed(futs):
                try:
                    url, meta, _ni = fut.result()
                    posts_data.append(meta)
                    all_links.append(url)
                    if _ni:
                        noindex_urls.add(url)
                    search_index.append({
                        'title': meta.get('title', ''),
                        'title_en': meta.get('title_en', ''),
                        'url': url,
                    })
                except Exception as e:
                    print(f"[WARN] 文章渲染错误: {e}")

    def _date_key(val):
        m = re.match(r'^\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})', str(val or ''))
        return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)

    posts_data.sort(key=lambda x: (_date_key(x.get('date')), str(x.get('uid', ''))), reverse=True)

    # ── 分类 / 标签 / 年月归档 统计（含双语映射） ──
    categories_count = {}
    categories_en_map = {}
    tags_count = {}
    tags_en_map = {}
    archive_tree = {}
    for post in posts_data:
        cat = (post.get('category') or '').strip()
        if cat:
            categories_count[cat] = categories_count.get(cat, 0) + 1
            if cat not in categories_en_map:
                categories_en_map[cat] = post.get('category_en') or cat
        for idx_t, t in enumerate(post.get('tags') or []):
            t = str(t).strip()
            if t:
                tags_count[t] = tags_count.get(t, 0) + 1
                if t not in tags_en_map:
                    t_en_list = post.get('tags_en') or []
                    tags_en_map[t] = t_en_list[idx_t] if idx_t < len(t_en_list) else t
        d = str(post.get('date', ''))
        if len(d) >= 7 and d[4] == '-':
            y, m = d[:4], d[5:7]
            archive_tree.setdefault(y, {})
            archive_tree[y][m] = archive_tree[y].get(m, 0) + 1

    categories_list = sorted(categories_count.items(), key=lambda kv: (-kv[1], kv[0]))
    tags_list = sorted(tags_count.items(), key=lambda kv: (-kv[1], kv[0]))
    archive_list = []
    for y in sorted(archive_tree.keys(), reverse=True):
        months = archive_tree[y]
        archive_list.append({
            'year': y,
            'count': sum(months.values()),
            'months': [{'month': m, 'count': c} for m, c in sorted(months.items(), reverse=True)],
        })

    try:
        posts_per_page = max(1, int(str(config.get('posts_per_page', 9) or 9)))
    except (TypeError, ValueError):
        posts_per_page = 9

    with open(os.path.join(lang_out, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(idx_tpl.render(posts=posts_data, i18n=I18N[lang], lang=lang,
                               config=config, config_en=config_en, nav_pages=nav_pages,
                               categories_list=categories_list, categories_en_map=categories_en_map,
                               tags_list=tags_list, tags_en_map=tags_en_map,
                               archive_list=archive_list, posts_per_page=posts_per_page,
                               theme_colors=theme_colors, audio_tracks=audio_tracks))
    all_links.append(url_prefix)

    valid = [s for s in search_index if s.get('title')]
    with open(os.path.join(lang_out, 'search.json'), 'w', encoding='utf-8') as f:
        json.dump(valid, f, ensure_ascii=False, indent=2)

    # 兼容旧的 /en/ 书签：自动切换到英文模式并重定向回统一首页
    en_out = os.path.join(OUTPUT_DIR, 'en')
    os.makedirs(en_out, exist_ok=True)
    with open(os.path.join(en_out, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(
            '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
            '<script>try{localStorage.setItem("cnfte_lang","en");}catch(e){}location.replace("/");</script>'
            '<meta http-equiv="refresh" content="0;url=/"></head><body></body></html>'
        )

    translator.save()

    # ── SEO ──
    site_url = config.get('site_url', '').rstrip('/')
    if site_url:
        domain = site_url.replace('https://', '').replace('http://', '').split('/')[0]
        if domain and 'github.io' not in domain:
            with open(os.path.join(OUTPUT_DIR, 'CNAME'), 'w') as f:
                f.write(domain)
        _generate_seo(OUTPUT_DIR, site_url, all_links, noindex_urls, config, BASE_DIR)

    elapsed = time.time() - t_start
    post_count = len([l for l in all_links if 'archive' in l])
    print(f"[OK] 构建完成！{post_count} 篇文章，耗时 {elapsed:.2f}s")


def _generate_seo(out_dir: str, site_url: str, urls: list, noindex_urls: set,
                   config: dict, base_dir: str):
    now = datetime.datetime.now().strftime('%Y-%m-%d')
    unique = sorted(u for u in set(urls) if u not in noindex_urls)
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    full_urls = []
    for u in unique:
        loc = f"{site_url.rstrip('/')}/{u.lstrip('/')}"
        full_urls.append(loc)
        pri = '1.0' if u in ('/', '/index.html', '/en/', '/en/index.html') else (
              '0.9' if '/archive/' in u else '0.7')
        lines.append(f'  <url><loc>{loc}</loc><lastmod>{now}</lastmod>'
                     f'<changefreq>weekly</changefreq><priority>{pri}</priority></url>')
    lines.append('</urlset>')
    with open(os.path.join(out_dir, 'sitemap.xml'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    robots = (
        f"User-agent: *\n"
        f"Allow: /\n"
        f"Disallow: /search.json\n"
        f"Disallow: /en/search.json\n"
        f"Disallow: /config.json\n"
        f"Sitemap: {site_url}/sitemap.xml\n"
    )
    with open(os.path.join(out_dir, 'robots.txt'), 'w', encoding='utf-8') as f:
        f.write(robots)

    # ── IndexNow ──
    # 1. 把密钥验证文件写进构建产物（{key}.txt），IndexNow 用它来确认站点归属；
    # 2. 把完整 URL 列表缓存到项目根目录（不进 public/，不会被推送进部署仓库），
    #    main.py 在推送成功后读取这份缓存去调用 IndexNow API。
    indexnow_key = str(config.get('indexnow_key', '') or '').strip()
    if indexnow_key and config.get('enable_indexnow', True):
        with open(os.path.join(out_dir, f'{indexnow_key}.txt'), 'w', encoding='utf-8') as f:
            f.write(indexnow_key)
        try:
            with open(os.path.join(base_dir, '.indexnow_urls.json'), 'w', encoding='utf-8') as f:
                json.dump(full_urls, f, ensure_ascii=False)
        except Exception:
            pass


if __name__ == '__main__':
    build()