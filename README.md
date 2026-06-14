# ModernBlogPanel

本文档面向所有使用 ModernBlogPanel 的用户，涵盖安装部署、后台操作、配置项详解、内容管理、部署流程等全部内容。阅读本文档前无需任何编程基础，按步骤操作即可完成完整博客站点的搭建与日常维护。

---

## 目录

1. 项目简介
2. 环境要求与安装
3. 启动后台管理面板
4. 后台界面概览
5. 配置项详解
6. 内容管理使用教程
7. 素材库使用说明
8. 追番列表管理
9. 友链管理
10. 构建与部署流程
11. Cloudflare 缓存清理
12. 广告代码与 sw.js
13. 日志系统
14. 目录结构说明
15. 常见问题

---

## 一、项目简介

ModernBlogPanel 是一款基于 Python + Flask 的静态博客生成系统，内置 Web 后台管理界面，无需数据库，无需服务器端渲染，最终产物为纯静态 HTML 文件，可直接部署到 GitHub Pages、Cloudflare Pages 等任意静态托管平台，无需像hexo那般面对黑乎乎的终端，只需简单几步即可获得动态博客般的后台管理体验，小白轻松上手。

核心特性如下：

- 双语支持：中文（zh）与英文（en）双语站点，共用同一套主题模板，自动生成各自独立的页面与索引。
- Markdown 写作：全部文章和页面均使用 Markdown 格式编写，支持代码高亮、表格、目录、围栏代码块等扩展语法。
- 图形化后台：通过浏览器访问本地后台，完成文章新建、编辑、删除、构建、推送一条龙操作，无需手动操作命令行。
- 一键部署：填写 GitHub 仓库地址后，点击"推送"按钮即可将构建产物强制推送至 GitHub Pages。
- SEO 友好：自动生成 sitemap.xml、robots.txt，内置 Open Graph 标签、JSON-LD 结构化数据、hreflang 多语言声明。
- Cloudflare 集成：支持在后台直接触发 Cloudflare 全站缓存清理，无需登录 CF 控制台。

---

## 二、环境要求与安装

### 系统要求

- Python 3.10 或更高版本
- Git（用于推送至 GitHub）
- 操作系统：Windows、macOS、Linux 均支持

### 安装依赖

先git本项目源码至本地

```bash
git clone https://github.com/Cnfte/ModernBlog.git
```
在项目根目录执行以下命令安装所需 Python 库：

```bash
pip install -r requirements.txt
```
### 文件结构

确保以下文件位于同一目录下：

```
项目根目录/
├── main.py          后台管理服务
├── builder.py       静态站点构建引擎
└── themes/
    └── default/
        ├── index.html    首页模板
        └── post.html     文章页模板
```

---

## 三、启动后台管理面板

在项目根目录执行：

```bash
python main.py
```

启动成功后，终端会输出以下信息：

```
=======================================================
  CNFTE GEEK_ADMIN // MOE_SYSTEM v8.0.0
  Flask WebUI Edition
  访问地址: http://0.0.0.0:32323
  本机访问: http://127.0.0.1:32323
=======================================================
```

用浏览器打开 `http://127.0.0.1:32323` 即可进入后台管理界面。

首次运行时，程序会自动完成以下初始化操作：

- 创建 `content/posts/zh`、`content/posts/en` 文章目录
- 创建 `content/pages/zh`、`content/pages/en` 页面目录
- 创建 `content/attachments` 素材目录
- 生成默认 `content/config.json` 配置文件

---

## 四、后台界面概览

后台顶部为标签导航栏，共分六个功能区：

| 标签 | 功能说明 |
|------|----------|
| 内容 | 编写、编辑、删除文章与页面，触发构建和部署 |
| 素材 | 上传图片等文件，获取可用于 Markdown 的链接 |
| 配置 | 填写站点名称、URL、社交账号、部署参数等全部配置项 |
| 追番 | 管理正在追看的动画列表，展示在站点侧边栏或专属页面 |
| 友链 | 管理友情链接，填写友站名称、网址和描述 |
| 日志 | 实时查看构建、部署、缓存清理等操作的执行日志 |

