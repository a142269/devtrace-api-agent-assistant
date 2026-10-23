# DevTrace 最终验收与发布结论（P14 后更新）

> 日期：2026-10-23（P13 收尾于当日 14:25 本地；P14 收尾于当日 16:41 本地，即 tag 时间）
> 主指挥：Lead（DSH）；执行：Qoder（P12 session 6474659f、P13/P14 session qoder-session-c）
> 最终 HEAD / 远程 main：`0ee0673`（P14 三个提交：`d6a1032` 代码、`5b3d238` 代码、`0ee0673` 文档）
> 发布 tag：**`demo-2026-10-23.1`**（附注 tag，tag 对象 `71a6a93` → commit `0ee0673` = `main` 最新提交；tag message 明写"**演示级补丁、非生产就绪**"）。历史 tag **未移动、未删除**：`demo-2026-10-23`（对象 `2e7d025`）仍在 `ad6674c`、`demo-2026-10-22`（对象 `513a8d3`）仍在 `ac3ec75`、`demo-2026-10-21`（对象 `0f8e4db`）仍在 `7448217`——推送后已用 `GET /git/refs/tags` 复核四个 tag 的对象哈希，与本轮开工前逐一相同；无 force push、无历史改写
> CI：run **#44**（id `39926015495`，head `0ee0673` = 当前 main = tag 指向的提交）success，三个 job 全绿（backend-tests / frontend-build / dependency-audit）；P14 代码提交 `5b3d238` 的 run **#43**（id `39924126290`）亦 success。此前 #42（`ad6674c`）、#41（`7e58feb`）、#40（`ac3ec75`）均 success。job 日志仍需仓库管理员权限（API 403），所以 CI 内部的 pytest 条数只有"本地同参数运行 + job 结论 success"这条间接支撑

## 1. 最终结论

**DevTrace 已达到"演示级完整 Agent"状态：十模块均有可运行实现、可复现测试与评测，P1–P14 逐阶段验收通过。**
**不是生产就绪系统**：单租户、单 Redis、单 Chroma、无 WORM 审计、外部 MCP 未接真实第三方、评测样本为作者手写小样本；P13 的会话锁与回收终态修复只到单元级 + 真实 Redis 的 Lua 原语，P14 补齐的最后两条终态路径（启动恢复耗尽重试、被判 `interrupted`）同样只有 fake Redis 与单元级证据，均**未在真实队列上端到端验证**。

P8 用已批准的预算补齐了两个评测空白：

- **完整 40 条模型 holdout**：真实模型 **37/40**（原批 19/19，新批 18/21），43 次 HTTP 请求、墙钟 487.3 秒、无基础设施重试；
- **真实 provider 流式 429**：预置配额后 1 条 stream + 1 条 sync 均在模型调用前 429，`Retry-After: 60`，无 task/run/started/入队，浏览器也验证了 429 提示。

## 2. 最终证据

