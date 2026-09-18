"""资料库：扫描、推断、引用键、正文质量、检索。

这些用例的取舍很清楚 —— 盯的是**真实语料里会出错的地方**，不是"功能有没有"：

* 推断的每一条约束都是从 `/mnt/f/Documents` 那 55 个 PDF 里抠出来的
  （标题跨行、封面印章、版权声明、地址行）；
* **乱码不许参与推断**（实测它差点把引用键从 `trefethenbau` 毁成 `doc-30e408e0`）；
* 引用键**可重复算出同一个值**（它是被别的笔记引用的东西，飘了就断链）；
* 元数据读写要绕得回来（我们自己写的格式，自己得读得懂）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import library as lib  # noqa: E402


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    """一个假资料根：几份"文件"够了，不需要真 PDF（真 PDF 的坑另有用例单独钉）。"""
    base = tmp_path / "Documents"
    (base / "cuda").mkdir(parents=True)
    (base / "cuda" / "2205.14135v2.pdf").write_bytes(b"%PDF-1.4 fake")
    (base / "cuda" / "Trefethen-Bau.pdf").write_bytes(b"%PDF-1.4 fake")
    (base / "cuda" / "Linux命令行与shell脚本编程大全（第3版）.pdf").write_bytes(b"%PDF-1.4 fake")
    (base / "ic" / "IEEE.1364-2005.pdf").parent.mkdir(parents=True)
    (base / "ic" / "IEEE.1364-2005.pdf").write_bytes(b"%PDF-1.4 fake")
    (base / "nvidia" / "EECS-2016-143.pdf").parent.mkdir(parents=True)
    (base / "nvidia" / "EECS-2016-143.pdf").write_bytes(b"%PDF-1.4 fake")
    (base / "cuda" / "__pycache__").mkdir()
    (base / "cuda" / "__pycache__" / "junk.pyc").write_bytes(b"\x00\x01")
    (base / "cuda" / ".hidden.md").write_text("看不见的\n", encoding="utf-8")
    (base / "cuda" / "notes.md").write_text(
        "---\ntitle: 多项式\n---\n\n![图](fig.png)\n![远程](https://x.com/a.png)\n", encoding="utf-8"
    )
    (base / "cuda" / "fig.png").write_bytes(b"\x89PNG fake")
    return base


def _item(root: Path, rel: str) -> lib.Item:
    return [item for item in lib.scan(root) if item.rel == rel][0]


# ------------------------------------------------------------------ 扫描


def test_scan_skips_caches_and_hidden(root: Path):
    rels = [item.rel for item in lib.scan(root)]
    assert "cuda/2205.14135v2.pdf" in rels
    assert not any("__pycache__" in rel for rel in rels), rels
    assert not any(rel.startswith(".") or "/.hidden" in rel for rel in rels), rels
    assert not any(rel.endswith(".pyc") for rel in rels), rels


def test_assets_are_the_files_the_primary_references(root: Path):
    """一个条目 = 一个主文件 + 它引用的附属资源；远程图片不算。"""
    assets = lib.assets_of(_item(root, "cuda/notes.md"))
    assert [path.name for path in assets] == ["fig.png"]
    assert lib.assets_of(_item(root, "cuda/2205.14135v2.pdf")) == []   # PDF 不读外部引用


# ------------------------------------------------------------------ 推断


def test_infer_reads_deterministic_ids_from_the_file_name(root: Path):
    arxiv = lib.infer(_item(root, "cuda/2205.14135v2.pdf"))
    assert arxiv["arxiv"] == "2205.14135"
    assert arxiv["year"] == 2022          # arXiv 号的 `2205` 就是 2022 年 —— 比在正文里猜可靠
    assert arxiv["kind"] == "paper"
    assert arxiv["topics"] == ["cuda"]

    report = lib.infer(_item(root, "nvidia/EECS-2016-143.pdf"))
    assert report["year"] == 2016 and report["kind"] == "report"

    ieee = lib.infer(_item(root, "ic/IEEE.1364-2005.pdf"))
    assert ieee["year"] == 2005


def test_prose_lines_are_judged_one_by_one():
    """判定粒度是"要用的那一行"，不是整篇 —— FlashAttention 整篇只有 48%（满页符号）。"""
    assert lib._looks_like_prose("FlashAttention: Fast and Memory-Efficient Exact Attention")
    assert not lib._looks_like_prose("'&     ( )'*%+,.-/)1032546464")       # 实测的乱码行
    assert not lib._looks_like_prose("Evaluation Copy")                     # 封面印章
    assert not lib._looks_like_prose("August 13, 2025")                     # 日期行


def test_author_lines_reject_titles_addresses_and_legal_boilerplate():
    """三条硬约束都是踩出来的：标题含冒号、地址含数字、声明含机构词。"""
    assert lib._looks_like_authors("Tri Dao, Daniel Y. Fu, Stefano Ermon, Atri Rudra, Christopher Re")
    # 标题里有 `and` 与逗号 —— 只看这两样会把标题当作者（实测就是这么写反的）
    assert not lib._looks_like_authors("FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness")
    assert not lib._looks_like_authors("New York, NY 10016-")               # 商标页地址
    assert not lib._looks_like_authors("Compute Express Link Consortium, Inc.")  # 版权声明
    assert not lib._looks_like_authors("COMPUTE EXPRESS LINK CONSORTIUM, INC.")  # 全大写 = 机构


def test_infer_joins_a_title_that_wraps_over_lines(root: Path):
    """跨行标题要拼回来 —— 只取一行会得到 `with IO-Awareness` 这种半截标题。"""
    head = (
        "\n"
        "FlashAttention: Fast and Memory-Efficient Exact Attention\n"
        "with IO-Awareness\n"
        "Tri Dao, Daniel Y. Fu, Stefano Ermon, Atri Rudra, Christopher Re\n"
        "Abstract\n"
    )
    meta = lib.infer(_item(root, "cuda/2205.14135v2.pdf"), head_text=head)
    assert meta["title"].startswith("FlashAttention: Fast")
    assert "IO-Awareness" in meta["title"]
    assert meta["authors"][0] == "Tri Dao"


def test_infer_leaves_unknowns_empty_instead_of_guessing(root: Path):
    """不猜的就不写：猜错一个作者，比留空更难发现。"""
    meta = lib.infer(_item(root, "cuda/Trefethen-Bau.pdf"), head_text="")
    assert meta["title"] == "Trefethen Bau"
    assert "authors" not in meta
    assert "year" not in meta


# ------------------------------------------------------------------ 引用键


def test_citekey_ladder(root: Path):
    author = lib.citekey_for(
        {"authors": ["Tri Dao"], "year": 2022, "title": "FlashAttention: Fast and Memory-Efficient"},
        fallback="cuda/2205.14135v2.pdf",
    )
    assert author.startswith("dao2022flashattention")
    assert author == author.lower() and " " not in author

    assert lib.citekey_for({"arxiv": "2205.14135"}, fallback="x.pdf") == "arxiv220514135"
    # 标准号里的 `1364` 不是 ASCII 词（是数字），所以键是 `ieee2005`；
    # 同族的标准靠年份区分够用，撞了还有 `-2` 后缀兜着
    assert lib.citekey_for({"year": 2005}, fallback="ic/IEEE.1364-2005.pdf") == "ieee2005"
    assert lib.citekey_for({}, fallback="cuda/Trefethen-Bau.pdf") == "trefethenbau"
    # 纯中文名 → 没有可用 ASCII 词 → 哈希兜底，但要**可重复**
    first = lib.citekey_for({}, fallback="某本中文书.pdf")
    assert first.startswith("doc-") and first == lib.citekey_for({}, fallback="某本中文书.pdf")


def test_garbled_text_never_reaches_inference(root: Path, tmp_path: Path):
    """**回归用例**：乱码正文不许参与推断。

    实测踩过：`Trefethen-Bau.pdf` 抽出来是 `"!$#%` 这种东西，照着推断会把引用键
    从 `trefethenbau` 毁成 `doc-30e408e0` —— 没有信息好过有错的信息。
    """
    text_dir = tmp_path / "lib" / ".text"
    text_dir.mkdir(parents=True)
    (text_dir / "trefethenbau.txt").write_text("'&     ( )'*%+,.-/)1032546464\n", encoding="utf-8")
    (text_dir / "trefethenbau.meta.json").write_text(
        json.dumps({"state": "garbled", "chars": 40, "ratio": 0.04, "mtime": 0}), encoding="utf-8"
    )
    item = _item(root, "cuda/Trefethen-Bau.pdf")
    assert lib.head_text_for(item, text_dir, "trefethenbau") == ""
    meta = lib.infer(item, head_text=lib.head_text_for(item, text_dir, "trefethenbau"))
    assert lib.citekey_for(meta, fallback=item.rel) == "trefethenbau"


def test_judge_text_bands():
    assert lib.judge_text("This is a normal English paragraph about compilers and caches.")["state"] == "ok"
    assert lib.judge_text("'&     ( )'*%+,.-/)1032546464 \"!$#%")["state"] == "garbled"
    assert lib.judge_text("")["state"] == "none"


# ------------------------------------------------------------------ 元数据


def test_metadata_round_trip_keeps_lists_and_odd_strings(tmp_path: Path):
    meta_dir = tmp_path / "library"
    saved = lib.save_metadata(
        meta_dir,
        {
            "citekey": "dao2022flashattention",
            "title": "FlashAttention: Fast and Memory-Efficient",   # 含冒号
            "authors": ["Tri Dao", "Daniel Y. Fu"],
            "year": 2022,
            "kind": "paper",
            "topics": ["cuda", "attention"],
            "files": {"path": "/x/y.pdf", "role": "primary"},
        },
        origin="inferred",
    )
    assert saved["origin"] == "inferred"
    assert not list(meta_dir.glob("*.tmp")), "原子写不该留下临时文件"

    back = lib.load_all_metadata(meta_dir)["dao2022flashattention"]
    assert back["title"] == "FlashAttention: Fast and Memory-Efficient"
    assert back["authors"] == ["Tri Dao", "Daniel Y. Fu"]
    assert back["year"] == 2022
    assert back["files"]["role"] == "primary"
    assert back["origin"] == "inferred"


def test_manual_edits_win_and_are_marked(tmp_path: Path):
    meta_dir = tmp_path / "library"
    lib.save_metadata(meta_dir, {"citekey": "k1", "title": "推断的"}, origin="inferred")
    lib.save_metadata(meta_dir, {"citekey": "k1", "title": "人改的", "year": 1997}, origin="manual")
    back = lib.load_all_metadata(meta_dir)["k1"]
    assert back["title"] == "人改的" and back["origin"] == "manual"


def test_entries_give_duplicate_keys_a_suffix(tmp_path: Path):
    """两个同名文件不能共用一个引用键 —— 否则"谁引用了它"会指向两个东西。"""
    base = tmp_path / "docs"
    (base / "a").mkdir(parents=True)
    (base / "b").mkdir(parents=True)
    (base / "a" / "Trefethen-Bau.pdf").write_bytes(b"%PDF fake")
    (base / "b" / "Trefethen-Bau.pdf").write_bytes(b"%PDF fake")
    keys = [entry.citekey for entry in lib.entries([base], tmp_path / "lib", tmp_path / "lib" / ".text")]
    assert sorted(keys) == ["trefethenbau", "trefethenbau-2"]


# ------------------------------------------------------------------ 检索与互引


def test_search_keeps_garbled_text_out(monkeypatch, tmp_path: Path):
    """乱码不进检索池：搜到了，比搜不到更坏（用户以为库里有，其实那是垃圾）。"""
    base = tmp_path / "docs"
    base.mkdir()
    (base / "a.pdf").write_bytes(b"%PDF fake")
    meta_dir = tmp_path / "lib"
    text_dir = meta_dir / ".text"
    text_dir.mkdir(parents=True)
    lib.save_metadata(
        meta_dir,
        {"citekey": "a2001thing", "title": "一份东西", "topics": ["x"], "source": str(base / "a.pdf")},
        origin="inferred",
    )
    (text_dir / "a2001thing.txt").write_text("banana banana banana", encoding="utf-8")
    (text_dir / "a2001thing.meta.json").write_text(
        json.dumps({"state": "garbled", "chars": 21, "ratio": 0.2, "mtime": 0}), encoding="utf-8"
    )
    assert lib.search([base], meta_dir, text_dir, "banana") == []       # 乱码里的词搜不到
    assert len(lib.search([base], meta_dir, text_dir, "title:东西")) == 1  # 元数据照样能命中


def test_frozen_key_does_not_have_to_match_the_guessed_one(tmp_path: Path):
    """**回归用例**：元数据按**源文件路径**认，不按"临时猜的键"。

    实测踩过：`2205.14135v2.pdf` 先猜到 `arxiv220514135`，抽完正文冻结成
    `dao2022flashattention`；列表时若只按键查，这份元数据永远查不到 ——
    表现是"索引跑完了，界面上标题与作者还是空的"。
    """
    base = tmp_path / "docs"
    base.mkdir()
    target = base / "2205.14135v2.pdf"
    target.write_bytes(b"%PDF fake")
    meta_dir = tmp_path / "lib"
    lib.save_metadata(
        meta_dir,
        {"citekey": "dao2022flashattention", "title": "FlashAttention", "source": str(target)},
        origin="inferred",
    )
    entries = lib.entries([base], meta_dir, meta_dir / ".text")
    assert [entry.citekey for entry in entries] == ["dao2022flashattention"]
    assert entries[0].brief()["title"] == "FlashAttention"


def test_referencing_notes_matches_on_the_key_or_the_file_name():
    notes = [
        {"path": "笔记.md", "title": "笔记", "body": "见 dao2022flashattention 那篇"},
        {"path": "别处.md", "title": "别处", "body": "提到 Trefethen-Bau.pdf"},
        {"path": "无关.md", "title": "无关", "body": "什么都没提"},
    ]
    hits = lib.referencing_notes(["dao2022flashattention", "Trefethen-Bau.pdf"], notes)
    assert sorted(hit["path"] for hit in hits) == ["别处.md", "笔记.md"]
    assert lib.referencing_notes([], notes) == []


def test_http_face_is_mounted_under_api(tmp_path: Path, monkeypatch):
    """**回归用例**：资料路由必须挂在 `/api/library/...` 下。

    实测踩过：前缀写成 `/library` 时路由确实挂上了，但 `/api/library/roots` 一律 404 ——
    前端只会看到"空的资料树"，不报错的那种。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("QF_LIBRARY_ROOTS", str(tmp_path))
    client = TestClient(app)
    assert client.get("/api/library/roots").status_code == 200
    assert client.get("/api/library/items").status_code == 200
    assert client.get("/api/library/roots").json()["items"] == 0


def test_bibtex_has_the_fields_that_matter():
    text = lib.bibtex(
        {
            "citekey": "dao2022flashattention",
            "title": "FlashAttention",
            "authors": ["Tri Dao", "Daniel Y. Fu"],
            "year": 2022,
            "kind": "paper",
            "arxiv": "2205.14135",
            "topics": ["attention"],
        }
    )
    assert text.startswith("@article{dao2022flashattention,")
    assert "author = {Tri Dao and Daniel Y. Fu}" in text
    assert "archivePrefix = {arXiv}" in text
