"""晨报草稿裸引号自动修复（morning_paper.parse_scout_text + morning_scout_repair）。

fixture 摘自 2026-09-11 冲浪班真实写坏的草稿（`.morning_paper/scout-2026-09-11
.json.bak-badjson-20260911` 第一条）：digest 里 `"没有",Thom` 这种"裸引号后面
紧跟半角逗号"的写法是最刁钻的一种——单看引号后面一个字符会把它误判成字
符串结尾。9/19 那次是同款（`从"打不通"变成"几小时破防"`）。
"""

import json
import logging
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import morning_paper
import morning_scout_repair

# 真实坏档第一条，一字未改（含 4 处裸引号，其中一处后面紧跟半角逗号）。
BAD_ITEM_TEXT = '''    {
      "section": "AI圈今日份",
      "title": "数学家力证:OpenAI的证明可能抄了我的聊天记录",
      "digest": "9月初,OpenAI高调宣布新模型Astra解出了10道菲尔兹奖级数学难题,其中一道跟格罗莫夫的非可分性猜想相关。德累斯顿工业大学数学家Andreas Thom看完证明后发现眼熟——证明的核心技术步骤,直接踩在他和合作者Gábor Kun 2019年论文、以及Kun 2016年一篇论文的肩膀上。更蹊跷的是,他此前正好在ChatGPT里和AI逐步推演过同一个expander matching问题的扩展思路。他去信OpenAI研究员追问:我的对话记录有没有进训练集、模型解题时有没有调用过这些对话?对方只回了一句"没有",Thom在9月10日的长文里说,这个回答"武断得可疑,现在回头看,甚至像是在说谎"。OpenAI后来悄悄改写了论文致谢部分,承认借鉴了Thom和Kun的既有工作,但Thom还没拿到实锤,只是要求OpenAI公开否认的依据。",
      "url": "https://www.science.org/content/article/how-ai-math-breakthrough-ignited-controversy",
      "url_warning": "长文",
      "source": "Science.org",
      "links": [
        {"url": "https://thomas.math.tu-dresden.de/blog", "note": "Thom 的\"原文\"博客"}
      ]
    }'''
BAD_DOC_TEXT = '{\n  "issue_date": "2026-09-11",\n  "items": [\n' + BAD_ITEM_TEXT + '\n  ]\n}\n'
EXPECTED_DIGEST_TAIL = '对方只回了一句"没有",Thom在9月10日的长文里说,这个回答"武断得可疑,现在回头看,甚至像是在说谎"。'


class ParseScoutTextTests(unittest.TestCase):

    def test_valid_json_takes_strict_path_untouched(self):
        doc = {"issue_date": "2026-09-11", "items": [{"digest": 'a "b" c'}]}
        parsed, repaired = morning_paper.parse_scout_text(json.dumps(doc, ensure_ascii=False))
        self.assertFalse(repaired)
        self.assertEqual(parsed, doc)

    def test_real_bad_draft_is_repaired_faithfully(self):
        with self.assertRaises(json.JSONDecodeError):
            json.loads(BAD_DOC_TEXT)
        parsed, repaired = morning_paper.parse_scout_text(BAD_DOC_TEXT)
        self.assertTrue(repaired)
        item = parsed["items"][0]
        self.assertIn(EXPECTED_DIGEST_TAIL, item["digest"])
        self.assertEqual(item["title"], "数学家力证:OpenAI的证明可能抄了我的聊天记录")
        self.assertEqual(item["links"][0]["note"], 'Thom 的"原文"博客')
        self.assertEqual(item["source"], "Science.org")

    def test_repaired_item_passes_schema_validation(self):
        parsed, _ = morning_paper.parse_scout_text(BAD_DOC_TEXT)
        item = morning_paper._validate_item(parsed["items"][0], seen_keys=set())
        self.assertIsNotNone(item)
        self.assertIn('"没有"', item["digest"])

    def test_stray_quote_followed_by_comma_and_quote_is_not_a_terminator(self):
        text = '{"items":[{"digest":"他说"好","坏"","source":"x"}]}'
        parsed, repaired = morning_paper.parse_scout_text(text)
        self.assertTrue(repaired)
        self.assertEqual(parsed["items"][0]["digest"], '他说"好","坏"')
        self.assertEqual(parsed["items"][0]["source"], "x")

    def test_code_fence_and_raw_newline_are_tolerated(self):
        text = '```json\n{"issue_date":"2026-09-11","items":[{"digest":"第一行\n第二行"}]}\n```\n'
        parsed, repaired = morning_paper.parse_scout_text(text)
        self.assertTrue(repaired)
        self.assertEqual(parsed["items"][0]["digest"], "第一行\n第二行")

    def test_already_escaped_quotes_survive_repair_path(self):
        # 同一份文件里既有转义好的 \" 又有裸引号：修复不能把已转义的再转一次。
        text = '{"items":[{"digest":"前面\\"好的\\"，后面"裸的"。","source":"x"}]}'
        parsed, repaired = morning_paper.parse_scout_text(text)
        self.assertTrue(repaired)
        self.assertEqual(parsed["items"][0]["digest"], '前面"好的"，后面"裸的"。')

    def test_garbage_still_raises_strict_error(self):
        with self.assertRaises(json.JSONDecodeError):
            morning_paper.parse_scout_text("{not valid json")
        with self.assertRaises(json.JSONDecodeError):
            morning_paper.parse_scout_text("完全不是 JSON 的一段话")

    def test_unregistered_new_key_after_stray_quote_is_not_swallowed_into_digest(self):
        # 键只认形状不认名单：将来契约新加字段不用登记，也不会被裸引号修复
        # 吞进上一个字段里。
        text = '{"items":[{"digest":"x"y","brand_new_key":"z"}]}'
        parsed, repaired = morning_paper.parse_scout_text(text)
        self.assertTrue(repaired)
        self.assertEqual(parsed["items"][0], {"digest": 'x"y', "brand_new_key": "z"})

    def test_body_text_shaped_like_a_key_fails_loudly_not_silently(self):
        # 正文里碰巧出现 `,"xx":`（半角逗号+引号+半角冒号）会被误判成下一个
        # 键，这种极端情况修不回来——但必须是报错，不是把错的结构当对的。
        text = '{"items":[{"digest":"他说"好","算了":我走了","source":"x"}]}'
        with self.assertRaises(json.JSONDecodeError):
            morning_paper.parse_scout_text(text)


