# 授权模型：行范围与 impersonation（设计）

> 对应审查 P0-2 的两半。2026-09-04 第一轮关掉的是**可复现的越权**；
> 这一份处理的是**策略模型本身表达不出来的东西**。
>
> 先出设计再动手，因为它牵动 `src/ai_sql_agent/auth/objects.py`、`enforcement.py` 与
> key store 的形状，而且**默认值选错就是把 `full_access` 的老毛病换个维度复发**。

## 0. 今天的模型缺什么

| 维度 | 今天 | 缺口 |
| --- | --- | --- |
| 库 / 表 | `allow_tables` / `deny_tables` glob | —— |
| 列 | `deny_columns` glob | —— |
| **行** | **没有** | 「不同客户只能看自己的订单」表达不出来 |
| **最终用户身份** | impersonation 换 `user_id`，**权限仍是服务身份的** | alice 和 bob 权限相同 |

第三行的后果：RLS 的过滤条件只能由运维手写，推不出来，所以
`src/ai_sql_agent/ops/least_privilege.py` 生成不了它。
第四行的后果：一把 `can_impersonate` 的服务 Key 替所有员工发请求，
每个员工拿到的都是**服务身份的全部权限**。

## 1. 行范围：`row_filters`

```json
"policies": {
  "estimating": {
    "allow_tables": ["erp_db.*"],
    "row_filters": [
      {"table": "erp_db.job",   "predicate": "owner_id = current_setting('app.user_id', true)"},
      {"table": "erp_db.*",     "predicate": "*"}
    ]
  }
}
```

- `predicate` 是一段 **SQL 布尔表达式**，落到 PostgreSQL 的 RLS 上；
- `predicate: "*"` 是**显式声明「这张表不做行隔离」**；
- 规则按声明顺序取**第一条匹配**（上面的例子：`job` 按 owner 过滤，其余全放）。

### 1.1 最终用户身份怎么进到 SQL 里

`current_setting('app.user_id', true)` 的值由**执行器**在事务里设：

```
BEGIN → SET TRANSACTION READ ONLY → SET LOCAL app.user_id = <principal.user_id> → 查询
```

`SET LOCAL` 是事务级的，随 rollback 一起还原，所以连接池复用不会串身份。
这一步让 RLS 在**通过 agent 发起的查询**上真的生效，而不是只在有人手工连库时生效。

没有绑定授权作用域时设成空串：RLS 的 `USING (owner_id = current_setting(...))`
对空串不匹配任何行 —— fail closed，与执行器那道关卡方向一致。

### 1.2 默认值：**不是「放行所有行」，也不是「拒绝所有行」**

这是本设计最要紧的一处，两个显而易见的选项都不能选：

| 选项 | 后果 |
| --- | --- |
| 没配 = 放行所有行 | **把 `full_access` 的老毛病换个维度复发**：漏配一张表就是全表可见，且无症状 |
| 没配 = 拒绝所有行 | 现有部署一条策略都没有 `row_filters`，翻上去等于**服务立刻不可用** |

所以取第三种：**运行时按「放行」，配置校验时按「必须显式声明」**。

- 运行时：没有匹配的 `row_filters` 条目 → 不加行过滤（行为与今天一致，不破坏现网）；
- **生产模式启动检查**：策略里任何一张 `allow_tables` 覆盖到的表，
  如果没有任何 `row_filters` 条目匹配它（包括显式的 `"*"`），**拒绝启动**。

于是「这张表不做行隔离」这句话必须是**有人写下来的**，而不是默认值兜出来的。
这与第一轮 `roles=()` 的处理是同一条原则：*显式的宽松可以接受，默认的宽松不行。*

开发模式下只告警。

## 2. impersonation：取交集，不是二选一

```json
"users": {
  "alice": {"roles": ["estimating"]},
  "bob":   {"roles": ["workshop"]}
}
```

`ApiKeyStore.resolve(key, requested_user_id)` 在 impersonation 时：

```
最终权限 = 服务身份的策略  ∩  最终用户的策略
```

