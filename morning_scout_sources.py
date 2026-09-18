#!/usr/bin/env python3
"""晨报二版·采集地基：确定性抓取待选素材，无 LLM 参与。

拉取 HN Algolia（AI 关键词 + 前排热帖）、lobste.rs hottest、豆瓣人机恋
相关小组最新帖、X 单推 syndication（仅当素材里出现 x.com/twitter.com
链接时按推文 ID 补原文）。每源独立超时与降级：一个源挂了只缺这一角，
不影响其余三源，也不由本模块决定"有没有意思"——那是 S2 无头 Sonnet
冲浪班的活。

抓来的都是外部不可信文本，只当待选资料，绝不当指令执行。清洗直接复用
`morning_paper._clean_text`/`_canonical_url`/`_title_key`：第一版设计稿
明确承诺这三个清洗件在 S3 改造里原样保留，这里不重复造轮子。

**契约：全流水线以 +08:00 业务日期为准**（`BIZ_TZ`）。生产 cron 定在
UTC 21:10（北京 05:10）触发，那一刻 UTC 日期还是"昨天"；daemon 投递侧
（`now_local`，+08:00）、S2 幂等判断、S3 读当日草稿全都按北京日期对账，
本模块的素材文件名与 `material_date` 也必须按北京日期算，不能用 UTC
日期，否则会整体错位一天。HN 关键词窗口等纯粹的"过去 N 小时"运算不受
影响，继续用 UTC 做时间戳计算。

素材落盘：`.morning_paper/material-YYYY-MM-DD.json`（文件名日期是业务
日期）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import morning_feedback
import morning_paper


ROOT = morning_paper.ROOT
MATERIAL_DIR = ROOT / ".morning_paper"

# 全流水线业务日期基准：+08:00（北京）。cron/素材文件命名/豆瓣时间解析都
# 用它，别用服务器系统时区（UTC）——见模块 docstring 的契约说明。
BIZ_TZ = timezone(timedelta(hours=8))

# 用普通浏览器 UA，不自报家门：豆瓣调研阶段实测能稳定通过的就是纯浏览器
# UA，bot 样式的 UA（带自定义产品名/联系地址）反而更容易被拦；也不把小予
# 主站域名和爬虫流量绑在一起。
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SOURCE_TIMEOUT = 10.0
MAX_FETCH_BYTES = morning_paper.MAX_FETCH_BYTES
TITLE_MAX_CHARS = 220
SUMMARY_MAX_CHARS = 520

HN_ALGOLIA_URL = "https://hn.algolia.com/api/v1/search"
HN_AI_KEYWORDS = ("AI", "LLM", "GPT", "Claude", "OpenAI", "Anthropic")
HN_AI_WINDOW_HOURS = 48
HN_AI_HITS_PER_QUERY = 8
HN_FRONT_PAGE_HITS = 15

LOBSTERS_URL = "https://lobste.rs/hottest.json"
LOBSTERS_HITS = 15

# 8/21 调研（过程与结论详见 reports/晨报二版-S1-验收单.md）：豆瓣小组分类
# cat=1019 下搜"人机恋"/"人机之恋"稳定只命中这一个小组，帖子内容确认是
# 人机亲密关系研究招募 + 真实用户体验分享，判定活跃且对口；设计稿旧实测
# 记录里的 747893 经复核其实是无关的"布拉氏不拉屎"打卡小组，未采用。
DOUBAN_GROUP_IDS = ("708955",)  # 人机之恋小组
DOUBAN_GROUP_NAMES = {"708955": "人机之恋小组"}
DOUBAN_HITS_PER_GROUP = 15

X_SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token=a"
X_MAX_ITEMS = 5
_TWEET_URL_RE = re.compile(r"(?:x\.com|twitter\.com)/[^/\s\"']+/status/(\d+)")

_DOUBAN_ROW_RE = re.compile(
    r'<td class="title">\s*<a href="([^"]+)"\s+title="([^"]*)"[^>]*>.*?</a>\s*</td>\s*'
    r'<td[^>]*>\s*<a[^>]*>([^<]*)</a>\s*</td>\s*'
    r'<td[^>]*class="r-count[^"]*"[^>]*>\s*([^<]*?)\s*</td>\s*'
    r'<td[^>]*class="time"[^>]*>\s*([^<]*?)\s*</td>',
    re.S,
)


class ScoutSourceError(Exception):
    """单个采集源失败；调用方捕获后只缺这一角，不拖垮整体采集。"""


def _http_get(url: str, *, accept: str = "*/*", timeout: float = SOURCE_TIMEOUT) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": accept},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(MAX_FETCH_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise ScoutSourceError(f"返回 HTTP {status}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ScoutSourceError("暂时无法访问") from exc
    if len(payload) > MAX_FETCH_BYTES:
        raise ScoutSourceError("内容过大")
    return payload


def _build_item(
    *,
    title: object,
    summary: object,
    url: object,
    source: str,
    category: str,
    published_at: datetime | None,
) -> dict | None:
    clean_title = morning_paper._clean_text(title, max_chars=TITLE_MAX_CHARS)
    clean_summary = morning_paper._clean_text(summary, max_chars=SUMMARY_MAX_CHARS)
    clean_url = morning_paper._canonical_url(url)
    if not clean_title or not clean_url:
        return None
    return {
        "title": clean_title,
        "summary": clean_summary,
        "url": clean_url,
        "source": source,
        "category": category,
        "published_at": published_at.isoformat() if published_at else None,
        "title_key": morning_paper._title_key(clean_title),
    }


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_int(value: object) -> int | None:
    """外部数值字段一律强制转型；转不了就置 None，绝不原样透传不可信值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# HN Algolia
