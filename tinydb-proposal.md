# Proposal: tinydb

## Why

需要一个轻量级嵌入式关系型数据库，既能通过造轮子深入理解数据库核心原理（存储引擎、SQL 解析、查询优化、索引、事务），又能作为可嵌入的 Python 库在实际项目中使用。现有方案中 SQLite 过于庞大复杂不适合学习拆解，而 Python 生态中缺乏一个简洁、可读、可教学的嵌入式关系型数据库实现。

## What Changes

从零构建一个 Python 嵌入式关系型数据库 `tinydb`，提供纯 SQL 字符串接口，支持 DDL/DML、条件查询、排序分页、聚合、列约束、B-tree 索引和 ACID 事务，数据以单文件形式持久化存储，并附带 CLI/REPL 交互界面。

## Scope

### In

- 纯 SQL 字符串接口（`db.execute("SELECT ...")`）
- DDL：`CREATE TABLE`、`DROP TABLE`
- DML：`INSERT`、`SELECT`、`UPDATE`、`DELETE`
- WHERE 条件过滤（AND/OR）
- ORDER BY、LIMIT、OFFSET
- 列约束：PRIMARY KEY、NOT NULL、UNIQUE
- 聚合函数：COUNT、SUM、AVG + GROUP BY
- B-tree 索引
- 数据类型系统：INT、FLOAT、TEXT、BOOL
- ACID 事务：BEGIN、COMMIT、ROLLBACK
- 单文件磁盘持久化
- CLI/REPL 交互界面

### Out

- 多表 JOIN 查询
- 并发控制（多线程/多进程安全）
- ALTER TABLE、视图、触发器、外键
- 网络/客户端-服务器模式

## Impact

- 新增 Python 包 `tinydb`，零外部依赖
- 单一 `.db` 文件作为数据存储格式
- 用户通过 Python API 或 CLI 与数据库交互

## Capabilities

| 能力 | 描述 |
|------|------|
| SQL 解析 | 将 SQL 文本解析为 AST 并执行 |
| 存储引擎 | 页式存储管理，单文件读写，缓冲池 |
| 查询执行 | 全表扫描 + 索引加速的查询计划与执行 |
| B-tree 索引 | 基于 B-tree 的索引结构，加速等值和范围查询 |
| 事务管理 | 基于 WAL 或影子分页的 ACID 事务 |
| 类型系统 | INT/FLOAT/TEXT/BOOL 的类型检查与存储 |
| CLI 界面 | 交互式 REPL，支持 SQL 输入和结果展示 |