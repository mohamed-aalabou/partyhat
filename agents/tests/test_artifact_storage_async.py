import asyncio
import threading
from types import SimpleNamespace

import api
from agents import code_storage
from schemas.coding_schema import CodeArtifact


async def _noop_ensure_project_context(project_id, user_id, session):
    return None


class FakeAsyncBatchUpload:
    def __init__(self, uploads: list[tuple[str, bytes]]):
        self._uploads = uploads

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def put_file(self, file_obj, remote_path: str) -> None:
        self._uploads.append((remote_path, file_obj.read()))


class FakeSyncBatchUpload:
    def __init__(self, uploads: list[tuple[str, bytes]]):
        self._uploads = uploads

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def put_file(self, file_obj, remote_path: str) -> None:
        self._uploads.append((remote_path, file_obj.read()))


class BlockingReload:
    def __init__(self):
        self.async_calls = 0

    def __call__(self):
        raise AssertionError("blocking reload() should not be used")

    async def aio(self):
        self.async_calls += 1


class BlockingListdir:
    def __init__(self, entries: list[SimpleNamespace]):
        self._entries = entries
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, path: str, recursive: bool = False):
        raise AssertionError("blocking listdir() should not be used")

    async def aio(self, path: str, recursive: bool = False):
        self.calls.append((path, recursive))
        return self._entries


class BlockingReadFile:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks
        self.calls: list[str] = []

    def __call__(self, path: str):
        raise AssertionError("blocking read_file() should not be used")

    def aio(self, path: str):
        self.calls.append(path)

        async def _gen():
            for chunk in self._chunks:
                yield chunk

        return _gen()


class BlockingBatchUploadFactory:
    def __init__(self, uploads: list[tuple[str, bytes]]):
        self._uploads = uploads
        self.calls: list[bool] = []

    def __call__(self, force: bool = False):
        raise AssertionError("blocking batch_upload() should not be used")

    def aio(self, force: bool = False):
        self.calls.append(force)
        return FakeAsyncBatchUpload(self._uploads)


class BlockingRemoveFile:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, path: str):
        raise AssertionError("blocking remove_file() should not be used")

    async def aio(self, path: str):
        self.calls.append(path)


class FakeModalVolume:
    def __init__(self):
        entries = [
            SimpleNamespace(
                path="generated_contracts/contracts/Foo.sol",
                type=1,
                size=42,
                mtime=0,
            ),
            SimpleNamespace(
                path="generated_contracts/contracts",
                type=2,
                size=0,
                mtime=0,
            ),
        ]
        self.reload = BlockingReload()
        self.listdir = BlockingListdir(entries)
        self.read_file = BlockingReadFile([b"contract Foo {}", b"\n"])
        self.uploads: list[tuple[str, bytes]] = []
        self.batch_upload = BlockingBatchUploadFactory(self.uploads)
        self.remove_file = BlockingRemoveFile()


class ThreadRecordingReload:
    def __init__(self):
        self.thread_ids: list[int] = []

    def __call__(self):
        self.thread_ids.append(threading.get_ident())


class ThreadRecordingListdir:
    def __init__(self, entries: list[SimpleNamespace]):
        self._entries = entries
        self.thread_ids: list[int] = []
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, path: str, recursive: bool = False):
        self.thread_ids.append(threading.get_ident())
        self.calls.append((path, recursive))
        return self._entries


class ThreadRecordingReadFile:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks
        self.thread_ids: list[int] = []
        self.calls: list[str] = []

    def __call__(self, path: str):
        self.thread_ids.append(threading.get_ident())
        self.calls.append(path)
        return iter(self._chunks)


class ThreadRecordingBatchUploadFactory:
    def __init__(self, uploads: list[tuple[str, bytes]]):
        self._uploads = uploads
        self.thread_ids: list[int] = []
        self.calls: list[bool] = []

    def __call__(self, force: bool = False):
        self.thread_ids.append(threading.get_ident())
        self.calls.append(force)
        return FakeSyncBatchUpload(self._uploads)


class ThreadRecordingRemoveFile:
    def __init__(self):
        self.thread_ids: list[int] = []
        self.calls: list[str] = []

    def __call__(self, path: str):
        self.thread_ids.append(threading.get_ident())
        self.calls.append(path)