| 项目 | 结果 |
| --- | --- |
| 测试 | **371 passed + 219 subtests**（P14 代码提交 `5b3d238`：不带 `PYTHONUTF8=1` 118.44s / 带 120.71s；文档工作树复跑：不带 130.18s / 带 119.85s；本机 Python 3.10.18，CI 3.12；P13 时为 355 + 219、P12 时为 337 + 217） |
| Ruff | CI `--select F` **62 文件** All checks passed（本地按 ci.yml 清单原样复核，exit 0） |
| CI | run **#44**（id `39926015495`，head `0ee0673` = 当前 main = tag 指向的提交）success，三个 job 全绿；P14 代码提交 `5b3d238` 的 run **#43**（id `39924126290`）亦 success；#42（`ad6674c`）、#41（`7e58feb`）、#40（`ac3ec75`）亦 success。job 日志 API 返回 403（需仓库管理员），CI 内部条数无直接读数 |
| 远程 | `HEAD = main = 0ee0673`；tag **`demo-2026-10-23.1`** → `0ee0673`（附注 tag 对象 `71a6a93`，消息首行"DevTrace 演示基线 demo-2026-10-23.1（P14 演示级补丁）"）；`demo-2026-10-23` 仍在 `ad6674c`（对象 `2e7d025`）、`demo-2026-10-22` 仍在 `ac3ec75`（对象 `513a8d3`）、`demo-2026-10-21` 仍在 `7448217`（对象 `0f8e4db`），**均未移动、未删除**（推送后用 `GET /git/refs/tags` 逐一复核对象哈希） |
| 固定案例 | 30/30 规则模式 |
| holdout 规则 | **40/40**（P13 回归重跑，输出 `.p13_rules.json`，零模型调用；P12 亦 40/40） |
| holdout 模型（全量） | **37/40**：原批 **19/19**，新批 **18/21**；3 条失败保留（P8 结论，P12 未重跑） |
| SSE 取消（P12） | 单次 `Task.cancel()` 传播、记录 `cancelled`、会话锁释放；空闲心跳不丢事件；`complete` 路径顺序不变、无泄漏 task；修复前 5× 集成对照 0/5、修复后 5/5 |
| 真实 kill -9（P12） | `docker kill` 持有 `running` 任务的 worker（exit 137、非 OOM）→ 幸存 worker 32.9s 回收、11.41s 重跑、终态唯一（`waiting`）、`task.finished` 恰好 1 条、`run_id` 唯一、会话锁释放、同会话补发成功、客户端未挂死 |
| 公开文档脚本（P12） | `verify_public_docs.py` 按 base 名比较工具后 **exit 0**（修复前 `has_playbook=false` → exit 1）；官方报告未被覆盖 |
| 会话锁归属（P13） | 队列会话锁终态释放改为 Redis 原子比较后删（锁值 = `task_id`）：owner 返回 1、非 owner 返回 0 且锁值与 TTL 都不动、锁缺失返回 0；三条生产终态路径（worker `finally`、启动前取消、入队回滚）全部换用；负向对照证明同一场景下无条件原语确实会删掉新任务的锁；真实 Redis（`devtrace-verify` 容器、Python 3.12.15、合成 key）复核了 Lua 与生产函数 |
| 回收终态收尾（P13） | `reclaim_expired` 打死信时 owner-aware 释放会话锁 + 写**恰好一条** `task.finished`（`status=failed`，`detail` 仅白名单 `status`/`attempts`）；锁故障降级为 warning 且审计照旧写入；不与 worker 二次审计。变异对照：把收尾调用换成 `pass` → 恰好 2 项失败 |
| 启动恢复终态（P14） | `recover_running_tasks()` 的 `attempts` 耗尽分支（原先只置 `failed` + 进死信，会话锁白挂 900 秒且无终态审计）改用公共 `task_queue.finalize_terminal(..., status='failed', record=record)`：owner-aware 释放 + 恰好一条 `task.finished`（`detail` 仍只含白名单 `status`/`attempts`）。4 项新用例：耗尽重试后锁消失且同会话可立即重新认领、死信且不在任何就绪队列、审计 `detail == {'attempts':2,'status':'failed'}`；仍有重试预算时保留锁且零终态审计；绝不删更晚任务的锁；锁故障有 warning 而审计照旧落地。变异 A（删掉收尾调用）→ **恰好 3 项失败**，"保留锁"那项正确绿色；变异 B（owner-aware 换成盲删）→ **4 项失败** |
| 中断任务收尾（P14） | `main._mark_interrupted()` 在改状态与清索引后调用同一 `finalize_terminal(status='interrupted', reason_code='stale_heartbeat')`，覆盖查询期（`GET /developer/tasks/{id}`）与启动期两个入口。**关键前提**：队列索引 `developer:tasks:queue-index` 与进程内索引 `developer:tasks:running` 是两套，但 `developer:task:<id>` 键空间共享，所以查询期修复确实能把持队列锁的任务判成 `interrupted`——这是真实僵尸入口。新 `tests/test_interrupted_tasks.py` 12 项（8 项直扫 + 4 项走真实 ASGI）：审计恰 1 条且 `workspace`/`trace_id`/`actor` 正确、消息正文不进审计流、两次 GET 不重复审计也不二次释放（fake 记录每次 `eval`）、进程内记录一次 `eval` 都不发起、缺 `user_id` 跳过释放且锁原地不动、活 worker 的任务完全不动、健康任务不被修复、视图不外泄 `payload`/`user_id`/租约字段。变异 C（收尾换成 `pass`）→ **8 失败 / 4 通过**；变异 D（去掉"不猜 key"门卫）→ **恰好 2 项失败** |
| 范围零改动（P14） | 代码提交只碰 5 个文件（`ci.yml`、`api/main.py`、`developer/task_queue.py`、`tests/test_queue_production.py`、新 `tests/test_interrupted_tasks.py`）；`planner.py`/`prompts.py`/`assistant.py`/`memory/`/`mcp/`/`frontend/`/限流器 diff 为空；`git diff ad6674c..HEAD -- <三份官方 holdout JSON>` 为空；零模型调用、未起 Docker、未重跑 kill 取证 |
| 记忆摘要（真实模型） | strict **6/10**、semantic **10/10**（主门槛 semantic；两列都报告） |
| 记忆任务 | 12/12 |
| 公开文档 / 错误手册 | 28/28、40/40 |
| MCP | 10/10；server 托管 8 个只读工具，工单保留本地 |
| 候选计划 | 11/11 |
| 流式限流（P7/P8） | 规则 31 并发：29×200 + 2×429；真实 provider：预置配额后 429，无模型调用/副作用；浏览器 429 提示通过 |
| 瞬时 Redis TTL（P7） | 默认 86400；注入 120 后六类 key 全部按 120 衰减；记忆 TTL 独立不变 |
| 并发验证（rules） | 77 请求、35 complete、36 run_id、无失败行 |


