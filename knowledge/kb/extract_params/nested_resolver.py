"""Dynamic nested config resolution.

Recursively expands nested config fields by following type annotations
through the config class registry. No hardcoded sub-config maps.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from .config import get_vllm_config_dir, get_vllm_ascend_src
from .dataclass_parser import parse_config_file, _extract_class_fields
from .utils import parse_default_value, truncate_description
from .category import categorize_by_class_name, categorize_by_field_name


# ---------------------------------------------------------------------------
# vLLM upstream nested configs
# ---------------------------------------------------------------------------

def resolve_nested_configs(config_class_cache: dict[str, dict]) -> list[dict]:
    """Expand nested config dataclass fields to leaf entries.

    Discovers the mapping from CLI flags to nested config classes by scanning
    arg_utils.py for parser.add_argument("--xxx-config", type=json.loads, ...)
    calls and matching them to config class definitions.
    """
    # config_dir = {repo}/vllm/config, parent = {repo}/vllm (the package dir)
    vllm_root = get_vllm_config_dir().parent
    arg_utils = vllm_root / "engine" / "arg_utils.py"
    if not arg_utils.exists():
        return []

    source = arg_utils.read_text(encoding="utf-8")
    flag_to_class = _discover_nested_config_flags(source, config_class_cache)
    results: list[dict] = []

    for cli_flag, (class_name, key_name) in flag_to_class.items():
        if class_name not in config_class_cache:
            continue
        fields = config_class_cache[class_name]
        prefix = key_name
        results.extend(_flatten_fields(
            fields, prefix, class_name, config_class_cache, set(),
        ))

    return results


def _discover_nested_config_flags(source: str, class_cache: dict[str, dict]) -> dict:
    """Scan arg_utils.py for --xxx-config flags that accept JSON nested configs.

    Finds patterns like: add_argument("--compilation-config", **kwargs["field"])
    and maps them to config class names via the class_cache.
    """
    flag_to_class: dict[str, tuple[str, str]] = {}

    # Find add_argument("--xxx-config", ...) — any config-suffixed flag
    pattern = re.compile(r'add_argument\(\s*"(--[\w-]+-config)"', re.DOTALL)
    for m in pattern.finditer(source):
        flag = m.group(1)
        flag_stem = flag.lstrip("-").replace("-", "_")  # compilation_config

        # Generate candidate class names by progressively converting to PascalCase
        candidates = _generate_class_candidates(flag_stem)

        for cand in candidates:
            if cand in class_cache:
                key_name = flag_stem
                # Remove trailing _config from the dotted prefix
                if key_name.endswith("_config"):
                    key_name = key_name[:-7]
                flag_to_class[flag] = (cand, key_name)
                break

    # Also check for non-standard config flags (e.g. --additional-config)
    alt_pattern = re.compile(r'add_argument\(\s*"(--[\w-]+)"[^)]*additional', re.DOTALL)
    for m in alt_pattern.finditer(source):
        flag = m.group(1)
        if flag in flag_to_class:
            continue
        flag_stem = flag.lstrip("-").replace("-", "_")

    return flag_to_class


def _generate_class_candidates(flag_stem: str) -> list[str]:
    """Generate candidate config class names from a CLI flag stem.

    e.g. 'compilation_config' → ['CompilationConfig', 'compilationConfig', ...]
         'speculative_config' → ['SpeculativeConfig', ...]
         'pooler_config' → ['PoolerConfig', ...]
    """
    parts = flag_stem.split("_")
    # Remove trailing 'config' if present
    if parts[-1] == "config":
        parts = parts[:-1]
    pascal = "".join(p.capitalize() for p in parts)
    candidates = [pascal, pascal + "Config"]

    # Known special-case mappings for common acronyms / abbreviations
    specials = {
        "kv_transfer": "KVTransfer",
        "ec_transfer": "ECTransfer",
        "kv_events": "KVEvents",
        "eplb": "EPLB",
        "uva": "UVA",
        "lora": "LoRA",
        "xlite": "Xlite",
    }
    if flag_stem in specials:
        candidates.insert(0, specials[flag_stem] + "Config")
    # Check partial matches with known acronym capitalization
    for key, val in specials.items():
        if key in flag_stem:
            # val is already correctly capitalized (e.g. "KVTransfer")
            candidates.insert(0, val + "Config")

    return candidates


def _flatten_fields(
    fields: dict,
    prefix: str,
    class_name: str,
    class_cache: dict[str, dict],
    visited: set,
    depth: int = 0,
) -> list[dict]:
    """Recursively flatten potentially-nested dataclass fields."""
    if depth > 3:
        return []

    results: list[dict] = []
    for field_name, field_info in fields.items():
        if field_name.startswith("_"):
            continue

        name = f"{prefix}.{field_name}"
        field_type = field_info.get("type", "")

        # Check if this field's type is a known config class → recurse
        type_basename = _extract_type_basename(field_type)
        if type_basename and type_basename in class_cache and type_basename not in visited:
            visited.add(type_basename)
            nested_fields = class_cache[type_basename]
            if nested_fields:
                results.extend(_flatten_fields(
                    nested_fields, name, type_basename, class_cache, visited, depth + 1,
                ))
                continue

        default = field_info.get("default")
        description = field_info.get("description")
        category = field_info.get("category", categorize_by_class_name(class_name))

        results.append({
            "name": name,
            "type": "nested",
            "category": category,
            "default": default,
            "description": truncate_description(description),
            "source_file": f"vllm/config/{class_name.lower()}.py",
            "scope": "vllm",
        })

    return results


def _extract_type_basename(type_str: str) -> str | None:
    """Extract the simple class name from a type annotation string.
    e.g. 'CompilationConfig | None' → 'CompilationConfig'
         'dict[str, Any]' → None
    """
    if not type_str:
        return None
    # Strip union types, take first non-None
    parts = type_str.split("|")
    for part in parts:
        part = part.strip()
        if part in ("None", "str", "int", "float", "bool", "dict", "list",
                      "tuple", "Any", "set", "Callable", "Type", "ClassVar"):
            continue
        if part.startswith("dict[") or part.startswith("list[") or part.startswith("tuple["):
            continue
        if part[0].isupper() and part.isidentifier():
            return part
    return None


# ---------------------------------------------------------------------------
# vllm-ascend additional_config parsing
# ---------------------------------------------------------------------------

def parse_ascend_additional_config() -> list[dict]:
    """Parse ascend_config.py for all additional_config parameters.

    Dynamically extracts:
    - Top-level keys from additional_config.get("key", default) calls
    - Sub-config class fields (parsed from their __init__ signatures)
    - KV transfer extra_config keys
    """
    results: list[dict] = []

    ascend_config = get_vllm_ascend_src() / "ascend_config.py"
    if not ascend_config.exists():
        return results

    source = ascend_config.read_text(encoding="utf-8")
    lines = source.split("\n")

    # 1. Top-level additional_config.get() calls
    results.extend(_parse_additional_config_gets(source, lines))

    # 2. Legacy __init__-style sub-config classes.  Kept for older releases.
    results.extend(_parse_ascend_subclasses(source, lines))

    # 3. Dataclass-based AscendConfig (vllm-ascend >= 0.30) and its
    #    canonical default config dictionary.
    results.extend(_parse_ascend_dataclass_config(source, lines))

    # 4. KV transfer extra_config keys
    results.extend(_parse_kv_extra_config())

    return _enrich_descriptions(_merge_additional_entries(results))


def _parse_additional_config_gets(source: str, lines: list[str]) -> list[dict]:
    """Extract additional_config.get("key", default) calls."""
    results: list[dict] = []

    # Pattern: additional_config.get("key_name", default_value)
    pattern = re.compile(
        r'additional_config\.get\(\s*["\'](\w+)["\']\s*,\s*([^)]+?)\)'
    )

    seen_keys: set[str] = set()
    for m in pattern.finditer(source):
        key = m.group(1)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        default_raw = m.group(2).strip()
        # Handle compound defaults like: ... and ... or ...
        if " and " in default_raw or " or " in default_raw:
            default = "<computed>"
        else:
            default = parse_default_value(default_raw)

        name = f"additional_config.{key}"
        category = _categorize_additional_name(name)

        results.append({
            "name": name, "type": "nested", "category": category,
            "default": default, "description": None,
            "source_file": "vllm_ascend/ascend_config.py",
            "scope": "vllm-ascend",
        })

    return _enrich_descriptions(results)


def _parse_ascend_subclasses(source: str, lines: list[str]) -> list[dict]:
    """Parse sub-config class fields from ascend_config.py.

    Field names, defaults, and descriptions are extracted from class definitions.
    Only emits leaf fields under the sub-config prefix – container dict entries
    are handled by _parse_additional_config_gets.
    """
    results: list[dict] = []

    # Discovered from AscendConfig.__init__: self.xxx = XxxConfig(...)
    class_to_prefix = _discover_ascend_class_prefixes(source)

    for class_name, prefix in class_to_prefix.items():
        class_fields = _extract_init_params(source, lines, class_name)
        for field_name, field_info in class_fields.items():
            results.append({
                "name": f"{prefix}.{field_name}",
                "type": "nested",
                "category": categorize_by_class_name(class_name),
                "default": field_info.get("default"),
                "description": field_info.get("description"),
                "source_file": "vllm_ascend/ascend_config.py",
                "scope": "vllm-ascend",
            })

    return results


# ---------------------------------------------------------------------------
# Dataclass-based AscendConfig (vllm-ascend >= 0.30)
# ---------------------------------------------------------------------------

_MISSING = object()

_ADDITIONAL_TOP_CATEGORY = {
    "xlite_graph_config": "compilation",
    "ascend_compilation_config": "compilation",
    "ascend_fusion_config": "compilation",
    "ascend_warmup_config": "compilation",
    "finegrained_tp_config": "parallelism",
    "eplb_config": "parallelism",
    "scheduler_config": "scheduling",
    "rejection_sampler_config": "speculation",
    "dynamic_spec_config": "speculation",
    "kvpp_config": "kv_cache",
    "sparse_kv_offload_config": "kv_cache",
    "rl_config": "other",
}

_ADDITIONAL_FIELD_CATEGORY = {
    "enable_cpu_binding": "hardware",
    "multistream_dsv4_dsa_overlap": "parallelism",
    "multistream_engram_overlap": "parallelism",
    "multistream_overlap_shared_expert": "parallelism",
    "enable_prefill_mc2": "communication",
    "enable_fused_mc2": "communication",
    "mc2_comm_alg": "communication",
    "enable_mc2_hierarchy_comm": "communication",
    "enable_kv_nz": "memory",
    "enable_transpose_kv_cache_by_block": "memory",
    "enable_dsa_cp": "parallelism",
    "enable_force_eplb": "parallelism",
    "enable_shared_expert_dp": "parallelism",
    "enable_pcp_o_proj_weight_sharding": "parallelism",
    "enable_pcp_embedding_lmhead_weight_sharding": "parallelism",
    "enable_sp_by_pass": "parallelism",
    "num_head_replica": "parallelism",
    "pd_head_ratio": "parallelism",
    "pd_tp_ratio": "parallelism",
    "mega_moe_max_tokens": "parallelism",
    "enable_mlapo": "compilation",
    "mlapo_keep_prefill_weights": "compilation",
    "pa_shape_list": "compilation",
    "combine_quant_mode": "quantization",
    "enable_sparse_sfa_c8": "quantization",
    "enable_sparse_li_c8": "quantization",
    "enable_sparse_li_c4": "quantization",
    "enable_sparse_sfa_turboquant": "quantization",
    "c8_enable_reshape_optim": "quantization",
    "weight_nz_mode": "memory",
    "enable_kvpp": "kv_cache",
    "draft_window_size": "speculation",
    "mix_placement": "scheduling",
    "ascend_log_path": "observability",
    "dump_config_path": "observability",
    "msmonitor_use_daemon": "observability",
    "sfa_dcp_force_tmajor_restore": "model",
    "refresh": "other",
}


def _infer_type_from_value(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return "Any"


def _categorize_additional_name(name: str) -> str:
    rel = name.replace("additional_config.", "", 1)
    parts = rel.split(".")
    top = parts[0]
    leaf = parts[-1]

    if top in _ADDITIONAL_TOP_CATEGORY:
        return _ADDITIONAL_TOP_CATEGORY[top]
    if top in _ADDITIONAL_FIELD_CATEGORY:
        return _ADDITIONAL_FIELD_CATEGORY[top]
    if leaf in _ADDITIONAL_FIELD_CATEGORY:
        return _ADDITIONAL_FIELD_CATEGORY[leaf]
    return _categorize_additional_key(top)


def _extract_ascend_default_dict(class_node: ast.ClassDef) -> dict:
    """Extract the JSON default-config block from AscendConfig's docstring."""
    doc = ast.get_docstring(class_node, clean=True)
    if not doc:
        return {}

    marker = doc.find("The values below show every supported key")
    if marker < 0:
        marker = doc.find("supported key")
    if marker < 0:
        marker = 0

    start = doc.find("{", marker)
    if start < 0:
        return {}

    depth = 0
    in_str = False
    escaped = False
    end = None
    for idx in range(start, len(doc)):
        ch = doc[idx]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = idx + 1
                break

    if end is None:
        return {}
    try:
        parsed = json.loads(doc[start:end])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_ascend_dataclass_config(source: str, lines: list[str]) -> list[dict]:
    """Parse dataclass-style AscendConfig and its default config dictionary.

    vllm-ascend >= 0.30 defines additional_config fields as dataclass
    attributes (often ``dataclasses.field(default_factory=...)``), so the
    legacy ``self.xxx = XxxConfig(...)`` scanner cannot discover them.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    class_nodes = {
        node.name: node
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.ClassDef)
    }
    if "AscendConfig" not in class_nodes:
        return []

    fields_cache: dict[str, dict] = {}

    def fields_for(class_name: str) -> dict:
        if class_name not in fields_cache:
            node = class_nodes.get(class_name)
            fields_cache[class_name] = (
                _extract_class_fields(node, lines) if node is not None else {}
            )
        return fields_cache[class_name]

    entries: dict[str, dict] = {}

    def add_entry(name: str, info: dict, default=_MISSING,
                  description: str | None = None) -> None:
        info = info or {}
        if default is _MISSING:
            default = info.get("default")
        entry = {
            "name": name,
            "type": "nested",
            "category": _categorize_additional_name(name),
            "default": default,
            "description": description or info.get("description"),
            "source_file": "vllm_ascend/ascend_config.py",
            "scope": "vllm-ascend",
        }
        if name in entries:
            current = entries[name]
            if not current.get("description") and entry.get("description"):
                current["description"] = entry["description"]
            if current.get("default") in (None, "<computed>") and \
                    entry.get("default") not in (None, "<computed>"):
                current["default"] = entry["default"]
            if current.get("category") in (None, "other") and \
                    entry.get("category") not in (None, "other"):
                current["category"] = entry["category"]
        else:
            entries[name] = entry

    def walk(prefix: str, class_name: str, visited: set[str],
             depth: int) -> None:
        if depth > 6:
            return
        for field_name, info in fields_for(class_name).items():
            if field_name.startswith("_") or "ClassVar" in (info.get("type") or ""):
                continue
            name = f"{prefix}.{field_name}"
            type_base = _extract_type_basename(info.get("type") or "")
            if type_base and type_base in class_nodes and type_base not in visited:
                walk(name, type_base, visited | {type_base}, depth + 1)
            else:
                add_entry(name, info)

    for field_name, info in fields_for("AscendConfig").items():
        if field_name.startswith("_") or "ClassVar" in (info.get("type") or ""):
            continue
        name = f"additional_config.{field_name}"
        type_base = _extract_type_basename(info.get("type") or "")
        if type_base and type_base in class_nodes and type_base != "AscendConfig":
            walk(name, type_base, {type_base}, 1)
        else:
            add_entry(name, info)

    default_config = _extract_ascend_default_dict(class_nodes["AscendConfig"])
    flat_defaults: dict[str, object] = {}

    def flatten(obj: dict, prefix: str) -> None:
        for key, value in obj.items():
            name = f"{prefix}.{key}"
            if isinstance(value, dict) and value:
                flatten(value, name)
            else:
                flat_defaults[name] = value

    flatten(default_config, "additional_config")

    for name, value in flat_defaults.items():
        if name in entries:
            entry = entries[name]
            if entry.get("default") in (None, "<computed>"):
                entry["default"] = value
            if entry.get("type") in (None, "Any", "unknown"):
                entry["type"] = _infer_type_from_value(value)
        else:
            add_entry(name, {}, default=value)

    return list(entries.values())


def _merge_additional_entries(results: list[dict]) -> list[dict]:
    """Merge duplicate additional_config entries, keeping richer metadata."""
    merged: dict[str, dict] = {}
    for entry in results:
        name = entry["name"]
        if name not in merged:
            merged[name] = entry
            continue
        current = merged[name]
        if not current.get("description") and entry.get("description"):
            current["description"] = entry["description"]
        if current.get("default") in (None, "<computed>") and \
                entry.get("default") not in (None, "<computed>"):
            current["default"] = entry["default"]
        if current.get("category") in (None, "other") and \
                entry.get("category") not in (None, "other"):
            current["category"] = entry["category"]
        if current.get("type") in (None, "Any", "unknown") and \
                entry.get("type") not in (None, "Any", "unknown"):
            current["type"] = entry["type"]
    return list(merged.values())


def _discover_ascend_class_prefixes(source: str) -> dict[str, str]:
    """Dynamically discover sub-config class → additional_config key mappings
    from AscendConfig.__init__ patterns like:
        self.xlite_graph_config = XliteGraphConfig(xlite_graph_config, ...)
    """
    mapping: dict[str, str] = {}
    pattern = re.compile(
        r'self\.(\w+)\s*=\s*(\w+Config)\(',
    )
    for m in pattern.finditer(source):
        attr = m.group(1)
        cls_name = m.group(2)
        mapping[cls_name] = f"additional_config.{attr}"
    return mapping


def _extract_init_params(source: str, lines: list[str], class_name: str) -> dict:
    """Extract config fields from a class definition using AST.

    Handles three patterns:
    - Typed __init__ params (AscendCompilationConfig, AscendFusionConfig)
    - Dict.get() in __init__ (XliteGraphConfig, FinegrainedTPConfig, WeightPrefetchConfig)
    - _defaults dict (EplbConfig)
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    class_node = None
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            class_node = node
            break
    if class_node is None:
        return {}

    # Strategy B: _defaults dict (EplbConfig style) – check first
    for node in ast.iter_child_nodes(class_node):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_defaults":
                    if isinstance(node.value, ast.Dict):
                        results: dict[str, dict] = {}
                        for key_node, val_node in zip(node.value.keys, node.value.values):
                            if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                                key = key_node.value
                                default = _ast_eval(val_node)
                                results[key] = {"default": default, "description": None}
                        return results

    # Try typed init first, but validate – reject if params are just dict containers
    init_node = None
    for node in ast.iter_child_nodes(class_node):
        if isinstance(node, ast.FunctionDef) and node.name == "__init__":
            init_node = node
            break
    if init_node is None:
        return {}

    typed_params = _parse_typed_init_params(init_node, lines)
    # If typed params look like dict-container args (contain the class's snake_case
    # root in param names), fall through to dict init parsing
    snake_root = _class_to_snake(class_name).replace("_config", "")
    if typed_params and not any(snake_root in p for p in typed_params):
        return typed_params

    # Strategy C: dict.get() calls in __init__
    dict_params = _parse_dict_init_params(init_node)
    if dict_params:
        return dict_params

    # Fallback to typed if dict parsing found nothing
    if typed_params:
        return typed_params
    return {}


