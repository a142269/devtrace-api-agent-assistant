> 本报告主体记录拆分前的实测结果。2026-10-17 独立化后：Docker 构建与规则模式 401 端到端通过（3 次工具调用、4 条证据）；开发者测试 16 项通过，Vue 生产构建通过。未重新运行真实模型评测。
>
> 2026-10-18 起本报告末尾另有"证据冲突与安全回归"与"Docker 全栈验证"两节：前者在本地隔离 Python 环境中运行进程内测试与评测脚本；后者在本机 Docker Desktop 上全新构建镜像并验证规则模式 HTTP、SSE、断流、并发与跨轮恢复；同一 Docker 栈随后用本机配置的模型服务跑了 4 条真实模型样例与 3 条抽样判定（见 Docker 一节），但样本量很小，仍不构成准确率评测。

# 开发者助手验证记录（更新于 2026-10-14）

本报告只记录本次实际运行的结果。模拟平台案例不能代表真实平台故障分布或线上准确率。

## 自动化测试

在 Python 3.12 生产镜像的一次性容器中安装 pytest 后运行：

```text
python -m pytest -q tests/test_agent_orchestrator.py tests/test_intent_recognizer.py tests/test_knowledge_base.py tests/test_developer_assistant.py
32 passed, 30 subtests passed in 1.15s
```

30 条参数化案例由 10 条鉴权、10 条限流、10 条 Webhook 固定记录组成，均检查了工具顺序、成功状态、问题标识与文档证据、模拟平台声明和规则决策模式。其余测试检查缺少标识后恢复计划、跨用户隔离、失败后计划调整、文档缺失不编造结果、工单重试去重、否定创建工单的表达、仅询问工单流程的表达、敏感信息脱敏、模型异常时标记规则降级、Anthropic 兼容接口的空文本块解析、ChromaDB 多条件过滤格式，以及模型提议顺序、行动选择、必需证据补全、依赖约束和重排计划的跨轮恢复。

## 同一判定器的 HTTP 对照评测（2026-10-14）

执行 `python -m developer.evaluate`，规则模式跑全部 30 条固定案例，真实模型模式从每类取首条，共 3 条。判定器不要求固定行动顺序，但要求计划完成、必需工具集合正确、所有行动成功、同时包含文档与记录证据、权限工具不越过请求记录、答复声明模拟数据，并核对 `decision_mode`。原始逐条结果保存在 [规则模式 JSON](eval_rules_2026-10-14.json) 和 [模型模式 JSON](eval_model_2026-10-14.json)。

| 模式 | 案例数 | 通过 | 规则降级 | 请求耗时中位数 |
| --- | ---: | ---: | ---: | ---: |
| 规则 | 30 | 30 | 0 | 87.7 ms |
| 配置模型 | 3 | 3 | 0 | 23,981.1 ms |

模型模式的 3 条是演示抽样，不能据此推断准确率；耗时也受外部模型网络影响，不宜与规则模式直接比较性能。首次模型试跑曾出现 401 请求超过评测器 120 秒等待上限、429 的模型文本为空且 `stop_reason=max_tokens`，因此该轮仅 Webhook 通过。将模型输出上限从 1,600 调到 4,096、评测器等待上限调到 300 秒后，再在提示中明确合法重排规则，用最终代码重新运行得到上表结果。三条真实模型案例的实际行动均先查模拟平台记录、再查文档；401 最后核对密钥权限摘要。规则模式仍按文档、记录、权限的参考顺序执行。这说明模型提议或行动选择已能影响实际顺序，同时服务端仍保持必需证据与依赖约束。

## HTTP 与界面联调

在本地 Docker 的规则模式下，调用 `POST /developer/chat` 得到：

| 场景 | 计划状态 | 工具行动数 | 引用文档 | 决策来源 |
| --- | --- | ---: | --- | --- |
| `req_auth_01` / 401 | complete | 3 | v1 / 鉴权与权限 | rules |
| `req_quota_01` / 429 | complete | 2 | v1 / 速率限制 | rules |
| `delivery_01` / Webhook | complete | 2 | v1 / Webhook 投递 | rules |

同一 HTTP 服务上的跨轮检查结果：首次缺少 `request_id` 时为 `waiting`，下一轮补充后为 `complete` 且读到 2 条最近消息；另一用户复用相同会话 ID 时读到 0 条消息；重复创建同一问题的工单返回同一编号；运行记录可由所属用户查询。

另外两次 HTTP 请求分别写明“不要创建工单”和“如何创建工单？”，计划仍完成故障排查，`ticket_id=null`，工具行动中都没有 `create_support_ticket`。

Vue 生产镜像此前使用 Node 20.20.2 构建通过。Playwright 实际打开本地页面，切换至开发者助手并提交 401 示例；页面显示初始计划、3 次工具行动及观察、步骤状态、证据要求、规则决策来源与最终结论。2026-10-14 再用本机已缓存的 `node:20-alpine` 镜像构建当前前端，`vite build` 成功，生成 94.33 kB JavaScript 与 22.76 kB CSS。新增的“模型提议 / 批准计划 / 约束调整”字段完成了编译验证，尚未重新做浏览器交互截图。

## 真实配置模型验证

在本地 `.env` 配置的 DeepSeek Anthropic 兼容接口上，三类端到端请求均得到 `decision_mode=model`、`plan.status=complete`，工具调用成功，并同时引用 v1 文档和模拟平台记录：

| 场景 | 实际工具顺序 | 记录证据 | 决策来源 |
| --- | --- | --- | --- |
| `req_auth_01` / 401 | `search_api_docs` → `get_request_trace` → `inspect_key_scope` | `doc:v1 / 鉴权与权限`、`get_request_trace:req_auth_01`、`inspect_key_scope:demo-auth-01` | model |
| `req_quota_01` / 429 | `search_api_docs` → `get_request_trace` | `doc:v1 / 速率限制`、`get_request_trace:req_quota_01` | model |
| `delivery_01` / Webhook | `search_api_docs` → `get_webhook_delivery` | `doc:v1 / Webhook 投递`、`get_webhook_delivery:delivery_01` | model |

第一次 Webhook 在线请求曾因模型输出缺少可解析 JSON 而标为 `rules_fallback`；提高模型输出上限后，同一案例重新运行得到上述模型结果。该次降级不计入成功的模型演示。另用 `req_auth_02` 在最终镜像上得到 `model/complete`，三项工具及证据均成功。

记忆的实际服务检查：使用开发者专属 ChromaDB collection 存入一条情景记忆后，同一用户检索命中、另一用户检索未命中；真实模型生成的用户画像写入后可读回 `entities` 与 `preferences` 字段。API 的 `decision_mode` 区分 `model`、`rules` 和 `rules_fallback`。以上均为本地模拟问题的实测结果，不代表生产准确率。

## 证据冲突与安全回归（2026-10-18，本地隔离虚拟环境）

本轮新增多假设/证据冲突诊断、关联请求排除法和不可信数据边界，并在 Windows + Python 3.10.18 的独立虚拟环境中真实运行。**未使用 Docker**，因此没有重新验证镜像构建、容器健康检查和真实 HTTP/SSE；也没有重跑真实模型。

自动化测试：

```text
python -m unittest discover -s tests
Ran 107 tests in 6.7s
OK
```

比 2026-10-17 的 35 项新增 24 项，覆盖：请求记录与密钥状态冲突、关联请求成功后的排除法、投递记录内的注入文本、用户消息中的注入表述、普通问题不误报、任务心跳刷新、查询时/启动时的 `running` 修复。ChromaDB 仍输出租户与遥测接口兼容警告。

| 评测 | 结果 | 原始报告 |
| --- | --- | --- |
| 分类回归（110 条公开模拟变体） | 基础 40/40、组合及多轮 30/30、故障注入 20/20、安全 20/20 | [eval_scenarios_2026-10-18.json](eval_scenarios_2026-10-18.json) |
| 计划对照（30 条） | 固定/条件检查均 30/30，根因标签均 20/20，证据支持均 30/30，平均工具 2.33 / 2.23 次 | [eval_planning_2026-10-18.json](eval_planning_2026-10-18.json) |
| 故障注入（20 次） | 20/20 报告失败、20/20 恢复，两轮 p95 约 3.9 ms | [eval_recovery_2026-10-18.json](eval_recovery_2026-10-18.json) |
| 诊断/安全夹具（5 条） | 5/5：冲突显式化、关联排除、记录注入被标记且无工单副作用、用户注入被标记但正常诊断、普通问题无误报 | [eval_diagnosis_2026-10-18.json](eval_diagnosis_2026-10-18.json) |

前端生产构建在非受限 shell 中真实通过：Vite 处理 13 个模块，535 ms，生成约 82.34 kB JavaScript 与 25.97 kB CSS。受限 shell 下 esbuild 子进程会被拒绝，属于本机工具环境限制，不是代码问题。

局限性：这些新增夹具是手写公开样例，仍复用同一批模拟平台记录；5/5 只说明规则模式行为，不能作为提示注入防护的完整性证明，也不能推断模型鲁棒性或统计准确率。

## Docker 全栈验证（2026-10-18）

Docker Desktop（Linux 容器）上使用当前 Dockerfile 与 `requirements.txt` 重新构建镜像，并以独立 Compose 项目 `devtrace-verify` 启动 redis 7-alpine、chromadb 0.5.23、模拟平台与 devtrace 四个容器，全部 healthy。宿主 8000/5173 被本机其他项目占用，未改动那些容器，本栈映射到 18002。

本机网络到 ChromaDB ONNX 模型 S3 地址的传输会停滞，因此构建使用 `--build-arg PRELOAD_ONNX=false`，运行所需模型缓存从本机已有镜像复制进容器；容器内确认 PyJWT 2.15.0、chromadb 0.5.23、fastapi 0.143.3 及本轮新代码存在。普通网络下按默认参数构建会把模型预下载进镜像。

| 检查 | 结果 |
| --- | --- |
| `/health` | 200，`{"status":"ok","service":"devtrace"}` |
| HTTP 规则模式 30 条固定案例 | 30/30 通过，证据 30/30，根因 20/20，平均工具 2.23 次，中位 80.0 ms，无降级 |
| 真实模型模式 4 条样例 | 全部 `decision_mode=model`、计划完成；401/429/Webhook/组合故障工具与假设正确；模型调用 3–5 次/请求，Token 入/出 3154/157、1721/140、1832/135、4944/223，模型耗时 2.3–4.1 s/请求 |
| 真实模型 SSE | 12 个事件，任务 `completed`，`decision_mode=model`，3 次模型调用，Token 2173/154，客户端 4.1 s |
| 真实模型抽样判定（每类 1 条） | 3/3 通过，0 次规则降级，中位 5.0 s |
| SSE 流式 | 16 个事件（started + 15 timeline + complete），任务 `completed` |
| 客户端断流 | 任务立即 `cancelled`，同会话继续返回 200/complete（会话锁已释放） |
| 同会话并发 | 200 + 409 |
| `Idempotency-Key` 重放 | 同一 `run_id`，两次均 200 |
| 跨轮恢复 | `waiting` → 补充 `req_auth_01` → `complete`，读取 2 条最近消息 |
| 组合故障 | 4 次工具调用，2 条假设，不再输出无关 Retry-After |
| 记录内注入 | 标记为疑似注入，未创建工单 |

原始报告：[eval_docker_rules_2026-10-18.json](eval_docker_rules_2026-10-18.json)、[eval_docker_http_2026-10-18.json](eval_docker_http_2026-10-18.json)、[eval_docker_disconnect_2026-10-18.json](eval_docker_disconnect_2026-10-18.json)、[eval_model_http_2026-10-18.json](eval_model_http_2026-10-18.json)、[eval_model_stream_2026-10-18.json](eval_model_stream_2026-10-18.json)、[eval_model_samples_2026-10-18.json](eval_model_samples_2026-10-18.json)。

这次真实 HTTP 验证发现并修复了一个单元测试未暴露的问题：Starlette 在客户端断开时会取消整个响应任务，导致原 `finally` 中的 `cancelled` 状态写入和 Redis 会话锁释放被跳过，任务会停在 `running` 且会话锁最长占用 180 秒。现在改用独立的 shielded 清理任务写入状态，并用 `asyncio.shield` 释放锁；断流测试随后立即得到 `cancelled`，同一会话可继续。另修正了 Dockerfile 的 ONNX 预下载可失败继续、compose 为 8100 端口的沙箱单独设置健康检查。

局限：真实模型只跑了 4 条固定样例与 3 条抽样判定，样本量小、结果依赖外部模型服务，不能作为准确率；前端已在真实浏览器完成登录、诊断、证据/安全卡片与注入场景联调（见下节）。

## 浏览器联调（2026-10-18）

在后端 Docker 栈以真实模型模式运行于 18002 时，用 Vite 7.3.5 开发服务器（5174 端口，`VITE_PYTHON_API_URL=http://127.0.0.1:18002`；5173 被本机其他项目占用）和 Playwright 1.61.1 + Chromium 149（headless）执行真实浏览器流程：打开页面 → 用本地演示账号登录 → 点击 401 示例并发送 → 等待诊断渲染 → 再发送一条带注入文本的投递记录。

| 检查 | 结果 |
| --- | --- |
| 模拟平台连接状态 | 显示"已连接" |
| 诊断正文 | 显示"凭证已过期"及来源 `get_request_trace:req_auth_01` |
| 决策来源 | 显示"配置模型"（`decision_mode=model`） |
| 证据卡片 | 证据可支持、已支持、置信度高、建议处理与来源列表均渲染 |
| 安全边界卡片 | 记录注入被标记"已标记"，列出指令覆盖/凭证外泄诱导与可疑来源，工具观察标注"已按数据处理" |
| 运行历史 / Run ID | 列表与实际 `Run ID` 均显示 |
| 桌面浏览器错误 | console error 0，失败请求 0 |
| 移动端 390×844 | 无横向溢出；用户名/密码/问题输入框均有可访问名称；诊断正文正常渲染；console error 0 |
| 纯键盘登录 | Tab 到"用户名"、输入、Tab 到"密码"、输入、回车登录成功 |
| 焦点可见 | 键盘聚焦输入框时 outline 为 2px 实线 |
| 键盘可达组件与提交 | Tab 可达问题输入框，`Ctrl+Enter` 可提交并渲染诊断 |
| 对比度抽查 | 标题、消息正文、目标/空态/按钮等选择器全部达到 WCAG AA，失败 0 项 |
| 页面性能（dev server） | DOMContentLoaded 62 ms、load 90 ms、FCP 116 ms；点击发送到诊断可见 2,868 ms（含一次真实模型调用） |

截图：[诊断主界面](assets/frontend-e2e-ui-2026-10-18.png)、[证据卡片](assets/frontend-e2e-evidence-2026-10-18.png)、[安全边界卡片](assets/frontend-e2e-security-2026-10-18.png)、[注入场景](assets/frontend-e2e-injection-2026-10-18.png)、[移动端](assets/frontend-e2e-mobile-2026-10-18.jpg)、[可访问性/性能检查页](assets/frontend-a11y-perf-2026-10-18.png)。机器可读结果：[eval_frontend_e2e_2026-10-18.json](eval_frontend_e2e_2026-10-18.json)、[eval_frontend_a11y_perf_2026-10-18.json](eval_frontend_a11y_perf_2026-10-18.json)。

局限：键盘导航、焦点可见、抽样对比度与基础性能已检查，但没有用 axe 做全量规则扫描，也没有屏幕阅读器实测；性能是 Vite dev server 单次数值，不是生产构建的基准。浏览器自动化脚本在仓库外，未纳入 CI。

## 独立 holdout 评测（2026-10-18）

新增 19 条手写标签的第二套公开 holdout 集，使用专用夹具（冲突的密钥记录、v2 缺 scope、关联请求 401/429、英文注入、未知标识等），覆盖冲突、鉴权、限流、Webhook、组合、注入、歧义、异常、负例和多轮。规则模式在进程内 ASGI 模拟平台上运行；模型模式在重建后的 Docker 栈上通过 HTTP 运行，两次使用同一套标签与记录。

| 模式 | 通过 | 分组 |
| --- | --- | --- |
| 规则（进程内） | **19/19** | 冲突 2/2、鉴权 2/2、限流 1/1、歧义 2/2、Webhook 2/2、组合 3/3、注入 3/3、异常 1/1、负例 2/2、多轮 1/1 |
| 真实模型（Docker/HTTP） | **19/19** | 同上，全部 `decision_mode=model` |

原始报告：[eval_holdout_rules_2026-10-18.json](eval_holdout_rules_2026-10-18.json)、[eval_holdout_model_2026-10-18.json](eval_holdout_model_2026-10-18.json)。

过程与局限：模型首轮运行时镜像未包含新夹具，记录 404 导致全部 `waiting`，重建镜像后重跑；首轮后删除了一个要求固定措辞的断言（行为断言保留），这是唯一一次标签调整。标签仍是项目作者手写、与夹具同批产出，属于第二套公开 holdout，不是第三方隐藏基准；19 条样本小、模型只来自一个 provider，19/19 不能推广为真实泛化。

## 跨轮任务记忆评测（2026-10-18）

新增 12 条两轮任务：首轮缺少标识进入 `waiting`，第二轮补充编号；比较"同会话恢复"（工作记忆 + Redis 计划检查点）与"新会话重开"（无状态复用）。规则模式、进程内 ASGI 模拟平台、工作记忆 stub + 假 Redis。

| 模式 | 成功 | 平均总工具调用 | 平均文档检索 | 第二轮文档检索 | 恢复事件 |
| --- | --- | --- | --- | --- | --- |
| 同会话恢复 | **12/12** | 2.67 | 1.17 | 0.17 | 12/12 |
| 新会话重开 | **12/12** | 3.58 | 2.08 | 1.08 | 0 |
| 差值 | 0 | **-0.91** | **-0.91** | **-0.91** | — |

恢复模式平均少约 0.91 次重复文档检索（文档调用约少 44%），总工具调用约少 25%，两种模式诊断成功率相同。原始报告：[eval_memory_tasks_2026-10-18.json](eval_memory_tasks_2026-10-18.json)。

局限：只衡量会话/计划复用，不衡量 ChromaDB 情景记忆质量、模型行为或生产延迟；12 条任务样本小；恢复模式在进程内的平均耗时反而更高（状态序列化开销），不能当作性能结论。

## 公开 API 文档知识库（2026-10-18）

新增面向真实公开 API 的文档问答路径：`backend/developer/public_docs.py` 内置 **641 条**文档。

