# 开发者平台 API 接入助手

这是独立的 DevTrace 开发者 API 接入诊断项目。默认使用现有 Anthropic 兼容配置。没有模型服务时，可设置 `DEVELOPER_MODEL_MODE=rules`，用确定性参考模型演示工具和计划状态；此模式不代表大模型规划效果。

## 阅读路线

- [完整构建与模块说明](完整构建与模块说明.md)：从数据、工具、计划、记忆到 API/UI 的构建顺序，并逐项对照 Agent 十模块。
- [工程化审查](工程化审查.md)：与原客服项目对照、已实现范围、生产级差距与面试表述。
- [实测评测报告](evaluation_report.md)：自动化测试、HTTP、真实配置模型及界面验证结果。
- [依赖与安全评估](依赖与安全评估_2026-10-18.md)：ChromaDB 漏洞核查、暴露面、处置决定与审计门禁。

## 运行

- 全栈：在 `backend` 执行 `docker compose up -d --build`。若构建网络无法访问 ChromaDB 的 ONNX 模型地址，可先 `docker compose build --build-arg PRELOAD_ONNX=false`；2026-10-18 的 Docker 全栈实测（规则模式 30/30 HTTP、SSE、断流、并发、跨轮恢复）见 [evaluation_report.md](evaluation_report.md)。
- 模拟平台单独运行：`python -m uvicorn developer.sandbox:app --port 8100`。
- Python 服务独立运行时，设置 `DEVELOPER_SANDBOX_URL=http://localhost:8100`，并配置 Redis、ChromaDB 与模型环境变量。

模拟平台的固定记录为 `req_auth_01`～`req_auth_10`、`req_quota_01`～`req_quota_10`、`delivery_01`～`delivery_10`，另有 `delivery_combo_01`～`delivery_combo_10` 组合案例。HTTP 接口提供脱敏请求、密钥前缀权限和投递记录；SQLite 保存模拟工单。

诊断/安全演示另有少量夹具，故意不加入 30 条固定评测集：`req_auth_11`（请求记录称凭证过期，但 `demo-auth-11` 密钥状态为 active，用于证据冲突）、`delivery_combo_11` + `req_ok_01`（关联请求成功，用于排除限流原因）、`delivery_inject_01`（投递记录的错误摘要内含指令文本，用于验证观察不可信边界）。

`req_auth_v2_01` 演示先读取请求记录确认 API 版本，再检索 v2 鉴权文档；版本切换覆盖目前仅限这个模拟案例。

## API

先用本地账号调用 `POST /auth/login`，在以下请求头中传入 `Authorization: Bearer <token>`。`POST /developer/chat` 请求示例：

```json
{"message":"请求 req_auth_01 返回 401，帮我排查","conversation_id":null}
```

响应包含 `conversation_id`、`run_id`、`answer`、`plan`、`actions`、`timeline`、`evidence`、`diagnosis`、`security`、`memory_used`、`metrics`、可选 `ticket_id` 和 `decision_mode`。`decision_mode` 区分 `model`、`rules` 和 `rules_fallback`。`diagnosis` 除 `hypotheses`（含 `code`、`description`、`status`、`confidence`、`evidence`、`contradicted_by`、`next_evidence`、`remediation`）外，还有整体 `status`（`evidence_available` / `conflicting_evidence` / `insufficient_evidence`）、`summary`、`conflicts` 和 `linkages`。`security` 给出 `injection_suspected`、`flags`、`tainted_sources` 与 `policy`；`actions[].tainted` 标记来自不可信记录的观察。等待补充时可用同一 `conversation_id` 继续。`GET /developer/runs` 和 `GET /developer/runs/{run_id}` 根据认证身份授权；客户端不能指定 `user_id`。

