# 正式 Round 的验收证据

控制器以 [round_evidence.py](../scripts/round_evidence.py) 重建每组对照。运行者提供原始目录、部署证据和冻结的比较契约。验收器复用 [单次校验](../evaluate/scripts/validate_run.py) 和 [配对比较](../evaluate/scripts/compare_trials.py)，重放成功请求的 SSE，按冻结的质量规则评分。比较器给出的建议、实际部署和控制器是否晋升分别记录。

## 事先固定 case 与质量规则

[可运行的比较契约](../assets/round-exact.example.json) 附有 [三题算数 workload](../assets/round-exact.workload.jsonl) 和 [冻结答案](../assets/round-exact.gold.json)。收集时取场景名 `arithmetic_smoke`、闭环模式和上述 JSONL。三题输出与少量配对适合验证本地管线。正式实验按实际负载重新约定最小收益、请求规模、时延约束与重复数。

在 [原生比较契约](../evaluate/assets/comparison_contract.example.json) 的顶层加入 `quality_policy`。每个性能或保护场景都需要一项规则。下面是一个完整的小规模契约，示例阈值用于演示。将 `reference` 替换为真实金标准文件的引用。路径相对契约所在目录，SHA-256 对原始文件字节计算。

```json
{
  "schema_version": 1, "min_pairs": 2, "confidence": 0.95, "bootstrap_samples": 1000, "seed": 42,
  "scenarios": {"short": {
    "primary": {"metric": "output_token_throughput", "direction": "higher", "min_relative_improvement": 0.03},
    "guardrails": [{"metric": "failure_rate", "max_value": 0.0}]
  }},
  "quality_policy": {"scenarios": {"short": {
    "method": "exact_match", "field": "content",
    "reference": {"path": "short.gold.json", "sha256": "<文件 SHA-256>"}
  }}}
}
```

若两个请求的正确输出分别为 `4` 和 `Paris`，对应文件可写为 `{"arithmetic-01":"4","capital-01":"Paris"}`。键必须精确覆盖同一 workload 的请求 ID。可选 `field` 为 `content` 或 `reasoning_content`。`exact_match` 要求全部正确。`exact_match_rate` 另需 `min_fraction`（大于零、至多一），允许预定比例的错误。分母包含失败请求，字符串按原样比较。质量结果只适用于预定的样本、输出字段与评分规则。任务有多种正确表述、数值误差或结构化输出时，可选择下文的任务评分程序。

以下命令在仓库根目录运行，标识可直接填入 campaign 协议。

```bash
python3 -B scripts/round_evidence.py artifact-ref /campaign/short.gold.json
python3 -B scripts/round_evidence.py describe-run /campaign/runs/short-baseline-calibration
```

`describe-run` 根据校验通过的运行给出 `identity` 与逐场景 `case_identity`。每个场景固定 workload、环境、计量契约、客户端策略、采集器五个 hash 以及测量模式。运行时还需一致的预热、负载干扰控制与设备归属。对大量候选进行搜索时，从未参与选择的运行对独立确认最终候选。

## 按逐请求 goodput 验收

在正式契约顶层加 `goodput_policy`，为需要计算 goodput 的场景冻结两项逐请求时限，单位为秒。以下片段适用于上面的 `short` 场景。

```json
{
  "goodput_policy": {
    "scenarios": {
      "short": {"max_ttft_s": 0.5, "max_e2e_s": 2.0}
    }
  }
}
```

一个请求只有在终态成功、质量达标、TTFT 和端到端时延都不超过各自上限时，其全部服务端最终 `completion_tokens` 才计入分子。两项时延都从实际发起请求计时。缺少首个输出时间的请求不计入，达到阈值恰好相等的请求仍可计入。分母沿用整批墙钟，包含批次内所有请求的等待和执行时间。