- 29 条人工摘要：GitHub、Stripe、OpenAI、**DeepSeek**、**Anthropic**、**Gemini**、**通义千问/DashScope**、Twilio、Petstore 与通用 429/503 语义；覆盖认证、限流、Webhook、幂等、上下文超限、流式事件与常见错误码（DeepSeek 的 402 余额不足、Per-account 并发与 user_id 隔离等按官方文档核对）。
- 612 条从 **Stripe 官方 OpenAPI 完整规格**导入：`stripe/openapi` 仓库（MIT 许可，commit `4d2b98a`，`openapi/spec3.json`）的全部 612 个操作；文件 `backend/developer/public_docs_data/stripe_openapi.json`，运行时自动加载。
- 每条记录 `api`、`title`、`content`、`source_url`、`license`、`updated`、`tags`；检索为 BM25 词法排序 + API 过滤 + 中文意图词扩展（如"退款"→refund）。

Agent 新增 `search_public_docs` 工具与 `public_api` 计划路径；模型模式在服务端对第三方 API 名做确定性覆盖，答复给出文档标题、来源链接和"公开文档摘要、不连接真实平台"的声明。

- 规则模式评测：**28 条**问题 **28/28 通过**（12 条概述类 + 6 条 Stripe 接口类 + 10 条 AI API 类），均计划完成、使用 `search_public_docs`、引用对应来源并带公开文档声明；见 [eval_public_docs_2026-10-18.json](eval_public_docs_2026-10-18.json)。
- Docker + 真实模型 HTTP：问题"DeepSeek 402 Insufficient Balance 是余额不足吗"返回 `kind=public_api`、`plan=complete`、工具 `search_public_docs`，答复引用 `api-docs.deepseek.com` 并区分 402 与 429，带公开文档声明；见 [eval_public_docs_http_2026-10-18.json](eval_public_docs_http_2026-10-18.json)。
- 导入命令：`python -m developer.ingest_public_docs --source <OpenAPI.json|Markdown> --api <名称> --source-url <链接> --license <许可> --filter "关键词1,关键词2" --output <json>`；生成文件放入 `public_docs_data/` 即自动加载。
- 局限：Stripe 导入的是完整 612 个操作规格，但规格描述可能滞后于厂商文档；AI API 摘要来自官方公开文档、非全文；中文意图扩展是手工映射；没有调用真实 API，也没有做真实故障注入；许可与归属已随条目记录，商业使用前需自行复核。

## 错误处置手册（2026-10-18）

针对"开发者平台 API 排障"补了一层结构化知识：`backend/developer/error_playbooks.json` 收录 Stripe、GitHub、Twilio 各 12 条高频错误（共 36 条），字段包括 `api`、`http_status`、`error_code`、症状词、含义、`retryable`、`retry_policy`、`recovery`、来源 URL 与更新时间。Agent 新增 `lookup_error_playbook` 工具，在 `public_api` 路径中先查处置手册、再查接口/文档，答复输出"可重试性 + 退避策略 + 处理建议 + 来源"。

| 平台 | 覆盖示例 | 规则模式评测 |
| --- | --- | --- |
| Stripe | 401 invalid key、403 restricted key、429 限流/并发、400 参数、402 拒付、404 资源、409 幂等、Webhook 验签、500/503 | 14/14 |
| GitHub | 401 Bad credentials/过期、403 主/二级限流/SSO、404、422、400、429、500/503、Webhook 验签/投递 | 13/13 |
| Twilio | 401 20003、403 20403/21408、429 20429/队列/日配额、400 21211/21602/21614、404 20404、500/503、Webhook 验签/回调 | 13/13 |
| **合计** | 40 条症状问题 | **40/40** |

- 报告：[eval_error_playbooks_2026-10-18.json](eval_error_playbooks_2026-10-18.json)；Docker 真实模型 HTTP 验证 "Stripe 429 怎么退避" → 计划完成、`lookup_error_playbook` + `search_public_docs`、答复含"可重试/退避策略/处理建议"与 `docs.stripe.com` 来源：[eval_error_playbooks_http_2026-10-18.json](eval_error_playbooks_http_2026-10-18.json)。
- 过程中修复了模型模式的一个真实缺陷：模型给工具传参可能缺少必填字段，现改为缺字段时回退到规则构造的参数，避免"计划等待 + 工具失败"。
- 局限：手册是官方文档摘要，不是完整错误码全集；40 条评测是作者手写症状，校验以关键词为主；未调用真实平台 API；厂商文档更新后需要人工复核。

## 候选计划评分（2026-10-18）

在保持默认行为不变的前提下，增加可选的候选计划评分：

- `planner.build_plan_candidates` 生成 `approved / evidence_first / minimal` 三种安全排序（工具只来自允许集合）；
- 评分：证据覆盖率、有标识时"记录先于文档"、依赖合法、长度惩罚；`choose_plan_candidate` 取最高分，平分保留 approved；
- `DEVTRACE_PLANNING_CANDIDATES=enabled` 时启用（Compose 已开启，默认关闭兼容旧行为），响应 `plan.candidates` 暴露候选、分数与最终选择；
- 评测：11 条 mock 案例（auth/quota/webhook/v2/组合/注入）规则模式 **11/11**，均选中最高分、依赖合法、有标识时记录先于文档；Docker + 真实模型验证 3 个候选、选中 evidence_first（`get_request_trace` 优先）、计划完成；报告 [eval_planning_candidates_2026-10-18.json](eval_planning_candidates_2026-10-18.json)、[eval_planning_candidates_http_2026-10-18.json](eval_planning_candidates_http_2026-10-18.json)；
- 测试：候选评分、开关启用/默认关闭，3 个新用例（测试总数 107）。

局限：评分是启发式，不优化延迟/模型偏好；候选只有三种安全排序，没有真正多分支搜索/ToT；重规划仍为 1 次。

## 记忆管理与质量（2026-10-18）

补齐记忆的可管理性，并给压缩管线加回归：

- 新接口：`GET /developer/memory/{conversation_id}` 导出该用户该会话的工作记忆/摘要/画像；`DELETE` 清空工作记忆、摘要、情景记录和计划检查点（用户画像保留，因为是用户级）。
- `MemoryManager.export_memory / forget_conversation`：Redis 工作记忆/摘要删除、ChromaDB 按 `user_id + conv_id` 删除情景记录；所有导出/删除按 token 用户隔离。
- 质量检查：`developer/evaluate_memory_quality.py` 用确定性摘要器 + 内存 Redis/Chroma 跑压缩管线，校验 3 个关键事实保留、压缩后保留 5 条、情景写入、用户隔离、遗忘清空；报告 [eval_memory_quality_2026-10-18.json](eval_memory_quality_2026-10-18.json)。
- 测试：`tests/test_memory_management.py` 6 个用例（导出、遗忘、profile 保留、用户隔离、两个端点鉴权/计划清理），测试总数 107；Docker + 真实模型 HTTP 验证一次诊断后消息数 2、删除后清空、计划检查点删除，报告 [eval_memory_management_http_2026-10-18.json](eval_memory_management_http_2026-10-18.json)。

局限：质量检查用确定性摘要器和内存替身，**不评测真实模型摘要/画像质量**；遗忘不删用户级画像；情景记忆仍依赖 ChromaDB 0.5.23。（2026-10-21 P5 已补上可配置 TTL、画像删除端点与真实模型摘要评测，见文末"记忆生命周期与真实摘要质量"一节。）

## MCP 协议接入（2026-10-18）

新增标准 Model Context Protocol 的最小实现：

- `backend/mcp/server.py`：JSON-RPC 2.0，支持 `initialize / tools/list / tools/call`，并带 `python -m mcp.server` stdio 入口；默认托管 `search_public_docs` 与 `lookup_error_playbook` 两个工具，复用现有公开文档/错误手册数据。
- `backend/mcp/client.py`：`MCPClient` + `InProcessTransport`（默认，本地演示）与 `StdioTransport`（子进程、按行 JSON）；`call_tool` 解析 `content` 文本并区分 `isError`。
- Agent 集成：`DEVTRACE_MCP_PUBLIC_TOOLS=enabled` 时，`public_api` 路径用 `mcp_lookup_error_playbook` / `mcp_search_public_docs`，由 MCP server 托管；本地 9 个工具保持不变，默认关闭时行为不变。
- 评测：10 条公开 API 问题走 MCP 工具路径 **10/10**（计划完成、仅使用 MCP 工具、带来源与公开文档声明），报告 [eval_mcp_2026-10-18.json](eval_mcp_2026-10-18.json)；Docker + 真实模型验证工具清单恰为 `mcp_lookup_error_playbook → mcp_search_public_docs`、有恢复建议与来源，报告 [eval_mcp_http_2026-10-18.json](eval_mcp_http_2026-10-18.json)。
- 测试：initialize/tools/list/tools/call、未知工具报错、Agent 走 MCP 路径，共 4 个新用例（测试总数 107）。

局限：MCP server 只托管 2 个文档工具，未迁移全部 9 个工具；没有外部 MCP server 配置/发现/鉴权；stdio transport 已实现但自动化评测主要用 in-process transport；协议版本固定 `2024-11-05`。（本节记录 P4 之前的状态；八类只读工具全部托管、`DEVTRACE_MCP_ALL_TOOLS` 路由与外部 stdio server 注册表见文末"MCP 全量只读工具路由与外部注册表（2026-10-19，P4）"。）

## 提示词版本化（2026-10-18）

把散落在 `assistant.py` 的提示词集中到 `backend/developer/prompts.py`：

- 五个 `string.Template`：`SYSTEM / PLAN / ACTION / NATIVE_ACTION / REVISE`，统一由 `PROMPT_VERSION`（当前 `2026-10-18.1`）标识；`render(name, **values)` 显式填充。
- 响应新增 `prompt_version`；`GET /developer/prompts` 只返回版本和每个模板字符数，不返回正文。
- 快照回归：`evaluate_prompts.py` 对固定 fixtures 渲染后计算 SHA-256 存入 `prompt_snapshots.json`，并检查安全契约（"不可信数据""只输出 JSON""ready_steps""不能重复"等）；CI 运行 `python -m developer.evaluate_prompts`，提示词变更必须显式 `--update` 并审阅 diff。
- 测试：快照匹配、契约、未知模板拒绝、元数据不泄露正文、响应带 `prompt_version`，共 5 个新用例（测试总数 107）。
- Docker + 真实模型 HTTP：`GET /developer/prompts` 与响应版本均为 `2026-10-18.1`，元数据不含提示词正文，诊断计划完成、`decision_mode=model`；报告 [eval_prompts_http_2026-10-18.json](eval_prompts_http_2026-10-18.json)，静态回归 [eval_prompts_2026-10-18.json](eval_prompts_2026-10-18.json)。

局限：没有 A/B 分流和逐模型提示词调优；快照只能证明"提示词没被悄悄改"，不能证明改得更好；注入防护仍是正则启发式。

## 原生 tool calling（2026-10-18）

行动选择从"提示词要求模型输出 JSON"升级为"原生工具调用 + 两级降级"：

- `planner.py` 新增 `TOOL_SCHEMAS`（与运行时工具注册表一致），`choose_action` 把当前 ready steps 对应工具的 JSON Schema 作为 Anthropic `tools` 传入，`tool_choice={"type":"auto"}`；
- 模型返回 `tool_use` 时校验必填参数，映射到 ready step；没有 `tool_use` 或参数不足时回退 JSON 提示，再回退规则；provider 不支持 tools 时进程内禁用原生路径（避免每轮失败调用）；
- 指标新增 `native_tool_calls`，可区分"模型原生调用"与"JSON 降级"；
- 测试：原生选择、缺参回退、Schema 覆盖 3 个新用例（测试总数 107）。

真实模型验证（DeepSeek Anthropic 兼容端点，Docker + `POST /developer/chat`）：4 次模型调用中 **3 次为原生 tool_use**，行动顺序 `search_api_docs → get_request_trace → inspect_key_scope`，计划完成、`decision_mode=model`、无规则降级；报告 [eval_native_tools_2026-10-18.json](eval_native_tools_2026-10-18.json)。

局限：只验证 Anthropic 兼容协议的原生工具调用；没有并行 tool use；`TOOL_SCHEMAS` 是手工维护的副本（有测试保证与 `TOOL_TITLES` 覆盖一致，但未自动从注册表生成）；不支持的 provider 会静默回退到 JSON 提示。

## 持久任务队列（2026-10-18）

流式诊断从"API 进程内执行"升级为"Redis 队列 + 独立 worker"：

- 队列：`developer:tasks:queue`（LPUSH/BRPOP）、任务索引 `developer:tasks:queue-index`、死信 `developer:tasks:dead`；
- 任务：`developer:task:{id}` 记录 `queued/running/waiting/completed/failed/cancelled`、`attempts`、`worker_id`、`run_id`；
- 事件：worker 把时间线事件写入 `developer:task-events:{id}`，API 的 SSE 只负责轮询并转发，客户端断开时写 `cancel_requested`；
- 可靠性：worker 执行失败重试一次（`MAX_ATTEMPTS=2`），耗尽进死信；worker 启动时扫描任务索引，把"worker 已消失"的 `running` 任务重新入队；同一会话用 Redis `SET NX` 防止重复排队；
- 部署：`docker-compose.yml` 新增 `worker` 服务（复用 `devtrace-app` 镜像，无端口、禁用 HTTP 健康检查），API 与 worker 均启用 `DEVTRACE_TASK_QUEUE=enabled`；同步接口仍走进程内路径。

验证：

- 单元测试 `tests/test_task_queue.py`：成功完成、瞬时失败重试、重试后成功、失败进死信、取消、同会话去重、worker 重启回收、活 worker 任务不回收；
- Docker + 真实模型 SSE：`started` → 14 个 `timeline` 事件 → `complete`，任务状态 `completed`，`GET /developer/tasks/{id}` 不泄露 payload；报告 [eval_queue_http_2026-10-18.json](eval_queue_http_2026-10-18.json)。

局限：目前只验证单 worker；队列基于 Redis List，没有优先级/延迟队列/定时任务；worker 崩溃后正在执行的那一次会重试（可能重复工具副作用，工单幂等键可防重复），没有多 worker 分片与租约。同步 `/developer/chat` 仍是进程内执行。（本节记录 P3 之前的状态；优先级、租约、延迟重试、死信重放与队列指标见下一节"队列生产化与可观测性"。）

## 队列生产化与可观测性（2026-10-19，P3）

只用 Redis，不引入 Celery/RabbitMQ/Kafka，不改前端、JWT 与 workspace 语义。

- 单元测试：128 → **150 项通过**（新增 `tests/test_queue_production.py` 22 项；本机 `.dsh-venv` Python 3.10）。覆盖：三级优先级入队与未知优先级 `ValueError`、多键 `BRPOP` 的 high→normal→low→legacy 领取顺序、退避序列 1s/2s/4s 与 30s 上限、延迟任务到点后原子搬回**原优先级**队列、重试耗尽进死信、`process_one` 运行中写入并可观察租约、过期租约被回收（`attempts+1`、回到延迟队列、租约清空）、活租约不回收、无租约字段的旧记录不参与租约回收、`SET NX EX` 互斥锁让第二个 worker 不重复回收、`DEVTRACE_TASK_LEASE_SECONDS` 环境变量矩阵（非法值回落默认、低于下限取 30）、重放的 ok/状态不符/不存在三种结果、`task.finished` 审计携带 `trace_id`、自动生成的 trace 为 32 位十六进制；管理端点侧覆盖死信列表 200/403/401 与 `limit=201→422`、重放成功与失败审计、metrics 的队列/死信/延迟/心跳/状态计数、SSE `started` 事件的 trace 一致性、死信 error 字段二次脱敏且不含消息正文。旧测试 `tests/test_task_queue.py` 的 `QueueRedis` 补齐 `llen/lrem/zadd/zcard/zrangebyscore/zrem` 假实现，重启回收用例断言的队列键由 legacy 改为 `normal`（回收已按优先级路由），7 项旧用例语义不变、全部通过。
- Docker HTTP 验证（本机 `devtrace-verify` 栈，API 18002，`--scale worker=2`；验证后 `.env` 已回滚、栈已 `down`，未使用 `down -v`）：
  - 阶段 1（基础链路）18/18 通过：队列模式直发与流式任务完成、任务视图不泄露 `payload/user_id/lease_id/lease_until`、`GET /developer/dead-letter` 与 `GET /developer/metrics` 管理员 200。
  - 阶段 2（租约与死信）16/18 通过：注入过期租约的 running 任务被另一 worker 回收→延迟→提升→完成，全程只执行一次、`attempts=1`、外部看不到租约字段；死信摘要字段齐全且疑似密钥被脱敏；重放 200 后任务重新排队并最终 `completed`，重复重放 409，审计同时存在成功与失败两条 `task.replayed`。两处未通过均为脚本判定问题、不是功能缺陷：① "领取顺序"断言在两个空闲 worker 场景失败——多键 `BRPOP` 会在某个键被写入的瞬间唤醒空闲消费者，严格优先级只在任务同时积压时成立（该边界已写入文档，并由阶段 3 用 worker=0 预排队做确定性验证）；② 重放成功审计的断言读取最新一条事件，命中的是紧随其后故意制造的 409 失败行，数据里两态都在。
  - 阶段 3（严格优先级）5/5 通过：`--scale worker=0` 预排队 legacy/high/normal/low 后同时拉起 2 个 worker，完成顺序为 high → normal → low → legacy，8 个任务每个恰好执行一次。
  - 回归：`verify_public_docs.py` 在同栈通过（HTTP 200、`kind=public_api`、计划 complete、命中公开文档来源与免责声明）。
- 未验证 / 局限：租约回收靠**注入过期租约**验证，没有做 `kill -9` 打断正在执行任务的实测；新端点的非管理员 403 在 Docker 侧未单独造第二个普通账号复测（仅单元测试覆盖）；`/developer/metrics` 是 best-effort 单次快照，状态分布最多统计 500 条索引任务，worker 心跳键 TTL 90 秒，容器强制重建后旧心跳最长残留 90 秒（曾观测到 `workers: 3` 而实际 2 个）；重放对"不存在 / 不在死信 / 状态不是失败"统一返回 409，不区分原因（避免泄露存在性）；仍未做定时任务、任务取消接口、队列按 workspace/租户分区与告警面板；全部为本机演示验证，不构成生产就绪结论。


## 依赖安全检查（2026-10-18）

`pip-audit` 与 OSV 核查确认 `chromadb==0.5.23` 有 3 条漏洞（1 CRITICAL 代码注入、2 HIGH 跨租户授权），OSV 的受影响范围到 `1.5.9`（当前最新版），**没有修复版本**。处置为不升级（升级不能修复且迁移风险大）、保持 ChromaDB 仅在 Compose 内部网络、明确单租户演示边界，并在 CI 增加审计门禁（只阻断已有修复版本的漏洞）。新增回归测试覆盖内部服务不发布端口、`.env` 不进 Git/镜像、镜像非 root 等部署约束。完整评估见 [依赖与安全评估_2026-10-18.md](依赖与安全评估_2026-10-18.md)。本机重跑 `pip-audit` 时 PyPI JSON 请求停滞，完整扫描沿用 2026-10-17 的 91 依赖报告（仅这 3 条）并以 OSV 官方数据核对。

## RBAC 与审计日志（2026-10-19）