---

## 五、配置项详解

所有配置项保存在 `content/config.json`，可在后台"配置"标签中通过图形界面修改，保存后立即写入文件。以下逐项说明每个配置字段的用途与填写规范。

---

### 基础站点

**site_name**
站点标题，显示在浏览器标签页标题、Open Graph 分享卡片以及 JSON-LD 结构化数据中。填写你的博客名称即可，例如 `「代码与星空」` 或 `Alex's Blog`。建议不超过 30 个字，避免在搜索引擎结果页被截断。

**site_url**
站点完整访问地址，必须包含协议头，例如 `https://username.github.io` 或 `https://blog.example.com`。该字段用于生成 sitemap.xml 中的绝对 URL、Canonical 标签、hreflang 声明以及 robots.txt 中的 Sitemap 地址。填写错误会导致搜索引擎收录异常，请务必确认地址与实际部署地址一致。如果使用自定义域名，填写自定义域名；如果是 GitHub Pages 默认域名，填写 `https://用户名.github.io`。

**logo_text**
导航栏左上角显示的 Logo 文字。通常填写站点缩写、英文名称或符号性文字，例如 `GEEK.LOG` 或 `STELLA`。该字段仅影响视觉展示，不影响 SEO。

**start_date**
建站日期，格式为 `YYYY-MM-DD`，例如 `2024-03-15`。该字段会显示在页脚或博主信息卡片中，用于展示站龄信息。填写实际开始运营博客的日期即可。

**hero_title**
首页 Hero 区域（顶部大图横幅）的主标题文字。通常是一句有个性的欢迎语或口号，例如 `WELCOME TO THE VOID` 或 `KEEP CODING, STAY CURIOUS`。支持大写英文风格，字数建议控制在 20 个字以内以保证视觉效果。

**hero_subtitle**
Hero 区域主标题下方的副标题文字。可以是对站点的一句话简介，或者配合主标题的补充语，例如 `一个程序员的日常记录` 或 `Code · Anime · Life`。

**site_keywords**
站点 SEO 关键词，多个关键词之间用英文逗号分隔，例如 `技术博客, 前端开发, Python, 动漫, 二次元`。这些关键词会写入 `<meta name="keywords">` 标签。现代搜索引擎对 keywords 标签的权重已大幅降低，但填写规范的关键词仍有助于部分搜索引擎的分类识别。建议填写 5 至 10 个与站点内容高度相关的词语。

**site_description**
站点 SEO 描述文字，一段简短介绍站点内容的话，建议 80 至 155 个字符之间。该内容会写入 `<meta name="description">` 标签，并作为 Open Graph 分享卡片的描述文字。这是影响搜索引擎点击率最重要的元数据之一，请认真撰写，清晰描述站点的主要内容与受众。例如：`一个专注于前端开发与动画文化的个人博客，分享技术教程、观影记录与生活随笔。`

**bg_url**
全站默认背景图片的 URL 地址。当文章没有单独设置背景图时，将使用此处填写的图片作为兜底。可填写任意可公开访问的图片直链，建议使用宽高比接近 16:9 的横版图片，分辨率不低于 1920x1080。也可使用随机图片 API，例如 `https://picsum.photos/1920/1080`。

**hero_bg_url**
专门用于首页 Hero 区域的背景图 URL。如果留空，首页 Hero 区域将使用 `bg_url` 字段指定的图片。如果希望首页与文章页使用不同的背景图，在此处单独填写首页背景图地址即可。

**post_bg_urls**
文章页随机背景图列表。每行填写一个图片 URL，或者填写一个或多个随机图 API 地址。构建时，每篇文章会从这个列表中随机抽取一张图片作为其背景，使得不同文章在视觉上产生差异化。如果此列表为空，文章页将回退到 `bg_url` 字段指定的背景图。多个地址示例：

```
https://picsum.photos/1920/1080?random=1
https://picsum.photos/1920/1080?random=2
https://source.example.com/wallpaper/001.jpg
```

