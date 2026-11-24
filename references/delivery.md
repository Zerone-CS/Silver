# 910C 交付与复算

使用 Python 3.9 或更新版本运行套件。910C 站点入口使用 Linux、Docker、`npu-smi` 和 `fuser`。先使用 [发布校验](runtime.md) 确认套件完整。以下命令使用已有的授权实验目录和可独占的设备。

## 冻结站点与实验

复制 [站点配置](../assets/site-910c.example.json)，填入真实的板卡、芯片、物理设备、回环端口、模型路径与镜像 ID。模型权重目录只读挂载。此站点入口验证 vLLM-Ascend 0.23.0 单引擎、配置生效、模型和设备进程归属。站点示例使用 Qwen3-0.6B 和指定的 no-think 模板。安装项目时提供与模型匹配的模板。工作目录内默认读取 `cases/no-think.jinja`，也可供应 `--template`。

初次初始化时，站点将模型目录内的所有普通文件（包括权重分片及分词器）复制到按完整文件清单 SHA-256 命名的快照。指向普通文件的符号链接会复制为独立文件。默认存储位置是站点工作目录同级的 `.inferopt-model-snapshots/`，可在站点 JSON 中用绝对路径 `model_store_dir` 指定。快照应放在可信、支持 POSIX 文件状态记录的文件系统上，位于实验目录和源模型目录以外。首次为一个新版本准备约一份模型大小的额外空间，内容相同的站点可共享快照。每个部署都只读挂载快照，核对实际挂载、清单与文件状态，状态变化时重算内容哈希。部署回执 `model-snapshot.json` 和实际采样环境保存文件指纹。旧站点再次采集时先核查原环境记录的模型指纹，新身份进入新采集的环境原件。

准备原始请求、gold、[比较契约](round_evidence.md)和三份输入文件。配置、质量和计量文件均在启动前冻结。

- `case-settings.json` 记录 benchmark 和热身设置。每个场景用 `dataset` 引用请求集，指定 `concurrency`，可加 `workers`、`mode`、`seed`、`temperature`、`top_p`、`timeout` 和 `warmup_rounds`。在 open loop 下，发压文件还须提供实际到达计划。
- 每个场景可用 `warmup_dataset` 提供独立 JSONL。该字段含 `path` 和 `sha256` 两个键，路径相对 case-settings 文件，摘要可用 `sha256sum warmup.jsonl` 生成。每条预热请求提供 `request_id`、`prompt_text` 和正整数 `output_tokens`。每轮按场景并发发完整个预热集。可以提供独立请求、与正式流量共享指定比例前缀的请求，或冻结的热流量重放样本，并在场景描述中写明重叠范围。仅设正数 `warmup_rounds` 时沿用原规则，每轮选择正式数据集的前 `concurrency` 条。将其设为 0 并省略 `warmup_dataset` 时，服务就绪后直接进入测量，代表无业务请求预热的场景。
- 可在 `warmup_dataset` 中加入 `"shared_prefix":{"characters":64,"matching_requests":8}`，要求正式请求中恰有 8 条与预热集内某条请求共享前 64 个 Unicode 字符。采集前验证该条件，离线验收用正式 workload 和预热原件复算命中条数与分母。此比例描述提示文本，实际 KV 缓存命中仍需引擎计数器或 trace 验证。
- 采集器保存实际预热输入和逐轮回执，将预热模式、轮数、并发和语料指纹写入该场景的计量身份。正式 A/B 与 A/A 的预热条件须匹配。不同策略可以预登记为不同场景。
- `metric.json` 描述稳定的批次计量规则。可从 [计量契约示例](../evaluate/assets/metric_contract.example.json) 开始。
- `proposal.json` 保存本轮的场景、两个配置键、假设、预测与恢复说明。可为诊断声明 `profile_requirements`。每项要求包含 `id`、`field`、`pattern` 和 `min_count`，并按 rank 检查。

```json
{"schema_version":1,"scenarios":{"c8":{"dataset":"cases/c8.jsonl","concurrency":8,"warmup_rounds":1}}}
```

