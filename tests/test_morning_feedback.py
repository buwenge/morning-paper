import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import morning_feedback


TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 11, 10, 0, tzinfo=TZ)


def sample_issue(issue_date="2026-09-11"):
    return {
        "issue_date": issue_date,
        "items": [
            {"section": "AI圈今日份", "title": "第一条标题", "url": "https://a.example/1"},
            {"section": "人机恋小报", "title": "第二条标题", "url": "https://a.example/2"},
            {
                "section": "冷知识",
                "title": "第三条比较长的标题用来测试短标题截断显示效果超过二十四字符看看会不会被砍掉",
                "url": "https://a.example/3",
            },
        ],
    }


class TmpPathTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="xiaoyu-test-morning-feedback-case-")
        self.addCleanup(self._tmpdir.cleanup)
        self.path = Path(self._tmpdir.name) / "feedback.json"


class ParseRatingSpecTests(unittest.TestCase):
    def test_basic_tokens(self):
        spec = morning_feedback.parse_rating_spec("1:0 2:3追 5:2")
        self.assertEqual(
            spec,
            [
                {"n": 1, "score": 0, "follow": False},
                {"n": 2, "score": 3, "follow": True},
                {"n": 5, "score": 2, "follow": False},
            ],
        )

    def test_fullwidth_colon_digit_and_space(self):
        spec = morning_feedback.parse_rating_spec("１：３追　２：０")
        self.assertEqual(
            spec,
            [
                {"n": 1, "score": 3, "follow": True},
                {"n": 2, "score": 0, "follow": False},
            ],
        )

    def test_empty_text_rejected(self):
        with self.assertRaisesRegex(morning_feedback.FeedbackError, "没看出要打的分"):
            morning_feedback.parse_rating_spec("   ")

    def test_malformed_token_rejected(self):
        with self.assertRaisesRegex(morning_feedback.FeedbackError, "没看懂"):
            morning_feedback.parse_rating_spec("1:0 abc")

    def test_score_out_of_range_rejected(self):
        with self.assertRaisesRegex(morning_feedback.FeedbackError, "分数不对"):
            morning_feedback.parse_rating_spec("1:9")

    def test_zero_index_rejected(self):
        with self.assertRaisesRegex(morning_feedback.FeedbackError, "编号不对"):
            morning_feedback.parse_rating_spec("0:1")

    def test_one_bad_token_rejects_whole_spec(self):
        # 前面几节合法，最后一节坏了：整条都不应该返回，调用方也就没有
        # 机会半应用前面合法的部分。
        with self.assertRaises(morning_feedback.FeedbackError):
            morning_feedback.parse_rating_spec("1:0 2:1 9:9")


