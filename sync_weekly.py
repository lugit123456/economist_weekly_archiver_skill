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
from datetime import datetime
from pathlib import Path
from typing import Any

# 延迟导入 requests/openai,方便无 LLM 依赖环境下跑单元测试。
# 真正调用 LLM / 推飞书时才触发。

# ---------------------------------------------------------------------------
# 常量(详见 DEVELOPMENT.md §3.2.3 / §3.2.4)
# ---------------------------------------------------------------------------

ALLOW_SECTIONS: set[str] = {
    "Science & technology",
    "Culture",
    "Books & arts",
    "Obituary",
    "Graphic detail",
    "Briefing",
    "Britain",
    "Europe",
    "United States",
    "Middle East & Africa",
    "The Americas",
    "Asia",
    "International",
    "1843"
}

DENY_PATH_KEYWORDS: tuple[str, ...] = (
    "/politics/", "/business/", "/finance-and-economics/",
    "/united-states/", "/china/", "/asia/", "/middle-east/",
    "/europe/", "/americas/", "/africa/", "/britain/",
    "/leaders/", "/letters/", "/by-invitation/",
    "/lexington/", "/banyan/", "/charlie-/", "/schumpeter/",
)

WEEKLY_URL = "https://www.economist.com/weeklyedition"


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
CONFIG_PATH = ROOT / "config.json"
DATABASE_JS = ROOT / "database.js"
LOGS_DIR = ROOT / "logs"

CN_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
MIN_CN_CHARS = 300
MIN_EN_CHARS = 1000

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


def load_config(
    config_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """加载 config.json,并用环境变量覆盖敏感字段。

    环境变量约定(均为可选;只有 LLM_API_KEY 是 LLM 调用所必需的):
      LLM_API_KEY        → llm.api_key
      LLM_BASE_URL       → llm.base_url(空字符串等价于未设置)
      LLM_MODEL          → llm.model
      FEISHU_WEBHOOK_URL → feishu.webhook_url(空字符串等价于未设置)

    设计:
      - config.json 提供结构化默认值(model/temperature/max_tokens/timeout/crawl 等)
      - 真实凭据(api_key / webhook)只放在 .env / 环境变量里,不入库
      - env 覆盖 json(后写优先),便于 CI / 本地临时切换

    `config_path` 默认惰性查找 CONFIG_PATH,以便测试时 monkeypatch 生效。
    """
    # 每次读 config 前确保 .env 已加载
    _load_dotenv()
    if config_path is None:
        config_path = CONFIG_PATH
    if not config_path.exists():
        sys.exit(f"config.json 不存在: {config_path}")
    with config_path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)

    src = env if env is not None else os.environ
    overlay = {
        "llm.api_key": src.get("LLM_API_KEY", "").strip(),
        "llm.base_url": src.get("LLM_BASE_URL", "").strip(),
        "llm.model": src.get("LLM_MODEL", "").strip(),
        "feishu.webhook_url": src.get("FEISHU_WEBHOOK_URL", "").strip(),
    }
    for dotted, val in overlay.items():
        if not val:
            continue
        section, key = dotted.split(".", 1)
        cfg.setdefault(section, {})[key] = val
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


def _date_key(s: str) -> int:
    """YYYY-MM-DD → 整数用于排序。非法字符串当作极小值。"""
    try:
        return int(datetime.strptime(s, "%Y-%m-%d").strftime("%Y%m%d"))
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# 板块过滤
# ---------------------------------------------------------------------------


def is_allowed_article(url: str, section: str) -> bool:
    """板块白名单精准过滤。

    不再在 URL 路径里去搜 "/britain/" 这种黑名单关键词，
    而是严格根据《经济学人》页面上渲染出来的 Section 名字进行白名单卡放。
    """
    # 1. 基础链接去重防御（非文章类型直接干掉）
    if any(kw in url for kw in ["/topic/", "/person/", "/newsletters/", "/audio/", "/video/"]):
        return False

    # 2. 严格遵循你给 AI 提的需求：除 politics 和 business 之外，只保留特定的严肃科技/文化知识板块
    # 页面上抓到的 section 文本如果属于 ALLOW_SECTIONS，则直接放行
    return section in ALLOW_SECTIONS


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

    return _parse_weeklyedition_html_v2(html_source)


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


def fetch_article_content(page, url: str) -> tuple[str, str]:
    """访问单篇文章，返回 (title, content_raw)。

    【核心修正版】：放弃不稳定的动态 DOM 抓取，全面转向提取并解析页面底部的 __NEXT_DATA__ JSON 块。
    100% 免疫前端改名、懒加载截断和动态闪烁，确保长文章全文无损恢复。
    """
    page.get(url)
    page.wait.load_start()
    time.sleep(1.5)  # 留出基础网络数据就绪时间

    html_source = page.html
    title = ""
    body_text = ""

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

    return title, body_text


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

            log_.warning(f"摘要纯净字数仍未达标({cn_count}<{MIN_CN_CHARS})，正在重新调用 LLM 跑流...")

        except Exception as e:
            last_err = e
            log_.warning(f"LLM 连线调用失败 (attempt {attempt + 1}): {e}")

        time.sleep(1.0)

    log_.error(f"摘要最终失败，丢弃: {title} ({last_err})")
    return ""


def count_cn_chars(s: str) -> int:
    return len(CN_CHAR_RE.findall(s))


# ---------------------------------------------------------------------------
# 飞书推送
# ---------------------------------------------------------------------------