```json
{"schema_version":1,"scenario":"c8","baseline":"A","candidate":"B","hypothesis":"并发 8 时基线槽位不足","prediction":"调度上限 8 能提高 goodput 并守住原吞吐和尾延迟","change":"change.txt"}
```

## 校准并执行一轮

先在授权主机上的空闲设备完成预检。例中的预算单位是单 die 秒，数值应依据实际启动、采集、确认和释放耗时事前冻结。站点初始化写入 A 和 B 的配置。本例中唯一差异是序列上限 4 和 8。其他配置键使用同一规范，在候选登记前写入相应的 `config-KEY.json`。

```sh
R=/data/infer-opt/round-001
python3 -B scripts/site_910c.py init --root "$R" --site "$R/site.json"
python3 -B scripts/device_budget.py init --db "$R/leases.sqlite" --limit-die-seconds 4200
for i in 1 2 3 4; do
  python3 -B scripts/site_910c.py measure --root "$R" --site "$R/site.json" \
    --case "$R/case-settings.json" --metric "$R/metric.json" --scenario c8 \
    --config-key A --budget "$R/leases.sqlite" --action "aa-$i" --run "$R/aa/run-$i"
done
python3 -B scripts/site_round.py --root "$R" --site "$R/site.json" \
  --case "$R/case-settings.json" --metric "$R/metric.json" --contract "$R/contract.json" \
  --campaign "$R/campaign" --budget "$R/leases.sqlite" --proposal "$R/proposal.json" \
  --baseline-run "$R/aa/run-1" --baseline-run "$R/aa/run-2" \
  --baseline-run "$R/aa/run-3" --baseline-run "$R/aa/run-4" \
  --scope 'Ascend 910C single engine, Qwen3-0.6B, c8' --experience-db "$R/experience.sqlite"
```

`site_round.py` 从已核验的 A/A 运行建立 campaign，分别采集 A 和 B 的诊断运行，执行已登记的筛选及独立确认。最后启动交付配置并做在线健康检查，再释放实验容器和设备。成功关闭后在 campaign 的轮次目录查看 `round-export.json`。每次租约的启动、预热、失败、清理和释放回执保存在同一工作区。正式运行的逐请求尾延迟报告与部署回执一起归档。用 `--goal-tps` 设置绝对目标，默认指标为 goodput。多轮任务设定 `--max-rounds`，下一轮由 Agent 提交新提案，并使用已晋升部署的 A/A 原件。

证据无效时保留本次目录，使用 [Round 控制器](round_control.md) 登记替换批次。租约未决时，先核对它记录的操作 ID、主机和实例，再在同一次主机启动中复核设备。以下命令只处理本站点标签和当前租约匹配的容器。

```sh
python3 -B scripts/device_budget.py status --db "$R/leases.sqlite"
python3 -B scripts/site_910c.py reconcile --root "$R" --site "$R/site.json" \
  --budget "$R/leases.sqlite" --action "$PENDING_ACTION"
python3 -B scripts/device_budget.py audit --db "$R/leases.sqlite"
```

### 结轮与设备释放

910C 执行器在实验实例在线时核验配置和健康，保存本轮决策及最后观测到的实例。此时导出中的 `site_release.status` 为 `pending`。停止实验容器并复查设备后，控制器从登记的预算账本核对同一动作、主机启动、设备、容器及原始释放证明，更新为 `released`，再写最终导出和经验索引。生产服务上线可依据单独的部署流程验收。

中断后先看 `campaign.py status` 的 `next_action` 及 `site_release.action_id`。设备账仍有未释放动作时，用上面的 `reconcile` 命令核实现场并补挂回执。设备已归还时可重新执行原 `site_round.py` 命令完成导出，也可执行 `campaign.py attach-release --campaign "$R/campaign" --round N` 补挂。重复补挂对同一证明返回同一结论。旧导出缺少 `site_release` 时，现场释放情况需查当时站点原件。