class LenientRatingSpecTests(unittest.TestCase):
    """2026-09-11 用户补充拍板：宽容语法——中文数字编号、"第N条"前缀、
    多种分隔符、"追/想追"后缀。"""

    def test_di_n_tiao_with_colon(self):
        self.assertEqual(
            morning_feedback.parse_rating_spec("第2条:3"),
            [{"n": 2, "score": 3, "follow": False}],
        )

    def test_di_n_tiao_with_fullwidth_colon(self):
        self.assertEqual(
            morning_feedback.parse_rating_spec("第2条：3"),
            [{"n": 2, "score": 3, "follow": False}],
        )

    def test_di_n_tiao_no_colon_score_glued(self):
        self.assertEqual(
            morning_feedback.parse_rating_spec("第2条3"),
            [{"n": 2, "score": 3, "follow": False}],
        )

    def test_chinese_numeral_index(self):
        self.assertEqual(
            morning_feedback.parse_rating_spec("第二条：3"),
            [{"n": 2, "score": 3, "follow": False}],
        )

    def test_chinese_numeral_score_paired_with_next_token(self):
        self.assertEqual(
            morning_feedback.parse_rating_spec("第二条 3"),
            [{"n": 2, "score": 3, "follow": False}],
        )

    def test_follow_suffix_xiang_zhui(self):
        self.assertEqual(
            morning_feedback.parse_rating_spec("第三条2想追"),
            [{"n": 3, "score": 2, "follow": True}],
        )
        self.assertEqual(
            morning_feedback.parse_rating_spec("第四条：1想追"),
            [{"n": 4, "score": 1, "follow": True}],
        )

    def test_dangling_item_follow_suffix_on_paired_score(self):
        self.assertEqual(
            morning_feedback.parse_rating_spec("第五条 2追"),
            [{"n": 5, "score": 2, "follow": True}],
        )

    def test_multiple_separators_and_mixed_forms(self):
        spec = morning_feedback.parse_rating_spec("第2条3，第三条 1、5:2；第1条0")
        self.assertEqual(
            spec,
            [
                {"n": 2, "score": 3, "follow": False},
                {"n": 3, "score": 1, "follow": False},
                {"n": 5, "score": 2, "follow": False},
                {"n": 1, "score": 0, "follow": False},
            ],
        )

    def test_dangling_item_without_following_score_rejected(self):
        with self.assertRaisesRegex(morning_feedback.FeedbackError, "没跟着看得懂的分数"):
            morning_feedback.parse_rating_spec("第二条")

    def test_dangling_item_followed_by_junk_rejected(self):
        with self.assertRaisesRegex(morning_feedback.FeedbackError, "没跟着看得懂的分数"):
            morning_feedback.parse_rating_spec("第二条 追加内容")

    def test_bare_ambiguous_number_rejected(self):
        # 裸数字串"11"——分不清是"第11条"还是"第1条打1分"，真歧义，不猜。
        with self.assertRaisesRegex(morning_feedback.FeedbackError, "分不清"):
            morning_feedback.parse_rating_spec("11")

    def test_lone_bare_digit_without_marker_also_rejected(self):
        # 单个裸数字同样歧义（不知道对应哪个编号），跟"11"同一条规则。
        with self.assertRaisesRegex(morning_feedback.FeedbackError, "分不清"):
            morning_feedback.parse_rating_spec("3")

    def test_invalid_chinese_numeral_combo_rejected(self):
        with self.assertRaisesRegex(morning_feedback.FeedbackError, "编号不对"):
            morning_feedback.parse_rating_spec("第十十条3")


class ApplyRatingsTests(TmpPathTestCase):
    def test_new_rating_written_and_receipt_text(self):
        issue = sample_issue()
        spec = morning_feedback.parse_rating_spec("1:2 2:3追")
        receipt = morning_feedback.apply_ratings(issue, spec, NOW, path=self.path)
        self.assertIn("1 《第一条标题》→ 2分（有意思）", receipt)
        self.assertIn("2 《第二条标题》→ 3分（很想深入）・追", receipt)
        self.assertIn("当前在追 1 条。", receipt)

        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["ratings"]), 2)
        rating_by_n = {r["n"]: r for r in saved["ratings"]}
        self.assertEqual(rating_by_n[1]["score"], 2)
        self.assertEqual(rating_by_n[1]["section"], "AI圈今日份")
        self.assertEqual(rating_by_n[2]["follow"], True)
        self.assertEqual(len(saved["follows"]), 1)
        self.assertEqual(saved["follows"][0]["id"], "f-0911-2")
        self.assertEqual(saved["follows"][0]["status"], "active")
        self.assertEqual(saved["follows"][0]["title"], "第二条标题")

    def test_repeated_rating_overwrites_old_value(self):
        issue = sample_issue()
        morning_feedback.apply_ratings(issue, morning_feedback.parse_rating_spec("1:0"), NOW, path=self.path)
        morning_feedback.apply_ratings(
            issue, morning_feedback.parse_rating_spec("1:3"), NOW + timedelta(minutes=5), path=self.path
        )
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["ratings"]), 1)
        self.assertEqual(saved["ratings"][0]["score"], 3)

    def test_out_of_range_index_rejects_whole_batch_atomically(self):
        issue = sample_issue()
        morning_feedback.apply_ratings(issue, morning_feedback.parse_rating_spec("1:1"), NOW, path=self.path)
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaisesRegex(morning_feedback.FeedbackError, "超出范围"):
            morning_feedback.apply_ratings(
                issue, morning_feedback.parse_rating_spec("1:2 9:0"), NOW, path=self.path
            )
        after = self.path.read_text(encoding="utf-8")
        self.assertEqual(before, after)  # 没有任何一条被半途写进去

    def test_empty_spec_rejected(self):
        with self.assertRaises(morning_feedback.FeedbackError):
            morning_feedback.apply_ratings(sample_issue(), [], NOW, path=self.path)

    def test_no_follow_flag_does_not_touch_existing_active_follow(self):
        issue = sample_issue()
        morning_feedback.apply_ratings(issue, morning_feedback.parse_rating_spec("2:3追"), NOW, path=self.path)
        morning_feedback.apply_ratings(
            issue, morning_feedback.parse_rating_spec("2:1"), NOW + timedelta(minutes=1), path=self.path
        )
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["follows"][0]["status"], "active")
        self.assertEqual(saved["ratings"][0]["score"], 1)