**P9 补充（2026-10-22）**：官方 P8 **37/40** 原样保留；离线 base 名补充评分 **39/40**（public_api 2/2，auth 4/5，v2 证据项仍失败）；278 项测试；flake 事件驱动 20/20；CI #36。补充评分不改官方口径，只解释 `mcp_` 传输前缀导致的标签绑定误判。

**P10 补充（2026-10-22）**：限流器 Redis/Lua 故障改为 **fail-open**（只吞 `RedisError/OSError/asyncio.TimeoutError`，429 不被吞；登录 429 补 `Retry-After: 60`）；新增 `python -m developer.memory_retention`，默认 dry-run、只清 episodic、拒绝画像集合、无 `ts` 跳过不删；304 项测试（Windows 需 `PYTHONUTF8=1`），CI #38。P10 未改官方 holdout、未调用模型。

**P11 补充（2026-10-22）**：MCP stdio 两端强制 UTF-8（服务端 `reconfigure` + 客户端子进程 `PYTHONUTF8=1`），Windows 不再需要手动设置；retention CLI 增加 `--page-size`（默认 500，1..5000）分页扫描，1200 条真实 Chroma 上 dry-run/apply 与不同页大小判定一致；325 项测试带/不带 `PYTHONUTF8` 均全绿，CI #39。未改官方 holdout、零模型调用。

**P12 补充（2026-10-22 → 10-23 凌晨）**：① 修掉一个**真实产品缺陷**——进程内 SSE 的 15 秒空闲心跳原写作 `asyncio.wait_for(queue.get(), 15)`，Python 3.10 的 `wait_for` 在内部 future 已完成时会返回其结果，一次外部 `Task.cancel()` 落在"事件刚入队还没被取走"的窗口里就被**静默丢弃**（客户端已断开而生成器继续跑、任务停在 `running`、会话锁等 180 秒 TTL）；改用 `asyncio.wait` 等待常驻 getter，事件格式/顺序、150s 上限、断开清理语义未变，新增 `tests/test_stream_cancellation.py` 12 项 + 6 subtest（含"旧写法不抛 `CancelledError`"的负向对照，3.11+ 按 `skipIf` 跳过）。② **真实 `kill -9` 崩溃恢复**：双 worker、租约取下限 30 秒、`restart: "no"`、不注入假租约，`docker kill` 掉持有 `running` 任务的 `devtrace-verify-worker-2`（exit 137、非 OOM，落在 running 后 1.22 秒、此时 `run_id` 仍为 null），幸存 worker 32.9 秒回收、`attempts` 0→1、11.41 秒重跑到唯一终态 `waiting`，`task.finished` 恰好 1 条、`run_id` 唯一、不进死信、队列清空、会话锁释放（ttl −2）、同会话补发请求成功，连着看的客户端全程收到心跳并在 +49.97s 拿到 `event: complete`。**终态是 `waiting` 不是 `completed`**（计划把下一步交回用户，`waiting` 是队列合法终态）。③ 会话锁**不挡** reclaim，故未做卡片里那条条件性提交，改用只读探针给 P13 取证两个未修缺口（重试耗尽不释放锁/不写审计；`release_session` 无条件 `DEL`）。④ `verify_public_docs.py` 工具名断言改按 base 名归一化，MCP 公开路径 **exit 0**，输出改到临时路径、不再覆盖仓库内官方报告。337 项测试带/不带 `PYTHONUTF8=1` 均全绿，Ruff 清单 59 → 60 文件，规则 holdout 回归 40/40，CI **#40、#41** 绿（#41 跑在修正提交 `7e58feb` = 当前 main 上），tag `demo-2026-10-22` 已推送（指向 `ac3ec75`；`7e58feb` 只重写了两处时间间隔数字 11.43→11.41 秒、并注明 32.9 秒的分母口径，**tag 未移动，是否重指向待 Lead 决策**）。**成本如实披露：卡片允许 ≤2 次模型调用，kill 取证实际用了 7 次**（rules 模式 `running` 窗口仅 0.151 秒、`docker kill` 打不进去，故切真实模型；容器 `down` 后日志已删、无法事后复核），P12 其余验证零调用。官方 holdout 案例/标签/断言与两份已发布模型报告零改动。

