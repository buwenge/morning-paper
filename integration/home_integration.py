"""摘自生产 home.py 的晨报接入点（不是完整可运行文件）。

生产里 `home.py` 是一个统一的"家庭指令"CLI 分发器（`home 手环 …` / `home
提醒 …` / `home 晨报 …` 等），本摘录只保留跟晨报相关的部分：域名别名表怎
么把"晨报"/"报纸"/"早报"这几个说法路由到 `handle_morning`、以及打分/追
后续/口味清单命令本身的解析与执行。

省略的部分：生产 `DOMAIN_ALIASES` 里还有其它域名（手环、床头灯、额度、聊
天记录、电话、提醒、小院子……），这里只留 `morning` 这一条；有一条私有彩
蛋功能的域名条目本仓库完全不包含，也不在这份摘录里出现。

依赖（本仓库都有）：`morning_paper.py`、`morning_feedback.py`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import morning_feedback
import morning_paper


TZ = ZoneInfo("Asia/Shanghai")

# 完整表还有 band/lamp/quota/history/phone/reminder/garden 等其它域名，这里
# 只保留晨报这一条；命令行第一个词跟这些别名模糊匹配（`best_match`，生产用
# 编辑距离阈值 0.49），匹配上就把 request.domain 设成对应的 key。
DOMAIN_ALIASES = {
    "morning": ("晨报", "报纸", "早报"),
}


class HomeError(Exception):
    pass


@dataclass(frozen=True)
class Request:
    domain: str
    text: str
    raw_text: str = ""
    help: bool = False
    dry_run: bool = False


def normalize(text: str) -> str:
    """生产版本做了更完整的全半角/大小写归一化，这里只示意签名。"""
    return text.strip()


def morning_help() -> str:
    return """晨报（打分/追后续/口味清单）：
  home 晨报                        今天这期的清单、在追的、口味清单
  home 晨报 打分 2:3追 5:0          按编号打分（0~3），号后加"追"=想看后续
  home 晨报 追 3                    把第3条标记为想追后续
  home 晨报 不追 3                  取消第3条的追踪（也可以用 f-开头的编号）
  home 晨报 想看 <话题>              以后多来点这个方向
  home 晨报 不想看 <话题>            以后避开这个方向
  home 晨报 取消想看 <话题>
  home 晨报 取消不想看 <话题>"""


def _morning_current_issue() -> dict | None:
    # 显式把 morning_paper.STATE_PATH 当参数传进去（而不是让 load_state 用
    # 它自己签名里焊死的默认值）：morning_paper.py 现有函数是
    # `path: Path = STATE_PATH`，默认值在模块导入那一刻就绑死，测试里改
    # `morning_paper.STATE_PATH` 对"不传 path"的调用没有任何效果（同
    # morning_feedback.py 模块 docstring 里记录的那个坑）。这里在调用点显
    # 式读一次模块属性，测试就能真的通过 patch morning_paper.STATE_PATH 生
    # 效，不用碰 morning_paper.py 本体。
    try:
        state = morning_paper.load_state(morning_paper.STATE_PATH)
    except morning_paper.MorningPaperError as exc:
        raise HomeError(str(exc)) from exc
    issue = state.get("issue")
    return issue if isinstance(issue, dict) and issue.get("items") else None


def _morning_require_issue() -> dict:
    issue = _morning_current_issue()
    if issue is None:
        raise HomeError("今天报纸还没来，晨报没开——等有报纸了再打分/追后续")
    return issue


def handle_morning(request: Request) -> str:
    """晨报打分/追后续/口味清单的薄胶水：纯本地确定性匹配，歧义宁可报错
    不猜。打分/追(按编号)只对"当前 state 里那一期"生效；口味清单增删、
    `不追 <f-id>` 不需要当天有报纸也能用。"""
    if request.help:
        return morning_help()
    raw = request.raw_text.strip()
    now = datetime.now(TZ)

    if not raw:
        return morning_feedback.status_text(_morning_current_issue())

    parts = raw.split(None, 1)
    keyword = normalize(parts[0])
    rest = parts[1].strip() if len(parts) > 1 else ""

    try:
        if keyword == "取消想看":
            return morning_feedback.remove_topic("prefer", rest)
        if keyword == "取消不想看":
            return morning_feedback.remove_topic("avoid", rest)
        if keyword == "想看":
            return morning_feedback.add_topic("prefer", rest)
        if keyword == "不想看":
            return morning_feedback.add_topic("avoid", rest)
        if keyword == "打分":
            spec = morning_feedback.parse_rating_spec(rest)
            return morning_feedback.apply_ratings(_morning_require_issue(), spec, now)
        if keyword == "追":
            if not rest.isdigit():
                raise HomeError("请说清楚要追哪一条的编号，比如『home 晨报 追 3』")
            return morning_feedback.set_follow(_morning_require_issue(), int(rest), True, now)
        if keyword == "不追":
            if not rest:
                raise HomeError("请说清楚要取消追哪一条，编号或 f-开头的编号都行")
            if rest.isdigit():
                return morning_feedback.set_follow(_morning_require_issue(), int(rest), False, now)
            return morning_feedback.set_follow(None, rest, False, now)
    except morning_feedback.FeedbackError as exc:
        raise HomeError(str(exc)) from exc

    raise HomeError(
        "没看懂想做什么，可以说：home 晨报 / 打分 <编号:分数> / 追 <编号> / "
        "不追 <编号或f-开头编号> / 想看 <话题> / 不想看 <话题>"
    )


def dispatch_example(request: Request) -> str:
    """示意 home.py 的 main() 里怎么把 domain == "morning" 接到 handle_morning，
    真实分发器还处理其它一堆 domain，这里只留晨报这一支。"""
    if request.domain == "morning":
        return handle_morning(request)
    raise HomeError(f"未知域名：{request.domain}")
