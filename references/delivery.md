# 910C 交付与复算

使用 Python 3.9 或更新版本运行套件。910C 站点入口使用 Linux、Docker、`npu-smi` 和 `fuser`。先使用 [发布校验](runtime.md) 确认套件完整。以下命令使用已有的授权实验目录和可独占的设备。

## 冻结站点与实验

复制 [站点配置](../assets/site-910c.example.json)，填入真实的板卡、芯片、物理设备、回环端口、模型路径与镜像 ID。模型权重目录只读挂载。此站点入口验证 vLLM-Ascend 0.23.0 单引擎、配置生效、模型和设备进程归属。站点示例使用 Qwen3-0.6B 和指定的 no-think 模板。安装项目时提供与模型匹配的模板。工作目录内默认读取 `cases/no-think.jinja`，也可供应 `--template`。

准备原始请求、gold、[比较契约](round_evidence.md)和三份输入文件。配置、质量和计量文件均在启动前冻结。

- `case-settings.json` 记录 benchmark 和热身设置。每个场景用 `dataset` 引用请求集，指定 `concurrency`，可加 `workers`、`mode`、`seed`、`temperature`、`top_p`、`timeout` 和 `warmup_rounds`。在 open loop 下，发压文件还须提供实际到达计划。
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

## 离线验证

证据包由一个 JSON spec 描述来源根和需要复算的对照。每个 `roots` 元素声明 `origin` 及本地 `source`。每个 `checks` 元素含 `name`、`contract`、`pairs`、`baseline_identity` 和 `candidate_identity`。文件地址使用归档前的绝对路径，并由来源根映射到包内。可额外填写 `expected_report`，以核对历史裁决。spec 的 `schema_version` 为 1。

```sh
BUNDLE="${R}-evidence-bundle"
python3 -B scripts/evidence_bundle.py pack --spec "$R/replay-spec.json" --output "$BUNDLE"
python3 -B scripts/evidence_bundle.py verify --bundle "$BUNDLE"
```

输出目录应位于所有来源根之外。包内写入发布版 ID、清单指纹、原件哈希和独立复算结果。移动整个证据包和对应发布版后可以再次执行 verify。包内含请求及响应原件，归档到经授权的受控目录。

单次运行的 SSE 分段和引擎证据对齐详见 [流式终态分析](../diagnose/references/stream_tail.md)。经验检索的预算对照详见 [经验库盲测](../knowledge/references/experience_study.md)。
