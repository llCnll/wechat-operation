# 微信小游戏圈数据分析接口调研

调研日期：2026-07-21
调研范围：微信公众平台插件“游戏圈数据 → 数据分析”页的内容消费、内容创作、内容分享、用户留存、排名和配置读取。
接口类型：`crud_read`（已授权受控浏览器会话内的私有只读查询）。

## 1. 结论

“内容分析只显示近 7 天”是页面默认视图，不是详情接口的固定限制。页面实际使用
`spacexgamemp_datacenter_getgroupconsumedetaildata`，以同一日期范围和互斥的
`need_*_data` 开关读取四个分析域。

在当前受控会话中，页面请求 `2026-04-21` 至 `2026-07-20`、`per_page=366` 后得到：

| 分析域 | `need_*_data` 开关 | 返回记录数 | 实际最早/最晚 `ds` | 结论 |
| --- | --- | ---: | --- | --- |
| 内容消费 | `need_content_consume_data` | 85 | `2026-04-27` / `2026-07-20` | 支持历史范围；无记录日期未补空行。 |
| 内容创作 | `need_content_publish_data` | 84 | `2026-04-28` / `2026-07-20` | 支持历史范围；无记录日期未补空行。 |
| 内容分享 | `need_content_share_data` | 84 | `2026-04-28` / `2026-07-20` | 支持历史范围；无记录日期未补空行。 |
| 用户留存 | `need_content_retain_data` | 85 | 当前长范围内为 `2026-04-27` 至 `2026-07-20` | 支持历史范围；响应顺序不能由 `desc_order=false` 推断。 |

因此，采集设计应把它视为“**可按范围回填、但只返回上游实际存在的日期记录**”的数据域：

- 不得把请求范围内缺失的日期写成零值；
- 应按每条 `data.list[].ds` 落库，而不是按请求日期补齐；
- 应按 `ds` 显式排序；当前留存响应在 `desc_order=false` 下仍观察到倒序；
- 历史回填窗口和每日复核窗口应分开设计，不能用一次“昨天”请求替代历史补齐。

## 2. 公共认证与安全边界

所有接口仅在 `gamemp.weixin.qq.com` 的已授权游戏圈 iframe 内调用，依赖当前运行时身份绑定产生的临时认证上下文。

- 不保存或回显 `token`、Cookie、完整认证 URL、运营者标识、原始响应。
- 不重放历史认证 URL；认证失效时重新确认受控 Chrome 登录态并在当前 iframe 内绑定身份。
- 本文只记录路径、非敏感请求字段、业务码、结构摘要和日期范围验证结果。

## 3. 内容分析详情接口

## 接口基本信息

| 项目 | 内容 |
| --- | --- |
| 接口名称 | `游戏圈内容分析历史详情` |
| 接口路径 | `/cgi-bin/gameopenmpwap/cfcommcgi/spacexgamemp_datacenter_getgroupconsumedetaildata` |
| 请求方式 | `GET` |
| 接口类型 | `crud_read` |
| 请求载体 | Query 中的 URL 编码 JSON `data`；认证参数由当前 iframe 运行时注入。 |
| 数据范围 | 当前运营账号可管理的游戏圈，受页面权限约束。 |
| 幂等性 | 是；只读。 |
| 调用后影响 | 不写入上游、不触发消息、不改变页面外业务状态。 |

### 请求字段

`data` 对象：

| 字段 | 类型 | 必填 | 空值/省略语义 | 说明 |
| --- | --- | --- | --- | --- |
| `start_ds` | integer | 是 | 不应省略 | 起始日期，`YYYYMMDD`。 |
| `end_ds` | integer | 是 | 不应省略 | 结束日期，`YYYYMMDD`。 |
| `desc_order` | boolean | 是 | 当前页面传 `false`；不能据此假设返回顺序。 | 排序偏好。 |
| `cur_page` | integer | 是 | 当前页面从 `0` 开始。 | 零基页码。 |
| `per_page` | integer | 是 | 当前页面传 `366`；服务端上限未单独验证。 | 每页记录数。 |
| `need_content_consume_data` | boolean | 四选一 | 未传即不请求该域。 | 内容消费。 |
| `need_content_publish_data` | boolean | 四选一 | 未传即不请求该域。 | 内容创作。 |
| `need_content_share_data` | boolean | 四选一 | 未传即不请求该域。 | 内容分享。 |
| `need_content_retain_data` | boolean | 四选一 | 未传即不请求该域。 | 用户留存。 |

业务规则：四个 `need_*_data` 开关应一次只传一个 `true`。页面切换分析标签时复用同一路径，替换该开关。

### 请求示例（脱敏）

```text
GET /cgi-bin/gameopenmpwap/cfcommcgi/spacexgamemp_datacenter_getgroupconsumedetaildata?token=<runtime_token>&auth_type=4&data=<urlencoded-json>
```

```json
{
  "start_ds": 20260421,
  "end_ds": 20260720,
  "desc_order": false,
  "cur_page": 0,
  "per_page": 366,
  "need_content_consume_data": true
}
```

### 响应结构

| 字段 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `errcode` | integer | 否 | `0` 为成功。 |
| `errmsg` | string | 否 | 业务结果文案。 |
| `data` | object | 否 | 详情结果。 |
| `data.total_count` | integer | 否 | 符合当前上游记录条件的总数。 |
| `data.list` | array<object> | 是 | 当前页真实存在的日期记录；空数组表示没有记录。 |

`data.list[]` 按开关的字段：

