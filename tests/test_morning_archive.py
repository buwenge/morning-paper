import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import morning_archive
import morning_paper


TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 21, 5, 20, tzinfo=TZ)

# 真实草稿/素材，取自本仓库 tests/fixtures/（跟 test_morning_paper.py 用的是
# 同一份 S2 真实产出）。异常分支在真实条目上改出来，不手写假设形状。
REAL_SCOUT_PATH = Path(__file__).resolve().parent / "fixtures" / "scout-2026-08-21.json"
REAL_SCOUT_DOC = json.loads(REAL_SCOUT_PATH.read_text(encoding="utf-8"))
REAL_ITEMS = REAL_SCOUT_DOC["items"]
# items[0] = dontpastetheai.com（普通 html）；items[1] = X 单推
# （MatznerJon，登录墙锁死，走 X 专属分支）；items[5] = zenodo 论文落地页
# （html，非 pdf——见 DetectKindTests 里对这条的专门验证）。
REAL_HTML_ITEM = REAL_ITEMS[0]
REAL_X_ITEM = REAL_ITEMS[1]

REAL_MATERIAL_PATH = Path(__file__).resolve().parent / "fixtures" / "material-2026-08-21.json"
REAL_MATERIAL_DOC = json.loads(REAL_MATERIAL_PATH.read_text(encoding="utf-8"))
REAL_X_SYNDICATION_ITEMS = REAL_MATERIAL_DOC["sources"]["x_syndication"]["items"]
# 真实验证过的联结键场景：REAL_X_ITEM 的 url 是
# https://x.com/MatznerJon/status/2090157152690196754（作者名段），而素材
# 文件里对应的条目 url 是 https://x.com/i/status/2090157152690196754
# （syndication 规范化成的 /i/ 段）——两个 URL 字符串不相等，只有数字推文
# ID 相同，这正是 _find_tweet_material 要按 ID 而不是按 URL 字符串匹配的
# 真实理由。


class FakeHeadResponse:
    def __init__(self, content_type: str):
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeBytesResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int = -1) -> bytes:
        return self.payload[: limit if limit and limit >= 0 else None]


class FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _success_payload() -> str:
    return json.dumps({"is_error": False, "permission_denials": [], "result": "ok"})


class SlugTests(unittest.TestCase):
    def test_strips_www_and_lowercases(self):
        self.assertEqual(morning_archive._slug_from_url("https://www.Example.COM/a/b"), "example-com")

    def test_falls_back_to_item_when_no_netloc(self):
        self.assertEqual(morning_archive._slug_from_url("not-a-url"), "item")

    def test_truncates_to_max_chars(self):
        long_host = "a" * 80 + ".com"
        slug = morning_archive._slug_from_url(f"https://{long_host}/x")
        self.assertLessEqual(len(slug), morning_archive.SLUG_MAX_CHARS)

    def test_real_urls_produce_stable_ascii_slugs(self):
        for item in REAL_ITEMS:
            slug = morning_archive._slug_from_url(item["url"])
            self.assertRegex(slug, r"^[a-z0-9-]+$")
            self.assertTrue(slug)


