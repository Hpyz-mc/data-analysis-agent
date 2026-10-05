# 数据分析 Agent

## 执行入口与预算收尾

趋势、比较和诊断任务必须先确认结构化范围，再由程序执行查询、核验、辅助查证和图表；范围缺失仅允许一次规划补正，不再落回自由决策循环。模型调用上限仍为 6，最后一个名额预留给报告。中途预算或模型失败时，已有可用结果会保留为未完成，不自动重跑工具。普通和流式接口新增安全 `diagnostics` 摘要，`usage.calls[].phase` 标记规划/决策/报告；失败摘要也保存在会话历史。实现、验证边界及复测建议见 [执行可靠性修复记录](outputs/execution_reliability_20261004.md)。

## 回答完整性与辅助查证

结构化单表变化分析现在会记录本轮应回答的要求：趋势、维度拆解、明确要求的折线图/贡献图，以及适用的订单量、客单价和供货记录。辅助数据必须沿用主分析范围，并与独立销售额总额核对；未核对成功不能宣布完成。对已确认可加总的订单数，程序按总销售额/总订单量计算客单价，再做对称机械分解。演示表的供货状态与缺货记录仅用于关联查证，不能证明因果，也不能把跨品类缺货记录相加为自然日。

追问继承当前表、指标、比较范围与筛选，澄清后的补充保留原问题。最终报告减少重复数字和内部工具术语；如果遗漏已核验的关键计算，程序补充有限的证据摘要，不额外调用模型。这不是通用多指标分析或完整自然语言事实校验器：未知订单粒度、额外主指标或不支持的图形先澄清，不静默漏答。统一测试步骤及实现边界见 [回答质量阶段二](outputs/answer_quality_20261004.md)。

## 文字报告中断时的结果保留

最终文字报告达到输出上限、等待超时或连接中断时，若已经取得可用查询结果，接口会返回 `finished=false` 的“本轮仅部分完成”结果：只展示程序执行过的查询预览、已核验计算和真实图表，不保留半截模型正文作为正式回答，不自动重跑工具或追加模型调用。会话保存为 `incomplete`，刷新后仍可查看已保留的回答和图表，但该轮不会进入后续已完成问答上下文。规划阶段尚未取得数据就失败的请求仍返回错误，不伪造可用结果。

API `usage.calls` 新增 `finish_reason`；状态区分 `output_limit`、`timeout` 和 `stream_error`。若收到模型末尾的 usage，仍记录实际 Token；未收到则保持未知，不估成零。程序另外提供最高/最低值的全部对应月份，以及相邻返回月份是否间隔一个月，避免把跨月变化称为月环比。这些事实与提示词约束仍不等于完整的自然语言事实校验器。

本次本地检查与部署记录见 [回答可靠性阶段一](outputs/report_reliability_20261004.md)。没有新增真实模型调用；不能据此宣称远程网络问题已解决或真实回答已验收通过。

## 报告表达边界

最终报告的 JSON 与 SSE 两条路径共享报告约束：左闭右开的查询结束日期不属于本轮范围，“未查询”不能写成“无数据”；范围外的原表情况不做推断。程序从本轮未截断的月度结果生成精确相邻差额与最大最小差额，提供给报告模型，避免把 50,300 写成“5 万以内”。归因贡献率按本轮净变化额解释，区分总体归因与筛选后的子范围归因，不与维度自身变化率混淆。

实现位于 `app/agent/report_facts.py` 和 `app/agent/prompts.py`。这些是证据整理与报告生成约束，不是完整的自然语言事实校验器；`finished=true` 仍不保证每句报告绝对正确。此次回归测试没有新增付费模型调用，不把此前真实验收报告当作本次修复后的模型复测结果。

## 复合任务完整性：趋势 + 维度拆解

“分析月度变化，并按渠道拆解主要来源”现在走复合趋势流程，而不是只完成总体走势：按相同时间、筛选和指标分别查询逐期独立总额与维度明细，再用 `sequential_attribution` 复用两期归因核验，计算每对返回的相邻时期贡献。明确要求贡献图时，额外从已核验的变化额绘图；保留总体折线图。两份 SQL 均为只读查询，不新增模型计算轮次。