**og_image**
Open Graph 封面图的 URL 地址。当博客链接被分享到微信、Twitter、Telegram、Discord 等平台时，会自动拉取此图片作为预览卡片的封面图。建议使用尺寸不小于 1200x630 像素的横版图片，图片中可包含站点 Logo 或名称。填写完整的 https 地址。

---

### 页脚内容

**footer_text**
页脚区域的自定义文字，支持 HTML 标签。例如可以填写版权声明、ICP 备案号、联系邮箱等。链接请使用 `href=` 属性，例如：

```html
本站由 GitHub Pages 强力驱动 · <a href="https://beian.miit.gov.cn" target="_blank">京ICP备XXXXXXXX号</a>
```

如果留空，系统会回退使用旧版的 `footer_custom` 字段内容（向后兼容旧配置文件）。

**footer_custom**
旧版页脚配置字段，仅在 `footer_text` 为空时生效，用于兼容早期配置文件。新用户直接使用 `footer_text` 即可，无需关注此字段。

---

### 弹窗公告与通知

**site_notice**
弹窗公告内容，支持 HTML。首次访问站点时会以弹窗形式展示给访客。适合用于发布重要通知、近期活动、站点迁移提醒等信息。链接请务必使用 `href=` 属性而非 `src=`，系统会自动修复 `src=` 的错误写法，但为了规范建议直接写正确的形式。如果不需要弹窗公告，留空即可。

**show_notice_widget**
布尔值，控制是否在侧边栏同时显示一个公告 Widget。勾选后，`site_notice` 中的内容不仅会在弹窗中出现，还会在文章页侧边栏固定展示一个小公告卡片。适合长期需要在侧边栏展示固定信息（如赞助渠道、重要链接）的场景。

---

### 博主信息

**username**
博主 ID 或昵称，显示在侧边栏博主信息卡片、文章元信息等位置。填写你在博客中希望展示的名字，例如 `Stellaria` 或 `老王`。

**avatar_url**
博主头像图片的 URL 地址。建议使用正方形或圆形裁剪的头像图片，尺寸不低于 200x200 像素。可使用 GitHub 头像地址（`https://github.com/用户名.png`）或其他图床地址。

**bio**
博主个人简介，一句话介绍自己，显示在侧边栏博主卡片中。例如 `热爱编程与动漫的独立开发者` 或 `Keep it simple. Keep it real.`，建议不超过 50 个字。

**email**
联系邮箱地址，例如 `me@example.com`。填写后会在博主信息卡片中生成邮件联系图标链接。如不希望公开邮箱，留空即可。

**github_url**
GitHub 主页完整 URL，例如 `https://github.com/username`。填写后会在社交图标栏生成 GitHub 链接。

**telegram_url**
Telegram 频道或个人主页的完整 URL，例如 `https://t.me/channel_name`。填写后生成 Telegram 图标链接。

**bilibili_url**
Bilibili 个人主页的完整 URL，例如 `https://space.bilibili.com/123456789`。面向中文受众的博主推荐填写。

**twitter_url**
Twitter（现更名为 X）个人主页完整 URL，例如 `https://twitter.com/username` 或 `https://x.com/username`。

**rss_url**
RSS 订阅地址。如果你的托管平台提供 RSS 功能，或者你手动维护了一个 feed.xml，填写其完整访问地址，例如 `/feed.xml` 或 `https://blog.example.com/feed.xml`。填写后会在导航区显示 RSS 图标供访客订阅。

---

### 部署与 Cloudflare

**deploy_repo**
GitHub 仓库的 SSH 克隆地址，用于将构建产物推送至 GitHub Pages。格式为 `git@github.com:用户名/仓库名.git`，例如 `git@github.com:alice/alice.github.io.git`。请确保当前运行环境已配置好对应 GitHub 账号的 SSH 密钥，否则推送会因权限不足而失败。如果使用的是 HTTPS 地址，需要在系统中配置 Git 凭证管理器。