class LoadScoutRepairLoggingTests(unittest.TestCase):

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tempdir.name)
        self.scout_path = self.state_dir / "scout-2026-09-11.json"
        self.manifest_path = self.state_dir / "archive" / "2026-09-11" / "manifest.json"

    def tearDown(self):
        self._tempdir.cleanup()

    def test_bad_draft_is_delivered_with_warning_logged(self):
        self.scout_path.write_text(BAD_DOC_TEXT, encoding="utf-8")
        with self.assertLogs(level=logging.WARNING) as captured:
            items = morning_paper._load_and_validate_scout(self.scout_path, [], self.manifest_path)
        self.assertEqual(len(items), 1)
        self.assertIn(EXPECTED_DIGEST_TAIL, items[0]["digest"])
        self.assertTrue(any("自动修复" in line for line in captured.output))

    def test_valid_draft_logs_nothing(self):
        doc = {"issue_date": "2026-09-11", "items": [json.loads(json.dumps(
            morning_paper.parse_scout_text(BAD_DOC_TEXT)[0]["items"][0]))]}
        self.scout_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        with self.assertNoLogs(level=logging.WARNING):
            items = morning_paper._load_and_validate_scout(self.scout_path, [], self.manifest_path)
        self.assertEqual(len(items), 1)


class RepairScriptTests(unittest.TestCase):

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tempdir.name)
        self.scout_path = self.state_dir / "scout-2026-09-11.json"
        self.today = datetime(2026, 9, 11, 5, 15)

    def tearDown(self):
        self._tempdir.cleanup()

    def test_valid_file_untouched_exit_zero(self):
        payload = json.dumps({"issue_date": "2026-09-11", "items": []}, ensure_ascii=False)
        self.scout_path.write_text(payload, encoding="utf-8")
        self.assertEqual(morning_scout_repair.check_and_repair(self.scout_path, self.today), 0)
        self.assertEqual(self.scout_path.read_text(encoding="utf-8"), payload)
        self.assertEqual(list(self.state_dir.iterdir()), [self.scout_path])

    def test_bad_file_is_backed_up_and_rewritten_valid(self):
        self.scout_path.write_text(BAD_DOC_TEXT, encoding="utf-8")
        self.scout_path.chmod(0o644)
        with self.assertLogs(level=logging.WARNING) as captured:
            status = morning_scout_repair.check_and_repair(self.scout_path, self.today)
        self.assertEqual(status, 0)
        backup = self.state_dir / "scout-2026-09-11.json.bak-badjson-20260911"
        self.assertEqual(backup.read_text(encoding="utf-8"), BAD_DOC_TEXT)
        rewritten = json.loads(self.scout_path.read_text(encoding="utf-8"))
        self.assertIn(EXPECTED_DIGEST_TAIL, rewritten["items"][0]["digest"])
        self.assertEqual(self.scout_path.stat().st_mode & 0o777, 0o644)
        self.assertTrue(any("自动修复" in line for line in captured.output))

    def test_second_backup_same_day_does_not_overwrite_first(self):
        first = self.state_dir / "scout-2026-09-11.json.bak-badjson-20260911"
        first.write_text("旧留底", encoding="utf-8")
        self.scout_path.write_text(BAD_DOC_TEXT, encoding="utf-8")
        self.assertEqual(morning_scout_repair.check_and_repair(self.scout_path, self.today), 0)
        self.assertEqual(first.read_text(encoding="utf-8"), "旧留底")
        second = self.state_dir / "scout-2026-09-11.json.bak-badjson-20260911-2"
        self.assertEqual(second.read_text(encoding="utf-8"), BAD_DOC_TEXT)

    def test_unrepairable_file_left_alone_exit_two(self):
        self.scout_path.write_text("{not valid json", encoding="utf-8")
        with self.assertLogs(level=logging.ERROR):
            status = morning_scout_repair.check_and_repair(self.scout_path, self.today)
        self.assertEqual(status, 2)
        self.assertEqual(self.scout_path.read_text(encoding="utf-8"), "{not valid json")
        self.assertEqual(list(self.state_dir.iterdir()), [self.scout_path])

    def test_missing_file_exit_two(self):
        with self.assertLogs(level=logging.ERROR):
            self.assertEqual(morning_scout_repair.check_and_repair(self.scout_path, self.today), 2)

    def test_main_requires_exactly_one_argument(self):
        self.assertEqual(morning_scout_repair.main(["prog"]), 2)


if __name__ == "__main__":
    unittest.main()