流式接口的 `started` 事件提供独立的 `task_id`；`GET /developer/tasks/{task_id}` 可查询该用户的状态。断开连接时记录 `cancelled`，同会话可从最近检查点继续。2026-10-22（P12）起进程内 SSE 的空闲心跳改用 `asyncio.wait` 等待常驻 getter：Python 3.10 上原来的 `asyncio.wait_for(queue.get(), 15)` 会在"事件刚入队还没被取走"的窗口里**吞掉一次外部 `Task.cancel()`**，导致客户端已断开而生成器继续跑、任务停在 `running`、会话锁要等 180 秒 TTL；现在单次取消即传播，心跳与事件顺序、150 秒上限、断开清理语义都没变。队列模式下 worker 进程被 `kill -9` 的恢复也已实测：租约到期后由幸存 worker 回收重跑，终态唯一、`task.finished` 只有一条、`run_id` 不重复、会话锁释放（详见评测报告 P12 一节）。任务记录带心跳时间：SSE 事件与 15 秒空闲心跳都会刷新 `updated_at`，`running` 超过 240 秒未更新会在查询时改为 `interrupted`，进程启动时也会把上一进程残留的 `running` 一并修正（仅适用于单进程/单副本共享 Redis）。同步与流式入口共用同一把 30 次/60 秒的用户级限流（2026-10-22 P7 起），流式超限在建立连接或入队之前返回 HTTP 429 + `Retry-After: 60`，不会创建任务记录、入队或写 `started`；连接建立后不再中途限流。该语义在 P7（rules 模式）与 P8（真实 provider 模式）各复验一轮，P8 同时测过前端 429 提示的实际渲染。P10 起限流路径的 Redis/Lua **基础设施故障**改为 fail-open：写一条 warning 日志与 `<kind>.rate_limit_unavailable` 审计事件（`detail` 仍只含白名单字段）后放行请求，不再把 Redis 抖动放大成 500——这是可用性优先的演示级取舍，故障窗口内配额确实不生效，生产应由网关或多副本限流兜底；真正的超限仍然照旧 429，登录侧也从裸 429 补成带 `Retry-After: 60`。固定窗口"被拒请求同样计入 `INCR`"的语义保持不变（31 次尝试 → 计数 31）。fail-open 只覆盖限流器：诊断主流程仍需要 Redis 会话锁，因此 Redis 整体不可用时 chat 请求会在锁获取阶段失败，只有不依赖 Redis 的登录路径表现为"降级不限流"。`DEVTRACE_TASK_QUEUE=enabled` 时由持久队列执行（Compose 默认），进程内模式不会自动重跑中断任务，中断后需在同一会话发送“继续”。队列侧的会话锁 `developer:session-task:<sha256(user\0conversation)>` 自 2026-10-23（P13）起**只由持有者释放**：锁值本身就是 `task_id`，所有任务终态路径（worker 的 `finally` 与启动前取消分支、入队失败回滚、回收耗尽重试的死信终结）改用 `release_session_if_owner()`，由 Redis 原子执行"比较 `GET` 与 `ARGV[1]` 后才 `DEL`"，因此晚到的清理不会放走已经重新认领同一会话的新任务；`release_session()` 作为不携带 task_id 的显式无条件原语保留，任务终态路径不得再使用。同样在 P13 补齐的是回收的终结收尾：`reclaim_expired()` 把任务打入死信时会 owner-aware 释放会话锁并写**恰好一条** `task.finished`（`status=failed`，`detail` 只有白名单 `status`/`attempts`），不再留下"会话在 900 秒锁 TTL 内不可用且审计无痕"的僵尸状态；收尾是 best-effort，锁释放失败只记 warning 且审计照旧写入。两处修复的证据边界要读清：Lua 原语在真实 Redis 上（`devtrace-verify` 容器、合成 key）验证过非持有者返回 0 且锁值与 TTL 不动、持有者返回 1 且 key 消失，但**回收终结路径只在 fake Redis 上验证**，未向真实队列注入合成任务。

