# 经济学人周报自动化抓取与归档

> 把《经济学人》每周新刊的非政治/非商业板块(科技、文化、书评、讣告、图表、长文 briefing)自动抓下来,
> 生成中文深度摘要,持久化到本地 `database.js`,可选推飞书,双击 `index.html` 离线浏览。

---

## 1. 项目特点

- **零后端**:不部署服务器、不依赖网络服务
- **零依赖前端**:`index.html` 双击即跑,纯原生 JS + 内联 marked.js,无 Vue/React
- **离线优先**:除大模型 API 外,其余链路都能离线完成
- **可读可搜索**:中文总结 + 英文原文并列,全文模糊搜索 + 关键词高亮
- **可审计**:每次抓取都有可重放的 source URL
- **逐篇落盘**:每抓一篇立即写盘,刷新 `index.html` 实时可见
- **去重**:URL 全局去重,重复抓取自动跳过,省 LLM token 和抓取次数

## 2. ⚠️ 安全与合规

| 项 | 规则 |
|---|------|
| `.env` | 含真实 API Key / Webhook,`.gitignore` 已覆盖,**禁止入库** |
| `database.js` / `*.md`(导出目录里的) | 含付费内容英文原文,**禁止推到公网仓库** |
| `economist_cookies.json` / `cookies*.json` | 含登录凭据,`.gitignore` 已覆盖,**禁止入库** |
| Cookie 传输 | **永远不要发到任何 AI 对话 / 工单 / 公开频道** — 用本地的 `--import-cookies` 走 |
| 抓取范围 | 仅限**个人付费账号**,不应用于商业分发 |
| 摘要版权 | 中文摘要属合理使用范围;**全文转载有版权风险** |
| 服务条款 | 自动化抓取可能违反经济学人 ToS,使用前请评估当地法规 |

## 3. 目录结构

```
economist_weekly_archiver_skill/
├── SKILL.md          # Skill 清单(Claude Agent 入口)
├── README.md         # 用户文档(本文)
├── DEVELOPMENT.md    # 开发规格(如果存在)
│
├── index.html        # 零依赖前端 SPA,双击即跑
├── database.js       # 数据持久层(window.economist_db 数组)
│
├── sync_weekly.py    # 主入口:周抓取 + LLM 总结 + 持久化 + 飞书推送
├── requirements.txt  # Python 依赖
├── .env.example      # 配置模板(复制成 .env,所有配置都在这里)
│
├── logs/             # 运行日志(sync_YYYYMMDD.log)
└── tests/            # 单元测试
```

## 4. 快速开始

### 4.1 安装

```bash
cd /Users/luzhe/Desktop/code/agent_skills/economist_weekly_archiver_skill

# 装依赖
pip install -r requirements.txt
```

依赖清单:

| 包 | 用途 |
|---|------|
| `drissionpage>=4.0.0` | Chrome 浏览器自动化(DrissionPage 4.x API) |
| `requests>=2.31.0` | 飞书 webhook 推送 |
| `openai>=1.0.0` | LLM 调用(OpenAI 兼容协议) |
| `python-dotenv>=1.0.0` | 从 `.env` 加载凭据 |
| `pytest>=7.4.0` | 单元测试 |

### 4.2 配置 `.env`

```bash
cp .env.example .env
```

编辑 `.env`:

```bash
LLM_API_KEY=sk-REPLACE-ME                # 必填
LLM_BASE_URL=https://api.example.com/v1   # 留空 = OpenAI 官方
LLM_MODEL=gpt-4o-mini                     # 默认
FEISHU_WEBHOOK_URL=                       # 留空 = 不推送
```

> 代码会自动剥离 `LLM_BASE_URL` 末尾的 `/chat/completions`(兼容两种写法)。

### 4.3 首次手动登录(一次性)

```bash
python -c "
from DrissionPage import ChromiumPage, ChromiumOptions
opts = ChromiumOptions()
opts.set_user_data_path('/Users/luzhe/.economist_archive/chrome_profile')
p = ChromiumPage(opts)
p.get('https://www.economist.com/weeklyedition')
# 手动登录账号 → 关闭浏览器
"
```

