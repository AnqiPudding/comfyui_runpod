from __future__ import annotations

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


def _model_rows() -> list[tuple[str, Path]]:
    models_dir = _models_dir()
    return [(path.relative_to(models_dir).as_posix(), path) for path in _iter_model_files(models_dir)]


def _matching_models(keyword: str) -> list[tuple[str, Path]]:
    keyword = keyword.strip().lower()
    if not keyword:
        return []
    return [(relative, path) for relative, path in _model_rows() if keyword in relative.lower()]


def _grouped_listing(matches: list[tuple[str, Path]] | None = None, keyword: str = "") -> str:
    models_dir = _models_dir()
    model_rows = _model_rows()
    if not models_dir.exists():
        return f"Models folder was not found:\n{models_dir}"

    if not model_rows:
        return f"No model files found in:\n{models_dir}"

    grouped: dict[str, list[str]] = {}
    for relative, _path in model_rows:
        folder = relative.split("/", 1)[0] if "/" in relative else "."
        grouped.setdefault(folder, []).append(relative)

    lines = [f"Models folder: {models_dir}", ""]

    if keyword.strip():
        lines.append(f"Keyword: {keyword.strip()}")
        lines.append("Similar matches:")
        if matches:
            lines.extend(f"- {relative}" for relative, _path in matches)
        else:
            lines.append("- <no matching models>")
        lines.append("")

    lines.append("All models:")
    for folder in sorted(grouped, key=str.lower):
        lines.append(f"[{folder}]")
        lines.extend(f"- {relative}" for relative in grouped[folder])
        lines.append("")

    return "\n".join(lines).rstrip()


class ModelFolderManager:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "keyword": ("STRING", {"default": "", "multiline": False}),
                "delete": ("BOOLEAN", {"default": False}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("model_list", "deleted_models")
    FUNCTION = "run"
    CATEGORY = "utils/model management"

    @classmethod
    def IS_CHANGED(cls, keyword, delete):
        return float("NaN")

    def run(self, keyword: str, delete: bool):
        keyword = keyword.strip()
        matches = _matching_models(keyword)
        deleted_models: list[str] = []

        if delete:
            if not keyword:
                listing = (
                    "Delete is on, but keyword is empty. No files were deleted.\n"
                    "Enter a keyword that appears in the model filename or relative path.\n\n"
                    f"{_grouped_listing(matches, keyword)}"
                )
                return (listing, "")

            for relative, target in matches:
                target.unlink()
                deleted_models.append(relative)

        listing = _grouped_listing(matches, keyword)
        if delete and deleted_models:
            listing = "Deleted matching models:\n" + "\n".join(f"- {name}" for name in deleted_models) + "\n\n" + _grouped_listing()
        elif delete:
            listing = f"Delete is on, but no model matched keyword: {keyword}\n\n{listing}"

        return (listing, "\n".join(deleted_models))


NODE_CLASS_MAPPINGS = {
    "ModelFolderManager": ModelFolderManager,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ModelFolderManager": "Model Folder Manager",
}