def build_feishu_card(articles: list[dict[str, Any]], issue_date: str) -> dict[str, Any]:
    lines = "\n".join(f"- **[{a['section']}]** {a['title']}" for a in articles)
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


def process_issue(
    cfg: dict[str, Any],
    issue_date: str,
    *,
    dry_run: bool = False,
    rewrite_id: str | None = None,
    no_feishu: bool = False,
    debug_html_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """抓一个 issue,返回新增/重写的文章列表。"""
    browser_cfg = cfg["browser"]
    delay_min = float(cfg["crawl"].get("delay_min_s", 5))
    delay_max = float(cfg["crawl"].get("delay_max_s", 10))

    log.info(f"启动浏览器 (user_data={browser_cfg['user_data_path']})")
    page = open_browser(browser_cfg["user_data_path"], bool(browser_cfg.get("headless", False)))

    try:
        log.info("抓取 weeklyedition 目录…")
        index = fetch_weekly_index(page, issue_date=issue_date, debug_html_dir=debug_html_dir)
        log.info(f"目录共 {len(index)} 条链接")
        # 板块过滤
        candidates = [a for a in index if is_allowed_article(a["url"], a["section"])]
        log.info(f"板块过滤后剩 {len(candidates)} 条")
        if dry_run:
            for c in candidates:
                print(f"[DRY] {c['issue_date']}  {c['section']:25s}  {c['title']}  {c['url']}")
            return []

        existing = read_database_js()
        existing_by_url = {a["url"]: a for a in existing}
        if rewrite_id:
            existing = [a for a in existing if a.get("id") != rewrite_id]
            existing_by_url = {a["url"]: a for a in existing}

        client = make_llm_client(cfg)
        new_articles: list[dict[str, Any]] = []
        # existing 是可变 list,每收录一篇就 append,write_database_js 拿最新 list 写盘
        seq = _next_seq(existing, issue_date)
        log.info(
            f"开始逐篇抓取(已存在 {len(existing_by_url)} 篇,本 issue 板块过滤后 {len(candidates)} 条)"
        )

        for cand in candidates:
            url = cand["url"]
            if url in existing_by_url and not rewrite_id:
                log.info(f"⏭ 已存在,跳过: {cand['title'][:50]}  ({url[:60]}…)")
                continue

            log.info(f"抓取正文: {cand['title'][:60]}")
            try:
                title, body = fetch_article_content(page, url)
            except Exception as e:
                log.warning(f"抓取失败 {url}: {e}")
                time.sleep(random.uniform(delay_min, delay_max))
                continue
            if len(body) < MIN_EN_CHARS:
                log.warning(f"原文过短({len(body)}<{MIN_EN_CHARS}),丢弃: {url}")
                time.sleep(random.uniform(delay_min, delay_max))
                continue

            title = title or cand["title"]
            summary = summarize(client, cfg, title, body, log)
            if not summary:
                time.sleep(random.uniform(delay_min, delay_max))
                continue

            article = {
                "issue_date": issue_date,
                "id": f"art_{issue_date}_{seq:03d}",
                "section": cand["section"],
                "title": title,
                "url": url,
                "summary_md": summary,
                "content_raw": body,
            }
            # 立即落盘:用户刷新 index.html 就能看到刚抓的那篇
            existing.append(article)
            existing_by_url[url] = article
            new_articles.append(article)
            seq += 1
            try:
                write_database_js(existing)
                log.info(
                    f"✓ 已收录并落盘: {article['id']} - {title[:60]}  "
                    f"(本 run 第 {len(new_articles)} 篇 / 累计 {len(existing)} 篇)"
                )
            except Exception as e:
                log.error(f"写盘失败 {url}:{e},该篇未持久化,下轮会重试")
                # 回滚内存里的累计,避免误以为已落盘
                existing.pop()
                existing_by_url.pop(url, None)
                new_articles.pop()
                seq -= 1
            time.sleep(random.uniform(delay_min, delay_max))

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
    if any(kw in url for kw in DENY_PATH_KEYWORDS):
        log.warning(f"URL 命中黑名单板块,但因 --single-url 显式指定,仍继续: {url}")

    browser_cfg = cfg["browser"]
    page = open_browser(browser_cfg["user_data_path"], bool(browser_cfg.get("headless", False)))
    try:
        log.info(f"[single-url] 抓取: {url}")
        title, body = fetch_article_content(page, url)
        if len(body) < MIN_EN_CHARS:
            log.warning(f"原文过短({len(body)}<{MIN_EN_CHARS}),丢弃: {url}")
            return []
        log.info(f"[single-url] 抓取成功 title={title!r}  body={len(body)} chars")

        client = make_llm_client(cfg)
        summary = summarize(client, cfg, title, body, log)
        if not summary:
            log.warning(f"[single-url] LLM 摘要失败,丢弃: {url}")
            return []

        article, existing_after = _resolve_single_url_article(
            url, title, body, summary, section=section,
        )
        write_database_js(existing_after + [article])
        log.info(f"[single-url] ✓ 已写入: {article['id']} - {article['title']}")

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
    log.info(f"=== 启动 sync_weekly  issue={issue_date}  dry-run={args.dry_run} ===")
    process_issue(
        cfg,
        issue_date,
        dry_run=args.dry_run,
        rewrite_id=args.rewrite_id,
        no_feishu=args.no_feishu,
        debug_html_dir=Path(args.debug_html) if args.debug_html else None,
    )
    log.info("=== 结束 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