明确的拆解字段不会因模型在计划中遗漏而静默丢失；无法唯一确定字段时先澄清。所有预定查询、计算和图表都要取得有效结果才能标记 `finished=true`。失败时保留已完成的总体变化，明确标注“本轮仅部分完成”，不将该轮作为已完成会话证据，也不额外调用模型伪装成成功报告。报告压缩重复口径与数字，优先覆盖各项要求和图表，不提高已有输出 Token 上限。

逐期归因支持最多 30 个返回时期、100 行未截断明细；贡献图沿用最多 30 个数据点的限制。仅适用于单表可加总指标和一个拆解维度，不支持均值/比率归因、跨表关联或因果识别。缺失时期不能直接当作零；这里只比较实际返回的相邻时期。针对性检查可运行 `python -m unittest tests.test_trend_breakdown tests.test_analysis_context -v`。

本次前端真实验收的结果和未通过项见 [复合分析验收记录](outputs/trend_breakdown_acceptance_20261004.md)。本地检查通过不等于真实模型验收通过。

## Token 运行观测

聊天页面使用 `POST /api/v1/analyze/stream` 的 SSE 流式接口：分析期间发送 `status` 进度，证据检查通过后发送模型真实生成的 `delta` 文本，完整回答入库后发送 `result`（包括图表、Token 用量、耗时和会话 ID）。终止错误为 `error` 事件；流式 HTTP 连接建立后，不能仅凭 HTTP 200 判断分析成功，必须收到 `result`。图表在最终结果返回后展示。中断草稿不作为已完成问答保存。

原有 `POST /api/v1/analyze` 非流式 JSON 接口保留，FastAPI 文档仍可查看完整用量。流式报告替代证据充足后的最终决策调用；直接结束的概念回答会另调用一次报告生成，该消耗同样计入 `usage`。模型流未返回用量时保持未知，不估算为零。

`POST /api/v1/analyze` 的响应增加 `usage`，与 `timings_ms` 一起在 FastAPI `/docs` 中查看，不在聊天前端展示。包含输入、输出、总 Token、缓存命中输入 Token、模型调用数、格式重试数，以及逐次调用的模型、状态和用量。缓存命中是输入的子集，不能重复计入总量。

供应商未返回用量时字段为 `null`，`complete=false`；没有模型调用时才是 0。格式失败后的重试消耗也计入汇总。SDK 隐式网络重试已关闭，避免不可见的额外调用。模型失败响应保留原有 `detail`，额外返回 `usage`；失败轮次的用量也保存到会话历史。

20 轮同会话真实测试及发现的问题见 [Token 测试报告](outputs/token_benchmark_report.md)。本次总消耗 294,848 Token、53 次模型调用，不能据此宣称所有分析问题都已正确完成。

当前进度是 V6：在 V5 的容器、数据导入、会话和图表基础上，加入按问题类型分流的分析任务、变化诊断的证据闭环和基础字段语义约束。它仍是本机演示项目，不应以“完全自主数据科学”或企业生产系统对外描述。

### 多层下钻、图表与追问

会话现在单独保存结构化分析范围：表、可加总指标、比较时期或群体、筛选条件、当前维度与下钻路径。追问“线下继续按品类看”会保留原指标和两个时期，新增线下渠道筛选，并重新核对该层独立总额与品类明细。用户明确修改时期或取消筛选时更新范围。页面展示当前范围，并提供适用的下钻或趋势快捷入口。

对于提交了结构化范围的单表分析，程序生成同口径的总额与明细 SQL，调用归因工具核验后，直接使用归因结果生成正负变化额柱状图；月度趋势使用本轮查询结果和变化率计算生成折线图。图表文件仍保存在 Docker 数据卷中，图表链接、快捷入口和每轮范围存入会话记录，历史重新打开后可恢复。

已有数据库首次使用此版本前运行 `sh docker/migrate_session.sh`，为会话表增量增加 `analysis_context` 和 `artifacts` 字段；已有记录保留。再运行 `docker compose up -d --no-deps --build api ui` 更新服务。本地验收已应用该升级。

目前支持单表、可加总数值指标（含数量×单价等表达式）、两个互不重叠时期或两个群体，以及逐轮选择维度下钻。缺少字段时会提示；比率、均值的归因、跨表关联和因果识别尚未支持。用法与可核对的演示数字见 [多层下钻验收指南](outputs/v6_drilldown_guide.md)。

