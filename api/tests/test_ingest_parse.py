"""解析阶段：PDF 不能被当成 UTF-8 文本读，扫描件要能自动走 OCR。

用户 2026-11-13 把一本 87MB 的教材拖进资料库，流水线一路绿灯跑完：19740 片、
354 万行、`完成`。而切片正文是 `%PDF-1.6` 和压缩流的乱码 ——
`materials._lines` 是按文本读的，喂它一个 PDF 它也不知道。于是一整本书
"入库"了、还算了两小时向量，检索里却一个字都没有。

第二刀是那份 PDF **是扫描件**（527 页只抽出 527 字）：光"抽不到就报错"只能把
问题推给用户，正确的落点是**自动走 OCR**（`ocr.convert`），并且逐页报进度。

这一组钉住：按类型抽、扫描件自动 OCR、抽不出来时报错而不是绿着入库。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app import attachments, ingest, materials, ocr
from app.db import get_session_factory
from app.models import Material, MaterialSlice


def test_plain_text_is_read_directly(tmp_path) -> None:  # noqa: ANN001
    """纯文本不折腾：原文件就是正文，不需要多一份缓存。"""
    path = tmp_path / "note.md"
    path.write_text("第一行\n第二行\n", encoding="utf-8")

    lines, extracted = materials.text_lines(str(path))

    assert lines == ["第一行", "第二行"]
    assert extracted is False


def test_pdf_is_extracted_not_decoded(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """PDF 走抽取，并把"给足时间"传下去（20 秒的默认值会把一本书抽成空串）。"""
    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF-1.6\n\xe2\xe3\xcf\xd3\n")
    calls: list[tuple[str, int, int]] = []

    def extract(target, kind, limit=0, timeout=0):  # noqa: ANN001, ANN202
        calls.append((kind, limit, timeout))
        return "第一章 绪论\n" + "采样定理讲的是带宽与频率的关系。\n" * 20

    monkeypatch.setattr(attachments, "extract", extract)
    monkeypatch.setattr(ocr, "pages_of", lambda _path: 0)

    lines, extracted = materials.text_lines(str(path))

    assert lines[0] == "第一章 绪论"
    assert extracted is True
    assert calls == [("pdf", 0, materials.PARSE_TIMEOUT)], "要整本、不截断、给足时间"


def test_a_scanned_pdf_falls_back_to_ocr(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """每页没几个字的 PDF 要自动 OCR —— 不是报错让用户自己去处理。"""
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.6\n")
    # 封面那三五行是**真文字**：只看"抽到了没有"的话，整本书会以这几行进库
    monkeypatch.setattr(attachments, "extract", lambda *a, **k: "电子技术基础\n模拟部分\n")
    monkeypatch.setattr(ocr, "pages_of", lambda _path: 400)
    seen: list[tuple[int, int]] = []

    def convert(pdf, *, first=1, last=0, on_page=None):  # noqa: ANN001, ANN202
        for page in range(1, 4):
            if on_page is not None:
                on_page(page, 400)
        return {"ok": True, "md": "## 第 1 页\n\n放大电路的输入电阻", "pages": 3, "chars": 900}

    monkeypatch.setattr(ocr, "convert", convert)

    lines, extracted = materials.text_lines(str(path), on_page=lambda done, total: seen.append((done, total)))

    assert lines[-1] == "放大电路的输入电阻", "正文要从 OCR 那份来"
    assert extracted is True
    assert seen == [(1, 400), (2, 400), (3, 400)], "几百页的长活要逐页报数"


def test_ocr_unavailable_or_failed_is_an_error(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """OCR 也不行时要红着停下：扫描件认不出字等于没有正文，不能绿着入库。"""
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.6\n")
    monkeypatch.setattr(attachments, "extract", lambda *a, **k: "")
    monkeypatch.setattr(ocr, "pages_of", lambda _path: 400)
    monkeypatch.setattr(ocr, "available", lambda: (False, "没装 rapidocr_onnxruntime"))

    with pytest.raises(materials.MaterialError) as caught:
        materials.text_lines(str(path))

    assert "抽不出正文" in str(caught.value)
    assert "OCR 不可用" in str(caught.value), "报错要说清缺什么"


def _drop(material_id: int, text_path: str) -> None:
    session = get_session_factory()()
    try:
        session.query(MaterialSlice).filter(MaterialSlice.material_id == material_id).delete()
        session.query(Material).filter(Material.id == material_id).delete()
        session.commit()
    finally:
        session.close()
    if text_path:
        Path(text_path).unlink(missing_ok=True)


def test_ingested_pdf_keeps_readable_text_and_the_original_path(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """端到端：切片里是正文，`source_path` 仍是那份 PDF（"打开原文"要它）。"""
    path = tmp_path / f"book-{uuid.uuid4().hex[:6]}.pdf"
    path.write_bytes(b"%PDF-1.6\n\xe2\xe3\xcf\xd3\n")
    body = "\n".join(f"第 {n} 行：采样定理讲的是带宽与频率的关系" for n in range(1, 41))
    monkeypatch.setattr(attachments, "extract", lambda *a, **k: body)
    monkeypatch.setattr(ocr, "pages_of", lambda _path: 0)

    session = get_session_factory()()
    material_id, text_path = 0, ""
    try:
        report = ingest.ingest_file(session, path, embed=False)
        material = session.query(Material).filter(Material.slug == report["slug"]).one()
        material_id, text_path = int(material.id), material.text_path
        slices = session.query(MaterialSlice).filter(MaterialSlice.material_id == material.id).all()

        assert text_path, "抽出来的正文要有落脚处，否则 read_lines 读不到"
        assert Path(text_path).read_text(encoding="utf-8") == body
        assert all("%PDF" not in one.text for one in slices), "库里不该再有二进制垃圾"
        assert "采样定理" in slices[0].text
        assert material.source_path == str(path), "原文件路径不能被正文缓存顶掉"
        assert materials.source_file(material) == text_path, "按行读的是纯文本那一份"
    finally:
        session.close()
        _drop(material_id, text_path)
