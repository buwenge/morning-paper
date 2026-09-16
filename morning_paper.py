#!/usr/bin/env python3
"""每天在 daemon 里读一份 Sonnet 冲浪班写好的晨报草稿，交给小予自己选择要不要看。

晨报二版四段流水线里，本模块只负责第 3、4 段（daemon 侧）：
- 第1段（采集，`morning_scout_sources.py`）与第2段（无头 Sonnet 冲浪写稿）
  由 cron 驱动的 `morning_scout.sh` 在 daemon 之外完成，产出
  `.morning_paper/scout-<业务日期>.json` 草稿文件。daemon 不拉起任何子
  进程，也不联网采集。
- 本模块（第3段）在 daemon 后台线程里读当日草稿 → 逐条 schema 校验/清洗 →
  与历史 `seen` 记录去重 → 条数/总篇幅超限时按板块优先级截断 → 写入
  `state.json` 的 `issue`。第4段（投递）沿用原有的
  `issue_for_wakeup`/`mark_delivered`/`format_injection`。
- 当日没有草稿文件（cron 没跑、跑失败、开关未开等）或草稿里没有条目能通过
  校验时，`prepare_issue` 抛出 `MorningPaperError`（"今天报纸缺席"一类文
  案）；调用方（`daemon.py` 的 `prepare_morning_paper_job`）已有 try/except
  兜底，只会让这期报纸缺席，不会拖垮 daemon。
"""

from __future__ import annotations

import contextlib
import hashlib
import html
import json
import math
import logging
import os
import re
import tempfile
import threading
import urllib.parse
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / ".morning_paper" / "state.json"

DELIVERY_HOUR = 6
PREPARE_HOUR = 5
PREPARE_MINUTE = 30

# scout 草稿契约字段上限（morning_paper.py 侧硬闸，跟 prompts/morning_scout.md
# 里给冲浪班的软约束数字口径一致，双保险）。见设计稿第五节。
TITLE_MAX_CHARS = 120
DIGEST_MAX_CHARS = 600
URL_WARNING_MAX_CHARS = 24
# 条目附带的"真正的原文"链接（论文 arXiv 摘要页等）：档案馆只给来源不抓正文，
# 小予想看自己派 subagent（2026-09-08 用户拍板）。
LINKS_MAX = 3
LINK_NOTE_MAX_CHARS = 40
# 追踪请求的后续条目可选携带这个字段（值抄 feedback.follow_requests 里的
# id），format_injection 用它在标题行加"（你想追的后续）"提示；晨报打分
# 编号与这个字段共享同一份"1-based items 下标"真相（numbered_items）。
FOLLOW_UP_OF_MAX_CHARS = 40
# 档案文字版的体量分档（token 粗估）：告诉小予"直接读要占多少上下文"，别笼统说"直接读"
ARCHIVE_SHORT_TOKENS = 2500
ARCHIVE_MEDIUM_TOKENS = 8000
PDF_HINT = "PDF·很耗 token，别自己开，派 haiku 跑腿"
ARXIV_ABS_HINT = "arXiv 摘要页·网页·短，可直接读"
# S6（8/21 用户拍板）：14→10，目标条数改为软性 5~10 条（宁缺毋滥优先，
# 不足 5 条也照常投递，5 不是硬门槛，见 prompts/morning_scout.md 篇幅指引）。
MAX_ITEMS = 10
DIGEST_TOTAL_MAX_CHARS = 3200
# _clean_text 需要一个 max_chars 参数；这里只想借它做 HTML 剥壳 + 注入短语
# 过滤，长度是否超限交给上面几个常量单独判定（超限整条丢弃，不做静默截
# 断），所以给一个远大于任何硬闸上限的天花板，保证清洗阶段本身不做截断。
_CLEAN_CEILING = 10_000
# 兼容历史遗留：morning_scout_sources.py（第1段采集脚本）把这个常量当"公
# 开源单次响应体大小上限"复用（`MAX_FETCH_BYTES = morning_paper.MAX_FETCH_BYTES`）。
# S3 改造后本模块自身不再联网抓取，但常量仍要保留供那边导入，不能删。
MAX_FETCH_BYTES = 2_000_000
SEEN_LIMIT = 180