class DetectKindTests(unittest.TestCase):
    def test_pdf_suffix_short_circuits_without_network(self):
        with patch.object(morning_archive.urllib.request, "urlopen") as urlopen:
            kind = morning_archive._detect_kind("https://example.org/paper.PDF")
        self.assertEqual(kind, "pdf")
        urlopen.assert_not_called()

    def test_content_type_pdf_detected_via_head(self):
        with patch.object(
            morning_archive.urllib.request, "urlopen", return_value=FakeHeadResponse("application/pdf")
        ):
            self.assertEqual(morning_archive._detect_kind("https://example.org/x"), "pdf")

    def test_content_type_html_detected_via_head(self):
        with patch.object(
            morning_archive.urllib.request,
            "urlopen",
            return_value=FakeHeadResponse("text/html; charset=utf-8"),
        ):
            self.assertEqual(morning_archive._detect_kind("https://example.org/x"), "html")

    def test_head_failure_falls_back_to_html_not_pdf(self):
        with patch.object(morning_archive.urllib.request, "urlopen", side_effect=OSError("boom")):
            self.assertEqual(morning_archive._detect_kind("https://example.org/x"), "html")

    def test_real_zenodo_landing_page_is_html_not_pdf(self):
        # S2.2 真实草稿里的 Zenodo 条目链接是论文落地页（本身是 text/html，
        # 真正的 PDF 在另一个子路径），内容类型探测应该正确判成 html，不能
        # 被 url_warning 里的"PDF"字样误导（本函数根本不看 url_warning）。
        zenodo_item = next(item for item in REAL_ITEMS if "zenodo.org" in item["url"])
        with patch.object(
            morning_archive.urllib.request,
            "urlopen",
            return_value=FakeHeadResponse("text/html; charset=utf-8"),
        ):
            self.assertEqual(morning_archive._detect_kind(zenodo_item["url"]), "html")


class DownloadPdfTests(unittest.TestCase):
    def test_writes_payload_to_dest(self):
        with tempfile.TemporaryDirectory() as tempdir:
            dest = Path(tempdir) / "a.pdf"
            with patch.object(
                morning_archive.urllib.request, "urlopen", return_value=FakeBytesResponse(b"%PDF-1.4 fake")
            ):
                morning_archive._download_pdf("https://example.org/a.pdf", dest)
            self.assertEqual(dest.read_bytes(), b"%PDF-1.4 fake")

    def test_oversized_payload_raises_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as tempdir:
            dest = Path(tempdir) / "a.pdf"
            oversized = b"x" * (morning_archive.PDF_MAX_BYTES + 1)
            with patch.object(
                morning_archive.urllib.request, "urlopen", return_value=FakeBytesResponse(oversized)
            ):
                with self.assertRaises(morning_archive.ArchiveError):
                    morning_archive._download_pdf("https://example.org/big.pdf", dest)
            self.assertFalse(dest.exists())

    def test_network_error_raises_archive_error(self):
        with tempfile.TemporaryDirectory() as tempdir:
            dest = Path(tempdir) / "a.pdf"
            with patch.object(morning_archive.urllib.request, "urlopen", side_effect=OSError("timeout")):
                with self.assertRaises(morning_archive.ArchiveError):
                    morning_archive._download_pdf("https://example.org/a.pdf", dest)