2026-10-23（P14）把同一形状剩下的两处也收掉了，P13 的私有收尾提取为公共 `task_queue.finalize_terminal(redis, task_id, *, status, record=None, reason_code='')`：读不到任务记录就整体跳过（不猜归属），只有 `user_id` 与 `payload.conversation_id` **同时存在**才调 `release_session_if_owner()`，否则记一条 info 后跳过，`task.finished` 审计始终 best-effort 写入。两个新调用点：① `recover_running_tasks()` 的 `attempts` 耗尽分支（原先只置 `failed` + 进死信，锁白挂 900 秒且无终态审计）复用手上那份记录收尾；② `api/main.py` 的 `_mark_interrupted()`——**注意两套任务索引并不相同**（队列任务在 `developer:tasks:queue-index` 且带会话、进程内流式记录在 `developer:tasks:running` 且不带会话），但 `developer:task:<id>` 键空间共享，所以 `GET /developer/tasks/{id}` 的查询期修复确实能把一个持队列锁的任务标成 `interrupted`，这就是僵尸入口；`interrupted` 的审计 `detail` 为 `{status, attempts, reason_code}`。既有的"写前重读 + `updated_at` 必须相等"门卫让重复 reconcile 天然幂等（第二次不再写审计、也不再发起比较删除），进程内流式记录因为不带会话字段而走"只写审计、跳过释放"分支。`reclaim_expired()` 的 `detail` 内容与调用形状未变，P13 已验收语义原样保留。`task_queue.dead_letter()` 没有生产调用点，是已知死代码，本轮只标注、删除留下一轮裁定。P14 的两条路径同样**只有 fake Redis 与单元证据**（本轮不起 Docker、零模型调用）。

## 设计边界

- 计划生成和下一行动选择可由模型决定；服务端限制工具白名单、从用户或查询结果获取标识符，并控制单轮最多 5 次行动与 1 次计划调整。
- 模型提出的合法工具顺序会影响批准计划；服务端补齐必需证据、修正依赖，并只允许从当前可执行步骤中选择行动。`plan.proposed_tools`、`approved_tools` 和 `adjustments` 可用于对照审查。最终答复由成功的工具观察整理生成。开发者工具默认在项目内注册与调用；另有一个独立的 MCP 协议服务（`mcp/server.py` 的 JSON-RPC 2.0 + stdio 入口），需显式开关才接管部分路径，见下文"MCP 协议"一节。
- Redis 计划状态和运行记录的有效期由 `DEVTRACE_REDIS_TTL_SECONDS` 统一配置（2026-10-22 P7 起，默认 86400 秒即 24 小时、下限 60 秒）；开发者记忆使用独立键前缀、`developer_episodic` 和 `developer_profile` collection；文档位于独立的 `developer_knowledge` collection。工作记忆与摘要的过期由另一个开关 `DEVTRACE_MEMORY_TTL_SECONDS` 控制，两者互不影响。
- 回复、界面和工单均说明数据来自本地模拟平台；不接入真实开发者账号、生产日志或工单系统。
- JWT 仅提供本地演示身份，仍需生产身份服务、TLS、密钥轮换和真实业务权限。请求幂等结果在完成后缓存；进程在副作用与结果缓存之间崩溃时并不保证通用操作恰好一次。模拟工单自身另有去重。

## 验证