诊断或在线收尾失败后，先核实上次租约已释放，再次执行原站点命令会使用新的动作编号和产物目录。已收集且有效的诊断回执可以复用。每个诊断侧和在线收尾分别沿用 campaign 的 `max_retries` 限制，默认各允许一次额外尝试。正式 A/B 批次继续沿用已登记的配对和替换规则。

## 离线验证

### 复核整个轮次

在原件仍可读时，为新版本登记并关闭的轮次执行以下命令。若 gold、评分脚本或其他输入存于另一目录，重复传入 `--source-root`。输出目录须在所有来源根之外。复制生成的整个目录和对应的套件发布版后，即可在另一台机器执行最后一行。评分契约使用自定义 Python 程序时，应在可信环境提供其运行依赖。

```sh
BUNDLE="${R}-round-evidence"
python3 -B scripts/evidence_bundle.py pack-round \
  --round-export "$R/campaign/round-001/round-export.json" \
  --source-root "$R" --output "$BUNDLE"
python3 -B scripts/evidence_bundle.py verify --bundle "$BUNDLE"
```

查看 `stage_recommendations` 可区分筛选和独立确认的结果。`comparison` 说明是否从原件重新计算了正式对照。`decision` 是当时记录的采纳、回退或放弃决定，`final_health` 核对该决定对应配置的健康回执，`device_release` 单独报告最终收尾租约的状态。已核验释放为 `released`，等待回执为 `pending`，未登记站点释放信息为 `unverified`。这些字段描述实验结束时的观测。包内保留诊断 trace、逐请求原件和登记的输入文件，模型权重通过部署回执中的文件清单核对。

### 复算指定 A/B 对照

旧轮次缺少整轮验收所需的阶段绑定，可以用原发布版复算当时保留的对照。已有证据包使用其清单指定的评估器版本。对照证据包由 JSON spec 描述来源根和需要复算的对照。每个 `roots` 元素声明 `origin` 及本地 `source`。每个 `checks` 元素含 `name`、`contract`、`pairs`、`baseline_identity` 和 `candidate_identity`。文件地址使用归档前的绝对路径，并由来源根映射到包内。可额外填写 `expected_report`，以核对历史裁决。spec 的 `schema_version` 为 1。

```sh
BUNDLE="${R}-evidence-bundle"
python3 -B scripts/evidence_bundle.py pack --spec "$R/replay-spec.json" --output "$BUNDLE"
python3 -B scripts/evidence_bundle.py verify --bundle "$BUNDLE"
```

输出目录应位于所有来源根之外。包内写入发布版 ID、清单指纹、原件哈希和独立复算结果。移动整个证据包和对应发布版后可以再次执行 verify。包内含请求及响应原件，归档到经授权的受控目录。

单次运行的 SSE 分段和引擎证据对齐详见 [流式终态分析](../diagnose/references/stream_tail.md)。经验检索的预算对照详见 [经验库盲测](../knowledge/references/experience_study.md)。

## 比较原件损坏时交付基线恢复证明

回合以 `revert` 或 `abandon` 关闭后，可以单独交付基线恢复凭据。默认的完整导出仍会复核已登记的比较原件。使用以下命令可保留关闭时的说明、已记录的停止原因和诊断复核错误，并携带恢复时的部署观测、同回合健康回执及已登记的设备释放证据。

```sh
python3 -B scripts/campaign.py export --campaign "$R/campaign" --round 1 --recovery-only > "$R/recovery.json"
python3 -B scripts/evidence_bundle.py pack-recovery --recovery-export "$R/recovery.json" \
  --source-root "$R" --output "${R}-recovery-bundle"
python3 -B scripts/evidence_bundle.py verify --bundle "${R}-recovery-bundle"
```

重放输出 `kind=baseline_recovery` 和 `comparison=not_verified`。可对外说明该回合在健康检查时恢复了登记基线，性能收益仍缺乏验证。设备结论单列在 `device_release` 中。`released` 表示已复核本次回收凭据，`pending` 表示仍待补挂，`unverified` 表示未登记现场释放证据。补挂完成后可重新导出到新目录。