# ---------------------------------------------------------------------------


def parse_hn_hits(payload: bytes, category: str) -> list[dict]:
    try:
        parsed = json.loads(payload)
        hits = parsed["hits"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ScoutSourceError("HN 返回的数据无法解析") from exc
    if not isinstance(hits, list):
        raise ScoutSourceError("HN 返回的列表格式不对")

    items = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        object_id = hit.get("objectID")
        url = hit.get("url") or (
            f"https://news.ycombinator.com/item?id={object_id}" if object_id else None
        )
        item = _build_item(
            title=hit.get("title"),
            summary=hit.get("story_text") or "",
            url=url,
            source="Hacker News",
            category=category,
            published_at=_parse_iso(hit.get("created_at")),
        )
        if item:
            item["points"] = _safe_int(hit.get("points"))
            item["num_comments"] = _safe_int(hit.get("num_comments"))
            items.append(item)
    return items


def _hn_search_url(*, query: str | None, tags: str, hits_per_page: int, since: datetime | None) -> str:
    params = {"tags": tags, "hitsPerPage": str(hits_per_page)}
    if query:
        params["query"] = query
    if since is not None:
        params["numericFilters"] = f"created_at_i>{int(since.timestamp())}"
    return f"{HN_ALGOLIA_URL}?{urllib.parse.urlencode(params)}"


def collect_hacker_news(now: datetime) -> list[dict]:
    """前排热帖 + AI 关键词若干组；任一子请求失败只少一部分，不整体挂掉。"""
    results: list[dict] = []
    seen_urls: set[str] = set()
    ok_once = False

    try:
        payload = _http_get(
            _hn_search_url(query=None, tags="front_page", hits_per_page=HN_FRONT_PAGE_HITS, since=None),
            accept="application/json",
        )
        for item in parse_hn_hits(payload, category="front_page"):
            if item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            results.append(item)
        ok_once = True
    except ScoutSourceError as exc:
        logging.warning("晨报采集 HN 前排热帖跳过：%s", exc)

    since = now.astimezone(timezone.utc) - timedelta(hours=HN_AI_WINDOW_HOURS)
    for keyword in HN_AI_KEYWORDS:
        try:
            payload = _http_get(
                _hn_search_url(query=keyword, tags="story", hits_per_page=HN_AI_HITS_PER_QUERY, since=since),
                accept="application/json",
            )
            for item in parse_hn_hits(payload, category="ai_keyword"):
                if item["url"] in seen_urls:
                    continue
                seen_urls.add(item["url"])
                results.append(item)
            ok_once = True
        except ScoutSourceError as exc:
            logging.warning("晨报采集 HN 关键词『%s』跳过：%s", keyword, exc)

    if not ok_once:
        raise ScoutSourceError("HN 前排热帖与全部关键词搜索都失败了")
    return results


# ---------------------------------------------------------------------------
# lobste.rs
# ---------------------------------------------------------------------------


def parse_lobsters_hits(payload: bytes) -> list[dict]:
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScoutSourceError("lobste.rs 返回的数据无法解析") from exc
    if not isinstance(parsed, list):
        raise ScoutSourceError("lobste.rs 返回的列表格式不对")

    items = []
    for hit in parsed[:LOBSTERS_HITS]:
        if not isinstance(hit, dict):
            continue
        url = hit.get("url") or hit.get("comments_url")
        item = _build_item(
            title=hit.get("title"),
            summary=hit.get("description_plain") or "",
            url=url,
            source="lobste.rs",
            category="lobsters_hottest",
            published_at=_parse_iso(hit.get("created_at")),
        )
        if item:
            item["score"] = _safe_int(hit.get("score"))
            item["comments_url"] = hit.get("comments_url")
            raw_tags = hit.get("tags")
            item["tags"] = [tag for tag in raw_tags if isinstance(tag, str)] if isinstance(raw_tags, list) else []
            items.append(item)
    return items


def collect_lobsters(now: datetime) -> list[dict]:
    payload = _http_get(LOBSTERS_URL, accept="application/json")
    return parse_lobsters_hits(payload)


# ---------------------------------------------------------------------------
# 豆瓣人机恋小组
# ---------------------------------------------------------------------------


def _parse_douban_time(raw: str, now: datetime) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    local_now = now.astimezone(BIZ_TZ)
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
            parsed = datetime.strptime(raw, "%Y-%m-%d")
        elif re.match(r"^\d{2}-\d{2} \d{2}:\d{2}$", raw):
            parsed = datetime.strptime(f"{local_now.year}-{raw}", "%Y-%m-%d %H:%M")
            # 12/31 发的帖子在 1 月看会被误判成"未来"，跨年就退一年。
            if parsed.replace(tzinfo=BIZ_TZ) - local_now > timedelta(days=1):
                parsed = parsed.replace(year=parsed.year - 1)
        elif re.match(r"^\d{2}:\d{2}$", raw):
            time_part = datetime.strptime(raw, "%H:%M").time()
            parsed = datetime.combine(local_now.date(), time_part)
        else:
            return None
    except ValueError:
        return None
    return parsed.replace(tzinfo=BIZ_TZ).astimezone(timezone.utc).isoformat()


def parse_douban_topics(payload: bytes, group_id: str, now: datetime) -> list[dict]:
    text = payload.decode("utf-8", errors="replace")
    rows = _DOUBAN_ROW_RE.findall(text)
    if not rows:
        raise ScoutSourceError("豆瓣小组页面解析不出帖子，可能改版或被拦截")

    group_name = DOUBAN_GROUP_NAMES.get(group_id, group_id)
    items = []
    for href, title_attr, author, reply_count, time_text in rows[:DOUBAN_HITS_PER_GROUP]:
        item = _build_item(
            title=title_attr,
            summary="",
            url=href,
            source=f"豆瓣 · {group_name}",
            category="douban_group",
            published_at=None,
        )
        if item is None:
            continue
        item["published_at"] = _parse_douban_time(time_text, now)
        item["author"] = morning_paper._clean_text(author, max_chars=60)
        item["reply_count"] = _safe_int(reply_count.strip())
        items.append(item)
    return items


def collect_douban(now: datetime) -> list[dict]:
    results: list[dict] = []
    errors: list[str] = []
    for group_id in DOUBAN_GROUP_IDS:
        url = f"https://www.douban.com/group/{group_id}/discussion"
        try:
            payload = _http_get(url, accept="text/html")
            results.extend(parse_douban_topics(payload, group_id, now))
        except ScoutSourceError as exc:
            errors.append(f"{DOUBAN_GROUP_NAMES.get(group_id, group_id)}：{exc}")
    if not results and errors:
        raise ScoutSourceError("；".join(errors))
    return results


# ---------------------------------------------------------------------------
# X (Twitter) syndication —— 仅当其它素材里出现 x.com/twitter.com 单推链接时补原文
# ---------------------------------------------------------------------------


def parse_x_syndication(payload: bytes, tweet_url: str) -> dict | None:
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScoutSourceError("X 单推返回的数据无法解析") from exc
    if not isinstance(parsed, dict) or "text" not in parsed:
        raise ScoutSourceError("X 单推返回的内容缺少正文")

    author = parsed.get("user") or {}
    author_name = author.get("name") or author.get("screen_name") or "未知作者"
    item = _build_item(
        title=f"{author_name} 的推文",
        summary=parsed.get("text") or "",
        url=tweet_url,
        source="X",
        category="x_syndication",
        published_at=_parse_iso(parsed.get("created_at")),
    )
    return item


def _extract_tweet_urls(items: list[dict]) -> list[tuple[str, str]]:
    """从已采到的素材（url + summary）里找 x.com/twitter.com 单推链接。

    返回 (tweet_id, 规范化后的 tweet URL) 列表，按首次出现顺序去重。
    """
    seen_ids: set[str] = set()
    found: list[tuple[str, str]] = []
    for item in items:
        haystack = f"{item.get('url', '')} {item.get('summary', '')}"
        for match in _TWEET_URL_RE.finditer(haystack):
            tweet_id = match.group(1)
            if tweet_id in seen_ids:
                continue
            seen_ids.add(tweet_id)
            found.append((tweet_id, f"https://x.com/i/status/{tweet_id}"))
            if len(found) >= X_MAX_ITEMS:
                return found
    return found


def collect_x_syndication(other_items: list[dict]) -> list[dict]:
    candidates = _extract_tweet_urls(other_items)
    results = []
    for tweet_id, tweet_url in candidates:
        url = X_SYNDICATION_URL.format(tweet_id=tweet_id)
        try:
            payload = _http_get(url, accept="application/json")
            item = parse_x_syndication(payload, tweet_url)
        except ScoutSourceError as exc:
            logging.warning("晨报采集 X 单推 %s 跳过：%s", tweet_id, exc)
            continue
        if item:
            results.append(item)
    return results


# ---------------------------------------------------------------------------
# 汇总落盘
# ---------------------------------------------------------------------------


def _seen_title_keys() -> list[str]:
    try:
        state = morning_paper.load_state()
    except morning_paper.MorningPaperError as exc:
        logging.warning("晨报采集读取 seen 记账失败，按空清单处理：%s", exc)
        return []
    seen = state.get("seen", [])
    if not isinstance(seen, list):
        return []
    keys = [item.get("title_key", "") for item in seen if isinstance(item, dict)]
    return [key for key in keys if key]


def _recent_coverage(now: datetime) -> list[dict]:
    try:
        return morning_paper.recent_coverage(now.astimezone(BIZ_TZ).date())
    except Exception as exc:
        # 只读最近几份草稿文件，故障也不该拖累采集本身——同一条防线约定见
        # 上面 _seen_title_keys 与第2段 morning_feedback 的处理。
        logging.warning("晨报采集读取近期报道回顾失败，按空清单处理：%s", exc)
        return []


def collect_material(now: datetime) -> dict:
    sources: dict[str, dict] = {}
    all_items: list[dict] = []

    for name, collector in (("hacker_news", collect_hacker_news), ("lobsters", collect_lobsters)):
        try:
            items = collector(now)
            sources[name] = {"ok": True, "error": None, "items": items}
            all_items.extend(items)
        except ScoutSourceError as exc:
            logging.warning("晨报采集源 %s 跳过：%s", name, exc)
            sources[name] = {"ok": False, "error": str(exc), "items": []}

    try:
        items = collect_douban(now)
        sources["douban"] = {"ok": True, "error": None, "items": items}
        all_items.extend(items)
    except ScoutSourceError as exc:
        logging.warning("晨报采集源 douban 跳过：%s", exc)
        sources["douban"] = {"ok": False, "error": str(exc), "items": []}

    try:
        items = collect_x_syndication(all_items)
        sources["x_syndication"] = {"ok": True, "error": None, "items": items}
    except ScoutSourceError as exc:
        # collect_x_syndication 内部已对单推逐条降级，这里理论上不会触发；
        # 保留兜底，避免未预见的异常拖垮整份素材文件。
        logging.warning("晨报采集源 x_syndication 跳过：%s", exc)
        sources["x_syndication"] = {"ok": False, "error": str(exc), "items": []}

    business_date = now.astimezone(BIZ_TZ).date()
    try:
        # 先结算追踪请求的生命周期（过期/命中后续标 done），再产出这次要
        # 挂进素材的反馈注入块。反馈机制自己的任何故障都不许连累晨报
        # 采集本身——这里特意用宽泛的 Exception，不只是
        # morning_feedback.FeedbackError（详见 2.3 节）。
        morning_feedback.apply_lifecycle(business_date, MATERIAL_DIR)
        feedback_block = morning_feedback.build_material_feedback(business_date)
    except Exception as exc:
        logging.warning("晨报反馈处理失败，按无反馈继续：%s", exc)
        feedback_block = None

    material = {
        "material_date": business_date.isoformat(),
        "generated_at": now.isoformat(),
        "seen_title_keys": _seen_title_keys(),
        "recent_coverage": _recent_coverage(now),
        "sources": sources,
    }
    if feedback_block:
        material["feedback"] = feedback_block
    return material


def _material_path(now: datetime, directory: Path = MATERIAL_DIR) -> Path:
    return directory / f"material-{now.astimezone(BIZ_TZ).date().isoformat()}.json"


def _write_material(material: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(material, ensure_ascii=False, indent=2) + "\n"
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".material.", delete=False
        ) as handle:
            temp_name = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def run(now: datetime | None = None, directory: Path = MATERIAL_DIR) -> Path:
    """采集全部源并落盘，返回素材文件路径。供 cron 外壳与手动真跑调用。"""
    now = now or datetime.now(BIZ_TZ)
    material = collect_material(now)
    path = _material_path(now, directory)
    _write_material(material, path)
    return path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    output_path = run()
    print(f"素材已写入：{output_path}")