后续 `sync_weekly.py` 会复用 `~/.economist_archive/chrome_profile/` 里的 cookie,无需再登。

> macOS 第一次会问「是否允许完全磁盘访问权限」,给 Terminal/IDE 授权,否则 Chrome profile 写不进去。

### 4.4 配置加载顺序

所有配置都在 `.env` 里(sync_weekly.py 内的 `DEFAULTS` dict 提供结构化兜底值)。加载顺序:

```
sync_weekly.py 内的 DEFAULTS dict(结构化兜底值)
    ↓ overlay
.env / 操作系统环境变量(优先级最高,空字符串视为未设置)
```

| 字段 | 环境变量 | 说明 |
|------|------|------|
| `llm.model` / `max_tokens` / `temperature` / `timeout_s` | `LLM_MODEL` / `LLM_MAX_TOKENS` / `LLM_TEMPERATURE` / `LLM_TIMEOUT_S` | 非敏感,留空走 `DEFAULTS` |
| `llm.api_key` | `LLM_API_KEY` | **必填** |
| `llm.base_url` | `LLM_BASE_URL` | 自建网关;留空走官方 |
| `feishu.webhook_url` | `FEISHU_WEBHOOK_URL` | 留空则不推送 |
| `browser.user_data_path` | `BROWSER_USER_DATA_PATH` | Chrome 复用 profile 路径 |
| `browser.headless` | `BROWSER_HEADLESS` | `true` / `1` / `yes` 启用 |
| `crawl.delay_min_s/max_s` | `CRAWL_DELAY_MIN_S` / `CRAWL_DELAY_MAX_S` | 防风控抖动范围 |
| `crawl.max_retries` | `CRAWL_MAX_RETRIES` | LLM 重试次数 |
| `paths.database_js` | `DATABASE_JS_PATH` | 数据库落盘路径 |
| `paths.index_html` | `INDEX_HTML_PATH` | 自包含 index.html 输出路径 |
| `paths.index_template` | `INDEX_HTML_TEMPLATE` | index.html 模板路径 |
| `paths.article_md_dir` | `ARTICLE_MD_DIR` | 每篇文章导出 `.md` 的目录(留空 = 不导出) |

## 5. CLI 用法

```bash
# === 抓取 ===

python sync_weekly.py                       # 抓最新一期(自动,推飞书)
python sync_weekly.py --issue 2026-06-27    # 抓指定周六一期(非周六直接拒绝)
python sync_weekly.py --dry-run             # 只列链接不抓正文
python sync_weekly.py --no-feishu           # 抓了不推飞书

# === 调试 ===

python sync_weekly.py --single-url <URL> --section "Briefing"   # 抓单篇
python sync_weekly.py --single-url <URL> --rewrite-id art_xxx_001  # 重写某篇
python sync_weekly.py --issue 2026-06-27 --dry-run --debug-html /tmp/econ-debug

# === Cookie / 维护 ===

python sync_weekly.py --import-cookies ~/Downloads/economist_cookies.json  # 灌 cookie
python sync_weekly.py --kill-stale         # 杀掉残留 Chrome + 删 lock(默认也会自动做)
python sync_weekly.py --rebuild-index      # 仅根据 database.js 重新生成自包含 index.html
```

### 5.1 `--issue` 周六校验

《经济学人》weekly edition **仅周六发布**。非周六日期 SSR 数据是空(`__NEXT_DATA__` 解析得到 0 篇文章),浪费一次 LLM 调用。

本 skill 自动校验:

| 输入 | 行为 |
|------|------|
| `--issue 2026-07-11`(周六) | ✓ 访问 `/weeklyedition/2026-07-11`,正常抓取 |
| `--issue 2026-07-13`(周一) | ✗ 拒绝,日志:`2026-07-13 是周一,不是周六。…` |
| 不传 `--issue` | 访问 `/weeklyedition`(最新一期,默认行为) |

### 5.2 自包含 `index.html` 生成(方案 A)

如果你希望 `index.html` 跟 `database.js` 物理分离(比如 `index.html` 放在桌面、`database.js` 放在项目目录),或者想把归档页面拷到任何位置独立打开,只要在 `.env` 里配置 3 行:

