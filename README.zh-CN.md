# GeekPulse

[English](README.md) · 简体中文

GeekPulse 在你的 [Sub2API](https://github.com/Wei-Shaw/sub2api) 站点上定时测两件事：

- **不降智监控**：给模型出同一道题，看它还能不能答对。可以用自带的糖果题，也可以自己出题。
- **画图监控**：让模型按同一句提示词画图，把几次的结果放在一起看。默认让它用 SVG 画一只骑自行车的鹈鹕。

[安装](#安装) · [在本地跑测试](#在本地跑测试)

## 安装

在 Sub2API 所在的 Linux 主机上运行下面的命令。站点由这台机器上的 Nginx 提供 HTTPS，Sub2API 的 HTTP 端口能从 `127.0.0.1` 访问。需要 systemd、sudo、Bash、Git、curl 和带 venv 的 `/usr/bin/python3`（3.11 或更高版本）。

```sh
curl -fL --proto '=https' --proto-redir '=https' -o geekpulse-install.sh https://github.com/zhanglx-kiko/GeekPulse/releases/latest/download/geekpulse-install.sh &&
sudo bash geekpulse-install.sh
```

向导会问站点域名和 **Sub2API 的本机 HTTP 端口**。用默认端口 `8080` 就直接回车，再输入 `y` 安装。`127.0.0.1:8091` 留给 GeekPulse 自己使用。

在原站 Nginx 的 **HTTPS `server` 块**里加一行 `include /etc/geekpulse/nginx-location.conf;`，然后运行 `sudo nginx -t && sudo systemctl reload nginx`。登录 Sub2API 后打开 `/model-lab/` 就能看到面板，也可以把它加到站点菜单。向导不会改你的虚拟主机配置，也不会重启 Sub2API。

安装本身不会调用模型。配置项和安装后的检查见[部署指南](docs/deployment.md)。

## 怎么看结果

「分组概览」里可以看模型的答题记录和上游返回的 Token 用量。「平均首 T」是这个分组等到第一个 Token 的平均时间。比如，过去 24 小时这个分组只跑了三次糖果题：一次答对，一次答错，一次超时。

| 近 24 小时指标 | 结果 | 怎么算 |
| --- | --- | --- |
| 渠道成功率 | 2/3 | 两次拿到了回答，答错的那次也算请求成功。 |
| 不降智率 | 1/2 | 收到的两个答案里只有一个答对。超时没有答案，不算答错。 |

同一分组的「图片检测」里，每条画图探测各有一排作品，最多 60 张，最新的在前。点开缩略图可以看完整的 HTML 或 SVG 作品。画图不打分，也不计入[答题统计](docs/contract.md)。

想单独试一次，可以到「自助测试」里用自己的 API Key 答题或画图，支持 Responses 和 Chat Completions。自助测试不影响分组统计。

## 开始监控

用原来的 Sub2API 账号登录，就能看自己有权限的分组。只有站点管理员能管理探测。适配版本：[Sub2API v0.2.23](docs/upstream.md)。

1. 打开 `/model-lab/`，在「探测管理」里新建探测。选好分组和模型，再选「答题测试」或「绘图测试」。新探测默认与分组同名，定时任务默认不开。
2. 可以手动跑一次，也可以定时跑，两种方式都可能计费。探测一旦公开、启用或运行过，就不能再换分组、模型、测试类型或预设，也不能改题目、画图提示词或预期答案。多久跑一次，随时可以改。
3. 换模型就新建一条探测。旧模型下架时，在「探测名称」下拉框里筛出同名探测，看清模型再删。旧探测的记录和作品会一起删除，它的答题结果也不再计入统计。其他同名探测的历史不受影响。

## 版本更新

GeekPulse 每 15 分钟检查一次新版本。管理员看到提醒后，在「探测管理 → 版本更新」点「升级」就行，不用关监控。只有 GeekPulse 会短暂重启，探测仍按原来的计划跑。若某次请求恰好被打断，不算答错，也不会自动补跑；上游仍可能对这次请求计费。

## 在本地跑测试

准备好 Git、Python 3.11 和 Node.js。在 Linux 或 macOS 上，从仓库根目录执行：

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -B -m unittest discover -s tests -q
node --test tests/test_frontend_session.cjs tests/test_frontend_interaction.cjs tests/test_frontend_theme.cjs
```

Windows 下用 `py -3.11 -m venv .venv` 创建环境，再把 `.venv/bin/python` 换成 `.\.venv\Scripts\python.exe`。Node 命令不变。

测试用的是临时 SQLite 数据库和模拟的模型回答。装好依赖就能离线跑，不需要 Sub2API 账号或模型 Key。只有前端测试用到 Node.js，不用装 npm 包，也不用构建前端。

## 许可证

[MIT](LICENSE) · © 2026 zhanglx · <changlinghsiao520@gmail.com>
