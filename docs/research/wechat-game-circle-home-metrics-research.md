# 微信小游戏圈首页指标接口调研

调研日期：2026-07-20
调研范围：微信公众平台插件中的“游戏圈 -> 首页 -> 游戏圈数据”。
接口类型：`crud_read`（受控浏览器会话内的只读统计查询）。

## 1. 结论与边界

首页由两类私有只读接口驱动：概览接口返回当前周的四组指标卡；详情接口返回按周的趋势/明细数据。已在授权的受控 Chrome、已登录游戏圈 iframe 中，以当前游戏身份复核响应、页面文案和页面前端枚举。

| 接口 | 页面用途 | 已验证结果 |
| --- | --- | --- |
| `spacexgamemp_gamegroupmap_getsummarydata` | 四组概览卡 | `errcode=0`，返回当前周的指标与概览卡环比。 |
| `spacexgamemp_gamegroupmap_getdetaildata` | “详细数据”表和趋势图 | `errcode=0`，`data.list[]` 为按周数据，`data.total_count` 为总周数。 |

这两项接口只读，不应成为内容采集、帖子审核或上游处置的依赖。它们仅可在已授权的受控 Chrome 的 `gamemp.weixin.qq.com` iframe 内调用；不得保存、打印或文档化 Cookie、临时 `token`、完整认证 URL、运营者标识或原始响应导出文件。

### 数据域与更新频率分组

| 数据域 | 更新频率/时间 | 本文状态 | 已知接口或字段范围 |
| --- | --- | --- | --- |
| 运营效果数据 | 每周一 `17:00` 更新 | 已验证 | 本文的概览与详情接口；所有第 5 节字段均属于此组。 |
| 内容粉丝数据 | 每日 `07:00` 更新 | 详情接口存在日期范围参数；当前账号权限待补齐 | 不与本文周指标混用；详见[每日指标接口调研](wechat-game-circle-daily-metrics-research.md)。 |
| 游戏圈粉丝数据 | 每日 `07:00` 更新 | 已验证可按日期范围查询历史日数据 | 不与本文周指标混用；详见[每日指标接口调研](wechat-game-circle-daily-metrics-research.md)。 |

页面的“日均”仅表示一个周周期内的日均值，例如 `avg_quanzi_dau_cur_week`；它仍属于每周更新的运营效果数据，不是每日 `07:00` 更新的数据。

## 2. 前置条件、认证与相关标识

| 字段/前置项 | 来源 | 作用 | 约束 |
| --- | --- | --- | --- |
| `appid` | 当前受控会话所选游戏；可由受控公众平台的游戏目录读取 | 详情查询的目标游戏 | 只可查询当前运营账号有权限管理的游戏。 |
| `token` | 当前 iframe 会话执行 `binduseridentity` 后即时取得 | 两接口的临时鉴权 | 会失效；仅在内存中使用，不持久化、不回显。 |
| `auth_type` | 页面请求固定观察值 | 认证类型 | 当前值为 `4`；应随页面实际请求，不将其视为公开稳定协议。 |
| Chrome 登录态 | 独立用户目录、CDP `9222` 的已登录插件页 | 使 Cookie 与 iframe 上下文可用 | 登录失效时停止请求并要求人工扫码，不尝试复用历史 token。 |

直接重放旧 URL 中的 token 已观察到失败：`errcode=40001`、`errmsg=API_Invalid_Credential`。这不是数据不存在，也不应触发重试历史 token；应在当前授权 iframe 中重新绑定游戏身份。

## 3. 概览接口

### 基本信息

```text
GET /cgi-bin/gameopenmpwap/cfcommcgi/spacexgamemp_gamegroupmap_getsummarydata
Host: gamemp.weixin.qq.com
```

请求载体为 URL Query；无业务请求体。页面实际请求必须携带运行时的 `token` 和 `auth_type=4`，但二者均为敏感认证信息。

### 最小请求示例（脱敏）

```text
GET /cgi-bin/gameopenmpwap/cfcommcgi/spacexgamemp_gamegroupmap_getsummarydata?token=<runtime_token>&auth_type=4
```

### 响应结构

