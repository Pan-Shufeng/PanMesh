# 任务簿接口

从工作区导入 `ledger_core`。模块只依赖 Python 标准库，工具定义由 `ledger_core.TOOLS` 提供，每项包含 `description`、`inputSchema` 和 `mode="collab"`。调用返回可直接序列化为 JSON 的字典。

```python
from pathlib import Path
from ledger_core import TOOLS, LedgerError, call

response = call("task_list", {"role": "owner", "status": "pending_review"},
                root=Path(r"E:\记忆库系统"), project="no3rr", actor="codex-main")
```

入口先校验连接具有协作权限，再注入已认证的项目和主体编号。不要从模型的工具参数、聊天正文或客户端自报信息取 `project`、`actor`、`root`。项目和主体区分大小写。恢复原负责人时沿用同一主体编号。将新窗口授予该主体编号前，接入方负责核实身份。执行方编号必须与负责人不同。会话编号用于绑定执行上下文，远端可信适配器应从原生会话取得编号。

权限按项目隔离。负责人查阅全部轮次，执行方查阅自己受派的轮次和该轮交接明确传入的内容。持久库路径可用 `db_path(root)` 取得，默认 `runtime/task_ledger.sqlite3`。备份运行中的库请使用 SQLite 的 `Connection.backup` 接口。材料地址、来源证据和做法版本由调用方在授予前按项目和执行方核对。任务簿保存已授权传入的引用和摘要，不会替连接打开文件、解析记忆编号或核验原文。

## 工具和数据

| 工具 | 作用 |
| --- | --- |
| `task_create` | 当前主体成为负责人。填写目标、交付要求和编号化验收条件，可附材料、版本化做法、前置成果引用及对照试验编号。 |
| `task_dispatch` | 负责人登记新轮次，得到 `task_id`、`dispatch_id` 和结构化 `handoff`。首轮传 `previous_dispatch_id: null`。补派传当前轮编号和具体指示。 |
| `task_send_begin`、`task_send_receipt` | 推送通道一次性授权和回执登记。调用顺序见下文。 |
| `task_claim` | 指定执行方用实际 `session_id` 领取。主动领取使用 `channel="pull"`，预计会话可选。已领取的轮次用原会话回报。 |
| `task_supplement`、`task_progress` | 负责人追加执行指示，执行方追加进展、证据和阻塞点。完整历史在 `task_history`。 |
| `task_submit` | 执行方按验收条件编号提交成果。记录成果位置、证据、自检、未决项和是否读原文。 |
| `task_review` | 负责人按条件独立记录 `accept`、`return` 或 `defer`，每项附核对证据。已接受项要求有对应交付。待用户判断的项可以追加结论，已接受或退回的结论保留。 |
| `task_interrupt` | 确认旧轮会话停止或任务尚未送达，登记原因和证据。随后可另派。 |
| `task_list`、`task_get` | 查找本人负责或受派的任务，查看状态、下一步和最近记录。历史轮次用 `task_get` 显式传 `dispatch_id`。 |
| `task_history`、`task_handoff_item` | 读取审计事件，或展开本轮交接明确引用的继承项。后者用本轮 `dispatch_id` 加指针中的 `source_task_id`（指针的 `task_id`）和 `criterion_id`。 |
| `task_record_usage`、`task_record_metrics` | 分主体记录模型用量与费用，负责人追加单 AI／双 AI 对照试验的观测。 |
| `task_compare` | 负责人只读对照同一案例和同版验收标准的单 AI 与协作任务。 |

完整参数、约束和必填项以 `inputSchema` 为准。所有写工具需要 `request_id`。每个项目和主体内，请为每个逻辑操作生成一个唯一编号，重试保留原编号及完全相同的参数。成功重放使用 `replayed: true` 返回当时的响应，当前状态请重新读取。相同编号传入其他工具或改了内容时返回 `idempotency_conflict`。`task_send_begin` 重放时固定返回 `send_allowed: false`。

`LedgerError.code` 包括 `unknown_tool`、`invalid_argument`、`not_found`、`state_conflict`、`dependency_pending`、`idempotency_conflict`、`schema_version`。无权访问的任务和缺失任务均返回 `not_found`。数据库错误由上层分类，因暂时锁定而重试时沿用请求编号。`task_list` 和 `task_history` 用返回的 `next_after` 继续同样条件，`null` 表示扫描结束。

## 推送、确认与恢复

