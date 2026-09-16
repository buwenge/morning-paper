"""摘自生产 daemon.py 的晨报接入点（不是完整可运行文件）。

展示的是四段流水线里"第 4 段：投递"怎么接进 daemon 的后台任务调度与自主
唤醒流程——第 1/2/3 段（采集/写稿/归档）完全在 daemon 之外由 cron 驱动
`morning_scout.sh` 跑完，daemon 只在自己的唤醒周期里读当天已经准备好的
`issue`。

省略/简化的部分：
- daemon.py 里跟晨报无关的其它注入块（天气、便签信箱、到期提醒、小院子
  彩蛋等）在 `do_wakeup()` 里跟晨报注入块拼在同一条 wakeup 消息里，这里
  只保留跟晨报相关的骨架，其余用 `...` 省略。
- 生产 `do_wakeup()` 里还有一段私有彩蛋功能的消息处理，跟本仓库的开源范围
  无关，本摘录完全不包含那部分逻辑。
- 完整的 `do_wakeup()`/`prepare_morning_paper_job()` 还处理群聊自动模式、
  折叠缓存检查点上报等跟晨报无关的收尾逻辑，同样省略。

依赖（本仓库都有）：`morning_paper.py`、`morning_feedback.py`。
"""

from __future__ import annotations

import asyncio
import logging

import morning_feedback
import morning_paper


# ── 模块级状态：同一天所有触发点共用一份后台准备任务 ──────────────────────
morning_paper_prepare_task: asyncio.Task | None = None


def start_morning_paper_prepare() -> asyncio.Task | None:
    """同一天所有触发点共用一份后台准备任务，避免启动补做和定时任务撞车。"""
    global morning_paper_prepare_task
    prepare_now = now_local()  # 项目自己的 +08:00 当前时间函数，自行实现
    if not morning_paper.should_prepare(prepare_now):
        return None
    if morning_paper_prepare_task is None or morning_paper_prepare_task.done():
        morning_paper_prepare_task = asyncio.create_task(
            asyncio.to_thread(morning_paper.prepare_issue, prepare_now)
        )
    return morning_paper_prepare_task


async def prepare_morning_paper_job():
    """定时任务：05:30 触发一次；启动/重启补做一次（应对 05:30 之后才起来的情况）。"""
    global morning_paper_prepare_task
    try:
        task = start_morning_paper_prepare()
        if task is None:
            return None
        issue = await task
        logging.info("晨报准备完成：%s（%d条）", issue["issue_date"], len(issue["items"]))
        return issue
    except Exception as exc:
        # 采集/写稿源暂时不可用只会让这期报纸缺席，不能拖垮 daemon。
        logging.warning("晨报本轮准备失败：%s", exc)
        return None
    finally:
        if morning_paper_prepare_task is not None and morning_paper_prepare_task.done():
            morning_paper_prepare_task = None


async def do_wakeup_morning_paper_section(chat, wake_now) -> str:
    """`do_wakeup()` 里跟晨报相关的部分：06:00 后读今天的 issue（没有就等最多
    20 秒补做一次），拼出要塞进唤醒消息的文本块，同时认领"打分导语只出现一
    次"的引导文案。真实 `do_wakeup()` 把这段跟天气/提醒/小院子等其它注入块
    拼在同一条 wakeup_msg 里，见下面 `wakeup_msg` 拼接处的骨架。
    """
    paper_issue = None
    try:
        paper_issue = morning_paper.issue_for_wakeup(wake_now)
        if paper_issue is None and wake_now.hour >= morning_paper.DELIVERY_HOUR:
            await asyncio.wait_for(
                asyncio.shield(prepare_morning_paper_job()),
                timeout=20,
            )
            paper_issue = morning_paper.issue_for_wakeup(wake_now)
    except asyncio.TimeoutError:
        logging.warning("晨报尚未准备好，本次唤醒先正常进行")
    except Exception as exc:
        logging.warning("晨报本轮跳过：%s", exc)

    feedback_intro = ""
    if paper_issue:
        try:
            # 打分导语只在同一个 session 第一次见到时出现一次（session_intro
            # 共用模块），session_id 取跟聊天子进程约定好的同一个值。
            feedback_intro = morning_feedback.claim_intro(chat.session_id)
        except Exception as exc:
            logging.warning("晨报打分导语认领失败，按空导语继续：%s", exc)

    return (
        morning_paper.format_injection(paper_issue, feedback_intro=feedback_intro)
        if paper_issue else ""
    )


async def do_wakeup(chat, now_local, mark_delivered):
    """骨架示意：真实 do_wakeup() 里跟晨报有关的部分怎么嵌进整条唤醒流程。
    其余注入块（天气/便签/提醒/小院子……）与收尾逻辑省略，用 `...` 代替。
    """
    wake_now = now_local()
    ...
    paper_section = await do_wakeup_morning_paper_section(chat, wake_now)

    wakeup_msg = (
        # f"{new_session_tag}[WAKEUP]\n现在是 ...\n"
        # f"{weather_line}{notes_section}{reminder_section}{其它注入块...}"
        f"{paper_section}"
        f"这是你的自由时间，随意活动吧！"
    )

    result = await chat.send(wakeup_msg)
    ...
    if not chat.is_error:
        try:
            paper_issue = morning_paper.issue_for_wakeup(wake_now)
            if paper_issue and await asyncio.to_thread(
                morning_paper.mark_delivered, paper_issue["issue_date"], now_local(),
            ):
                logging.info("晨报已送达（%d条）", len(paper_issue["items"]))
        except Exception as exc:
            # 模型已经看见报纸后，确认写盘失败也不能截断这轮正常回复。
            logging.warning("晨报送达确认失败，下次可能重复：%s", exc)


# ── 定时任务注册（APScheduler，daemon 启动时跑一次） ───────────────────────
def register_scheduler_jobs(scheduler):
    scheduler.add_job(
        prepare_morning_paper_job,
        "cron",
        hour=morning_paper.PREPARE_HOUR,
        minute=morning_paper.PREPARE_MINUTE,
    )


async def on_daemon_startup():
    # 05:30 后才启动或重启时补做当天晨报；函数自身会识别已有 issue 并静默返回。
    asyncio.create_task(prepare_morning_paper_job())
