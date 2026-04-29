import sys
import subprocess
import os

# Auto-install requirements
req_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), "requirements.txt")
if os.path.exists(req_file):
    print("Checking dependencies for CivitaiModelDownloader...")
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', '-r', req_file])

from .civitai_downloader import CivitaiModelDownloader

NODE_CLASS_MAPPINGS = {
    "CivitaiModelDownloader": CivitaiModelDownloader
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "CivitaiModelDownloader": "Civitai / Hugging Face Model Downloader"
}

__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS']