| 开关 | `data.list[]` 已验证字段 | 页面语义 |
| --- | --- | --- |
| `need_content_consume_data` | `ds`、`daily_active_user`、`content_consume_cnt` | 游戏圈访问用户数、内容浏览总量。 |
| `need_content_publish_data` | `ds`、`publish_pv`、`publish_uv` | 游戏圈发帖总数、游戏圈发帖人数。 |
| `need_content_share_data` | `ds`、`share_cnt`、`share_uv` | 内容分享次数、内容分享人数。 |
| `need_content_retain_data` | `ds`、`quanzi_dau` | 游戏圈活跃用户；留存的对比窗口由请求日期范围决定。 |

### 成功响应示例（结构脱敏）

```json
{
  "errcode": 0,
  "errmsg": "ok",
  "data": {
    "total_count": 85,
    "list": [
      {
        "ds": "2026-04-27",
        "daily_active_user": 0,
        "content_consume_cnt": 0
      }
    ]
  }
}
```

### 错误与兼容性

| HTTP 状态 | 业务码/现象 | 处理要求 |
| --- | --- | --- |
| `200` | `errcode=0` | 校验 `data.list`，按每条 `ds` 保存。 |
| `200` | 非 `0` `errcode` | 记录脱敏错误摘要，不写零值或猜测值。 |
| 非 `200` 或非 JSON | 传输/契约异常 | 不覆盖既有快照，待人工复核。 |
| 认证失效 | iframe 无法请求或绑定身份失败 | 延后任务等待人工扫码，不重放历史认证信息。 |

### 最小验证

1. 在当前已登录受控 Chrome 中，用不少于 30 天的范围分别请求四个开关。
2. 确认 `errcode=0`、`data.list` 为数组，且每个开关具有表中字段。
3. 验证请求范围与返回 `ds` 范围不同的情况，不为缺失日期补零。
4. 对留存分别切换 `7/14/30` 天，确认请求区间分别变为截至当前数据日的 7、14、30 天。

## 4. 同页其他分析接口目录

| 接口路径 | 类型 | 页面用途 | 已观察请求字段 | 已验证响应结构摘要 |
| --- | --- | --- | --- | --- |
| `spacexgamemp_datacenter_getgroupconsumesummarydata` | `crud_read` | 顶部昨日概览卡 | `data={}` | `daily_active_user`、`content_consume_cnt`、`topic_publish_cnt`、`publish_author_cnt` 及对应变化率、`data_last_update_ds`。 |
| `spacexgamemp_datacenter_getdatacenterrank` | `crud_read` | 游戏圈数据排名 | `data.ds`、`data.rank_type` | `current_rank`、`total_rank`、`rank_list[]`；数组项含 `rank`、`appid`、`group_name`、图标/封面字段。 |
| `spacexgamemp_gamegroupmap_getrankdata` | `crud_read` | 运营效果指标排名 | `data.ds`、`data.rank_type`、`data.data_type`、`data.category_3_id` | `current_rank`、`current_value`、`total_rank`、`rank_list[]`；数组项含 `rank`、`value`、`appid`、`group_name`。 |
| `spacexgamemp_gamegroupmap_getsummarydata` | `crud_read` | 运营效果当前周概览 | 无业务 `data` | 当前周活跃、触达、内容、服务指标与环比/比例字段。 |
| `spacexgamemp_gamegroupmap_getdetaildata` | `crud_read` | 运营效果周趋势 | `data.ds_begin`、`ds_end`、`appid`、`cur_page`、`per_page` | `data.total_count`、`data.list[]`，每项以 `ds` 为周起点。 |
| `spacexgamemp_gamegroupmap_getgamegroupcapcardlist` | `crud_read` | 游戏圈能力卡 | 无业务 `data` | `admin_base`、`capacity_type_list[]`、`map[]`、`total_count`。 |
| `spacexgamemp_gamegroupmap_getgroupconfiginfo` | `crud_read` | 运营配置与同类参照 | 无业务 `data` | 游戏分类、开关、均值/中位数、触达损失/未注册覆盖等配置指标。 |

### 已知枚举

| 字段 | 值 | 页面含义 |
| --- | --- | --- |
| 内容详情开关 | `need_content_consume_data` | 内容消费。 |
| 内容详情开关 | `need_content_publish_data` | 内容创作。 |
| 内容详情开关 | `need_content_share_data` | 内容分享。 |
| 内容详情开关 | `need_content_retain_data` | 用户留存。 |
| `getdatacenterrank.data.rank_type` | `1` | 游戏圈访问用户数。 |
| `getdatacenterrank.data.rank_type` | `2` | 内容总浏览量。 |
| `getdatacenterrank.data.rank_type` | `3` | 游戏圈发帖总数。 |
| `getdatacenterrank.data.rank_type` | `4` | 游戏圈发帖人数；尚未在当前页面资源记录中取得成功样本。 |

## 5. 采集建议（非实现变更）

1. 将内容分析详情作为独立日快照来源；来源维度至少区分 `CONSUME`、`PUBLISH`、`SHARE`、`RETAIN`。
2. 首次回填应按日期块、四个开关分别读取；以返回 `ds` 为唯一业务日，不按请求范围生成空快照。
3. 留存的 `7/14/30` 天是**查询窗口**，不能把同一 `quanzi_dau` 直接命名为 7 日、14 日或 30 日留存率，除非页面字段和计算口径另行验证。
4. 排名、能力卡、配置参照先作为页面诊断/运营辅助数据；是否持久化须先确定其业务口径和变更频率。

## 6. 变更记录

| 日期 | 作者 | 变更 | 兼容性影响 |
| --- | --- | --- | --- |
| 2026-07-21 | Codex | 新增“游戏圈数据 → 数据分析”页接口调研，验证内容分析范围查询、四个开关、留存窗口、排名与配置接口。 | 新增调研文档；未改变运行时实现。 |