def _class_to_snake(name: str) -> str:
    """PascalCase → snake_case, handling consecutive capitals (e.g. TP, KV)."""
    return re.sub(
        r'(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])',
        '_', name,
    ).lower()


def _parse_typed_init_params(func_node: ast.FunctionDef, lines: list[str]) -> dict:
    """Parse __init__(self, param1: type = default, ...) parameters."""
    results: dict[str, dict] = {}
    args = func_node.args

    # Combine positional + keyword args (skip self)
    all_args = list(args.args) + list(args.kwonlyargs)
    defaults = list(args.defaults) + list(args.kw_defaults)
    # Pad defaults so they align from the right
    defaults = [None] * (len(all_args) - len(defaults)) + list(defaults)

    for arg, default_node in zip(all_args, defaults):
        if arg.arg == "self":
            continue
        if arg.arg.startswith("_"):
            continue
        if arg.arg in ("kwargs", "args"):
            continue

        type_str = _ast_annotation_to_str(arg.annotation) if arg.annotation else "Any"
        default = _ast_eval(default_node) if default_node is not None else None

        # Extract description from docstring
        description = _extract_param_doc(func_node, arg.arg)

        results[arg.arg] = {
            "type": type_str,
            "default": default,
            "description": description,
        }

    return results


def _parse_dict_init_params(func_node: ast.FunctionDef) -> dict | None:
    """Parse dict.get() style __init__: self.x = config.get("x", default)."""
    results: dict[str, dict] = {}
    pattern = re.compile(r'(\w+)\.get\(\s*["\'](\w+)["\']\s*,\s*([^)]+?)\)')

    # Use unparsed source for simplicity – AST-based extraction of .get() calls
    # inside __init__ is very verbose. Fall back to regex on the function source.
    # We don't have line offsets here, but the pattern is reliable.
    for node in ast.iter_child_nodes(func_node):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                    field_name = target.attr
                    if field_name.startswith("_"):
                        continue
                    if isinstance(node.value, ast.Call):
                        default = _ast_eval_default(node.value)
                        results[field_name] = {"default": default, "description": None}

    return results if results else None


