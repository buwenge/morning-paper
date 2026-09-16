"""通用"同一个 session 只认领一次"逻辑：锁 + seen 列表 + 原子写 + 容错。

纯搬运自 `garden.py` 的 `claim_session_intro`（garden.py:2611 起）——晨报
打分导语（`morning_feedback.claim_intro`）要复用同一套去重/落盘逻辑，抽
成共用模块而不是再抄一份（2026-09-11 用户拍板）。**标准库 only**。

核心算法与原 `claim_session_intro` 保持行为一致：session_id 形状不对
（空/超长/含控制字符）当没有认领处理；写不进去（OSError）时宁可偶尔重
复也不能让调用方的主流程失败。锁的获取方式改成本模块自带的单一用途独
占锁（`garden._locked` 是给 `garden.json` 全部读改写共用的通用组件，服务
着十几处别的调用，没有一并搬出来）。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from pathlib import Path


def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(path)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def claim(session_id: str | None, *, path: Path, intro_text: str) -> str:
    """同一个 session 只认领一次；认领成功（或写不进去时的容错兜底）返回
    `intro_text`，已经认领过或 `session_id` 形状不对返回空字符串。"""
    session_id = str(session_id or "").strip()
    if not session_id or len(session_id) > 128 or any(ord(char) < 32 for char in session_id):
        return ""
    try:
        with _locked(path):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                payload = {}
            seen = payload.get("seen_session_ids")
            if not isinstance(seen, list) or any(not isinstance(item, str) for item in seen):
                seen = []
            if session_id in seen:
                return ""
            seen.append(session_id)
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", dir=path.parent, text=True,
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                    json.dump({"seen_session_ids": seen}, target, ensure_ascii=False, indent=2)
                    target.write("\n")
                os.replace(temp_name, path)
            except Exception:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise
    except OSError:
        # 认领去重是辅助状态：写不了时宁可偶尔重复，也不能让已经成功的
        # 主流程（院子动作、晨报投递……）因为一段引导文案而失败。
        return intro_text
    return intro_text
