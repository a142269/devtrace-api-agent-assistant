# GitHub 仓库展示配置（复制到仓库 Settings）

> 当前环境没有 `gh`/token，无法直接改 GitHub 元数据；下面的文案请粘贴到仓库 **Settings → General** 和 **Releases**。

## 1. About / Description（≤350 字符）

推荐：

```text
DevTrace：面向开发者平台 API 接入排障的受约束单 Agent。规划/工具/MCP/分层记忆/SSE/队列/审计；371 测试、40 条 holdout、可复核评测。演示级，非生产就绪。
```

英文备选：

```text
DevTrace — a constrained single-agent diagnosis system for developer-platform APIs (401/429/Webhook). Planning, MCP tools, tiered memory, SSE, Redis queues, audit. 371 tests; demo-grade, not production-ready.
```

## 2. Topics（逐个添加）

```text
ai-agent
llm
fastapi
vue3
redis
chromadb
mcp
model-context-protocol
rag
sse
distributed-queue
observability
rbac
audit-logging
evaluation
developer-tools
portfolio-project
```

## 3. Website

如果有 demo 地址就填；没有就留空，不要填 localhost。

## 4. Social preview / Pin 文案

**Pin 标题**：`DevTrace — Developer API Diagnosis Agent`

**Pin 描述**：

```text
受约束单 Agent：计划评分 → 工具/MCP → 证据诊断 → 记忆 → 流式队列 → 审计。
371 测试、40 条 holdout、真实 kill -9 恢复；演示级，非生产就绪。
```

**Social preview 图片**：建议用架构图截图（`docs/ARCHITECTURE.md` 的 Mermaid 渲染），或前端“计划/证据”页面截图；不要放含账号/Key 的图。

## 5. Release（tag `demo-2026-10-23.1`）

**Title**：

```text
demo-2026-10-23.1 — 演示级收尾
```

**Body**：

```markdown
演示级快照，**不是生产就绪系统**。

- 371 tests + 219 subtests；CI #44 全绿
- 40 条 holdout：规则 40/40；真实模型 37/40（失败项保留并分类）
- 记忆摘要：strict 6/10、semantic 10/10（两列都报告）
- 真实 `kill -9` worker 恢复验证；SSE 取消/stdio 编码/retention 分页/会话锁归属已收口
- 已知限制：单租户、单 Redis/Chroma、无 WORM、外部 MCP 未接真实第三方、标签作者手写

详见仓库 `docs/FINAL_REVIEW.md`、`docs/INTERVIEW.md`。
```

## 6. 仓库首页建议顺序

1. `PORTFOLIO.md`（一页速览，已新增）
2. `README.md`（技术细节）
3. `docs/ARCHITECTURE.md`
4. `docs/DEMO.md`
5. `backend/developer/evaluation_report.md`
6. `docs/FINAL_REVIEW.md`（已入库）

## 7. `FINAL_REVIEW` 已复制进仓库

已从工作区根复制为 `docs/FINAL_REVIEW.md`，不需要再手工复制。

提交前确认没有凭据/账号/Key；该文档只含项目数字与限制。

## 8. 不要做的事

- 不要把 `.env`、`.devtrace-demo-credential`、API Key 截图放进 README/Release；
- 不要宣称“生产可用/高可用/多租户”；
- 不要为了展示改测试通过数；37/40 就写 37/40；
- 不要 force push 已发布 tag；新内容用新 commit 或新 tag。