class ThreadRecordingModalVolume:
    def __init__(self):
        entries = [
            SimpleNamespace(
                path="generated_contracts/contracts/Foo.sol",
                type=1,
                size=42,
                mtime=0,
            ),
            SimpleNamespace(
                path="generated_contracts/contracts",
                type=2,
                size=0,
                mtime=0,
            ),
        ]
        self.reload = ThreadRecordingReload()
        self.listdir = ThreadRecordingListdir(entries)
        self.read_file = ThreadRecordingReadFile([b"contract Foo {}", b"\n"])
        self.uploads: list[tuple[str, bytes]] = []
        self.batch_upload = ThreadRecordingBatchUploadFactory(self.uploads)
        self.remove_file = ThreadRecordingRemoveFile()


class AsyncOnlyStorage:
    def __init__(self):
        self.alist_paths_calls = 0
        self.aload_code_calls: list[str] = []

    def save_code(self, artifact, code):
        raise AssertionError("blocking save_code() should not be used")

    def load_code(self, path):
        raise AssertionError("blocking load_code() should not be used")

    def list_paths(self):
        raise AssertionError("blocking list_paths() should not be used")

    async def asave_code(self, artifact, code):
        raise AssertionError("asave_code() should not be used in artifact GET routes")

    async def aload_code(self, path):
        self.aload_code_calls.append(path)
        return "pragma solidity ^0.8.20;"

    async def alist_paths(self):
        self.alist_paths_calls += 1
        return ["contracts/Foo.sol", "test/FooTest.t.sol"]


def test_modal_volume_code_storage_async_methods_use_modal_aio(monkeypatch):
    fake_volume = FakeModalVolume()
    monkeypatch.setattr(code_storage, "get_modal_volume", lambda volume_name: fake_volume)

    storage = code_storage.ModalVolumeCodeStorage(
        volume_name="partyhat-foundry-artifacts",
        base_dir="generated_contracts",
    )
    artifact = CodeArtifact(path="contracts/Foo.sol", language="solidity")

    saved_path = asyncio.run(storage.asave_code(artifact, "contract Foo {}"))
    storage._last_reload_at = 0.0
    content = asyncio.run(storage.aload_code("contracts/Foo.sol"))
    storage._last_reload_at = 0.0
    paths = asyncio.run(storage.alist_paths())

    assert saved_path == "contracts/Foo.sol"
    assert fake_volume.uploads == [
        ("generated_contracts/contracts/Foo.sol", b"contract Foo {}")
    ]
    assert fake_volume.batch_upload.calls == [True]
    assert content == "contract Foo {}\n"
    assert fake_volume.read_file.calls == ["generated_contracts/contracts/Foo.sol"]
    assert paths == ["contracts/Foo.sol"]
    assert fake_volume.listdir.calls == [("generated_contracts", True)]
    assert fake_volume.reload.async_calls == 2


def test_modal_volume_sync_methods_offload_when_called_from_async_context(monkeypatch):
    fake_volume = ThreadRecordingModalVolume()
    monkeypatch.setattr(code_storage, "get_modal_volume", lambda volume_name: fake_volume)

    storage = code_storage.ModalVolumeCodeStorage(
        volume_name="partyhat-foundry-artifacts",
        base_dir="generated_contracts",
    )
    artifact = CodeArtifact(path="contracts/Foo.sol", language="solidity")

    async def _run():
        caller_thread = threading.get_ident()
        saved_path = storage.save_code(artifact, "contract Foo {}")
        storage._last_reload_at = 0.0
        content = storage.load_code("contracts/Foo.sol")
        storage._last_reload_at = 0.0
        paths = storage.list_paths()
        return caller_thread, saved_path, content, paths

    caller_thread, saved_path, content, paths = asyncio.run(_run())

    assert saved_path == "contracts/Foo.sol"
    assert content == "contract Foo {}\n"
    assert paths == ["contracts/Foo.sol"]
    assert fake_volume.uploads == [
        ("generated_contracts/contracts/Foo.sol", b"contract Foo {}")
    ]
    assert fake_volume.batch_upload.calls == [True]
    assert fake_volume.read_file.calls == ["generated_contracts/contracts/Foo.sol"]
    assert fake_volume.listdir.calls == [("generated_contracts", True)]
    assert fake_volume.reload.thread_ids == [
        fake_volume.read_file.thread_ids[0],
        fake_volume.listdir.thread_ids[0],
    ]
    assert len(fake_volume.batch_upload.thread_ids) == 1
    assert len(fake_volume.read_file.thread_ids) == 1
    assert len(fake_volume.listdir.thread_ids) == 1
    assert all(thread_id != caller_thread for thread_id in fake_volume.batch_upload.thread_ids)
    assert all(thread_id != caller_thread for thread_id in fake_volume.read_file.thread_ids)
    assert all(thread_id != caller_thread for thread_id in fake_volume.listdir.thread_ids)
    assert all(thread_id != caller_thread for thread_id in fake_volume.reload.thread_ids)


