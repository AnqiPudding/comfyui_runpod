import os
from typing import Any

from vastai import BenchmarkConfig, HandlerConfig, LogActionConfig, Worker, WorkerConfig


MODEL_SERVER_URL = os.getenv("VAST_MODEL_SERVER_URL", "http://127.0.0.1")
MODEL_SERVER_PORT = int(os.getenv("VAST_MODEL_SERVER_PORT", "18000"))
MODEL_LOG_FILE = os.getenv("VAST_MODEL_LOG_FILE", "/workspace/logs/vast_comfy_server.log")


def workload(payload: dict[str, Any]) -> float:
    prompt = payload.get("comfy_payload", {}).get("prompt", {})
    if isinstance(prompt, dict):
        return max(1.0, float(len(prompt)))
    return 1.0


def benchmark_payload() -> dict[str, Any]:
    return {
        "comfy_payload": {
            "prompt": {},
            "extra_data": {"benchmark": True},
        }
    }


worker_config = WorkerConfig(
    model_server_url=MODEL_SERVER_URL,
    model_server_port=MODEL_SERVER_PORT,
    model_log_file=MODEL_LOG_FILE,
    handlers=[
        HandlerConfig(
            route="/generate",
            allow_parallel_requests=False,
            max_queue_time=float(os.getenv("VAST_MAX_QUEUE_TIME", "900")),
            workload_calculator=workload,
            benchmark_config=BenchmarkConfig(
                generator=benchmark_payload,
                runs=1,
                concurrency=1,
            ),
        ),
    ],
    log_action_config=LogActionConfig(
        on_load=["Uvicorn running on", "Application startup complete."],
        on_error=["Traceback (most recent call last):", "RuntimeError", "ERROR:"],
        on_info=["Starting ComfyUI", "Restored input image", "Queuing workflow"],
    ),
)


if __name__ == "__main__":
    Worker(worker_config).run()
