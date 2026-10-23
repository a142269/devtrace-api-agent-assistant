# DevTrace 项目速览（评审 / 面试版）

> 一句话：**DevTrace 是一个面向开发者平台 API 接入排障的受约束单 Agent**——覆盖 401、429、Webhook 失败三类问题，能把计划、工具调用、证据、记忆、流式任务和审计串成可回放的一条链。
>
> 状态：**演示级完整，非生产就绪**。代码冻结在 tag `demo-2026-10-23.1`，最终验收见 [FINAL_REVIEW](docs/FINAL_REVIEW.md)。

## 60 秒了解

| 维度 | 事实 |
| --- | --- |
| 定位 | 开发者平台 API 接入排障 Agent（单 Agent、受约束多步执行） |
| 技术栈 | Python 3.12 / FastAPI / Vue 3 / Redis / ChromaDB / MCP / Docker / GitHub Actions |
| 规模 | 十个核心模块；371 项测试 + 219 subtests；CI 全绿（run #44） |
| 评测 | 固定案例 30/30；40 条 holdout 规则 40/40；真实模型全量 37/40；记忆任务 12/12 |
| MCP | 自建 JSON-RPC 2.0 client/server，托管 8 个只读工具；写操作工单保留本地 |
| 工程亮点 | 候选计划评分、分层记忆、Redis 队列租约/优先级/延迟重试/死信、RBAC + 审计、workspace 隔离、SSE 断流取消、真实 `kill -9` 恢复 |
| 仓库 | https://github.com/a142269/devtrace-api-agent-assistant |
| 演示 tag | https://github.com/a142269/devtrace-api-agent-assistant/releases/tag/demo-2026-10-23.1 |

## 5 分钟评审路线

1. **本页**：知道项目做什么、证据在哪。
2. **[架构说明](docs/ARCHITECTURE.md)**：看请求如何经过 API → Agent → 工具/队列 → Redis/Chroma。
3. **[5 分钟演示脚本](docs/DEMO.md)**：知道实际跑起来该点哪里、看什么。
4. **[评测报告](backend/developer/evaluation_report.md)**：看真实数字、失败项、为什么 37/40 而不是只报 40/40。
5. **[面试问答](docs/INTERVIEW.md)**：看设计取舍、最难 bug、生产化差距。
6. **[简历段落](docs/RESUME.md)**：可直接改写进简历。

## 这个项目最能说明什么

- **不是“调个 API 套个壳”**：规划、工具白名单、证据校验、工单授权、记忆、队列、审计、限流都有独立实现和测试。
- **可复核**：每个 phase 都有评测 JSON、CI run、tag 和限制清单；失败样本保留，不为了好看改断言。
- **知道自己不知道什么**：README/评测报告明确写出单租户、单 Redis/Chroma、无 WORM、外部 MCP 未接真实第三方、作者手写标签等边界。

## 证据入口

- 测试与 CI：`.github/workflows/ci.yml`、[评测报告](backend/developer/evaluation_report.md)
- 真实模型 holdout：`backend/developer/eval_holdout_model_2026-10-22_full40.json`
- base 名补充归因：`backend/developer/eval_holdout_model_2026-10-22_full40_base_names.json`
- 记忆摘要双列：`backend/developer/eval_memory_quality_live_2026-10-21_r2.json`
- 并发/限流：`backend/developer/eval_load_2026-10-21.json`、`eval_stream_limit_2026-10-22.json`
- 最终验收：`docs/FINAL_REVIEW.md`（已入库）

## 诚实边界（面试请主动讲）

- 单 Agent，不做多 Agent 编排；候选计划评分是启发式，不是 ToT。
- 演示级单机：单 Redis、单 Chroma，无副本/故障切换；生产身份、TLS、密钥轮换、WORM 审计未实现。
- ChromaDB 0.5.23 有 3 条上游未修复漏洞，仅内网演示。
- 40 条 holdout 是作者手写/第二套公开集，不是第三方基准；模型只有一个 provider。
- 真实模型 holdout 37/40：2 条是工具名 `mcp_` 标签绑定，1 条是 v2 证据未命中；base 名补充口径 39/40 只是归因，不宣称能力提升。

---

如果你只有 30 秒：看上面的表 + 打开 tag 里的 README；如果你有 5 分钟：按 `docs/DEMO.md` 跑一遍；如果你在面试：直接看 `docs/INTERVIEW.md` 的 12 个问题。