- 单元测试：107 → **115 项通过**（新增 `tests/test_audit.py` 8 项；本机 `.dsh-venv` Python 3.10）。覆盖：管理员 200 / 非管理员 403 / 匿名 401 / `DEVTRACE_ADMIN_USERS` 为空时全员 403；登录成功与失败事件不含密码；`chat.sync` 事件不含消息正文（detail 仅 `kind`/`latency_ms`）；fake Redis `xadd` 抛异常与无 stream 客户端两种情况 chat 仍 200；bytes decode、`user`/`event` 过滤、最新优先、`limit=0/201/abc` 拒绝 422；`detail` 白名单外键丢弃、`sk-`/`Bearer ` 替换 `<redacted>`、200 字符截断；真实 `DeveloperAssistant` 规则模式跑 `req_auth_01` 每条工具调用写一条 `tool.call` 且不含观察载荷；`api/worker.py` 的 `process_one` 写 `task.finished`。
- Docker HTTP 验证（本机 `devtrace-verify` 栈，API 18002）：管理员（demo）读 `GET /developer/audit` **200**，非管理员（本机临时 guest 用户）**403**，匿名 **401**，`limit=500` **422**；`X-Request-ID: p1-audit-verify-01` 回写响应头并出现在 `chat.sync` 事件的 `request_id`；直发 chat + 队列 SSE 后 26 条事件覆盖 `auth.login.success/failure`、`chat.sync`、`tool.call`、`task.enqueued`、`task.finished`、`memory.export`、`prompts.read`、`audit.read`；对整份响应检索：不含两个口令、不含 `Bearer`、不含 `sk-`。回归：`verify_queue.py` 队列 SSE 全链路通过。报告在工作区根目录 `eval_audit_docker_2026-10-19.json`（本机文件，不入库）。
- 局限：单租户；"append-only"是应用层只追加（Redis 可被运维删改，非 WORM），流按 `maxlen≈10000` 近似裁剪；角色仅管理员/普通两档；评测样本为本机验证，非第三方审计基准。

## 工作区隔离（2026-10-19，P2）

- 单元测试：115 → **128 项通过**（新增 `tests/test_workspace.py` 13 项；本机 `.dsh-venv` Python 3.10）。覆盖：旧/新/混合格式用户配置解析与非法配置启动即 RuntimeError（workspace 名非法、缺 password、未知字段、类型错误）；run 详情与任务查询跨 workspace 一律 404、owner 200、无 workspace 字段的旧记录视为 `default` 仍可读、配置迁移后不一致记录 404；`task_queue.enqueue` 写入 workspace 且默认 `default`（签名向后兼容）；`process_one` 的 `task.finished` 审计与 run 记录带 workspace；审计事件 workspace 继承（contextvar）、旧事件读为 `default`、`?workspace=` 过滤、非法过滤值 422、非管理员 403；记忆导出/删除结果含 workspace；登录事件按用户配置打 workspace 且不含口令。
- Docker HTTP 验证（本机 `devtrace-verify` 栈，API 18002，两个临时 workspace 用户 wsusera(ws-a，管理员)/wsuserb(ws-b)，验证后 `.env` 已回滚、账号移除）：31 项检查全部通过——A/B 登录 200；A 的 chat 200，其 run B 读 **404**、A 读 200；B 队列 SSE 任务记录 workspace=ws-b，A 读该任务 **404**、B 读 200；记忆导出返回 `ws-a`；`GET /developer/audit` 200 且同时含 ws-a/ws-b 事件，`?workspace=ws-a` 过滤后仅 ws-a，`workspace=not valid!` 422，非管理员 403，匿名 401；`task.finished` 由 worker 打 `ws-b` 标签；整份审计响应检索不含两个口令、`Bearer`、`sk-`。回归：`verify_queue.py` 队列 SSE 全链路通过（started/15 timeline/complete/task_completed/无 payload 泄漏）。报告在工作区根目录 `eval_workspace_docker_2026-10-19.json` 与 `verify_workspace.py`（本机文件，不入库）。
- 局限：workspace 是"归属 + 可见性边界"，不是硬多租户——memory/ChromaDB 仍按 user+conversation 隔离、未按 workspace 重新分区；无共享 workspace 成员、邀请或跨租户管理后台；workspace 迁移（同一用户改配置）会让旧记录按 404 隐藏，不做数据搬迁；仍是本机演示，非生产权限系统。

## MCP 全量只读工具路由与外部注册表（2026-10-19，P4）

- 范围：`DEVTRACE_MCP_ALL_TOOLS=enabled` 时把 `auth` / `quota` / `webhook` 三类诊断的 6 个只读工具改由 MCP server 托管（`mcp_` 前缀），MCP server 侧共托管 8 个只读工具；`create_support_ticket` 属于写动作，始终留在本地。新增 `backend/mcp/registry.py` 支持 `DEVTRACE_MCP_SERVERS`（JSON 数组 `{name,command,args}`，**仅 stdio**）接入外部 MCP server。默认两个开关都关闭，行为与 P4 之前一致。
- 单元测试：150 → **201 项通过**（新增 `tests/test_mcp_all_tools.py` 26 项 + 20 subtest、`tests/test_mcp_registry.py` 25 项 + 10 subtest；本机 `.dsh-venv` Python 3.10）。路由侧覆盖：server `tools/list` 恰为 8 个只读工具且无工单工具、stdio 入口异常返回 `isError` 不崩溃、`auth`/`quota`/`webhook` 计划名全部 `mcp_*`、运行中新追加的步骤保持同一传输、工单动作保持本地、开关关闭时名称不变、**本地与 MCP 路由对同一案例产出结构一致的计划/证据/答案**、v2 版本标识重置逻辑在 MCP 拼写下同样生效、MCP 失败**不回退**本地工具、观察摘要使用"MCP 返回"、planner/verifier 一律按 base 名比较。注册表侧覆盖：空/非法/非数组配置不起子进程只告警、server 名与工具名字符集校验、默认拒绝（只读白名单命中才注册，写语义工具与写语义参数被拒）、`ext_<server>_<tool>` 命名、与内置工具重名跳过、连接/`tools/list` 失败只跳过该 server、`user_id/conversation_id/idempotency_key/ticket_id/api_key` 等 server 侧持有参数被剥离且实测只转发白名单字段（fake server 收到的参数恰为 `["query"]`）。
- Ruff：CI `--select F` 清单新增 `developer/planner.py`、`mcp/runtime.py`、`mcp/registry.py` 与三个新测试文件；本地对 P4 全部改动文件运行 `All checks passed!`。**披露**：`mcp/tool_manager.py` 存在一处历史遗留 F402（`_validate_params` 里循环变量 `field` 遮蔽了 `dataclasses.field` 导入），已用 `git show HEAD~2:backend/mcp/tool_manager.py` 证明早于本次改动存在，按"不顺手修范围外缺陷"未改动，也因此未加入 CI 清单。
- Docker HTTP 验证（`devtrace-verify` 栈，API 18002，`--scale worker=2`，**真实模型 `decision_mode: model`**、非规则模式；验证后 `backend/.env` 已回滚、栈 `down` 且未使用 `-v`）：
  - 开关 `enabled`：auth → `mcp_get_request_trace / mcp_search_api_docs / mcp_inspect_key_scope`（4 条证据）；quota → `mcp_get_request_trace / mcp_search_api_docs`（3 条）；webhook → `mcp_get_webhook_delivery / mcp_search_api_docs / mcp_get_related_request_trace / mcp_search_quota_docs`（6 条）；三条计划均 `complete`。
  - 工单案例（`req_auth_01` + "请创建工单"）：只读证据走 `mcp_*`，写动作恰为一个本地 `create_support_ticket`，响应中不存在 `mcp_create_support_ticket`，计划 `complete`、5 条证据。
  - 关闭回归（开关置空）：同四条案例的工具名全部回到本地拼写、无任何 `mcp_` 前缀、`status` 仍 `complete`、证据数 4/3/6/5 与开关打开时一致。
  - 验证脚本 `p4_verify_docker.py` 与报告 `.p4_docker_report.json` 在工作区根目录、不入库；脚本对上述断言全部通过（两种开关各一次）。
- 未验证 / 边界：Docker 栈内的 MCP 调用走 `InProcessTransport`，**外部 server 的真实 stdio 子进程只用自带的 `tests/fake_ext_mcp_server.py` 在单元测试中验证**，没有接入任何真实第三方 MCP server，也没有在容器里拉起外部进程，因此"第三方 server 行为正确"未经验证——注册表提供的是边界（默认拒绝只读白名单、写语义不注册、剥离 server 侧持有参数、重名/连接失败只跳过该 server、外部工具永不进入诊断计划）。没有 HTTP/SSE 传输、没有 OAuth 或任何 server 鉴权、没有多 server 健康检查与自动重连（熔断只在 `MCPToolManager` 层）。MCP 失败不回退本地同名工具，这是设计决定：静默回退会让同一次诊断的证据来源不可追溯。全部为本机演示级验证，不构成生产就绪结论。

## 记忆生命周期与真实摘要质量（2026-10-21，P5）

- 范围：`DEVTRACE_MEMORY_TTL_SECONDS` 让 Redis 侧工作记忆与摘要的过期时间可配置（默认 86400、下限 3600、非法或未设置回落默认，替换原先三处硬编码 86400）；`MemoryManager.forget_profile` + 新端点 `DELETE /developer/profile` 只删调用者本人的画像文档（同时清 `where user_id` 的历史格式文档）并写审计 `memory.profile_delete`；`export_memory` 新增 `exported_at`（ISO 秒）与 `workspace`，且**不**返回内部画像文档 id，原有 `messages/summary/user_profile/counts` 字段保持不变。ChromaDB 侧的情景记忆与画像**没有 TTL**，只能靠显式 API 删除——这是本阶段确定的生命周期边界，已写入模块说明与 `.env.example`。
- 单元测试：201 → **224 项通过 + 103 subtest**（新增 `tests/test_memory_lifecycle.py` 23 项；本机 `.dsh-venv` Python 3.10）。覆盖：TTL 解析（未设置/空值→86400、`7200`→7200、`60`→3600 取下限、`not-a-number`/`3600.5`/`86400s`/空白→回落默认）、构造函数显式值优先于环境变量、fake Redis 记录到的 `expire`/`setex` TTL 恰为配置值且工作记忆 key 与摘要 key 都被覆盖；画像删除（当前文档 + legacy 文档一起删、删后 `_get_profile` 为空、会话消息与情景记录保留、A 删除不影响 B、无画像时 `profile_deleted=false`）；导出新增字段（`exported_at` 可被 `fromisoformat` 解析、`workspace` 显式值与 `default` 回退、旧字段齐全、内部文档 id 不出现在序列化结果里）；`DELETE /developer/profile` 端点（匿名 401、登录 200 且 `workspace` 取自服务端配置、审计事件落 stream、响应与审计都不含画像内容/消息正文/口令/文档 id、删除后另一用户画像仍可读且本人会话消息未少）；live 评分口径（满保留、编造标识符、超长摘要、8/10 阈值聚合、无密钥 `skipped=true` 且 `passed=true`）、以及合成对话确实 ≥20 轮且埋 3 个事实。**旧 201 项无一改动、无一失败。**
- Ruff：CI `--select F` 清单新增 `tests/test_memory_lifecycle.py`、`developer/evaluate_memory_quality_live.py`、`memory/conversation_memory.py`（共 49 个文件），本地按 CI 同参数整表运行 `All checks passed!`。P4 披露的 `mcp/tool_manager.py` 历史 F402 仍未处理、仍不在清单内。
- 确定性压缩评测（CI）：`developer/evaluate_memory_quality.py` 本机重跑 `passed=true`，事实保留 3/3、压缩后工作记忆 5 条、情景写入 1 条、导出含摘要、用户隔离与遗忘清空均通过。
- 真实模型摘要评测（**不进 CI**，会花真实调用）：`developer/evaluate_memory_quality_live.py` 生成 10 场 × 22 轮合成对话，每场埋"请求编号 + 密钥前缀 + 结论"三个事实，全部走生产 `MemoryManager._compress` 提示词路径（每场恰好触发一次压缩，共 10 次真实摘要调用），Redis/Chroma 用内存替身。结果（provider 为本机 `.env` 里已配置的 Anthropic 兼容端点，模型 `deepseek-chat`，报告不读取也不打印任何密钥）：
  - 逐字保留：**请求编号 10/10、密钥前缀 10/10、结论 6/10**；`perfect_sessions=6`、`fact_retention_rate=0.6`。
  - 4 场失分全部是结论被**语义等价的改写**（如埋入"凭证已过期，需要轮换"，摘要写成"最终结论是凭证已过期需轮换"——逗号被吞掉导致逐字匹配失败），不是事实丢失；`fabricated_identifiers=0`（没有任何编造的请求号/密钥串）、`overlong_summaries=0`（最长 139 字，远低于 800 上限）；摘要延迟 mean 1026.8ms / max 1182ms。
  - 阈值 `perfect_sessions>=8 且 fabricated=0 且 overlong=0` **未达成**，脚本按设计打印结果后以退出码 1 结束。原始报告 [eval_memory_quality_live_2026-10-21.json](eval_memory_quality_live_2026-10-21.json)（卡片建议文件名日期为 2026-10-19，实际运行日是 2026-10-21，按真实日期命名并在此披露）。
  - 无密钥分支本机验证：清掉环境变量后输出 `{"skipped": true, "cases": 0, "passed": true}`、退出码 0。
  - 成本披露：第一次运行时 Windows 把容器内 `/tmp` 输出路径转换成了宿主机路径，10 次摘要调用已经完成但报告无法写出，随后关闭路径转换重跑一次，因此本阶段共消耗 **20 次**真实摘要调用。
- Docker + HTTP 验证（`devtrace-verify` 栈，API 18002，`--scale worker=2`，镜像 `--build` 重建后运行，`decision_mode: model`；结束后 `down` 且**未使用 `-v`**）：
  - TTL 真实生效：`docker compose exec -e DEVTRACE_MEMORY_TTL_SECONDS=…` 注入三种取值，**没有改动本机 `backend/.env`**（因此无需回滚）。默认 86400 → 工作记忆 key `TTL=86400`、摘要 key `TTL=86399`；`7200` → 7200/7199；非法下限测试 `60` → 配置被钳到 **3600**，Redis 实际 TTL 3600/3599。三种取值 `ttl_applied` 均为 true（探针 `p5_ttl_probe.py` 只打印数字与 key 名，Redis 连接串留在容器内）。
  - 记忆导出：登录后先跑一次真实诊断（`req_auth_01`，会话 `p5-lifecycle`），`GET /developer/memory/p5-lifecycle` **200**，响应含 `exported_at=2026-10-21T14:19:08`、`workspace=default`、`counts={messages:2, summary_chars:0, has_profile:true}`，消息带 `ts`。
  - 画像删除：先用生产文档 id 生成器（`_profile_doc_id`）往真实 Chroma 写入一条合成画像，`DELETE /developer/profile` **200**，返回 `profile_deleted=true`、`deleted_documents=1`、`conversation_memory_kept=true`、`episodic_kept=true`、`workspace=default`；再次导出 `has_profile=false` 而**消息仍为 2 条**（证明只删画像）。
  - 鉴权与脱敏：无 token 时 `DELETE /developer/profile` **401**、`GET /developer/memory/{id}` **401**；画像删除响应与两次导出响应中均不含口令、不含内部文档 id `user_profile:`、不含消息正文标记；容器内直接读 Redis 审计流（探针，口令经容器内文件读取、不经命令行）：扫描 274 条事件，`memory.profile_delete` 恰 1 条（actor=demo、resource=profile、status=success、workspace=default、`detail` 仅 `kind`/`success` 两个白名单键），整条流里检索不到消息正文标记、画像偏好标记、口令与 `user_profile:` 文档 id。验证脚本与报告（`p5_verify_http.py`、`p5_ttl_probe.py`、`p5_seed_profile.py`、`p5_audit_probe.py`、`.p5_http_report.json`、`.p5_audit_report.json`）放在工作区根目录，不入库。
- 未验证 / 边界：**跨用户画像隔离只在单元测试用替身验证，Docker 侧只有 demo 一个账号，没有再造第二个真实用户复测**；真实模型质量结论只覆盖"10 场单人模板合成对话 + 单一 provider"，评分是逐字匹配，对中文标点/省略写法偏严，也不评测画像准确性与情景检索召回；ChromaDB 情景记忆与画像无 TTL、无定时清理、无批量导出与按 workspace 分区，画像只能整份删除没有编辑界面，记忆未加密；Redis 里与记忆无关的 86400 硬编码（计划检查点 `assistant.py:617/837`、运行记录 `:838`、历史索引 `:843`、任务记录 `api/main.py:42`、幂等缓存 `:436`、任务事件流 `task_queue.py:21`；会话锁是 180 秒不在此列）不在本阶段范围，未改动；JWT/队列/MCP 语义未触碰。全部为本机演示级验证，不构成生产就绪结论。

## 评测扩展与发布收尾（2026-10-21，P6）

- 范围：修掉 P4/P5 一直披露的历史 F402；把真实模型摘要评分从单一逐字口径改成"按事实类别判定 + strict/semantic 两列同时报告"；holdout 从 19 条扩到 40 条并按标签批次分别复跑；对队列化流式入口做一次轻量并发/负载验证；把所有文档数字统一到同一份实测结果。约束：不改压缩提示词、不改 P5 已验收的 API 行为、不收敛其余 6 处硬编码 86400（继续记为债务）、不为了通过而改阈值或删失败样本。
- 历史缺陷修复（`7e8087a`）：`mcp/tool_manager.py` 的 `_validate_params` 里循环变量 `field` 更名为 `field_name`，不再遮蔽 `dataclasses.field` 导入（F402），并把该文件加入 CI `--select F` 清单；`tests/test_holdout_cases.py` 一并进入清单。CI Ruff 清单 49 → **51 个文件**，本地按 CI 同参数整表运行 `All checks passed!`。P4/P5 两段报告里"该缺陷未处理、不在清单内"的记录按原文保留，不改写已验收阶段的历史。
- 双口径评分（`12b0e45`）：`developer/evaluate_memory_quality_live.py` 对每场的三类事实分别判定，报告同时输出 `strict`（请求编号逐字、`密钥前缀 + 权限范围` 逐字、结论整句逐字）与 `semantic`（请求编号**仍要求逐字**；权限范围允许 `ACTION_SYNONYMS` 中的同义动作表述；结论按 `concept_groups` 概念组命中，命中前先用 `_negated()` 在归一化文本的 2 字窗口内检查否定词，因此"凭证未过期，也不需轮换"永远不计为保留）。门禁只看 semantic：`semantic perfect >= 8/10 且 fabricated_identifiers = 0 且 overlong_summaries = 0`，未达即退出码 1；`thresholds.rubric = "semantic"` 明确写出 strict 列只用于与 P5 那次单口径运行对照、从不参与门禁。每条结果新增 `missing_facts` 与三类分项，汇总新增 `by_category`。生产 `_compress` 提示词与压缩路径逐字未动。
- 真实模型复跑（10 场 × 22 轮、`deepseek-chat`、Anthropic 兼容端点，Redis/Chroma 仍是内存替身）：
  - `strict`：`perfect_sessions=6`、`fact_retention_rate=0.6`，分项 编号 10/10、密钥权限 10/10、结论 6/10。
  - `semantic`：`perfect_sessions=10`、`fact_retention_rate=1.0`，分项 10/10/10；`fabricated_identifiers=0`、`overlong_summaries=0`（最长 139 字，上限 800）；摘要时延 mean 984.9ms / max 1334ms；`passed=true`、退出码 0。
  - 4 场 strict 失分逐条人工核对过原文与摘要，全部是标点合并或用词替换、不是事实丢失："凭证已过期，需要轮换"→"凭证已过期需轮换"；"分钟配额耗尽，按 Retry-After 退避"→"分钟配额耗尽，应按 Retry-After 退避"；"重试风暴触发限流，需要指数退避"→"重试风暴触发限流，需采用指数退避"；"旧密钥已轮换，缓存里还是旧值"→"旧密钥已轮换但缓存仍为旧值"。semantic 口径没有放松标识符：编号在两列下都必须逐字，且否定改写有专项单测守着。
  - 报告 [eval_memory_quality_live_2026-10-21_r2.json](eval_memory_quality_live_2026-10-21_r2.json)（按真实运行日 2026-10-21 命名，`_r2` 表示同一评分脚本的第二代口径）；P5 的单口径报告 [eval_memory_quality_live_2026-10-21.json](eval_memory_quality_live_2026-10-21.json) 原样保留以便追溯。成本披露：本次复跑消耗 **10 次**真实摘要调用（一次跑成，无重试）。无密钥分支复测仍为 `{"skipped": true, "cases": 0, "passed": true}`、退出码 0。