1. 用 `channel="push"` 登记派工。发送到已有会话时传 `session_id`。CLI 首次创建会话时省略，表示目标会话尚未分配。
2. 调用 `task_send_begin`。仅收到本次的 `send_allowed: true` 时，原调用者可以用返回的 `reservation_id` 实际发送一次。把 `dispatch_id` 带进原生消息，供回执与恢复关联。返回的 `session_id` 为 `null` 时启动一个新会话。
3. 发送得到可信确认后，用同一 `reservation_id` 登记 `outcome="sent"`、`receipt_ref` 和 `evidence`。首轮新会话还须提供实际 `session_id`。任务簿会一次性绑定本轮会话。预先指定会话时，回执里的会话编号如果存在，必须是该目标。外部回执编号在项目、客户端、会话内只能归属一个轮次。
4. 超时或崩溃后，用 `task_list` 查找 `sending`、`send_unknown`，在 `task_get.dispatch` 查目标和发送编号。先核对原生会话和原消息，留存证据再补登记。`unknown` 带证据但不授予发送许可。只有查明原消息没有送达、上一发送者已经停止且无法再发出本次请求，才能提交 `not_sent`、证据和 `sender_stopped: true`，再申请新的发送编号。调用方负责判断发送者和原生操作是否已停止。单凭消息未找到、调用超时或进程退出无法证明未送达。
5. 已知目标会话的执行方实际收到消息而发送方失联时，可以用同一目标会话领取，并传 `received_evidence` 补证。新建目标尚未绑定时，必须由负责人先登记可信回执再领取。领取后执行方可独立回报，负责人重连后搜索 `pending_review`。
6. 原会话仍可用时继续用它。确认原会话已经停止或确定未送达，负责人可登记 `task_interrupt` 后补派。已有交付的轮次走验收。发送开始后仍未查明结果的轮次无法关闭或补派。

补派交接包含尚未验收的条件、已验收成果指针、材料、固定做法版本和本轮要求。补派时可传 `method_update={methods, reason}` 采用新版，历史轮次继续引用原版。前置成果须属于同项目、同一负责人，并且指定条件已经独立接受，才进入派工交接。由于任务簿保存的是引用，接续前需要不变的成果版本时，请传可追溯的位置或固定版本。每次补充要求留存为事件。

## 对照试验

`comparison` 用 `case_id`、`arm`、可选 `trial_id`、`rubric_version` 关联留出任务。单 AI 组可用一个执行主体和负责记录验收的人类主体。任务详情对已验收任务提供从首次领取到最终验收的系统时长。另记研究口径的用时、用户投入、重复解释、返工、漏错、原文阅读和实际采用的做法版本。

用量记录按任务、录入主体和 `sample_id` 去重。一条记录对应一段可核查、互不重叠的用量。历史的 `usage_recorded` 事件提供录入主体及完整记录。token 分为 `measured`、`estimated`、`unknown`，费用分为 `actual`、`estimated`、`unknown`。未知值省略数字，费用用十进制字符串和币种。汇总时先核对覆盖范围、币种与分段重合，再分别汇总实测、估算和未知项。独立验收退回已提交成果时可归为质量问题；失败说明匹配到网络、限流、权限等系统原因时会保留归因和证据等级。

调用 `task_compare` 时传入 `solo_task_id` 和 `collab_task_id`。两条任务须在当前项目，均由调用者负责，且填写相同的 `case_id` 和 `rubric_version`。返回两组各自的状态、登记时长、用量与评估观测。

- `usage` 给出本组已登记记录的小计和记录数，`usage.models` 用同一结构按模型展开。token 的实测与估算各自保留输入、输出和合计字段，合计取同一条记录的明确总 token 或完整输入加输出。费用的实付与估算各自按币种给出十进制小计。未知另记条数。小计为 `null` 时表示该栏没有可汇总数值。
- `token_coverage` 和 `cost_coverage` 可为 `missing`、`partial` 或 `unverified`，分别表示无数字记录、部分记录有数字但仍有缺口、现有记录有数值且整项任务的覆盖仍待核实。token 缺少完整的单条合计时视为有缺口。即使所有登记项都有数字，接口也只报告 `recorded_subtotal`，因为任务簿无法知道各客户端是否已经登记了全部用量。
- `metrics` 包含用户投入、返工次数、最终漏错、重复解释、用时及方法使用情况。每个字段采用负责人最近一次明确登记的值，`metric_sources` 保留对应来源和时间。未填项和显式未知均返回 `null`，显式未知保留来源。`ledger_elapsed_seconds` 在最终验收后给出从首次领取到验收的系统时长。

## 上游和范围

失败归因代码和对应 16 项测试来自固定上游提交，MIT 许可原文随代码保存在 `ledger_vendor/LICENSE`。本地将原执行投影设为可选输入，因而可以直接接任务簿验收记录。派工恢复采用上游执行端规则中的未知发送禁止重试，短交接采用仅继承已验收成果的规则，状态和存储按本项目重新实现。[上游固定提交](https://github.com/Gambler134201/multi-agent-collaboration/tree/54b74703e86564e388b900e0d5c0f8dab3f27264)

任务簿模块覆盖事务、状态、身份范围与持久记录。接入窗口负责原生会话创建、发送与通知，以及凭据和投递证据的核实。
