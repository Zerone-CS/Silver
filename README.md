# InferOptimize

用于 vLLM-Ascend 吞吐优化的 skills 和测量工具。核心是固定口径、保存原件、核验变更、比较效果；诊断方法和知识查询按需使用。

```text
确定负载与约束 → 测基线 → 实施候选 → 核验与比较 → 采纳或回退
                            ↑                       │
                            └──── 下一轮实验 ───────┘
```

## 运行一个受控 Round

910C 站点部署、设备预算、完整 Round 和离线复算见 [交付手册](references/delivery.md)。

[操作手册](references/round_control.md)包含初始化、采集、采纳、恢复和预算停止。可先运行下面的回环验收。它启动本机 HTTP 模拟服务，使用合成 trace，贯穿真实 CLI、计分、独立确认、部署核验和经验库。

```bash
python3 -B -m unittest discover -s tests -p test_campaign_integration.py -v
```

正式 A/B 可按逐请求质量、TTFT 和端到端时限复算 goodput，并将其作为主指标或保护项。也可要求 goodput 与原主指标分别达到各自的最小收益，其他护栏照常验收。[goodput 验收规则](references/round_evidence.md#按逐请求-goodput-验收)

## 复算吞吐和定位变化

已有诊断运行时，用 `python3 -B scripts/step_analysis.py run --run "$RUN"` 重算整批有效输出 token/s。传入同次调度、KV 或 trace 原件，可继续查看逐请求和逐步归属、慢步及覆盖缺口。下面的命令在新目录生成 A/B 样例，并打印可以直接运行的比较命令。[指标、样例结果和现场接入](diagnose/references/step_analysis.md)

```bash
python3 -B tests/test_step_analysis_integration.py --make-demo /tmp/throughput-step-demo
```

在 vLLM-Ascend 0.23.0 单引擎上，可用[现场适配器](diagnose/adapters/vllm_023_single_engine/README.md)记录实际调度完成和输出提交，并将引擎的独立迭代计数与客户端最终用量对账。

## 使用

让 agent 从 [主入口](SKILL.md) 开始，并提供实验环境、负载、质量/SLO 和预算。已有授权与实验产物可以复用，恢复时提供 campaign 路径。

> 使用 vllm-ascend-throughput-optimization，在指定服务和空闲设备上优化当前模型的有效输出吞吐。沿用已有负载与约束，完成候选实施、对照验证和必要回退，达到目标或预算后交付可复核结果。

| 入口 | 用途 |
|---|---|
| [evaluate](evaluate/SKILL.md) | 核心工具，采集、检查原始运行、比较吞吐和守卫指标 |
| [diagnose](diagnose/SKILL.md) | 按需解释日志或 trace，提出可检验原因 |
| [optimize](optimize/SKILL.md) | 记录变更、生效证据和实际回退 |
| [knowledge](knowledge/SKILL.md) | 查询参数和源码证据，按需维护知识快照 |

## 工具与维护

在套件根目录运行。evaluate 使用 Python 3.9+ 标准库。

```bash
python3 scripts/verify_suite.py
python3 scripts/run_tests.py --scope evaluate
```

实际测量命令见 [工具手册](evaluate/references/tool_usage.md)，结果判据见 [测量契约](evaluate/references/measurement_contract.md)。长任务与委派分别参考 [状态记录](references/campaign_state.md) 和 [任务接口](references/suite_contract.md)，安装注册见 [运行支持](references/runtime.md)。

完整 Round 的顺序、证据准入、确认与基线晋升由[持久化控制器](references/round_control.md)执行。Agent 准备假设并实施现场变更，可按 next_action 手工驱动或接入站点 worker。套件需要现场服务、负载和必要的质量评估；离线测试只验证工具行为，真实收益通过受约束的重复对照确认。