- holdout 19 → 40 条（`b73b593`、`ad99f5e`）：新增 21 条手写标签案例，覆盖 auth/quota/webhook/combo/conflict/anomaly/ambiguity/injection/multiturn/public_api/negative 组，配套 fixture 加在 `developer/data.py`；`evaluate_holdout.py` 给每条结果打上 `batch`、汇总新增 `by_batch`，并新增 `filter_batch()` + `--batch` 参数（未知批次直接报错，不会静默空跑）。
  - 规则模式整表 **40/40**，其中原批 `2026-10-18` 19/19、新批 `2026-10-21` 21/21；报告 [eval_holdout_rules_2026-10-21.json](eval_holdout_rules_2026-10-21.json)。CI 里 `evaluate_holdout --mode rules` 跑的就是这 40 条。
  - 真实模型模式为控制成本**只跑新批次 21 条**：通过 **18/21**（`public_api` 0/2、`auth` 2/3，其余各组全过），报告 [eval_holdout_model_2026-10-21_new_batch.json](eval_holdout_model_2026-10-21_new_batch.json)。成本披露：这一批经 HTTP 打到本机已配置的 Anthropic 兼容端点，21 条案例各发一次真实诊断请求（每条请求内部还有多轮工具决策与最终作答的模型调用，本轮没有单独统计 LLM 调用次数）。3 条失败原样列出，未删除也未改断言：
    - `h_public_playbook_path`、`h_public_docs_path`：`reasons = missing_tool:lookup_error_playbook / search_public_docs`，但响应里实际执行的是 `mcp_lookup_error_playbook`、`mcp_search_public_docs`。验证栈开着 `DEVTRACE_MCP_ALL_TOOLS`（**归因更正，见 P8 段落：P8 全量复跑时 `DEVTRACE_MCP_ALL_TOOLS` 并未设置，只有 base compose 默认的 `DEVTRACE_MCP_PUBLIC_TOOLS=enabled`，同样两条失败原样复现——所以这两条失败的触发条件是 PUBLIC_TOOLS，不需要 ALL_TOOLS。这是对本段归因的修正，P6 当时栈里是否真的开过 ALL_TOOLS 无法从现有证据复核，故按协议保留原文**），工具走 MCP 传输后带 `mcp_` 前缀，而标签按本地拼写断言——这是标签绑定传输拼写，不是能力缺失（P4 的 base 名比较只在计划/校验层，`tools` 字段保留传输拼写供人追溯）。
    - `h_quota_v2_doc_version_boundary`：`reasons = evidence_missing:doc:v1 / 速率限制`，模型带回了 `get_request_trace` + `search_api_docs` 但没命中 v1 版本文档这一具体证据项；规则模式里这条靠文档桩才稳定通过。
    - 三条的标签绑定已写进案例的 `label_scope` 字段（说明它绑定规则模式文档桩或本地工具拼写），断言值本身没有因为看到结果而修改；改完后规则模式复跑仍 40/40。
- 轻量并发/负载验证（`devtrace-verify` 栈，API 18002，`--scale worker=2`，**rules 决策模式**、零付费模型调用；模式通过工作区根目录未入库的 compose overlay 注入，`backend/.env` 全程未读未改）：报告 [eval_load_2026-10-21.json](eval_load_2026-10-21.json)，被测代码 `ad99f5e`，整轮墙钟 8.2 秒。
  - 10 并发 `/developer/chat/stream`：**10/10 都是 200 且都以 `complete` 收流**，共 134 条 timeline 事件；时延 min 415 / p50 1049 / p95 = max 1859 / mean 1105.3ms，首事件 p50 29 / p95 31ms；10 个 `run_id`、10 个 `task_id` 全不重复。
  - 任务落地核对：10 条任务记录状态全为 `completed`、`attempts` 取值集合只有 `[0]`、任务里的 `run_id` 与 SSE 事件一致（10/10）；逐个 `GET /developer/runs/{run_id}` 10/10 命中且 id 一致；运行历史索引里没有重复 `run_id`（该接口只返回最近 50 条，因此不用它证明每个 run 都在）。
  - 同一会话并发 3 请求：**1 个 200 跑完、2 个 409**（"该请求或会话正在处理中"），完成 1 个 run——会话锁把并发挡在执行之外，未观察到同一会话重复执行。
  - 相同 `Idempotency-Key` 并发 3 请求 + 完成后再放 1 次：竞争窗口内 **1 个 200、2 个 409**，完成后的重放返回 **200 且 `run_id` 与首次完全相同**（`replay_reuses_first_run=true`）——缓存复用生效，一个幂等键只对应一次真实执行。
  - 限流：同步 `/developer/chat` 连打 36 次 → **26×200 + 10×429**，与 30 次/60 秒/用户的窗口一致（先前两次独立运行得到同一 26/10 分布）；队列化 `/developer/chat/stream` 连打 24 次 → **24×200、0 个 429**（p50 1988 / p95 3063 / max 3257ms）。
  - **负面发现（记录但未修改）**：`/developer/chat/stream` 入口没有调用 `limit()`，因此流式路径不受 30 次/60 秒的用户级限流约束，只有同步路径受限。本阶段按"不改队列与 API 语义"的要求只把它写进文档与债务清单。（该项已在 **2026-10-22 P7** 修复并复验，见本节末尾的 P7 段落；此处原文保留。）
  - 汇总：整轮 77 次请求、35 次以 `complete` 收流、36 个不同 `run_id`，无异常/失败行。`GET /developer/metrics` 对演示用户是 **403**（该端点仅管理员，未为此改动账号权限），故本轮没有队列深度指标。
  - 边界：单机 Docker、2 worker、1 个 Redis 实例、1 个 Chroma 容器、单一演示用户、n=10 的时延样本；这是"能不能并发跑通、锁与限流是否按语义生效"的存在性验证，**不是容量压测**，不能据此推断生产吞吐或 p99。
- 单元测试：224 → **240 项通过 + 199 个 subtest**（本机 `.dsh-venv` Python 3.10）。新增 `tests/test_holdout_cases.py` 11 项（案例结构/expect 键合法性、40 条分组与批次数量、`filter_batch` 选择与原批回归、未知批次抛 `ValueError`）；`tests/test_memory_lifecycle.py` 由 23 → 28 项，其中 `LiveQualityRubricTests` 覆盖**语义等价必须通过**（semantic 满分、strict 只保留 1 项且 `missing_facts=[密钥权限, 结论]`）、**否定表述绝不通过**（"凭证未过期，也不需轮换"→ 结论在两列都判 false，编号与权限仍 true）、否定后接肯定复述仍算保留、权限同义计入而错误动作不计入、编号必须逐字、编造标识符与超长摘要被标记、报告在 10 场全逐字时两列都满分、每 4 场做语义改写时 semantic=10/strict=7 且 `thresholds.rubric=="semantic"`、丢 3 场事实或编造 1 个标识符都判不通过。**旧 224 项里除口径改造外无一改动、无一失败。** 注：Windows 上整表首跑出现过一次 `ResourceWarning`（未关闭的传输对象，与断言无关）的告警汇总，复跑即无，按实披露不隐藏。
- 未验证 / 边界：semantic 口径的结论概念组仍是作者为这 10 场模板对话手工挑选的关键词集合，样本仍是单人模板、单一 provider，只证明"埋入的事实能否活过压缩提示词"，不评测画像准确性、情景检索召回与真实任务成功率；模型模式只跑了新批 21 条，原 19 条没有用模型复跑（该项已在 **2026-10-22 P8** 用全量 40 条复跑，见本节末尾的 P8 段落；此处原文保留）；负载验证是 rules 模式的小样本存在性检查，未测 worker 崩溃恢复、Redis 故障切换与多租户混合流量。全部为本机演示级验证，**不构成生产就绪结论**。

## 流式入口限流与 Redis TTL 收敛（2026-10-22，P7）

- 范围：修掉 P6 负载验证留下的两个确定性问题——`/developer/chat/stream` 入口不调用 `limit()`（24 次流式请求 0 个 429），以及 6 处散落的 Redis 86400 硬编码。约束：不改 JWT/workspace/队列/MCP 语义、不动 P1–P6 已验收行为、不把记忆 TTL 混进来、不为了测试通过而放宽阈值或隐藏 429。
- 流式限流实现：`api/main.py` 新增 `enforce_chat_limit(user_id)`（同一把 `developer:chat-limit:<sha256(user)>`、30 次/60 秒、`limit()` 增加可选 `retry_after` 并回 `Retry-After` 头），同步路由改为薄壳、真实逻辑移到内部 `_chat_core(req, user_id, idempotency_key, *, enforce_limit=True)`。`/developer/chat/stream` 在**返回 StreamingResponse / 入队之前**消耗配额，进程内 SSE 的执行体改调 `_chat_core(..., enforce_limit=False)`，因此**一次 stream 只扣 1 个配额**。`enforce_limit` 只是内部函数关键字参数，绝不作为路由参数暴露（FastAPI 会把带默认值的标量变成查询参数，那样 `?enforce_limit=false` 就能绕过限流）——这条有专门的回归测试用签名检查 + 真实 POST 双重守着。超限写审计 `chat.rate_limited`，`detail` 只含 `{kind, status, reason_code}` 白名单字段，不含消息正文。同步入口的 429 也补上 `Retry-After: 60`。前端只在 `requestDeveloperChat` 里给非 2xx 错误补一句"（请约 60 秒后重试）"，UI 未重做。
- SSE 语义边界（需要明确）：429 是**建立连接之前的 HTTP 层错误**，响应体是普通 JSON 而不是流内 `error` 事件；连接一旦建立（收到 `started`）就不再中途限流，此后只能靠 150 秒执行上限、会话锁与断流取消收尾。被拒请求不创建任务记录、不入队、不写 `started`。
- TTL 收敛：新增 `developer/ttl.py`（`DEFAULT_REDIS_TTL_SECONDS=86400`、`MIN_REDIS_TTL_SECONDS=60`、`parse_redis_ttl_seconds()`、`redis_ttl_seconds()` 每次调用时读 `DEVTRACE_REDIS_TTL_SECONDS`，空/非法回落默认、合法值钳到不低于 60）。替换的写入点按 grep 复核是 **7 个代码点 / 卡片列的 6 处位置**：任务记录（`api/main.py` 的 `_write_task_record`/`_touch_task`/`_mark_interrupted` 共用一处常量 `TASK_TTL_SECONDS`，已删除）、幂等结果缓存、计划检查点（`assistant.py` 两处 `setex`）、运行记录、运行历史索引 `expire`、任务事件流（`task_queue.py` 的 `save_task`/`append_event`，`EVENTS_TTL_SECONDS` 已删除）。会话锁固定 180 秒、限流窗口固定 60 秒不属于此项，未改。`DEVTRACE_MEMORY_TTL_SECONDS` 与 `.env.example` 的记忆块逐字未动，只在后面新增注释示例 `# DEVTRACE_REDIS_TTL_SECONDS=86400`。
- 单元测试：240 → **258 项通过 + 211 个 subtest**（本机 `.dsh-venv` Python 3.10）。新增 `tests/test_stream_limits.py` 10 项：单次 stream 恰好扣 1 个配额并产生 1 条任务记录、30 次 stream 全 200、第 31 次 429 + `Retry-After: 60` + 固定文案且不新增任务记录/不动执行次数/审计只多 `chat.rate_limited`、sync 与 stream 共用配额、sync 429 同样带 `Retry-After`、不同用户互不影响、队列路径在入队前被拦（用 spy 替换 `_queued_stream`，断言前 30 次进 spy、第 31 次 429 且没有 `developer:task:*` 也没有 `task.enqueued`）、拒绝响应与审计都不含消息标记与明文用户、`enforce_limit` 不可经查询参数触达、拒绝不留悬挂会话锁。新增 `tests/test_redis_ttl.py` 8 项 + 12 个 subtest：非法值回落表、钳制表（`120→120`、`59→60`、`0→60`、`-1→60`）、调用时读 env、记忆 TTL 与 Redis TTL 互不串（120 与 7200 各自生效）、任务记录三写、`_stamp_run` 与真实 `/developer/chat` 的幂等缓存、`task_queue.enqueue`/`append_event`、以及真实 assistant 跑一轮后计划检查点/运行记录/历史索引 TTL 全等于配置值。CI 的 Ruff `--select F` 清单 51 → **54 个文件**，本地按 CI 同参数整表 `All checks passed!`。**旧 240 项无一改动、无一失败。**
- Docker / HTTP 验证（`devtrace-verify` 栈，API 18002，`--scale worker=2`，**rules 决策模式、零付费模型调用**；模式与 TTL 都由工作区根目录未入库的 compose overlay 注入，`backend/.env` 全程未读未改；验证后 `down` 不加 `-v`）。整轮证据见 [eval_stream_limit_2026-10-22.json](eval_stream_limit_2026-10-22.json)：
  - 队列路径（`DEVTRACE_TASK_QUEUE=enabled`，即 base compose 默认）：单条 stream → 200、`quota_units_spent=1`、`task_records_created=1`；31 条并发 stream → **29×200 + 2×429**，`Retry-After` 取值只有 `["60"]`、拒绝文案只有 `请求过于频繁，请稍后重试`、`rejected_with_started_event=0`、`rejected_with_task_id=0`、消息内埋的标记 `marker_in_rejected_bodies=false`、29 个不同 `run_id`；副作用核对 `task_records_created=29 == expected_task_records=29`。烧完配额后立刻发同步 `/developer/chat` → **429 + `Retry-After: 60`**，`task_records_created=0`、`run_records_created=0`，证明两条入口共用同一窗口且拒绝无副作用。
  - 进程内 SSE 路径（额外 overlay 设 `DEVTRACE_TASK_QUEUE=disabled`）：同一组数字复现（29×200 + 2×429、全部带 `Retry-After: 60`、0 个 `started`、0 个 `task_id`），并用 `docker compose logs worker | grep -c "task enqueued"` == **0** 证明这一轮真的走进程内而非队列。
  - 需要说明的一处计数：`burst_side_effects.quota_units_spent=31` 大于 `accepted_200=29`。固定窗口的 Lua 脚本是先 `INCR` 再判定，所以被拒请求同样消耗计数——这是 P1 就存在的窗口语义，本阶段未改，只在此如实记录。
  - 审计：两轮分别记录到 `chat.rate_limited` 3 条 / 6 条（后一轮包含前一轮的累计，因为审计流没有被清理），`rate_limited_detail_keys` 恒为 `["kind","reason_code","status"]`，`audit_detail_keys_outside_whitelist=[]`；整表 `recent_detail_keys` 仍只在既有白名单 7 个字段内。
  - TTL 默认（未注入）：一次同步 + 一次流式之后，六类 key 的实时 TTL 分别是幂等缓存 86389、计划检查点 86384、运行记录 86389、运行历史索引 86392、任务记录 86398、任务事件流 86392——与默认 86400 一致（差值是写入到探测之间的秒数）。
  - TTL=120（`docker compose` overlay 注入 `DEVTRACE_REDIS_TTL_SECONDS: "120"`，`--force-recreate`）：队列路径六类 key 实测 109 / 104 / 109 / 113 / **119**（任务记录）/ 113；进程内路径复跑得到任务记录 **119**，且 `developer:task-events:<task_id>` `exists=false`（该 key 只由队列写入，符合预期）。容器内直接调用解析函数得到 `abc→86400`、`""→86400`、`"  "→86400`、`59→60`、`0→60`、`-1→60`、`7200→7200`、`86400s→86400`、`3600.5→86400`、`120→120`。
  - 两个开关互不干扰（真实 Redis）：容器 `DEVTRACE_REDIS_TTL_SECONDS=120` 生效期间，工作记忆 key `developer:wm:*` 最新三个 TTL 仍是 86310 / 86301 / 86262（记忆默认 86400 未被动），同时刻那六个瞬时 key 已衰减到 21–36。
  - 边界：单机 Docker、2 worker、1 个 Redis、单一演示用户、并发度 31 的小样本；验证的是"配额是否共享、拒绝是否有副作用、TTL 是否落到真实 Redis"，**不是容量压测**，也不能据此推断生产吞吐或 p99。
- 未验证 / 边界：**全部在 rules 模式跑，真实 provider 下的 429 行为没有测**（本阶段未产生付费调用）；前端只加了最小 429 文案，**没有在浏览器里实测流式请求返回 429 时的展示**（`frontend/dist` 未入库、未重新构建）；Redis 不可达或 Lua `eval` 抛错时的限流降级行为未测；`GET /developer/metrics` 对演示用户仍是 403，本轮依旧没有队列深度指标；`DEVTRACE_REDIS_TTL_SECONDS` 是进程级配置，同一 Redis 实例上多版本/多环境共享 key 的隔离问题没解决；被拒请求仍会推进固定窗口计数（如上）。未做且仍列为后续候选：ChromaDB 情景记忆/画像的过期与清理、按 workspace 分区的记忆、画像编辑 UI、按 IP/租户分层的配额与网关级限流、模型模式全量 40 条 holdout。全部为本机演示级验证，**不构成生产就绪结论**。

