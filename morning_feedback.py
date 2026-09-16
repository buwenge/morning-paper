"""《小予晨报》读者反馈：打分、追后续、口味清单。

设计稿见项目内部文档（本仓库未附带设计稿全文，只保留最终实现）。

**标准库 only**——本模块同时被两条不同的 Python 解释器 import：`home.py`
（venv，处理小予的 `home 晨报 …` 命令）与 `morning_scout_sources.py`（系统
`/usr/bin/python3`，cron 里跑的采集脚本）。不许引入任何第三方依赖。

存储：`.morning_paper/feedback.json`（原子写：tmp+fsync+os.replace+0600），
`.morning_paper/feedback.lock`（`fcntl.flock` 护住 read-modify-write——
冲浪班 cron 与 home 命令理论上会并发碰这个文件，操作都是毫秒级，阻塞式
锁足够）。所有公开函数都接受 `path=` 覆盖默认路径，方便测试隔离，绝不
把 `FEEDBACK_PATH` 直接写进函数签名的默认值（那样会在模块导入的一瞬间把
路径锁死，测试里改 `morning_feedback.FEEDBACK_PATH` 不会生效——同样的坑
`garden.py` 的 `GARDEN_FILE` 已经踩过，见该模块 :12-14 的说明）。

读到损坏的 JSON 一律报错、绝不覆盖原文件（照抄 `morning_paper.load_state`
的"拒绝覆盖"姿势）——唯一例外是 `build_material_feedback`：它在冲浪班
链路（`morning_scout_sources.py`）里被调用，反馈机制自己的任何故障都不
许连累晨报本体，所以那里损坏只打印一句告警就返回 `None`，绝不往上抛。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import cn_numerals
import morning_paper
import session_intro

ROOT = Path(__file__).resolve().parent
FEEDBACK_PATH = ROOT / ".morning_paper" / "feedback.json"
# 打分导语"只出现一次"的认领记录（S0 抽出的 session_intro.claim 共用模
# 块，同小院子开场那套逻辑）；目录已经在 .gitignore（跟 feedback.json
# 同一个 .morning_paper/ 目录）。
INTRO_SESSIONS_PATH = ROOT / ".morning_paper" / "feedback_intro_sessions.json"

TZ = ZoneInfo("Asia/Shanghai")

FEEDBACK_VERSION = 1

# 每次写盘顺手修剪（设计稿 2.1）：ratings 只留最近 30 天；已了结的 follows
# （done/expired/cancelled）超过 30 天没人再提就清掉，active 的永远留着。
RATING_RETAIN_DAYS = 30
FOLLOW_RESOLVED_RETAIN_DAYS = 30
# 追踪请求超过 7 天冲浪班还没找到后续，就不再指望它，标过期（不欠账）。
FOLLOW_EXPIRE_DAYS = 7

TOPIC_KINDS = ("prefer", "avoid")
TOPIC_MAX_ITEMS = 20
TOPIC_MAX_CHARS = 20

# 素材注入块（build_material_feedback）的上限：见设计稿 2.3。
FOLLOW_REQUESTS_LIMIT = 5
RECENT_FEEDBACK_WINDOW_DAYS = 30
RECENT_ZERO_LIMIT = 15
RECENT_HIGH_LIMIT = 15

SHORT_TITLE_LIMIT = 24

_SCORE_LABELS = {0: "不感兴趣", 1: "看看就好", 2: "有意思", 3: "很想深入"}
_TOPIC_LABELS = {"prefer": "想看", "avoid": "不想看"}

# 打分串宽容语法（2026-09-11 用户补充拍板：小予怎么顺手怎么写，机器负责
# 理解）。编号既认阿拉伯数字也认中文数字（`cn_numerals`，先例 commit
# a315269，不重写第二份）。
_ITEM_SEP_RE = re.compile(r"[\s，、；,;]+")
_NUM_ALT = rf"(?:\d+|{cn_numerals.CN_NUM_PATTERN})"
_FOLLOW_ALT = r"(?:追|想追)"
# "2:3" / "2：3" / "第2条:3" / "第2条：3" / "第二条：3"（"第"/"条"都可省）。
_RATING_TOKEN_RE = re.compile(rf"^(?:第)?(?P<num>{_NUM_ALT})(?:条)?[:：](?P<score>\d+)(?P<follow>{_FOLLOW_ALT})?$")
# "第2条3" / "第二条3追"：不写冒号，分数直接贴在"条"后面。
_RATING_NO_COLON_RE = re.compile(rf"^第(?P<num>{_NUM_ALT})条(?P<score>\d+)(?P<follow>{_FOLLOW_ALT})?$")
# 只写编号没跟分数，指望紧跟着的下一节补上分数（"第二条 3"）。
_RATING_DANGLING_ITEM_RE = re.compile(rf"^第(?P<num>{_NUM_ALT})条$")
# 独立一节的裸分数（配对用），可选"追/想追"后缀。
_RATING_BARE_SCORE_RE = re.compile(rf"^(?P<score>\d+)(?P<follow>{_FOLLOW_ALT})?$")

_RATING_USAGE_EXAMPLE = "`2:3追 5:0` 或 `第二条 3` / `第2条追`"

# 每 session 只出一次的打分导语（2026-09-11 用户拍板：照小院子"session
# 初见"的路数，不做"每天报尾重复一句提示"）——文案是监理定稿，照抄。
RATING_INTRO_TEXT = (
    "【晨报打分·第一次见面的说明】这份晨报可以打分——看完想留个脚印的话："
    "`home 晨报 打分 2:3追 5:0` 这样按编号打（0 不感兴趣 / 1 看看就好 / "
    "2 有意思 / 3 很想深入；号后面加\"追\"= 想看后续，晨报班明天优先去找）。"
    "写法很随意，\"第一条：1\"这种也认。打 0 的话题以后会少来；也可以直接 "
    "`home 晨报 想看/不想看 <话题>` 管你自己的口味清单，单独敲 `home 晨报` "
    "能看本期编号和用法。只打有感觉的几条就行，全不打也完全可以——跟读报一样，"
    "这不是作业。"
)


class FeedbackError(Exception):
    pass


# ---------------------------------------------------------------------------
# 存储：读/写/锁
# ---------------------------------------------------------------------------


def _empty_feedback() -> dict:
    return {
        "version": FEEDBACK_VERSION,
        "ratings": [],
        "follows": [],
        "topics": {"prefer": [], "avoid": []},
    }


@contextlib.contextmanager
def _locked(path: Path):
    """护住 feedback.json 的完整读改写；冲浪班 cron 与 home 命令理论上会并发。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.parent / "feedback.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _load(path: Path) -> dict:
    if not path.exists():
        return _empty_feedback()
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeedbackError("晨报反馈数据文件损坏，拒绝覆盖") from exc
    if not isinstance(parsed, dict) or parsed.get("version") != FEEDBACK_VERSION:
        raise FeedbackError("晨报反馈数据文件格式不对，拒绝覆盖")
    if not isinstance(parsed.get("ratings"), list) or not isinstance(parsed.get("follows"), list):
        raise FeedbackError("晨报反馈数据文件格式不对，拒绝覆盖")
    topics = parsed.get("topics")
    if (
        not isinstance(topics, dict)
        or not isinstance(topics.get("prefer"), list)
        or not isinstance(topics.get("avoid"), list)
    ):
        raise FeedbackError("晨报反馈数据文件格式不对，拒绝覆盖")
    return parsed


