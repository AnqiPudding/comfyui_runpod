FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    COMFYUI_DIR=/workspace/ComfyUI

ARG COMFYUI_REF=dc719cde9c448c65242ae2d4ba400ba18c36846f
ARG COMFYUI_MANAGER_REF=66108ccdbc8cfc9549e42190d114d86d20fbe142
ARG COMFYUI_KJNODES_REF=4e1458c2417db5cc7ae764929b00afad9892e10b
ARG COMFYUI_QWENVL_REF=fcd1ada87a28f922cb887f779db32429f78a022c
ARG COMFYUI_WD14_TAGGER_REF=9e0a6e700299182fc05c58b62e7ad9f72182a78b
ARG COMFYUI_ESSENTIALS_REF=9d9f4bedfc9f0321c19faf71855e228c93bd0dc9
ARG COMFYUI_ULTIMATE_SD_UPSCALE_REF=bebd5696fddd61cb0d08949a222c508898ab5577
ARG RGTHREE_COMFY_REF=683836c46e898668936c433502504cc0627482c5
ARG WAS_NODE_SUITE_REF=afeee09ba44e713ec52a413ac6b105fd06b2d356

WORKDIR /workspace

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    cmake \
    ffmpeg \
    git \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    wget \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/Comfy-Org/ComfyUI.git "$COMFYUI_DIR" \
    && cd "$COMFYUI_DIR" \
    && git checkout "$COMFYUI_REF"

WORKDIR $COMFYUI_DIR

COPY requirements-runpod.txt /workspace/requirements-runpod.txt

RUN python -m pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt \
    && pip install -r /workspace/requirements-runpod.txt

RUN set -eux; \
    mkdir -p custom_nodes; \
    git clone https://github.com/ltdrdata/ComfyUI-Manager custom_nodes/comfyui-manager; \
    git -C custom_nodes/comfyui-manager checkout "$COMFYUI_MANAGER_REF"; \
    git clone https://github.com/kijai/ComfyUI-KJNodes custom_nodes/ComfyUI-KJNodes; \
    git -C custom_nodes/ComfyUI-KJNodes checkout "$COMFYUI_KJNODES_REF"; \
    git clone https://github.com/1038lab/ComfyUI-QwenVL custom_nodes/ComfyUI-QwenVL; \
    git -C custom_nodes/ComfyUI-QwenVL checkout "$COMFYUI_QWENVL_REF"; \
    git clone https://github.com/pythongosssss/ComfyUI-WD14-Tagger custom_nodes/ComfyUI-WD14-Tagger; \
    git -C custom_nodes/ComfyUI-WD14-Tagger checkout "$COMFYUI_WD14_TAGGER_REF"; \
    git clone https://github.com/cubiq/ComfyUI_essentials custom_nodes/ComfyUI_essentials; \
    git -C custom_nodes/ComfyUI_essentials checkout "$COMFYUI_ESSENTIALS_REF"; \
    git clone https://github.com/ssitu/ComfyUI_UltimateSDUpscale custom_nodes/ComfyUI_UltimateSDUpscale; \
    git -C custom_nodes/ComfyUI_UltimateSDUpscale checkout "$COMFYUI_ULTIMATE_SD_UPSCALE_REF"; \
    git clone https://github.com/rgthree/rgthree-comfy custom_nodes/rgthree-comfy; \
    git -C custom_nodes/rgthree-comfy checkout "$RGTHREE_COMFY_REF"; \
    git clone https://github.com/ltdrdata/was-node-suite-comfyui custom_nodes/was-node-suite-comfyui; \
    git -C custom_nodes/was-node-suite-comfyui checkout "$WAS_NODE_SUITE_REF"

COPY custom_nodes/ /workspace/ComfyUI/custom_nodes/
COPY extra_model_paths.yaml runpod_handler.py /workspace/ComfyUI/

RUN set -eux; \
    for req in custom_nodes/*/requirements.txt; do \
        if [ -f "$req" ]; then pip install -r "$req"; fi; \
    done

CMD ["python", "/workspace/ComfyUI/runpod_handler.py"]
