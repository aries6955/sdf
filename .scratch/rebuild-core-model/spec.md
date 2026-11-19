# 重头梳理:现状 → 目标(已实施)

> 依据:`GLOSSARY.md` 与 `docs/adr/0012..0023`。
> **状态:已实施**。`uv run pytest -q` 56 passed;服务在 8000 端口实测通过端到端流程。

## 一、数据模型

### 现状的问题

| 现状 | 问题 |
| --- | --- |
| `meters.household_id` 与 `takeovers` 双份归属 | 实测分叉:`billing.py` 走租期,`/readings/bill` 走字段,甲的表交给乙后应收记到乙 |
| `takeovers` 只有起点,无终点 | 退租后继续抄表,用量仍算给已退租的租户(实测) |
| `meters.base_value/base_date` + `readings.is_initial` | 同一件事存两份;换表会插出第二条 `is_initial`(实测) |
| `readings.department_price` | ADR-0009 已把部门单价移出抄表,字段残留,`reading_edit` 还在写 |
| `readings.billed` | 抄表即成账后无意义 |
| `adjustments` | 独立第三套钱,不进应收,三处口径不一;已删除 |
| `receipts.receivable_id` | 收款必须指定账单,不符现实;改为记在租户名下 |
| `meters.active` | 表只有在用/已换下,由换表历史推出 |
| `_migrate` / `_backfill_takeovers` | 为不存在的旧库维护补丁 |
| 抄表页自算 `prev_of` / `total_amount` | 第三份用量算法,筛选后金额变 0 |

### 目标 schema(表名沿用,语义重写)

```
households(id, name, note)

meters(id, name, meter_type, level, main_meter_id, phase,
       base_value, base_date, installed_on, retired_on, note)
  -- 无 household_id、无 active;retired_on 由换表写入
  -- 总表不填 base_value/base_date

lines(id, household_id, meter_type, label, note)
  -- 计量线:租户名下一路连续计量;同路可多条
  -- 一条 line 由若干 meter 前后接续而成
line_meters(line_id, meter_id, from_date, to_date)   -- 或退化:meter.line_id + 换表历史

tenancies(id, line_id, household_id, move_in_date, move_out_date,
          move_in_value, move_out_value, status)
  -- 归属唯一真源;open = move_out_date is null
  -- 同一 line 同时只有一条 open(部分唯一索引)

readings(id, line_id, meter_id, reading_date, value, r1, r2, r3,
         tenant_price, kind, note)
  -- kind: 'move_in' | 'periodic' | 'move_out' | 'switch_out'
  -- 读数挂 line,另记取自哪块 meter

receivables(id, tenancy_id, line_id, period_start, period_end,
            usage_amount, unit_price, amount, created_at)
  -- 一个账期一条;不再有 billed 标志

receipts(id, household_id, entry_date, amount, kind, note)
  -- kind: 'receipt' | 'refund';记在租户名下,不指定账单

payments(id, meter_id, payment_date, reading_value, unit_price, amount, note)

meter_switches(id, old_meter_id, new_meter_id, switch_date,
               old_final_value, new_base_value, note)
```

删除:`takeovers`(被 `tenancies` 取代)、`adjustments`。

## 二、代码

| 文件 | 动作 |
| --- | --- |
| `app/db.py` | 重写 `SCHEMA`;删 `_migrate`、`_backfill_takeovers`;加 `current_tenancy(conn, line_id)`、`open_tenancies(conn)` |
| `app/billing.py` | 重写:按线取账期、换表分段求和、空档期用量、毛差;对外只暴露 `periods_for_line`、`all_periods`、`vacancy_usage` |
| `app/ledger.py` | 重写:应收按线、收款按租户、先欠先冲、预收余额、退款 |
| `app/main.py` | 拆分:路由层只做校验与跳转,业务调用上面两个模块;见下方路由清单 |
| `app/sample.py` | 按新模型重写示例数据 |
| `app/views.py` | `NAV` 增"表管理"下的换表入口;`money` 等全局函数不变 |
| 模板 | `base/households/meters/readings/payments/statistics/home` 改;删 `adjustments.html`、`switches.html`;`household_ledger.html` 重做 |
| `tests/` | 按新模型重写;新增锁定、冲抵、换表跨期、空档期、鉴权五组 |

### 路由清单(现状 36 → 目标 24)

**保留改名**
- `GET /` 首页:改数据源(去冲正、加毛差入口)
- `GET /readings` 抄表:按"上次抄表日期"正序排;只列分表与计量线;按线展示
- `POST /readings/add` 抄表:插中间、成账、锁校验;一次只处理一条 line
- `POST /readings/edit` 改读数:未收款才可改
- `POST /readings/delete` 改名为"撤回读数":未收款才可删,删后前后两期重算
- `GET /payments` `POST /payments/add` 缴费:去掉读数差推算与校验,金额手填
- `GET /statistics` 统计:加毛差三行、空档期用量行
- `GET /statistics/pdf` 不变
- `GET /households` 租户:加"入住""退租"入口,去删除
- `GET /households/{id}/ledger` 台账:按线分行、先欠先冲明细、预收余额、退款
- `POST /households/add` 入住:一屏录多表读数
- `POST /households/checkout` 退租:录终读数、结算、关租期
- `GET /meters` 表管理:列表带"换表"行内动作
- `POST /meters/add` 建表:总表不填基数
- `POST /meters/edit` 改表:不能改归属
- `GET/POST /login` `GET /logout` 不变
- `GET /init` `POST /init/sample` `POST /init/clear` `POST /init/property` `POST /init/password` 不变

