import os
import sys
import json
import time
import re
import requests
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
import uvicorn
from dotenv import load_dotenv

# -----------------------------------------------------------------------------
# 🔌 自动加载 .env 配置文件
# -----------------------------------------------------------------------------
load_dotenv()

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET")
LLM_API_KEY = os.getenv("LLM_API_KEY")

raw_base_url = os.getenv("LLM_BASE_URL")
LLM_BASE_URL = raw_base_url.strip() if raw_base_url and raw_base_url.strip() else "https://api.openai.com/v1"
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

# 🎯 配置你的本地抓取数据路径（请根据你的真实爬虫保存路径修改）
LOCAL_DB_PATH = "database.js"

if not all([FEISHU_APP_ID, FEISHU_APP_SECRET, LLM_API_KEY]):
    print("❌ 错误：检测到关键环境变量缺失！请检查 .env 文件。")
    sys.exit(1)

app = FastAPI()


# -----------------------------------------------------------------------------
# 🔍 RAG 本地检索核心引擎
# -----------------------------------------------------------------------------
# 2. 🔄 升级检索函数，使其具备自动剥离 JavaScript 变量外壳的能力
def search_local_articles(query: str, limit: int = 2) -> str:
    """从本地抓取的 database.js 文件中模糊检索相关文章，为大模型提供上下文"""
    if not os.path.exists(LOCAL_DB_PATH):
        print(f"⚠️ 未找到本地数据归档文件: {LOCAL_DB_PATH}，将直接调用大模型盲测。")
        return ""

    try:
        with open(LOCAL_DB_PATH, "r", encoding="utf-8") as f:
            js_content = f.read().strip()

        # 🎯 核心黑魔法：利用正则剥离 window.economist_db = [ ... ]; 的外壳，提取出纯粹的 JSON 字符串
        json_match = re.search(r'window\.economist_db\s*=\s*(\[.*\]);?', js_content, re.DOTALL)
        if json_match:
            articles = json.loads(json_match.group(1))
        else:
            # 兼容处理：如果哪天你直接存成了纯 JSON 数组，这里也能兜底解析
            articles = json.loads(js_content)

    except Exception as e:
        print(f"❌ 解析本地 database.js 失败: {e}")
        return ""

    # 提取提问中的核心关键词
    clean_query = re.sub(r'@_user_\S+', '', query).strip()
    # 2. 剥离掉常见的提问噪音词、语气词和标点
    clean_query = re.sub(r'(有没有关于|有关于|的报道|分析一下|帮我分析|核心是什么|是什么|吗|？|\?)', ' ',
                         clean_query).strip()
    # 3. 按空格或特殊符号切分出真正的核心词，并过滤掉单字（如 "有"、"问"）
    keywords = [str(k).strip() for k in re.split(r'[\s·,，.、]+', clean_query) if len(str(k).strip()) > 1]
    if not keywords:
        keywords = [clean_query] if clean_query else ["Graham Platner"]
    print(f"🔎 正在本地 JS 智库中检索关键词: {keywords}")
    if not keywords:
        keywords = [clean_query]

    print(f"🔎 正在本地 JS 智库中检索关键词: {keywords}")
    matched_articles = []

    # 找到你的 feishu_bot.py 里的这个循环，把对字段的检索范围加进去：
    for art in articles:
        title = art.get("title", "")
        title_zh = art.get("title_zh", "")  # 🎯 引入中文标题字段
        summary = art.get("summary_md", "") or art.get("content", "")
        section = art.get("section", "")

        # 检索矩阵加入 title_zh.lower()，确保飞书在搜索时拿中文搜索也能瞬间秒中！
        hit_count = sum(1 for kw in keywords if
                        kw.lower() in title.lower() or kw.lower() in title_zh.lower() or kw.lower() in summary.lower() or kw.lower() in section.lower())
        if hit_count > 0:
            matched_articles.append((hit_count, art))

    matched_articles.sort(key=lambda x: x[0], reverse=True)
    top_matches = [item[1] for item in matched_articles[:limit]]

    if not top_matches:
        return ""

    context_block = "\n=== 已找到本地抓取的《经济学人》最新真实报道上下文 ===\n"
    for idx, art in enumerate(top_matches, 1):
        context_block += (
            f"【文章 {idx}】\n"
            f"板块: {art.get('section', '未知')}\n"
            f"标题: {art.get('title')}\n"
            f"发布日期: {art.get('issue_date', '未知')}\n"
            f"归档摘要与内容: {art.get('summary_md', '')[:2500]}\n\n"
        )
    return context_block


# -----------------------------------------------------------------------------
# 🤖 核心工具函数
# -----------------------------------------------------------------------------
def get_feishu_token():
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    try:
        r = requests.post(url, json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=5)
        return r.json().get("tenant_access_token")
    except Exception as e:
        print(f"[Feishu Token Error] {e}")
        return None