- `exact_match` 和 `exact_match_rate` 以每条请求的指定输出字段和金标准精确匹配作为逐请求质量条件。
- `python_score_v1` 在该场景的 `goodput_policy` 中另设 `min_request_score`，取值大于 0 且至多为 1。逐条得分达到此下限即通过逐请求质量条件，原质量规则的 `min_mean_score` 仍参与整个 run 的验收。
- 每个启用的场景都可在 `primary` 或 `guardrails` 中写 `"metric": "goodput"`。只配置 `goodput_policy` 时，报告给出逐运行 goodput，采纳依据已选指标。报告同时保留成功输出吞吐，质量、失败率、p99 及其他护栏继续按契约验收。

如需同时证明原主指标和 goodput 受益，可在场景中保留原 `primary`，并增加 `goodput_primary`。下面的规则要求两项指标的配对效应区间下界分别超过 3% 和 5%，已登记的护栏也要通过。

```json
{
  "primary": {"metric": "output_token_throughput", "direction": "higher", "min_relative_improvement": 0.03},
  "goodput_primary": {"metric": "goodput", "direction": "higher", "min_relative_improvement": 0.05},
  "guardrails": [{"metric": "failure_rate", "max_value": 0.0}]
}
```

`goodput_primary` 的指标固定为 `goodput`，方向固定为 `higher`。若把 `goodput` 直接设为 `primary`，只使用这一项作为主指标。

若基线为 0 token/s，该对的相对效应未定义，依赖相对 goodput 的判定保持待确认。绝对下限可用 `min_value` 约束候选。goodput 的逐请求时限与 p99 的批次护栏分别填写。正式发现与独立确认都以关闭 profiler 的原件重放和复算，沿用同一份契约。

## 使用本地任务评分程序

操作员可在可信的预注册契约中选择 `python_score_v1`。这一项允许评分代码以当前账户权限运行。应先审阅程序、固定单个 Python 文件和参考文件的字节 hash，用 `artifact-ref` 生成两份引用。现有精确匹配模式直接在验收器内运行，无需外部程序。

例如可将一个场景的质量规则替换为下列对象。参考文件仍以完整请求 ID 为键，每个值可以是字符串、数字、列表或 JSON 对象。示例数据 `{"capital-01":{"accepted":["paris","巴黎"]}}` 接受两种等价答案。

```json
{
  "method": "python_score_v1",
  "scorer": {"path": "grade.py", "sha256": "<grade.py 文件 SHA-256>"},
  "reference": {"path": "answers.json", "sha256": "<参考文件 SHA-256>"},
  "timeout_s": 10,
  "min_mean_score": 1.0
}
```

`grade.py` 可以写成如下形式。

```python
import json
import sys

samples = json.load(sys.stdin)["items"]
scores = {}
for item in samples:
    score = 0
    if item["status"] == "success":
        answer = item["output"]["content"].strip().casefold()
        score = int(answer in item["reference"]["accepted"])
    scores[item["request_id"]] = score
json.dump({"schema_version": 1, "scores": scores}, sys.stdout)
```

验收器按照 workload 原始顺序发送一个 JSON 对象 `{"schema_version":1,"items":[...]}`。每项包含 `request_id`、`prompt_text`、对应 `reference`、终态 `status` 和 `output`。成功请求的 `output` 含已通过 SSE 重放的 `content` 和 `reasoning_content`。失败请求的 `output` 为 null，只能得零分。程序必须为所有请求返回 0 到 1 的有限数值；验收器按全部请求的算术平均分与事前约定的 `min_mean_score` 比较。

运行时从已核对 SHA-256 的字节复制程序，在临时目录用当前 Python 解释器直接启动。请求与模型输出只通过标准输入传入。解释器的隔离模式可减少 Python 启动环境的干扰，评分程序仍具有操作员账户的文件与网络权限。第三方依赖版本和随机性由任务方控制。返回非零、超时、结果缺项、异常分数或程序指纹变化会使本次验收报错。报告保存输入文件目录、评分器和 Python 指纹、逐请求输入指纹、输入与响应的整体 SHA-256，以及逐请求分数。用已封存的 workload、原始输出、终态和参考数据即可按上述顺序重建标准输入。

## 采集前记录实际部署