```bash
DATABASE_JS_PATH=/Users/luzhe/Desktop/code/agent_skills/economist_weekly_archiver_skill/database.js
INDEX_HTML_PATH=/Users/luzhe/Desktop/economist_reading/index.html   # 桌面上的任意位置
INDEX_HTML_TEMPLATE=/Users/luzhe/Desktop/code/agent_skills/economist_weekly_archiver_skill/index.html
```

之后:

- **每抓一篇**都会自动重建 `INDEX_HTML_PATH`(把数据库整段内联进 HTML,不再依赖外部 `database.js`)
- 重建后的 HTML 是**自包含的**,拷到任何机器、U盘、邮件附件,双击即用
- **手动重建**:`python sync_weekly.py --rebuild-index`(不抓取,只重建)

实现细节见 `sync_weekly.build_index_html()`,单元测试在 `tests/test_build_index_html.py`。

### 5.3 单篇文章 `.md` 导出(可选)

如果想把每篇文章单独存成 **干干净净的英文原文** Markdown(便于导入 Obsidian / Notion / Logseq 等知识库、做文本分析、或本地全文搜索),在 `.env` 里加一行:

```bash
ARTICLE_MD_DIR=/Users/luzhe/Desktop/economist_md_archive
```

之后**每抓一篇**会自动按以下结构落盘:

```
$ARTICLE_MD_DIR/
└── 2026-07-04/
    ├── promised-reforms-could-change-cuba-radically-art_2026-07-04_019.md
    ├── another-article-title-art_2026-07-04_020.md
    └── ...
```

设计细节:

- **按 `issue_date` 分目录**:每篇文章自动放进 `{ARTICLE_MD_DIR}/{YYYY-MM-DD}/` 子目录,便于按期翻阅、跨期搜索不冲突
- **文件名 = 标题 slug + id**:`Promised reforms could change Cuba radically` → `promised-reforms-could-change-cuba-radically-art_2026-07-04_019.md`
  - **kebab-case slug**:小写、非字母数字/汉字转 `-`、连续 `-` 合并
  - **截断保护**:超长标题(默认 60 字符)在最近的 `-` 边界截断,避免切到单词中间
  - **id 在末尾**:稳定、可 grep `art_2026-07-04_019` 精确命中
  - **空标题** → 文件名只保留 `art_xxx.md`
- **md 内容 = 仅 `content_raw.strip()`**(零 front matter、零标题、零摘要、零链接包装)
- 原子写(同 id 重抓会**覆盖**更新)
- 失败只 `log.warning`,不影响抓取主流程

### 5.4 `--import-cookies`(替代手动登录)

如果你在别的浏览器已经登录了 Economist、但当前机器拿不到验证码:

1. 从浏览器导出 cookie(DevTools / EditThisCookie / Cookie-Editor / cURL 都行,只导出 `.economist.com` 域)
2. 存成本地 JSON 数组,放到项目外(比如 `~/Downloads/economist_cookies.json`)
3. 跑:

```bash
python sync_weekly.py --import-cookies ~/Downloads/economist_cookies.json
```

脚本会:
- 启 Chrome → 访问 economist.com → 注入 cookie → 刷新 weeklyedition → 扫页面文本验证是否还在登录墙
- 成功 log `✓ 验证通过,看起来已登录 Economist`
- 失败返回非零退出码

**支持格式**:JSON 数组(浏览器扩展导出)+ Netscape `cookies.txt`(`curl --cookie-jar` 风格)。**完全本机处理,cookie 不离开你的电脑**。

> **重要**:Cookie 是高敏感凭据,等同于账号密码。**永远不要发到任何 AI 对话 / 工单 / 公开频道**。如果已泄露,立刻在浏览器登出、清 cookie、重新登录,生成新的 cookie **直接** 保存到本地 `.json`,不再发对话。

### 5.3 实时观察 + 进度可见

`process_issue` 已经改成**逐篇落盘**:

