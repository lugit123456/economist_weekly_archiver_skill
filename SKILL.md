---
name: economist-weekly-archiver
description: |
  自动抓取《经济学人》周刊当前或指定期的所有允许板块文章,生成中文深度摘要,持久化到本地 database.js,
  并可选推送到飞书机器人。零后端、零依赖前端(双击 index.html 即用)。

  触发场景:用户想抓《经济学人》某期 / 某篇文章、补抓漏网文章、把已抓取的文章导给飞书群。
---

# Economist Weekly Archiver Skill

每周四早上《经济学人》新刊上线后,跑一次本 skill 把当期内容入库;离线浏览 + 搜索 + 飞书通知一条龙。

## 何时使用

- 想把《经济学人》新一期的非政治/非商业板块(科技、文化、书评、讣告、图表、长文 briefing)自动入库到本地
- 想补抓 weekly edition 目录里漏掉或抓失败的某一篇
- 已在别的浏览器登录了《经济学人》、但本机拿不到验证码,想把 cookie 灌过来
- 想要离线可搜索、可对照原文的个人知识库

**不要用本 skill 做的事**:
- 抓取 politics / business / finance / united-states / china / asia / europe 等板块 — 已被板块白名单过滤掉
- 商业分发抓到的内容 — 违反版权
- 把 `database.js` 推到公网仓库 — 含付费原文

## 快速调用

```bash
# 首次:装依赖 + 配 .env + 手动登录(只跑一次)
pip install -r requirements.txt
cp .env.example .env        # 填 LLM_API_KEY 和 FEISHU_WEBHOOK_URL
# 然后跑一次性脚本手动登录(详见 README.md 第 3 步)

# 抓最新一期 + 推飞书
python sync_weekly.py

# 抓指定周六一期(2026-06-27 是周六)
python sync_weekly.py --issue 2026-06-27

# 只列链接不抓正文
python sync_weekly.py --dry-run

# 抓单篇调试
python sync_weekly.py --single-url https://www.economist.com/briefing/.../... --section Briefing

# 从 JSON 文件灌 cookie 到 Chrome profile
python sync_weekly.py --import-cookies ~/Downloads/economist_cookies.json
```

完整文档见 [README.md](./README.md)。

## 输入 / 输出

| | |
|---|---|
| **输入** | `.env`(所有凭据 / 路径 / 调优参数);`sync_weekly.py` 内的 `DEFAULTS` dict 提供结构化兜底;Chrome profile 已登录状态 |
| **输出** | `database.js`(每抓一篇追加);可选 `{ARTICLE_MD_DIR}/art_*.md`(每篇独立 .md,只含英文原文);可选 `INDEX_HTML_PATH`(自包含 index.html,数据库内联);`logs/sync_YYYYMMDD.log`;可选飞书卡片 |
| **运行时长** | 47 篇 × 5-10s 间隔 ≈ 4-8 分钟抓正文,加 LLM 调用整体 10-30 分钟 |

## 产物构件(可独立启用)

每篇抓完后,根据 `.env` 配置,可同时产生最多三件互不依赖的产物:

| 产物 | 触发 env 变量 | 内容 | 默认 |
|------|------|------|------|
| `database.js` | `DATABASE_JS_PATH` | 全量 JSON 数组,供 index.html 加载 | 项目根 `database.js` |
| `art_<id>.md` | `ARTICLE_MD_DIR` | 单篇英文原文,**零包装**(纯 `content_raw.strip()`) | 关闭,需手动配 |
| `index.html`(自包含) | `INDEX_HTML_PATH` | 数据库内联进 HTML 模板,可双击即用 | 关闭,需手动配 |

## 核心不变量

- `database.js` 必须以 `window.economist_db = [` 开头、`];` 结尾,供 `index.html` 直接当 JS 加载
- 同一 `url` 全局去重,按 `(issue_date DESC, id ASC)` 排序
- 文章字段:`issue_date` / `id`(`art_<date>_NNN`) / `section` / `title` / `url` / `summary_md`(≥300 中文字)/ `content_raw`(≥1000 字符)
- 每周六才发布,非周六日期 `--issue` 直接拒绝
- LLM 摘要里若混入英文思考链,自动从 `🌟 一句话核心主旨` 开始截取

## 安全红线

- `database.js` 和 `{ARTICLE_MD_DIR}/art_*.md` 都含付费原文,**禁止推到公网仓库**(用户应自行在 `.gitignore` 加入对应的目录模式)
- 所有真实配置只放 `.env`(API Key、Webhook、Base URL、路径、调优参数),已加 `.gitignore`,**禁止入库**
- **Cookie 永远不要发到任何 AI 对话 / 工单 / 公开频道** — 本地 `--import-cookies` 走,全程不出本机

## 许可

本 skill 代码:MIT。《经济学人》内容版权归 Economist Newspaper Ltd 所有。