先按 [测量工具用法](../evaluate/references/tool_usage.md) 准备 config、environment 与 metric_contract。以同一组稳定字段对计划配置和从目标服务采集的实际解析配置进行规范化。实际采集材料应包含服务的原始启动日志、端点回显或进程观测。两个示例属于同一次部署。

`observed.json` 的最小内容如下。示例中的实例 ID 记录主机、容器、PID 和启动时刻。每次重启都重新采集。保留服务端与客户端一致的 base_url 和 served model。

```json
{
  "schema_version": 1,
  "instance_id": "host-a/container-a/993@2026-11-20T02:30:00Z",
  "base_url": "http://127.0.0.1:8000/v1", "model": "model-a",
  "effective_config": {"max_num_seqs": 32, "tensor_parallel_size": 2},
  "implementation_sha256": null
}
```

给现有 `prepare_metadata.py --config-evidence` 的 JSON 如下。两条路径相对该 JSON 文件解析。验收会核对这两份原件的字节 hash，按 `effective_config` 重算配置身份，并核对端点、served model 和实现身份。部署材料的采集可信度、进程在采样期间持续提供服务，由采集环境保证。服务可变时应在采集窗口前后核实同一进程与启动时刻。

```json
{
  "verified": true, "scope": "worker 993 parsed configuration at startup",
  "observation": "observed.json", "evidence": ["observed.json","worker-startup.log"]
}
```

传入 `--config` 的 JSON 应包含与上述 `effective_config` 相同的规范字段。原生参数对照的 `implementation_sha256` 为 null，稳定的软件版本固定于环境。源码对照使用原生 `--implementation` 和 `--implementation-evidence`，分别给出两侧实际加载版本的原件。检查器检验实现清单、其独立证明与外部文件指纹，两侧配置固定。计划中两个不同的实现 hash 可直接生成原生 `implementation_change`，契约若已声明身份则须完全一致。已采纳源码后可以继续调参。此时两侧都传入同一非空实现身份与各自的生效证明，分别校验实际版本，再交给原生参数比较器。报告保留固定的实现 hash 和每个 run 的两项身份。

采集时可沿用默认 `quality_status=not_evaluated`。验收会对本轮真实请求重新评分，将派生结果交给比较器，原始 manifest 保留原值。已有质量报告若曾明确判定失败，仍会阻止采纳。既有外部报告的 `pass` 也需通过本轮重放与评分。

## 比较、确认与复查

输入 `evidence.json` 沿用原生配对清单结构。两侧都填写 benchmark 生成的原始 run 目录。每个正式 run 应关闭 profiler。下面列出两对新运行，预热和带 profiler 的诊断采集单独留档。

```json
{
  "schema_version": 1,
  "pairs": [
    {"scenario":"short","baseline":"runs/01-a","candidate":"runs/01-b"},
    {"scenario":"short","baseline":"runs/02-a","candidate":"runs/02-b"}
  ]
}
```

生成的 `baseline.identity.json`、`candidate.identity.json` 各含 `config_sha256` 与 `implementation_sha256`。日常使用控制器执行验收。下面的离线命令适合独立复核，标准输出为完整对照报告。退出码 0、1、2 分别表示建议采纳、建议回退或待确认、输入或证据错误。

```bash
python3 -B scripts/round_evidence.py check /campaign/comparison.json /campaign/evidence.json \
  /campaign/baseline.identity.json /campaign/candidate.identity.json
```

报告保留 run ID、每个输入原件的绝对路径与 SHA-256、完整原生比较、逐场景 case 身份、逐运行质量得分与失败 ID、工具指纹。要求 metadata 原件所引用的文件在验收时仍能通过记录的路径读取，包括配置、环境和软件证明。迁移时可在受控存储中保留同一路径映射。重复确认使用另一份独立清单，由控制器检查 run ID 和请求记录的 hash 未复用。

验收按原生解析器重放完整成功 SSE，对照 usage、终止状态、输出事件数、输出文本、实际请求和冻结 workload。失败请求仍占用整段墙钟时间。客户端计时、机器是否独占、外部日志是否确实来自该服务，依赖可信采集现场。文件 hash 支持后续复核，无法从一组被整体替换的原件推断现场真实性。