`python -m pytest -q tests` 覆盖 371 项测试（另有 219 个 subtest；2026-10-22 P11 起 Windows 本机**不再需要** `PYTHONUTF8=1`——MCP stdio 的服务端与客户端都已强制 UTF-8，此前那条会因 GBK 子进程输出稳定解码失败的用例已修）：30 条固定案例、跨轮恢复、错误标识后调整、工单去重、用户隔离与敏感信息脱敏，证据冲突、关联请求排除、工具记录/用户消息中的提示注入和任务心跳修复，2026-10-22（P7）新增的流式与同步共用配额、429 无副作用、`enforce_limit` 不可经查询参数触达与瞬时 Redis TTL 统一配置用例，同日（P9）新增的 holdout 工具名 base 归一化用例（`mcp_` 前缀只在命中本地注册表时才剥离、`assess()` 默认口径不变、离线重算不改存盘官方报告），同日（P10）新增的限流 fail-open/登录 `Retry-After` 用例与情景记忆保留 CLI 用例（dry-run 默认、`--apply` 才删、无 `ts` 不删、画像集合拒绝、Chroma 故障非零退出），以及同日（P12）新增的 SSE 取消传播用例（单次 `Task.cancel()` 在事件已入队时仍传播、空闲心跳不丢事件、取消后记录 `cancelled` 并释放会话锁、正常 `complete` 路径事件顺序不变且无泄漏 task；其中"旧 `wait_for` 写法会吞取消"的负向对照在 Python 3.11+ 按设计跳过），2026-10-23（P13）新增的会话锁归属用例（`test_session_lock_ownership.py`：owner 释放返回 1、非 owner 返回 0 且锁值与 TTL 不动、锁已消失返回 0、送进 Redis 的脚本/key/argv 形状、"晚到的 A 释放后 C 仍进不来"的端到端竞态、三条终态路径各自 subTest，以及同场景下无条件原语确实会删掉 B 的锁这条负向对照；`test_queue_production.py` 另加 6 项覆盖连续两次崩溃、死信终结释放锁 + 恰好一条 `task.finished`、租约有效不误终结、不与 worker 二次审计、锁故障仍降级为 warning 且审计不丢；这里的 fake Redis `eval` 桩已从盲目 `return 1` 换成忠实比较删除，且记录每一次 `eval` 调用），2026-10-23（P14）新增的终态收尾用例（`test_queue_production.py` 再 4 项：启动恢复耗尽重试 → `failed` + 死信 + 锁释放 + 恰好一条白名单 `detail`、仍有重试预算时保留锁且不写终态审计、绝不删更晚任务的锁、锁故障有 warning 而审计照旧落地；新 `test_interrupted_tasks.py` 12 项，含 4 项走真实 ASGI 的 `GET /developer/tasks/{id}`：审计恰 1 条且 `workspace`/`trace_id`/`actor` 正确、消息正文不进审计流、两次 GET 不重复也不二次释放、进程内记录一次 `eval` 都不发起、缺 `user_id` 跳过释放且锁原地不动、owner 是活 worker 时完全不动、健康任务不被修复、视图不外泄 `payload`/`user_id`/租约字段）。实测结果见 [evaluation_report.md](evaluation_report.md)；模型在线演示需要有效的模型 API Key。

登录取得短期 token 后，写入当前进程环境变量 `DEVTRACE_EVAL_TOKEN`，再运行 HTTP 判定器；不要把 token 写入仓库或命令历史：

```powershell
python -m developer.evaluate --base-url http://localhost:18002 --mode rules --output developer/eval_rules.json
python -m developer.evaluate --base-url http://localhost:18003 --mode model --sample-per-family 1 --output developer/eval_model.json
```

评测检查完成状态、支持的诊断、文档与记录证据、重复调用、依赖顺序、模拟数据声明和决策模式；出现未通过案例时命令返回非零退出码。`--sample-per-family 1` 表示每类一条，是演示抽样，不是模型准确率。

以下实验可独立重跑，参数和结果的适用范围见 [升级进度](升级进度.md)：

```powershell
python -m developer.evaluate_planning --output developer/eval_planning.json
python -m developer.evaluate_recovery --output developer/eval_recovery.json
python -m developer.evaluate_retrieval --output developer/eval_retrieval.json
python -m developer.evaluate_scenarios --output developer/eval_scenarios.json
python -m developer.evaluate_diagnosis --output developer/eval_diagnosis.json
python -m developer.evaluate_holdout --mode rules --output developer/eval_holdout_rules.json
python -m developer.evaluate_memory_tasks --output developer/eval_memory_tasks.json
python -m developer.evaluate_public_docs --output developer/eval_public_docs.json
python -m developer.evaluate_error_playbooks --output developer/eval_error_playbooks.json
python -m developer.evaluate_prompts --output developer/eval_prompts.json
python -m developer.evaluate_mcp --output developer/eval_mcp.json
python -m developer.evaluate_memory_quality --output developer/eval_memory_quality.json
python -m developer.evaluate_memory --output developer/eval_memory.json
```

