# XAU Next 本机运行说明

本说明针对 Windows 本机候选。程序只读取 MT5，交易操作由用户在 MT5 人工完成。候选安装、启动和备份不替换旧系统入口，也不对外发布。

## 已有持仓的两层管理（r25候选）

开仓并明确关联方案后，在工作台查看“已有持仓管理”。第一层持续检查原止损、目标、时间和已定义的结构退出条件，并显示具体依据；新方案可预先定义成交后的结构退出，旧方案不会被补写规则。显示“尚未触发原退出条件”不代表目标必达。原方案目标与MT5实际止盈不同会分别列出，软件不会自动改价。

行情出现原分析登记的重要变化时，第二层提示可手动重评。点击“准备当前持仓重评包”只保存冻结资料，不调用模型、不收费；同一份仍新鲜的待回答包会复用。进入分析页后可复制给模型并导入，或按已有模型API通道手动生成（实际生成可能收费，准备包本身不生成）。重评会分别说明原目标仍获支持、依据减弱、逻辑失效或资料不足，建议和原文独立保存，不产生新的入场候选，不重置真实持仓计时。

已触发明确退出条件时，不等待新回答。重评建议不会自动覆盖原方案、修改MT5或替你交易。持仓数量、关联、实际止损止盈变化或建议过期后，旧意见标为历史；资料不足先等待事实恢复。当前候选功能不表示正式入口已切换，也不证明预测或交易效果改善。

## 安装与日常启动

完整候选包含独立 API wheel、静态 Web、只读桥源码及其锁文件、公开契约、启动工具和 `release-manifest.json`。安装需要本机 Python 3.12；候选使用随包的 uv 0.13.0，按锁定版本安装依赖。首次安装可能需要联网取得锁文件列出的包。日常启动不运行依赖安装，也不需要 Node.js。

1. 解压候选到独立目录，例如 `D:\XAU-Next-Candidate`。保留其中所有文件。
2. 双击 `Setup.cmd`，完成一次安装。
3. 双击 `Start.cmd`。启动器选择空闲 API/Web 端口，隐藏后台窗口并打开浏览器。
4. 使用结束时运行 `Stop.cmd`。它会核对进程 PID、创建时间、可执行文件和命令行，停止本次启动器创建的 Python 服务及只读桥，不停止 MT5 终端。

### r11 启动生命周期修复候选

r11 的 `start` / `restart` 在 Windows Job 中运行时，通过当前用户的 Windows 桌面启动独立启动器。桌面可能附加允许脱离的兼容性 Job；启动器仅尝试一次 Windows 允许的脱离，并重新核验已不在任何 Job 中，才执行启停。它使用基础 Python 运行启动脚本，API 与桥仍使用安装清单中的独立环境。此功能不安装系统服务、计划任务或开机自启，不提权。

`runtime/launcher.json` 的 `lifetime` 记录核验结果；桌面交接回执为同目录的 `desktop-launch-*.json`，不含会话令牌。CLI 等待健康检查及回执完成才显示成功。没有桌面、系统不允许脱离或无法核验时明确失败，不回退到受宿主管理的后台进程；若等待结果超时，先检查运行状态，避免盲目重启。`restart` 的桌面核验发生在停止现有服务之前。

日常仍从已批准入口启动。切换 r11 后需用新打开的页面，端口和令牌可能改变；不要仅刷新旧会话。源码与候选安装测试使用独立 SYNTHETIC 数据目录，强制终止测试调用者后确认 API、Web 和只读桥继续采集。此证据不能替代用户实际退出/重启 Codex 后的 MT5 读取验收，也不代表连续 8 小时稳定性已通过。

默认运行数据在 `%LOCALAPPDATA%\XAU-Next\data`。API/桥的安装环境、数据库、附件和日志都保存在数据目录；候选程序目录在安装和运行时保持不变。数据目录必须位于本机磁盘、当前用户可写、程序目录之外。

在 PowerShell 中指定自己的数据目录：

```powershell
py -3.12 .\tooling\launch\manage.py setup --data-root "D:\Example-Data"
py -3.12 .\tooling\launch\manage.py start --data-root "D:\Example-Data"
py -3.12 .\tooling\launch\manage.py stop --data-root "D:\Example-Data"
```

