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

from app import attachments as att  # noqa: E402
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


def test_a_symbol_heavy_cover_does_not_condemn_the_whole_document():
    """**回归用例**：封面加目录的符号密度不该判死整篇。

    实测踩过：第一版只看头 4000 字，于是 14 份说明书被判成"抽不出字" ——
    它们的开头是封面与目录（满是点线、页码、版本戳），而正文是好的。
    改成整篇分五段取中位数之后，那些文件的中位占比是 54~78%。
    """
    cover = ("CUDA Programming Guide\nRelease 13.3\n\n" + ". . . . . . . . . . 1-1\n" * 120)
    body = ("This chapter explains how the execution model maps threads to hardware, "
            "and why coalesced access matters for throughput. ") * 60
    got = lib.judge_text(cover + body)          # 短文本只采到很少几段，中位由正文那几段决定
    assert got["state"] != "garbled", got
    assert got["ratio"] > got["headRatio"], "头部与整篇的差距应当被记下来"


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
    (base / "cuda").mkdir(parents=True)
    (base / "nvidia").mkdir(parents=True)
    (base / "cuda" / "Trefethen-Bau.pdf").write_bytes(b"%PDF fake")
    (base / "nvidia" / "Trefethen-Bau.pdf").write_bytes(b"%PDF fake")
    keys = [entry.citekey for entry in lib.entries([base], tmp_path / "lib", tmp_path / "lib" / ".text")]
    # 后缀**来自路径**（目录名），不是流水号 —— 流水号随扫描顺序变，
    # 于是冻结的元数据与正文缓存会对不上（这条规矩踩过两次，见 `lib.disambiguate`）。
    # 目录名凑不出 ASCII 词时退到 8 位哈希，仍然稳定。
    # 先扫到的那个保留裸键，后面的按**路径**加后缀（不是流水号）；
    # 扫描顺序排过序，所以两遍跑出来结果一致 —— 这一点至关重要：
    # 键会变的话，冻结的元数据与正文缓存就再也对不上了。
    assert sorted(keys) == ["trefethenbau", "trefethenbau-nvidia"]
    again = [entry.citekey for entry in lib.entries([base], tmp_path / "lib", tmp_path / "lib" / ".text")]
    assert sorted(again) == sorted(keys), "重扫一遍键就变了"


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


# ------------------------------------------------------------------ 本轮实测揪出的三处


def test_media_never_becomes_an_item_of_its_own(root: Path):
    """附属资源不单独成条目，但要数得出来，并且挂得住。

    实测踩过：被正文引用的 `figs/fig-1.svg` 一边进了那条目的 `assets`，
    一边又自己成了列表里的一条 —— 真实语料 424 张 png，列表会被淹掉。
    """
    (root / "figs").mkdir(parents=True, exist_ok=True)
    (root / "figs" / "fig-1.svg").write_text("<svg/>", encoding="utf-8")
    (root / "Review.md").write_text("# Review\n\n![fig](figs/fig-1.svg)\n", encoding="utf-8")

    rels = [item.rel for item in lib.scan(root)]
    assert "figs/fig-1.svg" not in rels, "图片不该自己成条目"
    assert "Review.md" in rels
    # 注意：共用的 fixture 里本来就有一张 `fig.png`，所以这里只断言"包含"
    assert "fig-1.svg" in [path.name for path in lib.scan_media(root)], "但它得数得出来，不能悄悄消失"

    review = [item for item in lib.scan(root) if item.rel == "Review.md"][0]
    assert [path.name for path in lib.assets_of(review)] == ["fig-1.svg"], "而且要挂在引用它的那条目上"