class SetFollowTests(TmpPathTestCase):
    def test_follow_by_index_creates_active_record(self):
        issue = sample_issue()
        receipt = morning_feedback.set_follow(issue, 3, True, NOW, path=self.path)
        self.assertIn("已标记追踪", receipt)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["follows"][0]["id"], "f-0911-3")
        self.assertEqual(saved["follows"][0]["status"], "active")

    def test_unfollow_by_index_cancels(self):
        issue = sample_issue()
        morning_feedback.set_follow(issue, 3, True, NOW, path=self.path)
        receipt = morning_feedback.set_follow(issue, 3, False, NOW + timedelta(hours=1), path=self.path)
        self.assertIn("已取消追踪", receipt)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["follows"][0]["status"], "cancelled")

    def test_unfollow_index_not_currently_followed_is_friendly_noop(self):
        issue = sample_issue()
        receipt = morning_feedback.set_follow(issue, 2, False, NOW, path=self.path)
        self.assertIn("本来就没在追", receipt)

    def test_follow_index_out_of_range_raises(self):
        with self.assertRaisesRegex(morning_feedback.FeedbackError, "超出范围"):
            morning_feedback.set_follow(sample_issue(), 99, True, NOW, path=self.path)

    def test_follow_by_index_without_issue_raises(self):
        with self.assertRaisesRegex(morning_feedback.FeedbackError, "没有当前这期晨报"):
            morning_feedback.set_follow(None, 1, True, NOW, path=self.path)

    def test_unfollow_by_fid_string(self):
        issue = sample_issue()
        morning_feedback.set_follow(issue, 1, True, NOW, path=self.path)
        receipt = morning_feedback.set_follow(None, "f-0911-1", False, NOW, path=self.path)
        self.assertIn("已取消追踪", receipt)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["follows"][0]["status"], "cancelled")

    def test_follow_by_unknown_fid_raises(self):
        with self.assertRaisesRegex(morning_feedback.FeedbackError, "没找到"):
            morning_feedback.set_follow(None, "f-0101-9", False, NOW, path=self.path)

    def test_revive_cancelled_follow_by_fid(self):
        issue = sample_issue()
        morning_feedback.set_follow(issue, 1, True, NOW, path=self.path)
        morning_feedback.set_follow(None, "f-0911-1", False, NOW, path=self.path)
        receipt = morning_feedback.set_follow(None, "f-0911-1", True, NOW + timedelta(hours=1), path=self.path)
        self.assertIn("已标记追踪", receipt)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["follows"][0]["status"], "active")
        self.assertEqual(saved["follows"][0]["resolved_at"], "")


