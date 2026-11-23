# 可恢复的吞吐优化 Round

本版本适用于一个已授权的 vLLM-Ascend 服务端点和一套固定请求集。控制器管理实验资格、预算、基线和记录。现场 Agent 或站点 worker 负责读取实际生效配置、启停 profiler、实施和恢复服务。服务的启动方式、主机、容器和设备由现场决定。

## 冻结任务

将实验目录放在套件以外。SQLite 控制库放在同一台控制主机的本地文件系统。压测、证据校验和 worker 需要访问一致的原件路径。大 trace 可以存到单独的持久卷。每次采集分配新目录，保留失败产物。控制器以绝对路径和 SHA-256 连接原件，备份时同时保留数据卷、状态库和可重建的路径映射。

在运行前定义请求集、发压方式、测量窗口、质量规则、主指标、保护项、所需重复数、最小收益和预算。固定批次吞吐的分子为成功请求最终服务端 completion tokens 总数，分母为客户端单调时钟上的整批墙钟秒数。候选不能通过换并发、缩短输出、放宽质量门槛或只统计部分窗口获得可比性。每个场景使用一个通过质量与生效核验的初始运行冻结实际 case。计分、正确性和原件的要求见[正式验收](round_evidence.md)。

当业务需要比较在时限内交付的合格输出时，在初始化前为场景声明逐请求质量和 TTFT、端到端时限。发现与独立确认均从正式运行原件复算 goodput。它可用作主指标或护栏。也可加 `goodput_primary`，要求 goodput 和原主指标分别通过各自的最小收益门槛，并继续核验全部保护项。[goodput 契约](round_evidence.md#按逐请求-goodput-验收)

可用[三道算数题](../assets/round-exact.workload.jsonl)、[答案](../assets/round-exact.gold.json)和[比较规则](../assets/round-exact.example.json)打通现场流程。业务实验要替换请求、评分器及阈值。初始化前先按[测量工具手册](../evaluate/references/tool_usage.md)采集基线，用[现场配置记录格式](round_evidence.md#采集前记录实际部署)为 prepare_metadata 提供真实日志和解析后的配置。

以下命令在套件根目录运行。示例中 `case-A` 是初始 benchmark 目录，`C` 是此次 campaign 的控制目录。所有需要声明为正式运行目的地的路径均使用绝对路径。

```bash
C=/data/infer-opt/campaign-001
python3 scripts/campaign.py init --campaign "$C" \
  --contract /data/infer-opt/comparison.json --baseline-run /data/infer-opt/case-A \
  --scope '模型与构建、NPU 型号及拓扑、负载、部署适用条件' \
  --max-rounds 8 --hours 12
python3 scripts/campaign.py status --campaign "$C"
```

多场景时重复 `--baseline-run`。可设 `--goal-scenario S --goal-tps T --goal-metric goodput` 指定合格输出吞吐的绝对目标，目标场景须定义逐请求 SLO。省略指标时沿用成功输出吞吐目标。两种目标的单位均为 token/s。若每个场景都有至少 min_pairs 份初始运行，均通过质量及绝对保护约束，且目标场景每次都已达标，则直接结束。单份基线用于冻结 case。搜索模式要求筛选与独立确认两批对照。用户已事前指定唯一候选时，可在 init 指定 `--single --max-rounds 1` 使用一次预登记的正式对照。每批仍按契约完成规定的重复和保护场景。

## 一轮的实际操作

下表中的动作都带 `--campaign "$C" --round N`。控制器会拒绝与当前阶段不相符的动作。目录、用户数据、端点和适用范围在实验期间保持可用。

| 下一步 | Agent 或 worker 的动作 | 控制器核验 |
|---|---|---|
| begin | 选本轮要诊断的既有场景，提前声明 trace 所需 rank、目标窗口和事件。提交从 profile spec 提取的策略 | 轮号连续，上一轮已收尾，预算尚可用 |
| baseline_profile | stamp 基线的诊断 metadata，用它运行 benchmark，并采集/导入同次导出的 trace。用 profile 提交 bundle | 基线身份、阶段绑定、case、部署证据、rank 和窗口内事件 |
| plan | 引用基线观测，写下可证伪的假设、预期变化、全部改动和恢复方法，提交候选配置/实现 | 已验收基线诊断；固定候选身份和变更记录 |
| candidate_profile | 实施候选，取得现场生效证据。按基线同一策略 stamp、采集并提交 B 的诊断 bundle | 被测部署等于候选，策略与基线一致 |
| schedule | 为每个必测场景和每个配对提前登记两侧全新的原件目录 | 场景、配对数、唯一目录；清单冻结 |
| assess | 每个槽位单独 stamp 正式 metadata。关闭 profiler，预热并在指定绝对目录收集 A/B。提交原配对清单 | 原件重放、质量评分、启用时复算 goodput、身份、口径、效应区间及全部保护项 |
| schedule_confirmation / confirm | 搜索模式下，对有希望的候选登记另一份清单，用新 metadata 和新运行独立复核 | 发现与确认两批的阶段、目录、run ID 和请求记录相互独立 |
| accept / restore | 将服务置于要交付的版本，现场采集新观察，执行健康请求，然后 close | 配置、实现、端点、模型及本阶段登记的健康原件匹配；事务提交决策和基线 |

除正式采纳外，可以在任何活动阶段用 `close --decision abandon` 结束这次尝试。已经形成候选也可用 `revert`。两者都需要先核实原基线已在现场运行。证据只支持放弃某个候选时，可写明观察和成本原因。失败、中止和恢复记录也可以进入经验库。

下面示范一轮中的工具衔接。`$D` 为本轮不可覆盖的产物目录，`$N` 为轮号。diag 模式和 performance 模式的 metadata 分开准备。`$D/spec-A.json` 的 diagnostic_run 和 traces 写实际路径。候选侧按相同方式生成 spec-B 和 bundle-B。[profiling 适配及样例](../diagnose/references/profile.md)

```bash
python3 diagnose/scripts/profile.py policy --spec "$D/spec-A.json" > "$D/policy.json"
python3 scripts/campaign.py begin --campaign "$C" --round "$N" \
  --scenario arithmetic_smoke --profile-policy "$D/policy.json"
python3 scripts/campaign.py stamp --campaign "$C" --round "$N" \
  --metadata "$D/A-diagnostic.json" --output "$D/A-diagnostic-bound.json"
# 现场运行 benchmark，使用 A-diagnostic-bound.json，采集同一运行的 trace
python3 diagnose/scripts/profile.py capture --spec "$D/spec-A.json" --out "$D/bundle-A"
python3 scripts/campaign.py profile --campaign "$C" --round "$N" --bundle "$D/bundle-A"
python3 scripts/campaign.py plan --campaign "$C" --round "$N" \
  --hypothesis '本轮观测支持的瓶颈' --prediction '若解释成立会变化的指标' \
  --config "$D/B-effective-config.json" --change "$D/change-and-restore.txt"
# 现在实施 B，stamp 候选的诊断 metadata，采集并封存 bundle-B
python3 scripts/campaign.py profile --campaign "$C" --round "$N" --bundle "$D/bundle-B"
python3 scripts/campaign.py schedule --campaign "$C" --round "$N" --evidence "$D/discovery.json"
python3 scripts/campaign.py stamp --campaign "$C" --round "$N" \
  --metadata "$D/A-performance.json" --run-dir "$D/runs/A01" --output "$D/A01-bound.json"
# 每个槽位分别 stamp，传给 benchmark --metadata，并在相同的 --run-dir 下采集
python3 scripts/campaign.py assess --campaign "$C" --round "$N" --evidence "$D/discovery.json"
```

采用导入模式时，spec 需要声明可核验的 run_binding。诊断 stamp 生成唯一 profile_capture_id，采集作业须将它写入每个 rank 的 trace 指定字段。已有工具无法写入同一标记时，使用 capture 的现场命令模式生成新的 benchmark 和 trace。相同原件不能在多个阶段重复使用。

需要跟踪每轮有效输出归属或慢步时，在 A、B 各自的 `profile` 命令上加 `--step-sources 文件` 或 `--timeline-map 文件`。若映射引用原始 SSE，再给 `--diagnostic-run 原始benchmark目录`，其四份核心原件须与 bundle 相同。单侧报告保存在 campaign 的 `diagnosis/`，两侧均登记后另存对照。结轮导出中的 `evidence.profiles` 和 `evidence.step_comparison` 给出路径与 SHA-256。[逐步诊断手册](../diagnose/references/step_analysis.md)

若返回 `schedule_confirmation`，换用新的 confirmation.json、目录与 metadata，重复 schedule → stamp → benchmark → assess。若某批因中断或证据无效需要替换，用 `retry --reason 原因 --evidence 新清单`。控制器会先复验旧批次，完整有效的批次必须先记录结果；无效批次保留原清单与缺失原因，每轮额外次数由 init 的 `--max-retries` 限定，默认一次。诊断采集失败可用新目录补采。时间预算耗尽后只允许收尾。

## A/A 校准与平衡确认

先在固定场景里用两次以上独立启动收集 A/A 原始运行，一次部署每场景提交一批。保持客户端、部署和预热策略相同。校准脚本核验配置身份、质量、绝对护栏及运行归属，报告 A/A 中位值和相对极差。确认默认至少四个配对，超过契约最小收益门槛的一倍、两倍时分别建议增加两对。确认配对数保持偶数。这是按可观测波动安排重复次数的保守规则，区间解释仍需要现场独立性与环境稳定性的证据。

```bash
python3 scripts/trial_plan.py calibrate --contract "$CONTRACT" --out "$D/aa.json" \
  --baseline-run "$AA_1" --baseline-run "$AA_2"
python3 scripts/trial_plan.py plan --contract "$CONTRACT" --out "$D/screen" \
  --stage screen --calibration "$D/aa.json" --seed 17
python3 scripts/campaign.py schedule --campaign "$C" --round "$N" \
  --plan "$D/screen/plan.json" --evidence "$D/screen/pairs.json"
```

执行器按 plan.json 的 `slots` 顺序运行。每个 slot 指定场景、配对号、A/B 左右身份、冻结路径和顺序。在同一台 Linux 客户端的同次启动中测量，等待上一个槽结束后开始下一个。确认阶段用 `--stage confirm`、相同已核验 A/A 和独立输出目录重新生成计划。确认次数随筛选计划一起冻结。需要更多配对时，在筛选计划里提前登记 `--future-confirm-blocks`，并为确认计划指定相同的 `--blocks`。每个确认槽位必须获得全新的已核验部署实例。控制器从原运行检查顺序、非重叠、配对关联和部署实例是否复用。A/A、筛选和确认的 run ID 与证据均保留。初始化时增加 `--require-trial-plan` 可以强制完整计划。计划试验采用筛选和确认两阶段。

## 设备占用预算

在站点控制机初始化账本，并在 campaign init 带上 `--device-budget "$BUDGET"`。可用 `--minimum-device-seconds` 指定启动新阶段的最低剩余 die 秒。阶段内每次部署的占卡和确认预留由站点 worker 核验，墙钟限制仍可通过独立的 `--hours` 指定。

```bash
python3 scripts/device_budget.py init --db "$BUDGET" --limit-die-seconds 1800
python3 scripts/device_budget.py status --db "$BUDGET"
python3 scripts/device_budget.py audit --db "$BUDGET"
```

站点在独占锁内先登记唯一动作 ID、主机、boot ID、设备、占用起点、预计上界和预留，再启动服务。释放前完成清理和空载复查，保存带同一主机、boot 与设备身份的原始回执。计费取同一主机单调时钟差乘设备数。失败与超时也计实际占用。若清理超过预计上界，账本记录超支。遗留占用须现场清理和补齐回执，再通过 driver 的恢复入口继续操作。

## 核实实际部署、停止和恢复

若登记过的逐步证据后来缺失或变更，采纳与导出会被拒绝。现场仍可恢复基线，完成部署观察与健康核验后回退或放弃本轮。系统在关闭记录中保留 `step_analysis_review_error`，补回内容一致的证据后可重新导出。

现场提供一个只读观察命令，标准输出是[规范化部署观察 JSON](round_evidence.md#采集前记录实际部署)，包括实际 PID/启动身份、端点、模型和生效配置。它应在执行时读取真实进程、服务或已认证的管理接口，并区分重启前后的实例。适配器和现场设备归属是可信输入；仅复制计划配置会失去此项保障。

```bash
python3 scripts/campaign.py observe --campaign "$C" --round "$N" \
  --output "$D/final-observation.json" -- /path/to/site-read-live-config
python3 scripts/campaign.py health --campaign "$C" --round "$N" --side candidate \
  --deployment "$D/final-observation.json" --output "$D/health.json"
python3 scripts/campaign.py close --campaign "$C" --round "$N" --decision adopt \
  --deployment "$D/final-observation.json" --health "$D/health.json" --observation '本轮实测结果和适用范围'
python3 scripts/campaign.py export --campaign "$C" --round "$N" > "$D/round.json"
```

回退时先在现场恢复 A，health 的 side 选 baseline，close 的 decision 选 revert 或 abandon。observe 直接运行 argv。health 向固定端点发送一次非流式 chat 请求，API key 从 `--api-key-env` 指定的环境变量读取，默认 OPENAI_API_KEY。用单独的新文件保存每次回执。正式比较通过、当前部署和服务健康是三项分别核验的事实。

`status` 给出待做步骤、基线、候选、已验收的 profiles、已登记计划和最后核实的部署。`history` 记录操作及拒绝原因。重复提交使用同一轮号，数据库事务会阻止跳阶段及重复晋升。接手后先核实服务现场；已有完整目录可继续验收，半完成目录保留并按上节重试。目标、轮数、截止时间或用户明确停止均可收尾；调用 `stop --reason 理由` 登记人为停止。活动轮仍需恢复并核实部署。

无人值守运行使用同一台控制机的驱动。worker 收到状态 JSON 作为标准输入，按 next_action 执行一阶段工作，通过上述 CLI 提交结果后退出。它可以调用现有 Agent 或站点作业系统。站点负责实际授权、设备预留和远程作业状态；同台控制机上的协作 driver 须为相同硬件传入同一锁路径。

```bash
python3 scripts/drive_campaign.py --campaign "$C" --resource-lock /data/infer-opt/locks/devices-0-1.lock \
  --worker-timeout 3600 -- /path/to/authorized-site-worker
```

driver 持有 campaign 锁及资源锁，逐次记录请求、输出、退出结果。异常中断、超时或无状态进展会留下未决动作。先核实该动作是否已经操作服务，再使用 `--resume-after action-目录名` 接续，避免对一次结果未知的部署动作盲目重发。

## Git 与两类知识

代码候选在被测源码仓库中保留可重建提交或完整补丁，记录构建和实际加载身份；纯参数候选保留规范化配置、差异与恢复指令。在 plan 加 `--git-repo /path/to/source --git-rev REV` 可解析并记录实际存在的 commit 和 tree。将补丁和构建原件放进 change 记录，现场生效仍用独立观察核实。采纳版本的 Git 提交和结轮导出相互引用。原始 trace、请求、日志存入受控持久存储，Git 保存可发布的清单及摘要。控制器不会代替现场决定哪些经授权的数据可以提交。

[参数知识库](../knowledge/SKILL.md)按源码版本查询参数含义。完成的 Round 可用 [experience](../knowledge/references/experience.md) 纳入跨任务私有经验索引。保留实测结果、适用条件、尚待验证的机制及后续反证；下次任务仍按新 case 检验收益。
