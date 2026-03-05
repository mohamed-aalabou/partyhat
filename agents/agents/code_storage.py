import os
from pathlib import Path
from typing import Protocol

from schemas.coding_schema import CodeArtifact


class CodeStorage(Protocol):
    """
    Abstraction for persisting and retrieving code artifacts.

    Implementations should take a CodeArtifact plus raw code and return
    a path or key that can be stored in long-term memory as metadata.
    """

    def save_code(self, artifact: CodeArtifact, code: str) -> str: ...

    def load_code(self, path: str) -> str: ...


class LocalCodeStorage:
    """
    Default filesystem-backed storage for generated code artifacts.

    Files are written under a single base directory within the project
    (by default: ./generated_contracts). Only relative paths are
    returned so they can be safely stored in user memory.
    """

    def __init__(self, base_dir: str | Path | None = None):
        if base_dir is None:
            # Default to a top-level folder in the repo/workspace.
            base_dir = Path.cwd() / "generated_contracts"
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_path(self, relative_path: str) -> Path:
        # Normalise and ensure the path stays within base_dir.
        rel = Path(relative_path.lstrip("/"))
        full = (self.base_dir / rel).resolve()
        if self.base_dir not in full.parents and self.base_dir != full:
            raise ValueError("Attempted to escape base code storage directory.")
        full.parent.mkdir(parents=True, exist_ok=True)
        return full

    def save_code(self, artifact: CodeArtifact, code: str) -> str:
        """
        Persist the given code to disk and return the relative path used.
        """
        # Use artifact.path as the relative key.
        rel_path = artifact.path
        full_path = self._resolve_path(rel_path)
        full_path.write_text(code, encoding="utf-8")
        # Always return the normalised relative path for metadata storage.
        return str(rel_path)

    def load_code(self, path: str) -> str:
        """
        Load previously saved code from disk using a stored relative path.
        """
        full_path = self._resolve_path(path)
        return full_path.read_text(encoding="utf-8")

