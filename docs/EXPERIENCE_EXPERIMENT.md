# 历史经验增量配对验证

这是人工启动的独立验证工具，不是日常必做流程。`init`、`freeze`、`score`、`report` 均不调用模型。只有显式 `run --start-paid-pair` 可能收费，且每次只执行预登记的一对；没有计划次数预算、可用密钥或现有字符保真验证时不发送。

支持原冻结答案1.4/manual-analysis/8与新1.5/manual-analysis/9请求。不会升级原包，已有冻结实验保留原正文和格式。本次兼容验证只使用合成传输，不授予真实配对调用权限。

实验的业务口径只在 [REBUILD_SPEC.md 第8.2.1节](../REBUILD_SPEC.md#821-经验增量配对实验) 维护。本页说明运行方法。当前真实对照效果尚未验证，离线合成通过不能证明预测更准。

## 1. 先审阅并冻结计划

源码使用 `D:\XAU-Next\.venv\Scripts\python.exe`。独立候选使用安装时 `DATA_ROOT\runtime\install.json` 中的 `api_python`，脚本在候选的 `tooling\experiments\experience_pair.py`。不要改用旧日常候选的 Python，以免加载旧代码。

下面示例建议10对、最多20次，按720分钟间隔规划，观察180/360/720分钟；实际周期、起点、配对数和次数上限须按用户选择填写。建议避免重叠，但工具仍记录并合并重叠关系。开始时间应是未来明确的 UTC 时刻。每个槽位选其计划时点后5分钟内的一个新请求；错过则保留未执行，不换一个好看的样本填补。

```powershell
$python = 'D:\XAU-Next\.venv\Scripts\python.exe'
$tool = 'D:\XAU-Next\tooling\experiments\experience_pair.py'
$data = 'D:\Example-Data\daily-v1-storage'
$experiment = 'D:\Example-Data\validation\experience-forward-001'
# 把开始时刻改为实际约定的未来UTC时刻，周期与选定请求一致。
$start = '2026-10-20T00:00:00Z'
& $python $tool init --data-root $data --root $experiment --pairs 10 --horizons 180 360 720 --cohort forward --start-at $start --interval-minutes 720 --call-budget 20 --accept-unknown-cost
```

`init` 从所选真实用量目录读取当前本应用模型配置并冻结；不读取密钥。模型、协议、推理强度、输出格式、流式、输出上限、超时、响应/回答字节上限、单价、币种和日限额保存在 `plan.json`。不擅自更换已验证中转站或协议。`plan.sha256` 用于启动时确认这份具体计划。评分版本及实际代码哈希也在计划内。

价格未知时仅有明确次数限制，`--accept-unknown-cost` 表示接受费用仍未知；这不是零费用。若单价已完整配置，可改用 `--cost-budget 金额`。费用预留沿用现有输入字节数作为输入token保守代理、输出token上限的估算；调用后的费用仍是返回用量乘配置单价，不能替代中转账单。没有填写 `--call-budget` 的计划仅用于审阅，运行会被阻止。

历史冻结回放单独用 `--cohort historical_replay` 建计划。不得将已结束窗口伪称前向验证。合成测试只使用测试代码的 `synthetic` 类别，不混入真实样本。

## 2. 从已有新请求冻结一对

先由正常流程产生新格式 `1.4.0 / manual-analysis/8` 的新分析请求。旧请求保持原样，实验不会升级旧包；本工具也不会为了采样自动创建日常请求。

```powershell
$requestId = '填写实际新请求ID'
& $python $tool freeze --data-root $data --root $experiment --sample pair-0001 --request-id $requestId
& $python $tool report --root $experiment
```

检查 `pair-0001\input-diff.json` 的 `PASS`、两组实际正文、`frozen.json` 中的输入字节代理及费用估算。A保留完整经验，B经验为null并只移除经验专属引用；共享引用仍保留。其它事实、previous_analysis、statistics、时效、提示、Schema及调用参数相同。B可能通过上一版本间接受历史经验影响，本实验只测本次直接加入经验的增量。

两组使用独立实验身份，原请求/预测/市场身份对应关系保存在冻结目录。不会创建正式预测、方案、复盘、通知或自动重评。实验结果不会进入后续正常experience。

## 3. 仅在明确启动后调用指定一对

```powershell
$planHash = (Get-Content -LiteralPath (Join-Path $experiment 'plan.sha256') -Raw).Trim()
& $python $tool run --data-root $data --root $experiment --sample pair-0001 --start-paid-pair --plan-sha256 $planHash
```

调用顺序预先交替AB/BA。每组只有一次尝试，各自发送独立HTTP请求，不交换回答。第一组失败就停止另一组，剩余组记录未发送。拒绝、截断、超时和未知均保留；没有自动重试、自动补样或定时任务。

调用前在同一个真实数据库事务内检查日限额及实验总限额并预留，实验用途的调用和实际用量单独登记。即使复制实验目录，也不能重发同一实验身份。配置在两组之间改变时停止未发组。预留后中断按未知处理，目录不覆盖，不能删除标记后盲目重跑。确需再次试验应另建预登记计划并重新明确预算。

`A/B-body.json` 是实际交给HTTP客户端的正文；`A/B-raw.bin` 保留原响应字节（若回显本应用密钥则脱敏并拒绝有效回答，保留原字节哈希）；`A/B-answer.json` 是提取原文，`A/B-result.json` 记录首次结构/业务校验、引用错误、可用时间、延迟、用量、估算费用及未知状态。

## 4. 取得同源闭柱后离线评分

输入同账户、同品种、同来源的行情JSON，结构沿用 `MarketSnapshot`：保留 `snapshot_id`、`account_scope_id`、`symbol`、`source_kind`；`series` 中M1须明确 `price_basis=bid`、状态和每根柱的开闭时间、OHLC、closed及实际可用时间（若有）。真实来源身份不能改写为合成或反向改写。不能用未来数据填补原始缺口。

```powershell
$marketFile = 'D:\Example-Data\validation\same-source-closed-m1.json'
$asOf = '填写证据实际可用的UTC时间'
& $python $tool score --root $experiment --sample pair-0001 --actual-market $marketFile --as-of $asOf
& $python $tool report --root $experiment
```

评分存入新的 `score-*` 目录，实际行情和原回答哈希一起保存；不修改原回答。支持原完整窗口描述性、各自可用后、共同可用后三种范围，终点保持原值。共同起点取两组较晚可用时间；无剩余窗口不可评分。中性带固定0.00；闭柱非分钟边界按原客观算法裁剪，缺口和重复保持未知。

报告按周期列方向正确/可评分/预登记分母，同时列上下界绝对误差、区间覆盖和预测宽度。支持的阶段指标、转折命中和原冻结基准在JSON中完整保留；精确转折时间/价格误差和阶段顺序评分未支持。可用后基准方向因原锚点不同不评分。没有综合总分，也不用模型自信代替真实结果。

每次报告均保留全部预登记样本，包括没有输入、没有调用、失败和未知。若补充客观行情形成新评分，报告固定取最新记录的证据修订，其余修订保留，不按表现选择版本。报告同时保留逐对方向比较和重叠组内平均差异；技术重复、同轮版本和重叠窗口不当作新的独立样本。10对主要检验流程，不能据此宣称稳定优越性。
