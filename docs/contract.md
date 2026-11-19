# GeekPulse 接口契约

同源入口 `/model-lab/`，API 前缀 `/model-lab/api`。沿用 Sub2API 登录 Bearer，服务端校验用户和管理员权限。所有响应直接返回 JSON，无 data 外层。错误 `{error:{code,message}}`。毫秒为时长单位，时间为 UTC ISO8601，缺少指标用 null。24h、`hours=1|24`、最近 60 轮及 30h 保留均按 `created_at` 归窗/排序，不按完成时间；平均耗时取窗口内终态记录已知的 `duration_ms`，不只限正确答题，未知值不填 0。

此契约对应 GeekPulse 1.0.1，适配目标是 Sub2API `v0.2.23`。站点设置见[部署说明](deployment.md)。

## 页面与用户

- `GET /bootstrap` → `{user:{id,name,role},groups:[{id,name,rate_multiplier}],presets:[{id,name,kind,prompt,expected_answer}],limits:{max_output_tokens,max_timeout_seconds},reasoning_effort_blocks,public_origin,receipt_namespace,version}`。`public_origin` 来自部署配置，是自助表单的初始地址；`receipt_namespace` 指定本标签页的回执存储前缀，不是认证凭据。rate_multiplier 为原站当前分组基础倍率，有限非负数或 null；不读取用户 Key 或专属倍率补值。
- `bootstrap.groups` 是认证后可选择的完整分组目录。`reasoning_effort_blocks` 是按规范化模型名索引的禁用强度数组。省略的模型名使用通用选项。
- `GET /health` 无需认证，返回 `{status,version,sub2api_adapter}`；按发行版安装时还返回完整的 `revision`。`sub2api_adapter` 是本版本适配的 Sub2API 版本。
- v0.2.23 适配使用 auth_token/auth_user/refresh_token/token_expires_at，刷新共享锁 sub2api-auth-token-refresh。不从 URL 读取 token；入口 302 清除查询串，referrer=no-referrer。
- `GET /dashboard?hours=1|24` → `{updated_at,summary,groups}`。不接受服务端单组筛选；前端只能从已授权返回的 `groups` 中选一个或全部。全部用顶层 `summary`，单组用对应 `group.summary`。顶层卡固定显示近 24 小时综合不降智率、平均耗时、近期检测 Token，不跟随 `hours`。
- 顶层 `summary` = `{probes,enabled,runs,passed,scored,pass_rate,avg_duration_ms,total_tokens}`。`probes/enabled` 统计当前可见的全部已配置探测；其余字段只统计近 24 小时管理探测的答题请求。`pass_rate=passed/scored*100`，只有 `passed/failed` 是可判定样本，`error/cancelled/completed` 不进不降智率分母；没有判定返回 null。
- `dashboard.groups` = `[{id,name,display_multiplier,summary,stats,models,drawing_models}]`，仅由未删除、当前用户可见的已配置 probe 生成。暂停或无历史的 probe 仍生成分组；无 probe 的组不返回，删除最后一个 probe 后该组消失。管理员可见全部已配置 probe；普通用户只见 `publish=true` 且 `group_id` 在其可用分组内的 probe，无组的公开 probe 可供所有登录用户查看。自助测试不生成分组，也不进入任何监控统计。
- 分组 `summary` = `{runs,passed,scored,pass_rate,avg_duration_ms,total_tokens}`，固定近 24 小时，仅答题管理探测，含义与顶层相同。全组汇总按实际样本与 Token 加权计算，不平均各模型的百分比。
- 分组 `stats` = `{runs,success_rate,avg_ttft_ms}`，只统计 `hours` 内答题管理探测。界面名为“渠道成功率”“平均首 T”；`passed/failed/completed` 属于请求成功，答题 `failed` 仍在历史中并以红色表示降智；`error` 保留在渠道成功率的已尝试分母中，`cancelled` 不进分母。首 T 只对有值的终态样本取平均。缺样本为 null，不伪造 100%。缓存不在顶层或分组标题统计卡展示。
- `models` 仅包含 `kind=reasoning` 的探测，结构 `[{probe_id,name,model,kind,protocol,effort,enabled,interval_minutes,next_run_at,display_multiplier,stats,history}]`。服务端先从每个 probe 最近 60 条终态/活动全文中筛出成功答题 `passed/failed/completed`，作为 `history` 按时间升序返回；其余错误/取消不显示为历史条目，但错误数值样本仍进入渠道统计分母。答错 `failed` 保留并显示红色。历史与 stats 不受 `hours` 影响；活动项的保存/过期规则见记录保留段。
- 答题模型 `stats` = `{runs,passed,scored,pass_rate}`，只基于上述最近 60 轮历史，`passed/failed` 计入可判定样本。模型旁“不降智率”使用此值，另从 history 中最近一次 `passed/failed/completed` 答题显示“最近耗时”和“最近 Token”（总 Token）。不随 `hours` 改变；排队、运行中、错误、取消不替代完成结果，无完成记录或指标缺失时不造 0、不回退更早指标。原始用量中保留 `cached_tokens`。
- `drawing_models` 使用同样的探测元信息，返回 `history`，不返回 `stats`。每个图片探测各自保留最近 60 轮带作品的成功生成 RunSummary（`completed` 且 `has_artifact=true`），按时间降序，独立于 `hours`；失败、无作品和活动请求不进入图片作品列表，也不生成错误统计。界面把所有图片探测放在该分组答题模块之后，最新图片在最前，横向滚动和前后按钮可访问更早内容。图片 UI（含详情）不显示质量/成功率、Token 用量、缓存、首 T 或费用统计；API 摘要/详情中的原始 usage 数值仍保留，不进入渠道指标。
- 模型 `display_multiplier` 优先用管理员手填值，否则取当前分组 `rate_multiplier`；0 有效，未知 null。组 `display_multiplier` 仅在组内全部探测倍率已知且一致时给数值，否则 null。页面标“参考倍率”，统一值只在组标题显示，否则按探测显示已知值；不造 1，不代表用户专属或高峰倍率，不进入上游请求、费用估算或计费。
- 主题沿用 Sub2API v0.2.23 的 `localStorage.theme=light|dark` 和 `html.dark`。同源 iframe 立即读取并监听父文档 class；独立页读取同源保存值、监听 storage，无保存值时用系统偏好。同步脚本在 CSS 之前运行；页面字体、颜色、输入、按钮采用该版本实际色板。

