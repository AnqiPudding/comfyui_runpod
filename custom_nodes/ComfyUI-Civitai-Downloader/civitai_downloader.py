import os
import requests
import re
from urllib.parse import unquote, urlparse, urlunparse
from tqdm import tqdm
import folder_paths


HUGGINGFACE_HOSTS = {"huggingface.co", "www.huggingface.co", "hf.co", "www.hf.co"}


def get_filename_from_cd(cd):
    if not cd:
        return None
    # Check for filename*=UTF-8''...
    fname_star = re.findall(r"filename\*=([^;]+)", cd, flags=re.IGNORECASE)
    if fname_star:
        fname = fname_star[0].strip()
        if fname.lower().startswith("utf-8''"):
            return requests.utils.unquote(fname[7:])
        return requests.utils.unquote(fname)
    
    fname = re.findall(r"filename=([^;]+)", cd, flags=re.IGNORECASE)
    if fname:
        name = fname[0].strip().strip('"').strip("'")
        return requests.utils.unquote(name)
    return None


def is_huggingface_url(url):
    host = urlparse(url).netloc.lower()
    return host in HUGGINGFACE_HOSTS


def normalize_huggingface_url(url):
    """Convert public Hugging Face file pages to direct download URLs."""
    parsed = urlparse(url)
    parts = parsed.path.split("/")

    if parsed.netloc.lower() not in HUGGINGFACE_HOSTS:
        return url

    # Hugging Face file pages are /owner/repo/blob/revision/path/to/file.
    # The equivalent direct download endpoint is /owner/repo/resolve/revision/path/to/file.
    if len(parts) > 4 and parts[3] == "blob":
        parts[3] = "resolve"
        parsed = parsed._replace(path="/".join(parts))
        return urlunparse(parsed)

    # /raw/ also works for many text files, but model binaries should use /resolve/.
    if len(parts) > 4 and parts[3] == "raw":
        parts[3] = "resolve"
        parsed = parsed._replace(path="/".join(parts))
        return urlunparse(parsed)

    return url


def get_filename_from_url(url):
    parsed = urlparse(url)
    filename = os.path.basename(unquote(parsed.path))
    return filename or None

class AnyType(str):
    def __ne__(self, __value: object) -> bool:
        return False

ANY_TYPE = AnyType("*")

class CivitaiModelDownloader:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "download_url": ("STRING", {"default": "https://civitai.com/api/download/models/... or https://huggingface.co/.../blob/main/model.safetensors"}),
                "model_type": (["checkpoints", "loras", "vae", "clip", "unet", "diffusion_models", "text_encoders", "controlnet", "embeddings", "upscale_models", "clip_vision", "gligen", "style_models", "photomaker", "hypernetworks", "classifiers"],),
            },
            "optional": {
                "api_key": ("STRING", {"default": ""}),
                "file_name": ("STRING", {"default": ""}),
            }
        }
    
    RETURN_TYPES = (ANY_TYPE,)
    RETURN_NAMES = ("filename",)
    FUNCTION = "download_model"
    CATEGORY = "model/download"

    def download_model(self, download_url, model_type, api_key="", file_name=""):
        if os.name == "nt" and os.environ.get("ALLOW_LOCAL_MODEL_DOWNLOADS") != "1":
            raise RuntimeError(
                "Local Windows model downloads are disabled. Queue through the RunPod proxy, "
                "or set ALLOW_LOCAL_MODEL_DOWNLOADS=1 if you intentionally want to download on this PC."
            )

        if not download_url.startswith("http"):
            raise ValueError("Invalid download URL provided.")

        download_url = normalize_huggingface_url(download_url)

        headers = {}
        if api_key and not is_huggingface_url(download_url):
            headers["Authorization"] = f"Bearer {api_key}"

        print(f"Starting download from {download_url}...")
        
        try:
            response = requests.get(download_url, headers=headers, stream=True, allow_redirects=True)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise Exception(f"Failed to download: {str(e)}")

        # Determine filename
        final_filename = file_name
        if not final_filename:
            cd = response.headers.get("Content-Disposition")
            final_filename = get_filename_from_cd(cd)

        if not final_filename:
            # Fallback to URL parsing
            final_filename = get_filename_from_url(response.url)
            if not final_filename:
                final_filename = "downloaded_model.safetensors" # default

        # Sanitize final filename
        final_filename = re.sub(r'[\\/*?:"<>|]', "", final_filename)

        content_type = response.headers.get("Content-Type", "").lower()
        if is_huggingface_url(download_url) and "text/html" in content_type:
            raise Exception(
                "Hugging Face returned an HTML page instead of a model file. "
                "Use a public file URL, for example https://huggingface.co/owner/repo/blob/main/model.safetensors. "
                "Private or gated repositories cannot be downloaded without a token."
            )

        # Get output directory
        out_dir = folder_paths.get_folder_paths(model_type)
        if not out_dir:
            out_dir = [os.path.join(folder_paths.models_dir, model_type)]
        output_dir = out_dir[0]
        
        if not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)

        output_path = os.path.join(output_dir, final_filename)

        if os.path.exists(output_path):
            print(f"File {output_path} already exists. Skipping download.")
            return (final_filename,)

        # Download with progress
        total_size = int(response.headers.get('content-length', 0))
        block_size = 1024 * 1024 # 1 MB
        
        with open(output_path, 'wb') as file, tqdm(
                desc=final_filename,
                total=total_size,
                unit='iB',
                unit_scale=True,
                unit_divisor=1024,
            ) as bar:
            for data in response.iter_content(block_size):
                file.write(data)
                bar.update(len(data))
                
        print(f"Successfully downloaded to {output_path}")
        return (final_filename,)
