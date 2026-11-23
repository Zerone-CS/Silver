# 参数知识库查询速查

本文是套件维护的 CLI 使用说明；历史来源记录见 `provenance.json`。
统一入口为 `knowledge/scripts/kb.py query`，只读取选中目录的 YAML 卡片，不隐式触发建设或发布。
环境准备见 [运行环境](../../references/runtime.md)，证据保存、源码核验和知识建设见 [知识工作流](../references/workflow.md)。

## 入口与示例

`SKILL_DIR` 为主入口的真实目录，即套件根目录。使用已准备好的套件环境：

```bash
SUITE_ROOT=$(realpath "$SKILL_DIR")
SUITE_PYTHON="$SUITE_ROOT/.runtime/bin/python"
PARAMS_DIR="$SUITE_ROOT/knowledge/kb/parse_params/output/params"

# 统计选中快照；换用其他快照时显式替换 PARAMS_DIR
"$SUITE_PYTHON" "$SUITE_ROOT/knowledge/scripts/kb.py" query -d "$PARAMS_DIR" --count

# 查看当前 CLI 选项
"$SUITE_PYTHON" "$SUITE_ROOT/knowledge/scripts/kb.py" query --help

# 高影响的内存或编译类候选；推荐和约束仍须核对部署源码
"$SUITE_PYTHON" "$SUITE_ROOT/knowledge/scripts/kb.py" query -d "$PARAMS_DIR" \
  -w category=memory,compilation -w performance_impact=high \
  --show name,scope,constraints,source_file,tuning_advice.quick_guide

# 按名称子串定位，随后核对返回卡片的完整 name/type/scope
"$SUITE_PYTHON" "$SUITE_ROOT/knowledge/scripts/kb.py" query -d "$PARAMS_DIR" \
  -w name=--max-num-batched-tokens -a --format yaml

# 在通信类卡片的字符串字段中查机制线索，先查看候选名称
"$SUITE_PYTHON" "$SUITE_ROOT/knowledge/scripts/kb.py" query -d "$PARAMS_DIR" \
  -w category=communication -s HCCL --show name,scope
```

## 选项

| 选项 | 实际行为 |
|---|---|
| `--params-dir / -d DIR` | 选择卡片目录；默认是本套件 `knowledge/kb/parse_params/output/params`，与当前工作目录无关。 |
| `--where / -w KEY=V1,V2` | 取字段值并转成字符串后，不区分大小写地匹配子串；支持嵌套字典的点号路径。 |
| `--tag / -t DIM=V1,V2` | 在 `tags[DIM]` 列表中匹配完整元素，区分大小写；缺少对应标签时不命中。 |
| `--search / -s TEXT` | 递归搜索字符串字段值的子串，不区分大小写；不搜索字段名、数值或布尔值。 |
| `--show F1,F2` | 指定展示字段，支持点号路径；默认 `name,type,category,scope,performance_impact,tuning_advice.summary`。 |
| `--show-all / -a` | 从筛选结果收集字段，覆盖 `--show`。嵌套字典按点号路径展示，字典列表作为整体保留。 |
| `--format / -f FORMAT` | `table`（默认）、`yaml`、`summary` 使用展示字段；`list` 只输出名称。 |
| `--sort-by FIELD` | 按字段原值排序；列表转字符串，混合类型失败时回退到字符串排序。 |
| `--sort-reverse / -r` | 反转 `--sort-by` 排序；不单独改变顺序。 |
| `--count / -c` | 只输出筛选结果数量，优先于格式选项。 |
| `--list-fields` | 输出内置字段参考后退出；不是对选中快照实际字段的扫描。 |
| `--list-tags` | 输出选中目录全部卡片的标签维度和计数后退出；不应用筛选条件。 |

多个 `-w`、多个 `-t` 及 `-s` 之间是 AND；一个 `-w` 或 `-t` 中逗号分隔的值是 OR。
当前 CLI 没有精确字段比较、正则匹配、数值范围或 `--limit` 选项。

## 匹配与结果边界

- `-w scope=vllm` 同时命中 `vllm` 和 `vllm-ascend`；不能据此统计“仅上游”参数。
  `-w name=...` 也是子串筛选，不能替代完整身份核对。
- `--sort-by performance_impact` 按字符串排序，不按影响等级排序；加 `-r` 也不会得到 high、medium、low 的等级顺序。
- 默认快照未打标签；`--list-tags` 没有条目或标签查询为零，不表示缺少相关参数。本套件不包含标签生成流程。
- 使用 `--count` 计数；`--format list` 在无结果时输出 `(no results)`，不能用 `wc -l` 可靠计数。
  `-a --format yaml` 是展示投影，嵌套字段可能变为点号键；保留或迁移快照应使用原始卡片文件。
- 查询器会静默跳过 YAML 解析失败的卡片及非字典内容。成功退出或计数正常不代表快照完整；结构检查使用 `knowledge/scripts/check_kb_snapshot.py`。
- 卡片的 `constraints`、默认值、影响等级和建议值均需结合部署源码核验；字段名不保证其内容是硬约束或已证事实。
  未命中也不能证明参数不存在、无性能影响或不适用于当前模型。
