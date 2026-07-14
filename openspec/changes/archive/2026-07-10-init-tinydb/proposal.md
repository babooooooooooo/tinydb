## Why

需要一个轻量级嵌入式关系型数据库，既能通过造轮子深入理解数据库核心原理（存储引擎、SQL 解析、查询优化、索引、事务），又能作为可嵌入的 Python 库在实际项目中使用。SQLite 过于庞大复杂不适合学习拆解，而 Python 生态中缺少一个简洁、可读、可教学的嵌入式关系型数据库实现。

## What Changes

- 新增 Python 包 `tinydb`：从零实现的嵌入式关系型数据库，零外部依赖
- 提供纯 SQL 字符串接口 `db.execute("SELECT ...")`
- 新增存储引擎：页式存储 + 单文件 `.db` 持久化 + 缓冲池
- 新增 SQL 解析器：词法 + 语法分析，输出 AST
- 新增查询执行器：DDL（CREATE/DROP TABLE）、DML（INSERT/SELECT/UPDATE/DELETE）、WHERE/ORDER BY/LIMIT/OFFSET、聚合（COUNT/SUM/AVG + GROUP BY）
- 新增 B-tree 索引：加速等值与范围查询
- 新增列约束：PRIMARY KEY / NOT NULL / UNIQUE
- 新增类型系统：INT / FLOAT / TEXT / BOOL
- 新增 ACID 事务：BEGIN / COMMIT / ROLLBACK，基于 WAL 或影子分页
- 新增 CLI / REPL 交互界面

## Capabilities

### New Capabilities

- `sql-parser`: 将 SQL 文本词法分析、语法分析为 AST
- `type-system`: INT / FLOAT / TEXT / BOOL 的类型检查、序列化与存储
- `storage-engine`: 页式存储管理、单文件 `.db` 持久化、缓冲池
- `query-execution`: 全表扫描与索引加速的查询计划与执行（DDL / DML / WHERE / ORDER BY / LIMIT / OFFSET / 聚合 / 列约束校验）
- `btree-index`: 基于 B-tree 的索引结构，支撑等值与范围查询
- `transaction-management`: 基于 WAL 或影子分页的 ACID 事务（BEGIN / COMMIT / ROLLBACK）
- `cli-interface`: 交互式 REPL，支持 SQL 输入与结果展示

### Modified Capabilities

无（项目初始提案，无既有能力被修改）。

## Impact

- 新增 Python 包 `tinydb`，代码位于项目根 `tinydb/` 目录
- 零外部依赖（仅用 Python 标准库）
- 数据存储格式：单一 `.db` 文件
- 用户交互入口：Python API（`tinydb.Database`）与 CLI（`python -m tinydb <file.db>`）
- 显式不在范围内：多表 JOIN、并发控制、ALTER TABLE / 视图 / 触发器 / 外键、网络客户端-服务器模式