**P13 补充（2026-10-23）**：补掉 P12 取证但未修的两个会话锁缺口（基线 `7e58feb` → `8f437ca` + `d149689` + `ad6674c`）。① 队列会话锁的释放从无条件 `DEL` 改成按锁值 `task_id` 的 Lua 比较后删，三个生产终态点全部换用，`release_session()` 降级为“调用方不带 task_id”的显式原语；② `reclaim_expired` 耗尽 `MAX_ATTEMPTS` 打死的任务现在会 owner-aware 释放锁并补写恰好一条 `task.finished`（原先既不释放锁、也没有终态审计，会话在 900 秒 TTL 内不可用）。整表 337 → **355 项 + 219 subtest**（两种编码都全绿），Ruff 60 → **61 文件**，规则模式 holdout 回归 40/40，**零模型调用、未做 kill 复跑**；Lua 原语在真实 Redis 上取过证，回收终结路径只有 fake Redis 证据（刻意不向真实队列注入合成任务）。新增硬规则：**崩溃取证日志必须在 `docker compose down` 之前导出并结构化落盘**——P12 那 7 次调用计数因此永久不可复核。

**P14 补充（2026-10-23）**：把 P13 记录、待裁定的最后两处同形状僵尸锁/缺审计路径收掉（基线 `ad6674c` → `d6a1032` + `5b3d238` + 文档 `0ee0673`）。① P13 的私有 `_finalize_reclaimed_failure()` 提取为公共 `task_queue.finalize_terminal(redis, task_id, *, status, record=None, reason_code='')`：读不到记录就整体跳过、`user_id` 与 `payload.conversation_id` 必须同时存在才尝试 owner-aware 释放（否则记 info 后跳过，**绝不猜 key**）、`task.finished` 审计 best-effort 且 `detail` 只走白名单。② 接到两个新调用点：`recover_running_tasks()` 的 `attempts` 耗尽分支（`status=failed`，复用手上那份记录）与 `main._mark_interrupted()`（`status='interrupted'`、`reason_code='stale_heartbeat'`）。本轮最关键的事实核查是**两套任务索引不同**（队列 `developer:tasks:queue-index` 带会话、进程内 `developer:tasks:running` 不带），但 `developer:task:<id>` 键空间共享，所以查询期修复确实能把一个持队列会话锁的任务判成 `interrupted`——路径二因此不是假想问题。③ 幂等靠既有"写前重读 + `updated_at` 必须相等"门卫，重复 reconcile 不写第二条审计、不再发起比较删除。④ 整表 **355 → 371 项通过 + 219 个 subtest**（两种编码都全绿），Ruff 61 → **62 文件**，规则模式 holdout 回归 **40/40**、零模型调用、**未起 Docker、未重跑 kill 取证**（卡片明确禁止），官方 holdout 三份 JSON 与评分口径 diff 为空。⑤ 四处变异对照 A/B/C/D 分别恰好 3 / 4 / 8 失败（4 通过）/ 2 项失败，四次都字节备份 + `cmp` 还原后复跑整表。⑥ 流程上按 P13 修正后的顺序执行：**文档先进 HEAD，再打 tag**，`demo-2026-10-23.1` 是 main 最新提交；三个历史 tag 对象哈希推送后逐一复核未变。⑦ 遗留：两条新路径只有 fake Redis 与单元证据；worker 心跳过期但进程仍在计算时仍有窄的双审计窗口（未改成 Lua CAS，那会动 worker 终态链路与 P13 已验收断言）；`task_queue.dead_letter()` 无生产调用点，是已知死代码，本轮只标注、删除留 P15；`test_api_security.py`/`test_workspace.py`/`test_stream_cancellation.py` 三处 fake `eval` 仍是盲目 `return 1`，而它们现在位于 `interrupted` 路径上（P13 那句"这些 fake 不经过会话锁释放"已不完全成立，本轮在评测报告里就地更正）。

