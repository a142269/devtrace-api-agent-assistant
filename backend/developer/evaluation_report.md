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
