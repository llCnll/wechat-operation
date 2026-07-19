# 微信公众平台二维码登录调研

调研日期：2026-07-19  
调研范围：受控 Chrome 中的微信公众平台扫码登录页，以及登录成功后的小游戏目录读取。  
安全边界：本文只记录固定 URL、路径、方法、字段名、状态与响应包装；不记录二维码内容、临时标识、Cookie、token、带认证查询参数的完整 URL、运营者身份或账号资料。

## 结论

二维码登录可作为未来新增会话提供方式调研，但当前服务仍只使用受控 Chrome CDP。已验证登录页由 `mp.weixin.qq.com` 主页面与 `open.weixin.qq.com` 跨域二维码 iframe 共同组成；二维码生成和扫码状态不应由业务采集 Worker 承担。

在已完成扫码且确认登录的受控会话中，外层公众平台页面可调用 `getWxaList`。实际响应使用 `ret` 与 `wax_list` 包装，而非早期观察到的 `errcode/data` 形状；`ret=0` 表示成功，游戏项使用 `appid` 与 `app_name`。服务仅投影这两个字段到本地游戏目录。

## 登录前页面

已确认的固定 URL：

```text
https://mp.weixin.qq.com/cgi-bin/loginpage
https://mp.weixin.qq.com/cgi-bin/scanloginqrcode
https://open.weixin.qq.com/connect/qrconnect
https://open.weixin.qq.com/cgi-bin/mpqrconnect
```

页面包含两个跨域 iframe：

```text
https://open.weixin.qq.com/connect/qrconnect
https://open.weixin.qq.com/cgi-bin/mpqrconnect
```

二维码资源已观察到：

| 方法 | 路径 | 已观察字段 | 响应 |
| --- | --- | --- | --- |
| `GET` | `/cgi-bin/scanloginqrcode` | `action`、`login_appid`、`random` | `image/jpg` |

二维码 iframe 页面文案包含“扫描成功，在微信中轻触允许即可登录”。主页面登录脚本还包含以下路径字面量：`/cgi-bin/bizlogin`、`/cgi-bin/login`、`/cgi-bin/loginauth`、`/cgi-bin/mpfastlogin`、`/cgi-bin/oauthlogin`。

这些路径仅说明页面存在对应流程，不构成可直接调用的 HTTP 登录契约。二维码初始化、状态轮询、确认、跳转与风控字段仍需在单独的新增会话模式变更中，以新鲜二维码完整抓取并验证。

## 登录后游戏目录

```text
https://mp.weixin.qq.com/wxamp/cgi/getWxaList
```

调用位置为登录后的 `mp.weixin.qq.com` 外层页面上下文；请求需要当前页面临时认证上下文和浏览器 Cookie。服务在页面 isolated world 内临时读取并发起请求，不向 Python 层返回该认证上下文。

已验证响应最小结构：

```json
{
  "ret": 0,
  "wax_list": [
    {
      "appid": "...",
      "app_name": "..."
    }
  ]
}
```

响应可能还包含头像、邮箱、用户名、账号类型等字段；服务不得保存、显示或记录这些字段。当前实现只保留 `appid` 和 `app_name`，并由 Django Admin 的“从受控微信同步可管理游戏”动作人工触发。同步失败时保留本地目录缓存。

## 对二维码 HTTP 登录模式的影响

若后续新增浏览器外的二维码登录，至少需要独立验证：

1. 建立二维码前置会话所需的最小 Cookie 与请求字段。
2. 二维码图片生命周期、过期与重新生成策略。
3. 扫码、微信确认、取消和过期的状态码及轮询间隔。
4. 登录成功后的跳转链与会话续期。
5. Worker 跨进程会话使用的受控密钥存储方案。

在这些契约未验证前，不得将已观察到的登录页路径作为可用 API，也不得让 scheduler、采集 Worker 或 AI Worker 尝试登录。
