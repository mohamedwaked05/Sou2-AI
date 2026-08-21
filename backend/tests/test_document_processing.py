"""Focused Docker-independent parsing and chunking regression coverage."""

import pytest
from app.rag.document_processing import DocumentProcessingError, chunk_text, normalize


def test_normalization_preserves_arabic_and_franco_arabic() -> None:
    assert (
        normalize("Mar7aba\r\n\r\nأهلا\x00   ya   shabeb", 500_000)
        == "Mar7aba\n\nأهلا ya shabeb"
    )


def test_chunking_is_deterministic_and_bounded() -> None:
    text = "\n\n".join(["word " * 300, "second " * 300])
    first = chunk_text(text)
    assert first == chunk_text(text)
    assert all(len(chunk) <= 1600 for chunk in first)
    assert len(first) > 1


def test_normalization_rejects_empty_text() -> None:
    with pytest.raises(DocumentProcessingError, match="empty_extracted_text"):
        normalize("\x00\n\r", 500_000)