## 3. P1–P14 交付摘要

- **P1 RBAC + 审计**：`DEVTRACE_ADMIN_USERS`、`require_admin`、Redis Stream `developer:audit`、`GET /developer/audit`；append-only 但非 WORM。
- **P2 工作区隔离**：users 配置兼容旧/新格式，workspace 服务端映射（不改 JWT），run/task/memory/audit 归属与 404 隔离。
- **P3 队列生产化**：优先级队列 + legacy、任务租约/续租/过期回收、延迟退避、死信重放、trace_id、metrics。
- **P4 MCP 全读工具**：8 个只读工具经 MCP；`create_support_ticket` 保持本地；外部 stdio registry 默认拒绝、参数剥离、失败降级。
- **P5 记忆质量与生命周期**：TTL 可配置、画像删除、导出元数据、真实模型双列评测；strict 未达、semantic 达标。
- **P6 评测扩展与收尾**：F402 清理、holdout 40、双列评分、负载验证、发布文档与 tag。
- **P7 流式限流 + TTL 收敛**：两入口共用配额、SSE 前 429、7 个 TTL 点统一配置、证据入库。
- **P8 全量模型 holdout + 真实 provider 429**：37/40 与浏览器 429 证据；产品代码/提示词/案例/评分逻辑零改动。
- **P9 评测口径修正 + flake 修复**：离线 base 名补充评分 39/40（官方 37/40 不动）；时间敏感用例改事件驱动，只改测试。
- **P10 限流降级 + 情景记忆保留**：限流器对基础设施异常 fail-open、登录 429 补 `Retry-After`；新增 `developer.memory_retention` CLI（默认 dry-run）。
- **P11 MCP stdio UTF-8 + 保留 CLI 分页**：stdio 两端强制 UTF-8（Windows 不再需要 `PYTHONUTF8=1`）；`--page-size` 分页扫描，判定不变。
- **P12 SSE 取消语义 + 真实 kill -9 恢复 + 公开文档脚本**：修掉 Python 3.10 上 `wait_for(queue.get(), 15)` 吞外部取消的缺陷（改 `asyncio.wait` + 常驻 getter）；在双 worker 栈上 `docker kill` 掉持有 `running` 任务的 worker，幸存 worker 32.9 秒回收、11.41 秒重跑到唯一终态；`verify_public_docs.py` 的工具名断言改按 base 名归一化。
- **P13 会话锁归属 + 回收终态**：owner-aware 释放（Lua 比较后删）覆盖 worker 两条路径与入队回滚；回收打死信时释放锁 + 单条 `task.finished`；真实 Redis 复核 Lua，回收路径仅单元级证据、未上真实队列。
- **P14 剩余两条僵尸锁/缺审计路径**：P13 的私有收尾提取为公共 `task_queue.finalize_terminal()`，接到 `recover_running_tasks()` 的耗尽重试分支与 `main._mark_interrupted()`（查询期 + 启动期两个入口）；缺 `user_id`/会话字段时跳过释放而不是猜 key，重复 reconcile 不二次审计；`reclaim_expired` 的 `detail` 与调用形状未动，P13 已验收语义原样保留。新增 16 项测试（含 4 项走真实 ASGI 端点）与四处变异对照，端到端真实栈仍未验证。

## 4. 三条模型 holdout 失败（如实保留）

1. `h_public_playbook_path`、`h_public_docs_path`：标签按本地工具名断言，实际公开路径执行的是 `mcp_lookup_error_playbook` / `mcp_search_public_docs`；**标签绑定问题，不是能力缺失**。P8 已更正 P6/P7 的“ALL_TOOLS 触发”归因：`DEVTRACE_MCP_ALL_TOOLS` 未设置、仅默认 `DEVTRACE_MCP_PUBLIC_TOOLS=enabled` 时也会出现。
2. `h_quota_v2_doc_version_boundary`：工具与假设正确，但没带回 v1 文档那条具体证据；规则模式依赖文档桩才稳定通过，属证据项要求偏严/波动。