**git_user_name**
推送时使用的 Git 用户名，即 `git config user.name` 的值。填写你的真实姓名或 GitHub 昵称，例如 `Alice Wang`。该信息会出现在每次部署的 commit 记录中。

**git_user_email**
推送时使用的 Git 邮箱，即 `git config user.email` 的值。建议填写与 GitHub 账号绑定的邮箱，例如 `alice@example.com`，否则 commit 记录中的作者信息将无法与 GitHub 账号关联。

**cf_zone_id**
Cloudflare 站点的 Zone ID。登录 Cloudflare 控制台，在对应域名的"概述"页面右侧可以找到 Zone ID，格式为 32 位十六进制字符串，例如 `a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4`。填写后可在后台一键触发全站缓存清理。

**cf_api_token**
Cloudflare API Token。在 Cloudflare 个人资料页"API 令牌"中创建，权限需包含"Cache Purge"。Token 是敏感信息，请勿泄露。在后台界面中该字段以密码输入框形式展示，内容不会明文显示。

**cf_email**
Cloudflare 账号邮箱（可选）。如果使用的是 Global API Key 而非 API Token，则需要同时填写邮箱和 Key。如果使用的是 API Token（推荐），此字段留空即可。

---

### 广告代码

**monetag_tag_code**
Monetag 或其他广告平台的 `<script>` 标签代码。填写后，该代码会被注入到每个页面 `<head>` 标签的末尾。支持完整的脚本标签，例如：

```html
<script src="https://cdn.monetag.com/xxx.js" async></script>
```

如果不需要广告，留空即可。

---

## 六、内容管理使用教程

内容管理是后台最核心的功能区，涵盖文章与页面的新建、编辑、保存、删除以及触发构建部署的完整工作流。下面按照实际操作顺序逐步讲解。

### 6.1 内容类型说明

系统将内容分为两种类型：

- **文章（posts）**：博客的主要内容单元，有发布日期和标签，按时间倒序排列在首页，每篇文章生成一个独立的 `/archive/UID/` 路径。
- **页面（pages）**：独立的静态页面，不出现在文章列表中，路径直接对应文件名，例如 `about.md` 生成 `/about.html`。适合"关于我"、"版权声明"、"项目展示"等固定内容。

两种类型都支持中文（zh）和英文（en）两个语言版本，互相独立管理。

### 6.2 切换语言和内容类型

在"内容"标签左侧面板顶部有两个下拉菜单：

- 第一个下拉菜单选择语言：`ZH 中文` 或 `EN 英文`
- 第二个下拉菜单选择类型：`文章` 或 `页面`

切换后，左侧文件列表会自动刷新，显示对应目录下的所有 Markdown 文件。中文文章存储在 `content/posts/zh/`，英文文章存储在 `content/posts/en/`，页面同理。

### 6.3 新建文章

点击左侧面板中的"+ 新建"按钮，右侧编辑区会清空，进入新建模式。按以下步骤填写内容：

**第一步：填写标题**
在"文章标题"输入框中输入文章标题，这是必填项。标题会写入 Markdown 文件的 front matter，同时作为页面的 `<title>` 标签内容和首页文章卡片中显示的名称。

**第二步：填写发布日期**
在"发布日期"输入框中填写日期，格式严格为 `YYYY-MM-DD`，例如 `2025-06-13`。系统会根据日期对首页文章列表进行排序，日期越新的文章排在越前面。如果留空，文章的排序位置将不可预测，建议每篇文章都填写日期。

**第三步：填写标签**
在"标签"输入框中填写标签，多个标签之间用英文逗号分隔，例如 `技术, Python, 教程`。标签会显示在文章卡片和文章页面上，帮助访客快速了解文章主题。标签也可以留空。

**第四步：撰写正文**
在下方的大型文本区域中输入 Markdown 格式的正文内容。系统支持以下 Markdown 扩展语法：

- **围栏代码块**：使用三个反引号包裹，可指定语言实现语法高亮，例如：
  ```
  ```python
  print("Hello World")
  ```
  ```
