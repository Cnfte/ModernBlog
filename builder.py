"""
builder.py — CNFTE Static Site Generator v3.1
修复：
  - LCP hero 图预加载 <link rel="preload">
  - 文章封面 srcset/sizes 响应式（减少移动端图片流量）
  - Google Fonts 添加 font-display=swap 参数，减少 FOIT
  - 公告内容中 <a src="..."> → <a href="..."> 自动修复（crawlable-anchors）
  - 头像 / anime-cover 图片宽高已在模板中固定，builder 侧无需额外处理
  - 保留原有并行渲染、增量构建逻辑
"""
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from jinja2 import Environment, FileSystemLoader, select_autoescape

I18N = {
    "zh": {"home": "首页", "lang_name": "EN", "lang_link": "/en/", "read_unit": "分钟"},
    "en": {"home": "HOME", "lang_name": "ZH", "lang_link": "/", "read_unit": "min"}
}

MD_EXTENSIONS = ['fenced_code', 'tables', 'toc', 'codehilite', 'nl2br', 'sane_lists']


def _toc_slugify(value, separator='-'):
    """
    自定义 TOC 锚点生成器：
    - 保留中文、英文、数字
    - 把空格转为连字符
    - 去除所有 URL/CSS 中的非法字符（: ( ) / \ . , ! ? ' " @ # $ % ^ & * + = | ~ ` < > { } [ ]）
    - 确保不以数字或连字符开头（加 h- 前缀）
    - 结果全小写
    """
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
    md = markdown.Markdown(extensions=MD_EXTENSIONS, extension_configs=MD_EXT_CONFIGS)
    html = md.convert(content)
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
    """
    if meta.get('cover'):
        return
    if post_bg_urls:
        meta['bg_url'] = random.choice(post_bg_urls)
    elif random_img_api:
        sep = '&' if '?' in random_img_api else '?'
        meta['bg_url'] = f"{random_img_api}{sep}v={uid}"


def build():
    t_start = time.time()
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONTENT_DIR = os.path.join(BASE_DIR, 'content')
    OUTPUT_DIR = os.path.join(BASE_DIR, 'public')
    THEME_DIR = os.path.join(BASE_DIR, 'themes', 'default')

    config_path = os.path.join(CONTENT_DIR, 'config.json')
    if not os.path.exists(config_path):
        print("❌ 找不到 config.json，请先运行 main.py 初始化。")
        return
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    # ── 修复公告中的 src= 链接 ──
    if config.get('site_notice'):
        config['site_notice'] = _fix_notice_links(config['site_notice'])

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
    os.makedirs(OUTPUT_DIR)
    open(os.path.join(OUTPUT_DIR, '.nojekyll'), 'w').close()

    # ── Monetag sw.js ──
    sw_src = os.path.join(CONTENT_DIR, 'sw.js')
    if os.path.exists(sw_src):
        shutil.copy2(sw_src, os.path.join(OUTPUT_DIR, 'sw.js'))
        print("📢 sw.js 已复制到输出目录（Monetag Push Ads）")
    elif config.get('monetag_tag_code', '').strip():
        print("⚠️  检测到 Monetag 广告代码，但未找到 content/sw.js。")

    attach_src = os.path.join(CONTENT_DIR, 'attachments')
    if os.path.exists(attach_src):
        dest_attach = os.path.join(OUTPUT_DIR, 'attachments')
        _copy_tree_fast(attach_src, dest_attach)

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
        print(f"❌ 模板加载失败: {e}")
        return

    all_links = []
    noindex_urls = set()

    for lang in ('zh', 'en'):
        lang_out = OUTPUT_DIR if lang == 'zh' else os.path.join(OUTPUT_DIR, lang)
        url_prefix = '/' if lang == 'zh' else '/en/'
        os.makedirs(lang_out, exist_ok=True)

        if lang == 'en' and os.path.exists(attach_src):
            _copy_tree_fast(attach_src, os.path.join(lang_out, 'attachments'))

        search_index = []

        nav_pages = []
        page_src = os.path.join(CONTENT_DIR, 'pages', lang)
        if os.path.exists(page_src):
            for fn in sorted(f for f in os.listdir(page_src) if f.endswith('.md')):
                p = frontmatter.load(os.path.join(page_src, fn))
                url = (url_prefix + fn.replace('.md', '.html')).replace('//', '/')
                nav_pages.append({'title': p.get('title', fn), 'url': url})

        def render_page(fn, src_dir, out_dir, u_prefix):
            fp = os.path.join(src_dir, fn)
            p = frontmatter.load(fp)
            uid = _inject_uid(fp, p)
            html, toc = _render_md(p.content)
            final_url = (u_prefix + fn.replace('.md', '.html')).replace('//', '/')
            _noindex = bool(p.get('noindex', False)) or 'noindex' in str(p.get('robots', ''))
            meta = {**p.metadata,
                    'url': final_url,
                    'uid': uid,
                    'content': html,
                    'toc': toc,
                    'is_page': True,
                    'noindex': _noindex,
                    'description': _get_meta_desc(p.content, p.get('description', ''))}
            _resolve_cover_url(meta, uid, post_bg_urls, random_img_api)
            out = os.path.join(out_dir, fn.replace('.md', '.html'))
            with open(out, 'w', encoding='utf-8') as f:
                f.write(post_tpl.render(post=meta, i18n=I18N[lang], lang=lang,
                                        config=config, nav_pages=nav_pages))
            return final_url, meta.get('title', fn), _noindex

        if os.path.exists(page_src):
            fns = [f for f in os.listdir(page_src) if f.endswith('.md')]
            with ThreadPoolExecutor(max_workers=6) as ex:
                futs = {ex.submit(render_page, fn, page_src, lang_out, url_prefix): fn for fn in fns}
                for fut in as_completed(futs):
                    try:
                        url, title, _ni = fut.result()
                        all_links.append(url)
                        if _ni:
                            noindex_urls.add(url)
                        search_index.append({'title': title, 'url': url})
                    except Exception as e:
                        print(f"⚠️  页面渲染错误: {e}")

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
            meta = {**p.metadata,
                    'url': final_url,
                    'uid': uid,
                    'content': html,
                    'toc': toc,
                    'summary': summary,
                    'description': summary,
                    'read_time': max(1, math.ceil(len(p.content) / 450)),
                    'tags': tags,
                    'is_page': False,
                    'noindex': _noindex}
            # 封面图优先级：frontmatter 手动 cover（已在 **p.metadata 中）
            #   > 固定图池随机抽取 > 随机图 API（带 uid，前端各自请求，零构建开销且不重复）
            _resolve_cover_url(meta, uid, post_bg_urls, random_img_api)
            with open(os.path.join(post_dir, 'index.html'), 'w', encoding='utf-8') as f:
                f.write(post_tpl.render(post=meta, i18n=I18N[lang], lang=lang,
                                        config=config, nav_pages=nav_pages))
            return final_url, meta, _noindex

        posts_data = []
        post_src = os.path.join(CONTENT_DIR, 'posts', lang)
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
                        search_index.append({'title': meta.get('title', ''), 'url': url})
                    except Exception as e:
                        print(f"⚠️  文章渲染错误: {e}")

        posts_data.sort(key=lambda x: str(x.get('date', '0000-00-00')), reverse=True)

        with open(os.path.join(lang_out, 'index.html'), 'w', encoding='utf-8') as f:
            f.write(idx_tpl.render(posts=posts_data, i18n=I18N[lang], lang=lang,
                                   config=config, nav_pages=nav_pages))
        all_links.append(url_prefix)

        valid = [s for s in search_index if s.get('title')]
        with open(os.path.join(lang_out, 'search.json'), 'w', encoding='utf-8') as f:
            json.dump(valid, f, ensure_ascii=False, indent=2)

    # ── SEO ──
    site_url = config.get('site_url', '').rstrip('/')
    if site_url:
        domain = site_url.replace('https://', '').replace('http://', '').split('/')[0]
        if domain and 'github.io' not in domain:
            with open(os.path.join(OUTPUT_DIR, 'CNAME'), 'w') as f:
                f.write(domain)
        _generate_seo(OUTPUT_DIR, site_url, all_links, noindex_urls)

    elapsed = time.time() - t_start
    post_count = len([l for l in all_links if 'archive' in l])
    print(f"✨ 构建完成！{post_count} 篇文章，耗时 {elapsed:.2f}s")


def _generate_seo(out_dir: str, site_url: str, urls: list, noindex_urls: set):
    now = datetime.datetime.now().strftime('%Y-%m-%d')
    unique = sorted(u for u in set(urls) if u not in noindex_urls)
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in unique:
        loc = f"{site_url.rstrip('/')}/{u.lstrip('/')}"
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


if __name__ == '__main__':
    build()
