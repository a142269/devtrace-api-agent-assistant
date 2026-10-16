# EchoMind Developer API Integration Assistant

[English](#english) | [中文](#中文)

## 中文

面向开发者 API 接入排障的单 Agent 项目。它在原客服系统旁新增独立的开发者支持场景，围绕三类可复现问题演示受约束的多步 Agent 流程：**401 鉴权失败、429 限流、Webhook 投递失败**。

Agent 从会话记忆和历史证据开始，生成诊断计划，调用文档、请求记录、密钥权限或 Webhook 工具，并将每次观察写入状态。模型可以在服务端的证据和依赖约束内改变行动顺序；服务端负责工具白名单、参数来源、工单授权、脱敏与失败降级。

### 技术亮点

- **ReAct 与 Plan-and-Execute**：显示计划提议、服务端批准计划、工具行动、观察和状态变化；不展示内部推理。
- **分层记忆**：Redis 保存工作记忆与未完成计划；ChromaDB 保存隔离的情景记忆、用户画像和开发者文档。
- **可追溯工具调用**：版本化文档检索、脱敏请求记录、密钥权限摘要、Webhook 投递记录和幂等工单。
- **安全边界**：API Key 脱敏，工具参数从用户输入或已观察记录取得；工单必须由用户明确授权。
- **工程化验证**：32 个 Python 测试、30 个固定案例子测试；规则模式 30/30 HTTP 评测通过，配置模型在三类代表案例中 3/3 通过。

### 架构

```text
Vue 3 UI
   │ POST /developer/chat
FastAPI plan executor
   ├── Anthropic-compatible decision model / rule fallback
   ├── Redis + ChromaDB memory
   ├── local tool manager
   └── FastAPI mock developer platform + SQLite tickets
```

### 项目结构

```text
backend/   FastAPI、Agent 执行器、记忆、模拟平台、测试与技术文档
frontend/  Vue 3 + Vite 对话页，展示计划、行动、证据和记忆使用情况
```

### 本地运行

1. 复制 `backend/.env.example` 为 `backend/.env`，填写自己的模型 API Key。
2. 在 `backend/` 执行 `docker compose up -d --build`。
3. 在 `frontend/` 执行 `npm ci && npm run dev`。
4. 打开前端后切换到“开发者助手”，可使用 `req_auth_01`、`req_quota_01` 或 `delivery_01` 演示。

详细设计、工程审查、评测原始结果见 [backend/developer](backend/developer)。

## English

A portfolio-ready developer API diagnosis agent. It demonstrates constrained multi-step planning, ReAct-style tool use, Redis/ChromaDB memory, evidence-backed answers, and a Vue visualization for 401 authentication, 429 rate-limit, and Webhook delivery failures.

The project intentionally uses a local mock platform and masked records. It does not connect to production accounts, logs, or ticketing systems.

## Verification

```powershell
cd backend
python -m pytest -q tests/test_developer_assistant.py

cd ../frontend
npm ci
npm run build
```

See [backend/developer/evaluation_report.md](backend/developer/evaluation_report.md) for measured test and HTTP evaluation results.
