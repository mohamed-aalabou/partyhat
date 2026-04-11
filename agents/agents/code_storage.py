import asyncio
import os
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Protocol

from deepagents.backends.utils import perform_string_replacement

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

    def edit_code(
        self,
        path: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool = False,
    ) -> int: ...

    def delete_code(self, path: str) -> None: ...

    async def asave_code(self, artifact: CodeArtifact, code: str) -> str: ...

    async def aload_code(self, path: str) -> str: ...

    async def alist_paths(self) -> list[str]: ...


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

    def edit_code(
        self,
        path: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool = False,
    ) -> int:
        full_path = self._resolve_path(path)
        current = full_path.read_text(encoding="utf-8")
        result = perform_string_replacement(current, old_string, new_string, replace_all)
        if isinstance(result, str):
            raise ValueError(result)
        new_content, occurrences = result
        full_path.write_text(new_content, encoding="utf-8")
        return int(occurrences)

    def delete_code(self, path: str) -> None:
        full_path = self._resolve_path(path)
        if not full_path.exists():
            raise FileNotFoundError(path)
        if full_path.is_dir():
            raise IsADirectoryError(path)
        full_path.unlink()

    async def asave_code(self, artifact: CodeArtifact, code: str) -> str:
        return await asyncio.to_thread(self.save_code, artifact, code)

    async def aload_code(self, path: str) -> str:
        return await asyncio.to_thread(self.load_code, path)

    async def alist_paths(self) -> list[str]:
        return await asyncio.to_thread(self.list_paths)

    async def aedit_code(
        self,
        path: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool = False,
    ) -> int:
        return await asyncio.to_thread(
            self.edit_code,
            path,
            old_string,
            new_string,
            replace_all=replace_all,
        )

    async def adelete_code(self, path: str) -> None:
        await asyncio.to_thread(self.delete_code, path)


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
        self._last_reload_at = 0.0

    def _resolve_path(self, relative_path: str) -> Path:
        rel = Path(relative_path.lstrip("/"))
        if ".." in rel.parts:
            raise ValueError("Attempted to escape base code storage directory.")
        return self.base_dir / rel

    @staticmethod
    def _as_volume_path(path: Path) -> str:
        return path.as_posix().lstrip("/")

    @staticmethod
    def _is_missing_path_error(error: Exception) -> bool:
        return "No such file or directory" in str(error)

    def _relative_file_paths(self, entries: list) -> list[str]:
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

    @staticmethod
    def _run_sync_call(func, /, *args, **kwargs):
        """
        Run blocking Modal SDK calls off the event loop when sync APIs are invoked
        from async request handling.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return func(*args, **kwargs)

        result: list[object] = []
        error: list[BaseException] = []

        def _runner() -> None:
            try:
                result.append(func(*args, **kwargs))
            except BaseException as exc:  # pragma: no cover - exercised via re-raise.
                error.append(exc)

        worker = threading.Thread(target=_runner)
        worker.start()
        worker.join()

        if error:
            raise error[0]
        return result[0] if result else None

    def _safe_reload(self) -> None:
        """
        Refresh the volume view when running inside a Modal function.

        In local API processes (outside a running Modal function), Modal can raise
        a RuntimeError for reload(). In that case we skip reload and keep serving
        from the current mounted/accessible volume state.
        """
        now = time.monotonic()
        if now - self._last_reload_at < 0.25:
            return
        try:
            self._volume.reload()
            self._last_reload_at = now
        except RuntimeError as e:
            msg = str(e)
            if "can only be called from within a running function" in msg:
                return
            raise
        except Exception as e:
            # Modal may surface missing volume paths via generic SDK exceptions.
            if self._is_missing_path_error(e):
                return
            raise

    async def _asafe_reload(self) -> None:
        now = time.monotonic()
        if now - self._last_reload_at < 0.25:
            return
        try:
            await self._volume.reload.aio()
            self._last_reload_at = now
        except RuntimeError as e:
            msg = str(e)
            if "can only be called from within a running function" in msg:
                return
            raise
        except Exception as e:
            if self._is_missing_path_error(e):
                return
            raise

    def save_code(self, artifact: CodeArtifact, code: str) -> str:
        rel_path = Path(artifact.path.lstrip("/"))
        full_path = self._resolve_path(str(rel_path))
        return self._run_sync_call(
            self._save_code_sync,
            rel_path,
            full_path,
            code,
        )

    def _save_code_sync(self, rel_path: Path, full_path: Path, code: str) -> str:
        with self._volume.batch_upload(force=True) as batch:
            batch.put_file(
                BytesIO(code.encode("utf-8")),
                self._as_volume_path(full_path),
            )
        return str(rel_path)

    async def asave_code(self, artifact: CodeArtifact, code: str) -> str:
        rel_path = Path(artifact.path.lstrip("/"))
        full_path = self._resolve_path(str(rel_path))
        async with self._volume.batch_upload.aio(force=True) as batch:
            batch.put_file(
                BytesIO(code.encode("utf-8")),
                self._as_volume_path(full_path),
            )
        return str(rel_path)

    def load_code(self, path: str) -> str:
        rel_path = Path(path.lstrip("/"))
        full_path = self._resolve_path(str(rel_path))
        return self._run_sync_call(self._load_code_sync, full_path)

    def _load_code_sync(self, full_path: Path) -> str:
        self._safe_reload()
        chunks = list(self._volume.read_file(self._as_volume_path(full_path)))
        return b"".join(chunks).decode("utf-8")

    async def aload_code(self, path: str) -> str:
        rel_path = Path(path.lstrip("/"))
        full_path = self._resolve_path(str(rel_path))
        await self._asafe_reload()
        chunks: list[bytes] = []
        async for chunk in self._volume.read_file.aio(self._as_volume_path(full_path)):
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")

    def list_paths(self) -> list[str]:
        return self._run_sync_call(self._list_paths_sync)

    def _list_paths_sync(self) -> list[str]:
        self._safe_reload()
        try:
            entries = self._volume.listdir(self._as_volume_path(self.base_dir), recursive=True)
        except Exception as e:
            if not self._is_missing_path_error(e):
                raise
            return []
        return self._relative_file_paths(entries)

    async def alist_paths(self) -> list[str]:
        await self._asafe_reload()
        try:
            entries = await self._volume.listdir.aio(
                self._as_volume_path(self.base_dir),
                recursive=True,
            )
        except Exception as e:
            if not self._is_missing_path_error(e):
                raise
            return []
        return self._relative_file_paths(entries)

    def edit_code(
        self,
        path: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool = False,
    ) -> int:
        rel_path = Path(path.lstrip("/"))
        full_path = self._resolve_path(str(rel_path))
        return self._run_sync_call(
            self._edit_code_sync,
            full_path,
            old_string,
            new_string,
            replace_all,
        )

    def _edit_code_sync(
        self,
        full_path: Path,
        old_string: str,
        new_string: str,
        replace_all: bool,
    ) -> int:
        current = self._load_code_sync(full_path)
        result = perform_string_replacement(current, old_string, new_string, replace_all)
        if isinstance(result, str):
            raise ValueError(result)
        new_content, occurrences = result
        with self._volume.batch_upload(force=True) as batch:
            batch.put_file(
                BytesIO(new_content.encode("utf-8")),
                self._as_volume_path(full_path),
            )
        return int(occurrences)

    def delete_code(self, path: str) -> None:
        rel_path = Path(path.lstrip("/"))
        full_path = self._resolve_path(str(rel_path))
        self._run_sync_call(self._delete_code_sync, full_path)

    def _delete_code_sync(self, full_path: Path) -> None:
        self._safe_reload()
        self._volume.remove_file(self._as_volume_path(full_path))

    async def aedit_code(
        self,
        path: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool = False,
    ) -> int:
        rel_path = Path(path.lstrip("/"))
        full_path = self._resolve_path(str(rel_path))
        await self._asafe_reload()
        current = await self.aload_code(path)
        result = perform_string_replacement(current, old_string, new_string, replace_all)
        if isinstance(result, str):
            raise ValueError(result)
        new_content, occurrences = result
        async with self._volume.batch_upload.aio(force=True) as batch:
            batch.put_file(
                BytesIO(new_content.encode("utf-8")),
                self._as_volume_path(full_path),
            )
        return int(occurrences)

    async def adelete_code(self, path: str) -> None:
        rel_path = Path(path.lstrip("/"))
        full_path = self._resolve_path(str(rel_path))
        await self._asafe_reload()
        await self._volume.remove_file.aio(self._as_volume_path(full_path))


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
