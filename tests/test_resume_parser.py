from app.models import ParseStatus
from app.services.resume_parser import parse_resume
from tests.conftest import RESUME_LINES, make_docx, make_pdf


def test_pdf_ok():
    r = parse_resume("cv.pdf", make_pdf([RESUME_LINES]))
    assert r.status == ParseStatus.ok
    assert "FastAPI" in r.text
    assert r.warnings == []


def test_docx_ok_includes_tables():
    r = parse_resume("cv.docx", make_docx(RESUME_LINES.splitlines()))
    assert r.status == ParseStatus.ok
    assert "Kafka" in r.text
    assert "Skill | Python" in r.text


def test_pdf_with_image_only_page_is_partial():
    r = parse_resume("cv.pdf", make_pdf([RESUME_LINES, ""]))
    assert r.status == ParseStatus.partial
    assert any("page(s) 2" in w for w in r.warnings)


def test_blank_pdf_fails_with_warning():
    r = parse_resume("scan.pdf", make_pdf([""]))
    assert r.status == ParseStatus.failed
    assert any("scanned" in w for w in r.warnings)


def test_short_text_is_partial():
    r = parse_resume("cv.pdf", make_pdf(["Jane Doe"]))
    assert r.status == ParseStatus.partial


def test_corrupt_pdf_fails_without_raising():
    r = parse_resume("cv.pdf", b"%PDF-1.4 garbage garbage")
    assert r.status == ParseStatus.failed
    assert r.warnings


def test_wrong_type_rejected():
    r = parse_resume("cv.pdf", b"PK\x03\x04notapdf")
    assert r.status == ParseStatus.failed
