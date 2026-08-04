#!/usr/bin/env python3
"""Economist 周报自动化抓取与归档 — 主入口。

工作流: 启动浏览器 → 抓 weeklyedition → 板块过滤 → 逐篇 LLM 总结
       → database.js 原子写 → 飞书推送。

CLI 详见 DEVELOPMENT.md §3.2.2。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# 延迟导入 requests/openai,方便无 LLM 依赖环境下跑单元测试。
# 真正调用 LLM / 推飞书时才触发。

# ---------------------------------------------------------------------------
# 常量(详见 DEVELOPMENT.md §3.2.3 / §3.2.4)
# ---------------------------------------------------------------------------

NON_ARTICLE_PATH_KEYWORDS: tuple[str, ...] = (
    "/topic/", "/person/", "/newsletters/", "/audio/", "/video/",
)
GLOSSARY_VERSION = 1
GLOSSARY_TYPES = {
    "person", "organization", "company", "policy_law", "event",
    "place_context", "work", "proper_concept", "acronym",
}

WEEKLY_URL = "https://www.economist.com/weeklyedition"
PAPER_PUBLICATION_TYPE = "TE"
PAPER_PUBLICATION_NAME = "The Economist"


def validate_issue_date(issue_date: str) -> tuple[bool, str]:
    """校验 issue 日期合法性 + 是否周六(Economist weekly 仅周六发布,其他日期 SSR 数据是空)。

    返回 (is_valid, error_message)。合法返回 (True, "")。
    """
    try:
        d = datetime.strptime(issue_date, "%Y-%m-%d")
    except ValueError:
        return False, f"日期格式错误:{issue_date!r},期望 YYYY-MM-DD"
    if d.year < 2010 or d.year > 2100:
        return False, f"年份超出合理范围:{d.year}"
    # weekday(): Monday=0 ... Sunday=6; Saturday=5
    if d.weekday() != 5:
        weekday_name = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][d.weekday()]
        return False, (
            f"{issue_date} 是{weekday_name},不是周六。Economist weekly edition 仅周六发布,"
            f"其他日期 SSR 数据是空(返回 0 条),建议改成最近的周六。"
        )
    return True, ""

SELECTORS = {
    # 集中化，DOM 改版时只改这里
    "weekly_issue_card": "a[data-test-id='issue-card']",
    "weekly_article_link": "section.ds-volume-list a",
    "article_h1": "h1[data-test-id='article-headline'], h1.css-1tik00t", # 增加对单篇 H1 的兼容
    "article_body": "p[data-component='paragraph'], div.article-body p, section.ds-content-body p", # 精准指向段落组件
}

SUMMARY_SYSTEM_PROMPT: str = """\
你是一位专业的国际政经与科技评论员。请阅读《经济学人》文章的英文原文,深度总结为中文。

硬性要求:
1. 总结总字数严禁少于 300 字(中文字符计)。
2. 严禁遗漏任何核心论点和数据(数字、人物、机构名、年份)。
3. 严禁引入原文外的信息。
4. 若原文涉及争议,保留双方观点,标注来源。

严格按以下 Markdown 格式输出(不可增删章节):

### 🌟 一句话核心主旨
(1-2 句话,精准点出文章最核心的论点)

### 🔍 核心观点与论据拆解
(分点列出,3-5 个,每点 50-80 字)

### 🤨 争议与潜在挑战
(若文章未涉及,写"原文未涉及明显争议")

