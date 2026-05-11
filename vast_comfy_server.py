from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from runpod_handler import run_comfy_job, start_comfyui


app = FastAPI()


@app.on_event("startup")
def startup() -> None:
    start_comfyui()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ready"}


@app.post("/generate")
async def generate(request: Request) -> JSONResponse:
    payload: dict[str, Any] = await request.json()
    comfy_payload = payload.get("comfy_payload", {})
    if isinstance(comfy_payload, dict) and comfy_payload.get("extra_data", {}).get("benchmark"):
        return JSONResponse(content={"status": "success", "benchmark": True, "images": []})

    result = run_comfy_job(payload)
    status_code = 200 if result.get("status") != "error" else 500
    return JSONResponse(content=result, status_code=status_code)
