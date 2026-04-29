from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable


try:
    import folder_paths
except Exception:  # pragma: no cover - allows local syntax checks outside ComfyUI
    folder_paths = None


MODEL_EXTENSIONS = {
    ".ckpt",
    ".gguf",
    ".onnx",
    ".patch",
    ".pt",
    ".pth",
    ".safetensors",
    ".sft",
}

PLACEHOLDER_MODEL = "<no models found>"


def _models_dir() -> Path:
    if folder_paths is not None and hasattr(folder_paths, "models_dir"):
        return Path(folder_paths.models_dir).resolve()

    env_dir = os.environ.get("COMFYUI_MODELS_DIR")
    if env_dir:
        return Path(env_dir).resolve()

    return Path.cwd().joinpath("models").resolve()


def _is_model_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in MODEL_EXTENSIONS


def _iter_model_files(models_dir: Path) -> Iterable[Path]:
    if not models_dir.exists():
        return []

    files: list[Path] = []
    for child in models_dir.iterdir():
        if child.is_dir():
            files.extend(path for path in child.rglob("*") if _is_model_file(path))
        elif _is_model_file(child):
            files.append(child)

    return sorted(files, key=lambda path: path.relative_to(models_dir).as_posix().lower())


def _relative_model_paths() -> list[str]:
    models_dir = _models_dir()
    paths = [path.relative_to(models_dir).as_posix() for path in _iter_model_files(models_dir)]
    return paths or [PLACEHOLDER_MODEL]


def _safe_model_path(relative_model: str) -> Path:
    if relative_model == PLACEHOLDER_MODEL:
        raise ValueError("No model is selected.")

    models_dir = _models_dir()
    target = models_dir.joinpath(relative_model).resolve()

    try:
        target.relative_to(models_dir)
    except ValueError as exc:
        raise ValueError("Selected model is outside the ComfyUI models folder.") from exc

    if not target.is_file():
        raise FileNotFoundError(f"Selected model does not exist: {relative_model}")

    if not _is_model_file(target):
        raise ValueError(f"Selected file is not a recognized model file: {relative_model}")

    return target


def _grouped_listing() -> str:
    models_dir = _models_dir()
    model_paths = [path for path in _iter_model_files(models_dir)]

    if not models_dir.exists():
        return f"Models folder was not found:\n{models_dir}"

    if not model_paths:
        return f"No model files found in:\n{models_dir}"

    grouped: dict[str, list[str]] = {}
    for path in model_paths:
        relative = path.relative_to(models_dir).as_posix()
        folder = relative.split("/", 1)[0] if "/" in relative else "."
        grouped.setdefault(folder, []).append(relative)

    lines = [f"Models folder: {models_dir}", ""]
    for folder in sorted(grouped, key=str.lower):
        lines.append(f"[{folder}]")
        lines.extend(f"- {relative}" for relative in grouped[folder])
        lines.append("")

    return "\n".join(lines).rstrip()


def _scan_signature() -> str:
    models_dir = _models_dir()
    digest = hashlib.sha256(str(models_dir).encode("utf-8"))

    for path in _iter_model_files(models_dir):
        try:
            stat = path.stat()
        except OSError:
            continue
        relative = path.relative_to(models_dir).as_posix()
        digest.update(relative.encode("utf-8", "surrogateescape"))
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(str(stat.st_mtime_ns).encode("ascii"))

    return digest.hexdigest()


class ModelFolderManager:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": (_relative_model_paths(),),
                "action": (["list_only", "delete_selected"],),
                "confirm_delete": ("BOOLEAN", {"default": False}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("model_list", "deleted_model")
    FUNCTION = "run"
    CATEGORY = "utils/model management"

    @classmethod
    def IS_CHANGED(cls, model, action, confirm_delete):
        return _scan_signature()

    def run(self, model: str, action: str, confirm_delete: bool):
        listing = _grouped_listing()
        deleted_model = ""

        if action == "delete_selected":
            if not confirm_delete:
                listing = (
                    "Delete was requested, but confirm_delete is off.\n"
                    "Turn confirm_delete on to permanently delete the selected model.\n\n"
                    f"{listing}"
                )
            else:
                target = _safe_model_path(model)
                relative = target.relative_to(_models_dir()).as_posix()
                target.unlink()
                deleted_model = relative
                listing = f"Deleted model:\n{relative}\n\n{_grouped_listing()}"

        return (listing, deleted_model)


NODE_CLASS_MAPPINGS = {
    "ModelFolderManager": ModelFolderManager,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ModelFolderManager": "Model Folder Manager",
}
