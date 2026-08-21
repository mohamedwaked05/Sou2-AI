"""Private provider-neutral storage for knowledge source files."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO, Protocol


class KnowledgeStorage(Protocol):
    def store(
        self, business_id: uuid.UUID, document_id: uuid.UUID, source: BinaryIO
    ) -> str: ...
    def open(
        self, business_id: uuid.UUID, document_id: uuid.UUID, key: str
    ) -> BinaryIO: ...
    def delete(
        self, business_id: uuid.UUID, document_id: uuid.UUID, key: str
    ) -> None: ...
    def stage_delete(
        self, business_id: uuid.UUID, document_id: uuid.UUID, key: str
    ) -> object: ...
    def restore(self, staged: object) -> None: ...
    def finalize_delete(self, staged: object) -> None: ...


class LocalKnowledgeStorage:
    """Private local storage; keys, never filenames, determine all paths."""

    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()

    @staticmethod
    def key(business_id: uuid.UUID, document_id: uuid.UUID) -> str:
        return f"businesses/{business_id}/knowledge/{document_id}/source"

    def _path(self, business_id: uuid.UUID, document_id: uuid.UUID, key: str) -> Path:
        expected = self.key(business_id, document_id)
        if key != expected:
            raise ValueError("Invalid knowledge storage key.")
        path = (self.root / Path(*key.split("/"))).resolve()
        if self.root not in path.parents:
            raise ValueError("Invalid knowledge storage key.")
        return path

    def store(
        self, business_id: uuid.UUID, document_id: uuid.UUID, source: BinaryIO
    ) -> str:
        key = self.key(business_id, document_id)
        path = self._path(business_id, document_id, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as output:
                shutil.copyfileobj(source, output, length=64 * 1024)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return key

    def open(
        self, business_id: uuid.UUID, document_id: uuid.UUID, key: str
    ) -> BinaryIO:
        return self._path(business_id, document_id, key).open("rb")

    def delete(self, business_id: uuid.UUID, document_id: uuid.UUID, key: str) -> None:
        self._path(business_id, document_id, key).unlink(missing_ok=True)

    def stage_delete(
        self, business_id: uuid.UUID, document_id: uuid.UUID, key: str
    ) -> Path:
        path = self._path(business_id, document_id, key)
        staged = path.with_name(f".{path.name}.{uuid.uuid4().hex}.deleting")
        if path.exists():
            os.replace(path, staged)
        return staged

    def restore(self, staged: object) -> None:
        path = Path(staged)
        if path.exists():
            os.replace(path, path.with_name("source"))

    def finalize_delete(self, staged: object) -> None:
        Path(staged).unlink(missing_ok=True)


def get_knowledge_storage(settings: object) -> KnowledgeStorage:
    return LocalKnowledgeStorage(settings.knowledge_storage_root)  # type: ignore[attr-defined]
