# comfyui_runpod

Deployment overlay for running ComfyUI on RunPod Serverless while using a local ComfyUI UI as the workflow editor.

## What This Repo Contains

- `Dockerfile`: builds a RunPod worker image from ComfyUI plus the custom nodes used in the local setup.
- `runpod_handler.py`: starts ComfyUI inside the worker, forwards the real ComfyUI `/prompt` payload, waits for completion, and returns generated images.
- `serverless_proxy.py`: local FastAPI proxy that serves the normal ComfyUI UI from a local CPU instance, intercepts `/prompt`, sends it to RunPod, saves returned images locally, and emulates `/history` and `/view` enough for the UI to show results.
- `extra_model_paths.yaml`: points ComfyUI at `/runpod-volume/models`.
- `custom_nodes/`: local-only custom nodes that are not cloned by the Dockerfile.

## Local UI Flow

1. Start your local ComfyUI on port `8189`.

   ```powershell
   python main.py --listen 127.0.0.1 --port 8189
   ```

2. Install proxy dependencies.

   ```powershell
   python -m pip install -r requirements-proxy.txt
   ```

3. Set environment variables.

   ```powershell
   $env:RUNPOD_API_KEY="your-api-key"
   $env:RUNPOD_ENDPOINT_ID="your-endpoint-id"
   $env:LOCAL_COMFY_URL="http://127.0.0.1:8189"
   ```

4. Start the proxy.

   ```powershell
   python serverless_proxy.py
   ```

5. Open `http://127.0.0.1:8188`.

The UI loads from local ComfyUI, but queueing a prompt sends the execution payload to RunPod Serverless.

## Build Image Later

Build from this repo root:

```bash
docker build -t comfyui-runpod .
```

RunPod should mount persistent models at `/runpod-volume/models`, matching `extra_model_paths.yaml`.
The RunPod model paths are marked as `is_default: true`, so custom nodes that call ComfyUI's model-folder APIs, including the Civitai downloader node, write to the persistent volume first.

## Build With GitHub Actions

This repo includes `.github/workflows/docker-publish.yml`. It builds the Docker image in GitHub Actions and pushes it to Docker Hub whenever `main` changes, or when you run the workflow manually.

In GitHub, open this repo and add these repository secrets:

- `DOCKERHUB_USERNAME`: your Docker Hub username
- `DOCKERHUB_TOKEN`: a Docker Hub access token

Optional repository variable:

- `DOCKERHUB_IMAGE`: Docker Hub repository name. If omitted, the workflow uses this GitHub repo name, `comfyui_runpod`.

Then open `Actions`, choose `Build and Publish Docker Image`, and run the workflow. The image will be pushed as:

```text
DOCKERHUB_USERNAME/comfyui_runpod:latest
```

Use that image name when creating the RunPod Serverless endpoint.

## Notes

- The proxy sends the original ComfyUI `/prompt` JSON as `input.comfy_payload`, avoiding nested `prompt.prompt` payloads.
- The worker returns base64 image data by default because RunPod Serverless responses are easiest for the local proxy to consume that way.
- Set `RETURN_IMAGES=metadata` if you want the worker to return only ComfyUI image metadata.
- `custom_nodes/model_delete` can delete model files when present in a workflow. Keep it only if you intentionally want that ability in the serverless image.
- Do not leave this repo's `extra_model_paths.yaml` in a Windows local ComfyUI folder unless you intentionally want local paths like `D:\runpod-volume\models`.