### 🔮 未来趋势预判
(基于文章事实延伸,2-3 句话,不可编造新数据)
"""

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
DATABASE_JS = ROOT / "database.js"
LOGS_DIR = ROOT / "logs"

CN_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
MIN_CN_CHARS = 300

# ---------------------------------------------------------------------------
# 默认配置(全部从 .env 覆盖,默认值在这里 hardcode)
# ---------------------------------------------------------------------------
DEFAULTS = {
    "llm": {
        "provider": "openai",
        "api_key": "",
        "base_url": "",
        "model": "gpt-4o-mini",
        "max_tokens": 2048,
        "temperature": 0.4,
        "timeout_s": 60,
    },
    "feishu": {
        "webhook_url": "",
        "enabled": True,
    },
    "browser": {
        # Chrome 复用 profile 路径(Mac 默认位置,Windows/Linux 可在 .env 覆盖)
        "user_data_path": "/Users/luzhe/.economist_archive/chrome_profile",
        "headless": False,
    },
    "crawl": {
        "issue_default": "latest",
        "delay_min_s": 5,
        "delay_max_s": 10,
        "max_retries": 2,
    },
    "glossary": {
        "enabled": True,
        "model": "",
        "max_terms": 12,
        "max_tokens": 5000,
        "max_retries": 2,
    },
    "image_analysis": {
        "enabled": True,
        "model": "",
        "max_tokens": 2400,
        "max_retries": 2,
        "max_images": 6,
    },
    "pipeline": {
        "compile_workers": 2,
        "image_workers": 1,
        "max_pending": 4,
    },
    "paths": {
        "output_root": "",
        "database_js": "",   # 空 = 用全局 DATABASE_JS(项目根/database.js)
        "index_html": "",    # 空 = 不生成自包含 index.html(只用项目根的)
        "index_template": "",  # 空 = 用项目根/index.html 当模板
        "article_md_dir": "",  # 空 = 不导出 .md;填了则每篇文章单独一个 {id}.md
    },
}

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------


def setup_logger() -> logging.Logger:
    LOGS_DIR.mkdir(exist_ok=True)
    fname = f"sync_{datetime.now().strftime('%Y%m%d')}.log"
    log_path = LOGS_DIR / fname
    logger = logging.getLogger("sync_weekly")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


log = setup_logger()


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def load_config(env: dict[str, str] | None = None) -> dict[str, Any]:
    """从 DEFAULTS 起步,用 .env / 环境变量覆盖任何字段。

    所有配置都集中在 .env(详见 .env.example),不再依赖 config.json。

    环境变量约定(均可选,只有 LLM_API_KEY 是 LLM 调用所必需的):
      LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
        → llm.api_key / llm.base_url / llm.model
      FEISHU_WEBHOOK_URL → feishu.webhook_url
      DATABASE_JS_PATH / INDEX_HTML_PATH / INDEX_HTML_TEMPLATE → paths.*
      LLM_MAX_TOKENS / LLM_TEMPERATURE / LLM_TIMEOUT_S → llm.*
      BROWSER_USER_DATA_PATH / BROWSER_HEADLESS → browser.*
      CRAWL_DELAY_MIN_S / CRAWL_DELAY_MAX_S / CRAWL_MAX_RETRIES → crawl.*

    设计:DEFAULTS 提供结构化默认值,所有真实配置在 .env 里覆盖,不入库。
    env 覆盖 DEFAULTS(后写优先),便于 CI / 本地临时切换。
    """
    # 每次读 config 前确保 .env 已加载
    _load_dotenv()

    import copy
    cfg = copy.deepcopy(DEFAULTS)

    src = env if env is not None else os.environ
    overlay = {
        "llm.api_key": src.get("LLM_API_KEY", "").strip(),
        "llm.base_url": src.get("LLM_BASE_URL", "").strip(),
        "llm.model": src.get("LLM_MODEL", "").strip(),
        "llm.max_tokens": src.get("LLM_MAX_TOKENS", "").strip(),
        "llm.temperature": src.get("LLM_TEMPERATURE", "").strip(),
        "llm.timeout_s": src.get("LLM_TIMEOUT_S", "").strip(),
        "feishu.webhook_url": src.get("FEISHU_WEBHOOK_URL", "").strip(),
        "browser.user_data_path": src.get("BROWSER_USER_DATA_PATH", "").strip(),
        "browser.headless": src.get("BROWSER_HEADLESS", "").strip().lower() in ("1", "true", "yes"),
        "crawl.delay_min_s": src.get("CRAWL_DELAY_MIN_S", "").strip(),
        "crawl.delay_max_s": src.get("CRAWL_DELAY_MAX_S", "").strip(),
        "crawl.max_retries": src.get("CRAWL_MAX_RETRIES", "").strip(),
        "glossary.model": (
            src.get("LLM_GLOSSARY_MODEL", "").strip()
            or src.get("OPENAI_GLOSSARY_MODEL", "").strip()
        ),
        "glossary.max_terms": src.get("LLM_GLOSSARY_MAX_TERMS", "").strip(),
        "glossary.max_tokens": src.get("LLM_GLOSSARY_MAX_TOKENS", "").strip(),
        "glossary.max_retries": src.get("LLM_GLOSSARY_MAX_RETRIES", "").strip(),
        "image_analysis.model": (
            src.get("OPENAI_VISION_MODEL", "").strip()
            or src.get("LLM_IMAGE_ANALYSIS_MODEL", "").strip()
        ),
        "image_analysis.max_tokens": src.get("LLM_IMAGE_ANALYSIS_MAX_TOKENS", "").strip(),
        "image_analysis.max_retries": src.get("LLM_IMAGE_ANALYSIS_MAX_RETRIES", "").strip(),
        "image_analysis.max_images": src.get("LLM_MAX_IMAGES_PER_ARTICLE", "").strip(),
        "pipeline.compile_workers": src.get("LLM_COMPILE_WORKERS", "").strip(),
        "pipeline.image_workers": src.get("LLM_IMAGE_WORKERS", "").strip(),
        "pipeline.max_pending": src.get("LLM_MAX_PENDING", "").strip(),
        "paths.database_js": src.get("DATABASE_JS_PATH", "").strip(),
        "paths.output_root": src.get("OUTPUT_ROOT", "").strip(),
        "paths.index_html": src.get("INDEX_HTML_PATH", "").strip(),
        "paths.index_template": src.get("INDEX_HTML_TEMPLATE", "").strip(),
        "paths.article_md_dir": src.get("ARTICLE_MD_DIR", "").strip(),
    }
    for dotted, val in overlay.items():
        if val == "" or val is False:
            continue
        section, key = dotted.split(".", 1)
        cfg[section][key] = val

    # 类型转换(LLM/爬虫调优参数都是数字);非法值静默回退到 DEFAULTS
    int_fields = {
        "llm": ["max_tokens", "timeout_s"],
        "crawl": ["delay_min_s", "delay_max_s", "max_retries"],
        "glossary": ["max_terms", "max_tokens", "max_retries"],
        "image_analysis": ["max_tokens", "max_retries", "max_images"],
        "pipeline": ["compile_workers", "image_workers", "max_pending"],
    }
    for section, keys in int_fields.items():
        for k in keys:
            raw = cfg[section][k]
            try:
                cfg[section][k] = int(raw)
            except (ValueError, TypeError):
                cfg[section][k] = DEFAULTS[section][k]
    try:
        raw = cfg["llm"]["temperature"]
        cfg["llm"]["temperature"] = float(raw)
    except (ValueError, TypeError):
        cfg["llm"]["temperature"] = DEFAULTS["llm"]["temperature"]

    raw_glossary_enabled = src.get("LLM_GLOSSARY_ENABLED", "").strip().lower()
    if raw_glossary_enabled:
        cfg["glossary"]["enabled"] = raw_glossary_enabled in ("1", "true", "yes", "on")
    raw_image_analysis_enabled = src.get("LLM_ANALYZE_ARTICLE_IMAGES", "").strip().lower()
    if raw_image_analysis_enabled:
        cfg["image_analysis"]["enabled"] = raw_image_analysis_enabled in ("1", "true", "yes", "on")

    paths = cfg.get("paths") or {}
    if not str(paths.get("output_root") or "").strip():
        paths["output_root"] = str(ROOT / "output_results")
    cfg["paths"] = paths

    return cfg


def _load_dotenv() -> None:
    """从项目根 .env 加载环境变量(缺失 python-dotenv 时静默跳过)。"""
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        return
    env_path = ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)


# ---------------------------------------------------------------------------
# database.js 读写
# ---------------------------------------------------------------------------


def read_database_js(path: Path | None = None) -> list[dict[str, Any]]:
    """从 database.js 读出当前数组(剥离 window.* 赋值)。

    `path` 默认惰性查找模块级 DATABASE_JS,以便 monkeypatch 测试时改路径生效。
    """
    if path is None:
        path = DATABASE_JS
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    # 抓最后一个赋值(避免注释里出现相同模式)。
    matches = list(re.finditer(r"window\.economist_db\s*=\s*(\[[\s\S]*?\])\s*;", text))
    if not matches:
        log.warning("database.js 格式异常,按空数组处理")
        return []
    raw = matches[-1].group(1)
    if not raw.strip() or raw.strip() == "[]":
        return []
    return json.loads(raw)


def _serialize_for_js(obj: Any) -> str:
    """用 JSON 序列化,再用 _js_escape 保护 JS 字符串。"""
    return json.dumps(obj, ensure_ascii=False, indent=2)


def write_database_js(articles: list[dict[str, Any]], path: Path | None = None) -> None:
    """原子写 database.js: 按 url 去重 → 排序 → 临时文件 + os.replace。

    `path` 默认惰性查找 DATABASE_JS,以便测试 monkeypatch 生效。
    """
    if path is None:
        path = DATABASE_JS
    # 备份旧文件
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    # 合并去重(优先保留新数据)
    existing = read_database_js()
    by_url: dict[str, dict[str, Any]] = {a["url"]: a for a in existing}
    for a in articles:
        by_url[a["url"]] = a
    merged = list(by_url.values())
    # 排序:issue_date DESC, id ASC
    merged.sort(key=lambda a: (-_date_key(a.get("issue_date", "")), a.get("id", "")))
    # 原子写
    body = "window.economist_db = " + _serialize_for_js(merged) + ";\n"
    fd, tmp_path = tempfile.mkstemp(prefix="database.", suffix=".js.tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _paper_output_root(cfg: dict[str, Any]) -> Path:
    paths = cfg.get("paths") or {}
    out = str(paths.get("output_root") or "").strip()
    return Path(out) if out else (ROOT / "output_results")


def _group_articles_by_issue(articles: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for article in articles:
        issue_date = str(article.get("issue_date") or "").strip()
        if not issue_date:
            continue
        grouped.setdefault(issue_date, []).append(article)
    for issue_date, issue_articles in grouped.items():
        issue_articles.sort(key=lambda a: str(a.get("id") or ""))
    return dict(sorted(grouped.items(), key=lambda item: item[0], reverse=True))


def _normalise_paper_article(article: dict[str, Any], index: int) -> dict[str, Any]:
    article_id = str(article.get("id") or f"art_{index:03d}")
    source_paragraphs = article.get("paragraphs") if isinstance(article.get("paragraphs"), list) else []
    normalized_paragraphs: list[dict[str, Any]] = []
    for para_index, paragraph in enumerate(source_paragraphs, start=1):
        if not isinstance(paragraph, dict):
            continue
        en_text = str(paragraph.get("en_text") or paragraph.get("en_html") or "").strip()
        zh_text = str(paragraph.get("zh_text") or "").strip()
        role = str(paragraph.get("role") or "body").strip() or "body"
        if not en_text and not zh_text:
            continue
        normalized_paragraphs.append(
            {
                "para_id": str(paragraph.get("para_id") or f"{article_id}_p{para_index}"),
                "en_text": en_text,
                "zh_text": zh_text,
                "role": role,
            }
        )

    if not normalized_paragraphs:
        normalized_paragraphs = [
            {
                "para_id": f"{article_id}_p1",
                "en_text": str(article.get("content_markdown") or article.get("content_raw") or "").strip(),
                "zh_text": "",
                "role": "body",
            }
        ]

    content_markdown = str(article.get("content_markdown") or "").strip()
    if not content_markdown:
        content_markdown = "\n\n".join(
            f"## {para['en_text']}" if para.get("role") == "crosshead" else para["en_text"]
            for para in normalized_paragraphs
            if str(para.get("en_text") or "").strip()
        )

    return {
        "id": article_id,
        "publication_type": PAPER_PUBLICATION_TYPE,
        "publication_date": str(article.get("issue_date") or ""),
        "source_pdf": "Economist Weekly",
        "page": int(article.get("page") or 0) if str(article.get("page") or "").isdigit() else 0,
        "page_article_index": int(article.get("page_article_index") or 0) if str(article.get("page_article_index") or "").isdigit() else index,
        "category": str(article.get("section") or "General"),
        "title": str(article.get("title") or ""),
        "title_zh": str(article.get("title_zh") or ""),
        "markdown_path": f"articles/{article_id}.md",
        "summary_md": str(article.get("summary_md") or ""),
        "compiled_article": bool(article.get("compiled_article")),
        "compile_status": str(article.get("compile_status") or "pending"),
        "content_markdown": content_markdown,
        "content_raw": str(article.get("content_raw") or article.get("content_markdown") or "").strip(),
        "paragraphs": normalized_paragraphs,
        "images": article.get("images") or [],
        "image_insights": article.get("image_insights") or [],
        "term_annotations": article.get("term_annotations") or [],
        "glossary_analysis_complete": bool(article.get("glossary_analysis_complete")),
        "glossary_version": int(article.get("glossary_version") or 0),
    }


def _write_atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        raise


def _write_atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _build_issue_glossary(articles: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """把逐篇术语条目聚合为前端按 glossary_id 查询的期刊级字典。"""
    glossary: dict[str, dict[str, Any]] = {}
    for article in articles:
        for entry in article.get("glossary_entries") or []:
            if not isinstance(entry, dict):
                continue
            glossary_id = str(entry.get("id") or "").strip()
            if glossary_id:
                glossary[glossary_id] = dict(entry)
    return glossary


def _write_paper_issue_database(
    output_root: Path,
    issue_date: str,
    articles: list[dict[str, Any]],
    cover_image: str = "",
) -> tuple[Path, str, dict[str, Any]]:
    issue_dir = output_root / PAPER_PUBLICATION_TYPE / issue_date
    database_path = issue_dir / "database.js"
    pdf_id = f"{PAPER_PUBLICATION_TYPE}_{issue_date}_economist-weekly"
    normalized_articles = [
        _normalise_paper_article(article, index)
        for index, article in enumerate(articles, start=1)
    ]
    if not cover_image and database_path.exists():
        try:
            previous = database_path.read_text(encoding="utf-8")
            matched = re.search(r'"cover_image"\s*:\s*"([^"]*)"', previous)
            cover_image = matched.group(1) if matched else ""
        except Exception:
            pass
    payload = {
        "id": pdf_id,
        "publication_type": PAPER_PUBLICATION_TYPE,
        "publication_date": issue_date,
        "original_filename": f"Economist Weekly - {issue_date}",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cover_image": cover_image,
        "article_count": len(normalized_articles),
        "glossary_version": GLOSSARY_VERSION,
        "glossary": _build_issue_glossary(articles),
        "articles": normalized_articles,
    }
    text = (
        "window.paper_databases = window.paper_databases || {};\n"
        f'window.paper_databases[{json.dumps(pdf_id, ensure_ascii=False)}] = '
        f"{json.dumps(payload, ensure_ascii=False, indent=2)};\n"
    )
    _write_atomic_text(database_path, text)
    return database_path, pdf_id, payload


def _write_paper_database_index(
    output_root: Path,
    grouped_articles: dict[str, list[dict[str, Any]]],
) -> Path:
    index_path = output_root / "database_index.js"
    items: list[dict[str, Any]] = []
    for issue_date, issue_articles in grouped_articles.items():
        pdf_id = f"{PAPER_PUBLICATION_TYPE}_{issue_date}_economist-weekly"
        cover_image = ""
        issue_database = output_root / PAPER_PUBLICATION_TYPE / issue_date / "database.js"
        try:
            previous = issue_database.read_text(encoding="utf-8")
            matched = re.search(r'"cover_image"\s*:\s*"([^"]*)"', previous)
            cover_image = matched.group(1) if matched else ""
        except Exception:
            pass
        items.append(
            {
                "id": pdf_id,
                "publication_type": PAPER_PUBLICATION_TYPE,
                "publication_date": issue_date,
                "original_filename": f"Economist Weekly - {issue_date}",
                "database_path": f"{PAPER_PUBLICATION_TYPE}/{issue_date}/database.js",
                "cover_image": cover_image,
                "article_count": len(issue_articles),
                "sections": sorted({str(article.get("section") or "General") for article in issue_articles}),
                "titles": [article.get("title") for article in issue_articles if article.get("title")],
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
    text = "window.paper_db_index = " + json.dumps(items, ensure_ascii=False, indent=2) + ";\n"
    _write_atomic_text(index_path, text)
    return index_path


def _sync_paper_outputs(
    cfg: dict[str, Any],
    articles: list[dict[str, Any]],
    issue_date: str | None = None,
    issue_covers: dict[str, str] | None = None,
) -> None:
    output_root = _paper_output_root(cfg)
    grouped = _group_articles_by_issue(articles)
    for grouped_issue_date, issue_articles in grouped.items():
        _write_paper_issue_database(
            output_root,
            grouped_issue_date,
            issue_articles,
            cover_image=(issue_covers or {}).get(grouped_issue_date, ""),
        )
    _write_paper_database_index(output_root, grouped)


def _date_key(s: str) -> int:
    """YYYY-MM-DD → 整数用于排序。非法字符串当作极小值。"""
    try:
        return int(datetime.strptime(s, "%Y-%m-%d").strftime("%Y%m%d"))
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# index.html 生成(把数据库内联进 HTML,产出可独立打开的自包含文件)
# ---------------------------------------------------------------------------


# 模板里这一行会被替换成内联脚本块
_INDEX_TEMPLATE_MARKER = '<script src="database.js"></script>'


def build_index_html(
    output_path: Path,
    db_path: Path,
    template_path: Path,
) -> bool:
    """生成自包含的 index.html(数据库内联,不再依赖外部 database.js)。

    工作流:
      1) 读 template_path(index.html 模板,带 <script src="database.js"></script> 占位)
      2) 读 db_path(合法 database.js)
      3) 抽出 window.economist_db = [...] 数组
      4) 把模板里的占位行替换成 <script>window.economist_db = [...] ;</script>
      5) 原子写到 output_path(临时文件 + os.replace)

    返回 True/False 表示是否成功。
    """
    try:
        template = template_path.read_text(encoding="utf-8")
    except Exception as e:
        log.error(f"[index] 读模板失败 {template_path}:{e}")
        return False

    if _INDEX_TEMPLATE_MARKER not in template:
        log.error(
            f"[index] 模板缺少占位符 {_INDEX_TEMPLATE_MARKER!r},"
            f"请确认 template_path 是新版 index.html"
        )
        return False

    try:
        db_text = db_path.read_text(encoding="utf-8")
    except Exception as e:
        log.error(f"[index] 读数据库失败 {db_path}:{e}")
        return False

    m = re.search(r"window\.economist_db\s*=\s*(\[.*\])\s*;", db_text, re.DOTALL)
    if not m:
        log.error(f"[index] 数据库格式异常,未找到 window.economist_db = [...]")
        return False
    array_str = m.group(1)

    inline = (
        "<script>\n"
        "/* 自包含:数据已内联,无外部文件依赖 */\n"
        f"window.economist_db = {array_str};\n"
        "</script>"
    )
    rendered = template.replace(_INDEX_TEMPLATE_MARKER, inline)

    # 原子写
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="index.", suffix=".html.tmp", dir=output_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(rendered)
        os.replace(tmp_path, output_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        raise

    log.info(
        f"[index] ✓ 已生成自包含 index.html → {output_path}  "
        f"({output_path.stat().st_size:,} bytes,含 {array_str.count(chr(34) + 'id' + chr(34))} 篇文章)"
    )
    return True


def _maybe_rebuild_index(cfg: dict[str, Any]) -> None:
    """如果 .env 配置了 paths.index_html / paths.index_template,重建自包含 index.html。

    任何错误都只记 log 不抛,避免影响抓取主流程。
    """
    paths_cfg = cfg.get("paths") or {}
    out_str = paths_cfg.get("index_html") or ""
    if not out_str:
        return  # 未配置 → 跳过(不影响流程)
    template_str = paths_cfg.get("index_template") or ""
    # 默认模板 = 当前项目里的 index.html(用户可以直接复用既有文件作为模板)
    template_path = Path(template_str) if template_str else (ROOT / "index.html")
    output_path = Path(out_str)

    # db 路径优先用 cfg,否则用全局 DATABASE_JS
    db_str = paths_cfg.get("database_js") or ""
    db_path = Path(db_str) if db_str else DATABASE_JS

    if not template_path.exists():
        log.warning(f"[index] 模板不存在 {template_path},跳过重建")
        return
    if not db_path.exists():
        log.warning(f"[index] 数据库不存在 {db_path},跳过重建")
        return
    try:
        template = template_path.read_text(encoding="utf-8")
    except Exception as e:
        log.warning(f"[index] 读模板失败 {template_path}:{e}")
        return

    if _INDEX_TEMPLATE_MARKER not in template:
        try:
            db_text = db_path.read_text(encoding="utf-8")
            inline = (
                "\n<script>\n"
                "/* legacy inline database for compatibility */\n"
                f"{db_text}\n"
                "</script>\n"
            )
            rendered = template.replace("</body>", f"{inline}</body>")
            if rendered == template:
                rendered = template + inline
            _write_atomic_text(output_path, rendered)
            log.info(f"[index] ✓ 已复制模板并内联旧数据库到 {output_path}")
        except Exception as e:
            log.warning(f"[index] 复制模板失败(不影响抓取):{e}")
        return

    try:
        build_index_html(output_path, db_path, template_path)
    except Exception as e:
        log.warning(f"[index] 重建失败(不影响抓取):{e}")


# ---------------------------------------------------------------------------
# 单篇文章 .md 导出(可选)
# ---------------------------------------------------------------------------


def _article_to_markdown(article: dict[str, Any]) -> str:
    """把一条 article 渲染为「干干净净的英文原文」(不要 front matter / 标题 / 摘要 / 链接等)。

    只返回 content_raw.strip(),其它字段(title / summary_md / url / section 等)全部丢弃。
    """
    return (article.get("content_raw", "") or "").strip() + "\n"


# 文件名里标题部分的最大长度(超出按 word boundary 截断)
_MAX_TITLE_LEN = 60


def _slugify_title(title: str, max_len: int = _MAX_TITLE_LEN) -> str:
    """把标题转成适合做文件名的 slug。

    - 全小写
    - 非字母数字 / 中文字符 → -
    - 连续 - 合并
    - 去掉首尾 -
    - 长度限制(超出在最近的 - 边界截断,避免切到一半单词)
    - 空标题返回空字符串(让调用方只拿 id 命名)
    """
    if not title:
        return ""
    slug = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "-", title).strip("-").lower()
    if len(slug) <= max_len:
        return slug
    # 在 max_len 之前的最后一个 - 处截断,避免切到单词中间
    truncated = slug[:max_len]
    last_dash = truncated.rfind("-")
    if last_dash > max_len // 2:
        truncated = truncated[:last_dash]
    return truncated.strip("-")


def _article_filename(article: dict[str, Any]) -> str:
    """构造文件名:`{slug-title}-{id}.md`。

    - 标题太短或为空 → 只用 `{id}.md`
    - 标题 slug 截断后为空 → 只用 `{id}.md`
    - 标题 + id 之间用 `-` 拼接,便于 grep / Tab 补全 / 跨软件引用
    """
    art_id = article.get("id", "").strip()
    if not art_id:
        raise ValueError("article 必须有 id 字段")
    slug = _slugify_title(article.get("title", "") or "")
    if not slug:
        return f"{art_id}.md"
    return f"{slug}-{art_id}.md"


def write_article_md(article: dict[str, Any], output_dir: Path) -> Path:
    """把一条 article 写到 output_dir / {issue_date} / {slug-title}-{id}.md。

    - 在 output_dir 下按 issue_date(YYYY-MM-DD)建子目录,便于按期翻阅
    - 文件名 = 标题 slug + id(便于 grep / Tab 补全 / 跨软件引用)
    - 原子写(临时文件 + os.replace)
    - 创建中间目录(如不存在)
    - 覆盖已存在(同一 article.id 反复抓取应更新,而不是留多份)
    返回最终文件路径。
    """
    art_id = article.get("id", "").strip()
    if not art_id:
        raise ValueError("article 必须有 id 字段")
    issue_date = article.get("issue_date", "").strip()
    if not issue_date:
        # 兜底:缺 issue_date 时直接放根目录,不强行猜目录名
        sub_dir = output_dir
    else:
        sub_dir = output_dir / issue_date
    sub_dir.mkdir(parents=True, exist_ok=True)

    content = _article_to_markdown(article)
    out_path = sub_dir / _article_filename(article)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f"{out_path.name}.", suffix=".tmp", dir=sub_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, out_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        raise
    return out_path


def _maybe_export_article_md(cfg: dict[str, Any], article: dict[str, Any]) -> None:
    """如果 paths.article_md_dir 已配置,把 article 写成 .md。

    任何错误都只 log 不抛,不影响抓取主流程。
    """
    paths_cfg = cfg.get("paths") or {}
    dir_str = paths_cfg.get("article_md_dir") or ""
    if not dir_str:
        return
    output_dir = Path(dir_str)
    try:
        out = write_article_md(article, output_dir)
        log.info(f"[md] 已导出 {out.name} → {out} ({out.stat().st_size:,} bytes)")
    except Exception as e:
        log.warning(f"[md] 导出失败(不影响抓取):{e}")


# ---------------------------------------------------------------------------
# 板块过滤
# ---------------------------------------------------------------------------


def is_allowed_article(url: str, section: str) -> bool:
    """保留 weeklyedition 中的所有文章，只排除确定不是文章的链接。"""
    del section  # 板块不再参与过滤，Politics、Business 等均应抓取。
    path = urlparse(url).path.lower()
    return bool(path) and not any(keyword in path for keyword in NON_ARTICLE_PATH_KEYWORDS)


# ---------------------------------------------------------------------------
# 浏览器抓取
# ---------------------------------------------------------------------------


def open_browser(user_data_path: str, headless: bool):
    """懒导入 DrissionPage,失败时给出明确指引。

    DrissionPage 4.x API:`ChromiumPage(addr_or_opts=None, ...)`。
    `ChromiumOptions()` 默认 `_address='127.0.0.1:9222'`(连已有浏览器),
    `auto_port()` 又把 `_address` 清成空串导致 `address.split(':')` 崩。
    正确做法:自己挑一个空闲端口,`set_address('127.0.0.1:<port>')` 显式设置。

    创建后立即访问 about:blank 并等待,确保浏览器 UI 渲染好(否则开窗口但 URL 框还是空的)。
    """
    try:
        from DrissionPage import ChromiumOptions, ChromiumPage  # type: ignore
    except ImportError as e:
        sys.exit(f"未安装 drissionpage: pip install -r requirements.txt ({e})")

    opts = ChromiumOptions()

    # 0. 清残留进程 + lock(脚本崩过后会留下孤儿)
    _cleanup_stale_chrome_locks(user_data_path)

    # 1. 寻找空闲端口并显式强绑地址，避开 split(':') 报错
    free_port = _find_free_port()
    opts.set_address(f"127.0.0.1:{free_port}")

    # 2. 设置用户目录
    opts.set_user_data_path(user_data_path)
    opts.headless = bool(headless)

    # 3. Mac 环境下建议加上这两个参数以增加稳定性
    opts.set_argument('--no-sandbox')
    opts.set_argument('--disable-gpu')

    page = ChromiumPage(opts)
    # 强制初始化 + 等 UI 渲染;不然后续 page.get 可能 race-condition
    # (用户报告:窗口弹出但 URL 框空)
    page.get("about:blank")
    time.sleep(1.5)
    log.info(f"浏览器已就绪  url={page.url!r}")
    return page


def _find_free_port(start: int = 9600, end: int = 59600) -> int:
    """在 [start, end] 找一个未占用的 TCP 端口。

    步进 2 是给同进程下后续可能的 tab/连接留余地。
    失败抛 RuntimeError,提示用户检查是否有其它 Chrome 实例占着。
    """
    import socket
    for port in range(start, end + 1, 2):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(
        f"未找到空闲端口 ({start}-{end}),请检查是否有残留的 Chrome 进程:ps aux | grep -i chrome"
    )


def _cleanup_stale_chrome_locks(profile_path: str | Path) -> int:
    """杀掉引用指定 profile 的残留 Chrome 进程,删除 SingletonLock 等。

    返回杀掉的进程数。多次崩溃后会留下孤儿 Chrome 实例把 profile 锁住,
    后续启动会报 BrowserConnectError。脚本启动前清一遍最稳。
    """
    import subprocess
    profile_path = str(profile_path)
    killed = 0
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"user-data-dir={profile_path}"],
            capture_output=True, text=True, timeout=5,
        )
        pids = [p.strip() for p in result.stdout.split() if p.strip().isdigit()]
        for pid in pids:
            try:
                subprocess.run(["kill", "-9", pid], timeout=3, check=False)
                killed += 1
            except Exception:
                pass
    except Exception as e:
        log.warning(f"扫 Chrome 残留进程失败:{e}")
    # 删 lock 文件(进程杀掉后这些文件就没用了)
    for fname in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = Path(profile_path) / fname
        try:
            if p.exists():
                p.unlink()
        except Exception as e:
            log.warning(f"删 lock 文件失败 {p}:{e}")
    lock_p = Path(profile_path) / "Default" / "LOCK"
    try:
        if lock_p.exists():
            lock_p.unlink()
    except Exception:
        pass
    if killed:
        log.info(f"清理了 {killed} 个残留 Chrome 进程 + lock 文件")
    return killed


def fetch_weekly_index(page, issue_date: str | None = None, debug_html_dir: Path | None = None) -> list[dict[str, str]]:
    """访问 weeklyedition 目录,返回 [{url, section, title, issue_date}, ...]。

    `issue_date` 非空时:访问 /weeklyedition/<YYYY-MM-DD> 指定期次(必须是周六)。
    `issue_date` 为空时:访问 /weeklyedition 默认页(最新期)。

    优化策略:绕过容易返回 None 的客户端 JS 执行,直接用 Python 正则解析 SSR 源码。
    """
    if issue_date:
        ok, err = validate_issue_date(issue_date)
        if not ok:
            log.error(f"[weekly] {err}")
            log.error("[weekly] 拒绝抓取(日期不是周六)。如要强行试,绕过此检查即可。")
            return []
        url = f"{WEEKLY_URL}/{issue_date}"
    else:
        url = WEEKLY_URL
    page.get(url)
    page.wait.load_start()
    time.sleep(2.0)  # 留出基础加载时间

    try:
        log.info(f"[weekly] 确认浏览器当前真实 URL: {page.url!r} | 标题: {page.title!r}")
    except Exception as e:
        log.warning(f"[weekly] 读取浏览器基本状态失败: {e}")

    # 获取完整的 HTML 源码
    html_source = page.html

    if debug_html_dir is not None:
        debug_html_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        (debug_html_dir / f"weekly_{stamp}.html").write_text(html_source, encoding="utf-8")
        log.info(f"[weekly] 已 dump 诊断 HTML 到 {debug_html_dir}/weekly_{stamp}.html")

    articles = _parse_weeklyedition_html_v2(html_source)
    cover_url = _weekly_cover_url_from_html(html_source) or _visible_weekly_cover_url(page)
    if cover_url:
        for article in articles:
            article["cover_image_url"] = cover_url
        log.info(f"[weekly] 已识别本期顶部封面图: {cover_url[:120]}")
    else:
        log.warning("[weekly] 未识别到本期顶部封面图，不影响文章抓取")
    return articles


def _weekly_cover_url_from_html(html: str) -> str:
    """从 weekly edition 的结构化 content.cover 读取页面顶部期刊封面。"""
    try:
        matched = re.search(r'<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', html)
        if not matched:
            return ""
        payload = json.loads(matched.group(1).strip())
        cover = payload.get("props", {}).get("pageProps", {}).get("content", {}).get("cover", {})
        value = cover.get("url", "") if isinstance(cover, dict) else ""
        return str(value) if _is_article_image_url(value) else ""
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""


def _visible_weekly_cover_url(page) -> str:
    """选取 weekly edition 首屏中面积最大的 Economist 图片作为期刊封面。"""
    try:
        value = page.run_js(
            """
            (() => Array.from(document.images)
              .map(img => {
                const rect = img.getBoundingClientRect();
                return {
                  src: img.currentSrc || img.src || '',
                  top: rect.top,
                  area: Math.max(rect.width, 0) * Math.max(rect.height, 0)
                };
              })
              .filter(item => item.src && item.top > -80 && item.top < window.innerHeight * 1.5 && item.area > 12000)
              .sort((left, right) => left.top - right.top || right.area - left.area)
              .map(item => item.src)[0] || '')()
            """
        )
    except Exception as exc:
        log.warning(f"[weekly] 读取首屏封面图失败: {exc}")
        return ""
    return str(value or "") if _is_article_image_url(value) else ""


def _parse_weeklyedition_html_v2(html: str) -> list[dict[str, str]]:
    """终极解析版：通过解析 Next.js 的 __NEXT_DATA__ 结构化数据块实现 100% 稳健抓取"""
    import json
    import re
    from datetime import datetime

    out: list[dict[str, str]] = []

    try:
        # 1. 精准提取隐藏在 HTML 尾部的 Next.js 原生 JSON 数据块[cite: 5]
        next_data_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', html)
        if not next_data_match:
            log.error("[weekly] 严重错误：在 HTML 页面中未找到 __NEXT_DATA__ 标识，请检查是否触发高强度人机验证。")
            return []

        # 2. 解析 JSON
        raw_json = json.loads(next_data_match.group(1).strip())

        # 3. 顺藤摸瓜找到周刊的组件树路径[cite: 5]
        page_props = raw_json.get("props", {}).get("pageProps", {})
        content_data = page_props.get("content", {})
        components = content_data.get("components", [])  # 所有的板块大合集[cite: 5]

        # 获取封面日期用于归档[cite: 5]
        issue_date_raw = content_data.get("issueDate", "")  # 格式如 "2026-07-11T00:00:00.000Z"[cite: 5]
        if issue_date_raw:
            issue_date = issue_date_raw.split("T")[0]
        else:
            issue_date = datetime.now().strftime("%Y-%m-%d")

        log.info(f"[weekly] 成功加载 Next.js 核心数据，检测到本期封面日期为: {issue_date}[cite: 5]")

        # 4. 遍历每个 Section 组件[cite: 5]
        for comp in components:
            if comp.get("type") != "COLLECTION":
                continue

            section_name = comp.get("name", "Unknown").strip()  # 比如 "Leaders", "Science & technology"[cite: 5]
            articles = comp.get("articles", [])  # 该板块下的文章数组[cite: 5]

            for art in articles:
                title = art.get("headline", "").strip()  # 原生标题[cite: 5]
                relative_url = art.get("url", "").strip()  # 相对或绝对路径[cite: 5]

                if not relative_url or not title:
                    continue

                # 补全绝对路径 URL
                if relative_url.startswith("/"):
                    full_url = "https://www.economist.com" + relative_url
                else:
                    full_url = relative_url

                # 5. 调用你原有的板块白名单+排除政治商业过滤函数[cite: 3]
                if not is_allowed_article(full_url, section_name):
                    continue

                out.append({
                    "url": full_url,
                    "section": section_name,
                    "title": title[:200],
                    "issue_date": issue_date,
                })

    except Exception as e:
        log.error(f"[weekly] 解析 JSON 数据流时发生异常: {e}", exc_info=True)
        return []

    log.info(f"[weekly] 核心数据流解析完毕，本期共成功捕获 {len(out)} 篇符合条件的有效文章。")
    return out


def _is_article_image_url(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith(("https://", "http://")):
        return False
    parsed = urlparse(value)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if re.search(r"/\d{8}_wwp\d*\.(?:jpg|jpeg|png|webp)$", path):
        return False
    return (
        host.endswith("images.economist.com")
        or (host.endswith("economist.com") and "/img/" in path)
        or (host.endswith("economist.com") and path.endswith((".jpg", ".jpeg", ".png", ".webp")))
    )


def _collect_article_image_urls(value: Any, output: list[str], seen: set[str]) -> None:
    """从 Next.js 文章数据中提取 Economist CDN 图片 URL。"""
    if isinstance(value, str):
        if _is_article_image_url(value) and value not in seen:
            seen.add(value)
            output.append(value)
        return
    if isinstance(value, list):
        for item in value:
            _collect_article_image_urls(item, output, seen)
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_article_image_urls(item, output, seen)


def _visible_article_image_urls(page) -> list[str]:
    """只抓 Explore more 之前已渲染的正文和漫画图片。"""
    try:
        values = page.run_js(
            """
            (() => {
              const marker = Array.from(document.querySelectorAll('h1,h2,h3,h4,p,span,a,div'))
                .find(el => el.textContent.trim() === 'Explore more');
              const beforeMarker = node => !marker || Boolean(
                node.compareDocumentPosition(marker) & Node.DOCUMENT_POSITION_FOLLOWING
              );
              const imageUrls = Array.from(document.querySelectorAll('img'))
                .filter(beforeMarker).map(img => img.currentSrc || img.src);
              const backgroundUrls = Array.from(document.querySelectorAll('[style*="background-image"]'))
                .filter(beforeMarker).map(node => getComputedStyle(node).backgroundImage)
                .map(value => (value.match(/url\\(["']?(.*?)["']?\\)/) || [])[1]);
              return imageUrls.concat(backgroundUrls).filter(Boolean);
            })()
            """
        ) or []
    except Exception as exc:
        log.warning(f"图片 DOM 提取失败(不影响正文): {exc}")
        return []
    return [value for value in values if _is_article_image_url(value)]


def fetch_article_content(page, url: str) -> tuple[str, str, list[str]]:
    """访问单篇文章，返回 (title, content_raw, image_urls)。

    【核心修正版】：放弃不稳定的动态 DOM 抓取，全面转向提取并解析页面底部的 __NEXT_DATA__ JSON 块。
    100% 免疫前端改名、懒加载截断和动态闪烁，确保长文章全文无损恢复。
    """
    page.get(url)
    page.wait.load_start()
    time.sleep(1.5)  # 留出基础网络数据就绪时间

    html_source = page.html
    title = ""
    body_text = ""
    image_urls: list[str] = []
    seen_image_urls: set[str] = set()

    try:
        import re
        import json

        # 1. 强行用正则切出隐藏在源码底部的 Next.js 全局结构化数据块
        next_data_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', html_source)
        if next_data_match:
            raw_json = json.loads(next_data_match.group(1).strip())

            # 2. 定位到文章的内容核心(props -> pageProps -> content)[cite: 6]
            content_data = raw_json.get("props", {}).get("pageProps", {}).get("content", {})

            # 3. 提取文章的标准 Headline[cite: 6]
            title = content_data.get("headline", "").strip()

            # 4. 精准提取完整的正文数组（躺在 JSON 里的 body 节点中）[cite: 6]
            body_components = content_data.get("body", [])
            # 正文图通常在 body 中；部分漫画和导语图只出现在文章根节点的 lead image。
            # 只读这些明确字段，不能再递归扫描整个 content，否则会混入 Explore more 的周刊封面。
            for key in (
                "image", "imageUrl", "image_url", "leadImage", "lead_image",
                "leadMedia", "leadComponent", "media", "imageData",
            ):
                _collect_article_image_urls(content_data.get(key), image_urls, seen_image_urls)
            _collect_article_image_urls(body_components, image_urls, seen_image_urls)
            paragraphs = []

            for node in body_components:
                # 如果是标准英文段落，一字不落地提取纯文本[cite: 6]
                if node.get("type") == "PARAGRAPH":
                    text = node.get("text", "").strip()
                    if text:
                        paragraphs.append(text)
                # 如果是文章内部的排版小标题(CROSSHEAD)，带上 Markdown 格式保留[cite: 6]
                elif node.get("type") == "CROSSHEAD":
                    text = node.get("text", "").strip()
                    if text:
                        paragraphs.append(f"\n## {text}\n")

            if paragraphs:
                body_text = "\n\n".join(paragraphs)
                log.info(
                    f"[single-url] 成功通过 __NEXT_DATA__ 通道提取全文，共 {len(paragraphs)} 个正文/标题节点[cite: 6]。")

    except Exception as e:
        log.error(f"[single-url] 通过 JSON 核心提取正文失败，正在切换至常规 DOM 兜底保底: {e}")

    # =========================================================================
    # 🌟 强力兜底保底逻辑：如果上面的原生通道发生未料异常，用硬捞机制保底
    # =========================================================================
    if not body_text:
        log.info("[single-url] 触发兜底机制，正在捞取可见元素...")
        payload = page.run_js(
            """
            (() => {
              // 强兼容 2026 最新版特征的 H1 和标准 H1[cite: 6]
              const h1 = document.querySelector('h1.css-1tik00t') 
                      || document.querySelector('h1[data-test-id="article-headline"]') 
                      || document.querySelector('h1');

              // 强捞所有带 paragraph 标记的组件，以及普通的 p 标签[cite: 6]
              const ps = Array.from(document.querySelectorAll('p[data-component="paragraph"], div.article-body p, p'));
              return {
                title: h1 ? h1.innerText.trim() : '',
                body: ps.map(p => p.innerText.trim()).filter(Boolean).join('\\n\\n')
              };
            })()
            """
        )
        title = title or (payload or {}).get("title", "")
        body_text = (payload or {}).get("body", "")

    for image_url in _visible_article_image_urls(page):
        if image_url not in seen_image_urls:
            seen_image_urls.add(image_url)
            image_urls.append(image_url)
    return title, body_text, image_urls


def _image_extension(url: str, content_type: str) -> str:
    content_type = content_type.lower().split(";", 1)[0].strip()
    by_type = {
        "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
        "image/webp": ".webp", "image/gif": ".gif",
    }
    if content_type in by_type:
        return by_type[content_type]
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".jpg"


def materialize_article_images(
    image_urls: list[str], cfg: dict[str, Any], issue_date: str, article_id: str,
) -> list[str]:
    """下载图片到期刊目录；下载失败时保留原 URL，确保页面仍可展示。"""
    if not image_urls:
        return []
    try:
        import requests
    except ImportError as exc:
        log.warning(f"未安装 requests，图片保留远程 URL: {exc}")
        return image_urls

    image_dir = _paper_output_root(cfg) / PAPER_PUBLICATION_TYPE / issue_date / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for index, image_url in enumerate(image_urls[:20], start=1):
        try:
            response = requests.get(
                image_url,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"},
                timeout=30,
            )
            content_type = response.headers.get("Content-Type", "")
            if response.status_code != 200 or not content_type.lower().startswith("image/"):
                raise ValueError(f"HTTP {response.status_code}, Content-Type={content_type!r}")
            filename = f"{article_id}_{index:02d}{_image_extension(image_url, content_type)}"
            target = image_dir / filename
            _write_atomic_bytes(target, response.content)
            paths.append(f"images/{filename}")
        except Exception as exc:
            log.warning(f"图片下载失败，保留远程 URL: {image_url[:100]} ({exc})")
            paths.append(image_url)
    return paths


def materialize_issue_cover(cover_url: str, cfg: dict[str, Any], issue_date: str) -> str:
    """下载 weekly edition 首屏封面，返回相对期刊目录的路径。"""
    if not _is_article_image_url(cover_url):
        return ""
    try:
        import requests

        response = requests.get(
            cover_url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"},
            timeout=30,
        )
        content_type = response.headers.get("Content-Type", "")
        if response.status_code != 200 or not content_type.lower().startswith("image/"):
            raise ValueError(f"HTTP {response.status_code}, Content-Type={content_type!r}")
        target = _paper_output_root(cfg) / PAPER_PUBLICATION_TYPE / issue_date / f"cover{_image_extension(cover_url, content_type)}"
        _write_atomic_bytes(target, response.content)
        return target.name
    except Exception as exc:
        log.warning(f"[weekly] 封面图下载失败，不影响文章抓取: {exc}")
        return ""


def analyze_article_images(
    client: Any,
    cfg: dict[str, Any],
    issue_date: str,
    title: str,
    images: list[str],
    log_: logging.Logger,
) -> list[dict[str, Any]]:
    """用 .env LLM 对正文图片/图表做 50-80 字中文简析。"""
    settings = cfg["image_analysis"]
    if not settings.get("enabled") or not images:
        return []
    output_root = _paper_output_root(cfg)
    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": (
            "请逐张分析下面文章中的图片或图表。每张只写一段 50-80 个中文字符的简短说明，"
            "说明画面/图表展示的内容及其与文章的关系；不要编造图片中看不出的数字或事实。"
            "返回严格 JSON，不要 Markdown。格式："
            '{"images":[{"index":1,"image_type":"photo|chart|cartoon|illustration",'
            '"description":"50-80字中文简析"}]}\n文章标题：' + title
        ),
    }]
    usable_images = images[: max(1, int(settings["max_images"]))]
    for image_path in usable_images:
        if image_path.startswith("images/"):
            local_path = output_root / PAPER_PUBLICATION_TYPE / issue_date / image_path
            try:
                import base64
                import mimetypes
                mime = mimetypes.guess_type(local_path.name)[0] or "image/jpeg"
                data = base64.b64encode(local_path.read_bytes()).decode("ascii")
                image_url = f"data:{mime};base64,{data}"
            except Exception as exc:
                log_.warning(f"读取本地图片失败，跳过解析 {image_path}: {exc}")
                continue
        elif _is_article_image_url(image_path):
            image_url = image_path
        else:
            continue
        content.append({"type": "image_url", "image_url": {"url": image_url, "detail": "low"}})

    if len(content) == 1:
        return []
    model = settings.get("model") or cfg["llm"].get("model", "gpt-4o-mini")
    for attempt in range(int(settings["max_retries"]) + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是严谨的图片与数据图表编辑，只返回 JSON。"},
                    {"role": "user", "content": content},
                ],
                max_tokens=int(settings["max_tokens"]),
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            raw = _extract_json_payload(response.choices[0].message.content or "")
            raw_items = (
                raw.get("images")
                or raw.get("image_insights")
                or raw.get("analyses")
                or raw.get("items")
                or []
            )
            if not isinstance(raw_items, list):
                raise ValueError("图片解析结果不是 images 数组")
            insights: list[dict[str, Any]] = []
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                try:
                    index = int(item.get("index") or item.get("image_index") or item.get("image_number") or 0)
                except (TypeError, ValueError):
                    continue
                if index == 0 and len(usable_images) == 1:
                    index = 1
                description = str(
                    item.get("description") or item.get("analysis") or item.get("caption") or ""
                ).strip()
                if 35 <= count_cn_chars(description) < 50:
                    description += "，帮助读者把握文章所讨论的背景与变化。"
                if not (1 <= index <= len(usable_images)) or not (50 <= count_cn_chars(description) <= 80):
                    continue
                image_type = str(item.get("image_type") or "illustration").strip().lower()
                if image_type not in {"photo", "chart", "cartoon", "illustration"}:
                    image_type = "illustration"
                insights.append({
                    "path": usable_images[index - 1],
                    "image_type": image_type,
                    "description": description,
                })
            if insights:
                return insights
            raise ValueError(
                "图片解析结果没有合格的 50-80 字说明: "
                + json.dumps(raw, ensure_ascii=False)[:500]
            )
        except Exception as exc:
            log_.warning(f"图片解析失败 (attempt {attempt + 1}): {exc}")
            if not _should_retry_llm_error(exc, attempt, int(settings["max_retries"])):
                break
    return []


def _parse_article_html(html: str) -> tuple[str, str]:
    """从文章页 HTML 中抽 (title, content_raw)。

    - title: 第一个 <h1> 的纯文本
    - content_raw: 所有 <p data-component="paragraph"> 段落拼接(双换行分隔),
      每段去掉内嵌 HTML(<span>/<small>/<a>)只留文本
    """
    # 标题
    title = ""
    m = ARTICLE_H1_RE.search(html)
    if m:
        title = re.sub(r"<[^>]+>", "", m.group(1)).strip()

    # 正文段落
    paragraphs: list[str] = []
    for m in ARTICLE_P_RE.finditer(html):
        # 去内嵌 tag(drop caps 之类)
        text = re.sub(r"<[^>]+>", "", m.group(1))
        # 解码 HTML 实体
        text = (text.replace("&amp;", "&")
                    .replace("&lt;", "<")
                    .replace("&gt;", ">")
                    .replace("&quot;", '"')
                    .replace("&#39;", "'")
                    .replace("&nbsp;", " "))
        text = re.sub(r"\s+", " ", text).strip()
        if text and len(text) > 10:  # 跳过太短的(可能是 drop cap 残段)
            paragraphs.append(text)
    body = "\n\n".join(paragraphs)
    return title, body


# ---------------------------------------------------------------------------
# LLM 总结
# ---------------------------------------------------------------------------


def _normalize_base_url(url: str) -> str:
    """OpenAI SDK 会在 base_url 后自动拼 /chat/completions。

    如果用户配的是完整 endpoint(包含 /chat/completions),SDK 会拼成双段导致 404。
    这里自动剥离尾部 /chat/completions(以及可能的尾斜杠)。
    """
    u = url.rstrip("/")
    suffix = "/chat/completions"
    if u.endswith(suffix):
        u = u[: -len(suffix)].rstrip("/")
    return u


def make_llm_client(cfg: dict[str, Any]):
    """懒导入 OpenAI,避免无 LLM 依赖时测试失败。"""
    from openai import OpenAI  # type: ignore
    llm = cfg["llm"]
    kwargs: dict[str, Any] = {"api_key": llm["api_key"], "timeout": llm.get("timeout_s", 60)}
    base = llm.get("base_url")
    if base:
        kwargs["base_url"] = _normalize_base_url(base)
    return OpenAI(**kwargs)


def _strip_cot(text: str) -> str:
    """剥掉 LLM 返回里的英文 Chain of Thought,只留从「🌟 一句话核心主旨」开始的中文 Markdown。

    标记位置策略(从最具体到最宽松):
      1. 精确匹配「🌟 一句话核心主旨」(含 emoji)→ 从该位置开始
      2. 退而求其次匹配「一句话核心主旨」(无 emoji,某些模型会剥掉)
      3. 都没有 → 返回原文(后续 MIN_CN_CHARS 校验会失败,自然触发重试/丢弃)

    用 rfind(取最后出现的位置)是因为模型偶尔会在 CoT 里复述一遍 marker,
    取最后一次出现的版本能避开「假」marker,得到真正的最终回答。
    """
    for marker in ("🌟 一句话核心主旨", "一句话核心主旨"):
        idx = text.rfind(marker)
        if idx < 0:
            continue
        # 若 marker 前 10 字符内出现 ### 标题前缀,把它一起带上,保持 Markdown 完整
        prefix_start = max(0, idx - 10)
        prefix = text[prefix_start:idx]
        if "###" in prefix:
            start = prefix_start + prefix.rfind("###")
        else:
            start = idx
        return text[start:].strip()
    return text  # 不裁剪,让下游兜底


def summarize(
        client: Any,
        cfg: dict[str, Any],
        title: str,
        body: str,
        log_: logging.Logger,
) -> str:
    """调 LLM 生成中文摘要。

    【无情中文字段提取版】：
    1. 不再盲目进行从头到尾的粗暴切片。
    2. 采用精确块提取技术，分别去捞 4 个核心中文大类下最纯净的中文文本。
    3. 彻底过滤、原地蒸发任何大模型吐出来的中英混合碎碎念与字数统计。
    """
    llm = cfg["llm"]
    user_prompt = f"标题:{title}\n\n原文:\n{body[:12000]}"
    last_err: Exception | None = None

    for attempt in range(int(cfg["crawl"].get("max_retries", 2)) + 1):
        try:
            resp = client.chat.completions.create(
                model=llm.get("model", "gpt-4o-mini"),
                messages=[
                    {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=2048,
                temperature=0.3,
            )
            raw_text = (resp.choices[0].message.content or "").strip()

            # =========================================================================
            # 🎯 核心修正：精准打流，分块硬捞中文内容，彻底干掉任何英文和复读干扰
            # =========================================================================
            # 分别提取 4 个核心块的内容（通过寻找标志位之间的中文段落）
            block_1 = re.search(r'🌟\s*一句话核心主旨[\s\S]*?(?=🔍\s*核心观点|🤨\s*争议|🔮\s*未来|$)', raw_text)
            block_2 = re.search(r'🔍\s*核心观点与论据拆解[\s\S]*?(?=🤨\s*争议|🔮\s*未来|$)', raw_text)
            block_3 = re.search(r'🤨\s*争议与潜在挑战[\s\S]*?(?=🔮\s*未来|$)', raw_text)
            block_4 = re.search(r'🔮\s*未来趋势预判[\s\S]*?$', raw_text)

            # 安全组装 Markdown 字符串
            final_blocks = []
            if block_1: final_blocks.append(
                "### 🌟 一句话核心主旨\n" + block_1.group(0).split("一句话核心主旨")[-1].strip())
            if block_2: final_blocks.append(
                "### 🔍 核心观点与论据拆解\n" + block_2.group(0).split("核心观点与论据拆解")[-1].strip())
            if block_3: final_blocks.append(
                "### 🤨 争议与潜在挑战\n" + block_3.group(0).split("争议与潜在挑战")[-1].strip())
            if block_4:
                # 未来趋势可能带有多余英文统计，清洗掉英文字符开头的内容
                b4_text = block_4.group(0).split("未来趋势预判")[-1].strip()
                # 寻找可能存在的英文统计小句，平滑切除
                b4_text = re.split(r'\n(?:Let|Wait|I\s|Re-reading)', b4_text)[0].strip()
                final_blocks.append("### 🔮 未来趋势预判\n" + b4_text)

            # 如果捞取出了哪怕一两个干净的中文块，用它们作为完美输出
            if len(final_blocks) >= 2:
                final_chinese_summary = "\n\n".join(final_blocks)
            else:
                final_chinese_summary = raw_text  # 兜底保底

            # =========================================================================
            # 🎯 尾部残句平滑切除防乱码
            # =========================================================================
            if final_chinese_summary and final_chinese_summary[-1] not in ['。', '？', '！', '」', '】', '`']:
                last_period = final_chinese_summary.rfind('。')
                if last_period > 0:
                    final_chinese_summary = final_chinese_summary[:last_period + 1]

            # 最终中文字数统计
            cn_count = count_cn_chars(final_chinese_summary)
            if cn_count >= MIN_CN_CHARS:
                return final_chinese_summary

            if attempt >= 1:
                last_err = ValueError(f"摘要纯净字数不足({cn_count}<{MIN_CN_CHARS})")
                break
            log_.warning(f"摘要纯净字数仍未达标({cn_count}<{MIN_CN_CHARS})，再重试一次")

        except Exception as e:
            last_err = e
            log_.warning(f"LLM 连线调用失败 (attempt {attempt + 1}): {e}")
            if not _should_retry_llm_error(e, attempt, int(cfg["crawl"].get("max_retries", 2))):
                break

        time.sleep(1.0)

    log_.error(f"摘要最终失败，丢弃: {title} ({last_err})")
    return ""


def count_cn_chars(s: str) -> int:
    return len(CN_CHAR_RE.findall(s))


def _split_article_paragraphs(body: str) -> list[dict[str, str]]:
    """把文章正文拆成可翻译段落,保留 crosshead / 普通段落的角色信息。"""
    paragraphs: list[dict[str, str]] = []
    for part in re.split(r"\n\s*\n", str(body or "").strip()):
        text = part.strip()
        if not text:
            continue
        role = "body"
        if text.startswith("## "):
            role = "crosshead"
            text = text[3:].strip()
        elif text.startswith("### "):
            role = "crosshead"
            text = text[4:].strip()
        paragraphs.append({"role": role, "en_text": text})
    return paragraphs


def _format_source_content_markdown(paragraphs: list[dict[str, str]]) -> str:
    """把源正文整理成稳定的英文 Markdown。"""
    rendered: list[str] = []
    for paragraph in paragraphs:
        text = str(paragraph.get("en_text") or "").strip()
        if not text:
            continue
        if paragraph.get("role") == "crosshead":
            rendered.append(f"## {text}")
        else:
            rendered.append(text)
    return "\n\n".join(rendered).strip()


def _extract_json_payload(text: str) -> dict[str, Any]:
    """从 LLM 响应里抠出 JSON object。"""
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.IGNORECASE).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("LLM JSON 不是 object")
    return data


def _should_retry_llm_error(exc: Exception, attempt: int, max_retries: int) -> bool:
    """只重试短暂故障；422、认证和参数错误不会因重复请求而恢复。"""
    if attempt >= max_retries:
        return False
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code in {408, 409, 425, 429} or status_code >= 500
    if isinstance(exc, json.JSONDecodeError):
        return attempt == 0
    message = str(exc).lower()
    if "connection" in message or "timed out" in message or "timeout" in message:
        return True
    return "中文解读字数不合格" in str(exc) and attempt == 0


def _normalise_compiled_paragraphs(
    source_paragraphs: list[dict[str, str]],
    raw_paragraphs: Any,
    article_id: str,
) -> list[dict[str, str]]:
    """把 LLM 翻译结果对齐回原始段落。"""
    translated: list[dict[str, str]] = []
    raw_items = raw_paragraphs if isinstance(raw_paragraphs, list) else []
    for index, source in enumerate(source_paragraphs, start=1):
        raw_item = raw_items[index - 1] if index - 1 < len(raw_items) else {}
        if not isinstance(raw_item, dict):
            raw_item = {}
        zh_text = str(raw_item.get("zh_text") or raw_item.get("translation") or "").strip()
        role = str(raw_item.get("role") or source.get("role") or "body").strip() or "body"
        translated.append(
            {
                "para_id": str(raw_item.get("para_id") or f"{article_id}_p{index}"),
                "en_text": str(source.get("en_text") or "").strip(),
                "zh_text": zh_text,
                "role": role,
            }
        )
    return translated


def _glossary_id(term: str, term_type: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-")[:80]
    return f"{term_type}-{slug or 'term'}"


def _glossary_prompt(title: str, paragraphs: list[dict[str, str]], max_terms: int) -> str:
    rendered = []
    for index, paragraph in enumerate(paragraphs, start=1):
        zh_text = str(paragraph.get("zh_text") or "").strip()
        if zh_text:
            rendered.append(f"[P{index}.ZH] {zh_text}")
    return f"""You are a senior English-Chinese translator and global political-economic background editor. Analyze the Chinese translation below and select at most {max_terms} English-language proper terms that genuinely need contextual explanation for a Chinese reader.

Only annotate an exact English substring that remains visible in [P<number>.ZH]. Never annotate ordinary English vocabulary, generic abstract concepts, common roles, or terms such as democracy, inflation, President, US, CEO and similar common words.

Allowed types only: person, organization, company, policy_law, event, place_context, work, proper_concept, acronym.
Each description_zh must be an objective Chinese introduction of roughly 100-200 Chinese characters, stating what it is and why it matters in this article. Do not invent facts. An occurrence may contain only the first useful occurrence in the Chinese column.

Return strict JSON only. Do not quote or reproduce the article outside the exact surface field.

Article title: {title}

{chr(10).join(rendered)}

Return JSON ONLY:
{{
  "terms": [
    {{
      "term": "Jerome Powell",
      "term_zh": "杰罗姆·鲍威尔",
      "type": "person",
      "description_zh": "100-200字中文介绍",
      "occurrences": [
        {{"paragraph_index": 3, "text_field": "zh_text", "surface": "Jerome Powell", "occurrence": 1}}
      ]
    }}
  ]
}}"""


def _apply_glossary_terms(
    article: dict[str, Any], raw_terms: Any, complete: bool, max_terms: int | None = None,
) -> dict[str, Any]:
    """校验模型返回并转成前端使用的 glossary 和段落定位结构。"""
    paragraphs = article.get("paragraphs") or []
    entries: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    max_terms = max(1, int(max_terms or DEFAULTS["glossary"]["max_terms"]))
    if not isinstance(raw_terms, list):
        raw_terms = []
    for raw in raw_terms:
        if len(entries) >= max_terms or not isinstance(raw, dict):
            break
        term = str(raw.get("term") or raw.get("canonical_term") or "").strip()
        term_type = str(raw.get("type") or "proper_concept").strip().lower()
        description = str(raw.get("description_zh") or raw.get("explanation_zh") or "").strip()
        if not term or term_type not in GLOSSARY_TYPES or len(CN_CHAR_RE.findall(description)) < 60:
            continue
        glossary_id = _glossary_id(term, term_type)
        if glossary_id in seen_ids:
            continue
        valid_occurrences = []
        for occurrence in raw.get("occurrences") or []:
            if not isinstance(occurrence, dict):
                continue
            try:
                paragraph_index = int(occurrence.get("paragraph_index") or 0)
                ordinal = max(int(occurrence.get("occurrence") or 1), 1)
            except (ValueError, TypeError):
                continue
            surface = str(occurrence.get("surface") or term).strip()
            if not (1 <= paragraph_index <= len(paragraphs)):
                continue
            zh_text = str(paragraphs[paragraph_index - 1].get("zh_text") or "")
            if surface and surface in zh_text:
                valid_occurrences.append({
                    "glossary_id": glossary_id, "paragraph_index": paragraph_index,
                    "text_field": "zh_text", "surface": surface, "occurrence": ordinal,
                })
        if not valid_occurrences:
            continue
        seen_ids.add(glossary_id)
        entries.append({
            "id": glossary_id, "term": term, "term_zh": str(raw.get("term_zh") or "").strip(),
            "type": term_type, "description_zh": description[:200].rstrip(), "version": GLOSSARY_VERSION,
        })
        annotations.extend(valid_occurrences[:1])
    article["glossary_entries"] = entries
    article["term_annotations"] = annotations
    article["glossary_analysis_complete"] = complete
    article["glossary_version"] = GLOSSARY_VERSION if complete else 0
    return article


def enrich_article_glossary(
    client: Any, cfg: dict[str, Any], article: dict[str, Any], log_: logging.Logger,
) -> dict[str, Any]:
    """按 auto-paper-md-converter 的 glossary schema 为文章添加可定位术语。"""
    glossary_cfg = cfg["glossary"]
    paragraphs = article.get("paragraphs") or []
    if not glossary_cfg.get("enabled") or not any(p.get("zh_text") for p in paragraphs):
        article["glossary_entries"] = []
        article["term_annotations"] = []
        article["glossary_analysis_complete"] = False
        article["glossary_version"] = 0
        return article

    model = glossary_cfg.get("model") or cfg["llm"].get("model", "gpt-4o-mini")
    prompt = _glossary_prompt(article["title"], paragraphs, max(1, int(glossary_cfg["max_terms"])))
    raw_terms: Any = []
    for attempt in range(int(glossary_cfg["max_retries"]) + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": "Return JSON only."}, {"role": "user", "content": prompt}],
                max_tokens=int(glossary_cfg["max_tokens"]),
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            raw_terms = _extract_json_payload(response.choices[0].message.content or "").get("terms", [])
            break
        except Exception as exc:
            log_.warning(f"关键词解析失败 (attempt {attempt + 1}): {exc}")
            if not _should_retry_llm_error(exc, attempt, int(glossary_cfg["max_retries"])):
                break
    return _apply_glossary_terms(article, raw_terms, complete=True, max_terms=int(glossary_cfg["max_terms"]))


def _compile_article_prompt(
    title: str,
    section: str,
    paragraphs: list[dict[str, str]],
    glossary_enabled: bool,
    glossary_max_terms: int,
) -> str:
    paragraph_lines = []
    for index, paragraph in enumerate(paragraphs, start=1):
        role = paragraph.get("role") or "body"
        text = str(paragraph.get("en_text") or "").strip()
        if not text:
            continue
        paragraph_lines.append(f"{index}. [{role}] {text}")

    return f"""You are a meticulous English-Chinese editor for The Economist.

Translate and structure the following article into STRICT JSON only.

Rules:
- Keep the meaning faithful and do not add facts.
- title_zh must be a concise, natural Chinese title.
- summary_md must be a cohesive, flowing Chinese analysis of 400-500 Chinese characters, never exceeding 600 Chinese characters. Naturally integrate the core message, key arguments, supporting evidence, and potential implications. Use professional prose for a knowledgeable Chinese reader. Do not use bullet points, numbered lists, or section headers.
- Translate every paragraph semantically and naturally, not word-for-word.
- Preserve the paragraph order and count exactly.
- If a paragraph is a subheading/crosshead, translate it as a short Chinese heading.
- For proper nouns that need context, retain the English original at first mention in parentheses so they can be annotated later.
- Return JSON only. No Markdown fences, no explanations, no extra text.
{f'''- Also return at most {glossary_max_terms} genuinely useful proper terms in glossary_terms. A term's surface must remain exactly visible in the specified Chinese paragraph. Use only: person, organization, company, policy_law, event, place_context, work, proper_concept, acronym. Each description_zh must contain 60-100 Chinese characters and must not invent facts.''' if glossary_enabled else ''}

Title: {title}
Section: {section}

Source paragraphs:
{chr(10).join(paragraph_lines)}

Return JSON in this shape:
{{
  "title_zh": "中文标题",
  "summary_md": "一句中文解读",
  "paragraphs": [
    {{
      "zh_text": "中文翻译",
      "role": "body"
    }}
  ],
  "glossary_terms": [
    {{
      "term": "English proper term",
      "term_zh": "中文名称",
      "type": "organization",
      "description_zh": "60-100字中文背景说明",
      "occurrences": [{{"paragraph_index": 1, "surface": "中文段落中保留的英文原词", "occurrence": 1}}]
    }}
  ]
}}
"""


def compile_article_record(
    client: Any,
    cfg: dict[str, Any],
    *,
    issue_date: str,
    section: str,
    title: str,
    url: str,
    body: str,
    article_id: str,
    log_: logging.Logger,
    images: list[str] | None = None,
) -> dict[str, Any] | None:
    """把抓到的正文编译成 Economist 前端需要的结构化 article。"""
    source_paragraphs = _split_article_paragraphs(body)
    if not source_paragraphs:
        if not images:
            return None
        article = {
            "id": article_id,
            "issue_date": issue_date,
            "section": section,
            "title": title,
            "title_zh": "",
            "url": url,
            "summary_md": "",
            "content_raw": "",
            "content_markdown": "",
            "paragraphs": [],
            "images": images,
            "image_insights": [],
            "glossary_entries": [],
            "term_annotations": [],
            "glossary_analysis_complete": False,
            "glossary_version": 0,
            "compiled_article": False,
            "compile_status": "image_only",
        }
        return article

    glossary_cfg = cfg["glossary"]
    prompt = _compile_article_prompt(
        title=title,
        section=section,
        paragraphs=source_paragraphs,
        glossary_enabled=bool(glossary_cfg.get("enabled")),
        glossary_max_terms=max(1, int(glossary_cfg.get("max_terms", 12))),
    )
    llm = cfg["llm"]
    max_tokens = max(int(llm.get("max_tokens", 2048)), 4096)

    last_error: Exception | None = None
    for attempt in range(int(cfg["crawl"].get("max_retries", 2)) + 1):
        try:
            resp = client.chat.completions.create(
                model=llm.get("model", "gpt-4o-mini"),
                messages=[
                    {
                        "role": "system",
                        "content": "Return JSON only. Translate The Economist articles into faithful Chinese.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            payload = _extract_json_payload(resp.choices[0].message.content or "")
            title_zh = str(payload.get("title_zh") or "").strip()
            summary_md = str(payload.get("summary_md") or "").strip()
            compiled_paragraphs = _normalise_compiled_paragraphs(
                source_paragraphs,
                payload.get("paragraphs"),
                article_id,
            )
            if not title_zh or not summary_md:
                raise ValueError("LLM 结构化输出缺少 title_zh 或 summary_md")
            summary_cn_chars = count_cn_chars(summary_md)
            if not 400 <= summary_cn_chars <= 600:
                raise ValueError(
                    f"中文解读字数不合格({summary_cn_chars}，要求 400-600 中文字符)"
                )

            article = {
                "id": article_id,
                "issue_date": issue_date,
                "section": section,
                "title": title,
                "title_zh": title_zh,
                "url": url,
                "summary_md": summary_md,
                "content_raw": _format_source_content_markdown(source_paragraphs),
                "content_markdown": _format_source_content_markdown(source_paragraphs),
                "paragraphs": compiled_paragraphs,
                "images": images or [],
                "image_insights": [],
                "compiled_article": True,
                "compile_status": "complete",
            }
            if glossary_cfg.get("enabled"):
                return _apply_glossary_terms(
                    article,
                    payload.get("glossary_terms") or payload.get("terms"),
                    complete=True,
                    max_terms=int(glossary_cfg["max_terms"]),
                )
            return _apply_glossary_terms(article, [], complete=False)
        except Exception as exc:
            last_error = exc
            log_.warning(f"结构化编译失败 (attempt {attempt + 1}): {exc}")
            if not _should_retry_llm_error(exc, attempt, int(cfg["crawl"].get("max_retries", 2))):
                break
            time.sleep(1.0)

    log_.warning(f"结构化编译失败,回退到仅摘要模式: {title} ({last_error})")
    summary = summarize(client, cfg, title, body, log_)
    fallback_paragraphs = [
        {
            "para_id": f"{article_id}_p{index}",
            "en_text": str(paragraph.get("en_text") or ""),
            "zh_text": "",
            "role": str(paragraph.get("role") or "body"),
        }
        for index, paragraph in enumerate(source_paragraphs, start=1)
    ]
    article = {
        "id": article_id,
        "issue_date": issue_date,
        "section": section,
        "title": title,
        "title_zh": "",
        "url": url,
        "summary_md": summary,
        "content_raw": _format_source_content_markdown(source_paragraphs),
        "content_markdown": _format_source_content_markdown(source_paragraphs),
        "paragraphs": fallback_paragraphs,
        "images": images or [],
        "image_insights": [],
        "compiled_article": False,
        "compile_status": "fallback",
    }
    return enrich_article_glossary(client, cfg, article, log_)


# ---------------------------------------------------------------------------
# 飞书推送
# ---------------------------------------------------------------------------


def build_feishu_card(articles: list[dict[str, Any]], issue_date: str) -> dict[str, Any]:
    lines = "\n".join(
        f"- **[{a.get('section', 'General')}]** {a.get('title_zh') or a.get('title')}"
        for a in articles
    )
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"📰 经济学人周报更新 · {issue_date}",
                }
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": lines},
                },
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": "请双击本地 index.html 查看深度总结",
                        }
                    ],
                },
            ],
        },
    }


def push_feishu(cfg: dict[str, Any], card: dict[str, Any], log_: logging.Logger) -> None:
    """懒导入 requests,避免无网络依赖时测试失败。"""
    import requests  # type: ignore
    fs = cfg.get("feishu", {})
    if not fs.get("enabled", True):
        log_.info("飞书推送 disabled,跳过")
        return
    url = fs.get("webhook_url", "")
    if not url or "REPLACE-ME" in url:
        log_.warning("webhook 未配置,跳过推送")
        return
    try:
        r = requests.post(url, json=card, timeout=15)
        if r.status_code != 200:
            log_.error(f"飞书推送失败: {r.status_code} {r.text[:200]}")
        else:
            log_.info("飞书推送成功")
    except Exception as e:
        log_.error(f"飞书推送异常(不阻塞主流程): {e}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _next_seq(articles: list[dict[str, Any]], issue_date: str) -> int:
    used = [int(re.findall(r"(\d+)", a.get("id", "0"))[-1])
            for a in articles if a.get("id", "").startswith(f"art_{issue_date}_")]
    return (max(used) + 1) if used else 1


def _compile_article_task(cfg: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    """在线程中建立独立 LLM client，避免共享 HTTP client 的并发状态。"""
    return compile_article_record(
        make_llm_client(cfg),
        cfg,
        issue_date=payload["issue_date"],
        section=payload["section"],
        title=payload["title"],
        url=payload["url"],
        body=payload["body"],
        article_id=payload["article_id"],
        log_=log,
        images=payload["images"],
    )


def _analyze_article_images_task(cfg: dict[str, Any], issue_date: str, title: str, images: list[str]) -> list[dict[str, Any]]:
    return analyze_article_images(make_llm_client(cfg), cfg, issue_date, title, images, log)


def process_issue(
    cfg: dict[str, Any],
    issue_date: str,
    *,
    dry_run: bool = False,
    limit: int = 0,
    rewrite_id: str | None = None,
    no_feishu: bool = False,
    debug_html_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """抓一个 issue。浏览器串行，正文编译和图片解析由独立 LLM 队列并发完成。"""
    browser_cfg = cfg["browser"]
    delay_min = float(cfg["crawl"].get("delay_min_s", 5))
    delay_max = float(cfg["crawl"].get("delay_max_s", 10))
    pipeline = cfg["pipeline"]
    compile_workers = max(1, int(pipeline.get("compile_workers", 2)))
    image_workers = max(1, int(pipeline.get("image_workers", 1)))
    max_pending = max(compile_workers, int(pipeline.get("max_pending", compile_workers * 2)))

    log.info(f"启动浏览器 (user_data={browser_cfg['user_data_path']})")
    page = open_browser(browser_cfg["user_data_path"], bool(browser_cfg.get("headless", False)))

    try:
        log.info("抓取 weeklyedition 目录…")
        index = fetch_weekly_index(page, issue_date=issue_date, debug_html_dir=debug_html_dir)
        log.info(f"目录共 {len(index)} 条链接")
        # 板块过滤
        candidates = [a for a in index if is_allowed_article(a["url"], a["section"])]
        log.info(f"非文章链接过滤后剩 {len(candidates)} 条")
        if dry_run:
            dry_candidates = candidates[:limit] if limit > 0 else candidates
            for c in dry_candidates:
                print(f"[DRY] {c['issue_date']}  {c['section']:25s}  {c['title']}  {c['url']}")
            return []

        cover_url = next((str(item.get("cover_image_url") or "") for item in candidates if item.get("cover_image_url")), "")
        cover_image = materialize_issue_cover(cover_url, cfg, issue_date)

        existing = read_database_js()
        existing_by_url = {a["url"]: a for a in existing}
        if rewrite_id:
            existing = [a for a in existing if a.get("id") != rewrite_id]
            existing_by_url = {a["url"]: a for a in existing}

        new_articles: list[dict[str, Any]] = []
        seq = _next_seq(existing, issue_date)
        log.info(
            f"开始逐篇抓取(已存在 {len(existing_by_url)} 篇,本 issue 可抓取 {len(candidates)} 条"
            f"{f', 本 run 最多新增 {limit} 篇' if limit > 0 else ''}; "
            f"正文 LLM {compile_workers} 路, 图片 LLM {image_workers} 路)"
        )

        with ThreadPoolExecutor(max_workers=compile_workers, thread_name_prefix="econ-compile") as compile_pool, \
                ThreadPoolExecutor(max_workers=image_workers, thread_name_prefix="econ-image") as image_pool:
            pending_compile: dict[Future, dict[str, Any]] = {}
            pending_images: dict[Future, dict[str, Any]] = {}

            def persist_article(article: dict[str, Any]) -> None:
                existing.append(article)
                existing_by_url[article["url"]] = article
                new_articles.append(article)
                try:
                    write_database_js(existing)
                    log.info(
                        f"✓ 已收录并落盘: {article['id']} - {article['title'][:60]}  "
                        f"(本 run 第 {len(new_articles)} 篇 / 累计 {len(existing)} 篇)"
                    )
                    _maybe_export_article_md(cfg, article)
                    if cfg["image_analysis"].get("enabled") and article.get("images"):
                        future = image_pool.submit(
                            _analyze_article_images_task,
                            cfg,
                            issue_date,
                            str(article.get("title") or ""),
                            list(article["images"]),
                        )
                        pending_images[future] = article
                except Exception as exc:
                    log.error(f"写盘失败 {article.get('url')}:{exc},该篇未持久化")
                    existing.pop()
                    existing_by_url.pop(article.get("url"), None)
                    new_articles.pop()

            def drain_compiled(block: bool) -> None:
                if not pending_compile:
                    return
                done, _ = wait(
                    pending_compile,
                    timeout=None if block else 0,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    payload = pending_compile.pop(future)
                    try:
                        article = future.result()
                    except Exception as exc:
                        log.warning(f"结构化编译任务失败 {payload['url']}: {exc}")
                        continue
                    if article:
                        persist_article(article)

            def drain_images(block: bool) -> None:
                if not pending_images:
                    return
                done, _ = wait(
                    pending_images,
                    timeout=None if block else 0,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    article = pending_images.pop(future)
                    try:
                        article["image_insights"] = future.result()
                        write_database_js(existing)
                        log.info(
                            f"[images] 已解析 {article['id']}: "
                            f"{len(article.get('image_insights') or [])} 条图片说明"
                        )
                    except Exception as exc:
                        log.warning(f"图片解析任务失败 {article.get('url')}: {exc}")

            for cand in candidates:
                drain_compiled(block=False)
                drain_images(block=False)
                while limit > 0 and len(new_articles) + len(pending_compile) >= limit:
                    drain_compiled(block=True)
                    if len(new_articles) >= limit:
                        break
                if limit > 0 and len(new_articles) >= limit:
                    log.info(f"已达到本 run 限制 {limit} 篇,停止继续抓取")
                    break
                while len(pending_compile) >= max_pending:
                    drain_compiled(block=True)

                url = cand["url"]
                if url in existing_by_url:
                    log.info(f"⏭ 已存在,跳过: {cand['title'][:50]}  ({url[:60]}…)")
                    continue
                log.info(f"抓取正文: {cand['title'][:60]}")
                try:
                    title, body, image_urls = fetch_article_content(page, url)
                except Exception as exc:
                    log.warning(f"抓取失败 {url}: {exc}")
                    time.sleep(random.uniform(delay_min, delay_max))
                    continue
                if not body.strip() and not image_urls:
                    log.warning(f"未提取到正文或图片，丢弃: {url}")
                    time.sleep(random.uniform(delay_min, delay_max))
                    continue

                title = title or cand["title"]
                article_id = f"art_{issue_date}_{seq:03d}"
                seq += 1
                payload = {
                    "issue_date": issue_date,
                    "section": cand["section"],
                    "title": title,
                    "url": url,
                    "body": body,
                    "article_id": article_id,
                    "images": materialize_article_images(image_urls, cfg, issue_date, article_id),
                }
                pending_compile[compile_pool.submit(_compile_article_task, cfg, payload)] = payload
                time.sleep(random.uniform(delay_min, delay_max))

            while pending_compile:
                drain_compiled(block=True)
                drain_images(block=False)
            while pending_images:
                drain_images(block=True)

        try:
            _sync_paper_outputs(cfg, existing, issue_date=issue_date, issue_covers={issue_date: cover_image})
            _maybe_rebuild_index(cfg)
        except Exception as sync_exc:
            log.warning(f"paper 输出同步失败(不影响旧 database.js): {sync_exc}")

        log.info(f"=== 本 run 完成,新增 {len(new_articles)} 篇,database.js 累计 {len(existing)} 篇 ===")
        if new_articles and not no_feishu:
            push_feishu(cfg, build_feishu_card(new_articles, issue_date), log)
        elif not new_articles:
            log.info("本 run 无新增文章,不发飞书(避免空推)")
        return new_articles
    finally:
        try:
            page.close()
        except Exception:
            pass


def refresh_article_images(
    cfg: dict[str, Any], issue_date: str, article_ids: set[str], no_feishu: bool = True,
) -> list[dict[str, Any]]:
    """只刷新指定文章的正文图片和图片解析，不重新抓取或翻译正文。"""
    existing = read_database_js()
    targets = [
        article for article in existing
        if article.get("issue_date") == issue_date
        and (not article_ids or article.get("id") in article_ids)
    ]
    if not targets:
        log.warning(f"没有找到待刷新图片的文章: issue={issue_date}, ids={sorted(article_ids)}")
        return []

    page = open_browser(cfg["browser"]["user_data_path"], bool(cfg["browser"].get("headless", False)))
    client = make_llm_client(cfg)
    refreshed: list[dict[str, Any]] = []
    try:
        for article in targets:
            url = str(article.get("url") or "")
            try:
                _, _, image_urls = fetch_article_content(page, url)
                article["images"] = materialize_article_images(
                    image_urls, cfg, issue_date, str(article.get("id") or "article")
                )
                article["image_insights"] = analyze_article_images(
                    client, cfg, issue_date, str(article.get("title") or ""), article["images"], log
                )
                refreshed.append(article)
                log.info(
                    f"[images] 已刷新 {article.get('id')}: "
                    f"{len(article['images'])} 张图片, {len(article['image_insights'])} 条解析"
                )
            except Exception as exc:
                log.warning(f"[images] 刷新失败 {url}: {exc}")
        write_database_js(existing)
        _sync_paper_outputs(cfg, existing, issue_date=issue_date)
        _maybe_rebuild_index(cfg)
        if refreshed and not no_feishu:
            push_feishu(cfg, build_feishu_card(refreshed, issue_date), log)
        return refreshed
    finally:
        try:
            page.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Cookie 导入(把别处已登录的 cookie 灌到本机 Chrome profile)
# ---------------------------------------------------------------------------


def parse_cookie_file(path: Path) -> list[dict[str, Any]]:
    """从 JSON 数组或 Netscape cookies.txt 解析 cookie 列表。

    支持的 JSON 字段(浏览器扩展导出常见):
      name, value, domain, path, expirationDate(秒级 float),
      httpOnly, secure, sameSite, hostOnly, session

    Netscape 格式列序(domain, FLAG, path, secure, expires, name, value)。
    """
    text = path.read_text(encoding="utf-8")
    s = text.strip()
    if s.startswith("[") or s.startswith("{"):
        data = json.loads(s)
        if isinstance(data, dict):
            data = [data]
        return list(data)
    # Netscape cookies.txt
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _, path_, secure, expires, name, value = parts[:7]
        entry: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path_,
            "secure": secure.upper() == "TRUE",
        }
        if expires.lstrip("-").isdigit():
            entry["expirationDate"] = float(expires)
        out.append(entry)
    return out


def _cookie_for_drissionpage(c: dict[str, Any]) -> dict[str, Any]:
    """把浏览器扩展导出的字段映射成 DrissionPage set.cookies 接受的格式。"""
    payload: dict[str, Any] = {
        "name": c["name"],
        "value": c["value"],
        "domain": c.get("domain") or ".economist.com",
        "path": c.get("path") or "/",
    }
    exp = c.get("expirationDate")
    if isinstance(exp, (int, float)) and exp > 0:
        payload["expires"] = int(exp)
    if c.get("secure"):
        payload["secure"] = True
    if c.get("httpOnly"):
        payload["httpOnly"] = True
    return payload


PAYWALL_MARKERS = (
    "subscribe to read",
    "sign in to read",
    "create an account",
    "continue reading with",
    "already a subscriber",
)


def import_cookies_to_profile(
    cookies_file: Path,
    user_data_path: str,
    headless: bool,
) -> bool:
    """从本地文件读 cookie,灌进 Chrome profile,再访问 weeklyedition 验证。"""
    cookies_raw = parse_cookie_file(cookies_file)
    if not cookies_raw:
        log.error(f"未解析到任何 cookie: {cookies_file}")
        return False
    log.info(f"从 {cookies_file} 解析到 {len(cookies_raw)} 个 cookie")

    # 与 open_browser 一致:懒导入,缺包时给明确提示
    try:
        from DrissionPage import ChromiumOptions, ChromiumPage  # type: ignore
    except ImportError as e:
        sys.exit(f"未安装 drissionpage: pip install -r requirements.txt ({e})")
    _cleanup_stale_chrome_locks(user_data_path)
    opts = ChromiumOptions()
    opts.set_address(f"127.0.0.1:{_find_free_port()}")
    opts.set_user_data_path(user_data_path)
    opts.headless = bool(headless)
    opts.set_argument('--no-sandbox')
    opts.set_argument('--disable-gpu')
    page = ChromiumPage(opts)
    page.get("about:blank")
    time.sleep(1.5)
    log.info(f"浏览器已就绪  url={page.url!r}")
    try:
        # 必须先访问目标域,set.cookies 才不会因 domain 不匹配被丢弃
        page.get("https://www.economist.com/")
        time.sleep(1.5)
        payload = [_cookie_for_drissionpage(c) for c in cookies_raw]
        page.set.cookies(payload)
        log.info(f"已注入 {len(payload)} 个 cookie 到 Chrome profile")

        # 验证:访问 weeklyedition,扫一遍页面文本看是否还在登录墙
        page.get("https://www.economist.com/weeklyedition")
        time.sleep(2.0)
        try:
            body = (page.run_js("document.body && document.body.innerText") or "").lower()
        except Exception as e:
            log.warning(f"验证阶段读 body 失败:{e}")
            return False
        if any(m in body for m in PAYWALL_MARKERS):
            log.warning("页面文本含登录墙标记,cookie 可能不全或已过期")
            return False
        log.info("✓ 验证通过,看起来已登录 Economist")
        return True
    finally:
        try:
            page.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="经济学人周报抓取与归档")
    p.add_argument("--dry-run", action="store_true", help="只列链接,不入库不推")
    p.add_argument("--issue", default=None, help="指定 issue 日期 (YYYY-MM-DD)")
    p.add_argument("--limit", type=int, default=0, help="限制本次最多新增文章数(0=不限制)")
    p.add_argument("--refresh-images", action="store_true",
                   help="只重新抓取现有文章的正文图片并生成图片解析，不重抓正文")
    p.add_argument("--article-ids", default="",
                   help="配合 --refresh-images 使用，逗号分隔的 article id；留空表示本期全部")
    p.add_argument("--no-feishu", action="store_true", help="不推飞书")
    p.add_argument("--rewrite-id", default=None, help="强制重写指定 id 的文章")
    p.add_argument("--single-url", default=None, metavar="URL",
                   help="只抓取指定 URL 一篇(跳过 weeklyedition 目录),便于调试单篇")
    p.add_argument("--section", default=None,
                   help="配合 --single-url 使用,指定板块(默认 Manual)")
    p.add_argument("--import-cookies", default=None, metavar="FILE",
                   help="从本地 JSON / Netscape cookie 文件导入到 Chrome profile(完全本机处理)")
    p.add_argument("--debug-html", default=None, metavar="DIR",
                   help="把 weeklyedition 页面 HTML 写到该目录,便于排查 0 条原因")
    p.add_argument("--kill-stale", action="store_true",
                   help="启动前杀掉残留 Chrome 进程 + 删 lock 文件(默认也会自动做一次)")
    p.add_argument("--rebuild-index", action="store_true",
                   help="仅根据 database.js + 模板重新生成 index.html(不抓取)")
    return p.parse_args()


def _resolve_single_url_article(
    url: str,
    title: str,
    body: str,
    summary: str,
    *,
    section: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """构造单篇 article,并返回 (article, existing_after_removal)。

    逻辑:
      - 若 URL 已存在:沿用旧 article 的 issue_date / id / section
      - 若不存在:用今天的日期、新 seq、板块默认 Manual
    """
    existing = read_database_js()
    match = next((a for a in existing if a.get("url") == url), None)
    if match is not None:
        existing_after = [a for a in existing if a.get("url") != url]
        issue_date = match.get("issue_date") or datetime.now().strftime("%Y-%m-%d")
        seq = int(re.findall(r"(\d+)", match.get("id", "0"))[-1] or 1)
        section = section or match.get("section") or "Manual"
    else:
        existing_after = existing
        issue_date = datetime.now().strftime("%Y-%m-%d")
        seq = _next_seq(existing, issue_date)
        section = section or "Manual"
    article = {
        "issue_date": issue_date,
        "id": f"art_{issue_date}_{seq:03d}",
        "section": section,
        "title": title or url,
        "url": url,
        "summary_md": summary,
        "content_raw": body,
    }
    return article, existing_after


def process_single_url(
    cfg: dict[str, Any],
    url: str,
    *,
    section: str | None = None,
    no_feishu: bool = False,
) -> list[dict[str, Any]]:
    """只抓取并总结一篇指定 URL,跳过 weeklyedition 目录解析。

    用于:
      - 调试单篇 LLM 总结质量
      - 补抓 weekly 目录漏掉或抓失败的文章
      - 临时把任意文章手工归档进 database.js
    """
    if "economist.com" not in url:
        log.warning(f"URL 不像 Economist 域名,继续尝试: {url}")
    browser_cfg = cfg["browser"]
    page = open_browser(browser_cfg["user_data_path"], bool(browser_cfg.get("headless", False)))
    try:
        log.info(f"[single-url] 抓取: {url}")
        title, body, image_urls = fetch_article_content(page, url)
        if not body.strip() and not image_urls:
            log.warning(f"未提取到正文或图片，丢弃: {url}")
            return []
        log.info(f"[single-url] 抓取成功 title={title!r}  body={len(body)} chars")

        client = make_llm_client(cfg)
        article, existing_after = _resolve_single_url_article(
            url, title, body, "", section=section,
        )
        images = materialize_article_images(image_urls, cfg, article["issue_date"], article["id"])
        compiled = compile_article_record(
            client,
            cfg,
            issue_date=article["issue_date"],
            section=article["section"],
            title=article["title"],
            url=article["url"],
            body=body,
            article_id=article["id"],
            log_=log,
            images=images,
        )
        if not compiled:
            log.warning(f"[single-url] 结构化编译失败,丢弃: {url}")
            return []

        article = compiled
        if cfg["image_analysis"].get("enabled") and article.get("images"):
            article["image_insights"] = analyze_article_images(
                client, cfg, article["issue_date"], article["title"], article["images"], log
            )
        write_database_js(existing_after + [article])
        log.info(f"[single-url] ✓ 已写入: {article['id']} - {article['title']}")
        try:
            _sync_paper_outputs(cfg, existing_after + [article], issue_date=article["issue_date"])
        except Exception as sync_exc:
            log.warning(f"[single-url] paper 输出同步失败(不影响旧 database.js): {sync_exc}")
        _maybe_rebuild_index(cfg)
        _maybe_export_article_md(cfg, article)

        if not no_feishu:
            push_feishu(cfg, build_feishu_card([article], article["issue_date"]), log)
        return [article]
    finally:
        try:
            page.close()
        except Exception:
            pass


def main() -> int:
    args = parse_args()
    cfg = load_config()
    if args.import_cookies:
        log.info(f"=== 启动 cookie 导入 file={args.import_cookies} ===")
        ok = import_cookies_to_profile(
            Path(args.import_cookies),
            cfg["browser"]["user_data_path"],
            bool(cfg["browser"].get("headless", False)),
        )
        log.info(f"=== 结束  登录态:{'OK' if ok else 'FAIL'} ===")
        return 0 if ok else 1
    if args.rebuild_index:
        log.info("=== 仅重建 index.html(不抓取) ===")
        ok = _maybe_rebuild_index(cfg) or _maybe_rebuild_index.__name__  # noqa: 保留调用
        # _maybe_rebuild_index 内部已 log 成功/失败,这里只是兜底返回值
        from pathlib import Path as _P
        paths_cfg = cfg.get("paths") or {}
        out = _P(paths_cfg.get("index_html", "")) if paths_cfg.get("index_html") else None
        if out and out.exists():
            log.info(f"=== 完成  index.html 在 {out} ({out.stat().st_size:,} bytes) ===")
            return 0
        log.error("=== 完成  index.html 未生成,检查 .env INDEX_HTML_PATH 配置 ===")
        return 1
    if args.single_url:
        log.info(f"=== 启动 sync_weekly  single-url={args.single_url} ===")
        process_single_url(
            cfg,
            args.single_url,
            section=args.section,
            no_feishu=args.no_feishu,
        )
        log.info("=== 结束 ===")
        return 0
    if args.kill_stale:
        # 显式触发清理日志;open_browser 内部也会自动跑一次
        cfg_b = cfg["browser"]
        _cleanup_stale_chrome_locks(cfg_b["user_data_path"])
    issue_date = args.issue or datetime.now().strftime("%Y-%m-%d")
    if args.refresh_images:
        article_ids = {item.strip() for item in args.article_ids.split(",") if item.strip()}
        log.info(f"=== 刷新图片 issue={issue_date}, ids={sorted(article_ids) or 'all'} ===")
        refresh_article_images(cfg, issue_date, article_ids, no_feishu=args.no_feishu)
        log.info("=== 结束 ===")
        return 0
    log.info(f"=== 启动 sync_weekly  issue={issue_date}  dry-run={args.dry_run} ===")
    process_issue(
        cfg,
        issue_date,
        dry_run=args.dry_run,
        limit=max(0, args.limit),
        rewrite_id=args.rewrite_id,
        no_feishu=args.no_feishu,
        debug_html_dir=Path(args.debug_html) if args.debug_html else None,
    )
    log.info("=== 结束 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