```
[INFO] 开始逐篇抓取(已存在 1 篇,本 issue 板块过滤后 47 条)
[INFO] 抓取正文: The world is making heady progress...
[INFO] ✓ 已收录并落盘: art_2026-07-10_001 - ... (本 run 第 1 篇 / 累计 2 篇)
[INFO] ⏭ 已存在,跳过: ...(再跑时所有 URL 都跳过,不出现在写入日志)
[INFO] === 本 run 完成,新增 47 篇,database.js 累计 48 篇 ===
```

**实时观察**:

```bash
# 另一个终端实时 tail 日志
tail -f logs/sync_$(date +%Y%m%d).log

# 浏览器刷新 index.html,侧栏随抓取进度增长
open index.html
```

**重复抓取保护**:同一 URL 已在 `database.js` 里 → 自动跳过,不调 LLM,不访问正文。强制重写某篇:`--rewrite-id art_xxx_001` 或从 `database.js` 删掉那条再跑。

## 6. 排错

| 现象 | 排查 |
|------|------|
| `config.json 不存在` | 不再需要 — 所有配置在 `.env` 里 |
| `LLM_API_KEY 未配置` | 复制 `.env.example` 到 `.env` 并填入真实 key |
| 抓不到文章 | 跑 `python sync_weekly.py --dry-run --debug-html /tmp/econ-debug`,看 `[weekly]` 诊断行和导出的 HTML |
| 浏览器连接 9222 失败 | 不需要,代码已用 `_find_free_port` + `set_address('127.0.0.1:XXXXX')` 自动处理 |
| 残留 Chrome 锁文件 | 脚本启动前自动 `_cleanup_stale_chrome_locks`,可手动 `python sync_weekly.py --kill-stale` |
| 摘要字数不足 | 视为模型抽风,自动丢弃并重试,详见 `logs/sync_*.log` |
| LLM `404 page not found` | 检查 `LLM_BASE_URL` — 代码会自动剥离末尾 `/chat/completions`,如果还 404 看网关是否支持该路径 |
| LLM 返回含英文思考链 | 代码自动从 `🌟 一句话核心主旨` 开始截取,不需要手动处理 |
| 飞书推送失败 | 推送失败不阻塞主流程,只记 log;空跑(0 新增)不会推 |
| `index.html` 显示「没有加载到任何文章数据」 | 确认 `index.html` 跟 `database.js` 同目录,且 `index.html` 含 `<script src="database.js">` |

## 7. 数据格式

### 7.1 `database.js`

```javascript
window.economist_db = [
  {
    "issue_date": "2026-07-10",       // 周刊封面日期(YYYY-MM-DD,必须是周六)
    "id": "art_2026-07-10_001",       // 唯一 ID(基于 issue_date + 序号)
    "section": "Briefing",             // 板块原名
    "title": "...",                    // 英文标题
    "url": "https://www.economist.com/briefing/2026/07/09/...",  // 原文链接
    "summary_md": "### 🌟 一句话核心主旨\n...",  // 中文 Markdown 摘要(≥300 中文字)
    "content_raw": "WHEN ERIC STALLARD..."  // 英文原文(≥1000 字符)
  }
];
```

### 7.2 字段约束

| 字段 | 必填 | 约束 |
|------|------|------|
| `issue_date` | ✓ | `YYYY-MM-DD`,必须是周六封面日期 |
| `id` | ✓ | 格式 `art_<issue_date>_NNN`,seq 3 位零填充 |
| `section` | ✓ | `Science & technology` / `Culture` / `Books & arts` / `Obituary` / `Graphic detail` / `Briefing`(白名单) |
| `title` | ✓ | 英文原标题 |
| `url` | ✓ | 完整 URL,用作主键去重 |
| `summary_md` | ✓ | Markdown 文本,**≥ 300 中文字符**,无英文思考链残留 |
| `content_raw` | ✓ | 英文原文,纯文本,**≥ 1000 字符** |

## 8. 数据流