def test_index_extracts_text_even_when_metadata_was_hand_filled(tmp_path: Path, monkeypatch):
    """**回归用例**：手工补过元数据的条目，照样要抽正文。

    踩过的是判据：拿"元数据冻结没有"当"处理过没有"，于是人补过作者年份的那几份
    永远抽不到正文 —— 搜不到、正文面板一直是空的，而且怎么点"建索引"都没用。
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routers import library as route

    doc_root = tmp_path / "docs"
    doc_root.mkdir()
    (doc_root / "Hand-Filled-Note.md").write_text(
        "# Hand Filled Note\n\nThis one was touched by a human.\n", encoding="utf-8"
    )
    meta_dir = tmp_path / "library"
    text_dir = meta_dir / ".text"
    monkeypatch.setenv("QF_LIBRARY_ROOTS", str(doc_root))
    monkeypatch.setattr(route, "_meta_dir", lambda: meta_dir)
    monkeypatch.setattr(route, "_text_dir", lambda: text_dir)

    client = TestClient(app)
    listed = client.get("/api/library/items").json()["items"]
    assert len(listed) == 1
    key = listed[0]["citekey"]

    # 人先补了作者（这一步会把元数据冻结成 manual）
    assert client.post("/api/library/meta", json={"citekey": key, "meta": {"authors": ["A Human"]}}).status_code == 200

    indexed = client.post("/api/library/index", json={"limit": 5}).json()
    assert [one["citekey"] for one in indexed["done"]] == [key], "补过元数据的那份也必须进队列"
    assert lib.text_state(text_dir, key).get("at"), "抽过之后要留 at 标记，否则下一轮会重抓"


def test_detail_lists_primary_and_assets_with_roles(tmp_path: Path, monkeypatch):
    """详情里的 `files`：主文件一条、附属资源一条，各带 `role`（契约里写明的形状）。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routers import library as route

    doc_root = tmp_path / "docs"
    (doc_root / "figs").mkdir(parents=True)
    (doc_root / "figs" / "fig-1.svg").write_text("<svg/>", encoding="utf-8")
    (doc_root / "Review.md").write_text("# Review\n\n![fig](figs/fig-1.svg)\n", encoding="utf-8")
    monkeypatch.setenv("QF_LIBRARY_ROOTS", str(doc_root))
    monkeypatch.setattr(route, "_meta_dir", lambda: tmp_path / "library")
    monkeypatch.setattr(route, "_text_dir", lambda: tmp_path / "library" / ".text")

    client = TestClient(app)
    key = client.get("/api/library/items").json()["items"][0]["citekey"]
    detail = client.get("/api/library/item", params={"citekey": key}).json()
    roles = [(Path(one["path"]).name, one["role"]) for one in detail["files"]]
    assert roles == [("Review.md", "primary"), ("fig-1.svg", "asset")]


def test_source_id_folds_symlink_spellings(tmp_path: Path):
    """同一个文件的不同写法（软链）要算同一个源 —— 否则会长出"同一文件两把键"。

    实测：真实库里有 7 组这样的重复（`dao2022flashattentionfastme` 与 `…-cuda`），
    成因就是按字符串比 `source`：软链写法匹配不上 → 重新推断 → 撞键 → 加后缀。
    """
    real = tmp_path / "Documents"
    (real / "cuda").mkdir(parents=True)
    doc = real / "cuda" / "x.pdf"
    doc.write_bytes(b"%PDF-1.4 fake")
    link = tmp_path / "reference"
    link.symlink_to(real)

    assert lib.source_id(link / "cuda" / "x.pdf") == lib.source_id(doc)
    assert lib.source_id(doc) == str(doc.resolve())
    assert lib.source_id("") == ""
    assert lib.source_id(None) == ""


def test_metadata_lookup_survives_a_symlinked_root(tmp_path: Path):
    """元数据按"解析后的路径"索引：换个根写法也找得到那份元数据。"""
    real = tmp_path / "Documents"
    real.mkdir()
    (real / "paper.pdf").write_bytes(b"%PDF-1.4 fake")
    link = tmp_path / "reference"
    link.symlink_to(real)
    meta_dir = tmp_path / "library"
    meta_dir.mkdir()

    lib.save_metadata(
        meta_dir,
        {"citekey": "someone2024paper", "title": "A Paper", "source": str(link / "paper.pdf")},
        origin="llm",
    )
    _by_key, by_source = lib.metadata_index(meta_dir)
    assert by_source.get(lib.source_id(real / "paper.pdf"), {}).get("citekey") == "someone2024paper"