def _ast_eval(node: ast.expr | None) -> object:
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        val = node.value
        return None if val is None else val  # preserve None
    if isinstance(node, ast.Name) and node.id in ("True", "False"):
        return node.id == "True"
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        if isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, (int, float)):
            return -node.operand.value
    return "<computed>"


def _ast_eval_default(call_node: ast.Call) -> object:
    """Extract the default from a .get("key", default) call node."""
    # Looking for: some_dict.get("key", default_value)
    if len(call_node.args) >= 2:
        return _ast_eval(call_node.args[1])
    for kw in call_node.keywords:
        if kw.arg == "default":
            return _ast_eval(kw.value)
    return None


def _ast_annotation_to_str(node: ast.expr) -> str:
    if node is None:
        return "Any"
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.Subscript):
        base = _ast_annotation_to_str(node.value)
        return f"{base}[...]"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return f"{_ast_annotation_to_str(node.left)} | {_ast_annotation_to_str(node.right)}"
    return "Any"


def _extract_param_doc(func_node: ast.FunctionDef, param_name: str) -> str | None:
    """Extract a parameter description from the function docstring (Args: section)."""
    doc = ast.get_docstring(func_node)
    if not doc:
        return None
    in_args = False
    current_param = None
    lines_buf: list[str] = []
    for line in doc.split("\n"):
        stripped = line.strip()
        if stripped.startswith("Args:") or stripped.startswith("Args"):
            in_args = True
            continue
        if in_args:
            if stripped and not stripped.startswith(" ") and ":" in stripped:
                # Looks like a new section header
                if current_param == param_name:
                    break
                in_args = False
                continue
            m = re.match(rf"^{param_name}\s*\(([^)]*)\):\s*(.+)", stripped)
            if m:
                current_param = param_name
                lines_buf.append(m.group(2).strip())
                continue
            if current_param == param_name and stripped:
                lines_buf.append(stripped)
                continue
    return " ".join(lines_buf) if lines_buf else None