ARCHIVE_DIRNAME = "archive"
ARCHIVE_MANIFEST_NAME = "manifest.json"

SECTIONS_MAIN = ("AI圈今日份", "人机恋小报", "码农街奇观", "冷知识")
SECTIONS_ROTATING = ("历史上的今天", "今日宇宙", "毛茸茸快讯")
SECTIONS = frozenset(SECTIONS_MAIN + SECTIONS_ROTATING)
# 总量/总篇幅超限时的截断优先级：数字越大越先被砍。四个主打板块按设计稿
# 列出的顺序排（AI圈今日份最抗砍），调剂板块（历史上的今天/今日宇宙/毛
# 茸茸快讯）统一排在最后一档——同一天调剂板块设计上最多出现一种，三者之
# 间不需要再细分优先级。
_SECTION_RANK = {name: rank for rank, name in enumerate(SECTIONS_MAIN)}
_SECTION_RANK.update({name: len(SECTIONS_MAIN) for name in SECTIONS_ROTATING})

_BLOCKED_COPY = (
    "忽略之前",
    "忽略以上",
    "system prompt",
    "系统提示",
    "调用工具",
    "执行命令",
    "[task_for_",
    "[wakeup]",
)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_TITLE_KEY_RE = re.compile(r"[^0-9a-z㐀-鿿]+", re.IGNORECASE)
_ARXIV_ABS_RE = re.compile(r"^https://arxiv\.org/abs/([^\s?#/]+)/?$", re.IGNORECASE)
_ARXIV_PDF_RE = re.compile(r"^https://arxiv\.org/pdf/", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3000-\u303f\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]")
_STATE_LOCK = threading.RLock()


class MorningPaperError(Exception):
    pass