def send_feishu_reply(message_id, reply_content):
    token = get_feishu_token()
    if not token: return
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}

    # 🧼 数据洗白与格式化过滤
    clean_content = re.sub(r'<think>.*?</think>', '', reply_content, flags=re.DOTALL).strip()
    paragraphs = []
    lines = clean_content.split('\n')

    for line in lines:
        line_strip = line.strip()
        if not line_strip: continue
        if line_strip.startswith(('---', '===', '`')) and len(line_strip) < 10:
            paragraphs.append([{"tag": "text", "text": "──────────────────────────────\n"}])
            continue

        elements = []
        if line_strip.startswith('#'):
            title_text = re.sub(r'[▶️📊📌🎯🔍]', '', line_strip.lstrip('#')).strip()
            elements.append({"tag": "text", "text": f"📌 {title_text}\n", "style": ["bold"]})
        elif '|' in line_strip:
            if '-' in line_strip and len(line_strip.replace('|', '').replace('-', '').strip()) == 0: continue
            cells = [cell.strip().replace('**', '') for cell in line_strip.split('|') if cell.strip()]
            if not cells: continue
            elements.append({"tag": "text", "text": f"  📊 {' ｜ '.join(cells)}\n", "style": ["italic"]})
        else:
            clean_line = re.sub(r'^[▶️📊📌🎯🔍>\s•\-*]+', '', line_strip).strip()
            if not clean_line: continue
            prefix = "• " if line_strip.startswith(('-', '*', '•')) or line_strip.startswith('>') else ""
            if prefix: elements.append({"tag": "text", "text": prefix, "style": ["bold"]})

            parts = re.split(r'(\*\*.*?\*\*)', clean_line)
            for part in parts:
                if part.startswith('**') and part.endswith('**'):
                    elements.append({"tag": "text", "text": part.replace('**', '').strip(), "style": ["bold"]})
                else:
                    elements.append({"tag": "text", "text": part})
            elements.append({"tag": "text", "text": "\n"})

        if elements: paragraphs.append(elements)

    payload = {
        "msg_type": "post",
        "content": json.dumps({"zh_cn": {"title": "💡 经济学人·专属智库答复", "content": paragraphs}},
                              ensure_ascii=False)
    }
    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
    except Exception as e:
        print(f"[Feishu Reply Error] {e}")


def call_llm(user_question: str, local_context: str = "") -> str:
    """调用大模型，如果包含本地上下文，强引导模型基于真实数据回答"""
    system_prompt = (
        "你是一个精通《经济学人》(The Economist)报道的资深智库专家。\n"
        "如果用户提供的信息中包含【已找到本地抓取的最新的真实报道上下文】，你必须严格、优先基于该上下文数据来进行事实归纳和深度分析，不得胡编乱造、不得说自己不知道。\n"
        "请用专业、详实、逻辑严密的中文回答。回答需多用 Markdown 的加粗或列表符号，排版保持高度Scannable（易读性）。"
    )

    # 如果有本地文章，直接合并进 prompt 喂给大模型
    user_content = user_question
    if local_context:
        user_content = f"{local_context}\n\n根据以上最新的《经济学人》一手抓取数据，请回答用户提问：{user_question}"

    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.3  # 降低随机性，确保严格基于上下文事实
    }

    try:
        r = requests.post(f"{LLM_BASE_URL}/chat/completions", json=payload, headers=headers, timeout=60)
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"❌ 呼叫大模型时网络阻塞: {e}"


def background_llm_task(message_id, user_question):
    print(f"🚀 开始处理飞书请求: {user_question}")

    # 🌟 核心拦截：先去本地抓取的数据集里找文章
    local_context = search_local_articles(user_question)
    if local_context:
        print("🔥 成功命中本地知识库文章！已将其作为 Context 注入 Prompt。")
    else:
        print("🌐 本地知识库未命中，交由大模型原始知识库盲测答复。")

    ai_answer = call_llm(user_question, local_context)
    send_feishu_reply(message_id, ai_answer)
    print("✅ 回复投递完成！")


# -----------------------------------------------------------------------------
# 🌐 Webhook 核心路由
# -----------------------------------------------------------------------------
@app.get("/")
async def index():
    return {"status": "running", "msg": "后端已成功穿透！"}


@app.post("/webhook")
async def feishu_webhook(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    if data.get("type") == "url_verification" or "challenge" in data:
        return JSONResponse(content={"challenge": data.get("challenge")})

    event = data.get("event", {})
    message = event.get("message", {})

    if message:
        message_id = message.get("message_id")
        raw_content = message.get("content", "{}")
        try:
            content_json = json.loads(raw_content)
            user_text = content_json.get("text", "").strip()
            user_question = re.sub(r'<at.*?>.*?</at>', '', user_text).strip()
        except Exception:
            user_question = ""

        if user_question and message_id:
            background_tasks.add_task(background_llm_task, message_id, user_question)

    return JSONResponse(content={"status": "ok"})


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8099)