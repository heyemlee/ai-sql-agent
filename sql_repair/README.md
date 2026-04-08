# sql_repair

Log-driven SQL 修复回路。

## 流程

```text
执行失败
  ↓
ExecutionLogParser   解析错误日志 → 结构化错误（类型 + 相关对象）
  ↓
SqlClauseLocalizer   按顶层关键字切分SQL → 定位疑似出错子句
  ↓
重生成出错子句
  - 配置模型凭证：Coder 模型局部重写（只改出错子句）
  - 离线：启发式近名替换（difflib 就近匹配 Schema 中的表/字段名）
  ↓
重新执行，失败则继续，最多 max_attempts 次
```

## 覆盖的错误类型

`no_such_table` / `no_such_column` / `ambiguous_column` / `syntax_error` /
`no_such_function` / `timeout` / `misuse_aggregate`（SQLite 错误族，可按方言扩展）。

## 接入方式

Pipeline 默认启用（`PipelineConfig(enable_repair=True, max_repair_attempts=2)`），
修复过程记录在 `StepExecutionLog.repair_attempts`，
修复后成功执行的 SQL 记录在 `StepExecutionLog.final_sql`。

## 测试

```bash
python -m unittest tests.test_repair -v
```