## V6：变化诊断与证据闭环

普通事实查询保持快速路径；明确要求趋势分析时，至少核对两个时间点并调用 `percent_change`。需要解释时间变化时，Agent 先确定两个时期；需要比较地区、渠道等群体时，则按两个群体分析，不强行套用时间归因。两类诊断都需要独立总额、同口径维度明细和 `attribution` 核对。问题未给出必要的比较基准时先澄清，澄清轮会保留在会话历史中，用户补充后可继续原问题。未取得必要证据时不能把回答标记为完成。诊断最多比普通查询多两轮，仍保留只读账号、SQL 校验、超时和行数限制。

演示表已标注金额、订单数和断货天数的粒度与聚合方式。`stockout_days` 可能在不同品类重复记录同一段时间，程序会拦截对它的 `SUM`，避免把行值相加误写成实际断货自然日。用户导入表中疑似比率、均值或持续时间的字段只会得到谨慎提示；系统尚不能自动确认所有自定义字段的业务语义。

开发验收可通过 `POST /api/v1/datasets/preset` 准备合成演示表，再使用分析接口提问：`请分析 demo_ecommerce_orders 中 2026 年 2 月销售额为什么比 1 月下降：先量化变化，再按渠道归因，并检查表内可见的供应状态线索。` 正式前端不提供演示表加载入口或预设测试问题。演示数据的 1 月总额为 666,000，2 月为 652,300，净减少 13,700；线下门店减少 37,000，另外两个渠道的增长抵消了部分降幅。回答应把这些已核对的贡献与“断货是否导致下滑”的因果判断分开；仅凭表内同时出现的供应状态线索，不能证明因果。

这是面向变化问题的首版分析闭环，不保证能自动诊断所有业务场景。跨表关联、统计显著性、因果识别和复杂预测尚未实现；缺少合适的时间或维度字段时，应说明证据缺口。

## 本机分析工作台（正式 UI）

在项目根目录运行 `docker compose up -d --build` 后，打开 <http://127.0.0.1:8501>。项目唯一前端为 `web/` 中的 React + TypeScript 工作台，已替换旧 Streamlit。左侧是历史会话与数据集入口，其余区域展示提问、真实分析进度、流式报告和图表；不设置右侧分析范围栏，窄屏时自动调整布局。分析范围仍由后端保存，用于多轮追问。没有样稿中的固定分析数字或模拟进度。

页面左侧自动显示最近 20 个会话，点击即可加载历史并继续提问；“新建会话”在首次发送时自动创建会话，用户无需复制或填写 ID。当前轮的 SQL/工具过程可展开查看。删除历史会话前会要求确认，再通过 `DELETE /api/v1/sessions/{session_id}` 永久删除该会话及所有聊天记录；项目没有自动备份，删除后无法从页面恢复。若 8501 已被占用，可在现有 `.env` 中设置 `DOCKER_UI_PORT=其他空闲端口`。

浏览器通过同源 `/api/v1/` 访问 API，由 Nginx 转发到 Docker 内部的 API 服务；前端不会收到 DeepSeek 密钥或数据库密码。流式路径关闭代理缓冲。它仅供本机演示，不能作为公网访问入口。重开浏览器后，直接从左侧选择最近会话；数据库仍是会话记录的唯一持久化来源。列表接口是 `GET /api/v1/sessions`，按最近活动排序，每项标题来自第一轮问题去除前端自动表名前缀后的前 15 个字，悬停可查看完整问题；该展示清理不改写原始对话记录。当前没有用户登录或会话隔离，不要将服务暴露到公网。前端开发说明见 [web/README.md](web/README.md)。

页面故障先看 `docker compose ps` 和 `docker compose logs ui`；分析接口仍可从 <http://127.0.0.1:8001/docs> 独立测试。

如果沿用旧的 Compose 数据卷，首次初始化的授权不会自动更新。运行 `sh docker/migrate_session.sh` 为会话专用账号补上删除权限；它不会清除已有记录。

### 数据集导入