```json
{
  "errcode": 0,
  "errmsg": "ok",
  "cf_request_id": "<diagnostic-id>",
  "data": {
    "avg_quanzi_dau_cur_week": 0,
    "avg_quanzi_dau_huanbi_rate": 0,
    "res_expose_cnt_cur_week": "0"
  }
}
```

`data` 内的计数在当前响应中可能是 JSON 数字或数字字符串；接入方须按数值解析。比例/涨幅多为小数，例如 `0.1603` 由页面显示为 `16.03%`。

## 4. 详情接口

### 基本信息

```text
GET /cgi-bin/gameopenmpwap/cfcommcgi/spacexgamemp_gamegroupmap_getdetaildata
Host: gamemp.weixin.qq.com
```

### 请求定义

| Query 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `token` | string | 是 | 当前 iframe 的运行时临时令牌。 |
| `auth_type` | integer | 是 | 当前页面观察值为 `4`。 |
| `data` | URL 编码 JSON object | 是 | 查询对象，字段如下。 |

`data` 对象：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ds_begin` | integer | 是 | 起始日期，格式 `YYYYMMDD`。当前页面按周展示，建议使用周一。 |
| `ds_end` | integer | 是 | 结束日期，格式 `YYYYMMDD`。页面将每行 `ds` 加 7 天生成 `ds_text`，展示边界应以页面实际日期规则复核。 |
| `appid` | string | 是 | 当前受控会话选中的游戏 AppID。 |
| `cur_page` | integer | 是 | 零基页码；当前页面从 `0` 开始。 |
| `per_page` | integer | 是 | 每页周数；页面默认请求 `10`，未验证服务端最大值。 |

### 最小请求示例（脱敏）

```text
GET /cgi-bin/gameopenmpwap/cfcommcgi/spacexgamemp_gamegroupmap_getdetaildata?token=<runtime_token>&auth_type=4&data=%7B%22ds_begin%22%3A20260601%2C%22ds_end%22%3A20260719%2C%22appid%22%3A%22<managed_appid>%22%2C%22cur_page%22%3A0%2C%22per_page%22%3A10%7D
```

### 响应定义

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `errcode` | integer | `0` 表示业务成功。 |
| `errmsg` | string | 业务结果文案。 |
| `cf_request_id` | string | 短期诊断关联值；不作为业务主键或持久化字段。 |
| `data` | object | 详情结果容器。 |
| `data.total_count` | integer | 符合查询范围的总周记录数。 |
| `data.list` | array<object> | 当前页的周指标记录。空数组表示当前页无数据。 |
| `data.list[].ds` | string | 周起始日期，示例：`2026-07-13`。 |
| `data.list[].ds_text` | string | 页面显示的周期文本，示例：`2026-07-13至2026-07-20`。该文本由前端按 `ds + 7 天`生成。 |

### 最小响应示例

```json
{
  "errcode": 0,
  "errmsg": "ok",
  "data": {
    "total_count": 7,
    "list": [
      {
        "ds": "2026-07-13",
        "avg_quanzi_dau_cur_week": 0,
        "res_expose_cnt_cur_week": "0"
      }
    ]
  }
}
```

## 5. 字段与页面映射

下表以页面前端的“图表和表格配置”枚举为准。详情接口字段名为枚举名的小写形式；概览接口使用其中的当前周字段和部分变化字段。

### 5.1 每周更新组：小游戏圈活跃用户数据概况

| 原始字段 | 页面标签 | 备注 |
| --- | --- | --- |
| `avg_quanzi_dau_cur_week` | 游戏圈日均访问用户数 | 次数/人数值。 |
| `avg_quanzi_dau_huanbi_rate` | 游戏圈日均访问用户数环比 | 小数转百分比。 |
| `avg_quannzi_cross_game_uv_cur_week` | 游戏圈渗透 | 字段中 `quannzi` 为上游拼写。 |
| `avg_quannzi_cross_game_uv_increase` | 游戏圈渗透涨幅 | 小数转百分比。 |
| `avg_gact_cross_game_uv_cur_week` | 日均可拓展用户数预估 | 概览卡文案为“日均可提升用户数预估”。 |
| `avg_gact_cross_game_uv_huanbi_rate` | 日均可拓展用户数预估环比 | 小数转百分比。 |
| `avg_gact_cross_game_uv_rate_cur_week` | 渗透天花板预估 | 小数转百分比。 |
| `avg_gact_cross_game_uv_rate_increase` | 渗透天花板预估涨幅 | 小数转百分比。 |

### 5.2 每周更新组：小游戏圈触达用户数据概况

| 原始字段 | 页面标签 | 备注 |
| --- | --- | --- |
| `res_expose_cover_new_uv_cur_week` | 周触达新进用户 | 当前周人数。 |
| `res_expose_cover_new_uv_rate` | 触达新进用户占比 | 详情表语义。概览卡将该字段作为“周环比”展示，命名与概览语义不一致。 |
| `res_expose_cover_new_uv_huanbi_rate` | 触达新进用户环比 | 详情表语义。 |
| `res_expose_cover_back_uv_cur_week` | 周触达月回流用户 | 当前周人数。 |
| `res_expose_cover_back_uv_rate` | 周触达月回流用户占比 | 同样被概览卡用于“周环比”展示。 |
| `res_expose_cover_back_uv_huanbi_rate` | 周触达月回流用户环比 | 详情表语义。 |
| `res_expose_cover_deposit_uv_cur_week` | 周触达付费用户 | 当前周人数。 |
| `res_expose_cover_deposit_uv_rate` | 周触达付费用户占比 | 同样被概览卡用于“周环比”展示。 |
| `res_expose_cover_deposit_uv_huanbi_rate` | 周触达付费用户环比 | 详情表语义。 |
| `minigame_act_rate_cur_week` | 游戏圈活跃用户游戏内留存 | 小数转百分比。 |
| `minigame_act_rate_increase` | 游戏圈活跃用户游戏内留存涨幅 | 小数转百分比。 |
| `res_expose_cover_new_and_back_uv_cur_week` | 无当前独立概览卡 | 前端读取但未写入当前页面卡片。 |
| `res_expose_cover_new_and_back_uv_huanbi_rate` | 无当前独立概览卡 | 前端读取但未写入当前页面卡片。 |

### 5.3 每周更新组：小游戏圈内容数据概况

| 原始字段 | 页面标签 | 备注 |
| --- | --- | --- |
| `avg_content_expose_cnt_cur_week` | 游戏圈内容曝光 | 次数。 |
| `avg_content_expose_cnt_huanbi_rate` | 游戏圈内容曝光环比 | 小数转百分比。 |
| `avg_per_content_expose_cnt_cur_week` | 人均曝光 vv | 次数；页面字段保留 `vv` 原文。 |
| `avg_per_content_expose_cnt_huabi_rate` | 人均曝光 vv 环比 | 上游字段拼写为 `huabi`。 |
| `avg_publish_uv_rate_cur_week` | 发表率 | 小数转百分比。 |
| `avg_publish_uv_rate_huanbi_rate` | 发表率环比 | 小数转百分比。 |
| `avg_quality_publish_pv_rate_cur_week` | 优质内容率 | 小数转百分比。 |
| `avg_quality_publish_pv_rate_huanbi_rate` | 优质内容率环比 | 小数转百分比。 |

### 5.4 每周更新组：小游戏圈服务数据概况、详细数据

| 原始字段 | 页面标签 | 备注 |
| --- | --- | --- |
| `res_expose_cnt_cur_week` | 游戏圈内容曝光（含红点及公众号） | 次数。 |
| `res_expose_cnt_huanbi_rate` | 游戏圈内容曝光（含红点及公众号）环比 | 小数转百分比。 |
| `selected_content_expose_cnt` | 每周精选内容曝光 | 当前版本无独立概览卡，存在于详情配置。 |
| `selected_content_expose_cnt_ratio` | 每周精选内容曝光占比 | 小数转百分比。 |
| `selected_content_expose_rate_huanbi_rate` | 每周精选内容曝光环比 | 小数转百分比。 |
| `gzh_expo_cnt_cur_week` | 「微信游戏」公众号曝光 | 次数。 |
| `quality_content_redpot_expose_cnt_cur_week` | 发现-游戏推送曝光 | 次数。 |
| `friendfeeds_expose_cnt_cur_week` | 新鲜事曝光次数 | 选择“新鲜事曝光”后展示。 |
| `friendfeeds_expose_uv_cur_week` | 新鲜事曝光人数 | 同上。 |
| `friendfeeds_click_cnt_cur_week` | 新鲜事点击次数 | 同上。 |
| `friendfeeds_click_uv_cur_week` | 新鲜事点击人数 | 同上。 |
| `friendfeeds_click_uv_cur_week_rate` | 新鲜事点击率 | 小数转百分比。 |
| `friendfeeds_event_generate_uv_cur_week` | 游戏圈推荐触发人数 | 选择“游戏圈推荐曝光”后展示。 |
| `rcmd_friendfeeds_expose_cnt_cur_week` | 游戏圈推荐曝光次数 | 同上。 |
| `rcmd_friendfeeds_expose_uv_cur_week` | 游戏圈推荐曝光人数 | 同上。 |
| `rcmd_friendfeeds_click_cnt_cur_week` | 游戏圈推荐点击次数 | 同上。 |
| `rcmd_friendfeeds_click_uv_cur_week` | 游戏圈推荐点击人数 | 同上。 |
| `rcmd_friendfeeds_click_uv_cur_week_rate` | 游戏圈推荐点击率 | 小数转百分比。 |

## 6. 页面格式化与兼容性规则

- 比例、环比、涨幅和点击率均按 `raw_value * 100` 显示，保留两位小数并附 `%`。
- 概览卡的次数/人数大于 `100000` 时显示为一位小数的“万”；其他次数/人数向下取整显示。详情表保留最多两位小数和千分位。
- 详情图表将 `ds` 转为周区间文字，并将详情记录反序后显示在表格中；不能假设接口返回顺序就是页面表格顺序。
- `res_expose_cover_*_rate` 在详情表是“占比”，但当前概览卡把它们作为“周环比”文案展示。任何内部数据模型必须保留接口字段原名和页面展示语义，禁止强行统一为同一指标口径。
- 上游存在拼写兼容字段：`quannzi` 与 `huabi`。实现映射时必须按原字段读取，不能自行更正请求/响应键名。

## 7. 错误码、业务约束与最小验证

| 场景 | 观察结果 | 处理要求 |
| --- | --- | --- |
| 成功 | `errcode=0`、`errmsg=ok` | 验证 `data` 为对象；详情再验证 `data.list` 为数组。 |
| 令牌失效 | `errcode=40001`、`API_Invalid_Credential` | 不重放旧 URL；重新确认受控 Chrome 登录态并在 iframe 内重新绑定身份。 |
| 受控 Chrome/CDP 不可用 | 本地无法获得页面执行上下文 | 不改用保存的 Cookie 或 token；提示人工启动 Chrome/重新登录。 |
| 字段缺失或类型漂移 | 私有页面接口可能随版本变化 | 记录不含认证信息的结构摘要，停止对应指标写入，人工复核页面前端与响应。 |

最小验证方式：在已登录的受控 Chrome 中，先验证 CDP `9222` 可用和插件页登录态；随后只读请求两个接口，确认 `errcode=0`，并核对概览卡与详情表中至少一个当前周字段、一个比例字段及一个服务指标字段。验证日志只记录接口名、`errcode`、字段名和时间，不记录 token、Cookie、完整 URL 或原始响应。

## 8. 变更记录

| 日期 | 作者 | 变更 | 兼容性影响 |
| --- | --- | --- | --- |
| 2026-07-20 | Codex | 新增首页概览与详细数据接口契约、字段-页面映射、认证边界及最小验证方式。 | 新增调研文档；未改变运行时实现。 |
| 2026-07-20 | Codex | 新增运营效果、内容粉丝、游戏圈粉丝三类数据的更新频率分组；将已验证字段明确归为每周更新组。 | 文档口径澄清；未改变运行时实现。 |
| 2026-07-20 | Codex | 修正“每日更新不等于无历史查询”的口径，补充每日指标调研链接。 | 文档口径澄清；未改变运行时实现。 |