```
Economist.com
    │
    │ DrissionPage(复用已登录 Chrome profile)
    ▼
sync_weekly.py
    │
    ├─ fetch_weekly_index(page, issue_date)
    │   └─ 访问 /weeklyedition[/<YYYY-MM-DD>] → 解析 __NEXT_DATA__ JSON
    │
    ├─ 板块白名单过滤(Science & tech / Culture / Books & arts / Obituary / Graphic detail / Briefing)
    │
    ├─ 逐篇:fetch_article_content(page, url) → 解析 article 模板 → <p data-component="paragraph"> 段落
    │
    ├─ summarize(client, title, body) → 调 LLM(若返回含英文 CoT,从 🌟 截取)
    │
    ├─ write_database_js(existing + [new]) ← 每篇立即写盘
    │
    └─ 末尾:build_feishu_card → push_feishu(可选,新增 > 0 才发)
                                       │
                                       ▼
                              index.html(双击浏览)
                              ├─ 侧栏按 issue_date 分组、按板块分组
                              ├─ 全局搜索(title / section / summary_md)
                              ├─ 详情区 marked.js 渲染 Markdown
                              ├─ <details> 折叠显示英文原文
                              └─ 关键词 <mark> 高亮
```

## 9. 测试

```bash
cd /Users/luzhe/Desktop/code/agent_skills/economist_weekly_archiver_skill
python3 -m pytest tests/ -v
```

当前:117 个用例通过(14 个旧测试漂移未修,与本版功能无关;1 个依赖真实 dump 文件,无则 skip)。

| 测试文件 | 覆盖 |
|----------|------|
| `test_summary_length.py` | 中文字符计数、Prompt 章节 |
| `test_dedup.py` | URL 去重、原子写、备份 |
| `test_database_js_format.py` | `database.js` 格式、字段、URL 模式 |
| `test_feishu_card.py` | 卡片 JSON schema |
| `test_single_url.py` | `--single-url` 调试模式 |
| `test_import_cookies.py` | Cookie 文件解析、DrissionPage payload、set_address 校验 |
| `test_cleanup_locks.py` | 残留 Chrome 进程清理、lock 文件删除 |
| `test_parse_weekly_html.py` / `test_parse_weekly_html_v2.py` | weeklyedition HTML 解析(`__NEXT_DATA__` JSON 优先) |
| `test_parse_article_html.py` | 文章 HTML 解析(`<p data-component="paragraph">` + drop cap 处理) |
| `test_fetch_weekly_index.py` | fetch 防御(None / 字符串 / JS 错误) |
| `test_strip_cot.py` | LLM 返回英文 CoT 剥离(`🌟 一句话核心主旨` 锚点) |
| `test_llm_base_url.py` | 自动剥离末尾 `/chat/completions` |
| `test_config_env.py` | `.env` 加载 + 配置 overlay |
| `test_issue_date.py` | 周六校验、URL 构造、2026 年日历正确性 |

## 10. 容量与扩展

- 单篇 ~10 KB(中文字段 3-5 KB + 英文字段 5-10 KB)
- 50 周 × 8 篇 ≈ 4 MB
- `database.js` 接近 10 MB 时建议迁移 SQLite(v2,见 `DEVELOPMENT.md` §11)

## 11. 部署到 Netlify(分享给朋友看)

把 `index.html`(自包含,含数据库内联)和 `database.js` 当静态站托管,**朋友只需打开一个 URL,看不到任何代码**。

### 一次性配置(~30 分钟)

```bash
# 1) 装 Netlify CLI
npm install -g netlify-cli

# 2) 浏览器登录一次
netlify login

# 3) 关联项目到 Netlify site
cd /Users/luzhe/Desktop/code/agent_skills/economist_weekly_archiver_skill
netlify init

# 4) 推项目到 GitHub(repo 设为 private,避免 database.js 公开)
git init  # 如果还没 git
git add .
git commit -m "init"
# 在 github.com 新建一个 private repo,然后:
git remote add origin https://github.com/<yourname>/economist-archiver.git
git push -u origin master
```

`netlify.toml` 已包含在项目里,**publish = "."** 表示直接托管项目根。

### 工作流对比

| 方案 | 触发方式 | Mac 依赖 | 适合 |
|------|---------|---------|------|
| **A. Mac 本地 + git push**(推荐) | launchd 每周五自动跑 `run_weekly_sync.sh` | 需开机 | 你想**完全不动手** |
| **B. Mac 本地 + Netlify CLI** | 手动跑 `./deploy_to_netlify.sh` | 需开机(手动时) | 想**先验证再自动化** |
| **C. GitHub Actions** | 容器每周跑 cron | 不依赖 | 想**跑在云端** |

