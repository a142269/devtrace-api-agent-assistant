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

流式接口的 `started` 事件提供独立的 `task_id`；`GET /developer/tasks/{task_id}` 可查询该用户的状态。断开连接时记录 `cancelled`，同会话可从最近检查点继续。任务记录带心跳时间：SSE 事件与 15 秒空闲心跳都会刷新 `updated_at`，`running` 超过 240 秒未更新会在查询时改为 `interrupted`，进程启动时也会把上一进程残留的 `running` 一并修正（仅适用于单进程/单副本共享 Redis）。没有持久任务队列，也不会自动重跑中断任务，中断后需在同一会话发送“继续”。

## 设计边界

- 计划生成和下一行动选择可由模型决定；服务端限制工具白名单、从用户或查询结果获取标识符，并控制单轮最多 5 次行动与 1 次计划调整。
- 模型提出的合法工具顺序会影响批准计划；服务端补齐必需证据、修正依赖，并只允许从当前可执行步骤中选择行动。`plan.proposed_tools`、`approved_tools` 和 `adjustments` 可用于对照审查。最终答复由成功的工具观察整理生成。开发者工具是项目内注册与调用，并未提供独立 MCP 协议服务。
- Redis 保存 24 小时计划状态和运行记录；开发者记忆使用独立键前缀、`developer_episodic` 和 `developer_profile` collection；文档位于独立的 `developer_knowledge` collection。
- 回复、界面和工单均说明数据来自本地模拟平台；不接入真实开发者账号、生产日志或工单系统。
- JWT 仅提供本地演示身份，仍需生产身份服务、TLS、密钥轮换和真实业务权限。请求幂等结果在完成后缓存；进程在副作用与结果缓存之间崩溃时并不保证通用操作恰好一次。模拟工单自身另有去重。

## 验证

`python -m unittest discover -s tests` 覆盖 107 项测试：30 条固定案例、跨轮恢复、错误标识后调整、工单去重、用户隔离与敏感信息脱敏，以及证据冲突、关联请求排除、工具记录/用户消息中的提示注入和任务心跳修复。实测结果见 [evaluation_report.md](evaluation_report.md)；模型在线演示需要有效的模型 API Key。

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

原生 tool calling：`AnthropicDecisionModel.choose_action` 优先把 ready steps 的 JSON Schema 作为 Anthropic `tools` 传给模型（`tool_choice=auto`），校验 `tool_use` 参数后执行；不支持的 provider 自动回退 JSON/规则，指标 `native_tool_calls` 可观察。

提示词版本化：`developer/prompts.py` 集中五个模板并带 `PROMPT_VERSION`；`python -m developer.evaluate_prompts` 校验渲染快照与安全契约，CI 已运行；修改提示词后需 `--update` 并审阅 diff。`GET /developer/prompts` 只返回版本与字符数。

MCP 协议：`mcp/server.py` 提供 JSON-RPC 2.0 的 `initialize/tools/list/tools/call` 与 stdio 入口，`mcp/client.py` 提供进程内/stdio 传输；设置 `DEVTRACE_MCP_PUBLIC_TOOLS=enabled` 后 `public_api` 路径改用 `mcp_lookup_error_playbook`/`mcp_search_public_docs`。`python -m developer.evaluate_mcp` 对 10 条 MCP 路径问题做回归（2026-10-18 为 10/10）。

记忆管理：`GET /developer/memory/{conversation_id}` 导出工作记忆/摘要/画像，`DELETE` 清理工作记忆/摘要/情景/计划检查点（画像保留）；`python -m developer.evaluate_memory_quality` 用确定性摘要器检查压缩保留/隔离/遗忘管线（2026-10-18 通过），不代表真实模型摘要质量。

候选计划评分：`python -m developer.evaluate_planning_candidates` 检查 approved/evidence_first/minimal 三候选打分与选择（2026-10-18 为 11/11）；`DEVTRACE_PLANNING_CANDIDATES=enabled` 时启用，响应 `plan.candidates` 暴露候选与分数。

`evaluate_holdout` 使用 40 条手写标签的独立 holdout 集（`eval_holdout_cases.json` 与专用夹具；`2026-10-18` 批 19 条 + `2026-10-21` 批 21 条），规则模式在进程内运行；模型模式需额外传 `--mode model --base-url <地址>` 并设置 `DEVTRACE_EVAL_TOKEN`，`--batch <批次>` 只跑一个标签批次（未知批次直接报错，不会空跑）。结果按 `batch` 标注并在 `summary.by_batch` 分组。2026-10-21 规则模式整表 40/40（19/19 + 21/21）；为控成本真实模型只跑了新批 21 条，得 **18/21**（2 条因标签按本地工具拼写断言而验证栈开着 `DEVTRACE_MCP_ALL_TOOLS`，1 条未带回指定版本文档证据），3 条失败原样保留在报告里、断言未因结果而修改。样本小、标签由项目作者手写，不等同于第三方隐藏评测。`evaluate_memory_tasks` 用 12 条两轮任务比较同会话恢复与新会话重开（规则模式、stub 工作记忆），衡量跨轮任务成功与重复检索，不代表 ChromaDB 情景记忆或生产延迟。

检索评测需要可用的 ChromaDB 或本地向量模型。规则规划和故障注入使用进程内模拟平台，不能视作模型的泛化评估。

依赖治理：`python -m developer.audit_gate <pip-audit.json>` 只对存在修复版本的漏洞返回非零；无上游修复版本的记录会转为非阻断提示。漏洞核查与处置决定见 [依赖与安全评估](依赖与安全评估_2026-10-18.md)，扫描现状也记录在[升级进度](升级进度.md)中。