- **表格**：标准 GFM 表格语法
- **目录**：在正文中插入 `[TOC]` 会自动生成文章目录
- **代码高亮**：依赖 Pygments，构建时自动处理
- **自动换行**：段落内的单个换行会被保留（nl2br 扩展）
- **图片懒加载**：构建时自动为所有 `<img>` 标签添加 `loading="lazy"` 和 `decoding="async"` 属性

**第五步：保存文件**
点击右侧编辑区顶部的"保存"按钮。首次保存时，系统会自动生成一个唯一标识符（UID），该 UID 决定文章的访问路径（`/archive/UID/`）。保存成功后，左侧文件列表会出现新文章，右下角状态栏会显示保存成功提示。

文章以 Markdown 文件形式保存在本地目录中，你也可以直接用文本编辑器打开 `content/posts/zh/` 目录下的 `.md` 文件进行编辑，文件头部的 YAML front matter 格式如下：

```yaml
---
title: 我的第一篇文章
date: 2025-06-13
tags:
  - 技术
  - Python
uid: "174233xxxxxx"
---

正文内容从这里开始...
```

### 6.4 编辑已有文章

在左侧文件列表中点击任意文章条目，右侧编辑区会自动加载该文章的标题、日期、标签和正文内容。直接修改后点击"保存"按钮即可覆盖原文件。

文章的 UID 一旦生成便不会改变（除非手动修改 front matter），因此文章的访问 URL 在保存后始终保持不变，不会因为修改标题或内容而失效。

### 6.5 删除文章

在左侧文件列表中点击选中要删除的文章，然后点击右侧编辑区顶部的"删除"按钮。系统会弹出确认对话框，确认后该文章的 Markdown 源文件将被永久删除。注意：删除操作不可撤销，且不会自动触发构建，已删除的文章在下次构建前仍会存在于 `public/` 目录中。

### 6.6 管理独立页面

在左侧面板的类型下拉菜单中选择"页面"，操作方式与文章完全相同。页面文件名（不含 `.md` 扩展名）直接决定页面的访问路径，例如文件名为 `about.md` 则访问路径为 `/about.html`。建议使用英文小写字母和连字符命名页面文件，避免中文文件名。

### 6.7 触发构建

内容编辑完成后，需要触发构建才能将 Markdown 源文件转换为可访问的静态 HTML 文件。点击编辑区顶部的"构建"按钮，后台会立即启动构建流程。构建过程是异步执行的，不会阻塞界面操作。

切换到"日志"标签可以实时查看构建进度和结果。构建完成后终端会输出类似以下内容：

```
[10:32:15] 开始构建...
[10:32:16] ✨ 构建完成！12 篇文章，耗时 1.23s
[10:32:16] 构建完成
```

构建产物输出到项目根目录下的 `public/` 文件夹，包含完整的静态站点文件。

### 6.8 推送至 GitHub Pages

构建完成后，点击"推送"按钮，系统会在 `public/` 目录中初始化 Git 仓库，将所有文件提交，并强制推送到配置项中 `deploy_repo` 指定的 GitHub 仓库 main 分支。每次推送的 commit message 格式为 `update: YYYY-MM-DD HH:MM`。

推送完成后，GitHub Actions 通常会在 1 至 3 分钟内自动完成 Pages 的部署更新。

---

## 七、素材库使用说明

"素材"标签用于管理上传至 `content/attachments/` 目录的文件（主要是图片）。

点击"上传文件"按钮选择本地文件，支持一次性上传多个文件。上传完成后，文件会出现在下方的素材网格中。点击任意素材条目，上方的"生成的 Markdown / URL 链接"输入框会自动填充该文件对应的 Markdown 图片引用代码，格式如下：

```
![文件名](/attachments/文件名.jpg)
```

点击"复制"按钮将链接复制到剪贴板，然后粘贴到文章正文中即可引用该图片。

构建时，`content/attachments/` 目录中的所有文件会被复制到 `public/attachments/` 目录，使其在最终站点中可以通过 `/attachments/文件名` 路径访问。

如需删除某个素材，点击选中后点击"删除选中"按钮即可从服务器上删除该文件。

---

## 八、追番列表管理