公开 API 文档知识库：`developer/public_docs.py` 内置 641 条文档（29 条人工摘要，含 GitHub/Stripe/OpenAI/DeepSeek/Anthropic/Gemini/通义千问，+ 612 条 Stripe 官方 OpenAPI 完整规格导入；文件在 `public_docs_data/`，运行时自动加载），`search_public_docs` 按 API 检索；可用 `python -m developer.ingest_public_docs --source <OpenAPI.json|Markdown> --api <名称> --source-url <链接> --license <许可> --filter "关键词1,关键词2" --output <json>` 导入自己的文档。`python -m developer.evaluate_public_docs` 对 28 条公开 API 问题做回归（2026-10-18 为 28/28）。

错误处置手册：`developer/error_playbooks.json` 收录 Stripe/GitHub/Twilio 各 12 条（共 36 条）结构化错误条目，`lookup_error_playbook` 按症状/API/状态检索，返回可重试性、退避策略、恢复步骤与来源；`python -m developer.evaluate_error_playbooks` 对 40 条症状做回归（2026-10-18 为 40/40）。

持久任务队列：设置 `DEVTRACE_TASK_QUEUE=enabled` 后，流式诊断由 Redis 队列 + `python -m api.worker` 独立执行；失败重试一次、耗尽进死信、worker 重启回收中断任务。Compose 已包含 `worker` 服务；同步接口保持进程内。队列/worker 用例见 `tests/test_task_queue.py`。

情景记忆保留策略：`python -m developer.memory_retention --days 90` 读取 Chroma episodic 集合，按 metadata `ts` 找出早于保留窗口的记录。**默认 `--dry-run`**，只输出候选数量、按用户分布、涉及会话与"无可用 `ts` 的跳过数"，一条都不删；`--apply` 才真正删除（与 `--dry-run` 互斥），`--user <name>` 只能进一步缩小范围。三条硬边界：`ts` 缺失或不可解析的记录**不猜年龄、不删除**（记入 `skipped_no_ts`）；含 `profile` 的集合名直接拒绝执行——用户画像仍然只能通过 `DELETE /developer/profile` 显式删除；Redis 工作记忆与摘要继续由 `DEVTRACE_MEMORY_TTL_SECONDS` 过期，不归本工具管。`--days` 必须 ≥1，`--page-size`（P11 新增，默认 500、范围 1..5000，越界或非整数以退出码 2 失败且不触碰 Chroma）决定每页读取多少条：扫描按 `limit`/`offset` 逐页进行、报告带 `pages`/`page_size`，整集合不再一次性进内存，而**分页不改变任何判定**（真实 Chroma 实测 `--page-size 100/500/5000` 的扫描数、候选数、较新保留数与跳过数完全一致）；不支持或忽略分页参数的客户端退化为一次全量读取，不会死循环，候选 id 去重也保证同一条记录不会被删两次。Chroma 连不上或读写失败会打印原因并以非零码退出，不会静默报成功。集合名与连接沿用服务的配置（`CHROMA_HOST`/`CHROMA_PORT`/`CHROMA_PERSIST_DIRECTORY`，默认集合 `developer_episodic`），Chroma 仍是演示级单实例、无按 workspace 分区。

原生 tool calling：`AnthropicDecisionModel.choose_action` 优先把 ready steps 的 JSON Schema 作为 Anthropic `tools` 传给模型（`tool_choice=auto`），校验 `tool_use` 参数后执行；不支持的 provider 自动回退 JSON/规则，指标 `native_tool_calls` 可观察。

提示词版本化：`developer/prompts.py` 集中五个模板并带 `PROMPT_VERSION`；`python -m developer.evaluate_prompts` 校验渲染快照与安全契约，CI 已运行；修改提示词后需 `--update` 并审阅 diff。`GET /developer/prompts` 只返回版本与字符数。

MCP 协议：`mcp/server.py` 提供 JSON-RPC 2.0 的 `initialize/tools/list/tools/call` 与 stdio 入口，`mcp/client.py` 提供进程内/stdio 传输；设置 `DEVTRACE_MCP_PUBLIC_TOOLS=enabled` 后 `public_api` 路径改用 `mcp_lookup_error_playbook`/`mcp_search_public_docs`。`python -m developer.evaluate_mcp` 对 10 条 MCP 路径问题做回归（2026-10-18 为 10/10）。