启动器不会连接或登录未知账户。默认来源为 MT5；在页面设置中选择已运行的终端和来源身份，读取失败会保留历史及具体错误。服务器时区按实际终端来源配置；当前用户确认的固定 UTC+3 使用 `UTC+03:00`，它与界面显示时区 `Asia/Shanghai` 分别保存。合成演示需显式使用 `start --source synthetic`，页面显示合成来源。

会话令牌只交给 API 进程环境及浏览器 URL fragment，页面读取后立即清除 fragment；令牌不写入静态文件、启动状态或日志。关闭原标签后需要新会话时，运行 `restart`，它先停止自身服务再启动新会话。程序已运行时再次 `start` 会提醒使用现有标签，避免偷偷重启正在处理的任务。

更新到另一份候选时，先从旧候选运行 `Stop.cmd`，备份数据，再从新候选对同一 DATA_ROOT 运行 `Setup.cmd` 和 `Start.cmd`。保留旧候选与备份，直到确认历史和日常操作正常；不要同时让两个候选管理同一个数据目录。

## 模型 API（1.4 候选）

日常入口仍以已批准的候选为准。模型 API 候选未切换前，不会改变当前日常服务；可在独立 DATA_ROOT 启动候选进行联调，勿把未批准候选指向活动数据目录。

1. 在候选的「设置 → 模型 API」中启用入口，核对 `https://api.zyuou.com/v1`、`Responses`、`gpt-6.1-sol`、`high`，保存配置。`/dashboard` 是管理页，不能作 API 地址。字段修改只影响后续任务。
2. 在「本机 API Key」隐藏输入框录入密钥，点击「保存到 Windows 凭据」。不要把密钥发到聊天、写入文件或填进模型 ID。配置只留本应用凭据引用，备份不包含密钥；换机器恢复后重新录入。密钥轮换使用新引用，已经排队的任务仍使用原冻结引用；必要时先取消旧任务。
3. 分别点击「鉴权并读取模型列表」「按需查询余额」。失败保留上次成功查询的日期和配置修订；余额、单位、有效性缺失时显示未知，不默认美元或有效。余额不会跟着每5秒行情采集查询。
4. 勾选并主动启动「小型合成生成测试」。这会调用中转站，可能收费；只测试 SYNTHETIC JSON，不含行情。测试最多2048输出 token、120秒，若配置更低则用较低值。成功仅证明所选协议、参数和格式被接受且该合成回答合格，不能独立证明底层模型身份或推理强度，也不能替代完整分析的业务验收。
5. 配置验证后，在分析页点击「新分析（API）」「窗口内更新（API）」，或选已有有效待回答请求后点「通过 API 分析此请求」。后台先固定资料，再接收完整回答，复用手工导入的全部校验和版本登记。页面关闭不取消，重开同一请求可恢复状态；成功后工作台和版本历史刷新。
6. API 响应成功、回答校验通过、产生候选、当前可以入场是不同状态。`plans=[]` 正常保存；判断能否入场仍查看工作台实际条件和预算。MT5 一直只读，交易人工执行。

默认总超时300秒、输出12000 token、响应2 MB、每日10次为工程边界，真实联调后再调整。每日次数按 UTC 日期计算，包含合成测试和已发送但结果未知的调用。每日金额限制需本站输入/输出单价、币种和来源，价格未知则费用未知。按输入字节数与输出上限保守预留估算，未知调用不释放预留；此限制不是供应商账单上限，缓存、分档或附加费用需以本站真实规则为准。

普通 JSON 可选「JSON 对象格式」，若服务明确不接受格式参数，可由用户改为「提示词 JSON」，重新做合成测试；两种模式都执行相同的本地完整校验。只有本站明确不支持 Responses 时，再手动选择 Chat Completions 并测试。业务失败不会自动切换协议、模型或推理档位，也不会自动修复答案或重发生成请求。