class TopicTests(TmpPathTestCase):
    def test_add_and_status_reflects_it(self):
        morning_feedback.add_topic("prefer", "海洋冷知识", path=self.path)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["topics"]["prefer"], ["海洋冷知识"])

    def test_add_duplicate_is_idempotent(self):
        morning_feedback.add_topic("avoid", "官方财经新闻", path=self.path)
        receipt = morning_feedback.add_topic("avoid", "官方财经新闻", path=self.path)
        self.assertIn("已经在", receipt)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["topics"]["avoid"], ["官方财经新闻"])

    def test_add_too_long_phrase_rejected(self):
        with self.assertRaisesRegex(morning_feedback.FeedbackError, "太长了"):
            morning_feedback.add_topic("prefer", "字" * 21, path=self.path)

    def test_add_over_cap_rejected(self):
        for i in range(morning_feedback.TOPIC_MAX_ITEMS):
            morning_feedback.add_topic("prefer", f"话题{i}", path=self.path)
        with self.assertRaisesRegex(morning_feedback.FeedbackError, "已经有"):
            morning_feedback.add_topic("prefer", "多出来的话题", path=self.path)

    def test_remove_existing(self):
        morning_feedback.add_topic("prefer", "天文", path=self.path)
        receipt = morning_feedback.remove_topic("prefer", "天文", path=self.path)
        self.assertIn("已从", receipt)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["topics"]["prefer"], [])

    def test_remove_nonexistent_is_friendly_noop(self):
        receipt = morning_feedback.remove_topic("avoid", "不存在的话题", path=self.path)
        self.assertIn("本来就没有", receipt)

    def test_invalid_kind_rejected(self):
        with self.assertRaises(morning_feedback.FeedbackError):
            morning_feedback.add_topic("wrong", "x", path=self.path)


class StatusTextTests(TmpPathTestCase):
    def test_with_issue_lists_numbered_items_and_scores(self):
        issue = sample_issue()
        morning_feedback.apply_ratings(issue, morning_feedback.parse_rating_spec("2:3"), NOW, path=self.path)
        text = morning_feedback.status_text(issue, path=self.path)
        self.assertIn("今天（2026-09-11）的晨报：", text)
        self.assertIn("1 [AI圈今日份] 第一条标题", text)
        self.assertIn("2 [人机恋小报] 第二条标题（已打3分）", text)
        self.assertIn("目前没有在追的条目。", text)
        self.assertIn("想看：（还没设置）", text)
        self.assertIn("打分用法：", text)

    def test_without_issue_still_shows_follows_and_topics(self):
        morning_feedback.add_topic("avoid", "地缘政治", path=self.path)
        text = morning_feedback.status_text(None, path=self.path)
        self.assertIn("今天报纸还没来，晨报没开。", text)
        self.assertIn("不想看：地缘政治", text)

    def test_active_follow_listed(self):
        issue = sample_issue()
        morning_feedback.set_follow(issue, 1, True, NOW, path=self.path)
        text = morning_feedback.status_text(issue, path=self.path)
        self.assertIn("在追的：", text)
        self.assertIn("f-0911-1 第一条标题（2026-09-11）", text)