记忆管理：`GET /developer/memory/{conversation_id}` 导出工作记忆/摘要/画像，`DELETE` 清理工作记忆/摘要/情景/计划检查点（画像保留）；`python -m developer.evaluate_memory_quality` 用确定性摘要器检查压缩保留/隔离/遗忘管线（2026-10-18 通过），不代表真实模型摘要质量。

候选计划评分：`python -m developer.evaluate_planning_candidates` 检查 approved/evidence_first/minimal 三候选打分与选择（2026-10-18 为 11/11）；`DEVTRACE_PLANNING_CANDIDATES=enabled` 时启用，响应 `plan.candidates` 暴露候选与分数。

`evaluate_holdout` 使用 40 条手写标签的独立 holdout 集（`eval_holdout_cases.json` 与专用夹具；`2026-10-18` 批 19 条 + `2026-10-21` 批 21 条），规则模式在进程内运行；模型模式需额外传 `--mode model --base-url <地址>` 并设置 `DEVTRACE_EVAL_TOKEN`，`--batch <批次>` 只跑一个标签批次（未知批次直接报错，不会空跑）。结果按 `batch` 标注并在 `summary.by_batch` 分组。2026-10-21 规则模式整表 40/40（19/19 + 21/21）；同日模型模式为控成本只跑新批 21 条，得 **18/21**（`eval_holdout_model_2026-10-21_new_batch.json`）。2026-10-22（P8）用真实模型跑满**全量 40 条**，得 **37/40**（原批 **19/19**、新批 **18/21**，`eval_holdout_model_2026-10-22_full40.json`；43 次 HTTP 请求、0 重试、0 基础设施失败、墙钟 487.3 秒、单请求 mean 11321.6ms；provider 未返回 usage，故成本只披露请求数与时间、不估算 token）。3 条失败原样保留、断言未因结果而修改：`h_public_playbook_path`/`h_public_docs_path` 是标签按本地工具拼写断言、而 `DEVTRACE_MCP_PUBLIC_TOOLS=enabled`（base compose 默认）让实际工具带上了 `mcp_` 前缀（本轮 `DEVTRACE_MCP_ALL_TOOLS` 未设置仍原样复现这两条失败，说明触发条件是 base compose 默认的 PUBLIC_TOOLS 而非 ALL_TOOLS；P6 的归因已在评测报告中就地更正，原文按协议保留）；`h_quota_v2_doc_version_boundary` 是模型没带回 v1 版本文档那一条具体证据。2026-10-22（P9）另给一份**离线补充归因**（`--mode rescore --source <官方报告> --output <补充报告>`，零模型调用）：工具名按 base 名比较，`normalize_tool_name()` 只剥 `mcp_` 前缀且必须命中 `developer.planner.TOOL_SCHEMAS`，未知名原样返回，`assess()` 默认不归一化（官方口径逐字不变）；补充口径下 **39/40**，只有上述 2 条 public_api 因 `missing_tool:*` 被消除而 pass——同一份存盘 observed，只说明标签绑定对上，**不代表能力提升**，`h_quota_v2_doc_version_boundary` 仍失败、原因逐字不变，3 条多轮行拒绝重算并写明原因；官方 37/40 报告原样保留，补充报告同样在全过时才返回 0。样本小、**标签全部由项目作者手写**、模型只有一个 provider，不等同于第三方隐藏评测，也不构成跨 provider 结论。`evaluate_memory_tasks` 用 12 条两轮任务比较同会话恢复与新会话重开（规则模式、stub 工作记忆），衡量跨轮任务成功与重复检索，不代表 ChromaDB 情景记忆或生产延迟。

检索评测需要可用的 ChromaDB 或本地向量模型。规则规划和故障注入使用进程内模拟平台，不能视作模型的泛化评估。

依赖治理：`python -m developer.audit_gate <pip-audit.json>` 只对存在修复版本的漏洞返回非零；无上游修复版本的记录会转为非阻断提示。漏洞核查与处置决定见 [依赖与安全评估](依赖与安全评估_2026-10-18.md)，扫描现状也记录在[升级进度](升级进度.md)中。
