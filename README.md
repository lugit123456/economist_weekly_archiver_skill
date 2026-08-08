# 经济学人周报自动化抓取与归档

本项目抓取《经济学人》`weeklyedition` 的文章，使用 `.env` 中的 OpenAI-compatible LLM
生成中文解读、逐段翻译、关键词解析和图片简析，并将结果写入 `database.js`，配合
`index.html` 离线浏览。

## 功能

- 全量抓取 weekly edition 返回的文章，不受 `ALLOW_SECTIONS` 板块白名单限制。
- 仅排除 `/topic/`、`/person/`、`/newsletters/`、`/audio/`、`/video/` 等确定的非文章链接。
- 支持 `--limit 3` 先抓少量文章预览，也支持不限制的全量运行。
- 正文过短的文章不再跳过，卡通漫画栏也会保留并抓取图片。
- 图片范围截止到页面 `Explore more` 之前，排除周刊封面等推荐区图片。
- 图片下载到 `output_results/TE/{issue_date}/images/`，并生成 50-80 个中文字符的简短解析。
- 每期封面使用 weekly edition 顶部的 `content.cover` 图片，保存为 `output_results/TE/{issue_date}/cover.jpg`。
- 关键词独立解析生成 `glossary_entries` 和 `term_annotations`；先从中文译文确定性提取英文专名，
  再由 LLM 生成背景解释，漏项会自动补充解析。
- 浏览器抓取保持串行；正文编译和图片解析使用独立 LLM 队列并行执行。
- URL 去重、逐篇写盘、可选英文 Markdown 导出、可选 Feishu 通知。

## 安全与版权

`.env`、Cookie、`database.js`、`output_results/` 和英文 Markdown 可能包含 API 凭据、登录信息或
付费原文。不要把它们提交到公开 GitHub 仓库；即使被 `.gitignore` 忽略，已被 Git 跟踪的文件仍需
先执行 `git rm --cached` 才会停止跟踪。抓取内容仅供个人阅读和研究，并请自行评估 Economist
服务条款和当地版权法规。

## 目录

```text
economist_weekly_archiver_skill/
├── SKILL.md
├── README.md
├── sync_weekly.py                 # 唯一生产抓取入口
├── index.html                     # 本地离线浏览页面
├── database.js                    # 本地数据文件，建议不要公开
├── .env.example                   # 配置模板
├── requirements.txt
├── tests/test_glossary.py         # 关键词抽取、补漏和定位回归测试
├── run_weekly_sync.sh             # 可选的本地定时任务包装脚本
├── install_launchd.sh             # 可选的 macOS launchd 安装脚本
├── launchd/                       # 可选定时配置
└── deploy_to_netlify.sh           # 可选静态部署脚本
```

## 安装

