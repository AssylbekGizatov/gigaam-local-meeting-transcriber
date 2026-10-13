from pathlib import Path

import pytest

from gigaam_meeting.contracts import Segment
from gigaam_meeting.exports import export_transcript, format_time


TEXT = "Бүгін қазақша сөйлейміз. Сегодня проверяем речь. English speech & <test>."
SEGMENTS = [
    Segment("system", 5.0, 7.5, TEXT),
    Segment("microphone", 0.0, 2.4, "Микрофон: ә ғ қ ң ө ұ ү һ і"),
]


@pytest.mark.parametrize("format", ["TXT", "MD"])
def test_unicode_sources_and_shared_timestamps(tmp_path: Path, format: str):
    file = export_transcript(SEGMENTS, tmp_path / ("test." + format.lower()), format)
    text = file.read_text(encoding="utf-8")
    assert "ә ғ қ ң ө ұ ү һ і" in text
    assert "Бүгін қазақша сөйлейміз" in text
    assert "Сегодня проверяем речь" in text
    assert "English speech" in text
    assert text.index("00:00:00.000") < text.index("00:00:05.000")
    assert "Звук компьютера" in text
    assert "не обозначают отдельных людей" in text


@pytest.mark.parametrize("format", ["TXT", "MD", "DOCX", "PDF"])
def test_no_overwrite_even_when_optional_dependency_missing(tmp_path: Path, format: str):
    file = tmp_path / ("existing." + format.lower())
    file.write_bytes(b"original content")
    with pytest.raises(FileExistsError):
        export_transcript(SEGMENTS, file, format)
    assert file.read_bytes() == b"original content"


def test_docx_unicode_and_sources(tmp_path: Path):
    docx = pytest.importorskip("docx")
    file = export_transcript(SEGMENTS, tmp_path / "test.docx", "DOCX")
    document = docx.Document(file)
    text = "\n".join(p.text for p in document.paragraphs)
    assert TEXT in text
    assert "ә ғ қ ң ө ұ ү һ і" in text
    assert "Микрофон" in text and "Звук компьютера" in text


def test_pdf_contains_unicode_and_embedded_font(tmp_path: Path):
    pytest.importorskip("reportlab")
    from gigaam_meeting.exports import _pdf_font
    try:
        _pdf_font()
    except FileNotFoundError:
        pytest.skip("Bundled font unavailable in this source fixture")
    file = export_transcript(SEGMENTS, tmp_path / "test.pdf", "PDF")
    assert file.read_bytes().startswith(b"%PDF-")
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(file)
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert "ә ғ қ ң ө ұ ү һ і" in text
    assert "Бүгін қазақша сөйлейміз" in text
    assert "Сегодня проверяем речь" in text
    assert "English speech & <test>" in text
    resources = reader.pages[0]["/Resources"]["/Font"]
    descriptors = [font.get_object().get("/FontDescriptor") for font in resources.values()]
    assert any(descriptor and "/FontFile2" in descriptor.get_object() for descriptor in descriptors)


def test_unknown_format_creates_nothing(tmp_path: Path):
    with pytest.raises(ValueError):
        export_transcript(SEGMENTS, tmp_path / "test.bin", "BIN")
    assert not (tmp_path / "test.bin").exists()


def test_time_rounds_carry_and_pause_hours():
    assert format_time(59.9996) == "00:01:00.000"
    assert format_time(3600.123) == "01:00:00.123"
    assert format_time(-2) == "00:00:00.000"
