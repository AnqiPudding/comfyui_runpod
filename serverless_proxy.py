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
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse


app = FastAPI()

LOCAL_COMFY_URL = os.getenv("LOCAL_COMFY_URL", "http://127.0.0.1:8189").rstrip("/")
LOCAL_OUTPUT_DIR = Path(os.getenv("LOCAL_OUTPUT_DIR", "output")).resolve()
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "")
RUNPOD_ENDPOINT = os.getenv("RUNPOD_ENDPOINT", "")
RUNPOD_TIMEOUT = float(os.getenv("RUNPOD_TIMEOUT", "900"))

if not RUNPOD_ENDPOINT and RUNPOD_ENDPOINT_ID:
    RUNPOD_ENDPOINT = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/runsync"

_histories: dict[str, dict[str, Any]] = {}


def _unwrap_runpod_response(payload: dict[str, Any]) -> dict[str, Any]:
    output = payload.get("output")
    if isinstance(output, dict):
        return output
    return payload


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

    if not str(target).startswith(str(LOCAL_OUTPUT_DIR)):
        raise ValueError("Requested output path escapes LOCAL_OUTPUT_DIR")

    return target


def _save_runpod_images(prompt_id: str, runpod_output: dict[str, Any]) -> list[dict[str, str]]:
    saved: list[dict[str, str]] = []
    images = runpod_output.get("images") or []
    subfolder = "runpod"

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
    return {
        prompt_id: {
            "prompt": [
                0,
                prompt_id,
                comfy_payload.get("prompt", {}),
                comfy_payload.get("extra_data", {}),
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


@app.websocket("/ws")
async def websocket_proxy(websocket: WebSocket) -> None:
    await websocket.accept()
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


@app.post("/prompt")
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
            response = await client.post(RUNPOD_ENDPOINT, headers=headers, json=runpod_payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return JSONResponse(
                status_code=502,
                content={"error": f"RunPod HTTP {exc.response.status_code}", "body": exc.response.text},
            )
        except Exception as exc:
            return JSONResponse(status_code=500, content={"error": str(exc)})

    runpod_response = response.json()
    runpod_output = _unwrap_runpod_response(runpod_response)

    if runpod_output.get("status") == "error":
        return JSONResponse(status_code=502, content={"error": runpod_output.get("error", "RunPod job failed")})

    prompt_id = f"runpod-{uuid.uuid4()}"
    saved_images = _save_runpod_images(prompt_id, runpod_output)
    _histories[prompt_id] = _make_history(prompt_id, comfy_payload, saved_images)

    return JSONResponse(
        content={
            "prompt_id": prompt_id,
            "number": 0,
            "node_errors": {},
        }
    )


@app.get("/history")
async def history_all() -> JSONResponse:
    return JSONResponse(content=_histories)


@app.get("/history/{prompt_id}")
async def history(prompt_id: str) -> JSONResponse:
    if prompt_id in _histories:
        return JSONResponse(content=_histories[prompt_id])

    return JSONResponse(content={})


@app.get("/view")
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
    print("Starting local RunPod proxy on http://127.0.0.1:8188")
    print(f"Proxying UI metadata to local ComfyUI at {LOCAL_COMFY_URL}")
    uvicorn.run(app, host=os.getenv("PROXY_HOST", "127.0.0.1"), port=int(os.getenv("PROXY_PORT", "8188")))