取消阻止后续本地导入，不保证供应商停止或不计费。`outcome_unknown` 表示发送后未持久取得完整响应，不自动重发；先核对中转站记录，可切换手工导入，明确重试须确认可能重复费用。响应已经完整保存后重启只恢复本地校验，不再次生成。原始响应、提取回答、实际外发请求体及哈希保存在本地附件；若响应回显本应用密钥，会脱敏并记录原始字节摘要，同时阻止导入。

API 接入后仍可复制、下载分析包，以及粘贴或文件导入。外发字段只含冻结行情、统计、预算、必要订单身份和复盘摘要；账户范围为本机既有不透明 ID，必要券商订单 ID 保留以维持回答关联，终端路径与登录字段排除。

协议实现参考 [Responses](https://developers.openai.com/api/reference/resources/responses/methods/create)、[结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)；Windows 凭据接口参考 [CREDENTIALW](https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentialw)。本站部署和能力以实际联调为准。

## 数据目录

| 位置 | 内容 |
|---|---|
| `db/xau-next.sqlite3` | 请求、原文引用、版本、方案、账户事实、复盘、任务与设置 |
| `attachments/` | 按哈希保存的不可变原文和行情材料 |
| `staging/` | 未完成附件的准备目录 |
| `logs/` | 每次启动的 API/Web 诊断日志 |
| `runtime/install.json` | 当前候选对应的解释器位置与安装版本 |
| `runtime/launcher.json` | 自有进程身份、端口、启动耗时；不含会话令牌 |
| `backups/` | 可选的用户备份目录 |

`config.json` 可提供启动默认值；环境变量优先于该文件。常用变量为 `XAU_NEXT_DATA_ROOT`、`XAU_NEXT_SOURCE`、`XAU_NEXT_CONTRACT_ROOT`、`XAU_NEXT_BRIDGE_PYTHON`、`XAU_NEXT_BRIDGE_SOURCE`。一键入口自动设置路径、精确 Web origin 和会话鉴权，一般无需手工填写。业务设置由数据库维护。

## 备份与恢复

备份通过 SQLite backup API 得到一致数据库副本，并只复制该副本实际引用的不可变附件，逐个校验哈希。不要自行只复制运行中的 `.sqlite3` 主文件。

在原候选目录运行，目标备份目录必须不存在或为空：

```powershell
py -3.12 .\tooling\launch\manage.py backup --data-root "D:\Example-Data" --destination "D:\XAU-Next-Backups\backup-001"
```

恢复总是写入新的空目录，不覆盖当前数据，也不自动切换运行入口：

```powershell
py -3.12 .\tooling\launch\manage.py restore --data-root "D:\Example-Data" --backup-root "D:\XAU-Next-Backups\backup-001" --destination "D:\XAU-Next-Restored"
py -3.12 .\tooling\launch\manage.py setup --data-root "D:\XAU-Next-Restored"
py -3.12 .\tooling\launch\manage.py start --data-root "D:\XAU-Next-Restored"
```

恢复前后检查数据库完整性、外键、schema 版本以及全部引用附件的长度和 SHA256。备份覆盖数据库中的业务设置；安装环境、日志、会话令牌和外部环境变量不属于业务备份。若使用了独立 `config.json` 启动默认值，应单独保存该配置并在恢复后核对。

## 源码开发与构建候选

源码构建使用 Python 3.12、Node.js、`package.json` 固定的 pnpm 10.28.2、`uv.lock` 与 `pnpm-lock.yaml`。系统 PATH 上的其他 pnpm 版本不会直接用于构建；工具会核对版本，必要时通过 `npx --yes pnpm@10.28.2` 运行固定版本。

开发者可以从已准备的 `.venv` 和前端 `dist` 直接执行 `start`。源码 `setup` 会用当前源码构建 wheel、建立独立运行环境，并按锁文件安装和构建前端；后续改动 API 后需重新 `setup`，或者使用源码开发环境单独启动。

生成新候选，目标目录必须为空：

```powershell
.\.venv\Scripts\python.exe .\tooling\package\build_candidate.py --destination "D:\Example-Candidates\v1-local-001"
```

构建过程不包含业务数据、账户凭据或旧项目内容。清单记录代码提交、是否存在未提交源码改动、工具版本、锁文件哈希和每个候选文件的 SHA256。`source_dirty=true` 说明构建含当前工作树修改；候选文件哈希仍可单独核对，不冒充纯提交产物。安装时先验证清单，再将环境安装到 DATA_ROOT。

## 模型连接中断与流式诊断

2026-10-14 r9切换后的唯一诊断收到HTTP200及流式数据，超过此前61秒断点后持续到168.75秒；此次实际因2MB原始响应字节上限而停止。SSE逐小段传送推理内容时，封装可能远大于正文：本次约7949帧只含20.5KB解码文本，已占近2MB原始响应。响应字节预算与模型输出token预算是不同限制；不能用丢弃原始帧、拼补未完成回答或无声扩大上限来让检查通过。当前保持原限额，需用户确认后再调整。此次未复现此前61秒断开，不能把2MB限制倒推为旧断开的原因。详见候选旁置.diagnostic.json。

先区分：HTTP响应完整、模型成功终态、回答结构/业务校验、版本保存是四层结果。长非流式请求若本地先断开，中转站仍可能继续生成和计费，先看调用记录和提供方使用记录，再明确重试。切换流式会产生新的配置修订，需要按该配置重新做一次小型生成验证，不能沿用另一配置的通过记录。

r9候选把连接异常显示为建立/读取/写入/协议等预定义原因，并在内部调用证据保存时间与字节数，不保存可能含密钥的异常原文。SSE中的response.failed表示提供方明确失败，response.incomplete及max_output_tokens表示未完成或输出预算耗尽；有正文、output_text.done、output_item.done或空的response.completed事件名称，都不能替代完整成功终态。收到有效终态就结束接收，避免代理保持连接导致已完成回答被误记为超时。

2026-10-14本站实测：JSON对象非流式小测通过；完整非流式约61秒断开；Responses流式两次小测均以上游失败结束。没有通过完整API分析验证，不应持续重复完整请求。保留手工导入通道。本站文档列出Chat Completions兼容路径；协议更改须按用户确认的范围单独测试，不能在失败的业务调用内自动降级。来源：[本站报错说明](https://api.zyuou.com/docs/errors/)、[本站兼容客户端接入](https://api.zyuou.com/docs/cherry-chatbox/)、[OpenAI流式事件](https://developers.openai.com/api/docs/guides/streaming-responses)、[HTTPX异常类别](https://www.python-httpx.org/exceptions/)。 用户随后明确授权Chat Completions流式例外验证；该小测通过，但唯一完整分析仍在61.741秒断开、无响应头/正文。故仅换协议和流式未解决长请求问题。应优先核对返回链路的首响应等待上限、SSE缓冲/心跳及实际网络异常类型，再决定新的收费诊断，不重复消耗已有失败请求。

## 常见问题

历史载荷需要压缩时，先停止本应用并创建一致备份，再从备份恢复到新的空目录执行；原数据和备份保留：

```powershell
.\.venv\Scripts\python.exe tooling/backup/compact.py --backup-root "D:\Example-Data\backups\chosen-backup" --destination "D:\Example-Data\restored-compact"
```

工具核对所有业务表的逻辑内容摘要、附件和数据库完整性，压缩后回收空页，记录 `validation/storage-compaction.json`。只有检查成功后才把入口绑定到新目录；不会自动覆盖原运行目录。

| 提示或现象 | 处理 |
|---|---|
| API/桥环境不存在 | 在同一候选目录、同一 DATA_ROOT 运行 `setup` |
| 端口占用 | 省略端口让启动器自动分配，或显式指定不同的 `--api-port` 与 `--web-port` |
| 已在运行 | 使用原浏览器标签；需要新会话时运行 `restart` |
| 无法确认进程身份 | 保留现有进程，检查 PowerShell/CIM 读取权限；启动器不会按模糊进程名结束服务 |
| MT5 连接失败或时区未知 | 核对已登录终端路径、账户身份和已确认的服务器时区；历史仍可查询 |
| 启动时契约版本不匹配 | 使用同一候选内的 API、Web 与 `contracts/` 文件，重新 `setup` |
| 候选校验失败 | 保留原目录，重新构建到另一个空目录；不要修改清单来掩盖差异 |
| 数据目录不可写或在程序目录内 | 改用默认 LOCALAPPDATA 目录或独立可写本机目录 |

## 验证边界

运行工具测试位于 `tests/operations/`：覆盖独立进程启动、鉴权、动态配置、合成桥读取、完整自有 Python 子树退出、端口冲突、原文附件备份恢复、拒绝覆盖非空恢复目标。已构建候选另以 `XAU_NEXT_TEST_CANDIDATE` 指定目录，验证 wheel 安装、包内迁移、候选启动及安装运行前后程序目录哈希不变。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/operations -q
$env:XAU_NEXT_TEST_CANDIDATE = "D:\Example-Candidates\v1-local-001"
.\.venv\Scripts\python.exe -m pytest tests/operations/test_candidate.py -q
```

软件测试、真实只读终端数据验证和用户亲自完成日常操作是不同验收层级；当前整体验收及剩余缺口以 `docs/VALIDATION.md` 和 `docs/DELIVERY.md` 为准。V1.1 实验不作为本机日常版安装前置条件。

## 模型完整响应预算（1.4.1）

设置页分别显示原始响应字节上限与最终答案字节上限。原始流包括事件包装及推理增量，最高16 MB；提取后的最终JSON按UTF-8字节检查，最高2 MB。发送的输出token上限包括推理和回答，是否执行取决于上游支持；扩大字节上限不会扩大或建立token硬上限，总超时独立生效。新预算须按真实使用目的设置，过期资料仍拒绝导入。

只调整限额或价格时，可以沿用同一地址、密钥、协议、模型、档位、格式及流式设置的原小测证据；页面继续显示原测试修订和时间，不额外调用模型，也不表示完整分析已经通过。改变调用身份或密钥须重新主动测试。旧设置缺少最终答案上限时读取默认2 MB，不为升级改写原记录。

MODEL_ANSWER_TOO_LARGE表示最终答案超过独立限制，完整原响应和供应商用量仍保留但不导入；RESPONSE_TOO_LARGE表示原始流达到限制，结果仍可能未知。任何一种情况均不自动重发。API压缩仅改变外发排版和重复说明，不重写手工分析包、历史输入或答案。

2026-10-14本轮r10实测已完整收到4.44MB流及15.4KB答案，用量47138/18881 token；不存在本机容量截断。模型将22处日期写为YYYYMMDD，并在两项候选说明中漏填missing_conditions，结构校验拒绝，未导入或重试。后续应针对原契约加强模型输出约束；不能放宽日期/身份校验或自动补改本次回答来制造通过。完整诊断见D:\Example-Candidates\v1-local-20261014-model-completion-r10.diagnostic.json。

## 本地 CLIProxyAPI 接入边界（2026-10-16，源码与离线验证）

源码允许 `http://127.0.0.1:8317/v1`，必须明确填写端口和 `/v1`；远程仍要求 HTTPS。localhost、IPv6、其他回环写法、局域网 HTTP、凭据、查询、片段和额外路径均不在本机例外内。后端继续使用 Bearer 鉴权，不跟随重定向、不继承环境代理、不自动重试或改变模型及应用凭据。地址保存本身不发送模型请求；改变地址或协议会使旧能力验证失效。

本轮没有部署这项源码改动，也没有改变正式模型配置。CPA 8.0.30 的 Codex 转换源码存在明确限制：

| 入口 | 结构化格式 | 输出 token 上限 |
| --- | --- | --- |
| Chat Completions | 转换 `json_schema`；没有 `json_object` 分支，不能认为原 JSON mode 生效 | `max_completion_tokens` 映射被注释 |
| Responses | 转换保留 `text.format`，避免 Chat 格式映射损失；真实上游接受性待验证 | 主动删除 `max_output_tokens` 与 `max_completion_tokens` |

Anthropic Messages 使用单独协议：中转站名称任选，Base URL 填 `https://xcmapi.org/v1`，协议选择 Anthropic Messages，模型 ID 按该服务实际提供的值填写。程序向 `/v1/messages` 发送 `messages`、`max_tokens` 和 `stream`，并带 `anthropic-version: 2023-06-01` 与 Bearer Key；不发送 OpenAI 的 `response_format`、`reasoning_effort` 或 `stream_options`。回答格式由冻结提示约束，最终仍由本地完整 Schema 校验；流式响应须收到 `message_stop`，`max_tokens` 截断不导入。该 Bearer 鉴权约定针对 xcmapi 中转站，不代表原生 Anthropic 服务的认证方式。

后续隔离验收建议：Base URL=`http://127.0.0.1:8317/v1`，protocol=`responses`，model_id=`gpt-6.1-sol`，reasoning_effort=`high`，output_format=`json_schema`，stream=`true`，timeout_seconds=`600`，max_output_tokens=`25000`，max_response_bytes=`16000000`，max_answer_bytes=`2000000`。这些是待验参数，不是已验证的正式配置；XAU 继续发送 token 字段并保留审计，但此 CPA Codex 路径不能依靠它执行 25000 的硬上限。若 token 硬上限是调用前置条件，本版本 CPA Codex 路径不满足。不能通过取消结构校验、截取 JSON、修补价格或身份来兼容。

`json_schema` 使用现有的供应商约束子集，本地冻结的完整 Schema 与业务规则仍是导入依据，不因供应商声称严格输出而省略。正常结束必须有完整终止记录；仅有 JSON、`output_text.done` 或输出片段不算生成成功。超时、断连、字节超限保持结果未知，不重发；明确失败或截断不导入。停止接收不保证 CPA 上游停止或不消耗额度。

XAU 离线测试证明客户端每次只发一个固定模型、固定 Bearer 请求；CPA 内部的重试、账号选择、配额切换及 payload 覆盖属于另一层，本阶段未读取其私密配置，不能据此宣称已关闭。真实调用前须在不暴露密钥的情况下确认这些非秘密设置和单账号路由、当时版本、回环监听、Schema 接受性、用量与完整长流行为，并取得单独调用次数/预算授权。现有模型列表与离线模拟均不能证明完整 XAU 分析可用。

官方源码（固定版本）：[Chat 转换](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.30/internal/translator/codex/openai/chat-completions/codex_openai_request.go)、[Responses 转换](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.30/internal/translator/codex/openai/responses/codex_openai-responses_request.go)、[Codex 请求处理](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.30/internal/runtime/executor/codex_executor_request.go)。OpenAI 协议语义参考[结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)，不由 OpenAI 文档推定 CPA 行为。

## 保存中转站和 CPA 两套连接（1.8.0候选功能）

设置页的“已保存配置”保存每套地址、协议、模型、推理、流式、限额和Windows密钥引用。原单配置会自动保留为其原供应商名称，原参数、凭据引用、设置修订和历史验证不改写。尚未安装的源码功能不能由本文推断已在正式入口可用，安装状态以README与任务图当前记录为准。

首次添加CPA：点击“新增配置”，会预填本地CPA推荐值；填写配置名称，检查 `http://127.0.0.1:8317/v1` / Responses / json_schema / gpt-6.1-sol / high / 流式开启，保存为新配置并切换。然后在当前套的隐藏输入中录入 **CPA服务设置的本地访问API Key**，不是Codex账号登录密码或OAuth授权文件。只在本机界面输入，不发送到聊天。新套不会自动沿用原中转密钥，也不自动测试。

以后从下拉框选择中转站或CPA，再点“切换到所选配置”，地址、参数和各自密钥引用一起恢复，不必重填。普通“保存模型配置”和密钥更新只更新当前套，其他套保留；重启后仍可切换。表单或密钥尚未保存时先保存或取消草稿，避免切换丢失输入。模型生成或取消收尾进行中会拒绝切换。

切回未改动的原连接可沿用原字符保真证据和测试时间，不为切换额外调用模型；修改连接身份或密钥后仍须用户主动重新验证。每套的模型列表、余额和测试结果分别展示；较晚返回的查询仍归原套。每日生成次数和已有未知调用不因切换清零。切换不会自动选择账号、协议、模型或失败重试；CPA内部行为仍需另行核对。

CPA Codex路径无法依靠25000输出token字段执行硬上限，600秒、原始流16 MB和最终答案2 MB仍是独立的本机接收限制。保存/切换成功、字符小测成功、完整XAU分析成功是不同证据层级；新套未验证时保持“待验证”。

## API回答紧凑表达（r21）

新API调用记录`api-output/2`，要求减少重复叙述，保留各周期独立判断、证据与反证、风险、条件、时间和经验引用。字数仅为软建议，必要说明可超出，仍接受符合原Schema的详细回答；不会截断、缩写或补改收到的原文。模型、high等推理设置、冻结资料、分析周期、费用/时效及重评规则没有因这项改进改变。人工分析包和旧调用保持原样。

离线验证可证明输入/契约/校验边界保持和原回答兼容，不能证明新模型回答的准确率、判断质量或实际生成耗时不下降。完整真实对照须在同一冻结资料、模型和参数下单独进行，按调用预算授权执行；此次改进不自动调用、重试或取消已有请求。

## 到期复盘增量补算（r19）

“补算到期复盘”默认跳过已完成项；资料不足或顺序不明的项目保存为未知并等待新证据，不再每5分钟循环读取。仅同账户、同品种、同历史区间已保存的评价资料内容确有变化时再核对，当前报价或采集编号/时间变化不会唤醒旧复盘。没有完整历史入场资格轨迹的超短线方案继续保留未知。真实交易仅在成交、费用、关联或来源事实变化时更新；预测专项不附带重算所有交易。普通补算按钮不会强制重复读取资料不足项；它处理新到期与有新证据的项目，已保存未知仍计入原缺失分母。

每项保存检查点，中断后从未完成项继续。结果相同不因新采集编号增加修订，补齐资料产生不同结果时追加修订并保留原结果。刷新/离开再进入历史页，会恢复后台活动补算的数量和阶段；存在活动补算时按钮禁用，避免重复排队。任务结束后复盘列表刷新；无新到期/新资料时后台不应继续生成补算任务。补算不发模型请求，MT5只读。

### 按改动范围选择备份（2026-10-27）

页面文案/样式/纯前端修改不写业务数据时，保留原程序版本和相关检查即可，不机械增加全量数据备份。仅配置修改先保存该项原配置；需要保护数据库写入时使用更新快照。数据库结构/存储格式变更、迁移、清理资料或首次独立备份，使用可独立恢复的完整备份。数据未变化、验证仍适用时不反复做全量校验。

`tooling/backup/manage.py`保留默认full，增加`--mode snapshot`、`--reuse-backup <完整备份目录>`和`--workers 4`。独立标准库入口为`tooling/backup/fast.py`，当前本机可用副本在`D:/Example-Data/tools/backup/manage.py`，无需替换正式运行入口。已经封存候选自带的旧脚本保持原样。

snapshot模式通过只读SQLite连接在线产生一致数据库副本，校验数据库完整性/外键并保留当时全部附件登记；不读取或复制附件内容。生成独立的`snapshot-manifest.json`，明确`self_contained=false`及原附件目录依赖，不能当作灾难恢复备份或删除附件的许可。恢复时逐个检查原附件，缺失/损坏必须失败；仅恢复到新空目录。

full模式仍生成`xau-next-backup/1`。旧完整备份的同路径/同SHA/同长度文件可在备份之间硬链接，校验目标文件SHA256后才计入成功，不重复读取相同实时附件；不能把实时数据当作复用备份，也不把既有实时硬链接继续传播。不能硬链接时校验复制。相同物理路径的重复登记只处理一次，1至8个线程有界并行。源/旧备份/现有目标不可覆盖；失败不生成成功清单。完整备份可脱离旧备份路径恢复，旧平台恢复器兼容。

高频行情存档已采用gzip，较大文档已采用zlib及内容寻址去重。下一步应研究复用跨快照的相同K线和保存变化，保留采集时间、来源、闭柱状态与旧预测的原始引用；不能直接从未显式引用就推断可删除，也不能把降低读取频率当成无损存储优化。当前没有自动保留期、归档迁移、旧数据重压缩或删除授权实现。