### 方案 A(推荐,完全不动手)

`run_weekly_sync.sh` 已经写好,每次运行会:

1. 抓取 → 写 `database.js` 到 `.env` 的 `DATABASE_JS_PATH`
2. 重建 `index.html` 到 `.env` 的 `INDEX_HTML_PATH`
3. 把外部产物**拷贝到项目根**(给 Netlify 用)
4. `git add database.js index.html` + commit + push
5. Netlify 看到 push → 自动 redeploy

**你 Mac 的 launchd 配一次**(`~/Library/LaunchAgents/com.economist.archiver.weekly.plist`),周五自动触发即可:

```bash
# 1) 项目根 launchd/com.economist.archiver.weekly.plist 用 __PROJECT_DIR__ 占位符
# 2) 运行安装脚本(替换占位符 + 写 LaunchAgents + 可选加载)
./install_launchd.sh
# 或指定项目根:
./install_launchd.sh /Users/luzhe/Desktop/code/agent_skills/economist_weekly_archiver_skill
```

`install_launchd.sh` 会:
- 把 plist 里的 `__PROJECT_DIR__` 替换为脚本所在路径
- 写到 `~/Library/LaunchAgents/com.economist.archiver.weekly.plist`
- 用 `plutil -lint` 校验语法
- 询问是否立刻 `launchctl load` + `launchctl start`

之后**完全不用动**,每周五 Mac 自动跑 → git push → Netlify 自动 redeploy → 朋友看到最新内容。

**plist 关键字段说明**:

| 字段 | 作用 |
|------|------|
| `Label` | 唯一标识,`launchctl start com.economist.archiver.weekly` 触发用 |
| `ProgramArguments` | `/bin/zsh <脚本绝对路径>`,必须绝对路径(用 `__PROJECT_DIR__` 占位符) |
| `WorkingDirectory` | 双保险:脚本内部也会 cd |
| `EnvironmentVariables.PATH` | launchd 默认 PATH 极简,必须显式把 Homebrew 加进去 |
| `StartCalendarInterval.Weekday=5` | 周五(0=周日,5=周五) |
| `StartCalendarInterval.Hour=20` | 20:00 触发(本地时间) |
| `StandardOutPath` / `StandardErrorPath` | 排错时看这两个文件 |

**修改 plist 后**:

```bash
# 1) 改 launchd/com.economist.archiver.weekly.plist(占位符形式)
# 2) 重跑安装脚本
./install_launchd.sh
# 脚本会自动 unload 旧版 + load 新版
```

### 方案 B(手动验证,半天搞定)

```bash
# 抓取 + 重建 + copy + 部署 +(可选)git push
./deploy_to_netlify.sh --issue 2026-07-11
```

跑一次就上 Netlify CDN。验证 OK 后再配 launchd 切到方案 A。

### Netlify 配置要点

1. **Import existing project** → 选 GitHub repo → Publish directory = `.` → Deploy
2. Netlify 会给你一个 URL,类似 `https://xxx-xxxxx.netlify.app`
3. 自定义域名(可选):Netlify 后台 → Domain settings → Add custom domain

### 数据库安全提示

`database.js` 现在**入 git**(为 Netlify 自动 redeploy 服务)。**repo 必须设 private**。一旦设为 public,所有付费内容公开。

如果改主意想纯本地部署(不用 git push):
- 把 `database.js` 加回 `.gitignore`
- 用 `netlify deploy --prod --dir=.`(CLI 直接上传,不走 git)
- 改用 `./deploy_to_netlify.sh`,把 git push 步骤注释掉

## 12. 许可

本 skill 代码:MIT。经济学人内容版权归 Economist Newspaper Ltd 所有。

---

**相关文档**:
- [SKILL.md](./SKILL.md) — Skill 清单(Claude Agent 入口)
- `DEVELOPMENT.md` — 开发规格(架构、数据格式、字段约束)