# 开发者平台 API 接入助手

这是独立的 DevTrace 开发者 API 接入诊断项目。默认使用现有 Anthropic 兼容配置。没有模型服务时，可设置 `DEVELOPER_MODEL_MODE=rules`，用确定性参考模型演示工具和计划状态；此模式不代表大模型规划效果。

## 阅读路线

- [完整构建与模块说明](完整构建与模块说明.md)：从数据、工具、计划、记忆到 API/UI 的构建顺序，并逐项对照 Agent 十模块。
- [工程化审查](工程化审查.md)：与原客服项目对照、已实现范围、生产级差距与面试表述。
- [实测评测报告](evaluation_report.md)：自动化测试、HTTP、真实配置模型及界面验证结果。

## 运行

- 全栈：在 `backend` 执行 `docker compose up -d --build`。
- 模拟平台单独运行：`python -m uvicorn developer.sandbox:app --port 8100`。
- Python 服务独立运行时，设置 `DEVELOPER_SANDBOX_URL=http://localhost:8100`，并配置 Redis、ChromaDB 与模型环境变量。

模拟平台的固定记录为 `req_auth_01`～`req_auth_10`、`req_quota_01`～`req_quota_10`、`delivery_01`～`delivery_10`，另有 `delivery_combo_01`～`delivery_combo_10` 组合案例。HTTP 接口提供脱敏请求、密钥前缀权限和投递记录；SQLite 保存模拟工单。

`req_auth_v2_01` 演示先读取请求记录确认 API 版本，再检索 v2 鉴权文档；版本切换覆盖目前仅限这个模拟案例。

## API

先用本地账号调用 `POST /auth/login`，在以下请求头中传入 `Authorization: Bearer <token>`。`POST /developer/chat` 请求示例：

```json
{"message":"请求 req_auth_01 返回 401，帮我排查","conversation_id":null}
```

响应包含 `conversation_id`、`run_id`、`answer`、`plan`、`actions`、`timeline`、`evidence`、`diagnosis`、`memory_used`、`metrics`、可选 `ticket_id` 和 `decision_mode`。`decision_mode` 区分 `model`、`rules` 和 `rules_fallback`。等待补充时可用同一 `conversation_id` 继续。`GET /developer/runs` 和 `GET /developer/runs/{run_id}` 根据认证身份授权；客户端不能指定 `user_id`。

流式接口的 `started` 事件提供独立的 `task_id`；`GET /developer/tasks/{task_id}` 可查询该用户的状态。断开连接时记录 `cancelled`，同会话可从最近检查点继续。进程异常退出时 `running` 状态不会自动修复。

## 设计边界

- 计划生成和下一行动选择可由模型决定；服务端限制工具白名单、从用户或查询结果获取标识符，并控制单轮最多 5 次行动与 1 次计划调整。
- 模型提出的合法工具顺序会影响批准计划；服务端补齐必需证据、修正依赖，并只允许从当前可执行步骤中选择行动。`plan.proposed_tools`、`approved_tools` 和 `adjustments` 可用于对照审查。最终答复由成功的工具观察整理生成。开发者工具是项目内注册与调用，并未提供独立 MCP 协议服务。
- Redis 保存 24 小时计划状态和运行记录；开发者记忆使用独立键前缀、`developer_episodic` 和 `developer_profile` collection；文档位于独立的 `developer_knowledge` collection。
- 回复、界面和工单均说明数据来自本地模拟平台；不接入真实开发者账号、生产日志或工单系统。
- JWT 仅提供本地演示身份，仍需生产身份服务、TLS、密钥轮换和真实业务权限。请求幂等结果在完成后缓存；进程在副作用与结果缓存之间崩溃时并不保证通用操作恰好一次。模拟工单自身另有去重。

## 验证

`python -m unittest discover -s tests -p test_developer_assistant.py` 覆盖 30 条固定案例、跨轮恢复、错误标识后调整、工单去重、用户隔离与敏感信息脱敏。实测结果见 [evaluation_report.md](evaluation_report.md)；模型在线演示需要有效的模型 API Key。

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
```

检索评测需要可用的 ChromaDB 或本地向量模型。规则规划和故障注入使用进程内模拟平台，不能视作模型的泛化评估。
