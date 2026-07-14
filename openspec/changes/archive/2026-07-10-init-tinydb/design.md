# Design: tinydb

## Context

`tinydb` 是一个从零实现的 Python 嵌入式关系型数据库，零外部依赖，单文件持久化。其目标用户分两类：(1) 想深入理解数据库内部原理的学习者（通过可读的造轮子过程）；(2) 想在 Python 项目里嵌入一个轻量 SQL 数据库的开发者（不需要 SQLite 那么重，也不需要服务器）。本 change 是项目的首次落地，需要从空白仓库搭建出一套完整、可用、可测试的最小骨架，再在此之上逐步迭代。

约束条件：
- 纯 Python 标准库实现，零外部依赖（不引入 ply、sqlparse、readline 等）
- 教学优先：实现必须可读，能清晰反映数据库各子系统的职责与边界
- 单文件 `.db` 持久化，支持 ACID 事务
- 范围明确排除：JOIN、并发、ALTER TABLE、视图、触发器、外键、网络协议

## Goals / Non-Goals

**Goals:**
- 提供可嵌入的 Python API：`tinydb.Database(path).execute(sql)` 返回 `ResultSet`
- 提供 CLI：`python -m tinydb <dbfile>` 启动 REPL
- 实现一套最小但完整的 SQL 子集：DDL（CREATE/DROP TABLE）、DML（INSERT/SELECT/UPDATE/DELETE）、WHERE/ORDER BY/LIMIT/OFFSET、聚合（COUNT/SUM/AVG + GROUP BY）
- 实现列约束：PRIMARY KEY、NOT NULL、UNIQUE
- 实现 B-tree 索引，支持等值与范围查询
- 实现 ACID 事务（BEGIN/COMMIT/ROLLBACK）
- 实现基本类型系统：INT、FLOAT、TEXT、BOOL
- 关键模块都有单元测试，覆盖率 ≥ 80%

**Non-Goals:**
- 多表 JOIN、子查询、CTE
- 多线程/多进程并发控制
- ALTER TABLE、视图、触发器、外键约束
- 网络/客户端-服务器模式
- 查询优化器（仅实现规则式优化，例如谓词下推、索引选择）
- 字符集/排序规则（仅默认二进制比较）
- 用户/权限系统

## Decisions

### 1. 模块布局：`tinydb/<subsystem>/`，每个子系统一个子包

```
tinydb/
  __init__.py        # public API: Database, ResultSet, Row
  __main__.py        # CLI 入口
  cli/               # REPL
  parser/            # lexer + parser → AST
  ast.py             # AST 节点定义
  types/             # 类型系统（值、序列化、校验）
  storage/           # 页管理、缓冲池、磁盘 I/O
  catalog/           # 表/列/索引元数据
  executor/          # 查询计划 + 执行
  index/             # B-tree 实现
  txn/               # 事务管理（WAL、锁、提交/回滚）
  errors.py          # 统一异常类型
```

**理由**：按数据库经典分层（解析 → 计划 → 执行 → 存储）切分子系统，子包之间通过明确定义的数据结构（AST、Record、Page、Tuple）解耦，便于学习者按子系统阅读，也便于单测。

### 2. 存储格式：定长页 + 文件头 + 空闲页链表

- 页面大小固定 4 KiB
- 文件结构：`[FileHeader(1 page)] [Page 0] [Page 1] ...`
- `FileHeader` 记录：magic、版本、root catalog page id、first free page id、WAL 文件路径（若启用 WAL）
- 每页：`[PageHeader: page_id, page_type, free_space, next/prev pointers] [Slots...]`
- 元数据页（catalog page）持久化所有表、列、索引定义
- 缓冲池：默认 64 页，使用 LRU 替换，脏页延迟写回

**理由**：定长页是教科书级存储方案，实现简单、易于教学。4 KiB 与操作系统页对齐，未来扩展到 mmap 也方便。

**替代方案**：
- 变长页（slotted page 已部分支持）：复杂度高、收益小，本次不采用
- mmap：跨平台行为差异大、不利于教学

### 3. 事务实现：WAL（Write-Ahead Log）+ 单写者

- 启用 WAL 后，所有数据页修改前先写日志
- 日志格式：追加式，每条记录包含 `txn_id / prev_lsn / type / page_id / before_image / after_image`
- 提交时刷盘一条 `COMMIT` 记录
- 检查点（checkpoint）：简单全量 checkpoint，定期执行
- 单写者模型：当前进程内事务串行执行，跨进程用操作系统文件锁（`fcntl.flock`）做粗粒度互斥

**理由**：WAL 比影子分页（shadow paging）更主流，读不阻塞写，对教学场景也更易讲清楚 redo/undo 语义。

**替代方案**：
- 影子分页：实现更简单但回滚/恢复路径不如 WAL 直观
- MVCC：教学成本过高，超出本期范围

