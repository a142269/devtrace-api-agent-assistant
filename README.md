# DevTrace——开发者平台 API 接入 Agent 助手

[English](#english) | [中文](#中文)

## 中文

面向开发者 API 接入排障的单 Agent 项目。这是一个独立运行的开发者支持项目，围绕三类可复现问题演示受约束的多步 Agent 流程：**401 鉴权失败、429 限流、Webhook 投递失败**。

Agent 从会话记忆和历史证据开始，生成诊断计划，调用文档、请求记录、密钥权限或 Webhook 工具，并将每次观察写入状态。模型可以在服务端的证据和依赖约束内改变行动顺序；服务端负责工具白名单、参数来源、工单授权、脱敏与失败降级。

### 技术亮点

- **ReAct 与 Plan-and-Execute**：显示计划提议、服务端批准计划、工具行动、观察和状态变化；不展示内部推理。
- **分层记忆**：Redis 保存工作记忆与未完成计划；ChromaDB 保存隔离的情景记忆、用户画像和开发者文档。
- **可追溯工具调用**：版本化文档检索、脱敏请求记录、密钥权限摘要、Webhook 投递记录和幂等工单。
- **公开 API 文档知识库**：内置 GitHub、Stripe、OpenAI、Twilio、Petstore 等官方公开文档摘要（每条带来源链接与许可说明），支持按 API 检索并在答复中引用来源；提供 OpenAPI/Markdown 导入工具，可替换为自己的文档，且不连接真实平台。
- **证据驱动诊断**：多条带来源的假设，区分"被观察支持""被证据否定""仅观察到"；请求记录与密钥状态互相矛盾时显式报告证据冲突，关联请求成功时用排除法而不是默认因果关系。
- **安全边界**：API Key 脱敏，工具参数从用户输入或已观察记录取得；工单必须由用户明确授权；用户文本与工具观察中的指令会在时间线中标记为不可信数据，不会作为工具指令执行。依赖侧已做漏洞核查：ChromaDB 3 条漏洞上游暂无修复版本，保持内部网络隔离并加了 CI 审计门禁，详见[依赖与安全评估](backend/developer/依赖与安全评估_2026-10-18.md)。
- **工程化验证**：开发者 Agent 自动化测试 76 项；新增 19 条手写标签的独立 holdout 集（冲突/注入/异常/多轮），规则模式与真实模型模式均为 19/19；新增 12 条跨轮任务记忆评测（恢复 vs 重开均 12/12，恢复模式少约 0.91 次重复文档检索）；2026-10-18 已在本机 Docker 全栈复验规则模式 HTTP 30/30、SSE、断流释放锁、并发 409 与跨轮恢复，并用 Playwright 在桌面/移动端完成联调，另做键盘导航、焦点可见、抽样对比度（WCAG AA）与基础性能检查。历史评测结果详见评测报告。
- **组合故障与运行记录**：`delivery_combo_01` 会触发关联请求与限流文档检查；登录后可查看最近运行，逐步观察工具执行，并查看模型及工具调用耗时。

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

1. 在 `backend/` 执行 `python setup_local.py`，创建本地演示账号和密钥配置；已有 `.env` 不会覆盖。
2. 使用真实模型时在本地 `backend/.env` 填写模型 API Key；规则模式可设置 `DEVELOPER_MODEL_MODE=rules`。
3. 在 `backend/` 执行 `docker compose up -d --build`。若构建网络无法访问 ChromaDB 的 ONNX 模型地址，可先 `docker compose build --build-arg PRELOAD_ONNX=false`，再执行 `docker compose up -d`；此时使用独立 ChromaDB 服务端的嵌入能力。
4. 在 `frontend/` 执行 `npm ci && npm run dev`。若后端不在默认 8000 端口，用 `VITE_PYTHON_API_URL` 指定，例如 `$env:VITE_PYTHON_API_URL='http://127.0.0.1:18002'; npm run dev -- --port 5174`。
5. 登录后使用 `req_auth_01`、`req_quota_01` 或 `delivery_01` 演示。

接口使用本地配置账号登录并签发短期 JWT，身份由服务端验证；同一用户的会话通过 Redis 锁防止并发覆盖，重复请求可携带 `Idempotency-Key`。模拟平台仅在 Compose 内部网络访问。当前实现仍是本地演示：尚不具备生产身份服务、持久任务队列或真实业务权限。

详细设计、工程审查、评测原始结果见 [backend/developer](backend/developer)。
本轮优化的实现范围、测试结果和剩余工作见 [升级进度](backend/developer/升级进度.md)。

## English

DevTrace is a portfolio-ready developer API diagnosis agent. It demonstrates constrained multi-step planning, ReAct-style tool use, Redis/ChromaDB memory, evidence-backed answers, and a Vue visualization for 401 authentication, 429 rate-limit, and Webhook delivery failures.

The project intentionally uses a local mock platform and masked records. It does not connect to production accounts, logs, or ticketing systems.

## Verification

```powershell
cd backend
python -m unittest discover -s tests
python -m developer.evaluate_holdout --mode rules --output developer/eval_holdout.json
python -m developer.evaluate_memory_tasks --output developer/eval_memory_tasks.json
python -m developer.evaluate_public_docs --output developer/eval_public_docs.json
python -m developer.audit_gate dependency-audit.json   # pip-audit JSON; blocks only fixable findings

cd ../frontend
npm ci
npm run build
```

Dependency status and the unfixed ChromaDB advisories are documented in [backend/developer/依赖与安全评估_2026-10-18.md](backend/developer/依赖与安全评估_2026-10-18.md).

See [backend/developer/evaluation_report.md](backend/developer/evaluation_report.md) for measured test and HTTP evaluation results.