class LifecycleTests(TmpPathTestCase):
    def setUp(self):
        super().setUp()
        self._draftsdir = tempfile.TemporaryDirectory(prefix="xiaoyu-test-morning-feedback-drafts-")
        self.addCleanup(self._draftsdir.cleanup)
        self.drafts_dir = Path(self._draftsdir.name)

    def test_active_follow_older_than_7_days_expires(self):
        issue = sample_issue("2026-09-01")
        old_now = datetime(2026, 9, 1, 10, 0, tzinfo=TZ)
        morning_feedback.set_follow(issue, 1, True, old_now, path=self.path)
        morning_feedback.apply_lifecycle(date(2026, 9, 11), self.drafts_dir, path=self.path)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["follows"][0]["status"], "expired")

    def test_active_follow_within_7_days_stays_active(self):
        issue = sample_issue("2026-09-08")
        morning_feedback.set_follow(issue, 1, True, datetime(2026, 9, 8, 10, 0, tzinfo=TZ), path=self.path)
        morning_feedback.apply_lifecycle(date(2026, 9, 11), self.drafts_dir, path=self.path)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["follows"][0]["status"], "active")

    def test_follow_up_of_marks_done(self):
        issue = sample_issue("2026-09-10")
        morning_feedback.set_follow(issue, 3, True, datetime(2026, 9, 10, 10, 0, tzinfo=TZ), path=self.path)
        draft = {
            "issue_date": "2026-09-11",
            "items": [{"section": "AI圈今日份", "title": "后续来了", "follow_up_of": "f-0910-3"}],
        }
        (self.drafts_dir / "scout-2026-09-11.json").write_text(json.dumps(draft), encoding="utf-8")
        morning_feedback.apply_lifecycle(date(2026, 9, 11), self.drafts_dir, path=self.path)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["follows"][0]["status"], "done")

    def test_corrupt_draft_file_is_skipped_not_raised(self):
        issue = sample_issue("2026-09-10")
        morning_feedback.set_follow(issue, 1, True, datetime(2026, 9, 10, 10, 0, tzinfo=TZ), path=self.path)
        (self.drafts_dir / "scout-2026-09-11.json").write_text("{not json", encoding="utf-8")
        morning_feedback.apply_lifecycle(date(2026, 9, 11), self.drafts_dir, path=self.path)  # 不炸
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["follows"][0]["status"], "active")

    def test_missing_drafts_dir_does_not_raise(self):
        issue = sample_issue("2026-09-10")
        morning_feedback.set_follow(issue, 1, True, datetime(2026, 9, 10, 10, 0, tzinfo=TZ), path=self.path)
        missing_dir = self.drafts_dir / "does-not-exist"
        morning_feedback.apply_lifecycle(date(2026, 9, 11), missing_dir, path=self.path)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["follows"][0]["status"], "active")