class InvokeHaikuTests(unittest.TestCase):
    def test_success_returns_none(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.object(
            morning_archive.subprocess, "run", return_value=FakeCompletedProcess(0, stdout=_success_payload())
        ):
            morning_archive._invoke_haiku(
                "prompt", tools="WebFetch,Write", allowed_tools="WebFetch", workdir=Path(tempdir)
            )  # 不抛异常即通过

    def test_subprocess_timeout_raises_archive_timeout(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.object(
            morning_archive.subprocess,
            "run",
            side_effect=morning_archive.subprocess.TimeoutExpired(cmd="claude", timeout=120),
        ):
            with self.assertRaises(morning_archive.ArchiveTimeout):
                morning_archive._invoke_haiku(
                    "prompt", tools="WebFetch,Write", allowed_tools="WebFetch", workdir=Path(tempdir)
                )

    def test_nonzero_exit_raises_archive_error(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.object(
            morning_archive.subprocess,
            "run",
            return_value=FakeCompletedProcess(1, stderr="boom"),
        ):
            with self.assertRaises(morning_archive.ArchiveError):
                morning_archive._invoke_haiku(
                    "prompt", tools="WebFetch,Write", allowed_tools="WebFetch", workdir=Path(tempdir)
                )

    def test_invalid_json_raises_archive_error(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.object(
            morning_archive.subprocess, "run", return_value=FakeCompletedProcess(0, stdout="{not json")
        ):
            with self.assertRaises(morning_archive.ArchiveError):
                morning_archive._invoke_haiku(
                    "prompt", tools="WebFetch,Write", allowed_tools="WebFetch", workdir=Path(tempdir)
                )

    def test_is_error_true_raises_archive_error(self):
        payload = json.dumps({"is_error": True, "permission_denials": [], "result": "API Error"})
        with tempfile.TemporaryDirectory() as tempdir, patch.object(
            morning_archive.subprocess, "run", return_value=FakeCompletedProcess(0, stdout=payload)
        ):
            with self.assertRaises(morning_archive.ArchiveError):
                morning_archive._invoke_haiku(
                    "prompt", tools="WebFetch,Write", allowed_tools="WebFetch", workdir=Path(tempdir)
                )

    def test_permission_denial_raises_archive_error_even_if_exit_zero(self):
        payload = json.dumps({"is_error": False, "permission_denials": ["Write denied"], "result": "ok"})
        with tempfile.TemporaryDirectory() as tempdir, patch.object(
            morning_archive.subprocess, "run", return_value=FakeCompletedProcess(0, stdout=payload)
        ):
            with self.assertRaises(morning_archive.ArchiveError):
                morning_archive._invoke_haiku(
                    "prompt", tools="WebFetch,Write", allowed_tools="WebFetch", workdir=Path(tempdir)
                )


class ArchiveItemTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tempdir.name)
        self.archive_dir = self.state_dir / "archive" / "2026-08-21"
        self.archive_dir.mkdir(parents=True)
        self.workdir = self.state_dir / "workdir"
        self.workdir.mkdir()
        self.item = dict(REAL_HTML_ITEM)  # 真实条目，普通网页（不是 X 单推，不会被专属分支拦截）

    def tearDown(self):
        self.tempdir.cleanup()

    def test_html_success_writes_manifest_ok_entry(self):
        expected_slug = morning_archive._slug_from_url(self.item["url"])
        expected_filename = f"01-{expected_slug}.md"

        def fake_invoke(prompt, *, tools, allowed_tools, workdir):
            # 模拟 haiku 真的写出了文件（文件名跟 archive_item 自己算出来的一致）
            target = self.archive_dir / expected_filename
            target.write_text("---\ntitle: t\n---\n正文", encoding="utf-8")

        with patch.object(morning_archive, "_detect_kind", return_value="html"), patch.object(
            morning_archive, "_invoke_haiku", side_effect=fake_invoke
        ):
            entry = morning_archive.archive_item(1, self.item, self.archive_dir, self.state_dir, self.workdir)

        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["kind"], "html")
        self.assertEqual(entry["path"], f".morning_paper/archive/2026-08-21/{expected_filename}")
        self.assertEqual(entry["title_key"], morning_paper._title_key(self.item["title"]))
        self.assertEqual(entry["url_hash"], morning_paper._fingerprint(self.item["url"]))
        self.assertIsNone(entry["error"])
        # 体量记进清单，投递文案据此标"直接读要占多少上下文"
        written = "---\ntitle: t\n---\n正文"
        self.assertEqual(entry["chars"], len(written))
        self.assertEqual(entry["est_tokens"], morning_paper.estimate_tokens(written))

    def test_haiku_claims_success_but_no_file_is_failure(self):
        with patch.object(morning_archive, "_detect_kind", return_value="html"), patch.object(
            morning_archive, "_invoke_haiku", return_value=None
        ):
            entry = morning_archive.archive_item(1, self.item, self.archive_dir, self.state_dir, self.workdir)
        self.assertEqual(entry["status"], "failed")
        self.assertIsNone(entry["path"])
        self.assertIn("未产出文件", entry["error"])

    def test_html_timeout_is_recorded_not_raised(self):
        with patch.object(morning_archive, "_detect_kind", return_value="html"), patch.object(
            morning_archive, "_invoke_haiku", side_effect=morning_archive.ArchiveTimeout("超时（120s）")
        ):
            entry = morning_archive.archive_item(1, self.item, self.archive_dir, self.state_dir, self.workdir)
        self.assertEqual(entry["status"], "failed")
        self.assertIn("超时", entry["error"])

    def test_unexpected_exception_is_captured_not_propagated(self):
        with patch.object(morning_archive, "_detect_kind", side_effect=RuntimeError("糟糕")):
            entry = morning_archive.archive_item(1, self.item, self.archive_dir, self.state_dir, self.workdir)
        self.assertEqual(entry["status"], "failed")
        self.assertIn("未预期错误", entry["error"])

    def test_pdf_flow_downloads_then_invokes_and_cleans_up_raw_pdf(self):
        pdf_path = self.archive_dir / "01-example-com.pdf"

        def fake_download(url, dest_path):
            dest_path.write_bytes(b"%PDF-fake")

        def fake_invoke(prompt, *, tools, allowed_tools, workdir):
            target = self.archive_dir / "01-example-com.md"
            target.write_text("---\ntitle: t\n---\n论文正文", encoding="utf-8")
            # 此时原始 pdf 应该还在（还没被 archive_item 清理）
            self.assertTrue(pdf_path.exists())

        pdf_item = {**self.item, "url": "https://example.com/paper.pdf"}
        with patch.object(morning_archive, "_detect_kind", return_value="pdf"), patch.object(
            morning_archive, "_download_pdf", side_effect=fake_download
        ), patch.object(morning_archive, "_invoke_haiku", side_effect=fake_invoke):
            entry = morning_archive.archive_item(1, pdf_item, self.archive_dir, self.state_dir, self.workdir)

        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["kind"], "pdf")
        self.assertFalse(pdf_path.exists())  # 转写完成后原始 PDF 被清理，不留二进制垃圾

    def test_pdf_download_failure_still_cleans_up_and_records_error(self):
        pdf_item = {**self.item, "url": "https://example.com/paper.pdf"}
        with patch.object(morning_archive, "_detect_kind", return_value="pdf"), patch.object(
            morning_archive, "_download_pdf", side_effect=morning_archive.ArchiveError("PDF下载失败：boom")
        ):
            entry = morning_archive.archive_item(1, pdf_item, self.archive_dir, self.state_dir, self.workdir)
        self.assertEqual(entry["status"], "failed")
        self.assertIn("PDF下载失败", entry["error"])
        self.assertFalse((self.archive_dir / "01-example-com.pdf").exists())


class TweetArchiveTests(unittest.TestCase):
    """X 单推分支：不派 haiku、不联网，直接查第1段素材文件里已经抓到的
    正文。全部用真实数据（真实 scout X 条目 + 真实 material x_syndication
    条目），覆盖监理指出的核心场景——两处 URL 字符串不一样（作者名段 vs
    `/i/` 段），必须按数字推文 ID 匹配才能联结上。"""

    def test_is_single_tweet_url_true_for_real_x_item_false_for_others(self):
        self.assertTrue(morning_archive.is_single_tweet_url(REAL_X_ITEM["url"]))
        self.assertFalse(morning_archive.is_single_tweet_url(REAL_HTML_ITEM["url"]))
        self.assertFalse(morning_archive.is_single_tweet_url("https://x.com/someone"))  # 主页链接，非单推

    def test_tweet_id_matches_despite_different_url_shape(self):
        scout_id = morning_archive._tweet_id_from_url(REAL_X_ITEM["url"])
        material_item = next(
            i for i in REAL_X_SYNDICATION_ITEMS if i["title"].startswith("Jon Matzner")
        )
        material_id = morning_archive._tweet_id_from_url(material_item["url"])
        self.assertIsNotNone(scout_id)
        self.assertEqual(scout_id, material_id)
        self.assertNotEqual(REAL_X_ITEM["url"], material_item["url"])  # 字符串本身不相等

    def test_load_x_syndication_items_reads_real_material_file(self):
        items = morning_archive._load_x_syndication_items(REAL_MATERIAL_PATH)
        self.assertEqual(items, REAL_X_SYNDICATION_ITEMS)
        self.assertGreaterEqual(len(items), 1)

    def test_load_x_syndication_items_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tempdir:
            missing = Path(tempdir) / "material-2026-08-21.json"
            self.assertEqual(morning_archive._load_x_syndication_items(missing), [])

    def test_load_x_syndication_items_malformed_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "material-2026-08-21.json"
            path.write_text("{not valid json", encoding="utf-8")
            self.assertEqual(morning_archive._load_x_syndication_items(path), [])

    def test_find_tweet_material_matches_real_item_by_id_not_url_string(self):
        found = morning_archive._find_tweet_material(REAL_X_ITEM["url"], REAL_X_SYNDICATION_ITEMS)
        self.assertIsNotNone(found)
        self.assertTrue(found["title"].startswith("Jon Matzner"))
        self.assertIn("Claude", found["summary"])

    def test_find_tweet_material_returns_none_when_id_not_present(self):
        other_tweet_url = "https://x.com/nobody/status/9999999999999999999"
        self.assertIsNone(morning_archive._find_tweet_material(other_tweet_url, REAL_X_SYNDICATION_ITEMS))

    def test_write_tweet_archive_contains_header_author_and_verbatim_text(self):
        material_item = next(
            i for i in REAL_X_SYNDICATION_ITEMS if i["title"].startswith("Jon Matzner")
        )
        with tempfile.TemporaryDirectory() as tempdir:
            target = Path(tempdir) / "02-x-com.md"
            morning_archive._write_tweet_archive(REAL_X_ITEM, material_item, target)
            content = target.read_text(encoding="utf-8")
        self.assertIn(f"title: {REAL_X_ITEM['title']}", content)
        self.assertIn(f"url: {REAL_X_ITEM['url']}", content)
        self.assertIn("Jon Matzner", content)
        self.assertIn(material_item["summary"], content)  # 原文逐字出现，没有被改写
        self.assertNotIn("相关链接", content)  # 没 links 就不加尾巴

    def test_write_tweet_archive_lists_links_without_fetching(self):
        # 档案馆只给来源不抓正文（2026-09-08 用户拍板）：链接列在推文档案末尾
        material_item = next(
            i for i in REAL_X_SYNDICATION_ITEMS if i["title"].startswith("Jon Matzner")
        )
        item = {**REAL_X_ITEM, "links": [morning_paper.describe_link("https://arxiv.org/abs/2608.29530", "论文 arXiv 摘要页")]}
        with tempfile.TemporaryDirectory() as tempdir:
            target = Path(tempdir) / "02-x-com.md"
            morning_archive._write_tweet_archive(item, material_item, target)
            content = target.read_text(encoding="utf-8")
        self.assertIn("想看自己派 subagent 去抓", content)
        self.assertIn("论文 arXiv 摘要页：https://arxiv.org/abs/2608.29530（arXiv 摘要页·网页·短，可直接读）", content)
        self.assertIn("全文 PDF：https://arxiv.org/pdf/2608.29530（PDF·很耗 token，别自己开，派 haiku 跑腿）", content)

    def test_archive_item_uses_x_branch_and_never_calls_haiku(self):
        with tempfile.TemporaryDirectory() as tempdir:
            state_dir = Path(tempdir)
            archive_dir = state_dir / "archive" / "2026-08-21"
            archive_dir.mkdir(parents=True)
            workdir = state_dir / "workdir"
            workdir.mkdir()
            # 把真实素材文件复制进这个隔离的 state_dir，模拟当天素材已经落盘
            (state_dir / "material-2026-08-21.json").write_text(
                json.dumps(REAL_MATERIAL_DOC, ensure_ascii=False), encoding="utf-8"
            )

            with patch.object(morning_archive, "_invoke_haiku") as invoke, patch.object(
                morning_archive, "_detect_kind"
            ) as detect:
                entry = morning_archive.archive_item(2, REAL_X_ITEM, archive_dir, state_dir, workdir)

            invoke.assert_not_called()  # 核心断言：X 分支完全不碰 haiku
            detect.assert_not_called()  # 也不用先判断 html/pdf
            self.assertEqual(entry["status"], "ok")
            self.assertEqual(entry["kind"], "x_tweet")
            self.assertTrue(entry["path"].endswith(".md"))
            target = state_dir / "archive" / "2026-08-21" / Path(entry["path"]).name
            self.assertIn("Claude morally lecturing", target.read_text(encoding="utf-8"))

    def test_archive_item_x_branch_fails_gracefully_when_material_missing(self):
        with tempfile.TemporaryDirectory() as tempdir:
            state_dir = Path(tempdir)
            archive_dir = state_dir / "archive" / "2026-08-21"
            archive_dir.mkdir(parents=True)
            workdir = state_dir / "workdir"
            workdir.mkdir()
            # 故意不放 material 文件——模拟第1段那天没抓到这条推文

            with patch.object(morning_archive, "_invoke_haiku") as invoke:
                entry = morning_archive.archive_item(2, REAL_X_ITEM, archive_dir, state_dir, workdir)

            invoke.assert_not_called()
            self.assertEqual(entry["status"], "failed")
            self.assertEqual(entry["kind"], "x_tweet")
            self.assertIn("没有找到对应的 X 单推正文", entry["error"])


class CleanupOldArchivesTests(unittest.TestCase):
    def test_removes_other_dates_keeps_today(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "archive"
            (root / "2026-08-19").mkdir(parents=True)
            (root / "2026-08-20").mkdir(parents=True)
            (root / "2026-08-21").mkdir(parents=True)
            (root / "2026-08-21" / "manifest.json").write_text("{}", encoding="utf-8")
            (root / "not-a-dir.txt").write_text("x", encoding="utf-8")

            morning_archive.cleanup_old_archives(root, "2026-08-21")

            remaining = sorted(p.name for p in root.iterdir())
            self.assertEqual(remaining, ["2026-08-21", "not-a-dir.txt"])
            self.assertTrue((root / "2026-08-21" / "manifest.json").exists())

    def test_missing_root_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "does-not-exist"
            morning_archive.cleanup_old_archives(root, "2026-08-21")  # 不应抛异常


class BuildArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tempdir.name)
        self.workdir = self.state_dir / "workdir"
        self.workdir.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def _write_scout(self, items: list[dict]) -> Path:
        scout_path = self.state_dir / "scout-2026-08-21.json"
        scout_path.write_text(
            json.dumps({"issue_date": "2026-08-21", "items": items}, ensure_ascii=False), encoding="utf-8"
        )
        return scout_path

    def test_missing_scout_returns_empty_manifest_without_crashing(self):
        scout_path = self.state_dir / "scout-2026-08-21.json"  # 不存在
        manifest = morning_archive.build_archive(scout_path, self.state_dir, "2026-08-21", [], workdir=self.workdir)
        self.assertEqual(manifest["items"], [])
        self.assertIn("缺席", manifest["note"])

    def test_archives_every_final_item_and_writes_manifest(self):
        scout_path = self._write_scout([REAL_ITEMS[0], REAL_ITEMS[2]])

        def fake_archive_item(index, item, archive_dir, state_dir, workdir):
            return {
                "title_key": morning_paper._title_key(item["title"]),
                "url_hash": morning_paper._fingerprint(item["url"]),
                "path": f".morning_paper/archive/2026-08-21/{index:02d}-fake.md",
                "status": "ok",
                "kind": "html",
                "error": None,
            }

        with patch.object(morning_archive, "archive_item", side_effect=fake_archive_item):
            manifest = morning_archive.build_archive(
                scout_path, self.state_dir, "2026-08-21", [], workdir=self.workdir
            )

        self.assertEqual(len(manifest["items"]), 2)
        self.assertTrue(all(entry["status"] == "ok" for entry in manifest["items"]))
        manifest_path = self.state_dir / "archive" / "2026-08-21" / "manifest.json"
        self.assertTrue(manifest_path.exists())
        on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["items"], manifest["items"])

    def test_partial_failure_keeps_successful_entries(self):
        scout_path = self._write_scout([REAL_ITEMS[0], REAL_ITEMS[2]])
        call_count = {"n": 0}

        def fake_archive_item(index, item, archive_dir, state_dir, workdir):
            call_count["n"] += 1
            if index == 1:
                return {
                    "title_key": morning_paper._title_key(item["title"]),
                    "url_hash": morning_paper._fingerprint(item["url"]),
                    "path": None,
                    "status": "failed",
                    "kind": "html",
                    "error": "haiku 超时（120s）",
                }
            return {
                "title_key": morning_paper._title_key(item["title"]),
                "url_hash": morning_paper._fingerprint(item["url"]),
                "path": f".morning_paper/archive/2026-08-21/{index:02d}-fake.md",
                "status": "ok",
                "kind": "html",
                "error": None,
            }

        with patch.object(morning_archive, "archive_item", side_effect=fake_archive_item):
            manifest = morning_archive.build_archive(
                scout_path, self.state_dir, "2026-08-21", [], workdir=self.workdir
            )

        self.assertEqual(call_count["n"], 2)  # 第一条失败不影响第二条继续跑
        statuses = [entry["status"] for entry in manifest["items"]]
        self.assertEqual(statuses, ["failed", "ok"])

    def test_cleans_up_old_dated_dirs_before_writing_today(self):
        old_dir = self.state_dir / "archive" / "2026-08-19"
        old_dir.mkdir(parents=True)
        (old_dir / "leftover.md").write_text("x", encoding="utf-8")

        scout_path = self._write_scout([REAL_ITEMS[0]])
        with patch.object(
            morning_archive,
            "archive_item",
            return_value={
                "title_key": "k",
                "url_hash": "h",
                "path": ".morning_paper/archive/2026-08-21/01-fake.md",
                "status": "ok",
                "kind": "html",
                "error": None,
            },
        ):
            morning_archive.build_archive(scout_path, self.state_dir, "2026-08-21", [], workdir=self.workdir)

        self.assertFalse(old_dir.exists())
        self.assertTrue((self.state_dir / "archive" / "2026-08-21").exists())

    def test_seen_hit_item_is_excluded_from_archiving(self):
        # 复用 morning_paper._load_and_validate_scout 的去重逻辑：seen 命中
        # 的条目连带不进档案馆的名单，跟晨报本体最终不会投递它保持一致。
        seen_key = morning_paper._title_key(REAL_ITEMS[0]["title"])
        scout_path = self._write_scout([REAL_ITEMS[0], REAL_ITEMS[2]])
        seen = [{"url_hash": "irrelevant", "title_key": seen_key}]

        calls = []

        def fake_archive_item(index, item, archive_dir, state_dir, workdir):
            calls.append(item["title"])
            return {
                "title_key": morning_paper._title_key(item["title"]),
                "url_hash": morning_paper._fingerprint(item["url"]),
                "path": f".morning_paper/archive/2026-08-21/{index:02d}-fake.md",
                "status": "ok",
                "kind": "html",
                "error": None,
            }

        with patch.object(morning_archive, "archive_item", side_effect=fake_archive_item):
            manifest = morning_archive.build_archive(
                scout_path, self.state_dir, "2026-08-21", seen, workdir=self.workdir
            )

        self.assertEqual(len(manifest["items"]), 1)
        self.assertNotIn(REAL_ITEMS[0]["title"], calls)
        self.assertIn(REAL_ITEMS[2]["title"], calls)


class RunTests(unittest.TestCase):
    def test_run_uses_given_now_and_state_dir_and_returns_manifest(self):
        with tempfile.TemporaryDirectory() as tempdir:
            state_dir = Path(tempdir)
            with patch.object(morning_archive.scout_sources, "_seen_title_keys", return_value=[]):
                manifest = morning_archive.run(now=NOW, state_dir=state_dir)
        # 没有 scout 草稿文件，应该走"缺席"分支而不是抛异常
        self.assertEqual(manifest["items"], [])
        self.assertEqual(manifest["issue_date"], "2026-08-21")


if __name__ == "__main__":
    unittest.main()