"追番"标签用于维护正在追看的动画列表。该列表数据会通过模板渲染到站点的追番页面或侧边栏中。

点击"+ 添加番剧"打开添加对话框，需填写以下字段：

- **番剧名称**：动画的完整名称，例如 `进击的巨人 最终季`
- **当前集数**：当前已看到的集数，例如 `12`
- **总集数**：该番剧的总集数，例如 `16`，如果还未完结可填写 `?`
- **封面图 URL**：番剧封面图片的直链地址，可从 Bangumi、AniList 等站点获取
- **番剧链接**：指向番剧详情页的 URL

添加后，列表中的条目可以通过"上移"、"下移"按钮调整排列顺序，通过"编辑"按钮修改内容，通过"删除"按钮移除条目。所有修改完成后点击"保存"按钮，数据会立即写入配置文件。重新构建后变更会反映在站点页面中。

---

## 九、友链管理

"友链"标签用于管理友情链接列表。

点击"+ 添加友链"打开添加对话框，需填写：

- **名称**：友站的名称或博主昵称
- **URL**：友站的完整访问地址，例如 `https://friend.example.com`
- **描述**：一句话介绍友站，例如 `专注 Rust 开发的技术博客`
- **头像 URL**（可选）：友站或博主头像图片链接

友链数据同样支持排序、编辑、删除操作，修改后点击"保存"并重新构建即可生效。

---

## 十、构建与部署流程

### 构建原理

构建由 `builder.py` 负责执行，完整流程如下：

1. 读取 `content/config.json` 配置文件
2. 清空 `public/` 目录（保留 `.nojekyll`）
3. 复制 `content/attachments/` 至 `public/attachments/`
4. 遍历 `content/posts/zh/` 和 `content/posts/en/` 下的所有 `.md` 文件
5. 解析 front matter，渲染 Markdown 正文为 HTML
6. 使用 Jinja2 模板引擎渲染最终页面，输出至 `public/archive/UID/index.html`
7. 遍历 `content/pages/` 下的页面文件，类似方式处理
8. 渲染首页 `index.html` 和英文首页 `en/index.html`
9. 生成各语言的 `search.json` 搜索索引
10. 生成 `sitemap.xml` 和 `robots.txt`
11. 如有自定义域名，生成 `CNAME` 文件

构建使用多线程并发处理（最大 6 线程），大量文章时构建速度较快。

### 部署原理

点击"推送"后，系统在 `public/` 目录执行以下 Git 操作：

```
git init
git config user.name <git_user_name>
git config user.email <git_user_email>
git add .
git commit -m "update: YYYY-MM-DD HH:MM"
git branch -M main
git remote add origin <deploy_repo>
git push -f origin main
```

使用强制推送（`-f`），每次部署都会覆盖远程仓库的历史记录，保持仓库干净，不积累历史 commit。

---

## 十一、Cloudflare 缓存清理

如果你的站点通过 Cloudflare 代理访问，部署完成后需要清理 CDN 缓存，访客才能立即看到最新内容。

在"配置"标签中填写 `cf_zone_id`、`cf_api_token` 后，在"内容"标签顶部点击"清理CF"按钮（或在日志标签中操作），系统会向 Cloudflare API 发送全站缓存清理请求。操作结果会实时显示在"日志"标签中。

---

## 十二、广告代码与 sw.js

**monetag_tag_code 字段**接受完整的 `<script>` HTML 标签。填写后，该代码会注入到每个生成页面的 `<head>` 末尾，无需手动修改模板文件。

**sw.js 上传**：Monetag 的 Push Ads 功能需要在站点根目录放置一个 `sw.js` Service Worker 文件。在"配置"标签广告代码区域下方，可以点击"上传 sw.js"按钮将该文件上传至 `content/sw.js`。构建时会自动将其复制到 `public/sw.js`。点击"删除 sw.js"可移除该文件。当前 sw.js 的存在状态会在按钮旁实时显示。

---

## 十三、日志系统