**取交集不是二选一。** 服务 Key 的策略是「这个应用最多能碰什么」，
最终用户的策略是「这个人最多能碰什么」，两者都是上界，都要守。

### 2.1 交集怎么算

**不做 glob 代数。** `intersect(["erp_db.*"], ["erp_db.job"])` 要算出一个等价的
glob 列表，既复杂又容易错。改成一个组合对象：

```python
IntersectionPolicy(service=..., user=...)
    allows_table(...)      → 两边都放行才放行
    allows_column(...)     → 同上
    restricts_columns(...) → 任一边有限制就算有限制（取更严的）
    row_predicate(...)     → 两个谓词 AND 起来
```

它满足 `SchemaAccessFilter` 协议加上授权关卡用的那几个扩展方法，
所以对 `enforcement.py` 完全透明。

### 2.2 迁移路径（现有 Key 怎么办）

| 配置形态 | 行为 | 生产模式 |
| --- | --- | --- |
| 没有 `users` 段 | 保持今天的行为（用服务身份的策略） | **拒绝启动**，如果存在 `can_impersonate` 的 Key |
| 有 `users` 段，命中 | 取交集 | 正常 |
| 有 `users` 段，未命中 | **拒绝**（这个人的权限没声明过，推不出来） | 正常 |

同样是「显式的宽松可以接受，默认的宽松不行」：
不配 `users` 的老部署照常跑，但**上生产之前必须把这件事写下来**。

## 3. 规则里的名字怎么比：拒绝从宽，放行从严

规则是 `库.表` / `库.表.列` 的 glob。名字的大小写怎么算，目录（`/catalog`）、检索裁剪
和 SQL 授权关卡用的是**同一条规则**（`src/ai_sql_agent/auth/identifiers.py`）——
2026-10-04 之前它们各比各的：关卡把 SQL 里的名字全转成小写、策略按原样比，
于是 `deny_tables: ["shop_db.Orders"]` 在关卡上永远对不上，被拒的表照样查得出来。

规则跟着数据库自己的语义走：

| 数据库 | 名字怎么算同一个 |
| --- | --- |
| SQLite | 不区分大小写，加不加引号都一样（只对 ASCII 字母） |
| PostgreSQL | 没加引号的折成小写；加了引号的区分大小写——`"Orders"` 与 `orders` 是两张表 |

| 规则 | SQLite | PostgreSQL |
| --- | --- | --- |
| `deny_tables` / `deny_columns` / `row_filters` | 不区分大小写 | **不区分大小写**：`Orders` 这条会把 `"Orders"` 和 `orders` 一起拒掉 |
| `allow_tables` | 不区分大小写 | **必须与存储名大小写一致**：`orders` 放行 `orders`（SQL 里不加引号写成 `Orders` 的也是它），放不到 `"Orders"` 头上 |

为什么两个方向不对称：多拒一张同名的表，代价是有人来问一句；少拒一张，是数据已经
出去了。放行规则写错大小写，后果是「这张表查不了」——看得见，有人会来问。
所以没有做成「全部转小写再比」：那样一条只放行 `orders` 的规则会把 `"Orders"` 也放出去。

写规则时照着数据目录或 `information_schema` 里显示的名字抄即可。
PostgreSQL 分支只有判定逻辑的单元测试，没有在真实库上跑过带引号的混合大小写表。

## 4. 不在这一轮的

- **glob 代数**：交集用组合对象绕开了。要把交集落回一份可读的 `DataPolicy`
  （比如给运维看「这个人最终能查什么」），需要真的做 glob 求交，是独立一件事。
- **SQLite 的行隔离**：SQLite 没有 RLS。演示库因此只有应用层的表/列隔离，
  行隔离只在 PostgreSQL 上成立。**不要**用「在 SQL 里自动加 WHERE」来补——
  那等于在生成的 SQL 上做字符串手术，绕过它的方法比它本身多。
- **`users` 段的来源**：现在是 API Key 配置文件里的一段。接 LDAP / SSO
  取用户角色是另一件事，接口留在 `ApiKeyStore.user_roles()` 这一个点上。
