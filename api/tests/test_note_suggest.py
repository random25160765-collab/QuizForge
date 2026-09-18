"""模型建议标签与双链：**确定性的那一半必须有测试**（模型那半边打桩）。

这个功能的风险不在"模型答得不好"，而在两处会更糟的地方：
* 模型**编一个不存在的标题**当双链 → 必须按库里的索引核对，编的一律丢掉（还有计数）
* 复核时人会来回点「接受」→ 落盘必须**幂等**，不能每点一次就往正文里再堆一段
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import notelib  # noqa: E402
from pipeline import note_suggest as ns  # noqa: E402


@pytest.fixture()
def lib(tmp_path, monkeypatch) -> notelib.Library:
    monkeypatch.setenv("QF_DATA_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()
    notelib.reset_index()
    root = tmp_path / "notes" / "T"
    root.mkdir(parents=True)
    (root / "偏导数.md").write_text(
        "---\ntitle: 偏导数\ntags: [微积分]\n---\n\n偏导数与全微分都要求多元函数的极限存在。\n",
        encoding="utf-8",
    )
    (root / "全微分.md").write_text("---\ntitle: 全微分\n---\n\n全微分是线性主部。\n", encoding="utf-8")
    (root / "杂记.md").write_text("---\ntitle: 杂记\n---\n\n今天下雨。\n", encoding="utf-8")
    return notelib.library("T")


def test_candidates_put_notes_without_metadata_first(lib: notelib.Library):
    rows = ns.pick_candidates(lib, limit=3)
    assert [row["path"] for row in rows][0] != "偏导数.md"      # 它已经有标签了，排后面
    assert {"全微分.md", "杂记.md"} <= {row["path"] for row in rows}


def test_clean_links_drops_fabricated_targets(lib: notelib.Library):
    titles = {entry.title: entry.rel for entry in notelib.index(lib).entries() if entry.title}
    kept, dropped = ns.clean_links(
        [
            {"target": "全微分", "why": "前置"},
            {"target": "全微分", "why": "重复"},
            {"target": "微分流形", "why": "库里没有"},
            {"target": "[[偏导数]]", "why": "带括号"},
        ],
        titles=titles,
    )
    assert [item["target"] for item in kept] == ["全微分", "偏导数"]
    assert dropped == 1                                          # 编的那个被丢掉并计数


def test_clean_tags_normalizes_and_skips_known(lib: notelib.Library):
    assert ns.clean_tags(["#偏导数", "偏导数", "", "多元函数"], known={"偏导数"}) == ["多元函数"]


def test_apply_writes_tags_and_links_then_is_idempotent(lib: notelib.Library):
    first = ns.apply(
        lib,
        [{"path": "全微分.md", "tags": ["微分"], "links": [{"target": "偏导数", "why": "同一套里的另一面"}]}],
    )
    assert first["applied"][0]["tags"] == ["微分"]
    note = notelib.read_note(lib, "全微分.md")
    assert "微分" in note["tags"]
    assert "[[偏导数]]" in note["body"]
    assert "## 相关" in note["body"]

    # 再点一次「接受」：不该再写第二遍（人复核时会来回点）
    second = ns.apply(
        lib,
        [{"path": "全微分.md", "tags": ["微分"], "links": [{"target": "偏导数", "why": "同一套里的另一面"}]}],
    )
    assert second["applied"] == []
    assert second["skipped"][0]["reason"]
    assert notelib.read_note(lib, "全微分.md")["body"].count("[[偏导数]]") == 1


def test_apply_leaves_an_undo_point(lib: notelib.Library):
    """写之前要留快照 —— 与「文件恢复」同源，错了能退回来。"""
    ns.apply(lib, [{"path": "杂记.md", "tags": ["随手记"], "links": []}])
    versions = notelib.snapshots(lib, "杂记.md")
    assert versions, "写了内容却没有留快照"
    notelib.undo(lib, "杂记.md")
    assert "随手记" not in notelib.read_note(lib, "杂记.md")["tags"]
