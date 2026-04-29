import base64
import json
import mimetypes
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import runpod


COMFYUI_DIR = os.getenv("COMFYUI_DIR", "/workspace/ComfyUI")
COMFY_HOST = os.getenv("COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.getenv("COMFY_PORT", "8188"))
COMFY_URL = os.getenv("COMFY_URL", f"http://{COMFY_HOST}:{COMFY_PORT}")
STARTUP_TIMEOUT = int(os.getenv("COMFY_STARTUP_TIMEOUT", "180"))
POLL_INTERVAL = float(os.getenv("COMFY_POLL_INTERVAL", "1"))
JOB_TIMEOUT = int(os.getenv("COMFY_JOB_TIMEOUT", "900"))
RETURN_IMAGES = os.getenv("RETURN_IMAGES", "base64").lower()

_comfy_process: subprocess.Popen | None = None


def _json_request(path: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> dict[str, Any]:
    data = None
    headers = {}

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(f"{COMFY_URL}{path}", data=data, headers=headers)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ComfyUI HTTP {exc.code} for {path}: {body}") from exc


def start_comfyui() -> None:
    global _comfy_process

    if _comfy_process and _comfy_process.poll() is None:
        return

    args = [
        "python",
        "main.py",
        "--listen",
        COMFY_HOST,
        "--port",
        str(COMFY_PORT),
        "--disable-auto-launch",
    ]

    extra_args = os.getenv("COMFY_EXTRA_ARGS", "").strip()
    if extra_args:
        args.extend(extra_args.split())

    print(f"Starting ComfyUI: {' '.join(args)}", flush=True)
    _comfy_process = subprocess.Popen(args, cwd=COMFYUI_DIR)

    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if _comfy_process.poll() is not None:
            raise RuntimeError(f"ComfyUI exited early with code {_comfy_process.returncode}")

        try:
            _json_request("/system_stats", timeout=5)
            print("ComfyUI server is ready.", flush=True)
            return
        except Exception:
            time.sleep(1)

    raise TimeoutError(f"ComfyUI did not become ready within {STARTUP_TIMEOUT} seconds")


def _normalize_prompt_payload(job_input: dict[str, Any]) -> dict[str, Any]:
    if "comfy_payload" in job_input:
        comfy_payload = job_input["comfy_payload"]
        if not isinstance(comfy_payload, dict):
            raise ValueError("input.comfy_payload must be a JSON object")
        return comfy_payload

    if "prompt" in job_input:
        return job_input

    if "workflow" in job_input:
        payload: dict[str, Any] = {"prompt": job_input["workflow"]}
        for key in ("client_id", "extra_data", "front", "number", "prompt_id", "partial_execution_targets"):
            if key in job_input:
                payload[key] = job_input[key]
        return payload

    raise ValueError("Expected input.comfy_payload, input.prompt, or input.workflow")


def queue_prompt(comfy_payload: dict[str, Any]) -> dict[str, Any]:
    if "prompt" not in comfy_payload:
        raise ValueError("ComfyUI payload must contain a 'prompt' field")
    return _json_request("/prompt", comfy_payload, timeout=60)


def get_history(prompt_id: str) -> dict[str, Any]:
    deadline = time.time() + JOB_TIMEOUT

    while time.time() < deadline:
        history = _json_request(f"/history/{urllib.parse.quote(prompt_id)}", timeout=30)
        if prompt_id in history:
            return history[prompt_id]
        time.sleep(POLL_INTERVAL)

    raise TimeoutError(f"Prompt {prompt_id} did not finish within {JOB_TIMEOUT} seconds")


def get_image(filename: str, subfolder: str, folder_type: str) -> tuple[str, str]:
    query = urllib.parse.urlencode(
        {
            "filename": filename,
            "subfolder": subfolder,
            "type": folder_type,
        }
    )
    request = urllib.request.Request(f"{COMFY_URL}/view?{query}")

    with urllib.request.urlopen(request, timeout=120) as response:
        image_data = response.read()
        mime = response.headers.get_content_type()
        if not mime or mime == "application/octet-stream":
            mime = mimetypes.guess_type(filename)[0] or "image/png"
        return base64.b64encode(image_data).decode("ascii"), mime


def collect_images(history: dict[str, Any]) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []

    for node_id, node_output in history.get("outputs", {}).items():
        for image in node_output.get("images", []):
            item = {
                "node_id": node_id,
                "filename": image["filename"],
                "subfolder": image.get("subfolder", ""),
                "type": image.get("type", "output"),
            }

            if RETURN_IMAGES == "base64":
                data, mime = get_image(item["filename"], item["subfolder"], item["type"])
                item["data"] = data
                item["mime_type"] = mime

            images.append(item)

    return images


def handler(job: dict[str, Any]) -> dict[str, Any]:
    try:
        start_comfyui()

        job_input = job.get("input") or {}
        comfy_payload = _normalize_prompt_payload(job_input)

        print("Queuing workflow...", flush=True)
        prompt_response = queue_prompt(comfy_payload)
        prompt_id = prompt_response["prompt_id"]

        print(f"Workflow queued. Prompt ID: {prompt_id}", flush=True)
        history = get_history(prompt_id)
        images = collect_images(history)

        return {
            "status": "success",
            "prompt_id": prompt_id,
            "images": images,
            "history": history if os.getenv("RETURN_HISTORY", "0") == "1" else None,
        }
    except Exception as exc:
        print(f"RunPod handler error: {exc}", flush=True)
        return {"status": "error", "error": str(exc)}


if __name__ == "__main__":
    start_comfyui()
    print("Starting RunPod Serverless worker...", flush=True)
    runpod.serverless.start({"handler": handler})
