# DevTrace 简历项目段落

> 定位：**个人项目 / 演示级完整 Agent**。可直接改写进中文简历；英文版在后半部分。
> 不要写“生产就绪/高可用/多租户平台”；目标岗位是 AI Agent 应用、后端、平台工程时都能用。

## 中文版（推荐 3–4 条）

**DevTrace — 开发者平台 API 接入排障 Agent（个人项目）**  
技术栈：Python / FastAPI / Vue 3 / Redis / ChromaDB / MCP / Docker / GitHub Actions

- 独立设计并实现面向 401、429、Webhook 失败的受约束单 Agent，覆盖规划、工具调用、分层记忆、证据诊断、SSE 流式任务、队列、RBAC/审计等 10 个模块；371 项测试 + 219 subtests，GitHub Actions 全绿。
- 实现标准 MCP JSON-RPC 2.0 client/server，托管 8 个只读工具，支持外部 stdio server 注册与默认拒绝白名单；写操作工单保留本地授权，避免把授权/幂等上下文交给模型或第三方。
- 搭建 Redis 队列生产化能力：优先级、任务租约、过期回收、延迟重试、死信重放、trace_id/指标；用真实 `docker kill -9` 验证幸存 worker 回收与唯一终态。
- 建立可复核评测：30 条固定案例 30/30，40 条 holdout 规则 40/40、真实模型 37/40，失败项保留并分类；分层记忆真实模型摘要 strict 6/10、semantic 10/10，两列同时报告。

**可选第 5 条（安全/工程化）**

- 实现 RBAC + append-only 审计、workspace 隔离、用户级限流与流式取消修复；所有结论附带评测 JSON、CI run、tag 与限制清单，不把演示级包装成生产就绪。

## 中文精简版（一页简历空间不足时）

**DevTrace — 开发者平台 API 排障 Agent（个人项目）**

独立实现受约束单 Agent（规划/工具/MCP/记忆/SSE/队列/审计），覆盖 401/429/Webhook 场景；371 项测试、CI 全绿，40 条 holdout 规则 40/40、真实模型 37/40；用真实 `kill -9` 验证队列租约恢复。演示级，非生产就绪。

## 英文版（3 bullets）

**DevTrace — Developer Platform API Diagnosis Agent (Personal Project)**  
Python, FastAPI, Vue 3, Redis, ChromaDB, MCP, Docker, GitHub Actions

- Designed and built a constrained single-agent diagnosis system for 401/429/Webhook failures, covering planning, tool use, tiered memory, evidence-backed diagnosis, SSE streaming, queues, RBAC and audit logging; 371 tests + 219 subtests, CI green.
- Implemented a JSON-RPC 2.0 MCP client/server hosting 8 read-only tools, with an external stdio registry that defaults to deny; kept the write ticket action local to preserve user authorization and idempotency context.
- Built production-oriented queue mechanics (priority, leases, expired-lease reclaim, delayed retries, dead-letter replay, trace IDs) and validated crash recovery with a real `docker kill -9`; evaluation is reproducible with 40-case holdout (40/40 rules, 37/40 real model) and preserved failures.

## 关键词（ATS/筛选）

`Python` `FastAPI` `AI Agent` `LLM` `RAG` `MCP` `Redis` `ChromaDB` `Vue 3` `Docker` `GitHub Actions` `SSE` `distributed queue` `observability` `RBAC` `audit logging` `evaluation` `prompt engineering`

## 按岗位裁剪

- **AI Agent / LLM 应用**：保留第 1、2、4 条，强调规划、工具约束、MCP、记忆评测；弱化队列细节。
- **后端 / 平台工程**：保留第 1、3、5 条，强调 Redis 队列、锁语义、审计、限流、CI；MCP 一句带过。
- **全栈**：保留第 1、2、4 条，补一句 Vue 3 展示计划/证据/记忆；不要夸大 UI 工作量。
- **校招/转行**：项目标题下加“独立完成”，面试重点讲“怎么验证”和“哪里没做好”，比堆技术名词有效。

## 面试时主动补充的三句话

1. “它是演示级完整，不是生产就绪；身份/TLS/WORM/HA 我明确没做。”
2. “真实模型 holdout 是 37/40，2 条是标签绑定，1 条是证据未命中；我没有改断言。”
3. “如果继续，我先补身份/多租户和高可用，不会再加多 Agent 花活。”

## 不要写进简历的表述

- ❌ 生产可用 / 高可用 / 多租户 SaaS
- ❌ 40/40 全过（真实模型 37/40）
- ❌ 多 Agent 协作 / 自动修复线上故障
- ❌ 不可篡改审计 / 修复 Chroma 漏洞
- ❌ 管理过团队/带人（除非真实经历）

## 可验证链接

- 仓库：https://github.com/a142269/devtrace-api-agent-assistant
- 发布 tag：https://github.com/a142269/devtrace-api-agent-assistant/releases/tag/demo-2026-10-23.1
- 项目速览：`PORTFOLIO.md`
- 最终验收/限制：`FINAL_REVIEW.md`
