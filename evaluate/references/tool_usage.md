# 采集、校验与比较

从发现索引指向的真实模块目录取得 `EVALUATE_DIR`。以下命令使用 Python 3.9+ 标准库，在独立的 campaign 工作目录执行。已有原件可直接从“校验”或“比较”开始。

## 协议预检

准备已授权的 `BASE_URL`、实际服务名 `MODEL`，以及本次服务的两个非空 JSON 对象：

- `config.json` 保存实际部署的模型、启动 argv、必要环境和解析后配置，核对服务实例、配置生效及计划/实际 diff。
- `environment.json` 保存稳定的设备、驱动/CANN、镜像、实际包/源码与模型权重、客户端环境身份。另存 host、容器、PID/启动时间、设备映射和当次时间。排除凭据，动态时间/PID 与稳定环境指纹分开。

源码对照时，把预声明修改的项目源码/构建产物单独记入 `implementation.json`，其余环境继续固定。两侧共用服务配置；在下方元数据命令追加 `--implementation baseline/implementation.json --implementation-evidence baseline/implementation-evidence.json`，候选使用自己的文件。质量报告写入对应实现 hash，比较契约声明两侧 hash。字段与拒绝条件见 [实现对照](tool_contract.md#源码或构建产物对照)。

若从套件根启动，先设置 `EVALUATE_DIR="$PWD/evaluate"` 再进入工作目录。服务就绪、预热、排空后，使用内置两个小请求做协议预检，下面三步使用同一组文件。每个输出都选未使用的路径，记录各步退出码。

```bash
python3 "$EVALUATE_DIR/scripts/prepare_metadata.py" \
  --scenario smoke --config config.json --environment environment.json \
  --metric-contract "$EVALUATE_DIR/assets/metric_contract.example.json" \
  --profiler-enabled off --run-purpose diagnostic --output smoke.metadata.json
```

元数据成功生成后采集。该命令会向实际服务发送请求，认证取 `OPENAI_API_KEY` 或 `--api-key-env` 指定的变量。采集器使用直接 HTTP(S) 连接、每请求新建连接、校验证书，不读取代理设置。它不自动预热或重试。

```bash
python3 "$EVALUATE_DIR/scripts/benchmark.py" \
  --base-url "$BASE_URL" --model "$MODEL" --metadata smoke.metadata.json \
  --dataset "$EVALUATE_DIR/assets/workload-smoke.jsonl" \
  --run-dir runs/smoke-001 --mode closed_loop --concurrency 1 --workers 1
```

**校验。** 采集退出 0 或 1 且已有最终 manifest 时，运行下列命令。查看 `valid`、错误和警告，再读请求数、有效 token 数、完整耗时、输出吞吐及 TTFT/E2E 样本数。若采集中断或返回 2，先查原始日志与目录完整性。

```bash
python3 "$EVALUATE_DIR/scripts/validate_run.py" \
  --run-dir runs/smoke-001 --output smoke-001.validated.json
```

预检成功后再使用正式负载。请求 JSONL 格式和 [所有退出码](tool_contract.md#退出码) 均有定义。闭环使用空或缺失到达偏移，开环按每条请求的实际到达表传 `--mode open_loop`。有效并行请求上限为 `min(concurrency, workers)`。`--timeout` 从实际开始请求计时，DNS 解析在批次窗口外。`--ignore-eos` 仅在契约要求且服务支持时开启。

## 两种部署的正式比较

按 [计量与判读](measurement_contract.md) 冻结任务的 `metric-contract.json`、`comparison-contract.json` 和各场景 workload。参考 [计量模板](../assets/metric_contract.example.json) 和 [比较格式示例](../assets/comparison_contract.example.json)，将样例阈值换成本次已约定的判据。质量与配置证据随实际部署保存：

- 质量报告至少含 `status`（pass/fail/not_evaluated）、非空 `scope` 和 `method`，另附真实分数、容忍度和结果引用。
- 配置证据含布尔 `verified`、非空 `scope` 和非空 `evidence` 文件路径数组，相对路径以该 JSON 的目录为准。元数据工具检查文件及其 hash，调用者核实其内容。两个未核验的默认状态分别为 `not_evaluated`、`false`。

下面演示主场景的一次基线运行。为每次重复选新的编号和路径，把 `$MODEL`、部署身份文件、质量证据及负载绑定到本次实际服务。仅在上一步生成有效输入后执行下一步。

```bash
python3 "$EVALUATE_DIR/scripts/prepare_metadata.py" \
  --scenario W8_c64 --config baseline/config.json --environment environment.json \
  --metric-contract metric-contract.json --profiler-enabled off --run-purpose performance \
  --quality-report baseline/quality.json --config-evidence baseline/effective-config.json \
  --output W8_c64-baseline-001.metadata.json

python3 "$EVALUATE_DIR/scripts/benchmark.py" \
  --base-url "$BASE_URL" --model "$MODEL" --dataset W8_c64.jsonl \
  --metadata W8_c64-baseline-001.metadata.json --run-dir runs/W8_c64-baseline-001 \
  --mode closed_loop --concurrency 64 --workers 64 --seed 42 --temperature 0 --top-p 1

python3 "$EVALUATE_DIR/scripts/validate_run.py" \
  --run-dir runs/W8_c64-baseline-001 --output W8_c64-baseline-001.validated.json
```

切换并核验候选部署后，用其配置、生效和质量证据完成同场景的另一侧。交错完成全部独立重复和保护场景。按预定配对保存 `trial.json`，例如下面是一条记录的完整格式，实际文件要列入全部独立配对。路径相对 trial 文件。

```json
{"schema_version":1,"pairs":[
  {"scenario":"W8_c64","baseline":"W8_c64-baseline-001.validated.json","candidate":"W8_c64-candidate-001.validated.json"}
]}
```

```bash
python3 "$EVALUATE_DIR/scripts/compare_trials.py" \
  --contract comparison-contract.json --trial trial.json --output comparison-001.json
```

核对 `recommendation` 与逐场景主指标、约束、区间和未决原因。遇到不同指纹、证据不足或不支持的保护指标时，指出受影响的对照或 SLO，保留原件。候选选定后，独立确认使用新的运行。

## 离线抽检与自测

质量契约若要求精确匹配，从两侧独立保存的输出按同一规则抽取 content/reasoning，产生两个 `{prompt_id: output_string}` 文件。参考只读保存，重新建立参考时记录新身份和原因。差异需按任务质量标准判读。

```bash
python3 "$EVALUATE_DIR/scripts/compare_outputs.py" \
  --reference reference.json --candidate candidate.json --output correctness-001.json
```

仅验证工具安装及离线行为时，可运行模块测试，涉及网络的用例使用本机回环模拟服务：

```bash
python3 -B -m unittest discover -s "$EVALUATE_DIR/tests" -p 'test_*.py'
```
