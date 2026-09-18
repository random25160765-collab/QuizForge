"""资料归类：文件名线索、模型结果的收拾、落盘纪律。

这一批用例走**小子集**（临时目录三五个文件、纯函数），毫秒级跑完 ——
先前拿整个 282MB 的真库当验证循环，一次两分钟，是那轮拖慢的根源。
真语料只用来定"阈值与规则"，不用来当测试。

盯的是四件会写坏数据的事：

* `.pdf` **不许**再默认成"论文"（用户点出来的那处错）；
* 文件名线索要认得出（`cuda-programming-guide` → 手册、`IEEE.1364-2005` → 规范）；
* 模型返回的 JSON 无论怎么抖（缺字段、类型不认识、topics 是字符串）都不能写坏元数据；
* **人定过的（`manual`）不许被模型覆盖**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import library as lib  # noqa: E402
from pipeline import doc_classify as dc  # noqa: E402


@pytest.fixture()
def roots(tmp_path: Path) -> Path:
    """小子集：三份文件，名字分别对应"线索能判""线索判不了""非 PDF"。"""
    base = tmp_path / "Documents"
    (base / "cuda").mkdir(parents=True)
    (base / "cuda" / "cuda-programming-guide.pdf").write_bytes(b"%PDF fake")
    (base / "cuda" / "Trefethen-Bau.pdf").write_bytes(b"%PDF fake")
    (base / "cuda" / "notes.md").write_text("# 笔记\n\n随手记的\n", encoding="utf-8")
    return base


# ------------------------------------------------------------------ 类别表和默认值


def test_pdf_no_longer_defaults_to_paper(roots: Path):
    """**回归用例**：`.pdf` 不许默认成"论文"。

    用户点出来的：库里大量是技术文档（芯片手册、指令集规范、编程指南），
    一律标"论文"一眼就不对。类型交给"文件名线索 → 模型判 → 人复核"三步。
    """
    meta = lib.infer(lib.scan(roots)[1])
    assert meta["kind"] != "paper"
    assert meta["kind"] in lib.KINDS


def test_kind_table_is_the_single_source():
    """类别只有一处定义：提示词、校验、界面标签都从 `lib.KINDS` 来。"""
    for key in ("paper", "manual", "spec", "report", "book", "other"):
        assert key in lib.KINDS
        assert key in lib.KIND_LABEL
    assert len(set(lib.KINDS)) == len(lib.KINDS)


def test_file_name_hints_from_the_real_corpus():
    cases = {
        "cuda-programming-guide": "manual",
        "CUDA C Best Practices Guide": "manual",
        "ptx_isa_9.3": "spec",                      # `_` 曾让它漏判
        "IEEE.1364-2005": "spec",
        "amd-cdna-4-architecture-whitepaper": "report",
        "EECS-2016-143": "report",
        "RM0008 STM32 参考手册": "manual",
    }
    for stem, want in cases.items():
        assert lib._kind_hint(stem) == want, stem
    # 看不出就**别瞎猜**：线索为空时交给模型
    assert lib._kind_hint("Trefethen-Bau") == ""
    assert lib._kind_hint("2205.14135v2") == ""


# ------------------------------------------------------------------ 模型结果的收拾


def test_coerce_tolerates_every_shape_the_model_may_return():
    assert dc.coerce({"items": [{"citekey": "a", "kind": "manual"}]}) == [
        {"citekey": "a", "kind": "manual", "topics": [], "why": ""}
    ]
    assert dc.coerce([{"citekey": "a", "kind": "spec", "topics": ["verilog"]}])[0]["topics"] == ["verilog"]
    # topics 给成字符串（模型常这么干）、用顿号分隔
    assert dc.coerce({"items": [{"citekey": "a", "kind": "book", "topics": "线性代数、数值"}]})[0]["topics"] == [
        "线性代数",
        "数值",
    ]
    # 不认识的类别、缺 citekey 的一律丢掉，而不是写进元数据
    assert dc.coerce({"items": [{"citekey": "a", "kind": "论文"}]}) == []
    assert dc.coerce({"items": [{"kind": "manual"}]}) == []
    assert dc.coerce("一段散文") == []
    assert dc.coerce(None) == []


def test_accept_drops_unknown_citekeys():
    results = [{"citekey": "在", "kind": "book"}, {"citekey": "不在", "kind": "paper"}]
    good, dropped = dc.accept(results, {"在"})
    assert [one["citekey"] for one in good] == ["在"]
    assert dropped == [{"citekey": "不在", "why": "库里没有这个引用键"}]


# ------------------------------------------------------------------ 候选与落盘


def test_candidates_skip_what_is_already_decided(roots: Path, tmp_path: Path):
    meta_dir = tmp_path / "lib"
    text_dir = meta_dir / ".text"
    lib.save_metadata(meta_dir, {"citekey": "cudaprogrammingguide", "title": "x", "kind": "manual", "source": str(roots / "cuda" / "cuda-programming-guide.pdf")}, origin="manual")
    lib.save_metadata(meta_dir, {"citekey": "trefethenbau", "title": "y", "kind": "book", "source": str(roots / "cuda" / "Trefethen-Bau.pdf")}, origin="llm")
    picked = dc.pick_candidates([roots], meta_dir, text_dir, limit=10)
    # 人定的与模型定过的都不该再送模型
    assert [one["citekey"] for one in picked] == ["notes"]


def test_apply_records_llm_and_never_overwrites_a_human(roots: Path, tmp_path: Path):
    meta_dir = tmp_path / "lib"
    lib.save_metadata(
        meta_dir,
        {"citekey": "trefethenbau", "title": "数值线性代数", "kind": "other", "source": str(roots / "cuda" / "Trefethen-Bau.pdf")},
        origin="inferred",
    )
    lib.save_metadata(
        meta_dir,
        {"citekey": "handmade", "title": "人手填的", "kind": "paper", "source": "x"},
        origin="manual",
    )
    result = dc.apply(
        meta_dir,
        [
            {"citekey": "trefethenbau", "kind": "book", "topics": ["数值线性代数"]},
            {"citekey": "handmade", "kind": "spec", "topics": ["乱改"]},
            {"citekey": "没有这份", "kind": "paper"},
        ],
    )
    assert result["written"] == ["trefethenbau"]
    assert {item["citekey"] for item in result["skipped"]} == {"handmade", "没有这份"}

    back = lib.load_all_metadata(meta_dir)
    assert back["trefethenbau"]["kind"] == "book"
    assert back["trefethenbau"]["origin"] == "llm"
    assert back["trefethenbau"]["topics"] == ["数值线性代数"]
    assert back["handmade"]["kind"] == "paper"      # 人的东西没被动过
    assert back["handmade"]["origin"] == "manual"