## 全量模型 holdout 与真实 provider 流式 429（2026-10-22，P8）

- 范围：用已批准的模型预算补齐两项评测空白——**真实模型跑完整 40 条 holdout**（不再只跑新批）、**真实 provider 环境下复验流式入口 429**。约束（按卡片）：不改产品代码、提示词、案例、标签、断言、fixture 或评分口径；每条案例只跑一次，只有基础设施失败才重试（上限 2 次）；连续 3 条基础设施失败即停止并上报；不删失败样本、不回头调参、不跑第二遍挑好看的数字。本轮**零产品代码改动**。
- 评分口径复用：驱动脚本（工作区根目录 `p8_holdout_probe.py`，未入库）直接调用 `developer/evaluate_holdout.py` 的 `load_cases / run_case / assess / summarize`，**判定函数逐字未改**；脚本只增加卡片要求的记录：逐请求墙钟、HTTP 状态、`decision_mode`、基础设施重试次数、每条案例的 `expected / observed / failure_reason`。报告逐条落盘（每完成 1 条重写一次），长时运行中断也不会丢证据。
- 被测栈：`devtrace-verify`（API 18002，`--scale worker=2`，镜像由 `a407ac4` 构建），`DEVELOPER_MODEL_MODE=anthropic`、`ANTHROPIC_MODEL=deepseek-chat`（DeepSeek 的 Anthropic 兼容端点）、`DEVTRACE_MCP_PUBLIC_TOOLS=enabled`、`DEVTRACE_PLANNING_CANDIDATES=enabled`、`DEVTRACE_TASK_QUEUE=enabled`、TTL 全部默认。`DEVTRACE_MCP_ALL_TOOLS` **未设置**。`backend/.env` 全程未读、未改，容器自行加载；收尾 `down` 不加 `-v`。
- **完整 40 条模型 holdout：通过 37 / 40**（[eval_holdout_model_2026-10-22_full40.json](eval_holdout_model_2026-10-22_full40.json)）：
  - 批次：原批 `2026-10-18` **19/19**（与 2026-10-18 那次模型复跑同分，且这次是同一轮整表跑出来的）、新批 `2026-10-21` **18/21**（与 P6 的 18/21 **完全一致的三条失败、完全一致的原因**）。
  - 分组：conflict 3/3、auth 4/5、quota 3/3、ambiguity 3/3、webhook 4/4、combo 5/5、injection 7/7、anomaly 3/3、negative 2/2、multiturn 3/3、public_api 0/2。`decision_mode` 43 条请求全为 `model`。
  - 时间/成本：40 条案例共 **43 次** HTTP 请求（3 条 `multiturn` 是两步对话），全部 200、**0 次重试、0 次基础设施失败、0 个 429**；整轮墙钟 **487.3 秒（8 分 07 秒）**，单请求 9088–13315ms、mean 11321.6ms；最慢是三条 `multiturn` 的第 2 步（23596.5 / 21308.7 / 20170.3ms，两次真实诊断）。provider 侧不返回 usage，**token 消耗未采集、不估算**；每条请求内部仍含多轮工具决策与作答的模型调用，本轮没有单独统计 LLM 调用次数。
  - 驱动侧不削弱限流：P7 之后模型模式同样受 30 次/60 秒用户配额约束，脚本按 60 秒滚动窗口把本地发起速率压到 20 次/分钟（实测约 5.3 次/分钟），**没有改配额、没有绕过 `enforce_chat_limit`**。
- 三条失败逐条根因（全部保留，未删样本、未改断言、未重跑）：
  - `h_public_playbook_path`、`h_public_docs_path`（public_api）：`reasons = missing_tool:lookup_error_playbook / search_public_docs`，而 `observed.tools = [mcp_lookup_error_playbook, mcp_search_public_docs]`——工具**确实执行了**，答案里的要求片段也全部命中，失败纯粹是标签按本地工具拼写断言、而公开路径在 MCP 传输下带 `mcp_` 前缀。**分类：标签绑定（传输拼写），不是能力缺口。** 需要更正一处历史表述：P6 的文档把这两条归因于"验证栈开着 `DEVTRACE_MCP_ALL_TOOLS`"，本轮 `ALL_TOOLS` 并未设置，公开路径只要 base compose 默认的 `DEVTRACE_MCP_PUBLIC_TOOLS=enabled` 就会走 MCP——即**默认发布配置就会触发**，比 P6 的措辞更需要注意。案例上的 `label_scope` 原文是准确的，本轮未改标签：要在 P9 决定是"比较 base 工具名"还是"标签写 `mcp_` 拼写"，那属于改评分口径，本卡片明令禁止。
  - `h_quota_v2_doc_version_boundary`（auth）：`reasons = evidence_missing:doc:v1 / 速率限制`，`observed` 显示 `get_request_trace` + `search_api_docs` 都执行、假设 `minute_quota_exceeded=supported` 正确、答复含 `Retry-After 30`，只是没带回 v1 版本文档那一条具体证据。规则的版本门控靠文档桩（只有以 `v2` 开头的查询才返回 v2 文档）才稳定，模型走真实检索可能取到 v2 文档。**分类：标签绑定规则模式文档桩 + 证据选择波动**，与 P6 判定相同，非版本门控缺陷。
  - 没有一条失败被归因于"模型不好"：三条都能指出是断言绑定哪一个实现细节，或哪一个证据项未命中。
- 真实 provider 流式 429（[eval_stream_limit_model_2026-10-22.json](eval_stream_limit_model_2026-10-22.json)）：为控成本**没有**再发 31 条会进模型的并发流（那一项在 P7 的 rules 模式已实测 29×200 + 2×429）。做法是把该用户的 `developer:chat-limit:<sha256(user)>` 在 Redis 里 **SET 30、EX 60** 预置满配额，然后只发 2 条真实请求：
  - 1 条 `/developer/chat/stream` → **HTTP 429 + `Retry-After: 60`**，SSE 事件计数全 0（**没有 `started`**，`event_order` 为空）、`task_id=null`、`run_id=null`、响应体只有 `{"detail":"请求过于频繁，请稍后重试"}`、消息里埋入的标记未出现在拒绝体，往返 **3.7ms**。
  - 1 条同步 `/developer/chat` → **同样 429 + `Retry-After: 60`**，**46.9ms**，`run_id/task_id` 为 null。
  - 副作用核对（真实 Redis）：`task_record / run_record / idempotency_cache / plan_checkpoint` 四类 key 数量增量全为 **0**；两个 worker 容器的 `task enqueued` 日志计数 before=0 / after=0 / **delta=0**；审计增量 **+4 = 2 条 `chat.rate_limited` + 2 条 `auth.login.success`**（两个阶段各登录一次），`rate_limited_detail_keys` 仍是 `["kind","reason_code","status"]`、`audit_detail_keys_outside_whitelist=[]`。
  - 两个如实说明：① 预置后计数从 30 变成 **32**——固定窗口"先 `INCR` 再判定"，被拒请求同样计数（P1 起既有语义，P7 已记录、本轮仍未改）。② 报告里的 `rate_limited_count=6` 是**累计值**，因为 Redis 卷在 `down`（不加 `-v`）之后保留，本轮实际只贡献 2 条，以 delta 为准。
  - "未进入模型"的证据：3.7ms / 46.9ms 对比同栈同配置下 43 次真实模型请求的 mean **11321.6ms**（min 9088.3），相差约 **3000 倍**，且拒绝发生在建连之前、无任何 task/run。
- 浏览器 429 展示（卡片可选项，已做）：`vite` 开发服务器起在 **5174**（5173 被无关容器占用），页面走自带的 `/api/python` 代理到 18002；Playwright（本机 npx 缓存里的 chromium）登录后提交一条消息，此时配额同样是预置满的（SET 30 / EX 120，**零模型调用**）。实测助手气泡渲染出 **`请求过于频繁，请稍后重试（请约 60 秒后重试）`**，网络层记录到 `POST .../developer/chat/stream → 429`、`retry-after: 60`、服务端响应体只有 detail、不含埋入标记，助手气泡也不含标记（用户自己那条气泡当然会回显其输入，因此判定只看服务端响应体与助手气泡两处）。截图 `p8-frontend-429-2026-10-22.png` 留在工作区根目录**未入库**。这一项也更正了 P7 的清单："前端 429 提示未在浏览器实测"现已实测通过。为这项检查临时加的 compose overlay 只允许 5174 这个 CORS 来源，而页面实际走同源代理，所以该 overlay 最终并非必需，产品代码与 `backend/.env` 未动。
- 单元测试与静态检查：**258 项通过 + 211 个 subtest**，与 P7 基线逐项一致（产品代码未改，无新增用例；整表无负载 88.62 秒）。CI 的 Ruff `--select F` 清单仍是 **54 个文件**，本地按 CI 同参数整表 `All checks passed!`。
- 一次需要如实记录的波动：与本轮 holdout **并发**跑测试时，`tests/test_api_security.py::test_cancelled_stream_task_records_cancelled_and_releases_lock` 连续两次失败（整表 257 通过 / 1 失败，耗时 243.67 秒），失败信息是 `CancelledError not raised` 并伴随 `流式诊断请求失败: 504`；单独跑该用例 **2.01 秒通过**，holdout 结束后整表复跑 **258 全绿（88.62 秒）**。判断：这条用例用 `asyncio.sleep(0.05)` 做时序同步，在本机被 Docker + 模型评测占满时会被拖过窗口，属**测试对时序的依赖**（flake），不是产品缺陷；本轮按范围纪律未改，列为 P9 项（改成事件驱动同步或显式等待）。
- 未验证 / 边界：真实 provider 下的 **31 并发流式**突发仍未发（成本，且 P7 已在 rules 模式覆盖同一路径）；Redis 不可达或 Lua `eval` 抛错时的限流降级仍未测（现在会 500）；`GET /developer/metrics` 对演示用户仍是 403（未为此改权限），本轮依旧没有队列深度指标；模型仍是**单一 provider、单轮、40 条小样本**，token/费用未采集；共享 Redis 上的多环境 key 隔离、固定窗口"被拒请求也计数"的语义、按 IP/租户分层配额与网关限流都没做；标签与规则模式文档桩/传输拼写的绑定本轮只报告未修。全部为本机演示级验证，**不构成生产就绪结论**。

## 评测口径修正与时间敏感测试修复（2026-10-22，P9）

- 范围：只做两件事——给 P8 的 40 条模型结果加一层**补充评分**、修掉 P8 记录的那条时间敏感 flake。产品行为、holdout 案例、标签、断言、阈值、官方评分口径全部未改；本轮**零模型调用、零 HTTP 请求**（补充报告里 `cost.model_calls=0 / http_requests=0` 是实测口径，不是估算）。

### 1. base 名补充评分（官方口径不动）

- 官方报告 [eval_holdout_model_2026-10-22_full40.json](eval_holdout_model_2026-10-22_full40.json) 逐字节保留，**37/40 仍是本项目对外汇报的分数**。
- 归一化定义：`normalize_tool_name()` 只剥 `mcp_` 前缀，且**只有剥出来的名字命中 `developer.planner.TOOL_SCHEMAS`** 才生效；本地拼写、未知/外部名（`mcp_ext_something`）、裸 `mcp_`、大小写不符、多余空格、非字符串一律原样返回——归一化不可能凭空造出一个没被调用过的工具。`assess()` 只在 `normalize_tools=True` 时启用，默认 `False`，所以**官方判定函数行为逐字未变**（`tests/test_holdout_normalization.py` 里有一条专门锁住这个默认）。工具**排除**项在归一化下只会更严（`mcp_x` 与 `x` 都算命中排除），不会放宽。
- 离线重算：`python -m developer.evaluate_holdout --mode rescore --source developer/eval_holdout_model_2026-10-22_full40.json --output developer/eval_holdout_model_2026-10-22_full40_base_names.json`，只读已存盘的 `observed.tools` 与 `expected`；可重算的前提是"用存盘的工具能逐字复现官方 reasons"，复现不出就拒绝重算并写明原因，非工具类原因**永远原样带过、不重新判定**。
- 结果：**base_names 39/40**（public_api 0/2 → **2/2**，auth 仍 4/5，其余分组逐条与官方一致）。逐条变化只有两条，都能指出改了哪一个 `missing_tool`：
  - `h_public_playbook_path`：官方 reasons `["missing_tool:lookup_error_playbook", "missing_tool:search_public_docs"]` → 归一化后 `[]`；observed `[mcp_lookup_error_playbook, mcp_search_public_docs]`。
  - `h_public_docs_path`：官方 `["missing_tool:search_public_docs"]` → `[]`；observed `[mcp_search_public_docs, mcp_lookup_error_playbook]`。
  - 这两条**只说明标签绑定被对上，不说明能力提升**：同一份 observed、同一次运行，改的比较口径而已。报告里 `binding_fixed` 字段逐条列出被消除的原因。
  - `h_quota_v2_doc_version_boundary` 归一化后**仍失败**，reasons 逐字不变（`evidence_missing:doc:v1 / 速率限制`），仍算证据/标签绑定规则模式文档桩问题，保留不掩盖。
  - 3 条多轮案例（`h_multiturn_missing_then_id#2`、`h_multiturn_webhook_identifier_later#2`、`h_multiturn_wrong_id_then_correct#2`）**拒绝重算**，原因写明"合并记录只保留最后一步的工具，离线无法逐步骤归因"；这 3 条官方本来就是 pass，拒绝重算不会给补充分数注水。
  - 补充报告的 `official` 段直接引用 P8 存盘 summary，不重算、不改写；CLI 在 base_names 未全过时同样 `exit 1`，防止这份报告被当成第二套绿灯。
- 规则模式本轮**作为回归重跑过一次**：`--mode rules` 仍 **40/40**（原批 19/19、新批 21/21）、退出码 0，输出写到工作区根目录的临时文件、**未覆盖仓库里的官方规则报告**；CI 继续跑整表规则模式。

### 2. flake 修复（只改测试，产品代码零改动）

- 用例：`tests/test_api_security.py::ApiSecurityTests::test_cancelled_stream_task_records_cancelled_and_releases_lock`。
- P8 的猜测（"CPU 争用下 `sleep(0.05)` 被拖过窗口"）方向对、机制不对。实测根因有两层，都在这条用例里：
  - **单次 `Task.cancel()` 会被 SSE 生成器自己的 `asyncio.wait_for(queue.get(), timeout=15)` 吞掉**。本机 Python 3.10 的 `wait_for` 在被外部取消时，如果内层 future 已完成就直接返回其结果，取消就此丢失；Fake assistant 一进 run 就投递 2 条时间线事件，队列非空时取消命中的正是这一路。生产路径由 Starlette/anyio 的 cancel scope 驱动，**电平触发**（后续检查点持续重抛），所以不是产品缺陷；测试用的边沿触发单次 `cancel()` 才会丢。探针 `p9_flake_probe.py`（工作区根目录，未入库）在"确认已进 run"之后单次 cancel，**20/20 次 consumer 2 秒内没结束、任务记录仍是 `running`**——旧写法的 100×20ms 轮询于是读到 `running` 而失败。
  - **`assertRaises(asyncio.CancelledError)` 断的是 consumer 任务"以什么形态结束"，而这恰好由调度点决定**：取消落到 `_must_cancel` 路径 → 任务标记 cancelled、断言通过；落到 `_fut_waiter` 路径 → 取消传进生成器、被 `finally` 里那句 `except BaseException: pass` 咽掉、生成器正常返回 → 任务正常结束 → 报 "CancelledError not raised"。P8 两次失败信息正是后者。
  - 顺带实测确认：`api/main.py:456` 的 `asyncio.wait_for(_developer.run(...), timeout=150)` 解释了 P8 那句"伴随 504"——取消被吞后，被模拟的 run 在 150 秒时被超时终结（我第一版改写保留旧断言时，单跑耗时正好 150.15 秒后 "CancelledError not raised"）。
- 改法：
  - `await asyncio.wait_for(self.assistant.entered.wait(), timeout=5)` 取代 `sleep(0.05)`：等的是"worker 确实进了 run 且仍阻塞在执行中"这个事件（`resume` 全程不置位）。
  - 用 `mock.patch.object(main, '_write_task_record', spy)` 观测**产品自己写 `cancelled` 那一次记录**并置位一个 `asyncio.Event`，取代 100×20ms 轮询 Redis 值。
  - 取消改为**重挂直到事件置位**（对齐 anyio 的电平触发语义），上限 100×50ms；判定只看事件，不看睡眠时长。
  - 去掉对 consumer 异常形态的断言（上面第二条的不可判定项），改用 `contextlib.suppress(asyncio.CancelledError)` 收割任务。卡片列出的三项被测语义**全部保留并显式断言**：断流被取消、任务记录 `cancelled`、会话锁释放。
- 压力自证（本机 `.dsh-venv`，Python 3.10）：单跑 **20/20** 通过（用例本体 0.195 秒）；再起 6 个 CPU 忙循环进程压满机器，单跑 **20/20** 通过；用 pytest 单独挑该用例跑 **10/10** 通过。修改前的证据不是"重跑旧版失败"，而是上面探针的 20/20 吞取消 + P8 记录的两次并发失败，如实区分。
- 回归：整表 **278 项通过 + 211 个 subtest**（258 + P9 新增 20 项归一化用例），92.14 秒，旧用例无一改动、无一失败；CI 的 Ruff `--select F` 清单 54 → **55 个文件**（新增 `tests/test_holdout_normalization.py`），本地按 CI 同参数整表 `All checks passed!`。
- 未验证 / 边界：本机没有 Python 3.12 可用（`py -3.12` 无安装），"`wait_for` 吞取消"在 3.11+ 是否完全消失**未验证**——新写法不依赖该行为（事件置位才判定，并持续重挂取消），CI 的 3.12 全绿只作为交叉验证而非压测；anyio 电平触发结论来自其语义与 P8 生产路径表现，本轮**没有**在真实 uvicorn 断连场景下重测；`--mode rescore` 只重算工具名这一类原因，标签仍绑定规则模式文档桩（`h_quota_v2_doc_version_boundary` 那条就是例子），没有修。全部为本机演示级验证，**不构成生产就绪结论**。
- P10 候选（本轮按范围纪律不顺手做）：把 `eval_holdout_cases.json` 里那两条 public_api 标签改成 base 名或同时接受两种拼写（那是**改评分口径**，需要单独卡片与单独基线）；`verify_public_docs.py` 仍断言本地工具拼写，公开路径走 MCP 时 `has_playbook` 恒 false；SSE 生成器里 `wait_for(queue.get(), 15)` 的心跳写法可否换成不受外部取消吞噬的形式（属产品代码，需另做压测）；P7 遗留的被拒请求仍计数、登录 429 仍无 `Retry-After`、Redis 故障时限流降级仍 500、Chroma 保留策略（**这四项中除"被拒请求仍计数"是有意保留的固定窗口语义外，其余三项已由 P10 处理**：登录 429 补 `Retry-After: 60`、限流器对基础设施异常 fail-open、新增 `developer.memory_retention` 保留 CLI，见下方 P10 一节）。

