"""摘自生产 ws_handlers.py 的晨报接入点（不是完整可运行文件）。

前端晨报页是纯只读查询：不碰 `state.json`/`seen`/`mark_delivered`，业务
逻辑全在 `morning_paper.load_issue_for_date`/`list_scout_dates` 与
`morning_feedback.frontend_summary`，这个 handler 只是把三份读结果拼一个
WS 消息发回去。

依赖（本仓库都有）：`morning_paper.py`、`morning_feedback.py`。
依赖（本仓库不含，自行实现）：
- `now_local()`：项目自己的 +08:00 当前时间函数
- `_safe_send(websocket, text)`：吞掉已断开连接异常的发送封装
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime


async def handle_ws_get_morning_paper(websocket, msg: dict, *, now_local, _safe_send) -> None:
    import morning_feedback
    import morning_paper

    requested_date = msg.get("date")
    if isinstance(requested_date, str):
        try:
            datetime.strptime(requested_date, "%Y-%m-%d")
        except ValueError:
            requested_date = None
    else:
        requested_date = None
    date_str = requested_date or now_local().strftime("%Y-%m-%d")
    try:
        issue_data = await asyncio.to_thread(morning_paper.load_issue_for_date, date_str)
    except Exception as exc:
        issue_data = {"date": date_str, "has_draft": False, "items": [], "error": str(exc)}
    try:
        available_dates = await asyncio.to_thread(morning_paper.list_scout_dates)
    except Exception:
        available_dates = []
    try:
        feedback = await asyncio.to_thread(morning_feedback.frontend_summary, now_local().date())
    except Exception:
        # 反馈机制自己的故障不许连累晨报页本体（同 morning_feedback.py
        # docstring 定的姿势）——前端就当没有反馈数据显示。
        feedback = {"following": [], "not_interested": []}
    await _safe_send(websocket, json.dumps({
        "type": "morning_paper",
        "date": issue_data.get("date", date_str),
        "has_draft": issue_data.get("has_draft", False),
        "items": issue_data.get("items", []),
        "error": issue_data.get("error"),
        "available_dates": available_dates,
        "feedback": feedback,
    }, ensure_ascii=False))


# WS_HANDLERS 注册表（生产是模块级字典，登记一行）：
#   WS_HANDLERS = {
#       ...
#       "get_morning_paper": handle_ws_get_morning_paper,
#       ...
#   }
# 前端发 {"type": "get_morning_paper", "date": "2026-08-21"}（date 可选，
# 缺省取当天业务日期）即可触发。
