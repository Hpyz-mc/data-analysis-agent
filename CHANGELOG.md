# 变更记录

## 1.0.0（2026-10-06）

- 新增三类可核验分析：排名 TOP-N（ranking）、占比构成（distribution，合计 100%）、十分位分层（decile，各层合计与独立总额核对），贯通意图识别、结构化范围、确定性 pipeline 与报告。
- 新增相关性分析（correlation，数据库 corr 聚合 + 分箱均值散点图，相关 ≠ 因果）、异常值截尾/缩尾（outlier，1%/99% 分位边界对比处理前后，不改原数据）与描述性统计（summary，一次查询合计/平均/最小/最大）。
- 模型决策从手写 JSON 迁移为原生 Function Calling：`decide_next_step` 工具 + strict 模式（官方端点自动走 /beta），报告阶段强制 `finish_report`；保留内容 JSON 兜底与 `LLM_TOOL_CALLS` 开关，从根上解决结构化输出不稳定导致的 502 与规划失败。
- 修复“合计”追问在 trend 上下文仍按月度分组、“走势”追问继承归因维度的问题。
- 重整项目首页，补充实际截图、首次安装指南和合成数据演示。
- 统一环境模板、代码和 Compose 的默认模型及可选配置说明。
- 将历史报告归入 `docs/archive/`，SQL 示例归入 `examples/sql/`，验收脚本归入 `scripts/`；会话 schema 归入 `docker/db/`。
- 修复前端依赖清单与锁文件不一致导致首次 Docker 构建失败的问题，保留已锁定的依赖版本。
- 增加后端测试、前端测试与构建、Compose 配置检查的 GitHub Actions 工作流。
- 增加贡献指南、Bug / 功能建议模板及 PR 模板。

1.0.0 已发布：149 个单元测试全绿，7 轮真实模型验收通过，CI（后端/前端/Compose）通过。
