# 手动 SQL 示例

首次体验推荐通过 UI 上传 [sales_demo.csv](../data/sales_demo.csv)。本目录保留开发阶段的 SQL 数据，与当前 UI 上传流程相互独立。

- [sample_sales.sql](sample_sales.sql)：9 行早期销售示例；在项目根目录执行 `sh docker/seed_sample.sh` 可导入已运行的 Compose 数据库并授权只读账号。
- [advanced_test_data.sql](advanced_test_data.sql)：早期多表与业务口径测试数据。当前分析流程以单表可加总指标为主；保留数据不代表当前支持跨表分析。历史步骤见 [测试指南](../../docs/archive/advanced_test_guide.md)。

SQL 脚本可能重建同名测试表，运行前查看内容，仅在测试数据库执行。会话 schema 为部署依赖，位于 `docker/db/session_schema.sql`，由镜像初始化或 `docker/migrate_session.sh` 应用。
