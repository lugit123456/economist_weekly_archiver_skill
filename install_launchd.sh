#!/bin/bash
# install_launchd.sh — 把 launchd plist 安装到 ~/Library/LaunchAgents/
# 用法:
#   ./install_launchd.sh                    # 用脚本所在项目根作为 __PROJECT_DIR__
#   ./install_launchd.sh /path/to/project   # 指定其他项目根(必须包含 run_weekly_sync.sh)
#
# 流程:
#   1) 校验项目根有 run_weekly_sync.sh
#   2) 把 launchd/com.economist.archiver.weekly.plist 的 __PROJECT_DIR__ 替换成绝对路径
#   3) 写到 ~/Library/LaunchAgents/com.economist.archiver.weekly.plist
#   4) (可选)用 launchctl load 立即生效
#
# 卸载:
#   launchctl unload ~/Library/LaunchAgents/com.economist.archiver.weekly.plist
#   rm ~/Library/LaunchAgents/com.economist.archiver.weekly.plist
set -euo pipefail

PROJECT_DIR="${1:-$(cd "$(dirname "$0")" && pwd)}"
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"  # 转绝对路径

TEMPLATE="$(cd "$(dirname "$0")" && pwd)/launchd/com.economist.archiver.weekly.plist"
LABEL="com.economist.archiver.weekly"
TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"

# ---- 1) 校验 ----
if [[ ! -f "$PROJECT_DIR/run_weekly_sync.sh" ]]; then
  echo "❌ 项目根没找到 run_weekly_sync.sh:$PROJECT_DIR"
  echo "   请传项目根作为参数,或确认脚本放在项目根"
  exit 1
fi
if [[ ! -f "$TEMPLATE" ]]; then
  echo "❌ 模板 plist 不存在:$TEMPLATE"
  exit 1
fi
if ! command -v plutil >/dev/null 2>&1; then
  echo "❌ plutil 没装(macOS 自带,不应该出现)。无法校验 plist 语法"
  exit 1
fi

# ---- 2) 替换占位符 + 输出到 ~/Library/LaunchAgents/ ----
mkdir -p "$(dirname "$TARGET")"
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$TEMPLATE" > "$TARGET"
chmod 644 "$TARGET"

# ---- 3) 校验产物 ----
if ! plutil -lint "$TARGET" >/dev/null; then
  echo "❌ 生成的 plist 语法错误,请检查:"
  plutil -lint "$TARGET"
  exit 1
fi

echo "✓ 已生成 $TARGET"
echo "  - 替换 __PROJECT_DIR__ → $PROJECT_DIR"
echo "  - 校验:plist 语法 OK"

# ---- 4) 加载到 launchd ----
if launchctl list 2>/dev/null | grep -q "$LABEL"; then
  echo
  echo "⚠️  $LABEL 已经在 launchd 里(可能旧版本)。先 unload:"
  launchctl unload "$TARGET" 2>/dev/null || true
fi

echo
echo "立即加载到 launchd:"
read -p "  现在加载? [y/N] " ans
if [[ "$ans" == "y" || "$ans" == "Y" ]]; then
  launchctl load "$TARGET"
  echo "✓ 已 load。下周五 20:00 自动触发"
  echo
  echo "立即手动触发一次(测试):"
  read -p "  现在跑一次? [y/N] " ans2
  if [[ "$ans2" == "y" || "$ans2" == "Y" ]]; then
    launchctl start "$LABEL"
    echo "✓ 已 start,看 /tmp/economist-archiver-launchd.out.log 验证"
  fi
else
  echo
  echo "手动加载:"
  echo "  launchctl load $TARGET"
  echo "  launchctl start $LABEL   # 立即触发一次(可选)"
fi