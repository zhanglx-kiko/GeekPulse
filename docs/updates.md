# 页面升级

安装向导自带升级器。有新版本时，管理员打开「探测管理 → 版本更新」，点「升级」。监控可以全部开着，正在跑的测试也不妨碍升级。

下载期间照常监控。切换版本时只重启 GeekPulse，不重启 Sub2API。恰好被中断的测试不会当成答错，也不会自动重新发起；已发给上游的请求仍可能计费。启用的探测继续按计划运行，暂停的仍然暂停。看到「升级成功」后点「载入新版页面」。

## 更新提醒什么时候出现？

GeekPulse 启动时检查一次 GitHub，此后每 15 分钟检查一次。管理页每分钟刷新本站的检查结果；也可以点「检查更新」。提醒来自标为 **Latest** 的稳定 GitHub Release。普通提交和预发布不触发提醒，不需要向项目登记站点。

## 给发行维护者

- 在 `main` 上发布。Git 标签使用 `vX.Y.Z`，与 `release_updates.py` 的 `VERSION` 一致。将同一提交的 `deploy/geekpulse-install.sh` 作为同名附件发布。工作流会补齐缺失的附件，或比对已有附件，并在 Linux 上检查首装。
- 升级器核对标签、完整提交号、运行文件清单和实际内容；只接受在官方 `main` 上、继承本站当前提交的版本。Webhook 的 HMAC 用于验证通知来源，不是程序文件的数字签名。
- 保持数据库变更可回退。依赖文件变化、更新协议变化或不可逆迁移不走常规页面升级；需要人工处理的版本请在发行说明中明确写出步骤，并按需提升 `UPDATE_EPOCH`。
- 可选的 Release webhook 只负责让指定实例提前检查更新。站点设置 `GEEKPULSE_WEBHOOK_KEY_FILE` 指向私有密钥文件（至少 32 字节的随机文本）；在 GitHub 添加 HTTPS 地址 `/model-lab/api/release-webhook`，事件选 Releases、格式选 `application/json`，使用同一密钥。不配置也能收到版本提醒。

## 升级未完成时

新程序没有通过版本和健康检查时，升级器会切回切换前的程序并再做一次健康检查。页面失去连接时，先查看本次操作的结果，不必重复点击。

操作状态在 `/var/lib/geekpulse-updater/status.json`，成功切换或回退的记录在同目录的 `receipts/`。若页面提示需要恢复，检查 `systemctl status geekpulse.service geekpulse-updater.service`、`/opt/geekpulse/current` 指向的程序目录和回环健康端口。保留 `maintenance` 和 `recovery.json`；排除故障后重启升级器，它会继续恢复，数据库和主密钥不参与程序切换。