def _parse_kv_extra_config() -> list[dict]:
    """Scan vllm-ascend KV transfer directory for extra_config key references."""
    results: list[dict] = []
    kv_dir = get_vllm_ascend_src() / "distributed" / "kv_transfer"
    if not kv_dir.exists():
        return results

    seen: set[str] = set()
    for py_file in kv_dir.rglob("*.py"):
        try:
            source = py_file.read_text(encoding="utf-8")
        except Exception:
            continue
        # get_from_extra_config("key", {"default": val})  or  extra_config.get("key", val)
        for m in re.finditer(r'get_from_extra_config\(\s*["\'](\w+)["\']', source):
            key = m.group(1)
            if key not in seen:
                seen.add(key)
                results.append({
                    "name": f"kv_transfer_config.kv_connector_extra_config.{key}",
                    "type": "nested", "category": "kv_cache",
                    "default": None, "description": None,
                    "source_file": f"vllm_ascend/distributed/kv_transfer/{py_file.name}",
                    "scope": "vllm-ascend",
                })
        for m in re.finditer(r'extra_config\.get\(\s*["\'](\w+)["\']', source):
            key = m.group(1)
            if key not in seen:
                seen.add(key)
                results.append({
                    "name": f"kv_transfer_config.kv_connector_extra_config.{key}",
                    "type": "nested", "category": "kv_cache",
                    "default": None, "description": None,
                    "source_file": f"vllm_ascend/distributed/kv_transfer/{py_file.name}",
                    "scope": "vllm-ascend",
                })

    return results


