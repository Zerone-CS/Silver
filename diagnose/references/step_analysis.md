# 请求、有效输出和引擎 step 的对账

从一次已结束的诊断运行开始。在套件根目录运行以下命令。即使只有 benchmark 原件，第一条也能重算吞吐、逐请求状态、输出长度和完成原因。两条命令均输出 JSON。使用 `--output` 可以写入一个新文件。

```bash
python3 -B scripts/step_analysis.py run --run "$RUN"
python3 -B scripts/step_analysis.py compare --baseline "$A" --candidate "$B" --format markdown
```

## 固定吞吐口径

| 量 | 定义 | 证据 |
|---|---|---|
| N | 终态成功请求的服务端最终 completion_tokens 之和 | 固定负载、运行 manifest 和逐请求记录 |
| T | 从整批开始到整批结束的客户端单调时钟差，单位秒 | 同一次 benchmark 的 start_ts 和 end_ts |
| N / T | 每秒成功交付的输出 token 数 | 可用上述两项独立复算 |
| K | 测量窗口内所有副本各自完成的逻辑 step 数之和，含零产出 step | 每副本唯一 owner、逐步原件和独立的全窗口计数 |
| R、Y_bar | R = K / T，Y_bar = N / K。两者乘积等于 N / T | 还需将全部 N 个最终输出位置唯一关联至对应 step |

并发运行时，请求的单次 E2E 可以互相重叠。整批 T 包括受测窗口内的排队、失败和尾部排空；逐请求 E2E 另作分位数或 SLO。推测生成中的草稿、被拒 token、重算和重复的 TP rank 记录都可能消耗时间，这些工作可单独观测。N 始终取本次成功响应的最终用量。基准支持的输出通道与 EOS 规则随固定的客户端策略一起保存。[测量契约](../../evaluate/references/measurement_contract.md)

若业务另要求正确答案或逐请求时限，传入 `--quality-contract`、`--max-ttft-s` 或 `--max-e2e-s`。此时报告另给 goodput，即同时达标的成功请求所交付的 token 数除以同一个 T。可用 `--latency-origin`、`--first-output-channel` 和 `--token-source` 选择与正式验收一致的口径。若质量契约已冻结本场景的 goodput 规则，诊断自动采用该规则，命令行与契约不一致会被拒绝。使用可信的 `python_score_v1` 时还要通过 `--min-request-score` 指定单题阈值；确切匹配直接按每题答案判定。评分读取原始 benchmark 目录中的请求、SSE 和输出文件。[质量约定](../../references/round_evidence.md)

## 选择观测和验证假设

| 想判断的问题 | 可复核的观测 | 解读条件 |
|---|---|---|
| N 变了，是否完成了同一批工作？ | 逐请求成功/失败、finish reason、输出分布、实际质量或 SLO | 先比较业务请求 ID、配额、结束原因；有相同配额也可能因提前 EOS 得到不同长度 |
| 是否由调度影响有效产出？ | owner 的调度步数与持续时间、按请求提交的输出位置、每步关联的 prefill/decode/draft 工作 | 确认零输出步也进入 K。调度预算、batch size、接受的草稿数均有各自含义 |
| KV cache 是否造成停顿或额外工作？ | KV 块使用与释放、抢占、重算、队列变化；观测字段与采样范围见报告 | 先看时序与请求/step 的对应关系，再通过只改变相关配置的对照检验；占用率极值本身不能给出损失的 token 数 |
| 时间花在哪个调用阶段？ | 慢步的调度、runner、设备、通信、等待区间及来源事件 | 按显式 step 或提交 ID 关联，嵌套与并行活动用区间并集。跨时钟推断需对时证据 |

MS Service Profiler 的原生 CSV 可先做观测。若现场版本还提供引擎 ID、逻辑步或输出提交事件，可用映射接入，生成逐步证据；具体字段和命令见[数据源适配](step_sources.md)。需要追查执行时间时，可将同次封存的 trace 及字段映射传入分析器。[调用时间线](step_timeline.md)

```bash
python3 -B scripts/step_analysis.py run --run "$RUN" --sources "$SOURCES" \
  --bundle "$BUNDLE" --timeline-map "$TIMELINE_MAP" --output analysis.json
```

`coverage` 给出已关联 token、缺口、观测步数与独立计数。缺少字段或窗口覆盖时，报告保留可验证的部分。重复归属、交叠输出位置或文件指纹冲突会导致命令失败。跨运行的 step 编号通常会变化；A/B 只对同一定义、两侧完整且可由相同请求输出位置唯一匹配的 step 报告时长差。提交的工作量和执行路径仍可能不同。

## 离线复算与现场 Round

在一个新目录生成两份可复算的合成运行和源 CSV。脚本打印包含实际路径的比较命令。

```bash
python3 -B tests/test_step_analysis_integration.py --make-demo /tmp/throughput-step-demo
```

| 合成运行 | N | T（s） | K | R（step/s） | Y_bar（token/step） | N/T（token/s） |
|---|---:|---:|---:|---:|---:|---:|
| A | 16 | 10 | 4 | 0.40 | 4 | 1.60 |
| B | 16 | 8 | 2 | 0.25 | 8 | 2.00 |

B 每步的平均有效输出翻倍，每秒完成的逻辑步数降为 A 的 62.5%，二者共同给出吞吐比 1.25。这个例子验证算术、身份和输出守恒。现场用受控 Round 的非 profiling 配对运行与独立确认判定采纳。若要将诊断报告纳入某轮证据，在该轮 A、B 各自的 `campaign.py profile` 提交时传入对应的数据源或时间线映射。[Round 操作手册](../../references/round_control.md)