## 任务

- RunSummary = `{id,probe_id,group_name,model,kind,protocol,effort,status,created_at,finished_at,duration_ms,ttft_ms,input_tokens,output_tokens,cached_tokens,reasoning_tokens,total_tokens,tokens_per_second,cost_usd,public,error_message,has_artifact}`。
- status：`queued,running,passed,failed,error,cancelled,completed`；答题终稿评分 `passed/failed` 在界面显示“不降智/降智”。错误、取消、未评分 `completed` 分别保留真实状态，不强判为降智；图片 `completed` 仅表示生成完成。最近自助答题逐项显示模型、原生 `effort`（空值显示 `default`）、创建时间、判定、输入/输出 Token 和耗时。
- 弹窗以单层展示；从历史进入详情先关闭历史，关闭详情（外侧、Esc、关闭或返回按钮）恢复历史位置与焦点。弹窗内部点击和拖选不关闭；关闭探测配置清空 Key，其余草稿仅保留页面内存。
- RunDetail 在摘要上增加 `{output_text,artifact_html,prompt,expected_answer}`。artifact_html为服务端净化的HTML，仅在空sandbox iframe中srcdoc显示，CSP禁止脚本和联网；禁止innerHTML插入主页面。缩略图隔离样式适配尺寸并禁滚动，完整预览保留滚动访问；页面、弹窗和历史使用随主题变化的细滚动条。
- `GET /runs?scope=mine|public&kind=reasoning|drawing` → `{runs:[RunSummary]}` 最近60条。`scope=mine` 只列当前用户的独立自助记录（`probe_id IS NULL`），不列管理探测结果；`scope=public` 根据当前公开状态及组权限读取。
- `GET /runs/:id` → RunDetail，只有本人、管理员或获准公开/分组可见的记录可读。
- RunSummary 与 RunDetail 提供 `can_delete`。仅当前用户本人拥有、`probe_id IS NULL` 且已经终态、任务已退出活动集合的自助记录为 true；管理员身份不授予删除他人记录的权限，管理探测记录也不可通过此入口删除。
- `DELETE /runs/:id` → `{ok:true}`，只删除当前用户本人已终态的独立自助记录；本人独立自助活动任务返回 `409 {error:{code:"run_active",message}}`，他人或仍存在的管理探测记录（包括活动中的管理探测）按 404 隐藏。管理员身份不授予删除他人记录的权限，但管理员可删除自己拥有的独立自助记录。只有源 `runs` 已被本人删除或被探测级联删除、且请求所有者的最小 `requests` 回执仍保留时，重复 `DELETE` 才由该所有者确认并无副作用地幂等返回成功；管理探测记录仍存在时不能用此规则删除或绕过权限。自助记录和公开列表引用同一源记录，删除后该作品/结果也从公开列表消失。请求幂等回执不随正文删除。
- `POST /runs`，JSON `{base_url,api_key,model,protocol,effort,kind,preset_id,prompt?,expected_answer?,max_output_tokens,timeout_seconds,publish,request_id}` → `202 {id,status}`。自助Key仅任务内存，URL仅允许公网HTTPS，支持根地址、/v1或完整对应接口。protocol=`responses|chat_completions`；kind=`reasoning|drawing`；preset_id=`candy|custom|svg|custom_drawing`。缺省kind=reasoning、preset_id=candy、effort=none、max_output_tokens=64；直接API调用kind=drawing时须显式传svg或custom_drawing，省略preset_id会因缺省candy与drawing不匹配而返回400 invalid_preset。request_id UUID保证同用户重试幂等。
- candy是默认答题预设，题干固定，只接受完整JSON答案结构，不能附加解释；提交不同题干或答案返回`400 preset_locked`，改题须选择custom。custom和custom_drawing提示词必填，前者用于答题、后者用于用户自填HTML/SVG/CSS作品提示词；答题expected_answer可空，空时完整请求状态为completed而不评分，绘图不评分。前端主动切换为drawing时默认选择svg，名称“鹈鹕 SVG 测试”，默认提示词精确为 `Generate an SVG of a pelican riding a bicycle`（末尾无句号）；生成结果仍经过服务端净化并在空 sandbox 中展示。
- 鹈鹕提示词的一手来源为原作者 Simon Willison 的 [2024-10-25 文章](https://simonwillison.net/2024/Oct/25/pelicans-on-a-bicycle/)及[作者仓库 README](https://github.com/simonw/pelican-bicycle/blob/main/README.md)。绘图结果不参与评分。
- `effort` 允许 `""|default|none|minimal|low|medium|high|xhigh|max`。`""` 或 `default` 省略上游字段。Responses 使用 `reasoning.effort`，Chat Completions 使用 `reasoning_effort`。GPT-6.2 Sol、GPT-6.2 Astra 和 GPT-6.1 Sol 禁用 `none` 与 `minimal`。GPT-6.1 Terra 仅禁用 `minimal`。模型名自带禁用后缀也会触发 `400 effort_not_supported`，即使 `effort` 选了可用值。自助校验先于入队，执行器在联网前校验。分组策略可能映射强度，部分 Codex OAuth 非透传路由会删除输出上限。用量以响应 usage 和账单为准。
- 通用新建默认 `candy`、64 Token、`none`。切换绘图默认 `svg`，建议 1024 Token。切回答题默认 `candy`，建议 64 Token。`none` 不可用时建议 `low`。打开已有配置保留提示词和参数。主动切换任务类型或预设会清除未保存的旧提示词和预期答案，并应用所选预设。`custom_drawing` 留空等待输入。
- `POST /runs/:id/cancel` → `{id,status}`；取消不能撤销上游已发生费用。
- 前端每 2 秒单次读取当前任务，结束停止；运行中不显示秒表，终态耗时放在状态文字下方。答题完成后呈现全部终稿和已知用量；图片呈现作品、正文及必要状态/错误恢复，详情也不展示统计用量，API 原始 usage 保留。
- 自助提交使用独立的提交至终态锁，快速连点、回车、重绘不能新增任务；服务端每用户仅允许 1 个活动自助任务。管理员各 probe 分别锁定，不同 probe 可以并发，也可与自助任务并行；同一个 probe 不重叠。所有提交确认丢失时保持对应锁，先查询原 `request_id`；显式重提仍用原编号。`sessionStorage` 的 `<receipt_namespace>-pending` 保存自助最小回执，`<receipt_namespace>-probes` 保存 `{userId,requests:[{requestId,probeId}]}` 管理回执；不存 Key、提示词或上游地址。刷新只用 GET 找回全部未决回执，不自动 POST。
- GET /requests/:request_id → {id,status}，只读取已认证用户本人的最小幂等账本；管理员也不能用此接口查别人。非法UUID为400，未查到为404 request_not_found；终态正文过期后仍可返回回执，但详情按保留规则404。该接口不创建或重试任务。
- tokens_per_second是可靠文本速率：至少两次不同网络读取批次的正文delta、间隔至少100ms、终稿一致且usage完整，使用(输出Token-推理Token)/正文到达间隔；其余为null。结果内部写text_rate_version=1，没有此标记时不展示速率。未来改变计算规则须递增标记；不能与原站输出Token/总耗时的TPS直接比较。
- 作品缩略图与完整预览保留安全SVG/CSS原生动画。首次可见按需取详情，随后从页面内存重建srcdoc；离屏、隐藏页和关闭弹窗卸载iframe，不增加动画轮询或模型请求。减少动态效果偏好移除SMIL动画节点、暂停CSS动效；静态作品不会伪造动画。空sandbox和禁止联网/脚本的CSP保持。
- 高级选项默认折叠。答题结果分区展示状态、指标与正文，图片结果仅呈现作品、正文和必要状态或错误。图片 API 的原始 usage 保留但不呈现在 UI。

## 管理（role=admin）

- `GET /admin/probes` → `{probes:[Probe]}`，api_key永不返回，使用`has_key`布尔。
- Probe = `{id,identity_locked,name,group_id,group_name,base_url,model,protocol,kind,effort,preset_id,prompt,expected_answer,interval_minutes,enabled,publish,max_output_tokens,timeout_seconds,input_price,cached_price,output_price,display_multiplier,has_key,next_run_at}`。`base_url` 是服务端固定的本站地址，不由管理表单配置；用户 Key 不在响应中。价格均为USD/百万Token，可空；仅在三项必要价格齐备时计算预估费用。`display_multiplier`为手动展示覆盖值，默认null，允许有限数值0..1000000；null恢复分组基础倍率，0保留。编辑、启停回写保留该字段；它不参与费用估算。
- `GET /admin/groups/:id/models` → `{models:[string],reasoning_effort_blocks:{[model]:string[]},message?:string}`。管理员须有分组绑定权限。模型来自可调度账号显式非通配的 `model_mapping` 与组 Key 的 `/v1/models` 精确交集，过滤自带禁用强度后缀的模型标识。强度限制取模型标识本身和可调度 OpenAI 账号映射目标的并集，仅返回当前目录的禁用值。没有可用声明时返回空目录和 `message`，此时不创建托管 Key。网关目录无交集时显示通用空态。
- POST /admin/probes 创建时校验当前分组模型目录，未填探测名称时取该分组名称。页面只在名称仍为自动值时跟随切组更新。管理列表可按名称过滤，同名探测独立按 id 操作和统计。
- PUT /admin/probes/:id 按提供字段修改。未提供的 enabled 和其他字段取保存值。新建默认暂停、candy、none、64 Token；间隔 5..10080 分钟、超时 30..900 秒、输出 64..32768 Token。管理 API 拒绝传入 api_key 和 base_url。
- 首次公开、启用或首次手动任务成功入队，都会永久锁定分组、模型、类型、预设、实际题目和预期答案。未公开、未启用且从未运行的新草稿可改定义。identity_locked 为服务端只读状态。已锁定时直接 PUT 改定义返回 409 probe_identity_locked。暂停、取消公开、历史过期和重启均保留此锁。
- 单改间隔、名称、展示倍率或暂停时仍校验当前管理员的分组绑定权限，再复用保存的加密凭据，无需重新查模型目录。保持启用的配置在新的间隔到期后再运行。新建、更换草稿分组或模型、改变推理强度和重新启用时校验当前目录。旧模型下架后可直接 DELETE 此 id，全部相关模型和渠道样本一并移除。
- 管理探测使用按当前管理员和分组唯一的托管 Key：服务端通过当前原站会话在该组下创建，不同用户/分组互不共用；Key 密文加密保存，稳定请求 ID 保存在独立管理账本中，后续同用户同组探测复用。创建结果未知时不会自动重复 POST；下一次显式操作先按凭据/远端 ID 查询，只有仍确认不存在时才沿用同一幂等编号重放同一创建操作。已创建凭据被删除、停用、到期、额度不可用或标识变化时明确报错，不自动重建。该组级托管 Key 不属于单个 probe，删除其中一个 probe 不删除该 Key。
- `DELETE /admin/probes/:id` → `{ok:true}`，停止计划、取消该 probe 活动任务，并删除其全部答题/绘图 runs、公开结果和对应 `channel_metrics`；最小 `requests` 幂等回执保留，已删除探测不能由历史恢复或复现。删除与 worker 完成竞态时，先级联删除源记录，再拒绝迟到结果写回；原管理 run 仍存在期间，`DELETE /runs/:id` 必须返回 404，级联删除后仅请求所有者凭仍在的最小回执可获得无副作用幂等成功。该操作不删除按用户/分组复用的托管 Key。
- `POST /admin/probes/:id/run`，`{request_id}` → `202 {id,status}`，管理员手动立即测试。
- 配置修改即时生效，无需重启服务。共享执行池最多 4 个 worker、16 个已接纳任务；不同探测可由管理员手动或调度并发执行，同探测不可重叠。自助每用户 1 个活动任务只约束自助请求，不阻止该用户发起不同管理 probe。`request_id` 幂等仍按认证用户执行，同编号不能重复收费。

## 实现与隔离

独立Python服务和SQLite，独立systemd用户与项目路径。不修改Sub2api生产表结构、不限制其内存、不重启原服务。管理Key使用服务器独立主密钥加密存放；自助Key不持久化。鉴权固定访问原Sub2api并保留可信客户端IP和User-Agent，避免破坏会话绑定。所有任意上游请求通过DNS公网上网地址检查、连接IP固定、TLS证书验证、禁止重定向；不能访问内网/回环/metadata。

## 记录与原生监控

所有终态runs（passed/failed/error/cancelled/completed）按created_at统一保留最多30小时，并继续限制每个probe最近60条、自助每用户最近60条。queued/running不按过期规则删除。过期终态立即从列表、概览和详情读取结果隐藏；物理清理在启动、任务完成及空闲调度时执行，空闲调度每5分钟至多清理一次，故物理删除可能滞后一个周期。删除释放的SQLite页面留待复用，不执行在线VACUUM或强制WAL收缩。

请求幂等账本保留最小用户/请求ID、任务ID与状态，不随正文过期删除，防止已清理记录的重试再次收费。当前分组概览只展示实验室答题管理探测统计，不加载原站流量卡；不读取自助或图片样本来补渠道指标，也不改变原站监控模式。

历史列表和概览只读取摘要，详情先核对权限再读全文。

答题管理探测的终态指标另存 `channel_metrics` 数值账本，30 小时后清理；每 probe 最近 60 条全文清理不缩短固定 24 小时和 `hours` 指标范围。启动仅从仍保留的答题管理记录幂等回填。安装不足 24 小时时，统计只覆盖实际观测到的样本。账本保留运行时 public/group_id，再与当前 probe 可见范围共同授权，不能借历史公开状态绕过当前权限。绘图和自助均不写该账本。

## 稳定版更新

管理员专用 GET /admin/updates 返回 current、revision、source、checked_at、latest（含 tag 和 revision，或 null）、available、managed、operation。POST /admin/updates/check 用空 JSON 请求刷新，返回 202。POST /admin/updates/install 只接受当前可用的 tag 和 revision，202 表示升级器已接单，终态读取 operation。匿名请求须登录，普通用户返回 403。公开 POST /release-webhook 仅在配置签名密钥后开放，只接受有效 HMAC 通知。GET /health 在安装态返回 revision。正在运行的任务不阻止升级；切换时短暂拒绝新任务，保持探测启停配置。重启中断不重放付费请求，也不判为答题错误。首装、恢复与运维职责见[版本更新](updates.md)。