### 4. 解析器：手写递归下降 + 正则分词

- 词法：`re` 模块按 token 切片，关键字大小写不敏感
- 语法：递归下降，每个语句一个 parse 函数
- 输出：`ast.*` 节点对象（`SelectStmt`、`InsertStmt`、`CreateTableStmt` 等）
- 不支持嵌套 SELECT、不支持子查询

**理由**：手写解析器 ~300 行就能覆盖本项目所需 SQL 子集，比引入手写 parser generator（ply/lark）教学价值更高，且零依赖。

### 5. B-tree：经典 B+ tree，叶节点链表

- 每个 B+ tree 节点 = 一页
- 内部节点：`[keys..., child_page_ids...]`
- 叶子节点：`[keys..., (value_page_id, slot_id)...]` + `next_leaf` 指针
- 阶数 `m`：内部节点最多 `m` 个 child，最少 `⌈m/2⌉`；叶子同理
- 分裂/合并：插入触发分裂、删除触发再平衡；不实现前缀压缩

**理由**：B+ tree 是关系数据库的事实标准，叶子链表让范围扫描只需单向遍历，避免回溯到上层。

### 6. 索引模型：单列二级索引，PK 自动建索引

- 每张表有一个隐式主键索引（如果声明了 PRIMARY KEY）
- 可在任意列上声明 UNIQUE，自动建索引
- 索引条目：`key → (page_id, slot_id)`
- 执行器在 WHERE 等值/范围命中索引时切换到 IndexScan；否则 SeqScan

### 7. 类型系统：Tagged Value，定长优先

- 表示：`Value(tag, payload)`，`tag ∈ {INT, FLOAT, TEXT, BOOL, NULL}`
- 序列化：
  - INT → int64（8 字节定长）
  - FLOAT → float64（8 字节定长）
  - BOOL → 1 字节
  - TEXT → `[len:uint32][utf8 bytes]` 变长
- 校验：执行器在 INSERT/UPDATE 时按列定义强校验，不合法抛 `TypeError`/`ConstraintError`

**理由**：定长优先让定长字段可直接按 slot 计算偏移，简化页面布局。

### 8. 执行模型：Volcano / Iterator 风格

- 每个算子实现 `open() / next() / close()`
- 算子种类：`SeqScan`、`IndexScan`、`Filter`、`Project`、`Sort`、`Limit`、`Aggregate`、`Insert`、`Update`、`Delete`、`CreateTable`、`DropTable`
- EXPLAIN 不在 v1 范围，但保留 `print_plan` 调试方法

**理由**：Iterator 模型是数据库执行器的经典范式，便于扩展（加 JOIN 时只需新增算子）。

### 9. CLI：基于 `code` 模块的 REPL

- 入口：`python -m tinydb <dbfile>`
- REPL：用 `code.InteractiveConsole` 自定义提示符，支持多行语句以 `;` 结尾
- 输出：`SELECT` 结果以表格打印，其他语句打印影响行数
- 不实现历史/补全（首版可读性优先）

### 10. 错误处理：分层异常

- `tinydb.errors.TinyDBError` 基类
- `ParseError` / `ConstraintError` / `TypeError_(tinydb专属)` / `StorageError` / `TransactionError`
- API 抛异常；CLI 捕获后打印用户友好消息

## Risks / Trade-offs

- **[单写者事务模型下，并发退化]** → 文档明示不并发；为未来 MVCC 预留 `txn/` 子包扩展点。
- **[手写解析器遇到复杂 SQL 边界情况易崩]** → 单元测试覆盖每种语句类型；非法输入必须抛 `ParseError` 而不是 panic。
- **[B+ tree 实现 bug 导致页面腐败]** → 关键不变量（节点 keys 有序、叶子链表闭合、root 唯一）写在 `index/btree.py` 顶部作为契约；针对 split/merge 写专门的 invariant 测试。
- **[WAL checkpoint 期间写阻塞]** → 文档说明是简化实现；提供开关可关闭 WAL（仅用于学习和测试）。
- **[教学代码 vs 工程代码的张力]** → 遵循"教学优先"，但每个子系统仍保持小而内聚（≤ 400 行），关键路径加注释解释 WHY。
- **[零依赖导致部分平台特性缺失]** → 不使用 `mmap`、`fcntl` 之外的平台特定 API；Windows 下 flock 退化为 `None`（仅支持单进程）。

## Open Questions

- 是否在 v1 支持 `EXPLAIN` 语句？倾向不做，预留 `print_plan()` API。
- 索引是否在 v1 支持复合索引？倾向不支持（单列已能演示原理）。
- REPL 是否支持 `.tables` / `.schema` 元命令？倾向支持（教学体验好），作为 CLI 子任务。