# 未收录的接入层测试

`tests/` 目录收录的 4 份测试（`test_morning_paper.py`、`test_morning_feedback.py`、
`test_morning_archive.py`、`test_morning_scout_sources.py`）只测本仓库包含
的核心模块，import 路径不变，可以直接跑（见 README「本地跑测试」一节）。

生产还有两份测试覆盖"晨报接入到 home.py / ws_handlers.py 之后"的行为，本
仓库没有收录，因为它们各自 import 的是完整的生产 `home.py`/`ws_handlers.py`
（两个体量很大、包含大量跟晨报无关业务的分发器文件），本仓库只给了
`integration/` 下的摘录版，直接把这两份测试搬过来会因为 import 不到完整
模块而跑不起来。记录在这里，方便你把 `integration/home_integration.py` /
`integration/ws_handlers_integration.py` 接进自己的项目后，照着同样的用例
自己补测试。

## 生产 `tests/test_home_morning.py`（`DomainRoutingTests` + `HandleMorningTests`）

- `test_aliases_route_to_morning_domain` —— "晨报"/"报纸"/"早报"这几个说法
  都要能路由到 `morning` 域名
- `test_category_hint_mentions_morning` —— 无法识别域名时的提示文案里要
  提到 `home 晨报`
- `test_help` —— `home 晨报 --help`（或等价触发）返回 `morning_help()` 全文
- `test_status_without_issue` / `test_status_with_issue_lists_numbered_items`
  —— 无参 `home 晨报` 在"今天没报纸"与"今天有报纸"两种状态下的输出
- `test_rating_end_to_end` —— `home 晨报 打分 2:3追 5:0` 全流程：解析→校验
  编号范围→写入→连带建立 follow→返回确认文案
- `test_rating_lenient_di_n_tiao_form` —— 宽容语法（"第二条 3"这类）也要能
  打分成功
- `test_rating_without_issue_raises_home_error` / `test_rating_invalid_spec_raises_home_error`
  / `test_rating_out_of_range_index_raises_home_error` —— 三种该拒绝的场景
  （没有当天期刊 / 串解析不出来 / 编号超范围）都要报 `HomeError`，不静默
  丢弃、不部分生效
- `test_follow_by_index` / `test_follow_without_issue_raises` /
  `test_follow_non_numeric_raises` —— `home 晨报 追 <编号>` 正常路径与两种
  该拒绝的输入
- `test_unfollow_by_index` / `test_unfollow_by_fid_without_current_issue` ——
  `home 晨报 不追` 既能按当天编号取消，也能不依赖当天期刊、直接用
  `f-MMDD-n` 追踪 id 取消跨天的旧追踪
- `test_prefer_topic_add_remove_without_issue` / `test_avoid_topic_add_remove`
  —— 口味清单增删不需要当天有报纸也能用
- `test_unrecognized_subcommand_raises_home_error` —— 看不懂的子命令要报
  错并带用法提示，不能悄悄什么都不做

## 生产 `tests/test_ws_handlers.py` 里的 `get_morning_paper` 用例

不是独立测试函数，是一个"逐个消息类型过一遍"的参数化表驱动测试里的一行
数据（跟 `get_garden`/`get_diary`/`search_history` 等其它十几种消息类型共
用同一套断言逻辑，不是晨报专属的测试文件）。这一行验证的行为：

- 发送 `{"type": "get_morning_paper", "date": "2026-08-19"}`
- mock 掉 `morning_paper.load_issue_for_date`、`morning_paper.list_scout_dates`、
  `morning_feedback.frontend_summary` 三个依赖
- 断言回包严格等于 `{"type": "morning_paper", "date": ..., "has_draft": ...,
  "items": ..., "error": None, "available_dates": ..., "feedback": ...}`
  ——也就是 `integration/ws_handlers_integration.py` 里 `handle_ws_get_morning_paper`
  的输出契约。

另外同文件里还有一处静态断言：`WS_HANDLERS` 注册表里必须能找到
`"get_morning_paper"` 对应 `handle_ws_get_morning_paper` 这个 handler（防止
新增消息类型时忘记登记）。
