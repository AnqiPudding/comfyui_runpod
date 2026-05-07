import asyncio
import base64
import html
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
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse


app = FastAPI()

LOCAL_COMFY_URL = os.getenv("LOCAL_COMFY_URL", "http://127.0.0.1:8189").rstrip("/")
LOCAL_OUTPUT_DIR = Path(os.getenv("LOCAL_OUTPUT_DIR", "output")).resolve()
RUNPOD_OUTPUT_SUBFOLDER = os.getenv("RUNPOD_OUTPUT_SUBFOLDER", "runpod")
RUNPOD_OUTPUT_DIR = (LOCAL_OUTPUT_DIR / RUNPOD_OUTPUT_SUBFOLDER).resolve()
RUNPOD_HISTORY_INDEX = RUNPOD_OUTPUT_DIR / ".runpod_proxy_history.json"
SETTINGS_PATH = Path(os.getenv("RUNPOD_PROXY_SETTINGS", "runpod_proxy_settings.json")).resolve()
RUNPOD_TIMEOUT = float(os.getenv("RUNPOD_TIMEOUT", "900"))
RUNPOD_POLL_INTERVAL = float(os.getenv("RUNPOD_POLL_INTERVAL", "5"))

_histories: dict[str, dict[str, Any]] = {}
_websockets: set[WebSocket] = set()


def _load_settings() -> dict[str, Any]:
    settings: dict[str, Any] = {
        "runpod_api_key": "",
        "runpod_endpoint_id": "",
        "runpod_endpoint": "",
        "runpod_timeout": RUNPOD_TIMEOUT,
        "runpod_poll_interval": RUNPOD_POLL_INTERVAL,
        "local_comfy_url": LOCAL_COMFY_URL,
    }

    if SETTINGS_PATH.is_file():
        try:
            loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                for key, value in loaded.items():
                    if value not in (None, ""):
                        settings[key] = value
        except Exception as exc:
            print(f"Could not load RunPod proxy settings: {exc}", flush=True)

    return settings


def _save_settings(settings: dict[str, Any]) -> None:
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:5]}...{value[-4:]}"


def _current_settings() -> dict[str, Any]:
    settings = _load_settings()
    endpoint = str(settings.get("runpod_endpoint", "")).rstrip("/")
    endpoint_id = str(settings.get("runpod_endpoint_id", "")).strip()

    if not endpoint and endpoint_id:
        endpoint = f"https://api.runpod.ai/v2/{endpoint_id}"

    return {
        **settings,
        "runpod_endpoint": endpoint,
        "runpod_endpoint_id": endpoint_id,
        "runpod_api_key": str(settings.get("runpod_api_key", "")),
        "runpod_timeout": float(settings.get("runpod_timeout") or RUNPOD_TIMEOUT),
        "runpod_poll_interval": float(settings.get("runpod_poll_interval") or RUNPOD_POLL_INTERVAL),
    }


def _public_settings() -> dict[str, Any]:
    settings = _current_settings()
    return {
        "configured": bool(settings.get("runpod_api_key") and settings.get("runpod_endpoint")),
        "runpod_endpoint_id": settings.get("runpod_endpoint_id", ""),
        "runpod_endpoint": settings.get("runpod_endpoint", ""),
        "runpod_api_key_masked": _mask_secret(settings.get("runpod_api_key", "")),
        "runpod_timeout": settings.get("runpod_timeout"),
        "runpod_poll_interval": settings.get("runpod_poll_interval"),
        "local_comfy_url": settings.get("local_comfy_url", LOCAL_COMFY_URL),
        "settings_path": str(SETTINGS_PATH),
    }


def _unwrap_runpod_response(payload: dict[str, Any]) -> dict[str, Any]:
    output = payload.get("output")
    if isinstance(output, dict):
        return output
    return payload


def _runpod_url(operation: str, job_id: str | None = None) -> str:
    endpoint = str(_current_settings().get("runpod_endpoint", "")).rstrip("/")

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
                for prompt_id, history_item in loaded.items():
                    _histories[prompt_id] = _normalize_history_item(prompt_id, history_item)
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

        if filename:
            original_name = Path(filename).name
            suffix = Path(original_name).suffix or _extension_for_mime(mime_type)
            stem = Path(original_name).stem or "image"
            filename = f"{prompt_id}_{index:03d}_{stem}{suffix}"
        else:
            filename = f"{prompt_id}_{index:03d}{_extension_for_mime(mime_type)}"

        target = _safe_output_path(subfolder, filename)
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