def test_local_code_storage_async_methods_round_trip(tmp_path):
    storage = code_storage.LocalCodeStorage(base_dir=tmp_path)
    artifact = CodeArtifact(path="contracts/Local.sol", language="solidity")

    saved_path = asyncio.run(storage.asave_code(artifact, "contract Local {}"))
    content = asyncio.run(storage.aload_code("contracts/Local.sol"))
    paths = asyncio.run(storage.alist_paths())

    assert saved_path == "contracts/Local.sol"
    assert content == "contract Local {}"
    assert paths == ["contracts/Local.sol"]


def test_modal_volume_code_storage_async_edit_and_delete_use_modal_aio(monkeypatch):
    fake_volume = FakeModalVolume()
    monkeypatch.setattr(code_storage, "get_modal_volume", lambda volume_name: fake_volume)

    storage = code_storage.ModalVolumeCodeStorage(
        volume_name="partyhat-foundry-artifacts",
        base_dir="generated_contracts",
    )

    occurrences = asyncio.run(
        storage.aedit_code("contracts/Foo.sol", "Foo", "Bar")
    )
    asyncio.run(storage.adelete_code("contracts/Foo.sol"))

    assert occurrences == 1
    assert fake_volume.uploads == [
        ("generated_contracts/contracts/Foo.sol", b"contract Bar {}\n")
    ]
    assert fake_volume.read_file.calls == ["generated_contracts/contracts/Foo.sol"]
    assert fake_volume.remove_file.calls == ["generated_contracts/contracts/Foo.sol"]


def test_get_artifact_tree_uses_async_storage(monkeypatch):
    storage = AsyncOnlyStorage()
    monkeypatch.setattr(api, "ensure_project_context", _noop_ensure_project_context)
    monkeypatch.setattr(api, "get_code_storage", lambda project_id=None: storage)

    result = asyncio.run(
        api.get_artifact_tree(
            project_id="default",
            user_id="default",
            ctx=api.RequestContext(project_id="default", user_id="default"),
            session=None,
        )
    )

    assert storage.alist_paths_calls == 1
    assert result.name == "generated_contracts"
    assert result.type == "directory"
    assert [child.name for child in result.children] == ["contracts", "test"]


def test_get_artifact_tree_caches_by_code_version(monkeypatch):
    storage = AsyncOnlyStorage()
    monkeypatch.setattr(api, "ensure_project_context", _noop_ensure_project_context)
    monkeypatch.setattr(api, "get_code_storage", lambda project_id=None: storage)
    monkeypatch.setattr(
        api,
        "get_project_state_versions",
        lambda **kwargs: {"plan": "1", "code": "code-v7", "deployment": "1"},
    )
    api._ARTIFACT_TREE_CACHE.clear()

    first = asyncio.run(
        api.get_artifact_tree(
            project_id="project-123",
            user_id="user-123",
            ctx=api.RequestContext(project_id="project-123", user_id="user-123"),
            session=None,
        )
    )
    second = asyncio.run(
        api.get_artifact_tree(
            project_id="project-123",
            user_id="user-123",
            ctx=api.RequestContext(project_id="project-123", user_id="user-123"),
            session=None,
        )
    )

    assert storage.alist_paths_calls == 1
    assert first.path == second.path


def test_get_artifact_file_uses_async_storage(monkeypatch):
    storage = AsyncOnlyStorage()
    monkeypatch.setattr(api, "ensure_project_context", _noop_ensure_project_context)
    monkeypatch.setattr(api, "get_code_storage", lambda project_id=None: storage)

    result = asyncio.run(
        api.get_artifact_file(
            relative_path="contracts/Foo.sol",
            project_id="default",
            user_id="default",
            ctx=api.RequestContext(project_id="default", user_id="default"),
            session=None,
        )
    )

    assert storage.aload_code_calls == ["contracts/Foo.sol"]
    assert result.path == "contracts/Foo.sol"
    assert result.content == "pragma solidity ^0.8.20;"