def test_title_cleaning_keeps_leading_numbers():
    """标题收拾只去标记符号，**不许吃数字**。

    踩过：正则里带上 `^\\d+[.)]`（当有序列表编号），于是标准号 `1364.1 TM`
    被吃成了 `1 TM`。标题以数字开头太常见（标准号、`3D Graphics`），宁可不收拾。
    """
    assert lib.clean_title("# Layout polynomials") == "Layout polynomials"
    assert lib.clean_title("### —— 用生成函数") == "—— 用生成函数"
    assert lib.clean_title("  > 引用式标题  ") == "引用式标题"
    assert lib.clean_title("1364.1 TM") == "1364.1 TM"
    assert lib.clean_title("3D Graphics") == "3D Graphics"
    assert lib.clean_title("1. Introduction") == "1. Introduction"


# ------------------------------------------------------------------ 查看资料


def test_view_kind_covers_the_formats_the_user_asked_for():
    """pdf / docx / ppt / md 都有去处；老式二进制照实说看不了；html 按**源码**看。"""
    assert att.view_kind("规范.pdf") == "pdf"
    assert att.view_kind("手册.docx") == "docx"
    assert att.view_kind("讲义.pptx") == "pptx"
    assert att.view_kind("笔记.md") == "markdown"
    assert att.view_kind("说明.txt") == "text"
    assert att.view_kind("图.png") == "image"
    assert att.view_kind("老手册.doc") == "legacy"
    assert att.view_kind("老幻灯片.ppt") == "legacy"
    assert att.view_kind("页面.html") == "text", "本地 HTML 可能有脚本，不给它执行的机会"
    assert att.view_kind("包.zip") == "binary"
    assert att.mime_of("a.pdf") == "application/pdf"
    assert att.mime_of("a.zip") == "application/octet-stream"


def test_pptx_is_parsed_from_the_zip(tmp_path: Path):
    """pptx = zip + XML：自己解，页面按**数字**排序（slide2 要在 slide10 前）。"""
    import zipfile

    deck = tmp_path / "讲义.pptx"
    order = [10, 2, 1]
    with zipfile.ZipFile(deck, "w") as box:
        for number in order:
            body = "".join(
                f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>"
                for text in (f"第 {number} 页标题", f"第 {number} 页要点")
            )
            box.writestr(f"ppt/slides/slide{number}.xml", f"<p:sld><p:spTree>{body}</p:spTree></p:sld>")

    slides = att.slides_of(deck)
    assert [one["index"] for one in slides] == [1, 2, 3], "按数字排，不按字符串"
    assert [one["title"] for one in slides] == ["第 1 页标题", "第 2 页标题", "第 10 页标题"]
    assert slides[0]["lines"] == ["第 1 页要点"]
    assert "第 10 页要点" in att.extract(deck, "pptx")


def test_docx_goes_through_pandoc(tmp_path: Path):
    """docx 交给 pandoc：正文能抽出来，转 HTML 也带结构。

    pandoc 不在就当跳过 —— 但**先确认它在**，不然"没有 docx 支持"会被静默放过。
    """
    import shutil
    import subprocess

    if shutil.which("pandoc") is None:
        pytest.skip("这台机器上没有 pandoc（docx 的抽取与查看都靠它）")

    source = tmp_path / "src.md"
    source.write_text("# 第一章 卷积\n\n卷积是滑动加权求和。\n\n| 项 | 值 |\n|---|---|\n| 核 | 3x3 |\n", encoding="utf-8")
    deck = tmp_path / "真文档.docx"
    subprocess.run(["pandoc", "-t", "docx", "-o", str(deck), str(source)], check=True, timeout=60)

    text = att.extract(deck, "docx")
    assert "第一章 卷积" in text and "滑动加权求和" in text
    html = att.to_html(deck)
    assert "第一章 卷积" in html
    assert "<table" in html, "表格结构要留住"


def test_old_binary_formats_say_so_instead_of_guessing(tmp_path: Path):
    """`.doc` / `.ppt` 抽不出文字 —— 返回空串，让界面如实说，别凑一堆乱码。"""
    legacy = tmp_path / "老手册.doc"
    legacy.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    assert att.extract(legacy, "legacy") == ""


