import io
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from pathlib import PurePosixPath

import modal
import wcmatch.glob as wcglob
from deepagents.backends.protocol import (
    BackendProtocol,
    EditResult,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GrepMatch,
    WriteResult,
)
from deepagents.backends.utils import (
    check_empty_content,
    format_content_with_line_numbers,
    perform_string_replacement,
)
from modal.volume import FileEntryType


MODAL_IO_TIMEOUT = 30  # seconds — fail fast instead of hanging forever

_timeout_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="modal-vol-io")


def _run_with_timeout(fn, *args, timeout: int = MODAL_IO_TIMEOUT):
    """Run *fn* in a background thread and raise TimeoutError if it doesn't
    finish within *timeout* seconds.  This prevents Modal Volume API calls
    (reload, listdir, read_file) from blocking the pipeline indefinitely."""
    future = _timeout_pool.submit(fn, *args)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        future.cancel()
        raise TimeoutError(
            f"Modal Volume operation timed out after {timeout}s"
        ) from None


class ModalVolumeBackend(BackendProtocol):
    """
    Deepagents backend that reads/writes files directly in a Modal Volume.

    Paths exposed to deepagents tools are virtual absolute paths rooted at "/".
    They are resolved under base_dir inside the configured Modal Volume.
    """

    def __init__(
        self,
        *,
        volume_name: str,
        base_dir: str = "generated_contracts",
    ) -> None:
        self._volume = modal.Volume.from_name(volume_name, create_if_missing=True)
        self._base_dir = PurePosixPath(base_dir.strip("/"))

    def _to_volume_path(self, key: str) -> PurePosixPath:
        vpath = key if key.startswith("/") else f"/{key}"
        parts = PurePosixPath(vpath).parts
        if ".." in parts:
            raise ValueError("Path traversal is not allowed.")
        rel = PurePosixPath(vpath.lstrip("/"))
        return self._base_dir / rel

    def _to_virtual_path(self, volume_path: PurePosixPath) -> str:
        rel = volume_path.relative_to(self._base_dir)
        return f"/{rel.as_posix()}"

    def _safe_reload(self) -> None:
        """
        Reload when available in Modal runtime.

        Local callers (outside Modal functions) can trigger a RuntimeError for
        reload(); for those cases we intentionally proceed without reloading.
        """
        try:
            _run_with_timeout(self._volume.reload)
        except RuntimeError as e:
            if "can only be called from within a running function" in str(e):
                return
            raise
        except TimeoutError:
            return

    def _read_bytes(self, volume_path: PurePosixPath) -> bytes:
        self._safe_reload()
        chunks = list(
            _run_with_timeout(
                lambda: list(self._volume.read_file(volume_path.as_posix()))
            )
        )
        return b"".join(chunks)

    def _list(self, volume_path: PurePosixPath, recursive: bool) -> list:
        self._safe_reload()
        return _run_with_timeout(
            self._volume.listdir, volume_path.as_posix(), recursive
        )

    def ls_info(self, path: str) -> list[FileInfo]:
        try:
            target = self._to_volume_path(path)
            entries = self._list(target, recursive=False)
        except Exception:
            return []

        out: list[FileInfo] = []
        for entry in entries:
            entry_path = PurePosixPath(entry.path)
            try:
                virt = self._to_virtual_path(entry_path)
            except Exception:
                continue
            is_dir = entry.type == FileEntryType.DIRECTORY
            modified = datetime.fromtimestamp(entry.mtime, tz=timezone.utc).isoformat()
            out.append(
                {
                    "path": f"{virt}/" if is_dir else virt,
                    "is_dir": is_dir,
                    "size": int(entry.size),
                    "modified_at": modified,
                }
            )
        out.sort(key=lambda item: item.get("path", ""))
        return out

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> str:
        try:
            target = self._to_volume_path(file_path)
            content = self._read_bytes(target).decode("utf-8")
        except FileNotFoundError:
            return f"Error: File '{file_path}' not found"
        except Exception as e:
            return f"Error reading file '{file_path}': {e}"

        empty_msg = check_empty_content(content)
        if empty_msg:
            return empty_msg

        lines = content.splitlines()
        if offset >= len(lines):
            return f"Error: Line offset {offset} exceeds file length ({len(lines)} lines)"
        selected = lines[offset : offset + limit]
        return format_content_with_line_numbers(selected, start_line=offset + 1)

    def write(self, file_path: str, content: str) -> WriteResult:
        try:
            target = self._to_volume_path(file_path)
            # Force=False preserves write semantics: fail if file already exists.
            with self._volume.batch_upload(force=False) as batch:
                batch.put_file(io.BytesIO(content.encode("utf-8")), target.as_posix())
            return WriteResult(path=file_path, files_update=None)
        except FileExistsError:
            return WriteResult(
                error=(
                    f"Cannot write to {file_path} because it already exists. "
                    "Read and then make an edit, or write to a new path."
                )
            )
        except Exception as e:
            return WriteResult(error=f"Error writing file '{file_path}': {e}")

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        try:
            target = self._to_volume_path(file_path)
            current = self._read_bytes(target).decode("utf-8")
        except FileNotFoundError:
            return EditResult(error=f"Error: File '{file_path}' not found")
        except Exception as e:
            return EditResult(error=f"Error editing file '{file_path}': {e}")

        result = perform_string_replacement(current, old_string, new_string, replace_all)
        if isinstance(result, str):
            return EditResult(error=result)
        new_content, occurrences = result

        try:
            with self._volume.batch_upload(force=True) as batch:
                batch.put_file(io.BytesIO(new_content.encode("utf-8")), target.as_posix())
            return EditResult(path=file_path, files_update=None, occurrences=int(occurrences))
        except Exception as e:
            return EditResult(error=f"Error editing file '{file_path}': {e}")

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        try:
            base = self._to_volume_path(path)
            entries = self._list(base, recursive=True)
        except Exception:
            return []

        if pattern.startswith("/"):
            pattern = pattern.lstrip("/")

        out: list[FileInfo] = []
        for entry in entries:
            if entry.type != FileEntryType.FILE:
                continue
            entry_path = PurePosixPath(entry.path)
            try:
                virt = self._to_virtual_path(entry_path)
            except Exception:
                continue
            rel = virt.lstrip("/")
            if not wcglob.globmatch(rel, pattern, flags=wcglob.BRACE | wcglob.GLOBSTAR):
                continue
            modified = datetime.fromtimestamp(entry.mtime, tz=timezone.utc).isoformat()
            out.append(
                {
                    "path": virt,
                    "is_dir": False,
                    "size": int(entry.size),
                    "modified_at": modified,
                }
            )
        out.sort(key=lambda item: item.get("path", ""))
        return out

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list[GrepMatch] | str:
        try:
            base = self._to_volume_path(path or "/")
            entries = self._list(base, recursive=True)
        except Exception as e:
            return f"Error searching files: {e}"

        matches: list[GrepMatch] = []
        for entry in entries:
            if entry.type != FileEntryType.FILE:
                continue
            entry_path = PurePosixPath(entry.path)
            try:
                virt = self._to_virtual_path(entry_path)
            except Exception:
                continue
            rel = virt.lstrip("/")
            if glob and not wcglob.globmatch(rel, glob, flags=wcglob.BRACE | wcglob.GLOBSTAR):
                continue
            try:
                text = self._read_bytes(entry_path).decode("utf-8")
            except Exception:
                continue
            for idx, line in enumerate(text.splitlines(), start=1):
                if pattern in line:
                    matches.append({"path": virt, "line": idx, "text": line})
        return matches

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        responses: list[FileUploadResponse] = []
        for path, content in files:
            try:
                target = self._to_volume_path(path)
                with self._volume.batch_upload(force=True) as batch:
                    batch.put_file(io.BytesIO(content), target.as_posix())
                responses.append(FileUploadResponse(path=path, error=None))
            except ValueError:
                responses.append(FileUploadResponse(path=path, error="invalid_path"))
            except PermissionError:
                responses.append(FileUploadResponse(path=path, error="permission_denied"))
            except Exception:
                responses.append(FileUploadResponse(path=path, error="invalid_path"))
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse] = []
        for path in paths:
            try:
                target = self._to_volume_path(path)
                content = self._read_bytes(target)
                responses.append(FileDownloadResponse(path=path, content=content, error=None))
            except FileNotFoundError:
                responses.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
            except ValueError:
                responses.append(FileDownloadResponse(path=path, content=None, error="invalid_path"))
            except PermissionError:
                responses.append(FileDownloadResponse(path=path, content=None, error="permission_denied"))
            except IsADirectoryError:
                responses.append(FileDownloadResponse(path=path, content=None, error="is_directory"))
            except Exception:
                responses.append(FileDownloadResponse(path=path, content=None, error="invalid_path"))
        return responses