# ---------------------------------------------------------------------------
# Category & description helpers
# ---------------------------------------------------------------------------

def _categorize_additional_key(key: str) -> str:
    cat_map = {
        "xlite_graph_config": "compilation",
        "ascend_compilation_config": "compilation",
        "ascend_fusion_config": "compilation",
        "finegrained_tp_config": "parallelism",
        "eplb_config": "parallelism",
        "weight_prefetch_config": "model",
        "dump_config_path": "observability",
        "layer_sharding": "parallelism",
        "enable_shared_expert_dp": "parallelism",
        "multistream_overlap_shared_expert": "parallelism",
        "multistream_overlap_gate": "parallelism",
        "recompute_scheduler_enable": "scheduling",
        "enable_cpu_binding": "hardware",
        "SLO_limits_for_dynamic_batch": "scheduling",
        "pa_shape_list": "compilation",
        "enable_async_exponential": "other",
        "enable_kv_nz": "memory",
        "enable_sparse_c8": "model",
        "enable_mc2_hierarchy_comm": "communication",
        "enable_kvpp": "kv_cache",
        "refresh": "other",
    }
    return cat_map.get(key, categorize_by_field_name(key))


def _enrich_descriptions(results: list[dict]) -> list[dict]:
    desc_map = {
        "xlite_graph_config": "Xlite graph mode configuration for Ascend MLAPO optimization",
        "ascend_compilation_config": "Ascend graph compilation optimization configuration",
        "ascend_fusion_config": "Ascend operator fusion configuration",
        "finegrained_tp_config": "Fine-grained tensor parallelism configuration",
        "eplb_config": "Expert Parallelism Load Balancing configuration",
        "weight_prefetch_config": "Weight prefetching configuration",
        "dump_config_path": "Path for dump/precision debugger configuration",
        "layer_sharding": "Linear layer sharding configuration (works best with FLASHCOMM2 and DSA-CP)",
        "enable_shared_expert_dp": "Enable shared expert data parallelism",
        "multistream_overlap_shared_expert": "Enable multi-stream overlap for shared expert",
        "multistream_overlap_gate": "Enable multi-stream overlap for gate",
        "recompute_scheduler_enable": "Enable recompute scheduler",
        "enable_cpu_binding": "Enable CPU core binding for worker processes",
        "SLO_limits_for_dynamic_batch": "SLO limits for dynamic batch sizing (-1 means disabled)",
        "pa_shape_list": "List of shapes for PagedAttention kernel fallback",
        "enable_async_exponential": "Enable async exponential in AscendSampler",
        "enable_kv_nz": "Enable KV NZ format (MLA only, PD decode node only)",
        "enable_sparse_c8": "Enable sparse C8 acceleration (not supported on A5)",
        "enable_mc2_hierarchy_comm": "Enable dispatch/combine inter-node communication via ROCE",
        "refresh": "Force refresh AscendConfig even if already initialized",
    }
    for entry in results:
        key = entry["name"].replace("additional_config.", "")
        if key in desc_map and entry.get("description") is None:
            entry["description"] = desc_map[key]
    return results
