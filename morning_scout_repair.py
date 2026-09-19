#!/usr/bin/env python3
"""晨报二版·草稿体检（cron 侧第 2 段之后、第 3 段档案馆之前）。

冲浪班（无头 sonnet）写出来的 `scout-<业务日期>.json` 偶尔不是合法 JSON——
9/11、9/19 两次都是 `digest` 里引用原话用了半角直引号没转义，daemon 静默
吞掉解析异常、当天晨报缺席，直到有人手动改文件。本脚本在草稿刚落盘时就
用 `morning_paper.parse_scout_text` 读一遍：

- 合法：什么都不做，退出码 0。
- 不合法但修得回来：原文件改名留底（`<草稿名>.bak-badjson-<YYYYMMDD>`，
  跟 9/11、9/19 两次手工留底同一个命名），把修复后的合法 JSON 原子写回
  原路径，日志里大声记一笔，退出码 0——后面的档案馆与 daemon 读到的就
  是干净文件。
- 修不回来：不动文件，退出码 2，由外壳脚本记日志并按失败退出（跟修复层
  上线前一样，报纸缺席、草稿原样留给人工修）。

修复规则只覆盖裸引号/围栏/控制符这几种已知手误，且修完必须过严格
`json.loads`；更离谱的格式错误照旧报错，不会被吞掉（9/11 拍板"怕掩盖更
严重格式错误"的顾虑就靠这条兜住）。想知道冲浪班又写坏了几次，
`grep 自动修复 morning_scout.log` 就是账本。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import morning_paper

EXIT_OK = 0
EXIT_UNREPAIRABLE = 2


def backup_path_for(scout_path: Path, today: datetime) -> Path:
    """`scout-X.json.bak-badjson-<YYYYMMDD>`；同一天已经有一份就加序号，
    绝不覆盖旧留底。"""
    stamp = today.strftime("%Y%m%d")
    candidate = scout_path.with_name(f"{scout_path.name}.bak-badjson-{stamp}")
    counter = 2
    while candidate.exists():
        candidate = scout_path.with_name(f"{scout_path.name}.bak-badjson-{stamp}-{counter}")
        counter += 1
    return candidate


def check_and_repair(scout_path: Path, today: datetime | None = None) -> int:
    today = today or datetime.now()
    if not scout_path.exists():
        logging.error("晨报草稿体检：文件不存在 %s", scout_path)
        return EXIT_UNREPAIRABLE
    text = scout_path.read_text(encoding="utf-8")
    try:
        parsed, repaired = morning_paper.parse_scout_text(text)
    except json.JSONDecodeError as exc:
        logging.error(
            "晨报草稿体检：%s 不是合法 JSON 且自动修复失败（%s，第 %d 行第 %d 列），原样保留待人工修",
            scout_path.name, exc.msg, exc.lineno, exc.colno,
        )
        return EXIT_UNREPAIRABLE
    if not repaired:
        logging.info("晨报草稿体检：%s 是合法 JSON", scout_path.name)
        return EXIT_OK
    if not isinstance(parsed, dict):
        logging.error("晨报草稿体检：%s 修复后顶层不是对象，原样保留待人工修", scout_path.name)
        return EXIT_UNREPAIRABLE
    backup = backup_path_for(scout_path, today)
    original_mode = scout_path.stat().st_mode & 0o777
    scout_path.rename(backup)
    morning_paper._write_state(parsed, scout_path)
    # _write_state 是给 state.json 用的（0600）；草稿沿用冲浪班 Write 工具
    # 落盘时的权限位，别因为修一次引号就悄悄改了文件模式。
    scout_path.chmod(original_mode)
    logging.warning(
        "晨报草稿体检：%s 不是合法 JSON，已自动修复（裸引号补转义）并写回，原文留底 %s",
        scout_path.name, backup.name,
    )
    return EXIT_OK


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法：morning_scout_repair.py <scout-<业务日期>.json>", file=sys.stderr)
        return EXIT_UNREPAIRABLE
    return check_and_repair(Path(argv[1]))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main(sys.argv))