左侧“数据集”入口进入真实的数据管理页面，可上传 CSV、XLSX、XLS。正式前端不提供演示数据加载或预设测试问题，并从可选列表隐藏内置的 `demo_ecommerce_orders` 和 `agent_sales_sample`，不删除数据库表或历史会话。`GET /api/v1/datasets` 通过只读账号列出最近 20 份有访问权限的已登记数据集，返回列名和前 3 行预览；不会创建表，也不暴露文件哈希或会话 ID。选用数据集后，首次提问自动指定对应数据表。

文件最多 20MB、200 列；只导入前 50,000 行，若被截断会在响应和页面中提示。CSV 依次尝试 UTF-8 和 GBK；金额、日期和空值会标准化。相同文件内容再次上传会返回 409，不会重复建表，页面提供直接使用已有表的入口；不同内容的同名文件会创建 `user_文件名_日期_序号` 新版本，不会覆盖旧表。演示表的线下渠道 2026 年 2 月销售额较 1 月下降 18.5%，并包含缺货天数字段，供下钻分析；这只是一份合成示例数据。

导入使用 `SESSION_DATABASE_URL` 的平台写入账号；分析仍使用 `DATABASE_URL` 的只读账号。前者需要在当前 `agent_db` 的 `public` schema 中具备 CREATE 权限，后者仅获得新建数据表的 SELECT 权限。已有 Compose 数据卷须再次运行 `sh docker/migrate_session.sh` 应用授权。后台审计表 `system_upload_records` 记录文件哈希、行数、版本名及上传时间，不会展示给 Agent 的表结构；分析 SQL 也不能读取平台内部管理表。

这些功能仍是本机演示版本，没有登录鉴权、租户隔离、病毒扫描或上传内容的完整安全审计，不适合直接接入企业生产数据或开放公网。

## V5：本地容器部署（独立数据库）

V5 的 Compose 数据库使用自己的 Docker 数据卷，**不会连接、迁移或删除你在 PyCharm 中使用的本机 `agent_db`**。此部署仅限本机演示；项目尚无登录鉴权、HTTPS 或自动备份，不应开放到公网。

先安装并启动 Docker，然后在**已有的**项目根目录 `.env` 末尾增加以下三项；每项使用不同密码：

```env
DOCKER_POSTGRES_PASSWORD=自己生成的URL安全密码
DOCKER_READER_PASSWORD=另一条URL安全密码
DOCKER_SESSION_PASSWORD=第三条URL安全密码
```

可以在 PyCharm Terminal 运行 `python -c 'import secrets; print(secrets.token_urlsafe(24))'` 三次，分别复制结果。不要把密码发给别人或提交到仓库。Compose 只从该 `.env` 读取配置值，不会将文件复制进镜像；应用容器不会收到数据库管理员密码。本机的 `DATABASE_URL` 和 `SESSION_DATABASE_URL` 保持原样，Compose 会在应用容器内改用数据库服务名 `db`。若本机 8001 端口已占用，可在同一 `.env` 中设置 `DOCKER_APP_PORT=其他空闲端口`。

在项目根目录运行：

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

首次启动会创建 Docker 专用数据库、只读业务账号、会话写入账号和会话表。数据库没有自动导入业务数据；要用仓库中的 9 条**样例**销售数据演示，请显式运行：

```bash
sh docker/seed_sample.sh
```

脚本只向 Compose 的数据库创建或补齐 `public.agent_sales_sample`，并仅对该表授予 `agent_reader` 的 `SELECT` 权限；重复运行不会重复插入相同 ID。它不会访问本机 PostgreSQL。打开 <http://127.0.0.1:8001/health> 应看到 `{"status":"ok"}`，再打开 <http://127.0.0.1:8001/docs> 提问：`查询 agent_sales_sample 中 2026 年 1 至 3 月每月销售额，按 quantity * unit_price 计算。` 预期为 21000、17400、11600。若修改了 `DOCKER_APP_PORT`，请把网址中的 8001 换成相应端口。

再用接口创建会话，复制响应中的 `session_id`。重建或重启**应用**后用同一个 ID 查询历史；数据库记录和图表应继续存在。也可在容器内一次查看全部历史：

```bash
docker compose exec api python view_session_history.py 你的session_id
```