这三条没有删除、没有重跑挑结果、没有改标签或阈值。P9 已按 Lead 批准做**离线补充评分**（不改官方口径）：base 工具名归一化后 **39/40**（public_api 2/2、auth 仍 4/5），原始 37/40 保留；39/40 只是“工具身份与传输前缀无关”的归因口径，不是能力分。

## 5. 已知限制（必须保留，不要删除）

1. **安全依赖**：ChromaDB 0.5.23 有 `PYSEC-2026-3813/3814/3815`，无上游修复；仅单租户内网演示。
2. **单实例**：单 Redis、单 Chroma、单机 Docker；无副本/AOF/故障切换。worker 单点 `kill -9` 已由 **P12 实测**（双 worker、租约取下限 30 秒、幸存 worker 32.9 秒回收 + 11.41 秒重跑到唯一终态、`task.finished` 恰好一条、会话锁释放）；**仍未实测**的是 Redis 故障切换、连环 kill（两个 worker 先后被杀）与"kill 落在写运行记录瞬间"的分相演练。P12 另取证到两个**未修**的会话锁缺口：维护循环耗尽 `MAX_ATTEMPTS` 时既不释放会话锁（该会话在 900 秒 TTL 内不可用）也不写 `task.finished` 审计；`release_session` 是无条件 `DEL`，晚到的清理会删掉更新任务的锁（修法是按锁值里的 `task_id` 做 Lua 比较后删）。（P13 另补：worker 死掉后回收终态会释放会话锁并写审计，但该路径未在真实队列上端到端跑过。**P14 再补**：同形状的最后两处——`recover_running_tasks()` 的耗尽重试分支与 `reconcile_stale_tasks`/`_mark_interrupted` 判 `interrupted`——也统一走公共 `task_queue.finalize_terminal()`，owner-aware 释放 + 恰好一条 `task.finished`，缺会话字段则跳过释放而不是猜 key；同样只有 fake Redis 与单元级证据，端到端真实栈仍未验证。另留两处已知瑕疵：worker 心跳过期但进程仍在计算时的"同任务两条终态审计"窄窗口（未改成 Lua CAS，怕动 worker 终态链路与 P13 已验收断言），以及 `task_queue.dead_letter()` 是**无生产调用点的死代码**（本轮只标注，删除留 P15 裁定）。）
3. **审计**：应用层只追加，Redis 运维方可改；stream 约 1 万条窗口；查询无游标分页。
4. **多租户**：workspace 是归属/可见性层；无共享成员/邀请；Chroma 记忆仍按 user 隔离，无 workspace 物理分区。
5. **MCP**：外部 registry 只用自带 fake server 验证；无鉴权/信任模型/健康检查/重连；未接真实第三方。
6. **评测**：40 条 holdout 与 10 场记忆摘要均为作者手写/模板合成、单 provider 小样本，不是第三方基准；token/费用未采集。
7. **限流**：固定窗口先 INCR 后判定，被拒请求也计入窗口；只按用户计数，无 IP/租户/网关层限流。P10 已做限流器 fail-open（仅吞 Redis/OSError/Timeout 基础设施异常；登录 429 已带 `Retry-After: 60`），但 Redis 全挂时聊天仍会 500（会话锁依赖 Redis），生产需网关/多副本兜底。
8. **模型模式 429**：仅用预置配额验证“拒绝早于模型调用”，未发 31 条真实模型流；真实突发分布未测。
9. **测试 flake（P9 改测试，P12 才修到根因）**：P9 把 `test_cancelled_stream_task_records_cancelled_and_releases_lock` 改成事件驱动、不再依赖 `sleep(0.05)`，当时归因为"断言依赖调度点"。**P12 发现底下还压着一个真实产品缺陷**：进程内 SSE 的 15 秒空闲心跳写作 `asyncio.wait_for(queue.get(), 15)`，Python 3.10 的 `wait_for` 在内部 future 已完成时会直接返回其结果，**一次外部 `Task.cancel()` 落在"事件刚入队还没被取走"的窗口里就被静默丢弃**（3.11+ 重写为电平触发，CI 3.12 看不见）。已改用 `asyncio.wait` 等待常驻 getter，单次取消即传播；P9 那条用例一字未动，另加 `tests/test_stream_cancellation.py` 12 项，全量 337 项通过（带/不带 `PYTHONUTF8=1` 均绿）。**修复前后的集成对照是概率性的**：修复前连跑 5 次 0/5、修复后 5/5，但基线整表 `-v` 单跑时其中 2 项曾偶然通过，所以结论以确定性单元对照（新写法抛 `CancelledError`、同一位置旧 `wait_for` 写法不抛）+ 5× 循环为准。
10. **TTL**：`DEVTRACE_REDIS_TTL_SECONDS` 是进程级配置，多版本/多环境共享 Redis 时未隔离；TTL 缩短对业务可观测窗口的影响未做端到端验证。
11. **记忆**：Chroma 情景记忆/画像无 TTL，只能显式删除；无加密/画像编辑 UI；记忆 TTL 与 Redis TTL 两套配置并存。
12. **规划**：候选评分是启发式，无并行步骤/ToT；重规划 1 次。
13. **产品**：无生产身份服务、TLS、Key 轮换、RBAC 多角色、不可篡改审计、容量压测。

