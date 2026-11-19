# 安装 GeekPulse

在运行 Sub2API 的 Linux 主机上安装。向导使用宿主机的 systemd 和 Nginx：Nginx 为站点提供 HTTPS，Sub2API 的 HTTP 端口能从本机 `127.0.0.1` 访问。还需要 sudo、Bash、Git、curl 和带 venv 的 `/usr/bin/python3`（3.11 或更高版本）。安装时需要能访问 GitHub 和 PyPI。

## 运行向导

```sh
curl -fL --proto '=https' --proto-redir '=https' -o geekpulse-install.sh https://github.com/zhanglx-kiko/GeekPulse/releases/latest/download/geekpulse-install.sh &&
sudo bash geekpulse-install.sh
```

1. 输入你用来登录 Sub2API 的域名，不带路径。
2. 输入 **Sub2API 的本机 HTTP 端口**，默认 `8080`。`8091` 是 GeekPulse 的监听端口，需要留空闲。按 `y` 安装。只想检查环境，可以运行 `sudo bash geekpulse-install.sh --check`。
3. 看到「本机服务就绪」后，在 Sub2API 的 Nginx **HTTPS `server` 块**里加上这一行：

   ```nginx
   include /etc/geekpulse/nginx-location.conf;
   ```

4. 检查并重载 Nginx：

   ```sh
   sudo nginx -t && sudo systemctl reload nginx
   ```

5. 登录 Sub2API，打开同一域名下的 `/model-lab/`。想让用户从侧栏进入，就把这个页面的同源完整地址加到 Sub2API 的自定义菜单。

向导会安装监控和升级器，不会修改 Sub2API、替你编辑虚拟主机或运行模型。站点前面有 CDN 时，Nginx 要按站点的可信代理配置读取真实客户端 IP，且不要在 CDN 日志中记录菜单入口的查询参数。

## 检查服务

```sh
systemctl is-active geekpulse.service geekpulse-updater.service
curl -fsS http://127.0.0.1:8091/model-lab/api/health
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8091/model-lab/api/bootstrap
```

两个服务都应为 `active`。健康接口返回 `status: ok`、版本和提交号；最后一个请求没有登录，应返回 `401`。到站点上分别用普通用户和管理员打开面板，可以检查分组可见性和管理入口；不用点「立即测试」。

如果向导在启动服务时停下，先用 `systemctl status geekpulse.service geekpulse-updater.service` 看哪一项报错。如果两个单元都已安装，修好报错后运行 `sudo systemctl enable --now geekpulse.service geekpulse-updater.service`，再做上面的检查。向导不会覆盖占用中的文件或端口。

## 配置与数据

站点配置在 `/etc/geekpulse/geekpulse.env`，支持的变量见[配置示例](../.env.example)。

| 变量 | 用途 |
| --- | --- |
| `GEEKPULSE_PUBLIC_ORIGIN` | Sub2API 的公网 HTTPS 地址，不含路径、查询参数或凭据。 |
| `GEEKPULSE_SUB2API_PORT` | 本机 Sub2API 的 HTTP 端口，默认 `8080`。 |
| `GEEKPULSE_SITE_NAME`、`GEEKPULSE_LOGO_PATH` | 面板名称和图标；图标使用同源、以 `/` 开头的路径。 |
| `GEEKPULSE_RECEIPT_NAMESPACE` | 浏览器里未决请求的存储前缀，默认 `geekpulse-lab`。 |

SQLite 数据库在 `/var/lib/geekpulse/lab.sqlite3`，加密托管 API Key 的主密钥在 `/etc/geekpulse/master.key`。备份活库时使用 SQLite 的备份工具；主密钥单独保管，不要放进 Git。

后续有新版本，管理员在「探测管理 → 版本更新」点「升级」即可，不必再运行安装脚本。

自 v1.0.1 起，安装脚本内包含完整的 MIT 许可声明；新装或升级后可在 `/opt/geekpulse/current/web/license.html` 查阅。
