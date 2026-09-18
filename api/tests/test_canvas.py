"""画布（JSON Canvas）的读写。

这些用例盯的是**会把文件写坏或把界面弄崩**的地方，而不是"功能有没有"：

* 读：真实语料里既有 2 字节的 `{}`，也可能有写坏一半的 JSON —— 一律降级成空画布，不抛
* 写：删卡片**必须连带删边**（留着就是指向空气的边）；边两头必须真的存在；
  连到自己要挡掉；已经在的边重复连当成功（人常连两次）
* 未知字段**一个都不能丢**（Obsidian 将来加的字段，不该被我们吃掉）
* 一批改动只留**一个版本**（逐个存会把轮转冲干净、"撤销"随即失效）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import canvas, notelib  # noqa: E402


@pytest.fixture()
def lib(tmp_path, monkeypatch) -> notelib.Library:
    monkeypatch.setenv("QF_DATA_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()
    notelib.reset_index()
    root = tmp_path / "notes" / "T"
    root.mkdir(parents=True)
    return notelib.library("T")


def _write(lib: notelib.Library, rel: str, payload) -> None:
    path = lib.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    notelib.reset_index()


# ------------------------------------------------------------------ 读


def test_read_tolerates_empty_and_broken_json(lib: notelib.Library):
    """实测 `Untitled.canvas` 就是 2 字节的 `{}`；写坏的那种也得能打开。"""
    _write(lib, "空.canvas", "{}")
    got = canvas.read(lib, "空.canvas")
    assert got["nodes"] == [] and got["edges"] == [] and got["broken"] == ""

    _write(lib, "坏.canvas", '{"nodes": [{"id": "a", ')
    broken = canvas.read(lib, "坏.canvas")
    assert broken["nodes"] == [] and "JSON 坏了" in broken["broken"]


def test_read_drops_nodes_without_id_and_edges_to_nowhere(lib: notelib.Library):
    _write(
        lib,
        "乱.canvas",
        {
            "nodes": [
                {"id": "a", "type": "text", "x": 0, "y": 0, "width": 100, "height": 50, "text": "甲"},
                {"type": "text", "text": "没有 id"},
                {"id": "a", "type": "text", "text": "重复 id"},
            ],
            "edges": [
                {"id": "e1", "fromNode": "a", "toNode": "不存在"},
                {"id": "e2", "fromNode": "a", "toNode": "a"},
            ],
        },
    )
    got = canvas.read(lib, "乱.canvas")
    assert [node["id"] for node in got["nodes"]] == ["a"]
    assert got["edges"] == []


def test_read_keeps_unknown_fields(lib: notelib.Library):
    """Obsidian 将来加的字段不该被我们吃掉 —— 读一遍再写回，多余字段还在。"""
    _write(
        lib,
        "扩.canvas",
        {
            "nodes": [
                {
                    "id": "a",
                    "type": "text",
                    "x": 0,
                    "y": 0,
                    "width": 100,
                    "height": 50,
                    "text": "甲",
                    "未来的字段": {"深": [1, 2]},
                }
            ],
            "edges": [],
            "画布级的额外字段": 1,
        },
    )
    canvas.apply(lib, "扩.canvas", [{"op": "update_node", "id": "a", "x": 40}])
    saved = json.loads((lib.root / "扩.canvas").read_text(encoding="utf-8"))
    assert saved["nodes"][0]["未来的字段"] == {"深": [1, 2]}
    assert saved["nodes"][0]["x"] == 40


def test_read_clamps_nonsense_numbers(lib: notelib.Library):
    _write(
        lib,
        "怪.canvas",
        {
            "nodes": [
                {"id": "a", "type": "text", "x": "不是数", "y": 1e308, "width": -5, "height": 0, "text": "甲"},
            ],
            "edges": [],
        },
    )
    node = canvas.read(lib, "怪.canvas")["nodes"][0]
    assert node["x"] == 0                       # 不是数 → 0
    assert abs(node["y"]) <= canvas.COORD_LIMIT  # 天文数字 → 拉回范围
    assert node["width"] >= canvas.MIN_SIZE[0]
    assert node["height"] >= canvas.MIN_SIZE[1]


# ------------------------------------------------------------------ 改


def test_delete_node_takes_its_edges_with_it(lib: notelib.Library):
    """删卡片必须连带删边 —— 留着就是指向空气的边，渲染时必然炸。"""
    _write(
        lib,
        "连.canvas",
        {
            "nodes": [
                {"id": "a", "type": "text", "x": 0, "y": 0, "width": 100, "height": 50, "text": "甲"},
                {"id": "b", "type": "text", "x": 200, "y": 0, "width": 100, "height": 50, "text": "乙"},
                {"id": "c", "type": "text", "x": 400, "y": 0, "width": 100, "height": 50, "text": "丙"},
            ],
            "edges": [
                {"id": "e1", "fromNode": "a", "toNode": "b"},
                {"id": "e2", "fromNode": "b", "toNode": "c"},
            ],
        },
    )
    result = canvas.apply(lib, "连.canvas", [{"op": "delete_node", "id": "b"}])
    kept = result["canvas"]
    assert [node["id"] for node in kept["nodes"]] == ["a", "c"]
    assert kept["edges"] == []                  # 两条边都挂在 b 上


def test_add_edge_validates_both_ends(lib: notelib.Library):
    _write(lib, "甲.canvas", {"nodes": [{"id": "a", "type": "text", "x": 0, "y": 0, "text": "甲"}], "edges": []})
    with pytest.raises(canvas.CanvasError):
        canvas.apply(lib, "甲.canvas", [{"op": "add_edge", "fromNode": "a", "toNode": "没有这张"}])
    with pytest.raises(canvas.CanvasError):
        canvas.apply(lib, "甲.canvas", [{"op": "add_edge", "fromNode": "a", "toNode": "a"}])
    # 同一对节点再连一次：当成功（人常连两次），不报错、也不出现两条
    canvas.apply(lib, "甲.canvas", [{"op": "add_node", "type": "text", "text": "乙"}])
    other = canvas.read(lib, "甲.canvas")["nodes"][1]["id"]
    first = canvas.apply(lib, "甲.canvas", [{"op": "add_edge", "fromNode": "a", "toNode": other}])
    again = canvas.apply(lib, "甲.canvas", [{"op": "add_edge", "fromNode": "a", "toNode": other}])
    assert len(first["canvas"]["edges"]) == 1
    assert len(again["canvas"]["edges"]) == 1


def test_add_node_places_it_clear_of_others(lib: notelib.Library):
    """新卡片不该叠在别人的头上 —— 用户第一件事要是拖开它，那就是没做好。"""
    _write(lib, "摆.canvas", {"nodes": [{"id": "a", "type": "text", "x": 0, "y": 0, "width": 250, "height": 60, "text": "甲"}], "edges": []})
    result = canvas.apply(lib, "摆.canvas", [{"op": "add_node", "type": "text"}])
    fresh = [node for node in result["canvas"]["nodes"] if node["id"] != "a"][0]
    clash = (
        fresh["x"] < 0 + 250 + 20
        and 0 < fresh["x"] + fresh["width"] + 20
        and fresh["y"] < 0 + 60 + 20
        and 0 < fresh["y"] + fresh["height"] + 20
    )
    assert not clash, f"新卡片压在旧卡片上：{fresh}"


def test_batch_ops_leave_exactly_one_version(lib: notelib.Library):
    """一批改动只留一个版本 —— 逐个存会把快照轮转冲干净，"撤销"随即失效。"""
    _write(lib, "批.canvas", {"nodes": [{"id": "a", "type": "text", "x": 0, "y": 0, "text": "甲"}], "edges": []})
    # 第一次写会补记"改动前"那一版（所以 +2），之后每批只 +1 —— 这才是"能退回改动前"的来源
    canvas.apply(lib, "批.canvas", [{"op": "update_node", "id": "a", "x": 10}])
    baseline = notelib.history_state(lib, "批.canvas")["versions"]

    ops = [{"op": "update_node", "id": "a", "x": step * 10} for step in range(2, 32)]
    canvas.apply(lib, "批.canvas", ops)
    after = notelib.history_state(lib, "批.canvas")["versions"]
    assert after == baseline + 1, f"30 次位移留了 {after - baseline} 个版本（应当只 1 个）"

    # 再下一批同理：批量大小与版本数无关
    canvas.apply(lib, "批.canvas", [{"op": "update_node", "id": "a", "x": 999}])
    assert notelib.history_state(lib, "批.canvas")["versions"] == after + 1
    assert notelib.read_note(lib, "批.canvas")["can_undo"] is True


def test_update_can_clear_a_field_and_delete_needs_the_edge_to_exist(lib: notelib.Library):
    _write(lib, "清.canvas", {"nodes": [{"id": "a", "type": "text", "x": 0, "y": 0, "text": "甲", "color": "4"}], "edges": []})
    result = canvas.apply(lib, "清.canvas", [{"op": "update_node", "id": "a", "color": ""}])
    assert "color" not in result["canvas"]["nodes"][0]
    with pytest.raises(canvas.CanvasError):
        canvas.apply(lib, "清.canvas", [{"op": "delete_edge", "id": "没有这条"}])
    with pytest.raises(canvas.CanvasError):
        canvas.apply(lib, "清.canvas", [])
    with pytest.raises(canvas.CanvasError):
        canvas.apply(lib, "清.canvas", [{"op": "不认识的改动"}])


def test_bbox_covers_negative_coordinates(lib: notelib.Library):
    """真实画布里有 x=-17640 这种坐标，包围盒得算对，否则"适应窗口"会把内容甩出视野。"""
    _write(
        lib,
        "远.canvas",
        {
            "nodes": [
                {"id": "a", "type": "text", "x": -17640, "y": -10979, "width": 1120, "height": 2628, "text": "甲"},
                {"id": "b", "type": "text", "x": -15360, "y": -10988, "width": 1062, "height": 2648, "text": "乙"},
            ],
            "edges": [],
        },
    )
    box = canvas.bbox(canvas.read(lib, "远.canvas"))
    left = -17640
    right = -15360 + 1062          # 两块卡片各自右缘取更靠右的那个
    top = -10988
    bottom = max(-10979 + 2628, -10988 + 2648)
    assert (box["x"], box["y"], box["width"], box["height"]) == (left, top, right - left, bottom - top)


def test_file_node_reaches_the_link_graph(lib: notelib.Library):
    """画布上指向某篇笔记的卡片，要算进"谁引用了它" —— 否则最容易被漏掉的那种引用就漏了。"""
    (lib.root / "被引.md").write_text("---\ntitle: 被引\n---\n\n正文\n", encoding="utf-8")
    _write(
        lib,
        "引.canvas",
        {"nodes": [{"id": "a", "type": "file", "x": 0, "y": 0, "width": 400, "height": 240, "file": "被引.md"}], "edges": []},
    )
    entry = [item for item in notelib.index(lib).entries() if item.rel == "引.canvas"][0]
    assert any(str(ref.target).endswith("被引.md") or ref.target == "被引" for ref in entry.refs)
    backlinks = notelib.index(lib).backlinks("被引.md")
    assert any(item["path"] == "引.canvas" for item in backlinks)
