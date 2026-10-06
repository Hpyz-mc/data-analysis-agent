# 贡献指南

欢迎报告问题、改进文档和提交代码。项目当前用于本机演示；先阅读 [README](README.md)、[安装指南](docs/quickstart.md) 和 [已知限制](README.md#已知限制)。

## 提交问题

Bug 请使用 Issue 模板，填写复现步骤、预期结果、实际结果及操作系统、Docker 版本。分析结果问题请使用合成数据并给出可手工核对的数字。不要附带 `.env`、真实密钥、数据库连接串、完整业务数据或未经脱敏的会话记录。

## 开发与检查

后端使用 Python 3.12，在项目根目录运行：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Windows PowerShell 使用 `.venv\Scripts\Activate.ps1` 激活环境。前端使用 Node 22（22.13 或更新）和 pnpm 11.25.0：

```bash
cd web
pnpm install --frozen-lockfile
pnpm test
pnpm build
```

单元测试使用模拟依赖，不需要真实 API Key、模型调用或数据库。CI 执行相同的后端测试、前端测试和构建，并用占位值校验 Compose 配置。CI 不覆盖真实模型回答质量、浏览器交互或完整 Docker 首次启动。

修改数据库初始化文件时，需要分别核对新数据卷首次启动和既有数据卷迁移。修改 SSE 时，需要核对 Unicode 分块、断流及历史保存。修改分析逻辑时，请用 [演示数据](examples/data/sales_demo.csv) 检查数字及范围。

## 提交 PR

从 `main` 创建分支，按一个明确问题组织提交。在 PR 中说明问题、修改后的行为、验证方法及未验证的部分。涉及界面变化可附脱敏截图；涉及数据库变更请说明兼容与迁移方式。不要提交 `.env`、运行时图表、验收原始 JSON、依赖目录或构建产物。

真实模型验收脚本见 [scripts](scripts/README.md)。运行会产生模型费用并新建测试会话，按需手动执行，不接入 CI。