def _normalize_history_item(prompt_id: str, history_item: Any) -> dict[str, Any]:
    if isinstance(history_item, dict) and prompt_id in history_item and isinstance(history_item[prompt_id], dict):
        return history_item[prompt_id]
    if isinstance(history_item, dict):
        return history_item
    return _make_history(prompt_id, {"prompt": {}, "extra_data": {}}, [])


def _get_history_item(prompt_id: str) -> dict[str, Any] | None:
    history = _histories.get(prompt_id)
    if not history:
        return None
    return _normalize_history_item(prompt_id, history)


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
            target_url = str(_current_settings().get("local_comfy_url", LOCAL_COMFY_URL)).rstrip("/")
            response = await client.request(
                request.method,
                f"{target_url}/{path}",
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

    settings = _current_settings()
    deadline = time.time() + float(settings.get("runpod_timeout") or RUNPOD_TIMEOUT)

    while time.time() < deadline:
        status_response = await client.get(_runpod_url("status", job_id), headers=headers)
        status_response.raise_for_status()
        status_payload = status_response.json()
        status = status_payload.get("status")

        if status in {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}:
            return status_payload

        await asyncio.sleep(float(settings.get("runpod_poll_interval") or RUNPOD_POLL_INTERVAL))

    raise TimeoutError(f"RunPod job {job_id} did not finish within {settings.get('runpod_timeout')} seconds")


@app.websocket("/ws")
async def websocket_proxy(websocket: WebSocket) -> None:
    await websocket.accept()
    _websockets.add(websocket)
    query = urlencode(dict(websocket.query_params))
    local_comfy_url = str(_current_settings().get("local_comfy_url", LOCAL_COMFY_URL)).rstrip("/")
    target_url = local_comfy_url.replace("http://", "ws://").replace("https://", "wss://")
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
    settings = _current_settings()
    runpod_api_key = str(settings.get("runpod_api_key", ""))
    runpod_endpoint = str(settings.get("runpod_endpoint", ""))

    if not runpod_endpoint or not runpod_api_key:
        return JSONResponse(
            status_code=500,
            content={
                "error": "RunPod endpoint and API key must be configured at /runpod/settings",
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
        "Authorization": f"Bearer {runpod_api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=float(settings.get("runpod_timeout") or RUNPOD_TIMEOUT)) as client:
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
    history_item = _get_history_item(prompt_id)
    if history_item:
        return JSONResponse(content={prompt_id: history_item})

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


@app.get("/runpod/config")
async def runpod_config() -> JSONResponse:
    return JSONResponse(content=_public_settings())


@app.get("/runpod/settings")
async def runpod_settings_page() -> HTMLResponse:
    settings = _current_settings()
    public = _public_settings()
    html_body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RunPod Settings</title>
  <style>
    :root {{ color-scheme: dark; font-family: Inter, system-ui, sans-serif; }}
    body {{ margin: 0; background: #111; color: #f5f5f5; }}
    main {{ max-width: 720px; margin: 48px auto; padding: 0 24px; }}
    h1 {{ font-size: 28px; margin-bottom: 8px; }}
    p {{ color: #bbb; line-height: 1.5; }}
    form {{ display: grid; gap: 18px; margin-top: 28px; }}
    label {{ display: grid; gap: 8px; font-weight: 650; }}
    input {{ background: #1b1b1b; border: 1px solid #444; color: white; padding: 12px; border-radius: 6px; font: inherit; }}
    button, a.button {{ width: fit-content; border: 0; border-radius: 6px; background: #6f5cff; color: white; padding: 10px 16px; font-weight: 700; text-decoration: none; cursor: pointer; }}
    .row {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
    .status {{ border: 1px solid #333; border-radius: 8px; padding: 14px; background: #181818; margin-top: 18px; }}
    code {{ color: #a9d6ff; }}
  </style>
</head>
<body>
  <main>
    <h1>RunPod Settings</h1>
    <p>These values are saved only on this machine in <code>{html.escape(str(SETTINGS_PATH))}</code>. They are not committed to GitHub and do not need environment variables.</p>
    <div class="status">
      Status: <strong>{"configured" if public["configured"] else "not configured"}</strong><br>
      Current key: <code>{html.escape(str(public["runpod_api_key_masked"]))}</code>
    </div>
    <form id="settings-form">
      <label>Endpoint ID
        <input name="runpod_endpoint_id" autocomplete="off" value="{html.escape(str(settings.get("runpod_endpoint_id", "")))}" placeholder="your-endpoint-id">
      </label>
      <label>API Key
        <input name="runpod_api_key" autocomplete="off" type="password" value="" placeholder="leave blank to keep current key">
      </label>
      <label>Full Endpoint URL Override
        <input name="runpod_endpoint" autocomplete="off" value="{html.escape(str(settings.get("runpod_endpoint", "")))}" placeholder="https://api.runpod.ai/v2/endpoint-id">
      </label>
      <label>Local ComfyUI URL
        <input name="local_comfy_url" autocomplete="off" value="{html.escape(str(settings.get("local_comfy_url", LOCAL_COMFY_URL)))}">
      </label>
      <div class="row">
        <label>Timeout seconds
          <input name="runpod_timeout" type="number" min="30" value="{html.escape(str(settings.get("runpod_timeout", RUNPOD_TIMEOUT)))}">
        </label>
        <label>Poll interval seconds
          <input name="runpod_poll_interval" type="number" min="1" value="{html.escape(str(settings.get("runpod_poll_interval", RUNPOD_POLL_INTERVAL)))}">
        </label>
      </div>
      <div class="row">
        <button type="submit">Save</button>
        <a class="button" href="/">Back to ComfyUI</a>
      </div>
    </form>
    <p id="message"></p>
  </main>
  <script>
    document.getElementById('settings-form').addEventListener('submit', async (event) => {{
      event.preventDefault();
      const form = event.currentTarget;
      const payload = Object.fromEntries(new FormData(form).entries());
      payload.runpod_timeout = Number(payload.runpod_timeout || 900);
      payload.runpod_poll_interval = Number(payload.runpod_poll_interval || 5);
      const response = await fetch('/runpod/settings', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload)
      }});
      const result = await response.json();
      document.getElementById('message').textContent = response.ok ? 'Saved. You can return to ComfyUI and queue.' : (result.error || 'Could not save settings.');
    }});
  </script>
</body>
</html>"""
    return HTMLResponse(content=html_body)


@app.post("/runpod/settings")
async def save_runpod_settings(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    current = _current_settings()
    allowed = {
        "runpod_api_key",
        "runpod_endpoint_id",
        "runpod_endpoint",
        "runpod_timeout",
        "runpod_poll_interval",
        "local_comfy_url",
    }
    next_settings = {key: current.get(key, "") for key in allowed}
    for key in allowed:
        if key in payload:
            if key == "runpod_api_key" and payload[key] == "":
                continue
            next_settings[key] = payload[key]

    if not str(next_settings.get("runpod_endpoint", "")).strip() and str(next_settings.get("runpod_endpoint_id", "")).strip():
        next_settings["runpod_endpoint"] = f"https://api.runpod.ai/v2/{str(next_settings['runpod_endpoint_id']).strip()}"

    _save_settings(next_settings)
    return JSONResponse(content=_public_settings())


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


def _inject_settings_button(content: bytes, content_type: str) -> bytes:
    if "text/html" not in content_type.lower():
        return content

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content

    if "runpod-settings-button" in text or "</body>" not in text:
        return content

    script = """
<script>
(() => {
  const mount = () => {
    if (document.getElementById('runpod-settings-button')) return;
    const button = document.createElement('a');
    button.id = 'runpod-settings-button';
    button.href = '/runpod/settings';
    button.textContent = 'RunPod';
    button.title = 'RunPod endpoint settings';
    Object.assign(button.style, {
      position: 'fixed',
      right: '16px',
      bottom: '16px',
      zIndex: '2147483647',
      background: '#6f5cff',
      color: '#fff',
      padding: '10px 14px',
      borderRadius: '6px',
      font: '700 13px system-ui, sans-serif',
      textDecoration: 'none',
      boxShadow: '0 8px 28px rgba(0,0,0,.35)'
    });
    document.body.appendChild(button);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
})();
</script>
"""
    return text.replace("</body>", f"{script}</body>").encode("utf-8")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def catch_all(request: Request, path: str) -> Response:
    response = await proxy_request(request, path)
    if request.method == "GET" and path in {"", "index.html"}:
        content_type = response.headers.get("content-type", "")
        content = _inject_settings_button(response.body, content_type)
        response.headers["content-length"] = str(len(content))
        return Response(content=content, status_code=response.status_code, headers=dict(response.headers), media_type=content_type)
    return response


if __name__ == "__main__":
    _load_history_index()
    print("Starting local RunPod proxy on http://127.0.0.1:8188")
    print(f"Proxying UI metadata to local ComfyUI at {LOCAL_COMFY_URL}")
    uvicorn.run(app, host=os.getenv("PROXY_HOST", "127.0.0.1"), port=int(os.getenv("PROXY_PORT", "8188")))
