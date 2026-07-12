#!/usr/bin/env bash
# Netlify 一键部署脚本 —— 把当前 database.js + index.html 上传到 Netlify CDN
# 用法:
#   ./deploy_to_netlify.sh                          # 用最近一期
#   ./deploy_to_netlify.sh --issue 2026-07-04       # 抓指定一期 + 部署
#   ./deploy_to_netlify.sh --skip-sync              # 不抓取,只部署已有文件
#   ./deploy_to_netlify.sh --skip-copy              # 跳过 copy artifact 步骤
#
# 完整流程:
#   1) sync_weekly.py 抓取 + 写 database.js 到 .env 里的 DATABASE_JS_PATH
#   2) sync_weekly.py 重建 index.html 到 .env 里的 INDEX_HTML_PATH
#   3) 把外部路径里的产物拷到项目根(给 Netlify 用)
#   4) netlify deploy --prod --dir=.  上传
#
# 前置:
#   1) npm install -g netlify-cli
#   2) netlify login (浏览器登录一次)
#   3) netlify init  (关联项目到 Netlify site)
#   4) .env 里配好 LLM_API_KEY / INDEX_HTML_PATH / DATABASE_JS_PATH / FEISHU_WEBHOOK_URL 等

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

SKIP_SYNC=0
SKIP_COPY=0
ISSUE_DATE=""

for arg in "$@"; do
  case "$arg" in
    --skip-sync) SKIP_SYNC=1 ;;
    --skip-copy) SKIP_COPY=1 ;;
    --issue)     shift; ISSUE_DATE="$1" ;;
    --issue=*)   ISSUE_DATE="${arg#--issue=}" ;;
    --help|-h)
      sed -n '2,14p' "$0"
      exit 0
      ;;
  esac
  shift || true
done

# ---- 1. 抓取 ----
if [[ $SKIP_SYNC -eq 0 ]]; then
  echo "================================================================"
  echo "[$(date '+%F %T')] 抓取阶段"
  echo "================================================================"
  if [[ -n "$ISSUE_DATE" ]]; then
    python3 sync_weekly.py --issue "$ISSUE_DATE" --no-feishu
  else
    # 默认算上周六(已发布的最新一期)
    DEFAULT_DATE=$(python3 -c "
from datetime import datetime, timedelta
t = datetime.now()
# 周日 ~ 周五 → 上一个周六; 周六 → 今天
if t.weekday() == 5:
    delta = 0
elif t.weekday() == 6:
    delta = 1
else:
    delta = t.weekday() + 1
print((t - timedelta(days=delta)).strftime('%Y-%m-%d'))
")
    python3 sync_weekly.py --issue "$DEFAULT_DATE" --no-feishu
  fi
else
  echo "[$(date '+%F %T')] 跳过抓取(--skip-sync)"
fi

# ---- 2. 重建 index.html ----
echo "================================================================"
echo "[$(date '+%F %T')] 重建 index.html"
echo "================================================================"
python3 sync_weekly.py --rebuild-index

# ---- 3. 拷贝产物到项目根(给 Netlify 用) ----
if [[ $SKIP_COPY -eq 0 ]]; then
  INDEX_HTML_FILE=$(grep '^INDEX_HTML_PATH=' .env 2>/dev/null | cut -d= -f2- || true)
  DATABASE_JS_FILE=$(grep '^DATABASE_JS_PATH=' .env 2>/dev/null | cut -d= -f2- || true)

  echo "================================================================"
  echo "[$(date '+%F %T')] 同步产物到项目根"
  echo "================================================================"

  if [[ -n "$INDEX_HTML_FILE" ]] && [[ -f "$INDEX_HTML_FILE" ]]; then
    cp "$INDEX_HTML_FILE" "$PROJECT_DIR/index.html"
    echo "[$(date '+%F %T')] ✓ index.html ← $INDEX_HTML_FILE"
  fi
  if [[ -n "$DATABASE_JS_FILE" ]] && [[ -f "$DATABASE_JS_FILE" ]]; then
    cp "$DATABASE_JS_FILE" "$PROJECT_DIR/database.js"
    echo "[$(date '+%F %T')] ✓ database.js ← $DATABASE_JS_FILE"
  fi
fi

# ---- 4. 部署到 Netlify(CLI 直接上传) ----
echo "================================================================"
echo "[$(date '+%F %T')] 部署到 Netlify"
echo "================================================================"
if ! command -v netlify >/dev/null 2>&1; then
  echo "❌ netlify CLI 没装。运行: npm install -g netlify-cli"
  exit 1
fi
if [[ ! -f netlify.toml ]]; then
  echo "❌ 项目根没有 netlify.toml"
  exit 1
fi

netlify deploy --prod --dir=.

echo "================================================================"
echo "[$(date '+%F %T')] ✓ 部署完成"
echo "================================================================"