日常停止用 `docker compose down`，它不会删除命名数据卷；下次 `docker compose up -d` 可继续使用原数据。**不要执行 `docker compose down -v`，它会删除本项目的数据库和图表数据卷。** 如需在已有 Docker 数据卷上重新应用当前会话表定义和授权，可运行 `sh docker/migrate_session.sh`；PostgreSQL 的首次初始化脚本不会在非空数据卷上自动重跑。数据库账号密码在首次初始化时写入数据库，已有数据卷时不要只改 `.env` 中的三条密码，否则应用会连接失败。

部署故障先看 `docker compose ps` 和 `docker compose logs api db`。`/health` 只表示 API 进程可响应，不代表 DeepSeek 和数据库查询都正常；仍需执行上面的真实分析题。数据库首次初始化脚本已经打包进数据库镜像，避免 macOS 挂载脚本时的执行权限问题。

## V4：LangGraph 执行流程

```text
START → 读取表结构 → 决策节点 ──┬─ SQL 节点 ───────┐
                              ├─ Python 节点 ──┤
                              ├─ 图表节点 ─────┤
                              └─ 报告节点 → END │
                                 ↑              │
                                 └── 观察结果后再决策 ┘
```

图状态保存本次问题、分析计划、轮次、工具结果、失败 SQL、耗时与最终响应；条件边依据模型选择的动作决定下一节点。最多 7 轮、SQL 截断即停止、失败 SQL 不原样重试、缺少必要 Python/图表结果不允许结束等 V3 规则保持不变。会话历史仍由 PostgreSQL 的 `SessionStore` 保存，传入图中仅用于理解追问；本版**没有**另加 LangGraph checkpointer，避免与现有会话存储产生两套事实来源。

在项目根目录安装新增依赖后，可运行 `python -m unittest discover -s tests -v`。`tests/test_graph_v4.py` 覆盖图节点和路由；全套测试不需要真实 DeepSeek 或 PostgreSQL。依赖与图重构完成不代表线上分析一定正确，仍要核对实际 SQL、工具结果和最终结论。

## 已包含

- FastAPI 服务和自动接口文档
- DeepSeek API 客户端
- 最多 7 轮的 Agent Loop
- PostgreSQL 表结构读取与 SQL 查询
- SQL 只读校验、数据库只读事务、5 秒超时、最多返回 100 行；超过上限时立即停止分析并提示缩小查询范围，不基于截断数据生成结论
- Python 分析 SQL 结果：描述统计、相邻行变化率、占比
- Pandas 检查 SQL 结果：缺失值、空字符串、重复行（`profile`）
- Pandas 两期维度归因：独立总额与明细核对、变化额、贡献率（`attribution`）
- 从完整 SQL 结果生成折线图或柱状图，并在响应中返回 `charts` 链接
- SQL 失败后最多再尝试一条修正后的查询；拒绝原样重复失败的 SQL
- 首轮生成分析计划：问题类型、是否需要 SQL、指标口径、时间范围、对比基准、拆解维度和分析步骤
- 完成前检查：数据问题不能零 SQL 查询就结束；计划中的 Python 计算不能在已有可分析数据时跳过
- Pydantic 请求、响应和模型决策校验
- 配置错误与上游 API 错误处理
- 请求日志：记录请求 ID、路径、状态码和耗时，不记录问题正文、SQL 或密钥；响应头包含 `X-Request-ID`
- DeepSeek 决策为空或格式错误时只重试模型调用一次，不重复执行 SQL/Python/图表工具；两次都失败时返回 502
- `GET /health` 健康检查
- `POST /api/v1/analyze` 分析接口
- 可选的 PostgreSQL 会话：首次提问返回 `session_id`，后续携带同一个 ID 追问；只把同一会话最近已完成的问答作为理解指代的参考，不把旧数字当成本轮查询证据
- `GET /api/v1/sessions/{session_id}` 查看最近 20 轮的提问、回答、状态和时间；执行失败或证据不足的轮次保留记录，但不会进入下次提示词

Python Tool 接受结构化计算参数，分析 SQL 返回的行；它不执行模型临时生成的任意 Python 代码。Pandas `profile` 只检查该次查询返回的最多 100 行，不代表整张原始表已完成质量检查。`attribution` 需要两份独立 SQL 结果，核对每期明细合计与总额一致后才输出贡献率。它说明变化由哪些维度贡献，不证明因果原因。页面现支持受限的 CSV/Excel 导入，但没有任意 Python 执行或高级统计建模。

