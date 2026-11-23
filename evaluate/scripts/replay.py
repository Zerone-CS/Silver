"""Verify retained streams using the same protocol parser as the live benchmark."""
from . import benchmark


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


class ReplaySink:
    """The collector parser can replay an already-retained stream using its normal reader."""
    def refresh_timeout(self):
        pass

    def remaining(self):
        pass

    def write(self, data):
        return len(data)


def verify_stream(stream_path, request, recorded_output):
    state = dict.fromkeys(("finish_reason", "first_output_ts", "last_output_ts", "actual_prompt_tokens",
                           "actual_output_tokens", "token_count_source"))
    state.update(output_event_ts=[], stream_done_received=False, final_usage_received=False)
    channels = {"content": [], "reasoning_content": []}
    sink = ReplaySink()
    with stream_path.open("rb") as stream:
        benchmark.read_sse(stream, sink, sink, state, channels, None)
        require(not stream.read(1), "unexpected bytes after recorded DONE")
    for key in ("actual_output_tokens", "actual_prompt_tokens", "finish_reason", "token_count_source",
                "stream_done_received", "final_usage_received"):
        require(state[key] == request[key], "request record and SSE disagree: " + key)
    require(len(state["output_event_ts"]) == len(request["output_event_ts"]), "SSE event count differs")
    require(recorded_output == {k: "".join(v) for k, v in channels.items()}, "output differs from retained SSE")