def test_view_and_file_endpoints(tmp_path: Path, monkeypatch):
    """走一遍真接口：md 的查看与原样取文件（含 Range）。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routers import library as route

    doc_root = tmp_path / "docs"
    doc_root.mkdir()
    (doc_root / "笔记.md").write_text("# 标题\n\n正文在这里。\n", encoding="utf-8")
    (doc_root / "封面.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    (doc_root / "笔记.md").write_text(
        "# 标题\n\n正文在这里。\n\n![封面](封面.png)\n", encoding="utf-8"
    )
    monkeypatch.setenv("QF_LIBRARY_ROOTS", str(doc_root))
    monkeypatch.setattr(route, "_meta_dir", lambda: tmp_path / "library")
    monkeypatch.setattr(route, "_text_dir", lambda: tmp_path / "library" / ".text")

    client = TestClient(app)
    key = [one for one in client.get("/api/library/items").json()["items"] if one["rel"] == "笔记.md"][0]["citekey"]

    view = client.get("/api/library/view", params={"citekey": key}).json()
    assert view["kind"] == "markdown"
    assert "正文在这里" in view["text"]
    assert view["raw"].endswith("index=-1")

    raw = client.get("/api/library/file", params={"citekey": key})
    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("text/markdown")
    assert "inline" in raw.headers.get("content-disposition", "")
    assert "正文在这里" in raw.text

    # 附属资源（第 0 个）：图片按自己的 MIME 出去
    asset = client.get("/api/library/file", params={"citekey": key, "index": 0})
    assert asset.status_code == 200
    assert asset.headers["content-type"] == "image/png"

    # Range：大 PDF 靠它跳页
    part = client.get("/api/library/file", params={"citekey": key}, headers={"Range": "bytes=0-9"})
    assert part.status_code == 206, "要支持 Range（不然几百 MB 的规范书翻不动）"

    assert client.get("/api/library/file", params={"citekey": key, "index": 9}).status_code == 404
    assert client.get("/api/library/file", params={"citekey": "没这个"}).status_code == 404


# ------------------------------------------------------------------ 落盘操作（真的动磁盘）


def test_mkdir_and_move_really_touch_the_disk(root: Path):
    """资料的组织树也是磁盘上的目录树：新建 = mkdir，移动 = move。"""
    assert lib.mkdir(root, str(root / "新目录")) == "新目录"
    assert (root / "新目录").is_dir()

    moved = lib.move(root, str(root / "cuda" / "2205.14135v2.pdf"), str(root / "新目录"))
    assert moved == "新目录/2205.14135v2.pdf"
    assert (root / "新目录" / "2205.14135v2.pdf").is_file()
    assert not (root / "cuda" / "2205.14135v2.pdf").exists()

    # 拖到自己本来就在的目录：算成功，别报错（这是常见误操作）
    same = lib.move(root, str(root / "新目录" / "2205.14135v2.pdf"), str(root / "新目录"))
    assert same == "新目录/2205.14135v2.pdf"


def test_mkdir_move_refuse_what_would_bite(root: Path):
    """会咬人的三件事：越界、同名覆盖、把目录挪进它自己里面。"""
    outside = root.parent / "别处"
    outside.mkdir()
    with pytest.raises(lib.LibraryError):
        lib.mkdir(root, str(outside / "偷建"))
    with pytest.raises(lib.LibraryError):
        lib.move(root, str(root / "cuda" / "notes.md"), str(outside))

    lib.mkdir(root, str(root / "已存在"))
    with pytest.raises(lib.LibraryError):
        lib.mkdir(root, str(root / "已存在"))

    (root / "已存在" / "Trefethen-Bau.pdf").write_bytes(b"x")
    with pytest.raises(lib.LibraryError):
        lib.move(root, str(root / "cuda" / "Trefethen-Bau.pdf"), str(root / "已存在"))

    (root / "已存在" / "里头").mkdir()
    with pytest.raises(lib.LibraryError):
        lib.move(root, str(root / "已存在"), str(root / "已存在" / "里头"))


def test_safe_name_and_unique_in(root: Path):
    """拖进来的文件名要净化，同名要编号 —— 都是"别动用户已有文件"这条线。"""
    assert lib.safe_name("论文.pdf") == "论文.pdf"
    assert lib.safe_name("../../etc/passwd") == "passwd"
    assert lib.safe_name("a/b/c.pdf") == "c.pdf"
    with pytest.raises(lib.LibraryError):
        lib.safe_name("..")

    folder = root / "新目录"
    folder.mkdir()
    assert lib.unique_in(folder, "x.pdf").name == "x.pdf"
    (folder / "x.pdf").write_bytes(b"x")
    assert lib.unique_in(folder, "x.pdf").name == "x 2.pdf"
    (folder / "x 2.pdf").write_bytes(b"x")
    assert lib.unique_in(folder, "x.pdf").name == "x 3.pdf"


def test_place_names_are_not_authors() -> None:
    """署名区块里的**地名**不是作者。

    这条是拿两条真实数据换的。论文那篇的首页署名区块按逗号切开之后是
    `Osaka` / `Japan Tokyo` / `Japan Tokyo`…… 一串地名，全写进了作者
    （用户的原话："作者怎么会是 Osaka 和 Tokyo？肯定你解析的时候搞错了！"）。

    判据是"**这个词都算地名**"：所以 `Ann Arbor` 挡得住，`Ann Smith` 不会被误杀。
    """
    assert not lib._looks_like_a_person("Osaka")
    assert not lib._looks_like_a_person("Japan")
    assert not lib._looks_like_a_person("Japan                          Tokyo")
    assert not lib._looks_like_a_person("New York")
    assert not lib._looks_like_a_person("Ann Arbor")
    assert not lib._looks_like_a_person("Tokyo, Japan")
    # 真人不能一起被挡掉（含地名词的姓名、单个名字都要放过）
    assert lib._looks_like_a_person("Ann Smith")
    assert lib._looks_like_a_person("Yoshiaki Fukazawa")
    assert lib._looks_like_a_person("Tri Dao")


def test_title_fragments_are_not_authors() -> None:
    """封面**副标题**被逗号切开之后不是作者。

    `INTRODUCING AMD CDNA™ 4 ARCHITECTURE … with Enhanced AI Capabilities,
    Advanced Precisions, High Efficiency` —— 逗号之后那三段被当成了三个作者。
    """
    title = (
        "INTRODUCING AMD CDNA™ 4 ARCHITECTURE Breakthrough AI and HPC Acceleration "
        "with Enhanced AI Capabilities, Advanced Precisions, High Efficiency"
    )
    assert not lib._looks_like_a_person("Capabilities", title=title)
    assert not lib._looks_like_a_person("Advanced Precisions", title=title)
    # 不在标题里的名字照样认（判据是"出现在标题里"，不是"标题像什么"）
    assert lib._looks_like_a_person("Kiyoshi Honda", title=title)


def test_infer_drops_place_names_from_a_real_signature_block(root: Path) -> None:
    """整条链路：一份首页长这样，抽出来的作者里不许有地名。

    光有单元测试不够 —— 真正写进元数据的那一步是"整行按逗号切"，
    所以这条从 `infer` 走一遍。
    """
    # `_item(...)` 是"从磁盘上真扫一遍再挑出那一条"，所以文件得真在
    (root / "swe").mkdir(parents=True, exist_ok=True)
    (root / "swe" / "bugpred.pdf").write_bytes(b"%PDF-1.4 fake")
    head = (
        "An Empirical Study on Predicting Software Development Bugs\n"
        "Using Dynamic Bayesian Networks\n"
        "Kiyoshi Honda, Hironori Washizaki, Yoshiaki Fukazawa\n"
        "Osaka, Japan                          Tokyo, Japan\n"
    )
    meta = lib.infer(_item(root, "swe/bugpred.pdf"), head_text=head)
    assert "Osaka" not in meta.get("authors", [])
    assert "Japan" not in " ".join(meta.get("authors", []))
    # 真作者要留住（判据只该挡掉地名，不该把整行都丢掉）
    assert "Kiyoshi Honda" in meta.get("authors", [])
