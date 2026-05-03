import asyncio
import base64
import json
import mimetypes
import os
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import uvicorn
import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse


load_dotenv()

app = FastAPI()

LOCAL_COMFY_URL = os.getenv("LOCAL_COMFY_URL", "http://127.0.0.1:8189").rstrip("/")
LOCAL_OUTPUT_DIR = Path(os.getenv("LOCAL_OUTPUT_DIR", "output")).resolve()
RUNPOD_OUTPUT_SUBFOLDER = os.getenv("RUNPOD_OUTPUT_SUBFOLDER", "runpod")
RUNPOD_OUTPUT_DIR = (LOCAL_OUTPUT_DIR / RUNPOD_OUTPUT_SUBFOLDER).resolve()
RUNPOD_HISTORY_INDEX = RUNPOD_OUTPUT_DIR / ".runpod_proxy_history.json"
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "")
RUNPOD_ENDPOINT = os.getenv("RUNPOD_ENDPOINT", "")
RUNPOD_TIMEOUT = float(os.getenv("RUNPOD_TIMEOUT", "900"))
RUNPOD_POLL_INTERVAL = float(os.getenv("RUNPOD_POLL_INTERVAL", "5"))

if not RUNPOD_ENDPOINT and RUNPOD_ENDPOINT_ID:
    RUNPOD_ENDPOINT = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}"

_histories: dict[str, dict[str, Any]] = {}
_websockets: set[WebSocket] = set()


def _unwrap_runpod_response(payload: dict[str, Any]) -> dict[str, Any]:
    output = payload.get("output")
    if isinstance(output, dict):
        return output
    return payload


def _runpod_url(operation: str, job_id: str | None = None) -> str:
    endpoint = RUNPOD_ENDPOINT.rstrip("/")

    if endpoint.endswith("/runsync"):
        endpoint = endpoint[: -len("/runsync")]
    elif endpoint.endswith("/run"):
        endpoint = endpoint[: -len("/run")]

    if operation == "status":
        if not job_id:
            raise ValueError("job_id is required for RunPod status polling")
        return f"{endpoint}/status/{job_id}"

    return f"{endpoint}/{operation}"


def _decode_image_data(data: str) -> bytes:
    if "," in data and data.strip().lower().startswith("data:"):
        data = data.split(",", 1)[1]
    return base64.b64decode(data)


def _extension_for_mime(mime_type: str | None, fallback: str = ".png") -> str:
    if not mime_type:
        return fallback
    return mimetypes.guess_extension(mime_type.split(";", 1)[0]) or fallback


def _safe_output_path(subfolder: str, filename: str) -> Path:
    target_dir = (LOCAL_OUTPUT_DIR / subfolder).resolve()
    target = (target_dir / filename).resolve()

    try:
        target.relative_to(LOCAL_OUTPUT_DIR)
    except ValueError:
        raise ValueError("Requested output path escapes LOCAL_OUTPUT_DIR")

    return target


