#!/bin/zsh
# Economist 周报每周五晚定时抓取 —— launchd 调用入口
# 用法:
#   ./run_weekly_sync.sh                  (默认算上一个周六)
#   ./run_weekly_sync.sh --issue 2026-07-11  (显式指定 issue 日期,debug 用)
#
# 完整流程:
#   1) sync_weekly.py 抓取 + 写 database.js 到 .env 里的 DATABASE_JS_PATH
#   2) sync_weekly.py 重建 index.html 到 .env 里的 INDEX_HTML_PATH
#   3) 把 database.js 和 index.html 拷一份到项目根(给 Netlify 用)
#   4) git add + commit + push → Netlify 看到 push 自动 redeploy
#
# 注意:本脚本会写数据库内联版的 index.html 到项目根,覆盖原始模板。
# 模板恢复:git checkout index.html
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"
TS=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/launchd_$TS.log"

cd "$PROJECT_DIR"

# 读取 .env 里的外部路径配置(没有就回退默认)
INDEX_HTML_FILE=$(grep '^INDEX_HTML_PATH=' .env 2>/dev/null | cut -d= -f2- || true)
DATABASE_JS_FILE=$(grep '^DATABASE_JS_PATH=' .env 2>/dev/null | cut -d= -f2- || true)

# 计算目标 issue 日期
if [[ -n "${1:-}" ]]; then
  TARGET_DATE="$1"
else
  TARGET_DATE=$(python3 -c "
from datetime import datetime, timedelta
t = datetime.now()
if t.weekday() == 4:    # 周五 -> 明天就是周六
    delta = 1
elif t.weekday() == 5:  # 周六(容错:周五晚错过了) -> 今天
    delta = 0
elif t.weekday() == 6:  # 周日(容错) -> 上周六
    delta = 1
else:                   # 周一到周四 -> 上一个周六
    delta = t.weekday() + 2
print((t - timedelta(days=delta)).strftime('%Y-%m-%d'))
")
fi

{
  echo "================================================================"
  echo "[$(date '+%F %T')] 启动定时抓取,目标 issue=$TARGET_DATE"
  echo "  INDEX_HTML_FILE  = ${INDEX_HTML_FILE:-(project root index.html)}"
  echo "  DATABASE_JS_FILE = ${DATABASE_JS_FILE:-(project root database.js)}"
  echo "================================================================"
} | tee -a "$LOG_FILE"

# ---- 1. 抓取 ----
python3 sync_weekly.py --issue "$TARGET_DATE" 2>&1 | tee -a "$LOG_FILE"
RC=${PIPESTATUS[0]}
if [[ $RC -ne 0 ]]; then
  echo "[$(date '+%F %T')] ⚠️ 抓取退出码=$RC,继续完成后续步骤" | tee -a "$LOG_FILE"
fi

# ---- 2. 重建 index.html(防止 --issue 跑了但 index_html 没重建) ----
python3 sync_weekly.py --rebuild-index 2>&1 | tee -a "$LOG_FILE" || true

# ---- 3. 拷贝产物到项目根(给 Netlify 用) ----
{
  echo "================================================================"
  echo "[$(date '+%F %T')] 同步产物到项目根"
  echo "================================================================"
} | tee -a "$LOG_FILE"

# index.html
if [[ -n "$INDEX_HTML_FILE" ]]; then
  if [[ -f "$INDEX_HTML_FILE" ]]; then
    if cp "$INDEX_HTML_FILE" "$PROJECT_DIR/index.html" 2>&1 | tee -a "$LOG_FILE"; then
      echo "[$(date '+%F %T')] ✓ index.html 已同步 → $PROJECT_DIR/index.html" | tee -a "$LOG_FILE"
    fi
  else
    echo "[$(date '+%F %T')] ⚠️ INDEX_HTML_FILE 不存在:$INDEX_HTML_FILE" | tee -a "$LOG_FILE"
  fi
fi

# database.js
if [[ -n "$DATABASE_JS_FILE" ]]; then
  if [[ -f "$DATABASE_JS_FILE" ]]; then
    if cp "$DATABASE_JS_FILE" "$PROJECT_DIR/database.js" 2>&1 | tee -a "$LOG_FILE"; then
      echo "[$(date '+%F %T')] ✓ database.js 已同步 → $PROJECT_DIR/database.js" | tee -a "$LOG_FILE"
    fi
  else
    echo "[$(date '+%F %T')] ⚠️ DATABASE_JS_FILE 不存在:$DATABASE_JS_FILE" | tee -a "$LOG_FILE"
  fi
fi

# ---- 4. git add + commit + push(触发 Netlify 自动 redeploy) ----
{
  echo "================================================================"
  echo "[$(date '+%F %T')] Git 提交推送"
  echo "================================================================"
} | tee -a "$LOG_FILE"

cd "$PROJECT_DIR"
git add -A database.js index.html 2>&1 | tee -a "$LOG_FILE"
# 注意:database.js 在 .gitignore 里被排除。这里用 -f 强制 add
# (让 Netlify 能从 git 拿到最新数据库)
git add -f database.js 2>/dev/null || true

if git diff --staged --quiet 2>/dev/null; then
  echo "[$(date '+%F %T')] 无新内容,跳过 commit/push" | tee -a "$LOG_FILE"
else
  git commit -m "chore: weekly sync $TARGET_DATE" 2>&1 | tee -a "$LOG_FILE"
  if git push 2>&1 | tee -a "$LOG_FILE"; then
    echo "[$(date '+%F %T')] ✓ git push 成功,Netlify 将自动 redeploy" | tee -a "$LOG_FILE"
  else
    echo "[$(date '+%F %T')] ⚠️ git push 失败(Netlify 不会自动更新,但本地文件已更新)" | tee -a "$LOG_FILE"
  fi
fi

echo "[$(date '+%F %T')] 全部完成,exit code=$RC" | tee -a "$LOG_FILE"
exit 0