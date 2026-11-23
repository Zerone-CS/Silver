"""Local diagnostic instrument for vLLM 0.23.0 on one independent engine."""
import json
import os
from pathlib import Path
import time

from vllm.v1.core.sched.async_scheduler import AsyncScheduler
from vllm.v1.core.sched.scheduler import Scheduler
from vllm.v1.request import Request


class Capture:
    def __init__(self, cfg):
        p = cfg.parallel_config
        if (p.data_parallel_size, p.pipeline_parallel_size, p.tensor_parallel_size) != (1, 1, 1):
            raise ValueError("site probe validated only for DP=PP=TP=1")
        if not cfg.observability_config.enable_logging_iteration_details:
            raise ValueError("native iteration logs required for independent counts")
        self.root = Path(os.environ["SITE_LEDGER_DIR"])
        self.gate = self.root / "active"
        self.pending = {}
        self.positions = {}
        self.seq = 0
        self.handle = (self.root / ("engine-%d.jsonl" % os.getpid())).open("x", buffering=1)

        # v1.request.Request deliberately drops external_req_id. Record both IDs
        # at its construction seam, in this process only, before that information is lost.
        original = Request.from_engine_core_request.__func__

        def from_request(cls, request, block_hasher):
            result = original(cls, request, block_hasher)
            gate = self.active()
            if gate:
                self.write(gate, "bridge", response_id=request.external_req_id, backend_id=result.request_id)
            return result

        Request.from_engine_core_request = classmethod(from_request)

    def active(self):
        try:
            return self.gate.read_text().strip() or None
        except FileNotFoundError:
            return None

    def write(self, capture, kind, **fields):
        row = dict(capture_id=capture, kind=kind, monotonic_ns=time.monotonic_ns(), **fields)
        self.handle.write(json.dumps(row, separators=(",", ":")) + "\n")

    def scheduled(self, out):
        if not out.total_num_scheduled_tokens:
            return
        capture = self.active()
        if not capture:
            return
        self.seq += 1
        self.pending[id(out)] = (out, capture, self.seq)
        self.write(capture, "schedule", step_id=self.seq, scheduled=dict(out.num_scheduled_tokens))

    def completed(self, out, bundles):
        item = self.pending.pop(id(out), None)
        if not item:
            return
        original, capture, step = item
        if original is not out or self.active() != capture:
            self.write(capture, "error", step_id=step, reason="engine step crosses capture gate")
            return
        self.write(capture, "complete", step_id=step)
        for bundle in bundles.values():
            for event in bundle.outputs:
                count = len(event.new_token_ids)
                if count:
                    key = (capture, event.request_id)
                    first = self.positions.get(key, 0)
                    self.write(capture, "commit", step_id=step, backend_id=event.request_id,
                               output_start=first, output_count=count)
                    self.positions[key] = first + count
                if event.finish_reason is not None:
                    self.write(capture, "finish", step_id=step, backend_id=event.request_id,
                               reason=str(event.finish_reason))


class Observed:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._site_capture = Capture(kwargs["vllm_config"])

    def schedule(self):
        out = super().schedule()
        self._site_capture.scheduled(out)
        return out

    def update_from_output(self, scheduler_output, model_runner_output):
        result = super().update_from_output(scheduler_output, model_runner_output)
        self._site_capture.completed(scheduler_output, result)
        return result


class ObservedSync(Observed, Scheduler):
    pass


class ObservedAsync(Observed, AsyncScheduler):
    pass


class SiteScheduler:
    def __new__(cls, **kwargs):
        config = kwargs["vllm_config"].scheduler_config
        base = ObservedAsync if config.async_scheduling else ObservedSync
        return base(**kwargs)
