# DevTrace 前端

Vue 3 + Vite 开发者 API 接入助手，展示诊断计划、工具观察、证据与记忆。请先启动 `backend` 中的服务，再在本目录运行 `npm ci` 和 `npm run dev`。默认访问 `http://localhost:5173`，Vite 将 `/api/python` 代理到 `http://localhost:8000`。可通过 `VITE_PYTHON_API_URL` 指定后端地址。构建命令为 `npm run build`。2026-10-18 已用 Playwright + Chromium 在真实浏览器联调过登录、诊断、证据/安全卡片与注入场景（见 [评测报告](../backend/developer/evaluation_report.md)）。
