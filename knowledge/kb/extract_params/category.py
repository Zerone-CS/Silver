"""Dynamic category inference from naming conventions – zero hardcoded names."""

from __future__ import annotations

# Maps keyword tokens found in identifiers to categories.
# Ordered: first match wins, so put specific patterns before broad ones.
_FIELD_KEYWORD_RULES: list[tuple[tuple[str, ...], str]] = [
    # Parallelism / distribution
    (("parallel", "tensor_", "data_", "pipeline", "tp_size", "dp_",
      "node_rank", "nnodes", "ep_size", "expert_par", "expert_placement",
      "prefill_context_par", "context_par", "decode_tp", "prefill_tp",
      "dp_size", "world_size", "local_rank", "ray_", "elastic_ep",
      "enable_eplb", "enable_expert_par"), "parallelism"),
    # KV cache
    (("cache", "block_size", "prefix_cach", "kv_cache", "kv_offload",
      "cpu_offload", "swap_space", "gpu_memory_util",
      "kv_connector", "ec_connector", "kv_transfer",
      "kv_events", "ec_transfer", "kv_sharing",
      "kv_offloading", "hybrid_kv"), "kv_cache"),
    # Scheduling
    (("schedule", "max_num_seq", "max_num_batched", "chunked_prefill",
      "max_seqs", "queue", "dispatch", "scheduler", "SLO",
      "recompute_scheduler", "dynamic_batch", "ubatch",
      "stream_interval", "max_num_partial_prefills"), "scheduling"),
    # Compilation
    (("compil", "cudagraph", "inductor", "dynamo", "fusion",
      "npugraph", "static_kernel", "aot_compile", "triton",
      "pass_config", "compile", "xlite", "graph",
      "optimization_level", "attention_backend",
      "flashinfer", "bytecode", "custom_ops"), "compilation"),
    # Speculation
    (("speculat", "num_speculative", "spec_"), "speculation"),
    # Model-related
    (("quantiz", "dtype", "model", "tokeniz", "lora",
      "enforce_eager", "hf_", "seed", "revision",
      "trust_remote_code", "max_model_len", "max_logprobs",
      "pooler", "structured_output", "multimodal", "mm_",
      "embedding", "weight_prefetch", "sparse",
      "moe", "mlp", "expert", "eplb", "mamba",
      "safetensors", "load_format", "model_impl",
      "runner", "config_format", "reasoning_parser",
      "language_model", "generation_config"), "model"),
    # Memory
    (("memory", "gpu_memory", "offload", "mem", "alloc",
      "swap", "oom", "cpu_kvcache", "prefetch",
      "block_", "retain", "offload_"), "memory"),
    # Observability
    (("observ", "log_", "profiler", "trace", "stats",
      "debug", "logging", "monitor", "show_hidden",
      "collect_detailed", "nsight"), "observability"),
    # Communication
    (("comm", "addr", "port", "master", "rpc", "distributed",
      "socket", "network", "host", "ip", "endpoint",
      "timeout", "connect", "transfer", "allreduce",
      "nixl", "mooncake", "pynccl"), "communication"),
    # Hardware / device
    (("device", "platform", "hardware", "gpu", "npu", "cpu_bind",
      "visible_devices", "cuda_", "rocm", "ascend", "hpu",
      "target_device", "worker_cls", "worker_extension"), "hardware"),
]

_ENV_PREFIX_RULES: list[tuple[str, str]] = [
    ("VLLM_ASCEND_", "compilation"),
    ("VLLM_FLASHINFER_", "compilation"),
    ("VLLM_XLA_", "compilation"),
    ("VLLM_TORCH_PROFILER", "observability"),
    ("VLLM_LOGGING", "observability"),
    ("VLLM_LOG_", "observability"),
    ("VLLM_TRACE_", "observability"),
    ("VLLM_DEBUG_", "observability"),
    ("VLLM_USAGE_", "observability"),
    ("VLLM_NO_USAGE", "observability"),
    ("VLLM_DO_NOT_TRACK", "observability"),
    ("VLLM_MEDIA_", "model"),
    ("VLLM_VIDEO_", "model"),
    ("VLLM_AUDIO_", "model"),
    ("VLLM_IMAGE_", "model"),
    ("VLLM_MM_", "model"),
    ("VLLM_LORA_", "model"),
    ("VLLM_ASSETS_", "model"),
    ("VLLM_CPU_", "memory"),
    ("VLLM_ROCM_", "hardware"),
    ("VLLM_DBO_", "parallelism"),
    ("VLLM_PP_", "parallelism"),
    ("VLLM_RAY_", "parallelism"),
    ("VLLM_WORKER_MULTIPROC", "parallelism"),
    ("VLLM_RPC_", "communication"),
    ("VLLM_HTTP_", "communication"),
    ("VLLM_PORT", "communication"),
    ("VLLM_HOST_IP", "communication"),
    ("VLLM_SKIP_P2P", "communication"),
    # VLLM_ catch-all: delegate to keyword inference instead of "other"
    ("VLLM_", "_INFER_"),
    ("HCCL_", "communication"),
    ("LCCL_", "communication"),
    ("ASCEND_", "hardware"),
    ("PYTORCH_NPU_", "memory"),
    ("DYNAMIC_EPLB", "parallelism"),
    ("EXPERT_MAP_RECORD", "parallelism"),
    ("TASK_QUEUE", "scheduling"),
    ("CPU_AFFINITY", "hardware"),
    ("OMP_", "memory"),
    ("LD_PRELOAD", "memory"),
    ("LD_LIBRARY_PATH", "hardware"),
    ("GLOO_", "communication"),
    ("TP_SOCKET_", "communication"),
    ("HCCL_", "communication"),
    ("S3_", "other"),
    ("NO_COLOR", "other"),
    ("CUDA_", "hardware"),
    ("MAX_JOBS", "hardware"),
    ("NVCC_", "hardware"),
    ("CMAKE_", "hardware"),
    ("VERBOSE", "hardware"),
]