```bash
cd /Users/luzhe/Desktop/code/agent_skills/economist_weekly_archiver_skill
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

在 Chrome 中登录 Economist，并让工具复用默认 profile，或设置 `BROWSER_USER_DATA_PATH`。
也可以从本地文件导入 Cookie：

```bash
python sync_weekly.py --import-cookies ~/Downloads/economist_cookies.json
```

## 配置

所有大模型请求都使用 `.env` 配置，不使用 Codex 大模型。`LLM_API_KEY` 是唯一必需的 LLM 配置；
`LLM_BASE_URL` 可指向任意兼容 OpenAI Chat Completions 的网关。

### LLM 与抓取

| 环境变量 | 说明 | 默认值 |
|---|---|---|
| `LLM_API_KEY` | LLM API key | 空，必须填写 |
| `LLM_BASE_URL` | OpenAI-compatible API 地址 | 官方 API |
| `LLM_MODEL` | 文章中文解读和翻译模型 | `gpt-4o-mini` |
| `LLM_MAX_TOKENS` | 文章请求最大 token 数 | `2048` |
| `LLM_TEMPERATURE` | 文章请求 temperature | `0.4` |
| `LLM_TIMEOUT_S` | LLM 请求超时秒数 | `60` |
| `CRAWL_DELAY_MIN_S` / `CRAWL_DELAY_MAX_S` | 抓取间隔范围 | `5` / `10` |
| `CRAWL_MAX_RETRIES` | 抓取相关 LLM 重试次数 | `2` |

### 关键词解析

| 环境变量 | 说明 | 默认值 |
|---|---|---|
| `LLM_GLOSSARY_ENABLED` | 是否开启关键词解析 | `true` |
| `LLM_GLOSSARY_MODEL` 或 `OPENAI_GLOSSARY_MODEL` | 关键词解析模型，留空沿用 `LLM_MODEL` | 空 |
| `LLM_GLOSSARY_MAX_TERMS` | 每篇最多关键词数量 | `32` |
| `LLM_GLOSSARY_MAX_ZH_CANDIDATES` | 中文译文中英文专名候选上限 | `32` |
| `LLM_GLOSSARY_MAX_INPUT_CHARS` | glossary 请求最多输入的中英文字符数 | `24000` |
| `LLM_GLOSSARY_MAX_TOKENS` | 关键词请求最大 token 数 | `5000` |
| `LLM_GLOSSARY_MAX_RETRIES` | 关键词解析重试次数 | `2` |

关键词类型包括人物、组织、公司、法律/政策、事件、地点、作品、专有概念和缩写。翻译阶段会在
首次出现时保留专名的英文原文；glossary 阶段会逐项审查中文栏中的英文候选。只有能在中文段落
中实际定位到的术语才会写入 `term_annotations`。模型给错大小写或段落号时，代码会回查真实位置；
若首次响应漏掉候选，则会额外发起一次仅处理漏项的请求。仍有漏项或请求失败时不会误标为完成。

### 图片解析

| 环境变量 | 说明 | 默认值 |
|---|---|---|
| `LLM_ANALYZE_ARTICLE_IMAGES` | 是否开启图片解析 | `true` |
| `OPENAI_VISION_MODEL` 或 `LLM_IMAGE_ANALYSIS_MODEL` | 图片/图表解析模型，留空沿用 `LLM_MODEL` | 空 |
| `LLM_IMAGE_ANALYSIS_MAX_TOKENS` | 图片解析最大 token 数 | `2400` |
| `LLM_IMAGE_ANALYSIS_MAX_RETRIES` | 图片解析重试次数 | `2` |
| `LLM_MAX_IMAGES_PER_ARTICLE` | 每篇最多送入 LLM 的图片数 | `6` |

图片解析会对每张图生成 50-80 个中文字符的描述，并标注 `photo`、`chart`、`cartoon` 或
`illustration`。下载失败时保留远程 URL；没有图片时不产生解析记录。

### 并发队列

| 环境变量 | 说明 | 默认值 |
|---|---|---|
| `LLM_COMPILE_WORKERS` | 正文翻译、解读及后续关键词请求的并发文章数 | `2` |
| `LLM_IMAGE_WORKERS` | 图片 vision 解析并发数 | `1` |
| `LLM_MAX_PENDING` | 等待正文编译的最大文章数 | `4` |

每篇文章先完成正文结构化翻译，再执行独立 glossary 请求，避免复杂的合并响应漏掉专名。
文章完成正文和关键词处理后写入 `database.js`；图片解析在独立队列完成后再回填。遇到 `422`、
认证或参数错误不会重复重试，只会重试超时、连接错误、`429` 和服务端错误。

### 浏览器、输出与通知

| 环境变量 | 说明 |
|---|---|
| `BROWSER_USER_DATA_PATH` | Chrome profile 路径，留空使用默认路径 |
| `BROWSER_HEADLESS` | `true` / `1` / `yes` 启用无头模式 |
| `FEISHU_WEBHOOK_URL` | Feishu webhook，留空不推送 |
| `OUTPUT_ROOT` | 图片等 paper 产物根目录，默认项目根 `output_results` |
| `DATABASE_JS_PATH` | `database.js` 输出路径，默认项目根 |
| `INDEX_HTML_PATH` | 可选的自包含 HTML 输出路径 |
| `INDEX_HTML_TEMPLATE` | 自包含 HTML 使用的模板路径 |
| `ARTICLE_MD_DIR` | 可选的英文 Markdown 导出目录 |

## 使用

```bash
# 抓最新一期
python sync_weekly.py

# 抓指定周六期；先只新增 3 篇检查结果
python sync_weekly.py --issue 2026-08-01 --limit 3 --no-feishu