def _load_history_index() -> None:
    if RUNPOD_HISTORY_INDEX.is_file():
        try:
            loaded = json.loads(RUNPOD_HISTORY_INDEX.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                _histories.update(loaded)
                return
        except Exception as exc:
            print(f"Could not load RunPod history index: {exc}", flush=True)

    if not RUNPOD_OUTPUT_DIR.is_dir():
        return

    images: list[dict[str, str]] = []
    for path in sorted(RUNPOD_OUTPUT_DIR.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if not path.is_file() or path.name.startswith("."):
            continue
        mime_type = mimetypes.guess_type(path.name)[0] or ""
        if not mime_type.startswith("image/"):
            continue
        images.append({"filename": path.name, "subfolder": RUNPOD_OUTPUT_SUBFOLDER, "type": "output"})

    if images:
        prompt_id = "runpod-restored"
        _histories[prompt_id] = _make_history(prompt_id, {"prompt": {}, "extra_data": {}}, images)
        _save_history_index()


def _save_history_index() -> None:
    try:
        RUNPOD_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        RUNPOD_HISTORY_INDEX.write_text(json.dumps(_histories, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"Could not save RunPod history index: {exc}", flush=True)


async def _broadcast(event: str, data: dict[str, Any]) -> None:
    message = {"type": event, "data": data}
    stale: list[WebSocket] = []

    for socket in list(_websockets):
        try:
            await socket.send_json(message)
        except Exception:
            stale.append(socket)

    for socket in stale:
        _websockets.discard(socket)


async def _notify_prompt_finished(prompt_id: str, images: list[dict[str, str]]) -> None:
    # Let the /prompt response reach the browser first, so the UI knows this prompt_id.
    await asyncio.sleep(0.1)
    await _broadcast("execution_start", {"prompt_id": prompt_id})
    await _broadcast("executing", {"node": "runpod", "display_node": "runpod", "prompt_id": prompt_id})
    await _broadcast(
        "executed",
        {
            "node": "runpod",
            "display_node": "runpod",
            "output": {"images": images},
            "prompt_id": prompt_id,
        },
    )
    await _broadcast("execution_success", {"prompt_id": prompt_id})
    await _broadcast("executing", {"node": None, "prompt_id": prompt_id})
    await _broadcast("status", {"status": {"exec_info": {"queue_remaining": 0}}})


def _save_runpod_images(prompt_id: str, runpod_output: dict[str, Any]) -> list[dict[str, str]]:
    saved: list[dict[str, str]] = []
    images = runpod_output.get("images") or []
    subfolder = RUNPOD_OUTPUT_SUBFOLDER

    for index, image in enumerate(images):
        if not isinstance(image, dict):
            continue

        filename = image.get("filename")
        mime_type = image.get("mime_type") or image.get("mime")
        data = image.get("data") or image.get("base64")

        if not data:
            continue

        if not filename:
            filename = f"{prompt_id}_{index:03d}{_extension_for_mime(mime_type)}"

        target = _safe_output_path(subfolder, Path(filename).name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_decode_image_data(data))

        saved.append(
            {
                "filename": target.name,
                "subfolder": subfolder,
                "type": "output",
            }
        )

    return saved


def _make_history(prompt_id: str, comfy_payload: dict[str, Any], images: list[dict[str, str]]) -> dict[str, Any]:
    extra_data = comfy_payload.get("extra_data", {}) or {}
    extra_data.setdefault("create_time", time.time())

    return {
        prompt_id: {
            "prompt": [
                0,
                prompt_id,
                comfy_payload.get("prompt", {}),
                extra_data,
                [],
            ],
            "outputs": {
                "runpod": {
                    "images": images,
                }
            },
            "status": {
                "status_str": "success",
                "completed": True,
                "messages": [],
            },
            "meta": {},
        }
    }


def _get_history_item(prompt_id: str) -> dict[str, Any] | None:
    history = _histories.get(prompt_id)
    if not history:
        return None
    return history.get(prompt_id)


def _make_job(prompt_id: str, include_outputs: bool = False) -> dict[str, Any] | None:
    history_item = _get_history_item(prompt_id)
    if not history_item:
        return None

    prompt = history_item.get("prompt", [])
    extra_data = prompt[3] if len(prompt) > 3 and isinstance(prompt[3], dict) else {}
    outputs = history_item.get("outputs", {})
    images = outputs.get("runpod", {}).get("images", [])
    images_with_media = [
        {
            **image,
            "nodeId": image.get("nodeId", "runpod"),
            "mediaType": image.get("mediaType", "images"),
        }
        for image in images
    ]
    preview_output = None

    if images_with_media:
        preview_output = {
            **images_with_media[0],
        }

    job = {
        "id": prompt_id,
        "status": "completed",
        "priority": prompt[0] if prompt else 0,
        "create_time": extra_data.get("create_time"),
        "execution_end_time": time.time(),
        "outputs_count": len(images_with_media),
        "preview_output": preview_output,
        "workflow_id": extra_data.get("extra_pnginfo", {}).get("workflow", {}).get("id"),
    }

    if include_outputs:
        job["outputs"] = {"runpod": {"images": images_with_media}}
        job["execution_status"] = history_item.get("status", {})
        job["workflow"] = {
            "prompt": prompt[2] if len(prompt) > 2 else {},
            "extra_data": extra_data,
        }

    return {key: value for key, value in job.items() if value is not None}


def _parse_status_filter(status_param: str | None) -> set[str]:
    if not status_param:
        return {"pending", "in_progress", "completed", "failed", "cancelled"}
    return {status.strip().lower() for status in status_param.split(",") if status.strip()}


async def proxy_request(request: Request, path: str) -> Response:
    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("origin", None)
    headers.pop("referer", None)

    async with httpx.AsyncClient(timeout=RUNPOD_TIMEOUT) as client:
        try:
            response = await client.request(
                request.method,
                f"{LOCAL_COMFY_URL}/{path}",
                headers=headers,
                content=body,
                params=request.query_params,
            )
        except httpx.ConnectError:
            return JSONResponse(
                status_code=502,
                content={"error": f"Could not connect to local ComfyUI at {LOCAL_COMFY_URL}"},
            )

    excluded = {"content-encoding", "transfer-encoding", "connection"}
    response_headers = {k: v for k, v in response.headers.items() if k.lower() not in excluded}
    return Response(content=response.content, status_code=response.status_code, headers=response_headers)


async def submit_runpod_job(client: httpx.AsyncClient, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    response = await client.post(_runpod_url("run"), headers=headers, json=payload)
    response.raise_for_status()
    submitted = response.json()

    job_id = submitted.get("id")
    if not job_id:
        return submitted

    deadline = time.time() + RUNPOD_TIMEOUT

    while time.time() < deadline:
        status_response = await client.get(_runpod_url("status", job_id), headers=headers)
        status_response.raise_for_status()
        status_payload = status_response.json()
        status = status_payload.get("status")

        if status in {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}:
            return status_payload

        await asyncio.sleep(RUNPOD_POLL_INTERVAL)

    raise TimeoutError(f"RunPod job {job_id} did not finish within {RUNPOD_TIMEOUT} seconds")


@app.websocket("/ws")
async def websocket_proxy(websocket: WebSocket) -> None:
    await websocket.accept()
    _websockets.add(websocket)
    query = urlencode(dict(websocket.query_params))
    target_url = LOCAL_COMFY_URL.replace("http://", "ws://").replace("https://", "wss://")
    target_url = f"{target_url}/ws"
    if query:
        target_url = f"{target_url}?{query}"

    try:
        async with websockets.connect(target_url) as upstream:
            async def client_to_upstream() -> None:
                try:
                    while True:
                        client_message = await websocket.receive()
                        if "text" in client_message:
                            await upstream.send(client_message["text"])
                        elif "bytes" in client_message:
                            await upstream.send(client_message["bytes"])
                except WebSocketDisconnect:
                    await upstream.close()

            async def upstream_to_client() -> None:
                async for upstream_message in upstream:
                    if isinstance(upstream_message, bytes):
                        await websocket.send_bytes(upstream_message)
                    else:
                        await websocket.send_text(upstream_message)

            await asyncio.gather(client_to_upstream(), upstream_to_client())
    except Exception:
        try:
            await websocket.close()
        except RuntimeError:
            pass
    finally:
        _websockets.discard(websocket)


@app.post("/prompt")
@app.post("/api/prompt")
async def prompt(request: Request) -> JSONResponse:
    if not RUNPOD_ENDPOINT or not RUNPOD_API_KEY:
        return JSONResponse(
            status_code=500,
            content={
                "error": "RUNPOD_ENDPOINT or RUNPOD_ENDPOINT_ID and RUNPOD_API_KEY must be set",
            },
        )

    try:
        comfy_payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    if "prompt" not in comfy_payload:
        return JSONResponse(status_code=400, content={"error": "ComfyUI payload is missing 'prompt'"})

    runpod_payload = {
        "input": {
            "comfy_payload": comfy_payload,
        }
    }
    headers = {
        "Authorization": f"Bearer {RUNPOD_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=RUNPOD_TIMEOUT) as client:
        try:
            runpod_response = await submit_runpod_job(client, runpod_payload, headers)
        except httpx.HTTPStatusError as exc:
            return JSONResponse(
                status_code=502,
                content={"error": f"RunPod HTTP {exc.response.status_code}", "body": exc.response.text},
            )
        except Exception as exc:
            return JSONResponse(status_code=500, content={"error": str(exc)})

    runpod_output = _unwrap_runpod_response(runpod_response)

    if runpod_response.get("status") in {"FAILED", "CANCELLED", "TIMED_OUT"}:
        return JSONResponse(status_code=502, content={"error": "RunPod job failed", "runpod_response": runpod_response})

    if runpod_output.get("status") == "error":
        return JSONResponse(status_code=502, content={"error": runpod_output.get("error", "RunPod job failed")})

    prompt_id = f"runpod-{uuid.uuid4()}"
    saved_images = _save_runpod_images(prompt_id, runpod_output)
    _histories[prompt_id] = _make_history(prompt_id, comfy_payload, saved_images)
    _save_history_index()
    asyncio.create_task(_notify_prompt_finished(prompt_id, saved_images))

    return JSONResponse(
        content={
            "prompt_id": prompt_id,
            "number": 0,
            "node_errors": {},
        }
    )


@app.get("/history")
@app.get("/api/history")
async def history_all() -> JSONResponse:
    return JSONResponse(content=_histories)


@app.get("/history/{prompt_id}")
@app.get("/api/history/{prompt_id}")
async def history(prompt_id: str) -> JSONResponse:
    if prompt_id in _histories:
        return JSONResponse(content=_histories[prompt_id])

    return JSONResponse(content={})


@app.get("/api/jobs")
async def jobs(request: Request) -> JSONResponse:
    statuses = _parse_status_filter(request.query_params.get("status"))
    workflow_id = request.query_params.get("workflow_id")
    sort_order = request.query_params.get("sort_order", "desc").lower()

    try:
        limit = int(request.query_params.get("limit", "0") or "0")
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "limit must be an integer"})

    try:
        offset = max(0, int(request.query_params.get("offset", "0") or "0"))
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "offset must be an integer"})

    all_jobs = []
    if "completed" in statuses:
        for prompt_id in _histories:
            job = _make_job(prompt_id)
            if not job:
                continue
            if workflow_id and job.get("workflow_id") != workflow_id:
                continue
            all_jobs.append(job)

    all_jobs.sort(key=lambda job: job.get("create_time", 0), reverse=sort_order != "asc")
    total = len(all_jobs)
    page = all_jobs[offset:]
    if limit > 0:
        page = page[:limit]

    return JSONResponse(
        content={
            "jobs": page,
            "pagination": {
                "offset": offset,
                "limit": limit if limit > 0 else None,
                "total": total,
                "has_more": offset + len(page) < total,
            },
        }
    )


@app.get("/api/jobs/{prompt_id}")
async def job(prompt_id: str) -> JSONResponse:
    item = _make_job(prompt_id, include_outputs=True)
    if not item:
        return JSONResponse(status_code=404, content={"error": "job not found"})
    return JSONResponse(content=item)


@app.get("/view")
@app.get("/api/view")
async def view(request: Request) -> Response:
    filename = request.query_params.get("filename", "")
    subfolder = request.query_params.get("subfolder", "")
    folder_type = request.query_params.get("type", "output")

    if folder_type == "output" and filename:
        target = _safe_output_path(subfolder, filename)
        if target.is_file():
            return FileResponse(target)

    return await proxy_request(request, "view")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def catch_all(request: Request, path: str) -> Response:
    return await proxy_request(request, path)


if __name__ == "__main__":
    _load_history_index()
    print("Starting local RunPod proxy on http://127.0.0.1:8188")
    print(f"Proxying UI metadata to local ComfyUI at {LOCAL_COMFY_URL}")
    uvicorn.run(app, host=os.getenv("PROXY_HOST", "127.0.0.1"), port=int(os.getenv("PROXY_PORT", "8188")))