def _write(feedback: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(feedback, ensure_ascii=False, indent=2) + "\n"
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".feedback.", delete=False
        ) as handle:
            temp_name = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if temp_name:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_name)


def _parse_date_prefix(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _prune(feedback: dict, today: date) -> None:
    rating_cutoff = (today - timedelta(days=RATING_RETAIN_DAYS)).isoformat()
    feedback["ratings"] = [
        r for r in feedback["ratings"]
        if isinstance(r.get("issue_date"), str) and r["issue_date"] >= rating_cutoff
    ]

    kept_follows = []
    for follow in feedback["follows"]:
        if follow.get("status") == "active":
            kept_follows.append(follow)
            continue
        resolved = _parse_date_prefix(follow.get("resolved_at"))
        if resolved is None or (today - resolved).days <= FOLLOW_RESOLVED_RETAIN_DAYS:
            kept_follows.append(follow)
    feedback["follows"] = kept_follows


def _finalize(feedback: dict, path: Path, today: date) -> int:
    """修剪 + 落盘，返回当前 active 追踪数（写操作收尾的共用尾巴）。"""
    _prune(feedback, today)
    _write(feedback, path)
    return sum(1 for f in feedback["follows"] if f.get("status") == "active")


def _short_title(title: object, limit: int = SHORT_TITLE_LIMIT) -> str:
    text = (title or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _follow_id(issue_date: str, n: int) -> str:
    mmdd = issue_date[5:7] + issue_date[8:10]
    return f"f-{mmdd}-{n}"


def _activate_follow(feedback: dict, issue: dict, n: int, now: datetime) -> dict:
    """建立或复活第 n 条（1-based）的追踪记录；不做编号越界检查，调用方先查。"""
    issue_date = issue.get("issue_date", "")
    item = issue["items"][n - 1]
    follow_id = _follow_id(issue_date, n)
    existing = next((f for f in feedback["follows"] if f.get("id") == follow_id), None)
    if existing is not None:
        existing["status"] = "active"
        existing["resolved_at"] = ""
        return existing
    record = {
        "id": follow_id,
        "issue_date": issue_date,
        "n": n,
        "title": item.get("title", ""),
        "url": item.get("url", ""),
        "status": "active",
        "created_at": now.isoformat(),
        "resolved_at": "",
    }
    feedback["follows"].append(record)
    return record


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------


def _check_rating_bounds(token: str, n: int | None, score: int) -> None:
    if n is None or n < 1:
        raise FeedbackError(f"编号不对：『{token}』，编号从 1 开始数（阿拉伯数字、中文数字都行）")
    if not (0 <= score <= 3):
        raise FeedbackError(f"分数不对：『{token}』，只能是 0~3")


def parse_rating_spec(text: str) -> list[dict]:
    """解析打分串，宽容语法（2026-09-11 用户拍板：小予怎么顺手怎么写，机
    器负责理解）。

    条目间用空格/"，"/"、"/"；"/","分隔；单条接受 `2:3`、`2：3`、
    `第2条:3`、`第2条3`、`第二条：3`、`第二条 3`（带"第/条"标记时冒号可
    省，分数可以贴着写，也可以是紧跟着的下一节）；编号认阿拉伯数字与中
    文数字（一~十及组合，`cn_numerals`）；分数后可缀"追"或"想追"。

    没有"第/条"标记的裸数字串（比如单独一节的 `11`）分不清是"第11条"
    还是"第1条打1分"，算真歧义；其余解析不出来的一律算真非法——两种
    情况都整条拒绝、报错指出坏在哪一节 + 附一遍用法，不做半应用、不瞎
    猜（本函数管不到"编号是否落在今天条目范围内"，那交给
    `apply_ratings`/`set_follow` 在真正动笔前一并校验，同样是校验完才
    整体生效）。
    """
    normalized = unicodedata.normalize("NFKC", text or "").strip()
    segments = [seg for seg in _ITEM_SEP_RE.split(normalized) if seg]
    if not segments:
        raise FeedbackError(
            f"没看出要打的分，格式例子：{_RATING_USAGE_EXAMPLE}"
            "（0 不感兴趣/1 看看就好/2 有意思/3 很想深入，加个“追”就是想看后续）"
        )

    result: list[dict] = []
    i = 0
    while i < len(segments):
        seg = segments[i]

        match = _RATING_TOKEN_RE.match(seg) or _RATING_NO_COLON_RE.match(seg)
        if match:
            n = cn_numerals.to_int(match["num"])
            score = int(match["score"])
            _check_rating_bounds(seg, n, score)
            result.append({"n": n, "score": score, "follow": bool(match["follow"])})
            i += 1
            continue

        dangling = _RATING_DANGLING_ITEM_RE.match(seg)
        if dangling:
            next_seg = segments[i + 1] if i + 1 < len(segments) else ""
            score_match = _RATING_BARE_SCORE_RE.match(next_seg) if next_seg else None
            if score_match is None:
                raise FeedbackError(
                    f"『{seg}』后面没跟着看得懂的分数；格式例子：{_RATING_USAGE_EXAMPLE}"
                )
            n = cn_numerals.to_int(dangling["num"])
            score = int(score_match["score"])
            _check_rating_bounds(seg, n, score)
            result.append({"n": n, "score": score, "follow": bool(score_match["follow"])})
            i += 2
            continue

        if _RATING_BARE_SCORE_RE.match(seg):
            # 没有"第/条"标记单独出现的裸数字串：分不清是编号还是分数，
            # 真歧义，不猜。
            raise FeedbackError(
                f"『{seg}』分不清是第几条还是打几分——写成 `{seg}:分数` 或 `第{seg}条 分数` "
                f"才不会猜错；格式例子：{_RATING_USAGE_EXAMPLE}"
            )

        raise FeedbackError(f"这一节没看懂：『{seg}』；格式例子：{_RATING_USAGE_EXAMPLE}")

    return result


def _rating_line(n: int, title: str, score: int, follow: bool) -> str:
    label = _SCORE_LABELS.get(score, str(score))
    tail = "・追" if follow else ""
    return f"{n} 《{_short_title(title)}》→ {score}分（{label}）{tail}"


def apply_ratings(issue: dict, spec: list[dict], now: datetime, *, path: Path | None = None) -> str:
    """把 `parse_rating_spec` 解析好的打分整体应用到 `issue`（当前那期晨报）。

    同一 issue_date+n 重复打分覆盖旧值；带"追"的连带建立/复活对应
    follow。任何一个编号超出 `issue["items"]` 范围就整条拒绝，不写盘、
    也不留下部分应用的痕迹。
    """
    path = path or FEEDBACK_PATH
    items = issue.get("items") or []
    issue_date = issue.get("issue_date", "")
    if not spec:
        raise FeedbackError("没有可以打的分，格式例子：`打分 2:3追 5:0`")
    for entry in spec:
        if not (1 <= entry["n"] <= len(items)):
            raise FeedbackError(f"编号 {entry['n']} 超出范围，今天一共 {len(items)} 条")

    with _locked(path):
        feedback = _load(path)
        lines = []
        for entry in spec:
            n, score, follow = entry["n"], entry["score"], entry["follow"]
            item = items[n - 1]
            title = item.get("title", "")
            section = item.get("section", "")
            title_key = morning_paper._title_key(title) if title else ""
            existing = next(
                (r for r in feedback["ratings"] if r.get("issue_date") == issue_date and r.get("n") == n),
                None,
            )
            record = {
                "issue_date": issue_date,
                "n": n,
                "title": title,
                "title_key": title_key,
                "section": section,
                "score": score,
                "follow": follow,
                "rated_at": now.isoformat(),
            }
            if existing is not None:
                existing.update(record)
            else:
                feedback["ratings"].append(record)
            if follow:
                _activate_follow(feedback, issue, n, now)
            lines.append(_rating_line(n, title, score, follow))
        active = _finalize(feedback, path, now.date())

    tail = f"当前在追 {active} 条。" if active else "当前没有在追的条目。"
    return "已记下：\n" + "\n".join(lines) + "\n" + tail


# ---------------------------------------------------------------------------
# 追 / 不追
# ---------------------------------------------------------------------------


def set_follow(issue: dict | None, n: int | str, want: bool, now: datetime, *, path: Path | None = None) -> str:
    """单独标记"追/不追"。

    `n` 可以是当天条目的 1-based 编号（int，需要传 `issue`），也可以是
    `f-MMDD-n` 格式的追踪 id（str，可以不传 `issue`，用来取消跨天的旧追
    踪）。`want=True` 建立/复活，`want=False`（不追）等价 cancelled。
    """
    path = path or FEEDBACK_PATH
    with _locked(path):
        feedback = _load(path)

        if isinstance(n, str):
            record = next((f for f in feedback["follows"] if f.get("id") == n), None)
            if record is None:
                raise FeedbackError(f"没找到这条追踪记录：{n}")
            if want:
                record["status"] = "active"
                record["resolved_at"] = ""
                active = _finalize(feedback, path, now.date())
                return f"已标记追踪：{_short_title(record.get('title', ''))}\n当前在追 {active} 条。"
            if record.get("status") != "active":
                return f"这条本来就没在追：{_short_title(record.get('title', ''))}"
            record["status"] = "cancelled"
            record["resolved_at"] = now.isoformat()
            _finalize(feedback, path, now.date())
            return f"已取消追踪：{_short_title(record.get('title', ''))}"

        if issue is None or not issue.get("items"):
            raise FeedbackError("没有当前这期晨报，没法按编号操作——用 f- 开头的追踪编号试试")
        items = issue["items"]
        if not (1 <= n <= len(items)):
            raise FeedbackError(f"编号 {n} 超出范围，今天一共 {len(items)} 条")

        if want:
            record = _activate_follow(feedback, issue, n, now)
            active = _finalize(feedback, path, now.date())
            return f"已标记追踪：{n} 《{_short_title(record['title'])}》\n当前在追 {active} 条。"

        follow_id = _follow_id(issue.get("issue_date", ""), n)
        record = next((f for f in feedback["follows"] if f.get("id") == follow_id), None)
        if record is None or record.get("status") != "active":
            return f"第 {n} 条本来就没在追"
        record["status"] = "cancelled"
        record["resolved_at"] = now.isoformat()
        _finalize(feedback, path, now.date())
        return f"已取消追踪：{n} 《{_short_title(record.get('title', ''))}》"


# ---------------------------------------------------------------------------
# 口味清单
# ---------------------------------------------------------------------------


def _validate_topic_kind(kind: str) -> str:
    if kind not in TOPIC_KINDS:
        raise FeedbackError(f"口味清单类别不对：{kind}")
    return kind


def add_topic(kind: str, phrase: str, *, path: Path | None = None) -> str:
    kind = _validate_topic_kind(kind)
    phrase = (phrase or "").strip()
    label = _TOPIC_LABELS[kind]
    if not phrase:
        raise FeedbackError(f"话题不能是空的，格式例子：`home 晨报 {label} 猫的冷知识`")
    if len(phrase) > TOPIC_MAX_CHARS:
        raise FeedbackError(f"话题太长了，{TOPIC_MAX_CHARS} 字以内：{phrase}")
    path = path or FEEDBACK_PATH
    with _locked(path):
        feedback = _load(path)
        topics = feedback["topics"][kind]
        if phrase in topics:
            return f"已经在{label}清单里了：{phrase}"
        if len(topics) >= TOPIC_MAX_ITEMS:
            raise FeedbackError(f"{label}清单已经有 {TOPIC_MAX_ITEMS} 条了，先删几条再加")
        topics.append(phrase)
        _finalize(feedback, path, datetime.now(TZ).date())
    return f"已加进{label}清单：{phrase}"


def remove_topic(kind: str, phrase: str, *, path: Path | None = None) -> str:
    kind = _validate_topic_kind(kind)
    phrase = (phrase or "").strip()
    label = _TOPIC_LABELS[kind]
    if not phrase:
        raise FeedbackError(f"话题不能是空的，格式例子：`home 晨报 取消{label} 猫的冷知识`")
    path = path or FEEDBACK_PATH
    with _locked(path):
        feedback = _load(path)
        topics = feedback["topics"][kind]
        if phrase not in topics:
            return f"{label}清单里本来就没有：{phrase}"
        topics.remove(phrase)
        _finalize(feedback, path, datetime.now(TZ).date())
    return f"已从{label}清单删掉：{phrase}"


# ---------------------------------------------------------------------------
# 查看现状
# ---------------------------------------------------------------------------


def status_text(issue: dict | None, *, path: Path | None = None) -> str:
    """无参 `home 晨报` 的输出：本期编号清单 + 活跃追踪 + 口味清单 + 用法提示。"""
    path = path or FEEDBACK_PATH
    with _locked(path):
        feedback = _load(path)

        lines: list[str] = []
        has_issue = bool(issue and issue.get("items"))
        if has_issue:
            issue_date = issue.get("issue_date", "")
            ratings_by_n = {
                r.get("n"): r.get("score")
                for r in feedback["ratings"]
                if r.get("issue_date") == issue_date
            }
            lines.append(f"今天（{issue_date}）的晨报：")
            for n, item in morning_paper.numbered_items(issue):
                mark = ratings_by_n.get(n)
                mark_text = f"（已打{mark}分）" if mark is not None else ""
                lines.append(f"{n} [{item.get('section', '')}] {_short_title(item.get('title', ''))}{mark_text}")
        else:
            lines.append("今天报纸还没来，晨报没开。")

        active_follows = [f for f in feedback["follows"] if f.get("status") == "active"]
        lines.append("")
        if active_follows:
            lines.append("在追的：")
            for follow in active_follows:
                lines.append(
                    f"{follow.get('id', '')} {_short_title(follow.get('title', ''))}（{follow.get('issue_date', '')}）"
                )
        else:
            lines.append("目前没有在追的条目。")

        topics = feedback["topics"]
        lines.append("")
        lines.append("想看：" + ("、".join(topics.get("prefer", [])) or "（还没设置）"))
        lines.append("不想看：" + ("、".join(topics.get("avoid", [])) or "（还没设置）"))

        lines.append("")
        lines.append(
            "打分用法：home 晨报 打分 2:3追 5:0"
            "（0 不感兴趣/1 看看就好/2 有意思/3 很想深入，号后加“追”=想看后续；"
            "也可以单独 home 晨报 追/不追 <编号>）"
        )
    return "\n".join(lines)


def frontend_summary(today: date, *, path: Path | None = None) -> dict:
    """网页晨报页用：当前在追的条目 + 最近打了 0 分（不感兴趣）的条目。

    跟 `build_material_feedback` 的"素材注入块"是两码事——那个是给冲浪班
    LLM 看的紧凑格式（只挂 follow id，供 `apply_lifecycle` 回填），这个是
    给前端渲染用的展示格式（挂标题/日期/链接，不含 id）。`status_text`
    也重叠了一部分逻辑，但它是给小予本人看的纯文本，这里要结构化数据。
    """
    path = path or FEEDBACK_PATH
    with _locked(path):
        feedback = _load(path)

    following = [
        {
            "id": f.get("id", ""),
            "title": f.get("title", ""),
            "url": f.get("url", ""),
            "issue_date": f.get("issue_date", ""),
        }
        for f in feedback["follows"]
        if f.get("status") == "active"
    ]
    following.reverse()  # 最近标的排前面

    cutoff = today - timedelta(days=RECENT_FEEDBACK_WINDOW_DAYS)
    not_interested = [
        {
            "title": r.get("title", ""),
            "section": r.get("section", ""),
            "issue_date": r.get("issue_date", ""),
        }
        for r in feedback["ratings"]
        if r.get("score") == 0 and (_parse_date_prefix(r.get("issue_date")) or date.min) >= cutoff
    ]
    not_interested.reverse()

    return {"following": following, "not_interested": not_interested}


# ---------------------------------------------------------------------------
# 打分导语（每 session 只出一次）
# ---------------------------------------------------------------------------


def claim_intro(session_id: str | None, *, path: Path | None = None) -> str:
    """打分导语只在小予第一次见到这个 session 时出现一次；已经见过、或
    `session_id` 形状不对都返回空串，非空就是这次要渲染的导语原文。

    薄壳委托给 `session_intro.claim`（S0 抽共用，同小院子"session 初
    见"那套锁+seen列表+原子写+容错逻辑）。"""
    return session_intro.claim(session_id, path=path or INTRO_SESSIONS_PATH, intro_text=RATING_INTRO_TEXT)


# ---------------------------------------------------------------------------
# 生命周期（冲浪班每天开工前跑一次）
# ---------------------------------------------------------------------------


def apply_lifecycle(today: date, drafts_dir: Path, *, path: Path | None = None) -> None:
    """①追踪超过 7 天没等到后续就标过期；②扫描往后的草稿，`follow_up_of`
    命中哪条活跃追踪就标 done。草稿文件解析失败跳过，不炸。"""
    path = path or FEEDBACK_PATH
    with _locked(path):
        feedback = _load(path)
        dirty = False

        for follow in feedback["follows"]:
            if follow.get("status") != "active":
                continue
            created = _parse_date_prefix(follow.get("created_at"))
            if created is not None and (today - created).days > FOLLOW_EXPIRE_DAYS:
                follow["status"] = "expired"
                follow["resolved_at"] = today.isoformat()
                dirty = True

        active_ids = {f["id"] for f in feedback["follows"] if f.get("status") == "active"}
        done_ids: set[str] = set()
        if active_ids and drafts_dir.exists():
            for draft_path in sorted(drafts_dir.glob("scout-*.json")):
                try:
                    doc = json.loads(draft_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
                    continue
                for item in doc["items"]:
                    if isinstance(item, dict) and isinstance(item.get("follow_up_of"), str):
                        done_ids.add(item["follow_up_of"])

        if done_ids:
            for follow in feedback["follows"]:
                if follow.get("status") == "active" and follow.get("id") in done_ids:
                    follow["status"] = "done"
                    follow["resolved_at"] = today.isoformat()
                    dirty = True

        if dirty:
            _finalize(feedback, path, today)


# ---------------------------------------------------------------------------
# 冲浪班素材注入块
# ---------------------------------------------------------------------------


def build_material_feedback(today: date, *, path: Path | None = None) -> dict | None:
    """产出挂到 `material["feedback"]` 的注入块；没有任何反馈时返回 None。

    读取本身出故障（文件损坏）只打印告警、返回 None——这是唯一允许在本
    模块内部吞掉 `FeedbackError` 的地方，反馈机制自己的问题绝不能连累
    晨报采集。
    """
    path = path or FEEDBACK_PATH
    try:
        feedback = _load(path)
    except FeedbackError as exc:
        print(f"晨报反馈读取失败，按无反馈处理：{exc}")
        return None

    follow_requests = [
        {
            "id": f.get("id", ""),
            "title": f.get("title", ""),
            "url": f.get("url", ""),
            "requested_on": f.get("issue_date", ""),
        }
        for f in feedback["follows"]
        if f.get("status") == "active"
    ][-FOLLOW_REQUESTS_LIMIT:]

    cutoff = today - timedelta(days=RECENT_FEEDBACK_WINDOW_DAYS)
    recent = [
        r for r in feedback["ratings"]
        if (_parse_date_prefix(r.get("issue_date")) or date.min) >= cutoff
    ]
    recent_zero = [
        {"title": r.get("title", ""), "section": r.get("section", ""), "date": r.get("issue_date", "")}
        for r in recent
        if r.get("score") == 0
    ][-RECENT_ZERO_LIMIT:]
    recent_high = [
        {
            "title": r.get("title", ""),
            "section": r.get("section", ""),
            "score": r.get("score"),
            "date": r.get("issue_date", ""),
        }
        for r in recent
        if r.get("score") in (2, 3)
    ][-RECENT_HIGH_LIMIT:]

    topics = feedback.get("topics", {})
    avoid_topics = list(topics.get("avoid", []))
    prefer_topics = list(topics.get("prefer", []))

    avoid_block: dict = {}
    if avoid_topics:
        avoid_block["topics"] = avoid_topics
    if recent_zero:
        avoid_block["recent_zero"] = recent_zero

    prefer_block: dict = {}
    if prefer_topics:
        prefer_block["topics"] = prefer_topics
    if recent_high:
        prefer_block["recent_high"] = recent_high

    result: dict = {}
    if follow_requests:
        result["follow_requests"] = follow_requests
    if avoid_block:
        result["avoid"] = avoid_block
    if prefer_block:
        result["prefer"] = prefer_block

    return result or None
