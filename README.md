# DevTrace——开发者平台 API 接入 Agent 助手

[English](#english) | [中文](#中文)

## 中文

面向开发者 API 接入排障的单 Agent 项目。这是一个独立运行的开发者支持项目，围绕三类可复现问题演示受约束的多步 Agent 流程：**401 鉴权失败、429 限流、Webhook 投递失败**。

Agent 从会话记忆和历史证据开始，生成诊断计划，调用文档、请求记录、密钥权限或 Webhook 工具，并将每次观察写入状态。模型可以在服务端的证据和依赖约束内改变行动顺序；服务端负责工具白名单、参数来源、工单授权、脱敏与失败降级。

### 技术亮点

- **ReAct 与 Plan-and-Execute**：显示计划提议、服务端批准计划、工具行动、观察和状态变化；行动选择优先使用模型原生 tool calling（失败回退 JSON/规则），不展示内部推理。
- **候选计划评分**：启用 `DEVTRACE_PLANNING_CANDIDATES` 后，对 approved / evidence-first / minimal 三种安全排序按证据覆盖、记录先于文档、依赖合法和长度打分，选最高分执行，响应暴露候选与分数；11 条规则评测 11/11。
- **提示词版本化**：系统/计划/行动/原生工具/重试提示词集中到 `prompts.py`，带 `PROMPT_VERSION`、渲染快照与契约回归；CI 检测未审阅的提示词改动，`GET /developer/prompts` 只暴露版本与大小、不泄露正文。
- **标准 MCP 接入**：内置 JSON-RPC 2.0 的 MCP server/client（`initialize / tools/list / tools/call`，含 stdio 入口，线格式为 **UTF-8 换行分隔 JSON**：服务端显式 `reconfigure(encoding='utf-8')`、客户端起子进程时强制 `PYTHONUTF8=1`，中文工具描述不依赖调用方区域设置）。server 托管 **8 个只读工具**（平台文档、限流文档、请求记录、关联请求、密钥状态、Webhook 投递、公开文档、错误手册），`create_support_ticket` 始终留在本地——它需要用户显式授权和只有本服务持有的会话/幂等上下文。`DEVTRACE_MCP_PUBLIC_TOOLS=enabled` 时公开 API 路径走 MCP，`DEVTRACE_MCP_ALL_TOOLS=enabled` 时 `auth`/`quota`/`webhook` 的读工具全部走 MCP；计划、执行与证据校验按基础工具名比较，因此两种传输共用同一套依赖与门禁规则，MCP 调用失败记为失败步骤而不是静默回落本地工具。规则模式公开路径评测 10/10。
- **外部 MCP server 注册表（可选）**：`DEVTRACE_MCP_SERVERS` 以 JSON 声明 stdio 进程，工具注册为 `ext_<server>_<tool>`；默认拒绝——只有读语义名称且无写提示的工具才注册，与内置同名则跳过，server 连接失败只跳过它自己；`user_id`/`conversation_id`/`idempotency_key`/`ticket_id`/`api_key` 从 schema 移除且每次调用再过滤，绝不转发给第三方进程。空配置或非法 JSON 不启动任何子进程。
- **分层记忆与管理**：Redis 保存工作记忆与未完成计划；ChromaDB 保存隔离的情景记忆、用户画像和开发者文档；提供记忆导出与"遗忘会话"接口（清工作记忆/摘要/情景/计划检查点），并有压缩/导出/遗忘管线质量检查。
- **记忆生命周期与真实质量评测（P5，评分口径 P6 改造）**：Redis 侧工作记忆与摘要的过期时间由 `DEVTRACE_MEMORY_TTL_SECONDS` 配置（默认 86400 秒，下限 3600，非法或未设置回落默认；Docker 实测 86400 / 7200 / 60→3600 三种取值都落到真实 Redis TTL）；ChromaDB 的情景记忆与用户画像**不过期**，只能通过显式 API 删除——`DELETE /developer/memory/{id}` 清会话，`DELETE /developer/profile` 只清调用者本人的画像（含历史格式文档），返回 `profile_deleted`/`workspace` 并写审计事件 `memory.profile_delete`，不触碰会话消息与情景记忆。记忆导出新增 `exported_at` 与 `workspace`，不返回内部画像文档 id。真实模型摘要质量由 `developer/evaluate_memory_quality_live.py` 评测（10 场 × 22 轮合成长对话、每场埋 3 个事实，只走生产压缩提示词）：评分**按事实类别判定并同时报告两列**——`strict` 要求请求编号、`密钥前缀:权限`、结论整句逐字命中；`semantic` 仍要求请求编号逐字，权限允许同义动作表述，结论按概念组命中且命中前做否定检查（"未过期"绝不计为保留"过期"）。门禁只看 `semantic`。同一 provider（`deepseek-chat`）真实复跑：编号 10/10、密钥权限 10/10，结论 `strict` 6/10、`semantic` 10/10，完美场次 `strict` 6/10 / `semantic` 10/10，0 编造标识符、0 超长摘要（最长 139 字），平均 984.9ms；strict 失分的 4 场逐条核对为标点合并与同义替换而非事实丢失。该评测仍刻意不进 CI（会花真实调用），结论概念组与样本都由作者模板生成、单一 provider，`semantic` 满分不等于事实被完整复述。
- **可追溯工具调用**：版本化文档检索、脱敏请求记录、密钥权限摘要、Webhook 投递记录和幂等工单。
- **公开 API 文档知识库**：内置 641 条文档——29 条官方摘要（GitHub、Stripe、OpenAI、DeepSeek、Anthropic、Gemini、通义千问、Twilio、Petstore、通用 429/503）+ 612 条从 Stripe 官方 OpenAPI 完整规格（MIT）导入的接口定义；支持按 API 检索并引用来源，提供 OpenAPI/Markdown 导入工具，且不连接真实平台。
- **错误处置手册**：Stripe、GitHub、Twilio 共 36 条结构化错误条目（401/403/429/400/404/409/5xx、Webhook 验签与投递），每条含症状词、含义、是否可重试、退避策略、恢复步骤和官方来源；40 条症状评测规则模式 40/40。
- **证据驱动诊断**：多条带来源的假设，区分"被观察支持""被证据否定""仅观察到"；请求记录与密钥状态互相矛盾时显式报告证据冲突，关联请求成功时用排除法而不是默认因果关系。
- **安全边界**：API Key 脱敏，工具参数从用户输入或已观察记录取得；工单必须由用户明确授权；用户文本与工具观察中的指令会在时间线中标记为不可信数据，不会作为工具指令执行。依赖侧已做漏洞核查：ChromaDB 3 条漏洞上游暂无修复版本，保持内部网络隔离并加了 CI 审计门禁，详见[依赖与安全评估](backend/developer/依赖与安全评估_2026-10-18.md)。
- **RBAC 与审计日志**：`DEVTRACE_ADMIN_USERS` 定义管理员（服务端判定，角色不进 JWT）；Redis Stream `developer:audit` 应用层只追加登录、chat、任务、工具调用、记忆与提示词事件，`detail` 走字段白名单并对疑似密钥脱敏，绝不含消息正文或工具结果；`GET /developer/audit`（仅管理员，最新优先、limit 1–200、用户/事件过滤）。审计写入 best-effort，Redis 故障不阻断任何主流程。Redis 不是 WORM 存储，该日志定位是"应用层只追加的运维审计"，不是不可篡改存证。
- **工作区隔离（P2）**：`DEVTRACE_USERS_JSON` 支持 workspace 感知的新格式（`{"user": {"password": "...", "workspace": "acme"}}`），旧格式完全兼容并归入 `default`；workspace 由服务端配置解析，**不写入 JWT**。run/task 记录携带归属 workspace，跨 workspace 读取一律 404（不泄露存在性）；审计事件带 workspace 字段（旧事件视为 `default`），管理员查询支持 `?workspace=` 过滤。记忆仍按 user+conversation 隔离，workspace 只是上层归属；未做共享 workspace 成员/协作，也未按 workspace 重新分区 ChromaDB。
- **工程化验证**：开发者 Agent 自动化测试 325 项 + 211 个 subtest（2026-10-18 起为 150 项，随 P2–P11 逐阶段增加）；独立 holdout 集 40 条（原 19 条 + P6 新增 21 条，覆盖冲突/注入/异常/多轮/公开文档），**标签全部由项目作者手写、不是第三方隐藏基准**——规则模式 40/40（原批 19/19、新批 21/21，结果按 `batch` 分别标注，CI 跑整表规则模式），真实模型模式在 **P8 跑满全量 40 条得 37/40**（原批 **19/19**、新批 **18/21**，`eval_holdout_model_2026-10-22_full40.json`；43 次 HTTP 请求、0 重试、0 基础设施失败、墙钟 487.3 秒、单请求 mean 11321.6ms，provider 未返回 usage 因此只披露请求数与时间、不估算 token 成本；P6 那轮为控成本只跑新批 21 条得 18/21，两轮新批结果一致）。3 条失败原样保留并在案例里加 `label_scope` 说明、断言未因结果而修改：2 条是标签按本地工具拼写断言、而 base compose 默认的 `DEVTRACE_MCP_PUBLIC_TOOLS=enabled` 让实际工具带上了 `mcp_` 前缀（本轮 `DEVTRACE_MCP_ALL_TOOLS` 未设置仍原样复现，说明触发条件是默认的 PUBLIC_TOOLS 而非 ALL_TOOLS；P6 归因已在评测报告中就地更正），1 条是模型未带回 v1 版本文档那条具体证据；模型仍只有一个 provider；12 条跨轮任务记忆评测（恢复 vs 重开均 12/12，恢复模式少约 0.91 次重复文档检索）；轻量并发/负载验证（rules 模式、零付费调用、单机 Docker + 2 worker）：10 并发 `/developer/chat/stream` 10/10 收到 `complete`，时延 p50 1049ms / p95 1859ms，首事件 p50 29ms，`run_id`/`task_id` 无重复，同会话并发 1×200 + 2×409、同幂等键并发 1×200 + 2×409 且完成后重放复用同一 `run_id`，同步 `/developer/chat` 36 次得到 26×200 + 10×429 与 30/min/user 窗口一致；P6 那一轮实测到的"流式入口绕过 `limit()`"（24 次流式请求 0 个 429）已在 **P7 修复并复验**：单次流式请求只消耗 1 个配额，31 并发流式 = 29×200 + 2×429 且全部带 `Retry-After: 60`，被拒请求不产生任务记录、不入队、不发 `started` 事件，烧完配额后的同步请求同样 429（共用同一把 `developer:chat-limit:<sha256(user)>`），队列化与进程内 SSE 两条路径各实测一轮；**P8 再把同一语义放到真实 provider（`DEVELOPER_MODEL_MODE=anthropic`）下复验**：不为烧配额而发 31 条会进模型的请求，改为把计数预置到 30（`EX 60`）后只发 1 条流式 + 1 条同步请求，两者都 429 + `Retry-After: 60`，任务/运行/幂等缓存/任务事件四类 Redis key 增量与 worker `task enqueued` 日志增量全为 0，拒绝体与 `chat.rate_limited` 审计 `detail` 不含消息正文，429 时延 3.7ms/46.9ms 对同栈模型执行 mean 11321.6ms——拒绝确实早于模型调用（`eval_stream_limit_model_2026-10-22.json`）；前端 429 提示也已在浏览器实测渲染；2026-10-18 已在本机 Docker 全栈复验规则模式 HTTP 30/30、SSE、断流释放锁、并发 409 与跨轮恢复，并用 Playwright 在桌面/移动端完成联调，另做键盘导航、焦点可见、抽样对比度（WCAG AA）与基础性能检查。这些是小样本、单 provider、单机演示级验证，不构成生产就绪结论。历史评测结果详见评测报告。
- **组合故障与运行记录**：`delivery_combo_01` 会触发关联请求与限流文档检查；登录后可查看最近运行，逐步观察工具执行，并查看模型及工具调用耗时。
- **流式入口限流与 Redis TTL 收敛（P7）**：`/developer/chat/stream` 在进入流或入队**之前**扣减与同步 `/developer/chat` 相同的用户级配额（`developer:chat-limit:<sha256(user)>`，30 次/60 秒），一次流式请求只消耗 1 个配额；超限直接返回 HTTP **429 + `Retry-After: 60`**（不是流内 `error` 事件），且不创建任务记录、不入队、不写 `started` 事件——边界是"连接建立之后不再中途限流"，长连接的续传/重试语义不变。同步入口的 429 同样补上 `Retry-After`。被拒请求写一条 `chat.rate_limited` 审计事件，`detail` 只含白名单字段（`kind`/`status`/`reason_code`），不含消息正文。任务记录、计划检查点、运行记录与运行历史索引、幂等结果缓存、任务事件流这些**瞬时 Redis 数据**的过期时间统一到 `DEVTRACE_REDIS_TTL_SECONDS`（`developer/ttl.py`，默认 86400、下限 60、非法或空值回落默认；Docker 实测默认 86400 与注入 120 都落到真实 key TTL），与记忆 TTL `DEVTRACE_MEMORY_TTL_SECONDS`（下限 3600）保持两个独立开关。
- **评测复验（P8，2026-10-22）**：只增加证据、不改产品代码/提示词/案例/标签/阈值/评分逻辑。真实模型（`deepseek-chat`，Anthropic 兼容端点）跑满 **40 条 holdout：37/40**（原批 19/19、新批 18/21），每条只发一次、0 重试、0 基础设施失败，3 条失败逐条给出根因分类（2 条标签绑定传输拼写 + 1 条具体证据未命中）并原样保留；真实 provider 下复验流式 429 用"预置配额 + 只发 2 条"的方式取证，避免为此烧掉 31 次模型调用。
- **评测口径修正与测试稳定性（P9，2026-10-22）**：官方 37/40 报告逐字保留，另存一份**离线补充归因**（`eval_holdout_model_2026-10-22_full40_base_names.json`，`--mode rescore`，零模型调用）：工具名按"身份与传输前缀无关"比较——只剥 `mcp_` 前缀且必须命中本地注册表 `developer.planner.TOOL_SCHEMAS`，未知名原样保留，`assess()` 默认口径不变。补充口径下 **39/40**：只有 `h_public_playbook_path`、`h_public_docs_path` 因 `missing_tool:*` 被消除而 pass（同一份 observed，只说明标签绑定对上，**不代表能力提升**），`h_quota_v2_doc_version_boundary` 仍失败、原因逐字不变，3 条多轮行拒绝重算并写明原因。同轮把 P8 记录的时间敏感用例改成事件驱动同步（根因不是"机器太忙拖过 `sleep(0.05)`"，而是单次 `Task.cancel()` 会被生成器自己的 `asyncio.wait_for(queue.get(), 15)` 吞掉，加上对已取消任务"异常形态"的断言本身依赖调度点）；单跑 20/20、CPU 满载下再 20/20、pytest 挑该用例 10/10。整表 258 → **278 项通过**，CI Ruff 清单 54 → 55 个文件。

- **限流降级与情景记忆保留（P10，2026-10-22）**：限流路径的 Redis/Lua **基础设施故障**改为 fail-open——记 warning 日志并写一条 `<kind>.rate_limit_unavailable` 审计事件（`detail` 仍只含 `kind`/`status`/`reason_code` 白名单字段），请求继续处理，不再把 Redis 抖动放大成全量 500；这是可用性优先的演示级取舍，故障窗口内配额确实不再生效，生产应由网关或多副本限流兜底。真正的超限（`HTTPException` 429）绝不吞：登录、同步与流式超限一律 429，登录也补上 `Retry-After: 60` 与 chat 对齐。固定窗口"被拒请求同样 `INCR`"的既有语义**未改**（容器实测 31 次尝试后计数为 31，含被拒的那次）。边界要说清：fail-open 只覆盖限流器本身，chat 主流程仍依赖 Redis 会话锁，因此 Redis 完全不可用时诊断请求会在锁获取阶段失败，登录路径不依赖 Redis 所以只降级不限流。新增 `python -m developer.memory_retention --days N`：默认 `--dry-run` 只统计（候选数、按用户、涉及会话、无可用 `ts` 的跳过数），`--apply` 才删除，`--user` 只能缩小范围，`ts` 缺失/不可解析的记录**不猜年龄、不删除**，含 `profile` 的集合名直接拒绝，Chroma 读写失败可读退出且非零——情景记忆从此有了显式保留策略，用户画像仍然只能经 API 显式删除。整表 278 → **304 项通过**，CI Ruff 清单 55 → 58 个文件（本机复现需 `PYTHONUTF8=1`：Windows 下 MCP stdio 那条既有用例依赖子进程输出编码，与本轮改动无关，已在评测报告如实记录——该编码依赖已在 P11 修复，此处保留历史表述）；真实栈取证见评测报告 P10 一节。
- **MCP stdio 编码与保留 CLI 分页（P11，2026-10-22）**：两处低风险工程修复，不改产品 API、评分、案例、标签或提示词。① MCP stdio 明确 UTF-8：服务端 `python -m mcp.server` 在读写前对 `sys.stdin`/`sys.stdout` 调 `reconfigure(encoding='utf-8')`（无法 reconfigure 的流跳过而不报错），客户端 `StdioTransport` 起子进程时注入 `PYTHONUTF8=1`（**有意覆盖**继承来的 `PYTHONUTF8=0`），线上仍是"每行一个 JSON 对象"的 NDJSON、`ensure_ascii=False`、协议字段与顺序不变；Windows 下重定向 stdio 默认按 GBK 编码，中文工具描述因此会在客户端解码失败——修复后整表在 Windows 上**不再需要 `PYTHONUTF8=1`**。② 保留 CLI 分页扫描：`--page-size`（默认 500，范围 1..5000，非法退出码 2）按 `limit`/`offset` 逐页读取，报告新增 `pages`/`page_size`，dry-run 与 apply 都按页累计统计与候选 id，删除仍按 200 一批，**分页只改变读取批次与内存占用，不改变任何判定**（真实 Chroma 实测：同一集合 `--page-size 100/500/5000` 的 scanned/candidates/kept_fresh/skipped_no_ts 完全一致）；候选 id 去重，客户端不支持或忽略 `limit`/`offset` 时退化为一次全量读取而不是死循环，同一条记录绝不会被删两次。整表 304 → **325 项通过**，CI Ruff 清单 58 → 59 个文件。
- **持久流式任务（队列生产化 P3）**：SSE 诊断由 Redis 队列 + 独立 worker（`python -m api.worker`）执行，仍然只用 Redis，不引入 Celery/RabbitMQ/Kafka。三级优先级队列 `developer:tasks:queue:high|normal|low`（默认 normal，未知值报错）+ 旧队列兼容，worker 用多键 `BRPOP` 按 high→normal→low→legacy 顺序领取；任务记录带**租约**（`lease_id`/`lease_until`，`DEVTRACE_TASK_LEASE_SECONDS` 默认 90 秒、下限 30 秒，运行中每 30 秒仅由持有者续期），`reclaim_expired()` 用 `SET NX EX` 互斥锁回收租约过期的 running 任务（多 worker 只回收一次）；失败重试走**延迟队列** `developer:tasks:delayed`（有序集合，退避 1s/2s/4s，上限 30s），到点由任一 worker 原子搬回原优先级队列，worker 崩溃不丢延迟任务；死信列表可由管理员 `GET /developer/dead-letter` 查看、`POST /developer/tasks/{id}/replay` 重放（重置次数、回到原优先级，审计 `task.replayed`）。API 重启不丢排队任务，worker 启动时仍会回收中断任务（无租约字段的旧记录沿用 P2 的启动回收语义）；任务视图不暴露租约内部字段。
- **队列可观测性**：入队时生成 32 位 `trace_id`，贯通任务记录、SSE `started` 事件、worker 结构化日志与全部审计事件；`GET /developer/metrics`（仅管理员）返回各优先级队列长度、旧队列长度、延迟任务数、死信数、索引中任务数、近期状态分布与在线 worker 心跳数。审计字段语义与脱敏白名单未变；指标为 best-effort 快照，worker 心跳键有 90 秒 TTL，容器强制重建后旧心跳最长残留 90 秒。

### 架构

```text
Vue 3 UI
   │ POST /developer/chat (sync) / chat/stream (queue + SSE)
FastAPI plan executor
   ├── Redis task queue ── worker (python -m api.worker)
   ├── Anthropic-compatible decision model / rule fallback
   ├── Redis + ChromaDB memory
   ├── local tool manager
   └── FastAPI mock developer platform + SQLite tickets
```

十个核心模块（LLM、记忆、规划、工具、执行、观察、状态调度、提示词、安全、输出）的逐项对照、实现位置和差距见 [智能体核心模块说明](backend/developer/智能体核心模块说明.md)。

### 项目结构

```text
backend/   FastAPI、Agent 执行器、记忆、模拟平台、测试与技术文档
frontend/  Vue 3 + Vite 对话页，展示计划、行动、证据和记忆使用情况
```

### 本地运行

1. 在 `backend/` 执行 `python setup_local.py`，创建本地演示账号和密钥配置；已有 `.env` 不会覆盖。
2. 使用真实模型时在本地 `backend/.env` 填写模型 API Key；规则模式可设置 `DEVELOPER_MODEL_MODE=rules`。
3. 在 `backend/` 执行 `docker compose up -d --build`。若构建网络无法访问 ChromaDB 的 ONNX 模型地址，可先 `docker compose build --build-arg PRELOAD_ONNX=false`，再执行 `docker compose up -d`；此时使用独立 ChromaDB 服务端的嵌入能力。
4. 在 `frontend/` 执行 `npm ci && npm run dev`。若后端不在默认 8000 端口，用 `VITE_PYTHON_API_URL` 指定，例如 `$env:VITE_PYTHON_API_URL='http://127.0.0.1:18002'; npm run dev -- --port 5174`。
5. 登录后使用 `req_auth_01`、`req_quota_01` 或 `delivery_01` 演示。

接口使用本地配置账号登录并签发短期 JWT，身份由服务端验证；管理员角色由 `DEVTRACE_ADMIN_USERS` 服务端判定，`GET /developer/audit` 返回应用层只追加的审计事件；同一用户的会话通过 Redis 锁防止并发覆盖，重复请求可携带 `Idempotency-Key`。模拟平台仅在 Compose 内部网络访问。用户可归属到服务端配置的工作区（跨工作区读取 404、审计按工作区过滤），但这仍是本地演示：没有共享工作区的成员/协作、生产身份服务与真实业务权限，ChromaDB 也未按工作区分区。

详细设计、工程审查、评测原始结果见 [backend/developer](backend/developer)。
本轮优化的实现范围、测试结果和剩余工作见 [升级进度](backend/developer/升级进度.md)。

## English

DevTrace is a portfolio-ready developer API diagnosis agent. It demonstrates constrained multi-step planning, ReAct-style tool use, Redis/ChromaDB memory, evidence-backed answers, and a Vue visualization for 401 authentication, 429 rate-limit, and Webhook delivery failures.

The project intentionally uses a local mock platform and masked records. It does not connect to production accounts, logs, or ticketing systems.

## Verification

```powershell
cd backend
python -m pytest -q tests
python -m developer.evaluate_holdout --mode rules --output developer/eval_holdout.json
python -m developer.evaluate_holdout --mode rules --batch 2026-10-21 --output developer/eval_holdout.json   # one label batch; unknown batch is an error
python -m developer.evaluate_holdout --mode rescore --source developer/eval_holdout_model_2026-10-22_full40.json --output developer/eval_holdout_model_2026-10-22_full40_base_names.json   # offline base-name re-attribution; official score untouched
python -m developer.evaluate_memory_tasks --output developer/eval_memory_tasks.json
python -m developer.evaluate_memory_quality --output developer/eval_memory_quality.json
python -m developer.evaluate_memory_quality_live --output developer/eval_memory_quality_live.json   # real model calls; skipped with exit 0 when no key
python -m developer.evaluate_public_docs --output developer/eval_public_docs.json
python -m developer.audit_gate dependency-audit.json   # pip-audit JSON; blocks only fixable findings
python -m developer.memory_retention --days 90         # episodic retention plan; dry-run by default, --apply deletes, profiles never eligible
python -m developer.memory_retention --days 90 --page-size 1000 --apply   # paged scan (1..5000, default 500); paging changes memory use only, never verdicts

cd ../frontend
npm ci
npm run build
```

Dependency status and the unfixed ChromaDB advisories are documented in [backend/developer/依赖与安全评估_2026-10-18.md](backend/developer/依赖与安全评估_2026-10-18.md).

See [backend/developer/evaluation_report.md](backend/developer/evaluation_report.md) for measured test and HTTP evaluation results.

**演示与生产差距（结论）**：本项目是可在单机 Docker 复现的能力演示，不是生产就绪系统。已验证的边界包括：单租户 + 单 Redis + 单 Chroma 容器（workspace 只是归属与可见性边界，未按租户物理分区）；审计是应用层只追加、Redis 不是 WORM 存储；ChromaDB 0.5.23 有 3 条上游未修复漏洞，只能靠内网隔离与 CI 审计门禁（见依赖与安全评估）；Chroma 侧没有 TTL，情景记忆自 P10 起由 `python -m developer.memory_retention --days N` 做显式保留（默认 dry-run、需人工或外部调度触发，画像仍只能经 API 显式删除）；外部 MCP server 只用自带 fake server 在单测里验证过，未接入真实第三方进程；评测标签由项目作者手写、样本小、模型只来自一个 provider；负载验证是 rules 模式 n=10 的存在性检查。流式入口自 P7 起与同步入口共用 30 次/60 秒用户级限流（P8 已在真实 provider 下复验拒绝路径无副作用、且早于模型调用），但限流窗口仍是单 Redis 固定窗口、没有按 IP/租户分层配额，也没有网关级限流；Redis 不可达或 Lua 脚本抛错时，限流器自 P10 起对基础设施异常 fail-open 并写审计（不是全量 500），但这是可用性优先的演示级取舍，故障窗口内配额不再生效，且 chat 主流程仍依赖 Redis 会话锁。身份服务、TLS、密钥轮换、生产限流与容灾均未实现。