class BuildMaterialFeedbackTests(TmpPathTestCase):
    def test_no_feedback_returns_none(self):
        self.assertIsNone(morning_feedback.build_material_feedback(date(2026, 9, 11), path=self.path))

    def test_follow_requests_trimmed_to_latest_5_and_topics_pass_through(self):
        # 7 条追踪按创建顺序落盘：f-0801-1 / f-0802-2 / f-0803-3 / f-0909-1 /
        # f-0909-2 / f-0910-1 / f-0910-2；只应保留最后创建的 5 条。
        for day, n in (("2026-08-01", 1), ("2026-08-02", 2), ("2026-08-03", 3)):
            morning_feedback.set_follow(sample_issue(day), n, True, datetime(2026, 8, 1, tzinfo=TZ), path=self.path)
        for n in (1, 2):
            morning_feedback.set_follow(
                sample_issue("2026-09-09"), n, True, datetime(2026, 9, 9, tzinfo=TZ), path=self.path
            )
        for n in (1, 2):
            morning_feedback.set_follow(
                sample_issue("2026-09-10"), n, True, datetime(2026, 9, 10, tzinfo=TZ), path=self.path
            )

        morning_feedback.add_topic("avoid", "官方通稿", path=self.path)
        morning_feedback.add_topic("prefer", "海洋生物", path=self.path)

        block = morning_feedback.build_material_feedback(date(2026, 9, 11), path=self.path)
        self.assertIsNotNone(block)
        ids = [f["id"] for f in block["follow_requests"]]
        self.assertEqual(len(ids), morning_feedback.FOLLOW_REQUESTS_LIMIT)
        self.assertNotIn("f-0801-1", ids)  # 最早两条被裁掉
        self.assertNotIn("f-0802-2", ids)
        self.assertIn("f-0910-1", ids)  # 最新的留着
        self.assertIn("f-0910-2", ids)
        self.assertEqual(block["avoid"]["topics"], ["官方通稿"])
        self.assertEqual(block["prefer"]["topics"], ["海洋生物"])

    def test_recent_zero_and_recent_high_trimmed_to_15(self):
        zero_issue = {
            "issue_date": "2026-09-01",
            "items": [{"section": "冷知识", "title": f"零分条目{i}", "url": f"https://x/zero/{i}"} for i in range(20)],
        }
        for i in range(20):
            morning_feedback.apply_ratings(
                zero_issue, [{"n": i + 1, "score": 0, "follow": False}], datetime(2026, 9, 1, tzinfo=TZ), path=self.path
            )
        high_issue = {
            "issue_date": "2026-09-02",
            "items": [{"section": "AI圈今日份", "title": f"高分条目{i}", "url": f"https://x/high/{i}"} for i in range(20)],
        }
        for i in range(20):
            morning_feedback.apply_ratings(
                high_issue, [{"n": i + 1, "score": 3, "follow": False}], datetime(2026, 9, 2, tzinfo=TZ), path=self.path
            )

        block = morning_feedback.build_material_feedback(date(2026, 9, 11), path=self.path)
        self.assertIsNotNone(block)
        self.assertEqual(len(block["avoid"]["recent_zero"]), morning_feedback.RECENT_ZERO_LIMIT)
        self.assertEqual(len(block["prefer"]["recent_high"]), morning_feedback.RECENT_HIGH_LIMIT)
        self.assertNotIn("follow_requests", block)
        self.assertNotIn("topics", block.get("avoid", {}))

    def test_old_ratings_outside_window_excluded(self):
        old_issue = sample_issue("2026-01-01")
        morning_feedback.apply_ratings(
            old_issue, [{"n": 1, "score": 0, "follow": False}], datetime(2026, 1, 1, tzinfo=TZ), path=self.path
        )
        block = morning_feedback.build_material_feedback(date(2026, 9, 11), path=self.path)
        self.assertIsNone(block)

    def test_corrupt_file_returns_none_without_raising(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        result = morning_feedback.build_material_feedback(date(2026, 9, 11), path=self.path)
        self.assertIsNone(result)


class FrontendSummaryTests(TmpPathTestCase):
    def test_no_feedback_returns_empty_lists(self):
        summary = morning_feedback.frontend_summary(date(2026, 9, 11), path=self.path)
        self.assertEqual(summary, {"following": [], "not_interested": []})

    def test_active_follow_listed_newest_first(self):
        morning_feedback.set_follow(sample_issue("2026-09-10"), 1, True, datetime(2026, 9, 10, tzinfo=TZ), path=self.path)
        morning_feedback.set_follow(sample_issue("2026-09-11"), 2, True, NOW, path=self.path)
        summary = morning_feedback.frontend_summary(date(2026, 9, 11), path=self.path)
        self.assertEqual([f["id"] for f in summary["following"]], ["f-0911-2", "f-0910-1"])
        self.assertEqual(summary["following"][0]["title"], "第二条标题")
        self.assertEqual(summary["following"][0]["issue_date"], "2026-09-11")

    def test_cancelled_and_done_follows_excluded(self):
        morning_feedback.set_follow(sample_issue("2026-09-11"), 1, True, NOW, path=self.path)
        morning_feedback.set_follow(None, "f-0911-1", False, NOW, path=self.path)  # 取消
        summary = morning_feedback.frontend_summary(date(2026, 9, 11), path=self.path)
        self.assertEqual(summary["following"], [])

    def test_zero_score_rating_listed_as_not_interested(self):
        issue = sample_issue()
        morning_feedback.apply_ratings(
            issue, [{"n": 3, "score": 0, "follow": False}, {"n": 1, "score": 3, "follow": False}], NOW, path=self.path
        )
        summary = morning_feedback.frontend_summary(date(2026, 9, 11), path=self.path)
        self.assertEqual(len(summary["not_interested"]), 1)
        entry = summary["not_interested"][0]
        self.assertEqual(entry["section"], "冷知识")
        self.assertEqual(entry["issue_date"], "2026-09-11")

    def test_old_zero_rating_outside_window_excluded(self):
        old_issue = sample_issue("2026-01-01")
        morning_feedback.apply_ratings(
            old_issue, [{"n": 1, "score": 0, "follow": False}], datetime(2026, 1, 1, tzinfo=TZ), path=self.path
        )
        summary = morning_feedback.frontend_summary(date(2026, 9, 11), path=self.path)
        self.assertEqual(summary["not_interested"], [])


class CorruptFileTests(TmpPathTestCase):
    def test_apply_ratings_refuses_to_overwrite_corrupt_file(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaisesRegex(morning_feedback.FeedbackError, "损坏|拒绝覆盖"):
            morning_feedback.apply_ratings(
                sample_issue(), morning_feedback.parse_rating_spec("1:1"), NOW, path=self.path
            )
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_status_text_raises_on_corrupt_file(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("[]", encoding="utf-8")  # 合法 JSON 但不是期望的 dict 形状
        with self.assertRaises(morning_feedback.FeedbackError):
            morning_feedback.status_text(sample_issue(), path=self.path)

    def test_frontend_summary_raises_on_corrupt_file(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("[]", encoding="utf-8")
        with self.assertRaises(morning_feedback.FeedbackError):
            morning_feedback.frontend_summary(date(2026, 9, 11), path=self.path)


class ClaimIntroTests(TmpPathTestCase):
    """S5：打分导语每 session 只出一次（薄壳委托 session_intro.claim）。"""

    def setUp(self):
        super().setUp()
        self.intro_path = self.path.parent / "feedback_intro_sessions.json"

    def test_first_claim_returns_intro_text(self):
        text = morning_feedback.claim_intro("session-a", path=self.intro_path)
        self.assertEqual(text, morning_feedback.RATING_INTRO_TEXT)
        self.assertIn("home 晨报 打分", text)

    def test_second_claim_same_session_returns_empty(self):
        morning_feedback.claim_intro("session-a", path=self.intro_path)
        self.assertEqual(morning_feedback.claim_intro("session-a", path=self.intro_path), "")

    def test_different_session_gets_its_own_claim(self):
        morning_feedback.claim_intro("session-a", path=self.intro_path)
        text = morning_feedback.claim_intro("session-b", path=self.intro_path)
        self.assertEqual(text, morning_feedback.RATING_INTRO_TEXT)

    def test_blank_or_missing_session_id_returns_empty(self):
        self.assertEqual(morning_feedback.claim_intro("", path=self.intro_path), "")
        self.assertEqual(morning_feedback.claim_intro(None, path=self.intro_path), "")

    def test_write_failure_falls_back_to_intro_text_not_raising(self):
        # path.parent 实际是个文件不是目录：mkdir 必然失败，容错路径应该
        # 宁可偶尔重复也不炸调用方（daemon.py 那边还包了一层 try/except，
        # 这里额外验证 claim_intro 自身也不会往外抛）。
        blocked = self.path.parent / "blocked-as-file"
        blocked.write_text("x", encoding="utf-8")
        bad_path = blocked / "feedback_intro_sessions.json"
        text = morning_feedback.claim_intro("session-a", path=bad_path)
        self.assertEqual(text, morning_feedback.RATING_INTRO_TEXT)


class WriteAtomicityTests(TmpPathTestCase):
    def test_written_file_has_restrictive_permissions(self):
        morning_feedback.add_topic("prefer", "地质奇观", path=self.path)
        mode = self.path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_no_leftover_temp_files(self):
        morning_feedback.add_topic("prefer", "微生物", path=self.path)
        leftovers = [p for p in self.path.parent.iterdir() if p.name.startswith(".feedback.")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