## 限流降级与情景记忆保留（2026-10-22，P10）

基线 `d05d605`（P9 已验收）。本阶段做两件运维补强：限流路径的 Redis 故障不再放大成 500（fail-open），以及给 Chroma 情景记忆一条显式的保留策略 CLI。零模型调用、零付费 HTTP；官方 40 条 holdout 的案例、标签、断言与评分口径一律未动。

### 1. 限流 fail-open + 登录 `Retry-After`

- 产品改动只在 `api/main.py`：新增 `enforce_limit(kind, key, maximum, retry_after=)` 包住原来的 `limit()`。`limit()` 自身的 INCR/EXPIRE/比较脚本**一字未改**，登录入口从 `limit(..., 10)` 改为 `enforce_limit('login', _login_limit_key(ip), 10, retry_after=60)`，`enforce_chat_limit` 改为经 `enforce_limit('chat', ...)`。
- 只吞基础设施异常：捕获元组 `LIMITER_INFRA_ERRORS = (RedisError, OSError, asyncio.TimeoutError)`，`HTTPException` 显式先 `raise` 出去——429 是判定结果，不是故障。用例 `test_unexpected_limiter_error_is_not_swallowed` 用 `ValueError` 证明非基础设施异常**照样抛出**，不会被 fail-open 静默放过；`test_quota_breach_is_never_swallowed_by_fail_open` 把审计写入也弄坏之后，超限仍然是 429 + `Retry-After: 60`。
- 降级可观测：一行 warning（`限流不可用，已 fail-open 放行: kind=chat reason=ConnectionError`）+ 一条 `<kind>.rate_limit_unavailable` 审计事件，`detail` 仍走 `developer/audit.py` 的白名单（只出现 `kind/status/reason_code`），不含消息正文、用户名或 IP；`actor` 故意留空（登录侧的用户名未经认证、chat 侧没必要重复标识）。审计写失败依旧 best-effort，不影响放行。
- 登录 429 现在带 `Retry-After: 60`，与同步/流式 chat 对齐（`LOGIN_LIMIT_RETRY_AFTER`）。固定窗口"被拒请求同样 `INCR`"的既有语义**没改**：容器实测 31 次尝试后计数为 31，本轮只在文档里把它写清。
- 30 次/60 秒、10 次/60 秒的阈值与键名全部未变；`enforce_limit` 不暴露为路由参数，P7 那条"查询参数绕过"的防护用例继续通过。

### 2. `python -m developer.memory_retention`（新增，默认 dry-run）

- 参数：`--days`（必填、整数、≥1，否则 argparse 退出码 2）、`--dry-run`（默认）与 `--apply` 互斥、`--user`（只能缩小范围）、`--collection`（默认 `developer_episodic`）、`--json`。
- 只读 episodic 集合并按 metadata `ts` 判定：ISO 字符串与 epoch 数值都解析，统一转成带时区的时刻比较；**`ts` 缺失或不可解析的记录进 `skipped_no_ts`，绝不删除**（不猜年龄）。恰好等于 cutoff 的记录算"较新"保留。
- 画像保护是双保险：`guard_collection()` 直接拒绝任何名字含 `profile` 的集合，且实现里根本不会打开画像集合；报告固定带 `profile_collection_touched: false`。Redis 工作记忆/摘要继续归 `DEVTRACE_MEMORY_TTL_SECONDS`，本工具不碰。
- 失败可读：连接不可用 / `get` 失败 / `delete` 批次失败都打印原因并以退出码 1 结束；`delete_ids` 分批（默认 200）删除，部分失败时报出"已删除 x/y"，不会把失败说成成功。

### 3. 验证

- 回归：整表 **304 项通过 + 211 个 subtest**（278 + P10 新增 26 项：`tests/test_rate_limit_degradation.py` 10 项、`tests/test_memory_retention.py` 16 项），51.90 秒，旧用例无一改动、无一失败；计数为 `PYTHONUTF8=1` 口径（见"未验证"第 7 条的 Windows 编码依赖）；CI Ruff `--select F` 清单 55 → **58 个文件**（`developer/memory_retention.py` 与两个新测试文件），本地按 CI 同参数整表 `All checks passed!`。
- 规则模式 holdout 作为回归重跑：**40/40**（conflict 3/3、auth 5/5、quota 3/3、ambiguity 3/3、webhook 4/4、combo 5/5、injection 7/7、anomaly 3/3、negative 2/2、multiturn 3/3、public_api 2/2）、退出码 0，输出写到工作区根目录 `.p10_rules.json`，**仓库内官方报告未被覆盖**；零模型调用。
- Docker（隔离栈 `devtrace-verify`，API 18002，`--no-build` 复用镜像 + `devtrace-compose.bind.yml` 把仓库源码只读挂载进容器 + `devtrace-compose.rules.yml` 强制 rules 模式，容器与 worker 的 `DEVELOPER_MODEL_MODE` 都 `printenv` 确认为 `rules`；`backend/.env` 全程未读未改；收尾 `down` 未加 `-v`）：
  - **登录 429 带 `Retry-After`**：12 次连续登录请求 → `10×401 + 2×429`，两个 429 的 `Retry-After` 取值集合恰为 `["60"]`。这轮用的是故意写错的口令（工作区口令文件与该栈现有用户口令不匹配，见"未验证"第 1 条），但登录限流在口令校验之前按 IP 计数，401 同样消耗配额，所以"窗口 10 次 + 第 11 次起 429 带 header"这条结论成立；200 成功的登录路径由 ASGI 用例覆盖。
  - **真实 Redis 上的判定边界**（容器内探针把 `eval` 换成抛 `redis.ConnectionError` 的替身，其余操作委托给真客户端）：`enforce_limit('chat'|'login', ...)` 两种 kind 都无异常逃逸；把 `eval` 换回真实实现后连打 31 次 → **30 次通过 + 1 次 429**，`Retry-After` 头 `{"Retry-After": "60"}`，`counter_after_31_requests = 31`（被拒那次照样计数，固定窗口语义未变），探针用后即删临时 key。
  - **审计流实证**：`developer:audit` 里检索到 4 条本轮探针写入的 `rate_limit_unavailable` 事件（chat/login 各 2），`detail` 只有 `{"kind","reason_code":"limiter_unavailable","status"}`，`actor` 为空、`remote` 为 `unknown`，不含任何正文。
  - **HTTP 层降级范围**：容器内用 `issue_token` 现签一个演示用户令牌（令牌与 secret 都不打印）直接打本地 127.0.0.1:8000。Redis 正常：`/developer/chat` **200**；把 `devtrace-verify-redis-1` 停掉：`/auth/login` → **401 而不是 500**（限流器 fail-open，请求走到口令校验），`/developer/chat` → **500**（限流器已放行，但会话锁仍需 Redis）；恢复 Redis 后 chat 再次 **200**。这条把"fail-open 只覆盖限流器、不覆盖整个请求依赖链"的边界实测出来了，如实记录。
  - **保留 CLI 跑在真实 Chroma 服务上**：先建一次性集合 `p10_retention_demo` 灌 3 条（400 天前的 alice 旧记录、1 小时前的新记录、一条无 `ts`）。`--days 30` dry-run → 扫描 3、候选 1、较新保留 1、`skipped_no_ts` 1、`deleted 0`，集合仍 3 条；`--apply` → `deleted: 1`、`candidate_ids: ["old-alice"]`、`by_user {"alice":1}`、`conversations ["c-old"]`，集合剩 `fresh-alice` + `undated-carol`；再次 `--apply` → `deleted: 0`（幂等）；`--apply --collection developer_profile` → 退出码 **1** 并打印拒绝原因；`--days 0` → 退出码 **2**。全程 `developer_episodic` 仍 3 条、`developer_profile` 仍 3 条未被触碰，演示集合验证完即 drop。
- 工作区根目录留下（不入库）：`p10_start.py`、`p9_hb.py`（心跳复用）、`p10_login_probe.py`、`p10_redis_probe.py`、`p10_chroma_probe.py`、`p10_chat_probe.py`、`p10_retention_smoke.py`、驱动 `p10_verify.sh`/`p10_verify2.sh`/`p10_verify3.sh` 与报告 `p10_docker_report.json`/`p10_docker_report2.json`/`p10_docker_report3.json`、`.p10_rules.json`。

### 4. 未验证 / 风险

1. **登录成功（200）路径未在容器里复测**：工作区 `.devtrace-demo-credential` 与该栈的用户口令存储已不匹配（401），本轮不打算为取证改 `backend/.env`；200 登录 + fail-open 的组合由 ASGI 用例 `test_login_succeeds_when_limiter_redis_is_down` 覆盖，容器只证明了"Redis 挂掉时登录不会 500、且限流器照常降级"。
2. **fail-open 不等于全链路可用**：Redis 完全不可用时 `/developer/chat` 仍 500（会话锁、任务记录都依赖 Redis）。本轮按卡片范围只降级限流器，没有改锁/队列语义。
3. **降级窗口内的滥用风险**：fail-open 期间配额不生效，突发流量可直达模型。演示级取舍，生产需网关/多副本限流；本轮未做限流器与 Redis 断连的自动恢复压测（只做了 stop/start 一轮）。
4. **保留 CLI 的规模上限**：`fetch_all` 一次性把整个 episodic 集合读进内存（`get(include=['metadatas'])`），演示级数据量没问题，真实大集合需要分页/流式扫描——列为 P11（**P11 已实现 `--page-size` 分页扫描，`fetch_all` 退化为"客户端不支持 limit/offset 时的兜底一次全量读取"**）。Chroma 侧只测了服务地址不可达 → 嵌入式回退与真实服务两条路径，未测并发删除、未测 `ts` 为其他格式的历史脏数据（本轮只按 ISO/epoch 解析，其余一律 skip）。
5. 本机仍无 Python 3.12，`asyncio.TimeoutError` 在 3.10 与 3.11+ 的类层次不同（3.11 起才是内置 `TimeoutError`），`LIMITER_INFRA_ERRORS` 在 CI 的 3.12 下是否命中同一路径以 CI 全绿交叉验证，未做专项压测。
6. 审计事件里的 `remote` 是 `unknown`（探针进程无请求上下文），不代表线上取不到 IP；线上路径由 HTTP 中间件设置 contextvar，P1–P5 已验。
7. **本机整表计数依赖 UTF-8 模式**：同一份代码在本机不带 `PYTHONUTF8=1` 复跑时，`tests/test_mcp_all_tools.py::ServerSurfaceTests::test_stdio_entrypoint_starts_and_answers_tools_list` **连续两次**失败（`UnicodeDecodeError: 0xbc`）——被测的 stdio MCP 子进程在非 UTF-8 模式下用系统 GBK 写出中文工具描述，而测试按 UTF-8 解码 stdout；带 `PYTHONUTF8=1` 则 **304 项全绿**（52.51 秒）。P10 未触碰 `backend/mcp/` 与该测试文件（`git diff d05d605..HEAD --stat -- backend/mcp backend/tests/test_mcp_all_tools.py` 为空），属既有的 Windows 编码耦合，Linux CI 3.12 不受影响、本轮 CI 全绿。**本轮按范围纪律未修**，列 P11（子进程显式 `PYTHONUTF8=1` 或断言改用 `errors='replace'` 前先确认产品输出编码）。本文档报的 304 因此以 `PYTHONUTF8=1` 口径为准。**（P11 已修：`mcp/server.py` 的 stdio 入口显式 `reconfigure(encoding='utf-8')`、`mcp/client.py` 的 `StdioTransport` 强制子进程 `PYTHONUTF8=1`；本机不带 `PYTHONUTF8=1` 整表 325 项全绿。本节原文按记录纪律保留。）**
8. 全部为小样本、单 provider、单机演示级验证，**不构成生产就绪结论**。
9. P11 候选：限流器与 Redis 的健康检查/熔断与恢复压测、按 IP/租户分层配额或网关级限流、`memory_retention` 的分页扫描 + 定时任务（需运维决策）、按 workspace 分区 Chroma 集合、`verify_public_docs.py` 的工具拼写断言、MCP stdio 测试的跨平台编码依赖、真实 `kill -9` 恢复与多 provider 评测。

## MCP stdio 编码与保留 CLI 分页（2026-10-22，P11）

基线 `255bdcf`（P10 已验收）。只做卡片里的两件低风险工程修复：MCP stdio 显式 UTF-8（产品侧 + 客户端侧），以及情景记忆保留 CLI 的分页扫描。**零模型调用、零付费 HTTP**；官方 40 条 holdout 的案例、标签、断言、评分逻辑与两份已发布报告全部零改动（`git diff 255bdcf -- backend/developer/eval_holdout_model_2026-10-22_full40.json backend/developer/eval_holdout_model_2026-10-22_full40_base_names.json backend/tests/test_holdout_cases.py backend/developer/evaluate_holdout.py backend/developer/evaluate_diagnosis.py` 为空）。

### 1. MCP stdio 强制 UTF-8（`mcp/server.py` + `mcp/client.py`）

- **产品侧**：`_stdio_loop()` 第一件事就是 `force_utf8_stdio(sys.stdin, sys.stdout)`——对每个流调 `reconfigure(encoding='utf-8')`，**不能 reconfigure 的流（没有该属性的替身、已关闭的流）跳过而不是报错**，并把成功切换的流名记入返回值供测试断言。写入仍是原来的文本模式 `json.dumps(response, ensure_ascii=False) + "\n"`，协议字段与顺序、`tools/list` 的 8 个只读工具、`initialize` 的 `protocolVersion: 2024-11-05` 一字未改；线格式仍是"每行一个 JSON 对象"的 NDJSON。
- **客户端侧**：`StdioTransport._child_env()` 返回 `{**os.environ, 'PYTHONUTF8': '1'}` 并传给 `create_subprocess_exec(env=...)`。**这是有意覆盖**继承来的 `PYTHONUTF8=0`：stdio 通道的两端必须说同一种编码，否则中文工具描述会解码失败；`PATH` 等其余环境变量原样透传，30 秒超时、`stdout.decode('utf-8')` 与 `stderr=DEVNULL` 均未改。
- **为什么必须两边都改**：Windows 上被重定向的 `sys.stdout` 默认用系统区域编码（本机 GBK/cp936），而 `python -X utf8` 只作用于被启动的那个解释器、**不会被子进程继承**，所以 P10 那条 `UnicodeDecodeError: 0xbc` 的既有失败在 Linux CI 上看不见、在 Windows 上稳定复现。现在服务端自己声明 UTF-8，客户端再保证子进程以 UTF-8 模式启动，两端都不依赖调用方的环境变量。
- 文档口径统一写成"**UTF-8 换行分隔 JSON**"（`mcp/server.py` 模块 docstring、`StdioTransport` docstring）；P10 评测报告"未验证"第 4/7 条原文保留并就地标注"已在 P11 修复"。

### 2. 保留 CLI 分页扫描（`developer/memory_retention.py`）

- 新增 `--page-size`（`type=parse_page_size`，默认 **500**，范围 **1..5000**，越界或非整数 → argparse 退出码 **2**，且在任何 Chroma 读取之前失败）。`run_retention()` 内部也复检同一区间（直接调用 API 时给 `RetentionError`）。
- 读取拆成 `fetch_page(collection, limit=, offset=)` + `iter_pages()` 生成器：`collection.get(limit=..., offset=..., include=['metadatas'])` 每次只取一页，逐页 `classify()` 后**只累计计数与候选 id**，整集合不再一次性进内存（P10 的 `fetch_all` 保留，退化为"客户端不接受 `limit`/`offset` 时的兜底一次全量读取"）。
- 终止条件三条：最后一页短于 `page_size` → 停；空页 → 停；**同一批 id 连续出现两次**（客户端接受参数但忽略 `offset`）→ 停，不会死循环。候选 id 用 `seen_candidates` 去重，因此任何分页异常都不会让同一条记录被删两次；`skipped_no_ts`/`conversations` 同样去重后累计，`by_user` 只对首次出现的候选 id 计数。
- **删除仍在整页扫完之后**才发生（`delete_ids` 沿用 `DELETE_BATCH_SIZE = 200`），所以读取期间偏移量不会因删除而漂移；中途失败照旧抛"已删除 x/y"并以退出码 1 结束。
- 报告新增 `pages` / `page_size` 两个字段，文本输出多一行"分 N 页读取，每页 M 条"；`--days/--user/--collection/--dry-run/--apply` 的语义、画像集合拒绝、无 `ts` 跳过、幂等性全部未改。

### 3. 验证

- 回归两个口径**都是 325 项通过 + 211 个 subtest**：不带 `PYTHONUTF8=1`（Windows 本机 Python 3.10.18）**325 passed / 51.28s**，带 `PYTHONUTF8=1` **325 passed / 56.45s**——P10 报的"计数依赖 UTF-8 模式"这条限制本轮消除。304 → 325 是新增 21 项：`tests/test_mcp_stdio_encoding.py` **8 项**、`tests/test_memory_retention.py` 16 → **29 项**；旧用例无一改动、无一失败。
- **编码修复有负向对照**：`LocaleCodecControlTests` 在同一次测试里起一个**故意不 reconfigure** 的子进程，Windows 上断言它的 stdout 能按 GBK 解码、按 UTF-8 解码必抛 `UnicodeDecodeError`——证明"测试通过"不是因为宿主默认编码本来就是 UTF-8。另外手工把 `force_utf8_stdio()` 调用摘掉复跑，P10 那条 `UnicodeDecodeError: 0xbc` 立刻回来（2 项失败），恢复后 34 项全绿。
- 真实 Chroma 分页取证（隔离栈 `devtrace-verify`，Chroma 服务 0.5.23，`devtrace-compose.bind.yml` 把仓库源码只读挂载进容器，容器内 Python 3.12.15；`backend/.env` 全程未读未改；收尾 `down` 未加 `-v`）：一次性集合 `p11_probe_episodic` 灌 **1200 条**（600 条 45 天前、500 条 1 天/1 分钟内、100 条故意不带 `ts`）。
  - `--page-size 500` dry-run：`pages=3`、`scanned=1200`、`candidates=600`、`kept_fresh=500`、`skipped_no_ts=100`、`deleted=0`、`by_user {"alice":200,"bob":400}`、涉及会话 17 个；
  - **分页不改变判定**：同一集合 `--page-size 100`（`pages=12`）与 `--page-size 5000`（`pages=1`）的 `scanned/candidates/kept_fresh/skipped_no_ts/by_user` 与 `--page-size 500` **完全一致**；
  - `--page-size 500 --apply`：`pages=3`、`deleted=600`、`remaining_candidates=0`（按 200 一批共 3 次 `delete`）；随后再 dry-run：`pages=2`、`scanned=600`、`candidates=0`、`deleted=0`（幂等）；
  - `--page-size 100 --apply --collection p11_probe_profile`：退出码 **1** + 拒绝原因，集合列表里从未出现该名字；
  - 全程真实集合未动：`developer_episodic` 3 条、`developer_profile` 3 条、`developer_knowledge` 6 条；取证完 `delete_collection('p11_probe_episodic')` 已清理。
