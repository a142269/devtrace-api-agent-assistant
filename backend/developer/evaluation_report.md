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
Ran 94 tests in 5.9s
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

## 提示词版本化（2026-10-18）

把散落在 `assistant.py` 的提示词集中到 `backend/developer/prompts.py`：

- 五个 `string.Template`：`SYSTEM / PLAN / ACTION / NATIVE_ACTION / REVISE`，统一由 `PROMPT_VERSION`（当前 `2026-10-18.1`）标识；`render(name, **values)` 显式填充。
- 响应新增 `prompt_version`；`GET /developer/prompts` 只返回版本和每个模板字符数，不返回正文。
- 快照回归：`evaluate_prompts.py` 对固定 fixtures 渲染后计算 SHA-256 存入 `prompt_snapshots.json`，并检查安全契约（"不可信数据""只输出 JSON""ready_steps""不能重复"等）；CI 运行 `python -m developer.evaluate_prompts`，提示词变更必须显式 `--update` 并审阅 diff。
- 测试：快照匹配、契约、未知模板拒绝、元数据不泄露正文、响应带 `prompt_version`，共 5 个新用例（测试总数 94）。
- Docker + 真实模型 HTTP：`GET /developer/prompts` 与响应版本均为 `2026-10-18.1`，元数据不含提示词正文，诊断计划完成、`decision_mode=model`；报告 [eval_prompts_http_2026-10-18.json](eval_prompts_http_2026-10-18.json)，静态回归 [eval_prompts_2026-10-18.json](eval_prompts_2026-10-18.json)。

局限：没有 A/B 分流和逐模型提示词调优；快照只能证明"提示词没被悄悄改"，不能证明改得更好；注入防护仍是正则启发式。

## 原生 tool calling（2026-10-18）

行动选择从"提示词要求模型输出 JSON"升级为"原生工具调用 + 两级降级"：

- `planner.py` 新增 `TOOL_SCHEMAS`（与运行时工具注册表一致），`choose_action` 把当前 ready steps 对应工具的 JSON Schema 作为 Anthropic `tools` 传入，`tool_choice={"type":"auto"}`；
- 模型返回 `tool_use` 时校验必填参数，映射到 ready step；没有 `tool_use` 或参数不足时回退 JSON 提示，再回退规则；provider 不支持 tools 时进程内禁用原生路径（避免每轮失败调用）；
- 指标新增 `native_tool_calls`，可区分"模型原生调用"与"JSON 降级"；
- 测试：原生选择、缺参回退、Schema 覆盖 3 个新用例（测试总数 94）。

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

局限：目前只验证单 worker；队列基于 Redis List，没有优先级/延迟队列/定时任务；worker 崩溃后正在执行的那一次会重试（可能重复工具副作用，工单幂等键可防重复），没有多 worker 分片与租约。同步 `/developer/chat` 仍是进程内执行。

## 依赖安全检查（2026-10-18）

`pip-audit` 与 OSV 核查确认 `chromadb==0.5.23` 有 3 条漏洞（1 CRITICAL 代码注入、2 HIGH 跨租户授权），OSV 的受影响范围到 `1.5.9`（当前最新版），**没有修复版本**。处置为不升级（升级不能修复且迁移风险大）、保持 ChromaDB 仅在 Compose 内部网络、明确单租户演示边界，并在 CI 增加审计门禁（只阻断已有修复版本的漏洞）。新增回归测试覆盖内部服务不发布端口、`.env` 不进 Git/镜像、镜像非 root 等部署约束。完整评估见 [依赖与安全评估_2026-10-18.md](依赖与安全评估_2026-10-18.md)。本机重跑 `pip-audit` 时 PyPI JSON 请求停滞，完整扫描沿用 2026-10-17 的 91 依赖报告（仅这 3 条）并以 OSV 官方数据核对。