**新增**
- `POST /receipts/add` 收款与退款(记租户名下,`kind=receipt|refund`)
  —— 退款不设独立路由,与收款同一条路径、同一张表,见 ADR-0018
- `POST /receipts/delete` 删收款(解锁账单)

**删除**
- `POST /readings/bill`(抄表即成账)
- `POST /meters/delete` `POST /households/delete`(改退租/换下)
- `GET /adjustments` `POST /adjustments/add` `POST /adjustments/delete`
- `GET /switches`(并入 `POST /switches/add`,入口在表管理行内)
- `POST /meters/base`(基数只在建表与入住时录)
- `GET/POST /households/{id}/bind` 类残留(现无,确认)

### 偷渡路径一并堵上

- 全部 POST 走统一登录中间件(ADR-0023)。
- `households/add` 的"先插再删"回滚改为事务内校验,失败整笔回滚。
- 有应收时删表/删租户的 500 随删除路由消失。

## 三、顺序

1. `GLOSSARY.md` + ADR(已完成,待你确认)
2. `db.py` schema + 辅助函数;`sample.py`
3. `billing.py` / `ledger.py` 重写 + 单元测试
4. `main.py` 路由按上表改造
5. 模板
6. 全量测试 `uv run pytest -q` 通过;重启 `--reload` 服务自测

## 四、实施结果与偏差

已按顺序完成 1-6 步。与初稿的偏差:

1. **`lines` 表就是租期**,没有另建 `tenancies`:一条 `lines` 行即一段租期
   (`move_in_date` / `move_out_date`),不再需要第二个实体。
2. **没有 `line_meters` 连接表**:`meters.line_id` 指向当前服务的线,
   换表时把 `line_id` 从旧表移到新表、旧表写 `retired_on`;历史读数自带 `meter_id`,天然留痕。
3. **归属唯一性用部分唯一索引**兜底:`meters(line_id) WHERE line_id IS NOT NULL` ——
   一块表同一时间只能服务一条线。
4. **账期不单独存表**:账期由 `readings` 的边界行推导,`receivables` 只存结果;
   任何读数变动都走 `recompute_line()` 整体重算,没有增删账期的分支逻辑。
5. **补录只落在当前表上**:换下的旧表不再接受新读数(见 ADR-0021 的约束)。
6. **退款并入 `/receipts/add`**,未按初稿建 `/refunds/add`(ADR-0018 已认定两者同构)。
7. **`meters.base_date` 并入 `installed_on`**:总表与分表都只有一个"装表日期",
   分表的 `base_value` 即那一天的表码。
8. **分类型统计的口径**:`type=water|electricity` 时,缴费合计也只取该类型,
   毛差始终与应收同口径。

## 五、验收

- `uv run pytest -q` → 60 passed
- 端到端实测(临时库):建表 → 入住(水表 + 三相电表)→ 抄表成账 → 换表(用量两段相加)
  → 收款(超收转余额)→ 退租(含三相分相读数)→ 同一块表换租户(空档期 30 归房东)
  → 已收款读数被拒改 → 统计页毛差与空档期 → PDF 导出。


## 六、code-review 修正(1950 行之后的追改)

首次审查(`/code-review a922a36`)提了 11 条,全部修掉:

| 轴 | 问题 | 修法 |
| --- | --- | --- |
| Spec | 补录可绕过收款锁,账期被一分为二并重排冲抵 | `ledger.touched_by_reading()` 统一判据;`/readings/add` 增检查 |
| Spec | `type=water` 时毛差用"水应收 − 水电总缴费" | 缴费按类型拆开,毛差与应收同口径 |
| Spec | `/refunds/add` 与 spec 不符 | 保留合并实现,改 spec |
| Spec | 抄表页"换表后必须选新表"提示与机制重复 | 保留(仍有用:提示为何下拉会变) |
| Spec | `.scratch/`、备份文件入 gitignore 未记录 | 已记入本文第六节 |
| Standards | 三处重复的"该账期是否已收款" | 抽 `ledger.paid_periods()` + `touched_by_reading()` |
| Standards | `statistics` 的 `meter_id` 名实不符 | 改名 `line_id`,与术语表一致 |
| Standards | 零引用的 `line_for_meter` / `receivable_row` / `h` / `add_month` / `money` | 删除 |
| Standards | `br1/br2/br3` 等死变量 | 删除 |
| Standards | 未使用的 `period_ending_at` import、`html` import | 清理 |
| Standards | `base_date` 与 `installed_on` 语义重叠 | 合并为 `installed_on` |
| DESIGN | 4 处 `<label>` 缺 `for` | 三相组改 `role="group"` + `aria-labelledby`;其余补 `for`/`id` |

新增回归测试:`test_backfill_blocked_inside_paid_period`、`test_edit_blocked_inside_paid_period`、
`test_refund_is_a_receipt_entry`、`test_stats_margin_respects_type_filter`、
`test_closed_line_still_shows_meter_name`。