图表只接受最多 30 行未截断 SQL 结果，用随机文件名保存在 `outputs/charts/`。`charts` 给出相对 URL，在本地运行时加上 `http://127.0.0.1:8000` 即可打开。当前图表地址没有登录保护，只适合本机开发测试；不要把含敏感业务数据的服务直接暴露到公网。

`plan` 是模型制定的分析路线，`steps` 是实际执行记录。请同时核对两者；程序目前只能检查是否调用了必要工具，不能自动证明业务口径、SQL 逻辑或因果解释一定正确。概念问题可以直接回答；数据问题先查询再回答。每次请求仍会先读取可访问表结构，因此服务目前仍要求数据库可连接。

## 环境配置

项目直接从根目录的 `.env` 读取：

```env
LLM_API_KEY=你的密钥
LLM_API_ID=deepseek-v4-pro
LLM_BASE_URL=https://api.deepseek.com
DATABASE_URL=postgresql://agent_reader:你的密码@localhost:5432/agent_db
```

SQL 分析默认关闭 DeepSeek 的思考模式，以减少每轮模型调用的等待时间。如果遇到复杂问题需要更深推理，可在 `.env` 中加入 `LLM_THINKING=enabled` 并重启服务。此设置不会更换 `LLM_API_ID` 指定的模型。

请在已有 `.env` 中新增 `DATABASE_URL`，不要把真实密码写进代码或提交到仓库。数据库需提前存在。建议使用专门的只读账号；该账号必须能连接数据库、访问 `public` schema，并对希望 Agent 查询的表拥有 `SELECT` 权限。应用查询也会开启只读事务，但数据库权限仍应独立限制。

如果还没有数据库和只读账号，可以先用 PostgreSQL 管理员账号执行（密码请自行替换）：

```sql
CREATE DATABASE agent_db;
CREATE ROLE agent_reader LOGIN PASSWORD '请换成你自己的密码';
GRANT CONNECT ON DATABASE agent_db TO agent_reader;
```

然后切换连接到 `agent_db`，导入测试数据脚本。连接串中的密码如果含有 `@`、`:`、`/` 等字符，需要按 URL 规则编码。

可以用 [sample_sales.sql](outputs/sample_sales.sql) 在测试数据库中创建 `public.agent_sales_sample` 并插入 9 条数据。导入请使用有建表权限的账号；随后给 Agent 使用的只读账号授予该表的 `SELECT` 权限。例如在 PostgreSQL 管理工具中执行：

```sql
GRANT USAGE ON SCHEMA public TO agent_reader;
GRANT SELECT ON public.agent_sales_sample TO agent_reader;
```

这里的 `agent_reader` 请替换为你实际创建的只读账号名。

### 启用 V3 会话存储

业务查询仍使用 `DATABASE_URL` 中的只读账号。保存对话需要**另一个专用写入账号**，不要把 `agent_reader` 改成可写，也不要把数据库管理员账号填进应用配置。

1. 在 PyCharm Terminal 中用有建表权限的本地账号连接 `agent_db`：`psql -d agent_db`。确认提示符是 `agent_db=#`，再执行 `\i outputs/session_schema.sql`，创建两张会话表。不要在 `psql` 提示符下输入另一条 `psql -d ...` 命令。
2. 仍在 `agent_db=#` 下，为会话创建专用账号并授权（密码自己设置，不要发给我）：

   ```sql
   CREATE ROLE agent_session_writer LOGIN PASSWORD '请替换成你自己的强密码';
   GRANT CONNECT ON DATABASE agent_db TO agent_session_writer;
   GRANT USAGE ON SCHEMA public TO agent_session_writer;
   GRANT SELECT, INSERT, UPDATE ON public.analysis_sessions TO agent_session_writer;
   GRANT SELECT, INSERT, UPDATE ON public.analysis_turns TO agent_session_writer;
   ```

3. 在**已有的**项目 `.env` 中新增一行，并重启服务：

   ```env
   SESSION_DATABASE_URL=postgresql://agent_session_writer:你的密码@localhost:5432/agent_db
   ```

密码含 `@`、`:`、`/` 等字符时，需要先按 URL 规则编码。不要把真实 `.env` 提交到仓库。

