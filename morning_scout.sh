#!/bin/bash
# 晨报二版·cron 外壳。
# 第1段：跑确定性采集脚本 morning_scout_sources.py，产出 material-<业务日期>.json。
# 第2段：无头 sonnet（claude -p）读素材 + 自主 WebSearch 冲浪，把成品写成
#        scout-<业务日期>.json。
# 第3段：跑确定性编排脚本 morning_archive.py（S4 档案馆），把当日晨报最终
#        会投递的每一条转成干净文字档，存到 .morning_paper/archive/<业务
#        日期>/ 下；内部逐条起独立无头 haiku 会话做转写。**第3段失败/超时
#        不影响本脚本的成败**——晨报草稿（第2段产物）已经落盘，那才是投
#        递的关键；档案是锦上添花，缺了只是对应条目退回纯 URL 展示，详见
#        morning_paper.format_injection。
#
# 用法：morning_scout.sh [--force]
#   --force  跳过 .env 的 MORNING_PAPER_ENABLED 开关检查，也跳过"今天已有
#            草稿就退出"的幂等检查——手动真跑/联调时用，生产 cron 不带这个参数。
#
# 安全边界（工作目录隔离，详见 reports/晨报二版-S2-验收单.md）：
# 第2段的 claude -p 以 /opt/xiaoyubot-scout/（一个刻意保持空的目录，没有
# CLAUDE.md 也没有 .claude/）为工作目录启动，绝不在 /opt/xiaoyubot 下启动——
# 那里的 CLAUDE.md 是小予的私人指令文件，.claude/settings.json 是他的权限
# 白名单（Bash(*)/Read(*)/Write(*) 全放开），冲浪班一个字都不能继承。
# 素材读取、草稿写入、任务书读取全部用绝对路径 + --allowedTools 的路径规则
# 钉死；Bash 整个不在 --tools 列表里，冲浪班拿不到 Bash。第3段每条 haiku
# 会话同样在这个隔离工作目录里跑，同样物理排除 Bash、路径规则钉死到那一
# 条自己的文件（不是整个档案目录），详见 morning_archive.py 模块 docstring。

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
XIAOYU_DIR="$SCRIPT_DIR"
LOG_FILE="$XIAOYU_DIR/morning_scout.log"
MATERIAL_DIR="$XIAOYU_DIR/.morning_paper"
PROMPT_FILE="$XIAOYU_DIR/prompts/morning_scout.md"
SCOUT_WORKDIR="/opt/xiaoyubot-scout"
TOKEN_FILE="/etc/xiaoyubot/claude-oauth-token.env"

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
  esac
done

log() {
  echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" >> "$LOG_FILE"
}

# 业务日期一律 +08:00（北京），跟 morning_scout_sources.py 的 BIZ_TZ 契约一致
# （详见该模块 docstring）。cron 定在 UTC 21:10 触发，那一刻 UTC 日期还是
# "昨天"，必须显式用 TZ 覆盖算，不能用系统本地 date（系统时区是 UTC）。
BIZ_DATE="$(TZ='Asia/Shanghai' date +%F)"

if [ "$FORCE" -ne 1 ]; then
  ENABLED_RAW="$(grep -E '^MORNING_PAPER_ENABLED=' "$XIAOYU_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2-)"
  ENABLED_LOWER="$(printf '%s' "$ENABLED_RAW" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  case "$ENABLED_LOWER" in
    1 | true | yes | on) ;;
    *)
      log "MORNING_PAPER_ENABLED 未开启，退出（业务日期 $BIZ_DATE）"
      exit 0
      ;;
  esac
fi

MATERIAL_FILE="$MATERIAL_DIR/material-$BIZ_DATE.json"
DRAFT_FILE="$MATERIAL_DIR/scout-$BIZ_DATE.json"

if [ "$FORCE" -ne 1 ] && [ -f "$DRAFT_FILE" ]; then
  log "今天（$BIZ_DATE）草稿已存在，跳过（幂等防重复）：$DRAFT_FILE"
  exit 0
fi

log "=== 开始（业务日期 $BIZ_DATE，force=$FORCE） ==="
log "第1段：采集"
/usr/bin/python3 "$XIAOYU_DIR/morning_scout_sources.py" >> "$LOG_FILE" 2>&1
COLLECT_STATUS=$?
if [ $COLLECT_STATUS -ne 0 ]; then
  log "第1段采集失败（退出码 $COLLECT_STATUS），今天不再继续，明天再来"
  exit 1