# 全量抓取指定期；已存在 URL 会自动跳过
python sync_weekly.py --issue 2026-08-01 --no-feishu

# 只列出候选，不访问文章正文
python sync_weekly.py --issue 2026-08-01 --dry-run --no-feishu

# 刷新已有文章图片和图片解析
python sync_weekly.py --issue 2026-08-01 --refresh-images \
  --article-ids art_2026-08-01_007,art_2026-08-01_008 --no-feishu

# 按新版规则回填已有中文译文的关键词；省略 --article-ids 则处理本期全部
python sync_weekly.py --issue 2026-08-01 --refresh-glossary \
  --article-ids art_2026-08-01_007,art_2026-08-01_008

# 单篇调试、重写和维护
python sync_weekly.py --single-url '<URL>' --section 'Briefing' --no-feishu
python sync_weekly.py --rewrite-id art_2026-08-01_007 --no-feishu
python sync_weekly.py --rebuild-index
python sync_weekly.py --kill-stale
```

`--limit` 只控制本次新增数量，不是白名单，也不会改变页面返回的候选数。某期实际文章数以
页面解析结果为准，不能在代码或文档中硬编码成 69 或 73。

完整参数：

```text
--issue DATE              指定期，必须是周六
--limit N                 最多新增 N 篇，0=不限
--dry-run                 只列候选 URL
--no-feishu               不推送 Feishu
--refresh-images          刷新图片和图片解析
--refresh-glossary        重新解析已有中文译文的关键词
--article-ids IDS         配合刷新命令指定 article id，逗号分隔
--single-url URL          单篇抓取
--section NAME            单篇抓取时指定板块
--rewrite-id ID           强制重写已有文章
--import-cookies FILE     导入本地 Cookie
--debug-html DIR          导出目录页面 HTML
--kill-stale              清理 Chrome 残留进程和锁
--rebuild-index           只重建 index.html
```

## 数据格式

`database.js` 的顶层形式为 `window.economist_db = [...]`。每条记录的核心字段包括：

| 字段 | 用途 |
|---|---|
| `issue_date` / `id` | 期刊日期和稳定文章 ID |
| `section` / `title` / `url` | 板块、英文标题和原文地址 |
| `title_zh` / `summary_md` | 中文标题和约 400-500 字中文解读 |
| `content_raw` / `paragraphs` | 英文原文和逐段中英内容 |
| `images` | 本地图片相对路径或下载失败时的远程 URL |
| `image_insights` | 图片类型和 50-80 字中文解析 |
| `glossary_entries` | 关键词及中文背景说明 |
| `term_annotations` | 关键词在中文段落中的定位 |
| `glossary_analysis_complete` / `glossary_version` | 关键词解析状态和版本 |

文章 URL 全局去重，重复执行时不会重复调用 LLM。每篇文章立即写入数据库，便于长任务中断后
继续运行。`index.html` 可直接双击打开，或通过 `INDEX_HTML_PATH` 生成自包含副本。

## 排错

```bash
# 导出目录页面，观察候选解析
python sync_weekly.py --issue 2026-08-01 --dry-run --debug-html /tmp/econ-debug --no-feishu

# 查看日志
tail -f logs/sync_$(date +%Y%m%d).log
```

- `LLM_API_KEY 未配置`：复制 `.env.example` 为 `.env` 并填写 key。
- `抓不到文章`：先用 `--dry-run --debug-html` 检查页面登录状态和 HTML。
- `图片没有下载`：检查页面图片是否位于 `Explore more` 之前，以及网络和 `requests` 依赖。
- 摘要或图片解析不合格：查看日志；代码会按配置重试，失败不会伪造结果。
- Cookie 失效：重新登录或重新导入本地 Cookie。

## 可选定时与部署

`run_weekly_sync.sh`、`install_launchd.sh`、`launchd/` 和 `deploy_to_netlify.sh` 仅用于需要时的
本地定时或静态部署，不是抓取主流程。公开部署前必须使用 private repository，并确认没有上传
付费原文、Cookie、`.env` 或 `output_results/`。

## 许可

本项目代码采用 MIT。Economist 内容版权归 Economist Newspaper Ltd 所有。