"日志"标签通过 SSE（Server-Sent Events）技术实现实时日志推送。打开该标签后，后台所有操作（构建、部署、配置保存、文件上传等）的执行日志会即时流式显示在页面中，无需手动刷新。

日志颜色说明：

- 绿色（含"OK"或"完成"）：操作成功
- 红色（含"FAIL"或"失败"）：操作出错
- 青色：普通状态信息

日志面板自动滚动到最新条目，方便追踪长时间运行任务的进度。

---

## 十四、目录结构说明

```
项目根目录/
├── main.py                   后台管理服务入口
├── builder.py                静态站点构建引擎
├── content/                  所有内容和配置（不对外发布）
│   ├── config.json           站点配置文件
│   ├── posts/
│   │   ├── zh/               中文文章（.md 文件）
│   │   └── en/               英文文章（.md 文件）
│   ├── pages/
│   │   ├── zh/               中文独立页面（.md 文件）
│   │   └── en/               英文独立页面（.md 文件）
│   ├── attachments/          素材文件（图片等）
│   └── sw.js                 Monetag Service Worker（可选）
├── themes/
│   └── default/
│       ├── index.html        首页 Jinja2 模板
│       └── post.html         文章页 Jinja2 模板
└── public/                   构建输出目录（部署此目录）
    ├── index.html
    ├── en/
    │   └── index.html
    ├── archive/
    │   └── <UID>/
    │       └── index.html
    ├── attachments/
    ├── search.json
    ├── sitemap.xml
    ├── robots.txt
    └── CNAME（有自定义域名时生成）
```

---

## 十五、常见问题

**Q：点击"推送"后提示"请先配置 GitHub 仓库 SSH 地址"**

请检查"配置"标签中的 `deploy_repo` 字段是否已填写 SSH 格式的仓库地址（以 `git@github.com:` 开头），并确认当前系统已配置好对应账号的 SSH 密钥。可在终端执行 `ssh -T git@github.com` 验证 SSH 连接是否正常。

**Q：构建成功但网站没有更新**

构建只是生成了本地的 `public/` 目录，还需要点击"推送"将其发布到 GitHub。如果已推送但仍未更新，请检查 GitHub Pages 的部署来源设置是否指向正确的分支（main），以及 GitHub Actions 是否有报错。如果使用了 Cloudflare 代理，还需要清理 CF 缓存。

**Q：文章顺序不对**

文章按 `date` 字段的字符串值降序排列。请确保日期格式为标准的 `YYYY-MM-DD`，非标准格式（例如 `2025/6/13`）可能导致排序异常。

**Q：构建时提示"找不到 config.json"**

请先运行 `python main.py` 启动后台，系统会在首次启动时自动初始化 `content/config.json`。不要尝试直接运行 `python builder.py`，除非确认 config.json 已存在。

**Q：Cloudflare 缓存清理提示权限错误**

请检查 API Token 的权限范围，确保包含 `Zone.Cache Purge` 权限，且 Token 对应的域名与填写的 Zone ID 匹配。使用全局 API Key 时还需在 `cf_email` 字段填写 Cloudflare 账号邮箱。

**Q：文章图片不显示**

请确认图片路径正确。若图片位于 `content/attachments/`，在 Markdown 中应使用 `/attachments/文件名.jpg` 的绝对路径引用，而非相对路径。也可以使用"素材"标签自动生成的链接代码，避免路径错误。

---

*本文档基于 ModernBlogPanel Ver1.0 编写，适用于 main.py 与 builder.py 的当前版本。*

## LICENSE

**本项目采用`GNU GENERAL PUBLIC LICENSE Version 3`二改请严格遵守本协议执行，未经允许禁止使用本工具盈利或商用，违者依法追究**
| 权限          | 局限性     | 条件                  |
|---------------|------------|-----------------------|
| ✓ 商业用途   | ✗ 责任    | ① 许可与版权声明    |
| ✓ 改装       | ✗ 保修    | ① 州变更             |
| ✓ 分布       |            | ① 披露来源           |
| ✓ 专利使用   |            | ① 同一许可           |
| ✓ 私人使用   |            |                       |