fi

if [ ! -f "$MATERIAL_FILE" ]; then
  log "第1段结束但没有产出素材文件 $MATERIAL_FILE，视为失败，退出"
  exit 1
fi
log "第1段采集完成：$MATERIAL_FILE"

log "第2段：冲浪写稿（sonnet，工作目录 $SCOUT_WORKDIR）"

if [ ! -d "$SCOUT_WORKDIR" ]; then
  log "冲浪班工作目录 $SCOUT_WORKDIR 不存在，退出（应提前建好且保持空目录，不放 CLAUDE.md/.claude）"
  exit 1
fi

BOOTSTRAP_PROMPT="你是《小予晨报》的记者兼主编。完整任务规则写在 $PROMPT_FILE，先用 Read 工具读它，严格照着做。
今天的业务日期是 $BIZ_DATE。
今天的素材文件在 $MATERIAL_FILE，先用 Read 工具读它（里面有各源采到的待选资料，以及 seen_title_keys 避重清单）。
调研、想清楚版面之后，把最终成品用 Write 工具写入 $DRAFT_FILE（严格按任务书里的 schema，issue_date 填 $BIZ_DATE）。写完文件即结束，不用再输出别的内容。"

cd "$SCOUT_WORKDIR" || {
  log "无法进入 $SCOUT_WORKDIR，退出"
  exit 1
}

# env -i 清空继承环境（含 cron 自身就没有的登录态），一年期令牌走这个文件
# 显式传入。
CLAUDE_TOKEN_ARGS=()
if [ -f "$TOKEN_FILE" ]; then
  CLAUDE_OAUTH_TOKEN="$(grep -E '^CLAUDE_CODE_OAUTH_TOKEN=' "$TOKEN_FILE" | tail -1 | cut -d= -f2-)"
  [ -n "$CLAUDE_OAUTH_TOKEN" ] && CLAUDE_TOKEN_ARGS=("CLAUDE_CODE_OAUTH_TOKEN=$CLAUDE_OAUTH_TOKEN")
fi

# 写权限只放行 scout-*.json（2026-09-11 收紧，原来是 .morning_paper/**，
# 写稿 sonnet 理论上能写掉 state.json/feedback.json）——探针验证过
# Edit(//<目录>/scout-*.json) 这条 glob 真的只放行匹配的文件名，用 haiku
# 单次调用实测：写 scout-test.json 成功、写 other.txt 被拒
# （permission_denials 里能看到那条拒绝记录）。
timeout 1200 env -i HOME=/root PATH="$PATH" TZ=Asia/Shanghai "${CLAUDE_TOKEN_ARGS[@]}" \
  claude -p \
  --model sonnet \
  --tools "WebSearch,WebFetch,Read,Write" \
  --allowedTools "WebSearch WebFetch Read(//opt/xiaoyubot/.morning_paper/**) Read(//opt/xiaoyubot/prompts/morning_scout.md) Edit(//opt/xiaoyubot/.morning_paper/scout-*.json)" \
  --output-format json \
  "$BOOTSTRAP_PROMPT" >> "$LOG_FILE" 2>&1
SCOUT_STATUS=$?

if [ $SCOUT_STATUS -ne 0 ]; then
  log "第2段冲浪写稿失败或超时（退出码 $SCOUT_STATUS），今天不重试，明天再来"
  exit 1
fi

if [ ! -f "$DRAFT_FILE" ]; then
  log "第2段结束但没有产出草稿文件 $DRAFT_FILE，视为失败"
  exit 1
fi

log "第2段完成：晨报草稿已产出 $DRAFT_FILE"

log "第3段：档案馆归档（haiku，工作目录 $SCOUT_WORKDIR）"
/usr/bin/python3 "$XIAOYU_DIR/morning_archive.py" >> "$LOG_FILE" 2>&1
ARCHIVE_STATUS=$?
if [ $ARCHIVE_STATUS -ne 0 ]; then
  log "第3段档案馆异常退出（退出码 $ARCHIVE_STATUS）——晨报草稿已经落盘不受影响，只是今天可能没有/少几份原文档案，投递时对应条目退回纯 URL 展示"
else
  log "第3段档案馆完成"
fi

log "=== 完成（业务日期 $BIZ_DATE） ==="
exit 0