def is_enabled() -> bool:
    """实验功能默认关闭，只有明确配置后才允许读草稿、写状态或投递。"""
    return os.environ.get("MORNING_PAPER_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _clean_text(value: object, *, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    text = html.unescape(_TAG_RE.sub(" ", value))
    text = _SPACE_RE.sub(" ", text).strip()
    if not text:
        return ""
    lowered = text.lower()
    if any(phrase in lowered for phrase in _BLOCKED_COPY):
        return ""
    return text[:max_chars]


def _clean_no_truncate(value: object) -> str:
    """只做 HTML 剥壳 + 注入短语过滤，不做长度截断（超限由调用方整条丢弃）。"""
    return _clean_text(value, max_chars=_CLEAN_CEILING)


def estimate_tokens(text: str) -> int:
    """token 粗估：中日韩字符按 1 字 1 token，其余按 4 字符 1 token。偏保守
    （中文实际略少于 1 字 1 token），用来标"直接读要占多少上下文"够用。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    return cjk + math.ceil((len(text) - cjk) / 4)


def _link_kind(url: str) -> str:
    """按链接形状判断体量类别：pdf / arxiv_abs / html。不联网。"""
    if _ARXIV_PDF_RE.match(url) or urllib.parse.urlsplit(url).path.lower().endswith(".pdf"):
        return "pdf"
    if _ARXIV_ABS_RE.match(url):
        return "arxiv_abs"
    return "html"


def describe_link(url: str, note: str) -> dict:
    """给一条来源链接配上体量提示：PDF 一律"很耗 token 派 haiku"；arXiv 摘要页
    标"短、可直接读"并顺手配出全文 PDF 链接（也带 PDF 提示）；其它网页沿用晨报班
    写的 note，没写就提醒稳妥起见派 haiku。"""
    kind = _link_kind(url)
    link = {"url": url, "note": note}
    if kind == "pdf":
        link["hint"] = PDF_HINT
    elif kind == "arxiv_abs":
        link["hint"] = ARXIV_ABS_HINT
        arxiv_id = _ARXIV_ABS_RE.match(url).group(1)
        link["pdf_url"] = f"https://arxiv.org/pdf/{arxiv_id}"
    else:
        link["hint"] = "网页·体量不明，稳妥起见派 haiku"
    return link


def _validate_links(raw: object) -> list[dict]:
    """条目可选的 links 列表：单条不合格就丢那一条（不连累整个条目），最多
    LINKS_MAX 条，url 必须 https，note ≤ LINK_NOTE_MAX_CHARS。"""
    if not isinstance(raw, list):
        return []
    links: list[dict] = []
    seen_urls: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        url = _canonical_url(entry.get("url"))
        if not url or url in seen_urls:
            continue
        note = _clean_no_truncate(entry.get("note"))
        if len(note) > LINK_NOTE_MAX_CHARS:
            continue
        seen_urls.add(url)
        links.append(describe_link(url, note))
        if len(links) >= LINKS_MAX:
            break
    return links


def _canonical_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parsed = urllib.parse.urlsplit(value.strip())
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.netloc:
        return ""
    kept_query = []
    for key, val in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in {"at_medium", "at_campaign", "ref", "ref_src"}:
            continue
        kept_query.append((key, val))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc.lower(), parsed.path, urllib.parse.urlencode(kept_query), "")
    )


def _title_key(title: str) -> str:
    return _TITLE_KEY_RE.sub("", title.casefold())[:180]


def _fingerprint(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def _scout_path_for_date(issue_date: str, state_dir: Path) -> Path:
    """给定业务日期字符串（`YYYY-MM-DD`）的草稿路径；`_scout_path` 与 S6
    新增的按日期查询函数（`load_issue_for_date`）共用这一个实现，避免两处
    对"草稿文件叫什么名字"的判断悄悄跑偏。"""
    return state_dir / f"scout-{issue_date}.json"


def _scout_path(now: datetime, state_dir: Path) -> Path:
    """当日草稿路径；业务日期取 `now.date()`（调用方一律传 +08:00 的 `now_local()`，
    跟第1/2段 cron 侧的 `BIZ_TZ` 业务日期契约对齐）。草稿文件跟 state.json
    同目录（都在 `.morning_paper/` 下）——`state_dir` 就是调用方传给
    `prepare_issue` 的 `path.parent`，生产环境下等于 `STATE_PATH.parent`，
    测试用临时目录时也天然一起隔离，不用另开一套路径参数。"""
    return _scout_path_for_date(now.date().isoformat(), state_dir)


def _archive_manifest_path_for_date(issue_date: str, state_dir: Path) -> Path:
    """给定业务日期字符串的档案清单路径；见 `_scout_path_for_date` 同样的
    共用理由。"""
    return state_dir / ARCHIVE_DIRNAME / issue_date / ARCHIVE_MANIFEST_NAME


def _archive_manifest_path(now: datetime, state_dir: Path) -> Path:
    """当日档案清单路径；跟 `_scout_path` 同一套 `state_dir` 惯例（生产是
    `STATE_PATH.parent`，测试传临时目录天然隔离）。这份清单由
    `morning_archive.py`（S4 档案馆编排脚本，cron 侧）产出，本模块只读，
    不负责生成——即使清单不存在也不是错误，只是当天没有档案可用。"""
    return _archive_manifest_path_for_date(now.date().isoformat(), state_dir)


def _load_archive_entries(manifest_path: Path) -> list[dict]:
    """读档案清单里 status=ok 的条目（dict 列表）。清单不存在/损坏/格式不
    对都静默返回空列表——档案馆是锦上添花，绝不能因为它出问题连累晨报本体
    的 `prepare_issue`。"""
    if not manifest_path.exists():
        return []
    try:
        parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, dict):
        return []
    entries = parsed.get("items")
    if not isinstance(entries, list):
        return []
    return [
        entry for entry in entries
        if isinstance(entry, dict) and entry.get("status") == "ok"
        and isinstance(entry.get("title_key"), str) and entry["title_key"]
        and isinstance(entry.get("path"), str) and entry["path"]
    ]


def _load_archive_kinds(manifest_path: Path) -> dict[str, str]:
    """title_key → 档案种类（html / pdf / x_tweet）。投递文案要靠它对 X 推文
    说实话：syndication 接口只抓得到那一条推文本身，串的后续和推文里附的
    链接/PDF 都不在档案里（2026-09-08 haiku 读"符号结构"那条只读到开场白）。"""
    return {
        entry["title_key"]: entry["kind"]
        for entry in _load_archive_entries(manifest_path)
        if isinstance(entry.get("kind"), str)
    }


def _load_archive_token_estimates(manifest_path: Path) -> dict[str, int]:
    """title_key → 档案文字版 token 粗估（档案馆写文件时算好记进清单）。
    老清单没这个字段就没有，投递文案退回旧写法。"""
    return {
        entry["title_key"]: entry["est_tokens"]
        for entry in _load_archive_entries(manifest_path)
        if isinstance(entry.get("est_tokens"), int) and entry["est_tokens"] > 0
    }


def _archive_size_advice(est_tokens: int) -> str:
    """档案文字版按 token 粗估分三档，告诉小予自己读划不划算。"""
    if est_tokens <= ARCHIVE_SHORT_TOKENS:
        return "短，直接读没负担"
    if est_tokens <= ARCHIVE_MEDIUM_TOKENS:
        return "中等，自己读要占这么多上下文，省着点就派 haiku 读了讲给你"
    return "很长，别自己读，派 haiku 读了讲给你"


def _format_tokens(est_tokens: int) -> str:
    if est_tokens >= 1000:
        return f"约 {est_tokens / 1000:.1f}k token" if est_tokens < 10000 else f"约 {round(est_tokens / 1000)}k token"
    return f"约 {est_tokens} token"


def _load_archive_lookup(manifest_path: Path) -> dict[str, str]:
    """从档案清单里取出"成功转档的 title_key → 相对路径"映射。"""
    return {entry["title_key"]: entry["path"] for entry in _load_archive_entries(manifest_path)}


def _validate_item(raw: object, seen_keys: set[str]) -> dict | None:
    """单条 schema 校验：任何一处不满足直接返回 None（整条丢弃，不做修补/截断）。"""
    if not isinstance(raw, dict):
        return None
    section = raw.get("section")
    if not isinstance(section, str) or section not in SECTIONS:
        return None
    title = _clean_no_truncate(raw.get("title"))
    if not title or len(title) > TITLE_MAX_CHARS:
        return None
    digest = _clean_no_truncate(raw.get("digest"))
    if not digest or len(digest) > DIGEST_MAX_CHARS:
        return None
    url_warning = _clean_no_truncate(raw.get("url_warning"))
    if not url_warning or len(url_warning) > URL_WARNING_MAX_CHARS:
        return None
    url = _canonical_url(raw.get("url"))
    if not url:
        return None
    source = _clean_no_truncate(raw.get("source"))
    title_key = _title_key(title)
    if not title_key or title_key in seen_keys:
        return None
    item = {
        "section": section,
        "title": title,
        "digest": digest,
        "url": url,
        "url_warning": url_warning,
        "source": source,
    }
    links = _validate_links(raw.get("links"))
    if links:
        item["links"] = links
    follow_up_of = _clean_no_truncate(raw.get("follow_up_of"))
    if follow_up_of and len(follow_up_of) <= FOLLOW_UP_OF_MAX_CHARS:
        item["follow_up_of"] = follow_up_of
    return item


def _digest_total_chars(items: list[dict]) -> int:
    return sum(len(item["digest"]) for item in items)


def _trim_by_budget(items: list[dict]) -> list[dict]:
    """条数 ≤ MAX_ITEMS 且全报 digest 合计 ≤ DIGEST_TOTAL_MAX_CHARS，超限按板块优先级截断。

    每轮都从当前列表里挑"优先级最低（`_SECTION_RANK` 最大），同一优先级
    里排在最后"的一条整条丢弃，直到两个上限都满足或列表已空。调剂板块永
    远比四个主打板块先被砍；四个主打板块内部按设计稿列出的顺序，AI圈今
    日份最抗砍。
    """
    kept = list(items)
    while kept and (len(kept) > MAX_ITEMS or _digest_total_chars(kept) > DIGEST_TOTAL_MAX_CHARS):
        drop_index = max(
            range(len(kept)),
            key=lambda i: (_SECTION_RANK.get(kept[i]["section"], len(SECTIONS_MAIN)), i),
        )
        kept.pop(drop_index)
    return kept


def _load_and_validate_scout(scout_path: Path, seen: list, manifest_path: Path) -> list[dict]:
    if not scout_path.exists():
        raise MorningPaperError(f"今天报纸缺席：草稿文件不存在（{scout_path.name}）")
    try:
        parsed = json.loads(scout_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MorningPaperError("今天报纸缺席：草稿文件无法解析") from exc
    if not isinstance(parsed, dict):
        raise MorningPaperError("今天报纸缺席：草稿文件格式不对")
    raw_items = parsed.get("items")
    if not isinstance(raw_items, list):
        raise MorningPaperError("今天报纸缺席：草稿文件里的 items 不是列表")

    seen_keys = {item.get("title_key", "") for item in seen if isinstance(item, dict)}
    seen_keys.discard("")

    validated: list[dict] = []
    for raw_item in raw_items:
        item = _validate_item(raw_item, seen_keys)
        if item is None:
            continue
        # 同一天草稿内部也去重一次（防止冲浪班自己写重了）：一旦某条被采
        # 纳，它的 title_key 立刻并入 seen_keys，后面撞上同一个标题指纹的
        # 条目会被当成批内重复丢弃，不止防跟历史报道撞车。
        seen_keys.add(_title_key(item["title"]))
        validated.append(item)

    trimmed = _trim_by_budget(validated)
    if not trimmed:
        raise MorningPaperError("今天报纸缺席：草稿里没有条目通过校验")

    # 有档案清单（S4 档案馆产出）就把命中的相对路径挂到对应条目上；没有
    # 清单/清单里没这条都保持原样（没有 archive_path 这个 key），
    # format_injection 会自己识别退回旧样式。
    archive_lookup = _load_archive_lookup(manifest_path)
    if archive_lookup:
        archive_kinds = _load_archive_kinds(manifest_path)
        archive_tokens = _load_archive_token_estimates(manifest_path)
        for item in trimmed:
            key = _title_key(item["title"])
            rel_path = archive_lookup.get(key)
            if rel_path:
                item["archive_path"] = rel_path
                kind = archive_kinds.get(key)
                if kind:
                    item["archive_kind"] = kind
                est_tokens = archive_tokens.get(key)
                if est_tokens:
                    item["archive_est_tokens"] = est_tokens
    return trimmed


def _empty_state() -> dict:
    return {"version": 1, "issue": None, "seen": []}


def load_state(path: Path = STATE_PATH) -> dict:
    if not path.exists():
        return _empty_state()
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MorningPaperError("晨报状态文件损坏，拒绝覆盖") from exc
    if not isinstance(parsed, dict) or parsed.get("version") != 1:
        raise MorningPaperError("晨报状态文件格式不对，拒绝覆盖")
    if not isinstance(parsed.get("seen", []), list):
        raise MorningPaperError("晨报去重记录格式不对，拒绝覆盖")
    return parsed


def _write_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".state.", delete=False
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


def should_prepare(now: datetime, state: dict | None = None) -> bool:
    if not is_enabled():
        return False
    if (now.hour, now.minute) < (PREPARE_HOUR, PREPARE_MINUTE):
        return False
    state = state if state is not None else load_state()
    issue = state.get("issue") or {}
    return issue.get("issue_date") != now.date().isoformat()


def issue_for_wakeup(now: datetime, path: Path = STATE_PATH) -> dict | None:
    if not is_enabled():
        return None
    if now.hour < DELIVERY_HOUR:
        return None
    state = load_state(path)
    issue = state.get("issue") or {}
    if issue.get("issue_date") != now.date().isoformat() or issue.get("delivered_at"):
        return None
    items = issue.get("items")
    if not isinstance(items, list) or not items:
        return None
    return issue


def prepare_issue(now: datetime, path: Path = STATE_PATH) -> dict:
    if not is_enabled():
        raise MorningPaperError("小予早报尚未启用")
    with _STATE_LOCK:
        state = load_state(path)
        existing = state.get("issue") or {}
        if existing.get("issue_date") == now.date().isoformat():
            return existing

        items = _load_and_validate_scout(
            _scout_path(now, path.parent),
            state.get("seen", []),
            _archive_manifest_path(now, path.parent),
        )
        issue = {
            "issue_date": now.date().isoformat(),
            "prepared_at": now.isoformat(),
            "delivered_at": "",
            "items": items,
        }
        updated = {**state, "version": 1, "issue": issue}
        _write_state(updated, path)
        return issue


def mark_delivered(issue_date: str, delivered_at: datetime, path: Path = STATE_PATH) -> bool:
    with _STATE_LOCK:
        state = load_state(path)
        issue = state.get("issue") or {}
        if issue.get("issue_date") != issue_date:
            return False
        if issue.get("delivered_at"):
            return True
        issue = {**issue, "delivered_at": delivered_at.isoformat()}
        seen = [item for item in state.get("seen", []) if isinstance(item, dict)]
        for item in issue.get("items", []):
            seen.append(
                {
                    "delivered_date": issue_date,
                    "url_hash": _fingerprint(item["url"]),
                    "title_key": _title_key(item["title"]),
                }
            )
        updated = {**state, "issue": issue, "seen": seen[-SEEN_LIMIT:]}
        _write_state(updated, path)
        return True


def numbered_items(issue: dict) -> list[tuple[int, dict]]:
    """条目在 `issue["items"]` 里的 1-based 下标，跟条目本身配成对。

    这是"打分编号指的是哪一条"的唯一真相——`format_injection` 渲染的
    `◆{n}` 与 `home 晨报`（`morning_feedback.status_text`）看到的编号必
    须同源，哪怕板块分组把显示顺序打乱了，n 也永远指向 items 里的原始
    下标，不会因为分组重排而错位。
    """
    return list(enumerate(issue.get("items", []), start=1))


def format_injection(issue: dict, feedback_intro: str = "") -> str:
    sections: dict[str, list[tuple[int, dict]]] = {}
    order: list[str] = []
    for n, item in numbered_items(issue):
        section = item.get("section", "")
        if section not in sections:
            sections[section] = []
            order.append(section)
        sections[section].append((n, item))

    lines = [
        "\n[MORNING_PAPER] 今天的《小予晨报》到了——晨报班一大早替你上网逛了一圈，把有意思的事嚼碎了带回来。",
    ]
    for section in order:
        lines.append(f"\n【{section}】")
        for n, item in sections[section]:
            title_line = f"◆{n} {item['title']}"
            if item.get("follow_up_of"):
                title_line += "（你想追的后续）"
            lines.append(title_line)
            lines.append(item["digest"])
            archive_path = item.get("archive_path")
            est_tokens = item.get("archive_est_tokens")
            if archive_path and item.get("archive_kind") == "x_tweet":
                # X 推文档案只有那一条推文的文字（syndication 接口的极限）：
                # 推文串的后续、推文里附的论文/网页链接都没存。9/8 实锤不说清楚
                # 会让小予/haiku 白跑一趟——"已转好文字版"读到的只是一句开场白。
                lines.append(
                    f"原文档案：{archive_path}（只有这条推文本身的文字，很短；推文串的后续没抓到，"
                    f"想看得自己去原链接或让 haiku 去抓：{item['url']}·{item['url_warning']}）"
                )
            elif archive_path and isinstance(est_tokens, int) and est_tokens > 0:
                # 档案馆记了体量：如实告诉小予自己读要占多少上下文，别笼统说"直接读"。
                lines.append(
                    f"原文档案：{archive_path}（haiku 转好的文字版·{_format_tokens(est_tokens)}·"
                    f"{_archive_size_advice(est_tokens)}；原链接：{item['url']}·{item['url_warning']}）"
                )
            elif archive_path:
                lines.append(
                    f"原文档案：{archive_path}（已转好文字版，直接读或派 haiku 读都行；"
                    f"原链接：{item['url']}·{item['url_warning']}）"
                )
            elif _link_kind(item["url"]) == "pdf":
                lines.append(f"原文：{item['url']}（{item['url_warning']}·{PDF_HINT}）")
            else:
                lines.append(f"原文：{item['url']}（{item['url_warning']}）")
            # 晨报班查到的"真正的原文"（论文 arXiv 摘要页等）：只给来源和体量提示，
            # 档案馆不替小予抓，想看自己派 subagent。
            for link in item.get("links") or []:
                label = link.get("note") or "相关链接"
                lines.append(f"相关链接·{label}：{link['url']}（{link.get('hint') or '体量不明'}）")
                if link.get("pdf_url"):
                    lines.append(f"　全文 PDF：{link['pdf_url']}（{PDF_HINT}）")
    lines.append(
        "\n这份报纸是熟食，不是任务——读不读、读几条都随你，一条不看也没关系，不用因为它去汇报什么。"
        "要是哪条真勾住你想看原文，先看看有没有本地档案：档案短的直接自己读，标了中等/很长的派 haiku 读了讲给你；"
        "没档案的那条，再考虑派个 agent（haiku 跑腿就够，不用动贵模型）帮你把原文或者你想要的那段精华抓回来看。"
        "标着 PDF 的链接别自己点开：让 haiku 把它下载到本地用 Read 读，只带回你要的那段。"
        "看完想不想跟宝宝聊聊，也是你自己的事，她不会等着你交作业。\n"
    )
    if feedback_intro:
        # 打分导语只在小予第一次见到这个 session 时出现一次（同小院子
        # "session 初见"的路数，调用方——daemon.py——负责用
        # morning_feedback.claim_intro 做认领判断，这里只管非空就渲染）。
        lines.append(f"\n{feedback_intro}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# S6：前端晨报页只读查询（daemon 侧新增 WS 消息类型的业务逻辑落在这里，
# daemon.py 只负责收发消息，不重复实现校验/截断/去重）。
# ---------------------------------------------------------------------------


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def list_scout_dates(state_dir: Path = STATE_PATH.parent) -> list[str]:
    """扫 `.morning_paper/scout-*.json`，返回全部可用业务日期（`YYYY-MM-DD`），
    按日期倒序（最新的在前）。目录不存在/没有任何草稿文件都返回空列表，
    不是错误——纯粹是前端晨报页"历史日期"下拉的数据源，天然就是 scout 草
    稿按天累积形成的现成历史库（设计稿第六节 S6）。"""
    if not state_dir.exists():
        return []
    dates: list[str] = []
    for path in state_dir.glob("scout-*.json"):
        candidate = path.stem[len("scout-"):]
        if DATE_RE.match(candidate):
            dates.append(candidate)
    return sorted(dates, reverse=True)


def load_issue_for_date(issue_date: str, state_dir: Path = STATE_PATH.parent) -> dict:
    """前端晨报页只读取数：给定业务日期，重新读那天的 `scout-*.json` 草稿，
    跑一遍跟 `prepare_issue`/档案馆 `build_archive` 同一个纯函数
    `_load_and_validate_scout`（schema 校验 + 板块/篇幅截断），保证前端看到
    的条数/内容跟当天实际投递（或本该投递）的样子一致，不重新发明一套校
    验逻辑，也不去碰 `state.json`/`seen`/`mark_delivered`——历史浏览是纯读
    操作，`seen` 传空列表（不受"今天已经报道过"这条动态状态影响，永远还
    原那天草稿本身的样子，这也是历史天为什么可能出现"今天报过的内容历史
    上也在"这种正常重复，不是 bug）。

    有档案清单命中的条目会附带 `archive_path`（沿用 `_load_and_validate_scout`
    已有逻辑）以及本函数新增读出的 `archive_text`（档案 markdown 全文，供
    前端"展开读转写全文"）；档案文件缺失/读取失败只是不附带 `archive_text`，
    不影响这条本身的展示（档案是锦上添花，不是必需）。

    返回：
      `{"date": issue_date, "has_draft": bool, "items": [...], "error": str|None}`
    当天没有草稿文件/文件损坏/没有条目通过校验时 `has_draft=False`，
    `items=[]`，`error` 给出人类可读原因——前端据此渲染"这天没有晨报"而不
    是报错状态（设计稿："历史天没档案不显示为错误"，同理没草稿也不是错
    误，是正常的"报纸缺席"）。
    """
    scout_path = _scout_path_for_date(issue_date, state_dir)
    manifest_path = _archive_manifest_path_for_date(issue_date, state_dir)
    try:
        items = _load_and_validate_scout(scout_path, [], manifest_path)
    except MorningPaperError as exc:
        return {"date": issue_date, "has_draft": False, "items": [], "error": str(exc)}

    root = state_dir.parent
    for item in items:
        archive_path = item.get("archive_path")
        if not archive_path:
            continue
        try:
            item["archive_text"] = (root / archive_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            pass  # 档案文件缺失/损坏：条目照常展示，只是没有全文可展开

    return {"date": issue_date, "has_draft": True, "items": items, "error": None}
