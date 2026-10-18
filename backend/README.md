# DevTrace 后端

独立 FastAPI 服务，提供 API 接入诊断 Agent、记忆与本地模拟平台。完整启动和项目介绍见[仓库首页](../README.md)，实现细节见[开发者文档](developer/README.md)。

运行 `python setup_local.py` 设置本地演示账号，随后在生成的 `.env` 中填写模型配置；不要提交真实密钥。执行 `docker compose up -d --build` 启动服务。也可配置 `DEVELOPER_MODEL_MODE=rules` 使用规则模型演示。
