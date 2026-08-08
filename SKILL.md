---
name: economist-weekly-archiver
description: |
  抓取《经济学人》weekly edition 当前或指定期的全部文章，使用 .env 中配置的
  OpenAI-compatible LLM 生成中文解读、段落翻译、关键词解析和图片简析，下载正文图片，
  持久化到 database.js，并可选推送到 Feishu。支持 --limit 做小批量预览。

  触发场景：用户想抓取《经济学人》某一期、补抓单篇文章、刷新已有文章图片或关键词，
  或导入本地 Cookie。
---

# Economist Weekly Archiver Skill

这是一个本地运行的《经济学人》周刊归档工具。它复用已登录的 Chrome profile，访问
`weeklyedition` 目录，筛选出实际文章链接后逐篇抓取正文和图片，调用 `.env` 中的 LLM
生成中文内容，最后写入 `database.js` 供 `index.html` 离线浏览。

## 使用范围

- 默认全量抓取 weekly edition 返回的文章，不再使用 `ALLOW_SECTIONS` 板块白名单。
- 只排除明确的非文章路径，例如 `/topic/`、`/person/`、`/newsletters/`、`/audio/`、`/video/`。
- 不再因为正文过短跳过文章；卡通漫画等短内容也会保留，并抓取页面中的漫画图片。
- `--limit N` 只限制本次最多新增的文章数，适合先抓 3 篇验证；不改变全量候选列表。
- 文章和图片仅用于个人阅读和研究。Economist 内容受版权及服务条款保护，不应公开分发。

## 快速调用

```bash
pip install -r requirements.txt
cp .env.example .env
# 在 .env 填写 LLM_API_KEY，并确保 Chrome profile 已登录 Economist

# 先抓 3 篇预览
python sync_weekly.py --issue 2026-08-01 --limit 3 --no-feishu

# 需要时再抓本期剩余文章
python sync_weekly.py --issue 2026-08-01 --no-feishu

# 重新抓取已有文章图片并生成图片简析
python sync_weekly.py --issue 2026-08-01 --refresh-images \
  --article-ids art_2026-08-01_007,art_2026-08-01_008 --no-feishu

# 按当前规则重新解析已有中文译文中的关键词
python sync_weekly.py --issue 2026-08-01 --refresh-glossary \
  --article-ids art_2026-08-01_007,art_2026-08-01_008
```

## 输入与输出

输入包括 `.env`、已登录的 Chrome profile，以及可选的本地 Cookie 文件。默认输出为：

- `database.js`：归档文章数据，逐篇写盘并按 URL 去重。
- `output_results/TE/{issue_date}/images/`：下载后的正文图片。
- `output_results/TE/{issue_date}/cover.jpg`：weekly edition 顶部的真实期刊封面。
- `logs/sync_YYYYMMDD.log`：运行日志。
- 可选 `ARTICLE_MD_DIR`：按期导出的英文 Markdown 原文。
- 可选 `INDEX_HTML_PATH`：数据库内联后的自包含浏览页面。

图片只取文章主体和明确的 `leadComponent`/`leadImage` 等字段，并限制在页面
`Explore more` 之前；`weeklyEdition.cover`、`squareCover` 等周刊封面不会作为正文图片保存。
每张图片可生成 50-80 个中文字符的 `image_insights`，说明图片、图表或漫画内容及其与文章的关系。

浏览器访问始终串行；默认使用 2 个正文 LLM worker 和 1 个图片 LLM worker。每篇文章先生成
段落翻译和中文解读，再独立解析关键词；关键词解析会提取中文栏英文候选并对漏项补充请求。
图片解析完成后异步回填，不阻塞文章落库。可通过
`LLM_COMPILE_WORKERS`、`LLM_IMAGE_WORKERS` 和 `LLM_MAX_PENDING` 调整并发。

每篇文章还可以产生：

- `summary_md`：约 400-500 个中文字符的连贯中文解读。
- `paragraphs`：按原文段落保存英文和中文内容。
- `glossary_entries`：关键词、中文名称、类型和中文背景说明。
- `term_annotations`：关键词在中文段落中的定位信息。
- `glossary_analysis_complete`、`glossary_version`：关键词解析状态和版本。

## CLI

```text
--issue YYYY-MM-DD       抓指定周六期；省略则抓最新一期
--limit N                本次最多新增 N 篇，0 表示不限制
--dry-run                只列出候选链接，不抓正文、不写库
--no-feishu              不推送 Feishu
--refresh-images         只刷新已有文章图片和图片简析
--refresh-glossary       只重新解析已有中文译文的关键词
--article-ids IDS        配合刷新命令，逗号分隔 article id
--single-url URL         只抓指定 URL 一篇
--section NAME           配合 --single-url 指定板块
--rewrite-id ID          强制重写指定文章
--import-cookies FILE    导入本地 JSON 或 Netscape Cookie 文件
--debug-html DIR         保存 weeklyedition HTML 供排错
--kill-stale             清理残留 Chrome 进程和锁文件
--rebuild-index          只根据 database.js 重建 index.html
```

## LLM 配置

所有 LLM 调用都读取 `.env` 的配置，不使用 Codex 模型。默认的文章解读、关键词解析和图片解析
共用 `LLM_API_KEY` 与 `LLM_BASE_URL`；各功能可单独指定模型：

```dotenv
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.example.com/v1
LLM_MODEL=gpt-4o-mini
LLM_GLOSSARY_ENABLED=true
LLM_GLOSSARY_MODEL=
OPENAI_VISION_MODEL=
LLM_ANALYZE_ARTICLE_IMAGES=true
```

完整配置项和默认值见 `.env.example` 与 `README.md`。

## 安全要求

- 不要提交 `.env`、Cookie、`database.js`、`output_results/` 或含付费原文的导出目录。
- `economist_cookies.json` 是登录凭据，不要发送到聊天、工单或公开仓库。
- 修改 glossary 逻辑后运行 `python -m unittest discover -s tests -v` 和静态语法检查。

## 参考

完整安装、配置和数据字段说明见 [README.md](README.md)。

## 许可

本 skill 代码采用 MIT。Economist 内容版权归 Economist Newspaper Ltd 所有。