- 单元层的分页边界（`RetentionPagingTests` 10 项 + `CliTests` 4 项新增）：3 页 dry-run 统计与 `get(limit, offset)` 调用序列 `[(3,0),(3,3),(3,6)]`、跨页 apply 只删过期行、`batch_size=2` 时的删除批次 `[["r-0","r-2"],["r-5"]]`、空集合与短尾页不死循环、**不接受 `limit`/`offset` 的客户端**退化为一次全量读取（`pages=1`、结果不变）、**忽略 `offset` 的客户端**只读两次就停且不重复删、二次 apply 幂等、`--user` 按页过滤、`page_size` 越界（0/-1/5001）在读之前失败且 CLI 退出码 2、分页请求画像集合仍被拒绝且 `get_calls=0`、文本输出带页信息。
- 规则模式 holdout 作为回归重跑：**40/40**（conflict 3/3、auth 5/5、quota 3/3、ambiguity 3/3、webhook 4/4、combo 5/5、injection 7/7、anomaly 3/3、negative 2/2、multiturn 3/3、public_api 2/2；`by_decision_mode` 只有 `rules`、`by_batch` 19+21），退出码 0，输出写到工作区根目录 `.p11_rules.json`，**仓库内官方报告未被覆盖**；零模型调用。
- CI Ruff `--select F` 清单 58 → **59 个文件**（新增 `tests/test_mcp_stdio_encoding.py`；`mcp/server.py`、`mcp/client.py`、`developer/memory_retention.py`、`tests/test_memory_retention.py` 本就在清单里），本地按 CI 同参数 `All checks passed!`。
- 范围证据：`git diff --name-only 255bdcf` 只有 8 个已跟踪文件（`.github/workflows/ci.yml`、`README.md`、`backend/developer/README.md`、`backend/developer/evaluation_report.md`、`backend/developer/memory_retention.py`、`backend/mcp/client.py`、`backend/mcp/server.py`、`backend/tests/test_memory_retention.py`）+ 新增 `backend/tests/test_mcp_stdio_encoding.py`；`backend/api`、`backend/memory`、`backend/developer/prompts.py`、`assistant.py`、`planner.py`、`task_queue.py`、`frontend` 的 diff 为空。

### 4. 未验证 / 风险

1. **真实 Chroma 上的分页顺序假设**：本轮实测 0.5.23 的 `get(limit, offset)` 按 id 顺序返回、三次分页无重叠无遗漏（1200 → 3×500，删除后 600 → 2 页）。Chroma 未把"分页期间顺序稳定"写成契约，并发写入时理论上可能漏读或重读某条记录；漏读的后果是"这轮少删一条"（下一轮 dry-run 会再报出来），重读的后果被候选 id 去重挡住，都不会误删较新记录，但**未做并发写入下的分页压测**。
2. **真实 Chroma 上未验删除中途失败**：`已删除 x/y` 的非零退出路径只有单元测试（`fail_delete` 替身 + `batch_size` 分批断言）覆盖，没在真实 Chroma 上人为制造批次失败；理由是造这种故障需要注入或删除中途 kill 容器，超出本轮范围。
3. **stdio 编码只在本机（Windows/GBK）与 Linux CI 两个环境验证**：macOS 与非 GBK 的 Windows 区域设置未测；`force_utf8_stdio` 对"无法 reconfigure 的流"只跳过不报错，若某平台的 `sys.stdout` 既没有 `reconfigure` 又默认非 UTF-8，中文描述仍会解码失败——这一分支由 `StdioLoopStreamSwapTests` 用替身流覆盖，属实现级验证而非平台级。
4. **`PYTHONUTF8=1` 的有意覆盖**：调用方若刻意设 `PYTHONUTF8=0`（例如想强制 ASCII 输出以便日志落盘），`StdioTransport` 会覆盖它。这是"两端必须同编码"的取舍，已在 `client.py` docstring 写明，但没有提供关闭开关。
5. `--page-size` 上限 5000 是拍脑袋的演示级上限（单页 5000 条 metadata 的量级），没有按真实 Chroma 的响应体大小做压测；下限 1 只用于拒绝 0/负数，没有测过 1 的极端分页在真实服务上的耗时。
6. 本机仍无 Python 3.12，`reconfigure()` 在 3.10 与 3.12 的行为差异只以"本机 3.10 全绿 + CI 3.12 全绿"交叉验证，未做专项对照。
7. 全部为小样本、单 provider、单机演示级验证，**不构成生产就绪结论**。
8. P12 候选：限流器与 Redis 的健康检查/熔断与恢复压测、按 IP/租户分层配额或网关级限流、保留 CLI 的定时任务与"删除窗口内并发写入"的一致性设计、按 workspace 分区 Chroma 集合、`verify_public_docs.py` 的工具拼写断言、MCP stdio 的 Windows 区域设置矩阵与真实 `kill -9` 恢复、多 provider 评测。

## SSE 取消语义、真实 kill -9 恢复与公开文档脚本（2026-10-22，P12）

基线 `6beb3ca`（P11 已验收）。本轮做三件收尾：修进程内 SSE 心跳吞掉外部取消（**产品代码**）、真实 `docker kill` 崩溃恢复取证（**Docker，不注入假租约**）、修工作区验证脚本 `verify_public_docs.py` 的工具拼写断言（**不入库**）。官方 40 条 holdout 的案例、标签、断言、评分口径与所有已发布报告零改动，官方评测未重跑打分。

### 1. SSE 心跳的取消语义（`api/main.py`）

- **根因**：进程内 SSE 生成器用 `asyncio.wait_for(queue.get(), 15)` 兼作空闲心跳。Python 3.10 的 `wait_for` 内部是 `except CancelledError: if fut.done(): return fut.result()`——**当 `queue.get()` 已经完成（事件刚入队还没被取走）时，落在这个窗口里的一次外部 `Task.cancel()` 被静默丢弃**。后果：客户端已断开而生成器继续跑，任务记录停在 `running`，会话锁要等 180 秒 TTL 才自然消失。3.11+ 的 `wait_for` 改用 `asyncio.timeout` 重写、取消是电平触发的，所以这条只在 3.10 稳定复现（本机 3.10.18），CI 3.12 看不见。
- **修法**：新增 `_next_stream_event(get_task, timeout)`，用 `asyncio.wait({get_task}, timeout=timeout)` 取代 `wait_for`。`asyncio.wait` 不吞取消，而且**超时不会取消内部 task**，所以两次心跳之间已入队的事件不会丢；空闲时返回 `None`，生成器照旧发 `: heartbeat\n\n` 并 `_touch_task` 刷新任务心跳。
- 生成器改为"**一个事件一个 getter**"：`get_task` 在循环外创建，**取到事件之后**才重新 arm，空闲 tick 保留那个挂起的 getter（这正是旧写法丢取消的对偶问题：在 wait 里现开 `queue.get()`，超时就会取消一个可能已经取到事件的 get）。
- `finally` 里的断开清理**逻辑未变**：取消 worker task 与 getter、`gather(..., return_exceptions=True)`、未终结则写 `cancelled` 记录 + `task.finished` 审计，仍用 `asyncio.shield` 包住以免清理自身被同一个取消带走。
- 心跳周期提为模块常量 `SSE_HEARTBEAT_SECONDS = 15.0`，测试可缩短它，不必真等 15 秒。
- **未改**：SSE 事件格式与 `started/timeline/complete/end` 顺序、150 秒上限、断开清理语义、队列模式 relay（`_queued_stream` 的心跳本来就是 sleep 轮询，不受此 bug 影响）、`_chat_core` 的 `asyncio.wait_for(..., 150)` + 504、以及 P9 修好的那条 `test_api_security.py` 用例（一字未动，本轮只补不改）。

**证据（新增 `tests/test_stream_cancellation.py`，12 项 + 6 个 subtest）**

- **确定性单元对照**（不依赖调度）：`test_single_cancel_propagates_when_the_event_is_already_queued` 断言 `_next_stream_event` 在"事件已入队"时收到单次 `cancel()` 会抛 `CancelledError`；`test_stdlib_wait_for_shape_swallows_the_same_cancel` 在同一位置用旧的 `wait_for` 写法断言**不抛**（`@skipIf(sys.version_info >= (3, 11))`，本机 3.10 实跑通过，即负向对照成立；CI 3.12 上该项按设计跳过）。
- 空闲语义：`test_idle_timeout_leaves_the_getter_armed_and_loses_nothing`（超时后 getter 仍挂着，随后发布的事件照样拿到）、`test_cancelling_the_getter_after_an_idle_tick_keeps_the_queue_intact`、`test_each_wait_returns_the_next_event_not_a_repeat`（三条计划逐条返回，不是重复第一条——这条是为了钉住"重新 arm"这个不变量，漏掉它就是死循环）。
- 集成层：单次 `cancel()` 后流 promptly 结束、记录 `cancelled`、会话锁释放；**事件驱动**（spy `_write_task_record` 置 `asyncio.Event`），无 sleep 轮询；4 条 timeline 已入队时单次 cancel 仍传播；连续 6 次"单次 cancel"子测试全过；心跳用 `SSE_HEARTBEAT_SECONDS=0.05` 验证仍发 `: heartbeat` 且刷新任务心跳；正常 `complete` 路径事件顺序不变、`asyncio.all_tasks()` 前后差集为空（无泄漏 task）；`aclose()` 模拟客户端断开仍记 `cancelled`。
- **修复前后的对照是概率性的，如实记录**：修复前把这条集成用例连跑 5 次 **0/5 通过**，修复后 **5/5 通过**；但基线整表 `-v` 单跑时其中 2 项曾偶然通过、另 2 项失败——"吞取消"是否显形取决于取消落在哪个调度点。因此结论以**确定性单元对照 + 5× 循环**为准，不用单次绿灯或单次红灯下结论。

### 2. 真实 `kill -9` 崩溃恢复（Docker，隔离栈 `devtrace-verify`）

- **栈与配置**：API 映射 18002，`--scale worker=2`，`DEVTRACE_TASK_QUEUE=enabled`，`DEVTRACE_TASK_LEASE_SECONDS=30`（代码下限 `MIN_LEASE_SECONDS`，用未入库的 `devtrace-compose.p12kill.yml` overlay 注入），worker overlay 里 `restart: "no"`（被 `docker kill` 的 worker 必须保持死掉，才能证明是**幸存**那个回收了租约），两个 worker 都通过 bind overlay 只读挂载工作树源码，跑的就是被测代码。`backend/.env` 全程未读未改，收尾 `down` **未加 `-v`**（三个卷保留）。
- **模型模式的取舍与成本超支（如实披露）**：卡片要求优先 rules 模式。实测 rules 模式下同一任务的 `running` 窗口只有 **0.151 秒**（task `d4e85e6c`、会话 `p12-window-1`），宿主侧 `docker kill` 无法落进去，测不到"打断执行中任务"；因此本轮 kill 取证切到 `DEVELOPER_MODEL_MODE=anthropic`，一次真实调用把 `running` 撑到 ~11 秒。**卡片允许最多 2 次模型调用，实际按当时容器日志统计为 7 次 provider 调用**：被 SIGKILL 打断的那次运行 1 次（kill 落在它 3 次调用的第 1 次前后，未完成整轮）+ 幸存 worker 重跑 3 次 + 复验"会话锁已释放"的补发请求 3 次（这个 build 里一次 assistant 运行 = 3 次 provider 调用，卡片给的 2 次预算是按"1 次运行 = 1 次调用"估的，估算错了）。容器 `down` 之后日志随之删除，**这 7 次无法事后复核**，按当时读数披露。P12 其余所有验证（第 3 项与整表回归）都在 rules 模式，**零额外调用**。
- **时间线**（UTC，全部取自容器内探针写出的时间戳；会话 `p12-kill-120736`，task `f26a644e-58db-4e64-9252-0ad7b5278734`，消息 `API 返回 401，怎么排查？`）：

  | 时刻 | 事件 |
  | --- | --- |
  | 04:07:40.348 | 客户端 POST `/developer/chat/stream`，任务入队 |
  | 04:07:40.766 | worker-2（`d10e9f831c1a-1-2876e1`，容器 `devtrace-verify-worker-2`）置 `running`，`attempts=0`，`run_id=null`，租约到 04:08:10.741 |
  | 04:07:41.984 | `docker kill devtrace-verify-worker-2`：`State.ExitCode=137`、`OOMKilled=false`（= SIGKILL），**落在任务进入 running 后 1.22 秒** |
  | 04:08:10.741 | 租约到期 |
  | 04:08:13.860 | 幸存 worker-1（`6e5a960e2f5b-1-79bc71`）日志 `reclaimed 1 task(s) with expired leases`（kill 后 **32.9 秒**；分母取探针记录的 `killed_at=1792728461` 秒级精度，若改用 Docker 容器退出时间戳 04:07:41.984 则是 **31.88 秒**，租约到期后 **3.12 秒**） |
  | 04:08:13.907 | 记录 → `queued`，`attempts` **0 → 1**，进延迟重试队列 |
  | 04:08:18.910 | 提升后被 worker-1 认领，重新 `running`，新租约 |
  | 04:08:30.323 | 终态 `waiting`，`run_id=5c258006-4297-4b73-817f-34abc59e6a69`，重跑耗时 **11.41 秒** |

- **"任务确实在执行中"的证据**：kill 时记录是 `running`、`attempts=0`、`run_id=null`（还没有任何运行记录）、租约有效；同一条消息由幸存 worker 重跑用了 11.41 秒，而被杀那次只跑过 1.22 秒。不是"刚入队还没开始"或"已经跑完"。
- **客户端全程连着**（队列 relay 路径），SSE 相对发送时刻：`event: started` +0.39s → `: heartbeat` +15.45s、+30.51s（**跨过 kill 与回收窗口**，连接没断也没静默）→ timeline 事件 +39.74s…+40.74s（plan/approval/selection/action/observation/status）→ `: heartbeat` +45.56s → `event: complete` + `<eof>` +49.97s。客户端拿到的是恢复后的结果，没有挂死。
- **卡片要求的断言逐条核对**（`04_verify.json` / `09_verify_after_resend.json`）：终态唯一 = `waiting`；`task.finished` 审计**恰好 1 条**（`task_finished_count=1`，statuses `["waiting"]`），该任务的审计事件只有 `task.enqueued` + `task.finished`；`run_id` 唯一且运行记录存在、归属用户匹配；`attempts` 0 → 1（合理增长，没有无谓重跑）；`error=null`、`cancel_requested=false`（**没有**因为客户端还连着而被误判成取消）；不在死信队列、不在延迟队列；high/normal/low 三条队列长度全 0；会话锁已释放（`session_lock_exists=false`、`ttl=-2`）。
- **终态是 `waiting` 而不是 `completed`，说明清楚**：`api/worker.py` 的映射是 `status = 'waiting' if (result.get('plan') or {}).get('status') == 'waiting' else 'completed'`。这一轮计划提议 `search_api_docs → get_request_trace → inspect_key_scope`，只执行完 s1 就把下一步交回用户，计划状态是 `waiting`。`waiting` 是队列的**第三个终态**：worker 同样写 `finished_at`、发 `task.finished`、释放会话锁，relay 同样发 `event: complete`。所以"唯一终态、不挂死、锁已释放"这些断言成立，但**不能说它跑成了 `completed`**——卡片期望的是 `completed` 或明确的 `failed`，实际拿到的是第三种合法终态。
- **锁释放后同会话可继续**：在 `p12-kill-120736` 上补发一条流式请求 → 新任务 `ce311c56-a6b0-41d8-88d2-20163cde0638`，`status=waiting`、`attempts=0`、`run_id=aa846e5f-8211-48a6-bb4c-c26955aed890`，成功执行。
- **会话锁是否挡住 reclaim？——不挡，所以卡片里那条"条件性提交"没有做**：Docker 轮已经证明锁还握在死掉的 worker 名下时，幸存 worker 照样回收了租约并重跑成功（`release_session` 是在任务终态时由 worker 调的，不是 reclaim 的前置条件）。按范围纪律，本轮**不顺手改** `release_session`，改为用只读探针（`p12_reclaim_probe.py`，复用队列单测的 fake Redis，不入库、不改产品代码）把两个真实缺口取证给 P13：
  1. **维护循环耗尽重试次数时不释放会话锁、也不写 `task.finished`**：第一次崩溃 → `reclaim_expired` 重排队（`attempts=1`，锁仍属 task-A，这是对的，任务还要重跑）；第二次崩溃 → `reclaim_expired` 走 `requeue_or_fail` 撞到 `MAX_ATTEMPTS=2`，记录变 `failed` + 进死信，但**锁仍 held by task-A**，紧接着 `try_claim_session('task-B')` 返回 **false** —— 这个会话在锁的 900 秒 TTL 内彻底不可用，而且这条终结路径**没有任何 `task.finished` 审计**（`developer/task_queue.py` 里零审计调用，`task.finished` 只在 `api/main.py` 与 `api/worker.py` 写）。
  2. **`release_session` 是无条件 `DEL`，会删掉别人的锁**：task-A 的延迟清理在 task-B 已持有同一会话锁之后执行 → 锁被删（`held: true/owner task-B` → `held: false`），随后 task-C 的 `try_claim_session` 返回 **true** —— 同一会话出现两个并发执行的任务。锁值里本来就存着 `task_id`，所以正确修法是 Lua 比较后删（`GET == ARGV[1] then DEL`），**绝不能**盲删其他任务/用户的锁；本轮只取证，不改。

### 3. 公开文档验证脚本 `verify_public_docs.py`（工作区文件，**不入库**）

- **修复前的断言差异**：脚本把 `has_playbook` 写成 `"lookup_error_playbook" in tools`（本地拼写）。base compose 默认 `DEVTRACE_MCP_PUBLIC_TOOLS=enabled`，公开文档路径改由 MCP server 托管，`actions` 里的工具名带传输前缀，实际是 `mcp_lookup_error_playbook` / `mcp_search_public_docs`，于是 `has_playbook` 恒 **false** → 脚本 exit **1**，而其余五项判定（`has_retry_guidance`/`has_recovery`/`has_source`/`has_public_disclaimer`/`plan_status=complete`）全为 true。这与 P9 给 holdout 那两条 public_api 标签做的是同一层归因：**标识符与传输拼写绑定，不是能力缺口**。
- **修复**：比较改为按 base 名归一化（剥掉 `mcp_` 前缀后与 `lookup_error_playbook` 相等），其余判定、消息、门禁条件、退出码语义**一字未改**。顺带把输出路径从仓库内的官方报告 `backend/developer/eval_error_playbooks_http_2026-10-18.json` 改到临时路径（`tempfile.gettempdir()/verify_public_docs.json`）——脚本原来会**覆盖已发布的官方报告**，这轮跑完 `git status backend/developer/` 为空，官方报告未被触碰。脚本本身按约定不提交。
- **实测（`devtrace-verify` 栈，rules 模式，`DEVTRACE_MCP_PUBLIC_TOOLS=enabled`，零模型调用）**：
  - 修复前的断言逻辑（同一份请求，控制跑）：HTTP 200、`kind=public_api`、`plan_status=complete`、`tools=["mcp_lookup_error_playbook","mcp_search_public_docs"]`、`decision_mode=rules`、`has_playbook=false`（base 名口径为 true）、其余四项 true → **exit 1**；
  - 修复后：**同一组判定 `has_playbook=true`，其余五项不变，exit 0**。
  - 前后**只有 `has_playbook` 这一项翻转**，其他断言的取值逐字相同，说明修复没有放宽任何别的东西。