## 6. 下一轮候选（P13 后更新，需 Lead/用户决策）

1. **holdout 正式评分口径修正**：P9 已做离线补充评分（39/40，官方 37/40 保留）；是否把 base 名比较写进官方 `assess()` 是改评分口径，需批准 + 新基线 + 规则重跑。P12 只修了工作区脚本 `verify_public_docs.py` 的同类断言（不入库），**没有**碰官方标签。
2. ~~修时间敏感 flake~~（P9 改测试、**P12 修到产品根因**：SSE 心跳吞取消，见第 5 节第 9 条）。
3. **限流治理**：Redis 故障时放行/503（P10 已 fail-open）、登录 `Retry-After` 统一（P10 已做）、按 IP/租户/网关分层配额（未做）、限流器健康检查/熔断与断连-恢复压测（未做）。
4. **Chroma 保留策略**：情景记忆过期已有 CLI（P10）+ 分页（P11），仍缺外部定时触发（cron）、画像保留策略、按 workspace 分区、记忆加密、"删除窗口内并发写入"的一致性设计。
5. **队列/运维**：cron/定时任务、任务取消 API、指标历史/告警仍未做；~~真实 `kill -9` 恢复~~（**P12 已实测单点 kill**），剩连环 kill 与分相落点演练。
6. ~~会话锁两个缺口~~（**P13 已修**：终态释放改 Lua 比较后删、回收打死信时释放锁 + 补 `task.finished`）；~~同类"僵尸锁"形状还剩的两处~~（**P14 已修**：`task_queue.recover_running_tasks()` 的耗尽重试分支与 `api/main.py` 的 `reconcile_stale_tasks`/`_mark_interrupted` 统一走公共 `task_queue.finalize_terminal()`）。**仍待裁定/未做**：① 回收终结与 `interrupted` 两条路径在真实队列上的端到端验证、连环 kill 与分相落点（P13/P14 都只做到 fake Redis + 单元级）；② `release_session_if_owner` 返回 0 无法区分"锁已消失"与"锁属他人"；③ 其余 fake Redis 的 `eval` 仍是 `return 1`（`test_api_security.py`/`test_workspace.py`/`test_stream_cancellation.py`），而 `_mark_interrupted` 现在会经过它们，若要断言锁语义必须先换成忠实模拟；④ worker 心跳过期但仍在计算时的窄双审计窗口（要根治得改成 Lua CAS，会动 worker 终态链路与 P13 已验收断言）；⑤ `task_queue.dead_letter()` 无生产调用点，是否删除留 P15。
7. **SSE relay 收敛**：P12 实测从任务终态到客户端 `<eof>` 之间还夹了一次 15 秒心跳间隔（终态 +49.97s 收到 `complete`，但 relay 的空闲轮询粒度决定了尾延迟），可评估把 relay 的等待改成事件驱动。
8. **生产化**：多租户身份、TLS、WORM/审计导出、Redis/Chroma 高可用、容量与故障演练。
9. **评测**：多 provider 复跑、更大样本、第三方独立标注；`frontend/dist` 重建入库。

## 7. 复现命令

```powershell
$root = "D:\echomind\EchoMind所有代码+详细文档+简历"
$py = "$root\.dsh-venv\Scripts\python.exe"
$env:PYTHONPATH = "$root\.dsh-venv\Lib\site-packages"
$env:PYTHONUTF8 = "1"
cd "$root\.portfolio-github-staging\backend"
& $py -m pytest -q tests
```

