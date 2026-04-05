import os
from io import BytesIO
from pathlib import Path
from typing import Protocol

from schemas.coding_schema import CodeArtifact
from agents.modal_runtime import get_modal_volume


def _resolve_project_id(project_id: str | None) -> str | None:
    """If project_id is None, try to read from request context."""
    if project_id is not None:
        return project_id
    try:
        from agents.context import get_project_context

        pid, _ = get_project_context()
        return pid
    except Exception:
        return None


class CodeStorage(Protocol):
    """
    Abstraction for persisting and retrieving code artifacts.

    Implementations should take a CodeArtifact plus raw code and return
    a path or key that can be stored in long-term memory as metadata.
    """

    def save_code(self, artifact: CodeArtifact, code: str) -> str: ...

    def load_code(self, path: str) -> str: ...

    def list_paths(self) -> list[str]: ...


class LocalCodeStorage:
    """
    Default filesystem-backed storage for generated code artifacts.

    Files are written under a single base directory within the project
    (by default: ./generated_contracts, or generated_contracts/{project_id} when
    project_id is set). Only relative paths are returned so they can be safely
    stored in user memory.
    """

    def __init__(
        self,
        base_dir: str | Path | None = None,
        project_id: str | None = None,
    ):
        if base_dir is None:
            base = Path.cwd() / "generated_contracts"
            if project_id:
                base = base / project_id
            base_dir = base
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_path(self, relative_path: str) -> Path:
        rel = Path(relative_path.lstrip("/"))
        full = (self.base_dir / rel).resolve()
        if self.base_dir not in full.parents and self.base_dir != full:
            raise ValueError("Attempted to escape base code storage directory.")
        full.parent.mkdir(parents=True, exist_ok=True)
        return full

    def save_code(self, artifact: CodeArtifact, code: str) -> str:
        rel_path = artifact.path
        full_path = self._resolve_path(rel_path)
        full_path.write_text(code, encoding="utf-8")
        return str(rel_path)

    def load_code(self, path: str) -> str:
        full_path = self._resolve_path(path)
        return full_path.read_text(encoding="utf-8")

    def list_paths(self) -> list[str]:
        if not self.base_dir.exists():
            return []
        return [
            str(p.relative_to(self.base_dir).as_posix())
            for p in self.base_dir.rglob("*")
            if p.is_file()
        ]


class ModalVolumeCodeStorage:
    """
    Modal Volume-backed storage for generated artifacts.

    Files are written under a single base directory within a Modal Volume.
    When project_id is set, volume_name and base_dir are project-scoped.
    Relative paths (e.g. contracts/Foo.sol) are used as keys and returned
    to callers so existing metadata remains valid.
    """

    def __init__(
        self,
        volume_name: str | None = None,
        base_dir: str | Path | None = None,
        project_id: str | None = None,
    ):
        base_name = volume_name or os.getenv(
            "FOUNDRY_ARTIFACT_VOLUME_NAME", "partyhat-foundry-artifacts"
        )
        if project_id:
            volume_name = f"{base_name}-{project_id}"
        else:
            volume_name = base_name
        self._volume = get_modal_volume(volume_name)
        if base_dir is None:
            root = os.getenv("FOUNDRY_ARTIFACT_ROOT", "generated_contracts")
            if project_id:
                base_dir = f"{root.rstrip('/')}/{project_id}"
            else:
                base_dir = root
        self.base_dir = Path(str(base_dir).lstrip("/"))

    def _resolve_path(self, relative_path: str) -> Path:
        rel = Path(relative_path.lstrip("/"))
        if ".." in rel.parts:
            raise ValueError("Attempted to escape base code storage directory.")
        return self.base_dir / rel

    @staticmethod
    def _as_volume_path(path: Path) -> str:
        return path.as_posix().lstrip("/")

    def _safe_reload(self) -> None:
        """
        Refresh the volume view when running inside a Modal function.

        In local API processes (outside a running Modal function), Modal can raise
        a RuntimeError for reload(). In that case we skip reload and keep serving
        from the current mounted/accessible volume state.
        """
        try:
            self._volume.reload()
        except RuntimeError as e:
            msg = str(e)
            if "can only be called from within a running function" in msg:
                return
            raise
        except Exception as e:
            # Modal may surface missing volume paths via generic SDK exceptions.
            if "No such file or directory" in str(e):
                return
            raise

    def save_code(self, artifact: CodeArtifact, code: str) -> str:
        rel_path = Path(artifact.path.lstrip("/"))
        full_path = self._resolve_path(str(rel_path))
        with self._volume.batch_upload(force=True) as batch:
            batch.put_file(
                BytesIO(code.encode("utf-8")),
                self._as_volume_path(full_path),
            )
        return str(rel_path)

    def load_code(self, path: str) -> str:
        rel_path = Path(path.lstrip("/"))
        full_path = self._resolve_path(str(rel_path))
        self._safe_reload()
        chunks = list(self._volume.read_file(self._as_volume_path(full_path)))
        return b"".join(chunks).decode("utf-8")

    def list_paths(self) -> list[str]:
        self._safe_reload()
        try:
            entries = self._volume.listdir(self._as_volume_path(self.base_dir), recursive=True)
        except Exception as e:
            if "No such file or directory" not in str(e):
                raise
            return []
        out: list[str] = []
        for entry in entries:
            # modal.volume.FileEntryType.FILE == 1
            if int(getattr(entry, "type", 0)) != 1:
                continue
            path_obj = Path(entry.path)
            try:
                rel = path_obj.relative_to(self.base_dir)
            except ValueError:
                continue
            out.append(rel.as_posix())
        out.sort()
        return out


_STORAGE_CACHE: dict[tuple[bool, str | None], CodeStorage] = {}


def get_code_storage(project_id: str | None = None) -> CodeStorage:
    """
    Factory that chooses the appropriate storage backend.

    When FOUNDRY_USE_MODAL_VOLUME is truthy, artifacts are stored in a Modal
    Volume; otherwise the local filesystem-backed storage is used.
    If project_id is not passed, it is read from request context (contextvars).
    """
    pid = _resolve_project_id(project_id)
    use_modal = os.getenv("FOUNDRY_USE_MODAL_VOLUME", "").lower() in {
        "1",
        "true",
        "yes",
    }
    key = (use_modal, pid)
    if key not in _STORAGE_CACHE:
        if use_modal:
            _STORAGE_CACHE[key] = ModalVolumeCodeStorage(project_id=pid)
        else:
            _STORAGE_CACHE[key] = LocalCodeStorage(project_id=pid)
    return _STORAGE_CACHE[key]


def save_text_artifact(
    path: str,
    content: str,
    project_id: str | None = None,
) -> str:
    storage = get_code_storage(project_id=project_id)
    storage.save_code(
        CodeArtifact(
            path=path,
            language="text",
        ),
        content or "",
    )
    return path


def save_execution_logs(
    *,
    project_id: str | None,
    pipeline_run_id: str | None,
    pipeline_task_id: str | None,
    stdout: str,
    stderr: str,
) -> tuple[str, str]:
    run_segment = pipeline_run_id or "manual"
    task_segment = pipeline_task_id or "standalone"
    base = f"logs/{run_segment}/{task_segment}"
    stdout_path = save_text_artifact(
        f"{base}/stdout.log",
        stdout,
        project_id=project_id,
    )
    stderr_path = save_text_artifact(
        f"{base}/stderr.log",
        stderr,
        project_id=project_id,
    )
    return stdout_path, stderr_path
