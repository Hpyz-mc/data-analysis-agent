# 首次安装与运行

推荐使用 Docker Compose：它同时启动 PostgreSQL、API 和前端，无需手动安装本机数据库。

## 准备环境

- Git：私有仓库需要有访问权限的 GitHub 账号。
- 已启动的 Docker Desktop 或 Docker Engine，包含 Compose 插件。
- DeepSeek API Key 和账号可调用的模型 ID。实际分析会调用远程模型并产生 API 用量。
- 默认本机端口：工作台 `8501`，API `8001`。数据库不发布到宿主机端口。

确认 Docker 可用：

```bash
docker version
docker compose version
```

## 下载项目

```bash
git clone https://github.com/Hpyz-mc/data-analysis-agent.git
cd data-analysis-agent
```

已有本地仓库可直接进入其根目录，以下命令均从根目录执行。

## 配置环境

macOS / Linux：

```bash
cp -n .env.example .env
```

Windows PowerShell：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

两个命令都会保留已有 `.env`。用文本编辑器打开它，按下表填写；不要把占位符当作实际密码。

| 配置项 | Docker 首次安装的用途 |
| --- | --- |
| `LLM_API_KEY` | 必填，自己的模型 API Key |
| `LLM_API_ID` | 账号可调用的模型 ID；模板中的名称是示例，请按服务实际支持情况确认 |
| `LLM_BASE_URL` | 模型 API 地址，模板为 `https://api.deepseek.com` |
| `DOCKER_POSTGRES_PASSWORD` | 必填，Docker 数据库管理员密码 |
| `DOCKER_READER_PASSWORD` | 必填，业务分析只读账号密码 |
| `DOCKER_SESSION_PASSWORD` | 必填，数据导入及会话写入账号密码 |
| `DATABASE_URL` / `SESSION_DATABASE_URL` | 直接运行本机 Python 后端时使用；Compose 自动替换为内部 `db` 服务的连接串 |

三个数据库密码各不相同，建议使用 URL 安全的随机密码。若有本机 Python，可以执行下面的命令三次，将结果分别填入对应配置项：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

Windows 可将 `python3` 换为 `python`。没有本机 Python 时，可以用 Docker 一次生成三条密码：

```bash
docker run --rm python:3.12-slim-bookworm python -c "import secrets; print('\n'.join(secrets.token_urlsafe(24) for _ in range(3)))"
```

此命令仅打印密码，需自行填入 `.env`。`.env` 已被 Git 忽略，不要放进截图或反馈记录。

可选配置：

```env
DOCKER_UI_PORT=8501
DOCKER_APP_PORT=8001
LLM_THINKING=disabled
LLM_TIMEOUT_SECONDS=60
```

更多模型调用上限与预算设置见 [`app/core/config.py`](../app/core/config.py) 和 [`compose.yaml`](../compose.yaml)。首次运行可保留默认值。

## 启动服务

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

第一次构建需要下载镜像和依赖。预期 `db`、`api` 为 running / healthy，`ui` 为 running。`config --quiet` 只校验配置，不打印连接串或密钥。

| 页面 | 默认地址 | 用途 |
| --- | --- | --- |
| 工作台 | <http://127.0.0.1:8501> | 上传数据、提问、查看历史与图表 |
| API 文档 | <http://127.0.0.1:8001/docs> | 查看和调试接口 |
| 健康检查 | <http://127.0.0.1:8001/health> | 预期返回 `{"status":"ok"}` |

健康检查仅说明 API 进程可响应，不代表模型调用、数据库分析已成功。请继续完成一次[合成数据演示](demo.md)。

## 完成第一次分析

1. 在工作台进入“数据集管理”。
2. 选择 [`examples/data/sales_demo.csv`](../examples/data/sales_demo.csv)，确认上传。
3. 核对导入结果：应为 27 行、7 列，没有截断。表名由系统分配，例如 `user_sales_demo_日期_序号`。
4. 选择该数据开始分析，输入：“分析 2026 年 1 至 3 月的月度销售额走势，销售额按 sales_amount 求和，并生成折线图。”
5. 等待完整结果，核对月销售额 `666,000 / 652,300 / 702,600` 和图表，再尝试同会话追问。

示例是合成数据。正式工作台会隐藏内置测试表，因此通过 CSV 上传进行首次体验，无需调用开发用的演示表初始化接口。

## 升级与维护

已有安装升级：先保存本地代码修改，拉取更新，保持既有数据库密码，然后运行：

```bash
git pull --ff-only
sh docker/migrate_session.sh
docker compose up -d --no-deps --build api ui
```

迁移脚本要求 `db` 已运行，为已有会话表补齐字段和当前授权，保留记录。Windows 可在 Git Bash / WSL 中执行 `sh` 脚本。首次初始化只在空数据库卷上执行；重建应用镜像不会自动迁移已有数据库。

日常停止和再次启动：

```bash
docker compose down
docker compose up -d
```

不带 `-v` 的停止命令保留数据库和图表卷。**不要为排错执行 `docker compose down -v`，它会删除持久化数据。** 当前没有自动备份；已有数据库卷时，仅修改 `.env` 密码不会同步修改数据库账号密码。

## 常见问题

| 现象 | 排查方法 |
| --- | --- |
| 无法克隆 / GitHub 404 | 确认仓库地址与账号访问权限；私有仓库不能匿名克隆 |
| Docker 无法连接 | 启动 Docker Desktop / Docker Engine，重新执行 `docker version` |
| 提示缺少环境变量 | 确认根目录存在 `.env`，并填入 API Key 和三个数据库密码 |
| 端口被占用 | 修改 `DOCKER_UI_PORT` 或 `DOCKER_APP_PORT`，再次 `docker compose up -d`，使用新端口访问 |
| 页面打不开 / 502 | 查看 `docker compose ps` 和 `docker compose logs --tail=80 ui api` |
| 数据库连接或导入权限失败 | 查看 `docker compose logs --tail=80 api db`；已有数据卷确认密码未被单独改动，并应用迁移脚本 |
| 模型报错、超时、回答未完成 | 确认 Key、模型 ID、API 地址和网络；查看页面状态和 API 错误。已有可用结果不代表该轮全部完成 |
| 重复上传返回 409 | 使用页面提供的已有数据版本入口；同一内容不会重复建表 |
| 提示缩小查询范围 | 缩小时间或筛选范围，或请求汇总；截断结果不能作为完整结论 |

反馈错误前请脱敏日志。普通日常使用只需工作台；本机 Python / PostgreSQL 开发的历史说明见[开发记录](development-notes.md)，前端热更新见 [`web/README.md`](../web/README.md)。
