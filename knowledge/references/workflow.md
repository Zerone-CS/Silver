# 知识库操作

## 查询

`SUITE_ROOT` 指向实际套件根目录。查询只需带 PyYAML 的兼容 Python。下例使用套件的隔离解释器；如果已有可用环境，可将 `KB_PYTHON` 改为对应解释器。随附离线轮子适配 Linux aarch64 / CPython 3.9，匹配的主机可用 `python3 "$SUITE_ROOT/scripts/setup_runtime.py"` 建立环境。[运行环境](../../references/runtime.md)

```bash
KB_PYTHON="$SUITE_ROOT/.runtime/bin/python"
KB="$SUITE_ROOT/knowledge/scripts/kb.py"
SNAPSHOT="$SUITE_ROOT/knowledge/kb/parse_params/output"

"$KB_PYTHON" "$KB" query -d "$SNAPSHOT/params" -w name=--max-num-batched-tokens \
  --show name,type,scope,source_file,constraints --format yaml
```

已固定其他快照时用它的卡片目录替换 `-d`。查现象可用 `-s HCCL` 全文检索；`-w` 做子串匹配，多个条件取交集，同一条件内逗号分隔的值取并集。核对卡片完整身份。`scope` 标注源码归属，部署上是否生效还须查当前后端的调用路径。缺卡时可追查提取输入 `parameters.json`、`logs/stage1_skipped.json`、`logs/stage2_skipped.json` 和现场源码。查询器会跳过无法解析的 YAML，核查快照完整性时用下文的结构检查。[查询字段与示例](../kb/query_usage.md)

内置快照的来源标签是 vLLM `ced6857` 和 vLLM-Ascend `8fde6d4e3`。历史清单记载分析 888 项，758 项有关性能，其中 122 项高影响；落盘 756 张卡，其中高影响 120 张。保留这项差异。对当前部署，分别核对两套实际加载源码的 commit、未提交修改或文件指纹；历史标签相同仍需核对内容。各项主张的核验状态随引用交付。[历史构建清单](../kb/parse_params/output/manifest.yaml) · [记录字段](records.md)

## 构建与恢复

构建使用上述 `KB`、装有知识依赖的 `KB_PYTHON`、两套源码绝对路径 `VLLM_REPO` 和 `ASCEND_REPO`。为本次构建选择源码和内置快照以外的新目录 `NEW_BUILD`。后续步骤按所需范围执行，并检查各命令退出码。

```bash
"$KB_PYTHON" "$KB" extract \
  --vllm-src "$VLLM_REPO" --vllm-ascend-src "$ASCEND_REPO" \
  --output "$NEW_BUILD/parameters.json" --pretty
```

默认静态提取。需要预览筛选结果时运行 Stage 1，生成进度、筛选日志及 schema，期间无 API 调用。

```bash
"$KB_PYTHON" "$KB" parse \
  --vllm-src "$VLLM_REPO" --vllm-ascend-src "$ASCEND_REPO" \
  --input "$NEW_BUILD/parameters.json" --output "$NEW_BUILD/preview" --dry-run-stage1
```

LLM 解析和提取的可选 `--with-claude` 从当前进程读取 `ANTHROPIC_MODEL` 及 `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN`。可用 `ANTHROPIC_BASE_URL` 指定端点，凭据只放专用环境变量。记录模型、端点和调用成本，保密令牌。以下示例只解析 Stage 1 通过的前八项；`--max-params` 不限制含重试在内的 API 请求次数。定向构建使用可追溯的参数子集；有硬预算时需有能执行该上限的机制。

```bash
"$KB_PYTHON" "$KB" parse \
  --vllm-src "$VLLM_REPO" --vllm-ascend-src "$ASCEND_REPO" \
  --input "$NEW_BUILD/parameters.json" --output "$NEW_BUILD/analysis" \
  --max-params 8 --concurrency 1
```

解析器在完整筛选通过集上预检文件名碰撞，并检查重复名称和模型返回的 `name/type/scope`。中断后可给原命令加 `--resume`，复用原目录的 `build-inputs.json`、`progress.json` 和已完成产物。输入路径与内容、两套源码、构建器、规则/提示/schema、Python 版本、模式、范围、并发、模型与端点均须与原记录一致；其中任一变化或从预演转正式解析时，使用新目录。历史错误保留为错误，不会自动重试；检查退出码、manifest、progress 和日志，记录未完成项及恢复点。

## 审查与发布

```bash
"$KB_PYTHON" "$SUITE_ROOT/knowledge/scripts/check_kb_snapshot.py" \
  --params-dir "$NEW_BUILD/analysis/params" --manifest "$NEW_BUILD/analysis/manifest.yaml" \
  --output "$NEW_BUILD/structure-check.json"
```

检查报告使用新路径。工具核验卡片结构、名称及清单统计；审查人另按输入核对成卡、规则跳过、无性能影响、错误、待处理和别名，再到部署源码核实准备采用的默认值、限制条件及执行路径。发布任务先保存已审查的不可变快照及清单，再更新指定发布引用并保留旧引用以便回退。运行中的实验继续使用已固定版本，直至协调者显式切换。[构建记录模板](../assets/knowledge-build.example.json)
