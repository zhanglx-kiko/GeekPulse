# 适配边界与第三方来源

GeekPulse 使用 [Sub2API v0.2.23](https://github.com/Wei-Shaw/sub2api/tree/v0.2.23) 的登录、分组和模型接口。健康接口里的 `sub2api_adapter` 是适配目标，不是对站点当前版本的检测结果。仓库不分发 Sub2API 的程序或数据。

升级原站时，在隔离站点分别用普通用户和管理员验证：

| 接口或约定 | 验证内容 |
| --- | --- |
| `/api/v1/auth/me`、`/api/v1/auth/refresh` | 同源会话、共享刷新锁 `sub2api-auth-token-refresh`、退出后不能被迟到响应重新登录。受信代理传递的 IP 和 User-Agent 必须与原站一致。 |
| `/api/v1/groups/available`、`/api/v1/admin/groups/all` | 普通用户与管理员的可见分组不同；服务器按实际权限授权，不能只看页面上是否有按钮。 |
| `/api/v1/admin/accounts`、`/api/v1/keys`、`/v1/models` | 可用模型是组内显式映射与网关目录的交集；无结果不回退全站列表。首次准备托管 Key 属于写操作，先确认测试范围。 |
| 自定义菜单、主题和站内流量 | 菜单从干净的同源 `/model-lab/` 进入；深浅主题跟随原站。实际流量仅来自原站支持的接口，与主动探测数据分开。 |
| 协议和用量 | 用合成夹具回归模型别名、强度限制及 Token 参数；输出上限是请求建议，不是实际用量保证。真实生成需单独授权。 |

## 来源

- [Sub2API 标签的许可证](https://github.com/Wei-Shaw/sub2api/blob/v0.2.23/LICENSE)适用于上游项目。登录、主题和接口均是为兼容该版本而适配；名称不表示隶属或获得背书。
- Python 的 [cryptography](https://pypi.org/project/cryptography/) 提供 Fernet 密钥加密，通过依赖安装，不内嵌发行包。站点应保持依赖安全更新。
- SVG 绘图预设里的短提示词来自 Simon Willison 的[原文](https://simonwillison.net/2024/Oct/25/pelicans-on-a-bicycle/)和[示例仓库](https://github.com/simonw/pelican-bicycle)。本仓库不分发该仓库的图片，也不把绘图结果作为自动评分依据。
- 自带的 `web/pulse.svg` 是本项目绘制的简单图标；部署方可指定同源站点资源，不需要分发站点的品牌素材。

## 版本更新参考

[Sub2API v0.2.23 更新服务](https://github.com/Wei-Shaw/sub2api/blob/v0.2.23/backend/internal/service/update_service.go)通过缓存的 GitHub Release 检查、管理员应用更新和制品校验完成二进制更新。GeekPulse 独立实现对应的页面流程，按完整 Git 提交部署 Python 和前端，使用独立本机升级器及任务门禁。
