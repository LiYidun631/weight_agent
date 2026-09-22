# Weight Agent

一个使用 FastAPI 构建、由 `uv` 管理 Python 与依赖的服务框架。

## 本地启动

```powershell
uv sync
uv run python src/main.py
```

启动入口会读取项目根目录 `.env`：

```dotenv
WEIGHT_AGENT_HOST=127.0.0.1
WEIGHT_AGENT_PORT=8001
WEIGHT_AGENT_RELOAD=true
```

修改 `WEIGHT_AGENT_PORT` 后，服务会使用对应端口启动。也可以直接使用 Uvicorn 命令：

```powershell
uv run uvicorn weight_agent.main:app --host 127.0.0.1 --port 8001 --reload
```

服务启动后可访问：

- API 文档：`http://<host>:<port>/docs`
- 健康检查：`http://<host>:<port>/api/v1/health`
- 身体成分报告：`POST /api/v1/report`

## 常用命令

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

配置通过 `WEIGHT_AGENT_` 前缀的环境变量传入，参考 `.env.example`。
配置按接口分组：

- 服务基础配置：应用名、环境、监听地址、端口和 API 前缀。
- Chat 接口配置：意图识别阈值、会话记忆 TTL、轮次上限和默认上下文。
- Report 接口配置：报告模型、超时时间、温度、最大输出 token 数和 thinking 开关。
- 模型服务配置：模型服务地址和 API 密钥。

以上配置均可在 `.env` 中调整，无需修改代码。敏感密钥只写入本地 `.env`，不要提交到版本库。

## 设计文档

- [Chat 接口设计](docs/chat-interface-design.md)：记录 Chat V1 的接口契约、LangGraph 工作流、SSE 协议、设计决策和实施进度。
- [Report 接口设计](docs/report-interface-design.md)：记录 Body270 字段映射、响应契约和模型超时兜底规则。

## 目录结构

```text
src/weight_agent/
  api/            # HTTP 路由
  core/           # 配置等基础设施
  main.py         # 应用工厂与服务入口
tests/            # 自动化测试
```