未配置 `SESSION_DATABASE_URL` 时，原有单问分析仍可使用，但返回的 `session_id` 是 `null`；带 `session_id` 的追问和历史接口会返回 503。当前版本没有登录鉴权，知道 `session_id` 的人就能读到对应对话；只适合本机开发，不要把服务暴露到公网。

## 在 PyCharm 中运行

1. 确认右下角选择的是项目的 `.venv` 解释器。
2. 打开 PyCharm 的 Terminal，安装依赖：

   ```bash
   python -m pip install -r requirements.txt
   ```

3. 启动服务：

   ```bash
   python -m uvicorn app.main:app --reload
   ```

4. 打开 <http://127.0.0.1:8000/docs>。

## 手动测试

先调用 `GET /health`，应返回：

```json
{"status":"ok"}
```

再调用 `POST /api/v1/analyze`：

```json
{
  "question": "请查询 agent_sales_sample：2026 年 1 到 3 月每个月的销售额分别是多少？销售额按 quantity * unit_price 计算。"
}
```

预期的月销售额：1 月 21000，2 月 17400，3 月 11600。`steps` 会显示 Agent 实际生成的 SQL 和返回的查询结果；`rounds` 是执行轮数，`finished` 表示是否生成最终回答。

会话启用后，上面的请求会额外返回 `session_id`。复制它，再请求一次 `POST /api/v1/analyze`：

```json
{
  "question": "那其中哪个月最高？请重新查询数据库，不要沿用上次的数字。",
  "session_id": "把第一次响应中的 session_id 粘贴到这里"
}
```

第二次响应应返回同一个 `session_id`，并在 `steps` 中出现本轮新执行的 SQL。再调用 `GET /api/v1/sessions/{session_id}`，应看到两条 `completed` 记录。重启服务后用同一个 ID 查询历史，记录仍应存在。开启另一段新会话时不传 `session_id`，其历史不会混入第一段会话。

要在 PyCharm Terminal 中一次查看某个会话的**全部**记录（包含失败和未完成轮次，而不只是接口默认的最近 20 轮），在项目根目录执行：

```bash
python view_session_history.py 这里替换成真实的session_id
```

命令从现有 `.env` 读取 `SESSION_DATABASE_URL`，不会打印数据库密码。输出包含每轮的问题、回答、状态和时间；会话内容可能含敏感数据，不要把完整输出公开。

会话边界的自动化验收用例在 `tests/test_sessions.py`：两个不同会话的并发请求必须各自只看到自己的历史；模型报错与超过 100 行导致的未完成轮次要留存状态，但不得进入后续提问的上下文。运行 `python -m unittest tests.test_sessions -v`。这些用例使用模拟存储和模型，不代替真实 PostgreSQL 与 DeepSeek 的端到端验证。

响应中的 `timings_ms` 是耗时（毫秒）：`schema` 为读取表结构，`llm_round_1`、`llm_round_2` 为每次调用模型，`sql_round_1` 为执行 SQL，`total` 为整次分析。用这些数字判断等待主要发生在哪一步。

Python Tool 的调用会在 `steps` 中显示 `action: "python"`，耗时显示为 `python_round_N`。参见 [V1.2 手动测试指南](outputs/python_tool_test_guide.md)。

V2 的 Pandas 检查和图表测试见 [V2 手动测试指南](outputs/v2_test_guide.md)。

V2.1 的两期维度归因测试见 [V2.1 手动测试指南](outputs/v2_1_attribution_test_guide.md)。

想测试新加的“判断是否查询、按数据分析、谨慎下结论”流程，请看 [分析流程手动测试指南](outputs/analysis_flow_test_guide.md)。

截断保护已在真实接口上用只读查询复测：把 `public.agent_sales_sample` 与 `generate_series(1,17)` 交叉连接，查询超过 100 行时，`steps` 中显示 `row_count: 100`、`truncated: true`，响应 `finished: false`，并提示缩小查询范围。这个测试不向数据库插入数据。自动化边界测试可运行 `python -m unittest discover -s tests -v`。

另一个问题：`agent_sales_sample 中，2026 年第一季度哪个商品类别销售额最高？`。按测试数据，答案应是电脑，销售额 24000。
