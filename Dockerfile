FROM python:3.13-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    COMFYUI_DIR=/workspace/ComfyUI \
    PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cu128

ARG CACHE_BUST=manual
ARG COMFYUI_REF=main
ARG COMFYUI_MANAGER_REF=
ARG COMFYUI_KJNODES_REF=
ARG COMFYUI_QWENVL_REF=
ARG COMFYUI_WD14_TAGGER_REF=
ARG COMFYUI_ESSENTIALS_REF=
ARG COMFYUI_ULTIMATE_SD_UPSCALE_REF=
ARG RGTHREE_COMFY_REF=
ARG WAS_NODE_SUITE_REF=

WORKDIR /workspace

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    cmake \
    ffmpeg \
    git \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    libsm6 \
    libxext6 \
    libxrender1 \
    wget \
    && rm -rf /var/lib/apt/lists/*

RUN echo "Cache bust: ${CACHE_BUST}" \
    && git clone https://github.com/Comfy-Org/ComfyUI.git "$COMFYUI_DIR" \
    && cd "$COMFYUI_DIR" \
    && git checkout "$COMFYUI_REF"

WORKDIR $COMFYUI_DIR

COPY requirements-runpod.txt /workspace/requirements-runpod.txt

RUN python -m pip install --upgrade pip setuptools wheel \
    && pip install torch torchvision torchaudio --index-url "$PYTORCH_INDEX_URL" \
    && pip install -r requirements.txt \
    && pip install -r /workspace/requirements-runpod.txt

RUN set -eux; \
    maybe_checkout() { repo_dir="$1"; ref="$2"; if [ -n "$ref" ]; then git -C "$repo_dir" checkout "$ref"; fi; }; \
    mkdir -p custom_nodes; \
    git clone https://github.com/ltdrdata/ComfyUI-Manager custom_nodes/comfyui-manager; \
    maybe_checkout custom_nodes/comfyui-manager "$COMFYUI_MANAGER_REF"; \
    git clone https://github.com/kijai/ComfyUI-KJNodes custom_nodes/ComfyUI-KJNodes; \
    maybe_checkout custom_nodes/ComfyUI-KJNodes "$COMFYUI_KJNODES_REF"; \
    git clone https://github.com/1038lab/ComfyUI-QwenVL custom_nodes/ComfyUI-QwenVL; \
    maybe_checkout custom_nodes/ComfyUI-QwenVL "$COMFYUI_QWENVL_REF"; \
    git clone https://github.com/pythongosssss/ComfyUI-WD14-Tagger custom_nodes/ComfyUI-WD14-Tagger; \
    maybe_checkout custom_nodes/ComfyUI-WD14-Tagger "$COMFYUI_WD14_TAGGER_REF"; \
    git clone https://github.com/cubiq/ComfyUI_essentials custom_nodes/ComfyUI_essentials; \
    maybe_checkout custom_nodes/ComfyUI_essentials "$COMFYUI_ESSENTIALS_REF"; \
    git clone https://github.com/ssitu/ComfyUI_UltimateSDUpscale custom_nodes/ComfyUI_UltimateSDUpscale; \
    maybe_checkout custom_nodes/ComfyUI_UltimateSDUpscale "$COMFYUI_ULTIMATE_SD_UPSCALE_REF"; \
    git clone https://github.com/rgthree/rgthree-comfy custom_nodes/rgthree-comfy; \
    maybe_checkout custom_nodes/rgthree-comfy "$RGTHREE_COMFY_REF"; \
    git clone https://github.com/ltdrdata/was-node-suite-comfyui custom_nodes/was-node-suite-comfyui; \
    maybe_checkout custom_nodes/was-node-suite-comfyui "$WAS_NODE_SUITE_REF"

COPY custom_nodes/ /workspace/ComfyUI/custom_nodes/
COPY extra_model_paths.yaml runpod_handler.py /workspace/ComfyUI/

RUN set -eux; \
    for req in custom_nodes/*/requirements.txt; do \
        if [ -f "$req" ]; then pip install -r "$req"; fi; \
    done

CMD ["python", "/workspace/ComfyUI/runpod_handler.py"]