### 4. 回归与静态检查

- 整表 **325 → 337 项通过 + 211 → 217 个 subtest**（新增 `tests/test_stream_cancellation.py` 12 项 + 6 个 subtest），两个口径都全绿：带 `PYTHONUTF8=1` **337 passed / 70.99s**，不带 **337 passed / 54.63s**（Windows 本机 Python 3.10.18）。旧用例无一改动、无一失败。CI 跑 3.12，`test_stdlib_wait_for_shape_swallows_the_same_cancel` 按 `skipIf` 设计跳过，预期 CI 为 336 passed + 1 skipped。
- CI Ruff `--select F` 清单 59 → **60 个文件**（新增 `tests/test_stream_cancellation.py`），本地按 CI 同参数 `All checks passed!`。
- 规则模式 holdout 作为回归重跑：**40/40**（conflict 3/3、auth 5/5、quota 3/3、ambiguity 3/3、webhook 4/4、combo 5/5、injection 7/7、anomaly 3/3、negative 2/2、multiturn 3/3、public_api 2/2；`by_decision_mode` 只有 `rules`、`by_batch` 19+21），退出码 0，输出写到工作区根目录 `.p12_rules.json`，**仓库内官方报告未被覆盖**；零模型调用。这只是回归，**官方模型模式 37/40 与 base 名补充 39/40 两份结论本轮未重跑、未改动**。
- 范围证据：`git diff --name-only 6beb3ca` 只有 4 个已跟踪文件（`.github/workflows/ci.yml`、`backend/api/main.py` + 本轮文档）与新增 `backend/tests/test_stream_cancellation.py`；`backend/developer/task_queue.py`、`api/worker.py`、`developer/planner.py`、`prompts.py`、`assistant.py`、`memory/`、`mcp/`、`frontend/`、以及所有 `eval_*.json` 官方报告的 diff 为空。

### 5. 未验证 / 风险

1. **3.11+/3.12 上的 `wait_for` 行为无法本机复现**：本机只有 3.10.18，负向对照用例在 3.11+ 按设计跳过。修复本身（`asyncio.wait`）在两个版本上语义一致，但"3.11+ 的 `wait_for` 确实会传播取消"这条只来自 CPython 变更说明与 CI 绿灯，没有本机专项对照。
2. **只做了单次 kill，没做"连环 kill"**：两个 worker 先后被杀、或幸存 worker 也在重跑中被杀的路径未在 Docker 上跑过。第 2 节的探针在 fake Redis 上覆盖了"第二次崩溃"，但那是实现级取证，不是真实栈。
3. **`waiting` 终态不等于任务做完了**：这一轮的计划把下一步交回用户，所以 `waiting` 是正确结果，但它同时说明"崩溃恢复后任务成功终结"与"用户问题被完整解决"是两件事，本轮只验证前者。
4. **kill 落点是概率性的**：`docker kill` 落在 `running` 后 1.22 秒，靠的是真实模型调用把窗口撑到 ~11 秒。rules 模式窗口 0.151 秒，打不进去；如果 provider 更快或更慢，落点会漂移。没有做"kill 恰好在工具执行中间 / 恰好在写运行记录时"的分相验证。
5. **成本超支已披露但无法复核**：7 次 provider 调用超出卡片允许的 2 次，计数来自当时的容器日志，`down` 之后不可复核（见第 2 节）。
6. **两个会话锁缺口只取证未修**（第 2 节末尾）：维护循环终结路径不释放锁 / 不写审计，以及 `release_session` 无条件 `DEL` 可能删掉更新任务的锁。当前 900 秒锁 TTL 会把第一种情况的影响限制在 15 分钟内，第二种需要"任务 A 的清理延迟到任务 B 已认领之后"才会显形，本轮**没有在真实栈上构造出这个竞态**，只有 fake Redis 上的路径证明。
7. 全部为小样本、单 provider、单机演示级验证，**不构成生产就绪结论**：没有多副本 API、没有真实故障切换演练、没有 WORM 审计、没有 TLS 与身份服务。
8. P13 候选：`release_session` 改 Lua 比较后删（锁值已存 `task_id`）+ 单测；`reclaim_expired`/`requeue_or_fail` 的终结路径补 `release_session` 与 `task.finished` 审计；连环 kill 与"kill 落在写运行记录瞬间"的分相演练；队列 relay 的 `end` 事件延迟收敛（本轮终态到 `<eof>` 之间还有一次 15 秒心跳间隔）；限流器健康检查/熔断；按 IP/租户分层配额；保留 CLI 的定时触发；多 provider 评测。

## 会话锁归属与维护循环终态（2026-10-23，P13）

范围：只修 P12 取证到的两个会话锁缺口，基线 `7e58feb`，代码提交 `8f437ca`（owner-aware 释放）与 `d149689`（回收终态收尾）。**本轮零模型调用**，没有起 `kill -9` 复跑，官方 holdout、评分口径、案例、标签、提示词、限流器、MCP、JWT、workspace、前端均未改动。

### 1. 缺口二：`release_session` 无条件 `DEL` → 按持有者比较后删

- **根因**：队列侧会话锁 `developer:session-task:<sha256(user\0conversation)>` 用 `SET NX EX 900` 认领，**锁值本身就是 `task_id`**，但释放一直是无条件 `DEL`。task-A 的清理若晚于 task-B 认领同一会话（终态处理延迟、回收后重排队、客户端重试），A 的 `release_session` 会把 B 的锁删掉——P12 的只读探针已复现这条：`held: true / owner task-B` → `held: false`，紧接着 task-C `try_claim_session` 返回 **true**，同一会话出现两个并发执行的任务。
- **修法**：`developer/task_queue.py` 新增 `RELEASE_IF_OWNER_LUA` 与 `release_session_if_owner()`（`backend/developer/task_queue.py:172`），由 Redis 原子执行 `if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) else return 0 end`，返回值归一为 `int`（1 = 本任务删掉了自己的锁，0 = 锁已不存在或属于别人），**绝不触碰其他任务的锁**。
- **保留原语与逐调用点判定**：`release_session()` 保留（`backend/developer/task_queue.py:162`），docstring 写明它是"不携带 task_id 的调用方（运维清理、测试）用的无条件原语，任何任务终态路径必须改用 owner-aware 版本"。生产代码里的三个终态释放点**全部替换**：`api/worker.py` 启动前取消分支与 `finally`、`api/main.py` 的 `_queued_stream` 入队回滚；替换后 `release_session` 在生产路径**已无调用点**，只剩原语定义与单测夹具。
- **边界**：同步 `/developer/chat` 用的是**另一把**锁 `developer:session-lock:<sha256>`（redis-py `lock()`，本身带持有者语义），P13 未动；本轮改动面只有 `developer:session-task:*`。
- **测试**（新增 `tests/test_session_lock_ownership.py`，12 项 + 2 个 subtest）：owner 释放返回 1 且锁消失、非 owner 返回 0 且锁值与 TTL 都不变、锁已不存在返回 0；一条脚本形状断言用 spy 替换 `redis.eval`，核对送进 Redis 的正是 `RELEASE_IF_OWNER_LUA`、`numkeys=1`、key 为 `session_key(user, conversation)`、argv 为 `task_id`，并检查脚本内含 `redis.call('GET',KEYS[1]) == ARGV[1]`（防止"逻辑看着对、脚本发错"）；端到端竞态用 `TakeoverAssistant` 在 A 执行期间把同一会话重新认领给 B，A 的终态释放没有删掉 B 的锁，随后 task-C `try_claim_session` 返回 **false**（必须等 B 释放）；三条终态路径各以 subTest 覆盖"别人持锁时不动"与"自己持锁时仍能释放"；`release_session` 的无条件语义单独保留一条断言，防止原语被悄悄改宽。
- **负向对照**：`test_blind_primitive_clobbers_the_newer_lock_the_owner_aware_one_does_not` 在同一场景里证明无条件 `release_session` 确实删掉 B 的锁，而 owner-aware 版本不删——这条决定性地说明修复改变了结果，而不是测试被写宽。
- **配套 fake 修正**：`ProdRedis.eval`（`test_queue_production.py`）、`QueueRedis.eval`（`test_task_queue.py`）、`StreamRedis.eval`（`test_audit.py`）原本只是 `return 1` 的桩；换上 owner-aware 释放后必须**忠实模拟比较删除**，否则"锁已释放"只是桩的产物。实现成 `if args and self.values.get(key) == args[0]` 才删——`if args` 守卫是必需的：限流器的 INCR 脚本不传 argv，无条件索引 `args[0]` 会让 `test_started_event_carries_trace_id` 抛 `IndexError`。

### 2. 缺口一：维护循环耗尽重试后的僵尸锁与缺失终态

- **根因**：`reclaim_expired()` 走 `requeue_or_fail()` 撞到 `MAX_ATTEMPTS=2` 时，只把记录置 `failed` 并塞进死信；会话锁仍挂在该 task 名下，而且这条终结路径**从来不写 `task.finished`**（`task_queue.py` 原本零审计调用，`task.finished` 只在 `api/main.py` 与 `api/worker.py` 写）。后果：该会话在整个 900 秒锁 TTL 内不可用，且审计流里这条任务凭空消失。
- **修法**：`reclaim_expired` 取得 `state = await requeue_or_fail(...)`，`state == 'failed'` 时调用 `_finalize_reclaimed_failure()`（`backend/developer/task_queue.py:237`）：读任务记录 → 用记录里的 `task_id` 做 **owner-aware** 释放（因此这条修复不可能删别人的锁）→ 写**恰好一条** `task.finished` 审计（`status=failed`，`detail={'status': 'failed', 'attempts': N}`，全部落在 `audit.DETAIL_FIELDS` 白名单内，不含消息正文与工具结果，`workspace`/`trace_id` 取自记录）。
- **尽力而为的取舍**：记录读取失败 → warning 后返回；锁释放失败 → warning 后**继续写审计**（两段各自独立 `try`——最初草稿把三步塞进同一个 `try`，一个锁故障会顺带吞掉终态审计，已改掉）；`audit.record()` 本身不抛。设计定位与其余审计一致：best-effort，Redis 故障不阻断主流程。
- **为什么不会与 worker 重复写审计**：这条只在"租约过期、原 worker 已死"的分支触发，死掉的 worker 从未跑到自己的终态处理；worker 正常失败路径照旧由 `api/worker.py` 写审计并释放锁。有专门用例断言"worker 已终结的任务被 reclaim 扫到不会二次审计"。
- **测试**（`tests/test_queue_production.py` 新增 6 项）：连续两次崩溃——第一次只重排队、**不写终态审计**、锁仍属 task-A（任务还要重跑，这是正确行为）；第二次进死信 → 锁被 owner-aware 释放、`task.finished` **恰好 1 条**、`status=failed` 且 `attempts` 进 detail；`reclaim_expired` **绝不删掉别的任务现在持有的锁**；租约仍有效的 `running` 任务不被终结也不写审计；worker 正常终态后 reclaim 不二次审计；把 `release_session_if_owner` patch 成抛异常时用 `assertLogs('developer.task_queue', level='WARNING')` 断言有 WARNING 且审计仍然写入。实现局限如实说明：fake Redis 没有 TTL，两轮崩溃之间要手工 `delete(RECLAIM_PREFIX + 'r2')` 清掉回收互斥锁，这是测试夹具的限制，不是被测语义。

### 3. 真实 Redis 上的 Lua 取证

单元测试跑的是 fake，"这段 Lua 真能被 Redis 执行"必须单独取证。用卡片允许的 `devtrace-verify` 栈（工作树源码经只读 bind overlay 挂载、容器 Python 3.12.15）跑只读探针 `p13_lua_probe.py`（工作区文件，**不入库**），产物 `.p13_lua_probe.json`：

- 只碰合成 key（`session_key('p13-lua-probe-user', 'p13-lua-probe-conversation')`），先 `EXISTS` 确认没被真实任务占用，再 `SET NX EX 60` 写入合成持有者 `t-A`（60 秒是探针自拟的短 TTL，生产是 900）。
- 非持有者 `EVAL(RELEASE_IF_OWNER_LUA, 1, key, 't-B')` → **0**，值仍是 `t-A`，TTL 仍是 60（没被删也没被改期）；持有者 → **1**，key 消失；key 已不存在时再调用 → **0**（重复释放幂等、不报错）。
- 直接调用生产函数 `task_queue.release_session_if_owner`：非持有者返回 0 且锁保留、持有者返回 1 且锁消失，返回类型是 `int`。
- 收尾：探针 key 已删除；`developer:session-task:*` 的 **key 数量**前后都是 0（只数数量、不读值，因此不会打印任何真实会话标识），应用侧锁未被触碰；连接串只从容器环境读取，未打印。整轮重复执行一次，结论一致。
- **明确没有做**：没有往真实队列注入合成 `running` 任务去端到端跑 `reclaim_expired`——那会污染应用的索引与优先级列表，并有被 worker 真的执行、从而产生模型调用的风险。第 2 节的回收终态语义只有 fake Redis 证据，真实栈上**未验证**。
- 栈收尾 `docker compose down`（未加 `-v`，三个数据卷保留），未触碰其他项目的容器。

### 4. 变异对照

- **变异 A**：把 `release_session_if_owner` 实现换成盲删（`delete(session_key(...))` 后 `return 1`）→ `test_session_lock_ownership.py` **9 项失败**，其中包括 `api/main.py` 的入队回滚点；还原后全绿。
- **变异 B**：把 `reclaim_expired` 里的 `if state == 'failed': await _finalize_reclaimed_failure(...)` 换成 `pass` → **恰好 2 项失败**（`'r2' is not None` 即锁没释放、以及缺失的那条 `task.finished`），而"第一次崩溃不写终态审计"与"绝不删别人的锁"两项**正确地保持绿色**，说明它们不是被这条修复顺带带过去的。
- 两次变异都用字节级备份 + `cmp` 校验还原，之后重跑整表。

### 5. 回归与静态检查

- 整表 **337 → 355 项通过 + 217 → 219 个 subtest**（新增 `tests/test_session_lock_ownership.py` 12 项 + 2 个 subtest、`tests/test_queue_production.py` 6 项），两个口径都全绿：不带 `PYTHONUTF8=1` **355 passed / 106.00s**，带 **355 passed / 118.13s**（Windows 本机 Python 3.10.18）。旧用例无一改动、无一失败。墙钟比 P12 的 54–71 秒翻倍，本轮做了排查而不是默认接受：`--durations=8` 显示最慢的 8 项里前 7 项全是既有的 `tests/test_stream_limits.py`（7.25–7.80 秒/项）、第 8 项是既有的 `tests/test_rate_limit_degradation.py`（4.15 秒），本轮新增用例一项都不在其中，判定为宿主负载而非新测试引入的等待；三次运行（含变异还原后的复跑，另一次 **355 passed / 109.67s**）报出的通过数完全一致。
- CI Ruff `--select F` 清单 60 → **61 个文件**（新增 `tests/test_session_lock_ownership.py`），本地按 CI 同参数运行 `All checks passed!`。
- 规则模式 holdout 作为回归重跑：**40/40**（conflict 3/3、auth 5/5、quota 3/3、ambiguity 3/3、webhook 4/4、combo 5/5、injection 7/7、anomaly 3/3、negative 2/2、multiturn 3/3、public_api 2/2；`by_decision_mode` 只有 `rules`，`by_batch` 19+21），退出码 0，输出写到工作区根目录 `.p13_rules.json`，**仓库内官方报告未被覆盖**；零模型调用。这只是回归——**官方模型模式 37/40 与 base 名补充 39/40 两份结论本轮未重跑、未改动**。
- 范围证据：`git diff --name-only 7e58feb HEAD` 只有 8 个文件（`.github/workflows/ci.yml`、`backend/api/main.py`、`backend/api/worker.py`、`backend/developer/task_queue.py`、`backend/tests/test_audit.py`、`test_queue_production.py`、`test_task_queue.py`、新增 `test_session_lock_ownership.py`）；`developer/planner.py`、`prompts.py`、`assistant.py`、`memory/`、`mcp/`、`frontend/`、限流器与所有 `eval_*.json` 官方报告的 diff 均为空。

### 6. 流程硬规则（写进交接文档）

**任何以 `docker kill` / 崩溃取证为目的的验证，必须在 `docker compose down` 之前把日志导出并结构化落盘**（容器名、时间戳、计数、`ExitCode`/`OOMKilled` 等原始字段都要有可读文件），因为 P12 的 7 次模型调用计数只能靠当时的容器日志统计，`down` 之后**永久无法复核**。这条不是建议：崩溃取证的可复核性完全取决于 down 之前的那一次导出。

### 7. 未验证 / 风险

1. **真实栈上的连环 kill / 双 worker 崩溃回收未复跑**（卡片明确禁止本轮做 kill 演练）。回收终态路径只有 fake Redis 证据，第 3 节只覆盖了 Lua 原语本身。
2. `test_api_security.py`、`test_workspace.py`、`test_stream_cancellation.py` 里的 fake `eval` 桩仍是 `return 1`，不模拟比较删除。它们驱动的代码路径不经过会话锁释放，所以本轮结论不受影响；但**如果以后让这些 fake 也走 owner-aware 释放，必须先换成忠实模拟**，否则"锁释放成功"是桩给的假证据。
3. `release_session_if_owner` 返回 0 无法区分"锁已消失"与"锁属于别人"。调用方只需保证"没碰别人的锁"，因此够用；若将来要据此判断"会话里是否还有活跃任务"，需要额外读取。
4. Lua 脚本硬编码单 key 单参数（`KEYS[1]`/`ARGV[1]`），换 Redis Cluster 或改用 `register_script`/`EVALSHA` 时需重审。
5. 同类"僵尸锁"形状还有两处**按范围纪律未修、只记录等裁定**：`task_queue.recover_running_tasks()`（worker 启动恢复）与 `api/main.py` 的 `reconcile_stale_tasks` / `_mark_interrupted`——它们标记中断任务时的锁处理本轮没有改动，也没有为其编写用例。
6. 全部仍是演示级、单 Redis、小样本验证，**不构成生产就绪结论**：没有多副本 API、没有真实故障切换、没有 WORM 审计、没有 TLS 与身份服务。