def categorize_by_field_name(field_name: str) -> str:
    """Infer a category from a dataclass field / CLI flag name."""
    name = field_name.lower().replace('-', '_')
    for keywords, cat in _FIELD_KEYWORD_RULES:
        if any(kw in name for kw in keywords):
            return cat
    return "other"


def categorize_by_class_name(class_name: str) -> str:
    """Infer a category from a config class name."""
    # Direct class-name matches take priority over keyword scanning
    _direct = {
        "ModelConfig": "model", "LoadConfig": "model",
        "AttentionConfig": "model", "StructuredOutputsConfig": "model",
        "MultiModalConfig": "model", "LoRAConfig": "model",
        "PoolerConfig": "model", "WeightTransferConfig": "model",
        "WeightPrefetchConfig": "model",
        "ParallelConfig": "parallelism", "EPLBConfig": "parallelism",
        "EplbConfig": "parallelism", "FinegrainedTPConfig": "parallelism",
        "CacheConfig": "kv_cache", "KVTransferConfig": "kv_cache",
        "ECTransferConfig": "kv_cache", "KVEventsConfig": "kv_cache",
        "OffloadConfig": "memory", "UVAOffloadConfig": "memory",
        "PrefetchOffloadConfig": "memory",
        "SchedulerConfig": "scheduling",
        "CompilationConfig": "compilation", "KernelConfig": "compilation",
        "AscendCompilationConfig": "compilation", "AscendFusionConfig": "compilation",
        "XliteGraphConfig": "compilation",
        "SpeculativeConfig": "speculation",
        "ObservabilityConfig": "observability", "ProfilerConfig": "observability",
        "DeviceConfig": "hardware",
    }
    if class_name in _direct:
        return _direct[class_name]
    return categorize_by_field_name(class_name)


def categorize_by_env_name(name: str) -> str:
    """Infer a category from an environment variable name."""
    for prefix, cat in _ENV_PREFIX_RULES:
        if name.startswith(prefix):
            if cat == "_INFER_":
                # Use keyword inference on remainder after stripping VLLM_ prefix
                rest = name[5:] if name.startswith("VLLM_") else name
                return _infer_vllm_env_category(rest)
            return cat
    return categorize_by_field_name(name)


def _infer_vllm_env_category(rest: str) -> str:
    """Keyword-based inference for VLLM_* env vars."""
    r = rest.lower()
    # Observability
    if any(kw in r for kw in ('log', 'trace', 'debug', 'stats', 'profiler',
                                'monitor', 'usage', 'do_not_track', 'logging')):
        return "observability"
    # Communication
    if any(kw in r for kw in ('comm', 'rpc', 'socket', 'network', 'port', 'addr',
                                'endpoint', 'timeout', 'p2p', 'allreduce', 'nixl',
                                'mooncake', 'llmdd', 'pynccl')):
        return "communication"
    # Memory
    if any(kw in r for kw in ('mem', 'cache', 'alloc', 'swap', 'offload',
                                'block_size', 'gpu_memory', 'oom')):
        return "memory"
    # Compilation
    if any(kw in r for kw in ('compil', 'cudagraph', 'inductor', 'dynamo',
                                'kernel', 'flashinfer', 'triton', 'aot',
                                'pregrad', 'fusion', 'custom_ops')):
        return "compilation"
    # Parallelism
    if any(kw in r for kw in ('parallel', 'tensor_', 'dp_', 'tp_', 'ep_',
                                'pp_', 'pipeline', 'elastic', 'worker',
                                'multiproc', 'ray', 'spmd', 'rank')):
        return "parallelism"
    # Model
    if any(kw in r for kw in ('model', 'token', 'lora', 'quant', 'dtype',
                                'multimodal', 'mm_', 'media', 'video', 'audio',
                                'image', 'embed', 'moe', 'expert', 'speculat',
                                'pooler', 'assets', 'block_scale')):
        return "model"
    # KV cache
    if any(kw in r for kw in ('kv_cache', 'kv_events', 'kv_transfer',
                                'ec_transfer', 'prefix_cach', 'hybrid_kv')):
        return "kv_cache"
    # Scheduling
    if any(kw in r for kw in ('sched', 'queue', 'batch', 'chunk',
                                'dispatch', 'iteration', 'keep_alive')):
        return "scheduling"
    # Hardware
    if any(kw in r for kw in ('device', 'platform', 'cuda', 'rocm', 'npu',
                                'ascend', 'hpu', 'gpu', 'cpu', 'target',
                                'docker', 'precompiled', 'cudart')):
        return "hardware"
    return "other"


def categorize_by_module_path(path: str) -> str:
    """Infer a category from a source file path."""
    p = path.lower()
    if 'compilation' in p or 'compile' in p:
        return 'compilation'
    if 'cache' in p or 'kv_transfer' in p or 'kv_events' in p or 'ec_transfer' in p:
        return 'kv_cache'
    if 'parallel' in p:
        return 'parallelism'
    if 'scheduler' in p or 'sched' in p:
        return 'scheduling'
    if 'model' in p or 'lora' in p or 'multimodal' in p or 'pooler' in p:
        return 'model'
    if 'observ' in p or 'profiler' in p or 'log' in p:
        return 'observability'
    if 'speculat' in p:
        return 'speculation'
    if 'device' in p or 'platform' in p:
        return 'hardware'
    return 'other'
