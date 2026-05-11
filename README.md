# comfyui_runpod

Deployment overlay for running ComfyUI on RunPod Serverless while using a local ComfyUI UI as the workflow editor.

## What This Repo Contains

- `Dockerfile`: builds a Python 3.13 RunPod worker image from the latest default ComfyUI branch plus the custom nodes used in the local setup.
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

3. Start the proxy.

   ```powershell
   python serverless_proxy.py
   ```

4. Open `http://127.0.0.1:8188`.

5. Click the floating `RunPod` button, or open `http://127.0.0.1:8188/runpod/settings`, and save:

   - RunPod endpoint ID
   - RunPod API key
   - optional timeout / local ComfyUI URL

The settings are saved only on your machine in `runpod_proxy_settings.json`, which is ignored by Git. The UI loads from local ComfyUI, but queueing a prompt sends the execution payload to RunPod Serverless. The proxy submits jobs with RunPod's async `/run` operation and polls `/status/{job_id}`, which is better for long model downloads than holding a single `/runsync` request open.

## Build Image Later

Build from this repo root:

```bash
docker build -t comfyui-runpod .
```

The image uses Python 3.13 and installs CUDA PyTorch wheels from `https://download.pytorch.org/whl/cu128`.
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
- Uploaded input images are captured by the local proxy and sent as `input.input_images`; the serverless worker restores them into ComfyUI's `input` folder before queueing the workflow.
- The worker returns base64 image data by default because RunPod Serverless responses are easiest for the local proxy to consume that way.
- The local proxy persists RunPod results under `output/runpod/.runpod_proxy_history.json`, so completed outputs survive proxy restarts and can appear in the modern ComfyUI jobs/history UI.
- Set `RETURN_IMAGES=metadata` if you want the worker to return only ComfyUI image metadata.
- `custom_nodes/model_delete` can delete model files when present in a workflow. Keep it only if you intentionally want that ability in the serverless image.
- Do not leave this repo's `extra_model_paths.yaml` in a Windows local ComfyUI folder unless you intentionally want local paths like `D:\runpod-volume\models`.

## Vast.ai Serverless

The Docker image can run in either mode:

- RunPod: default, or `SERVERLESS_PROVIDER=runpod`
- Vast.ai: set `SERVERLESS_PROVIDER=vast`

For Vast.ai, the image starts a local ComfyUI-backed model server on port `18000` and a Vast PyWorker route at `/generate`. In the local proxy settings page, choose `Vast.ai`, enter your Vast API key, endpoint name, and keep the route as `/generate`.

Model storage is controlled by `MODEL_VOLUME_DIR` or `VAST_MODEL_DIR`. If neither is set, the image uses `/data/models` unless `/runpod-volume` exists. The startup script symlinks that model directory to `/runpod-volume/models`, so existing ComfyUI extra model paths and downloader nodes still write to persistent model storage.

Vast.ai does not provide a direct equivalent to RunPod cross-host network volumes. Its migration docs map RunPod network volumes to machine-local disk, and recommend object storage for data that must persist across different machines.