```powershell
# Docker 验证（本机需 ONNX 缓存/构建参数）
cd "$root\.portfolio-github-staging\backend"
docker compose -p devtrace-verify -f docker-compose.yml -f "$root\devtrace-compose.verify.yml" up -d
# API: http://127.0.0.1:18002
docker compose -p devtrace-verify -f docker-compose.yml -f "$root\devtrace-compose.verify.yml" down
```

```bash
# P12 真实 kill -9 复现（Git Bash；需要模型模式，会产生真实 provider 调用）
cd "$root/.portfolio-github-staging/backend"
export MSYS_NO_PATHCONV=1
CHAIN="-f docker-compose.yml -f ../../devtrace-compose.verify.yml -f ../../devtrace-compose.bind.yml -f ../../devtrace-compose.rules.yml -f ../../devtrace-compose.p12kill.yml"
docker compose $CHAIN -p devtrace-verify up -d --no-build --scale worker=2
# 发一条队列化 /developer/chat/stream，等 status=running 后：
docker kill devtrace-verify-worker-2      # 或持有该任务的那个 worker
# 约 30 秒租约 + 维护周期后，幸存 worker 回收重跑；收尾：
docker compose $CHAIN -p devtrace-verify down     # 不要加 -v
```

`devtrace-compose.p12kill.yml`（工作区文件，**未入库**）注入 `DEVTRACE_TASK_LEASE_SECONDS=30`（代码下限）、worker `restart: "no"`（被杀的必须保持死掉，才能证明是幸存 worker 回收）与 `DEVELOPER_MODEL_MODE=anthropic`（rules 模式 `running` 窗口只有 0.151 秒，`docker kill` 打不进去）。驱动脚本 `p12_kill_test.sh` + 容器内探针 `p12_kill_probe.py`、会话锁只读探针 `p12_reclaim_probe.py`、脚本修复前的控制跑 `p12_docs_before.py` 同样未入库。

## 8. 关联文件

- 最终报告：`backend/developer/evaluation_report.md`（**P12 一节**含 SSE 根因/修法、kill -9 完整时间线与逐条断言、脚本修复前后差异、未验证清单；**P13 一节**含 Lua 归属释放、真实 Redis 取证与两处未修缺口的记录；**P14 一节**含"两套任务索引"事实核查、两条终态路径修法、四处变异对照 A/B/C/D 与对 P13 第 7 节那句"这些 fake 不经过会话锁释放"的就地更正）
- 模块说明：`backend/developer/智能体核心模块说明.md`
- 升级进度：`backend/developer/升级进度.md`
- 交接文档：`DevTrace项目交接文档.md`
- 发布清单：`发布清单_2026-10-18.md`
- **P13 证据（工作区根，未入库）**：`.p13_lua_probe.json`（真实 Redis 上的 Lua 与生产函数取证，容器 Python 3.12.15）、`.p13_rules.json`（规则 holdout 40/40）、`.p13_runs.json` / `.p13_jobs.json`（CI run #42 三 job success）、`p13_lua_probe.py`（探针脚本）、`.p13_final_task_queue.py` / `.p13_final_test_queue_production.py`（变异对照用的字节备份）
- **P12 证据（工作区根，未入库）**：`p12_kill_evidence/`（`00_meta.txt` 容器名与 worker_id 映射、`01_state_running.json`、`02_state_final.json` 四次状态跃迁、`03_run_result.json` 含 21 行客户端 SSE 时间线、`04_verify.json` / `09_verify_after_resend.json` 逐条断言、`05_lock.json`、`06_survivor_log.txt` 回收日志、`07_containers.txt`、`08_resend.json`）、`p12_reclaim_probe.json`（会话锁两个缺口）、`p12_docs_before.json`（脚本修复前断言）、`verify_public_docs.json`（修复后 exit 0）、`.p12_rules.json`（规则 holdout 40/40）
- P8 证据：`eval_holdout_model_2026-10-22_full40.json`、`eval_stream_limit_model_2026-10-22.json`
- P7 证据：`eval_stream_limit_2026-10-22.json`
- 记忆/ holdout / 负载证据：`eval_memory_quality_live_2026-10-21_r2.json`、`eval_holdout_rules_2026-10-21.json`、`eval_holdout_model_2026-10-21_new_batch.json`、`eval_load_2026-10-21.json`
