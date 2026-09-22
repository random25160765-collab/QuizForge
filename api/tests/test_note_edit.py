"""`edit_note` —— 改一篇笔记的**某一处**（模型侧那个编辑工具）。

这一批盯的不是"能不能改"，而是**"会不会改错地方"**。区别在于：改不动只是没帮上忙，
改错地方是**动了用户自己的文件**，而且很可能是静默的。所以三条必须拒绝：

* 给一段正文里根本没有的原文 —— 拒绝，**不做模糊匹配**（"差不多就行"就意味着
  改到别处去）；
* 给一段出现多次的原文 —— 拒绝并说明它在哪几处，**不默认改第一处**
  （用户说的是小 Alice，默认改第一处就改成了小 Bob）；
* `old` 与 `new` 一样 / `new` 忘了给 —— 拒绝（`new` 给空串才是"删掉这一段"，
  **省了不给**与"想删"长得太像，不能靠猜）。

还有一条同样重要：改完之后**只有那一段变了** —— YAML 头与其余正文一个字都不许动，
而且能撤回（走的是笔记页保存的同一条路 `notelib.write_body`）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import notelib, tools

HEAD = "---\ntitle: 测试笔记\ntags: [a, b]\n---\n\n"
TAIL = "\n\n## 末尾一节\n\n这一段谁都不该碰。\n"


@pytest.fixture()
def lib(tmp_path, monkeypatch):  # noqa: ANN001
    """一个**临时**笔记库。

    非做不可：`notes_root()` 默认是 `data/notes/` —— 也就是**用户真实那些笔记**。
    测试里忘了 monkeypatch，一次"编辑成功"就动了他的文件（`extra_roots` 也一起
    掐掉，免得真实库混进来影响库名解析）。
    """
    monkeypatch.setattr(notelib, "notes_root", lambda: tmp_path / "notes")
    monkeypatch.setattr(notelib, "extra_roots", lambda: [])
    root = tmp_path / "notes" / "T"
    root.mkdir(parents=True)
    return root


def _make(lib, body: str) -> Path:  # noqa: ANN001
    path = lib / "n.md"
    path.write_text(HEAD + body + TAIL, encoding="utf-8")
    return path


def _edit(**args) -> dict:
    args.setdefault("lib", "T")
    args.setdefault("path", "n.md")
    return tools.edit_note(None, None, args)


# ---------------------------------------------------------------- 改对了


def test_replaces_only_that_span(lib) -> None:  # noqa: ANN001
    """改一处，其余**逐字不动** —— 头、正文、末尾那一节都不许被顺手重排。"""
    before = HEAD + "中间这句要改。\n\n后面还有别的。" + TAIL
    path = _make(lib, "中间这句要改。\n\n后面还有别的。")
    assert path.read_text(encoding="utf-8") == before

    result = _edit(old="中间这句要改。", new="中间这句改过了。")

    assert result["ok"] is True
    assert result["replaced"] == 1
    after = path.read_text(encoding="utf-8")
    assert after == before.replace("中间这句要改。", "中间这句改过了。"), "只该动那一处"
    assert after.startswith("---\ntitle: 测试笔记\ntags: [a, b]\n---\n"), "YAML 头要原样"


def test_result_reads_back_the_changed_span(lib) -> None:  # noqa: ANN001
    """结果里要**把那一段念回来** —— 模型据此自查"改的是不是我想改的地方"。

    字数也要报：改了多少字是**能对账**的信息（这里故意让新句更长，
    断言差值正好等于改动量 —— 免得"报了字数但其实和改动无关"这种假信号）。
    """
    _make(lib, "开头。\n\n这里是被改的句子。\n\n结尾。")
    old, new = "这里是被改的句子。", "这里换成了一句更长的话。"
    result = _edit(old=old, new=new)
    assert new in result["after"]
    assert result["chars"]["after"] - result["chars"]["before"] == len(new) - len(old)


def test_empty_new_deletes_the_span(lib) -> None:  # noqa: ANN001
    """`new` 给空串 = **删掉**这一段（不是"什么也没做"）。"""
    path = _make(lib, "留下的。\n\n删掉这一句。\n\n也留下的。")
    result = _edit(old="删掉这一句。\n\n", new="")
    assert result["ok"] is True
    text = path.read_text(encoding="utf-8")
    assert "删掉这一句" not in text
    assert "留下的。" in text and "也留下的。" in text


def test_all_replaces_every_occurrence(lib) -> None:  # noqa: ANN001
    """`all=true` 才全改 —— 这是**明确要求**，不是默认行为。"""
    path = _make(lib, "同一句。\n\n中间。\n\n同一句。")
    result = _edit(old="同一句。", new="都改掉了。", all=True)
    assert result["replaced"] == 2
    assert path.read_text(encoding="utf-8").count("同一句。") == 0


# ---------------------------------------------------------------- 该拒绝的


def test_refuses_when_old_is_missing(lib) -> None:  # noqa: ANN001
    """找不到就**拒绝**，并说清往哪儿找（逐字复制 / 长笔记用 startChar 读回来）。"""
    path = _make(lib, "正文里只有这句话。")
    before = path.read_text(encoding="utf-8")
    result = _edit(old="这句话正文里没有", new="x")
    assert "error" in result
    assert "逐字" in result["error"] and "read_note" in result["error"]
    assert path.read_text(encoding="utf-8") == before, "拒绝的路径一个字节都不该写"


def test_refuses_ambiguous_and_shows_the_places(lib) -> None:  # noqa: ANN001
    """出现多次就拒绝，**并把那几处念给它看** —— 这比"请给得更具体"可执行。"""
    path = _make(lib, "小张。\n\n中间隔开。\n\n小张。")
    before = path.read_text(encoding="utf-8")
    result = _edit(old="小张。", new="小李。")
    assert "error" in result
    assert "2 次" in result["error"]
    assert len(result["occurrences"]) == 2, "要列出每一处，而不是只说'有多处'"
    assert all("context" in one for one in result["occurrences"])
    assert path.read_text(encoding="utf-8") == before, "拒绝时不许写"


@pytest.mark.parametrize(
    "args, keyword",
    [
        ({"old": "a", "new": "a"}, "一模一样"),          # 等于没改
        ({"old": "a"}, "得给 new"),                       # 忘了给（而不是想删）
        ({"old": ""}, "得给 old"),                        # 空原文会把整篇撑爆
        ({"old": "a", "new": 3}, "得是字符串"),
        ({"new": "a"}, "得给 old"),
    ],
)
def test_refuses_bad_input(lib, args, keyword: str) -> None:  # noqa: ANN001
    """坏输入一律**拒绝并说清**，不能"尽力而为" —— 猜的代价是改坏用户的笔记。"""
    _make(lib, "正文 a。")
    result = _edit(**args)
    assert "error" in result
    assert keyword in result["error"]


def test_refuses_without_path(lib) -> None:  # noqa: ANN001
    result = tools.edit_note(None, None, {"lib": "T", "old": "a", "new": "b"})
    assert "error" in result and "path" in result["error"]


def test_refuses_unknown_library(lib) -> None:  # noqa: ANN001
    result = tools.edit_note(None, None, {"lib": "不存在的库", "path": "n.md", "old": "a", "new": "b"})
    assert "error" in result


# ---------------------------------------------------------------- 撤回与库解析


def test_edit_is_undoable(lib) -> None:  # noqa: ANN001
    """改前留快照、能撤回（与笔记页那个撤销**同一条路**）。

    只撤**一步**（不是一路退回开头）—— 这里把这条语义也钉住：写过两次之后撤一次，
    该回到"第一次改完"的样子。
    """
    path = _make(lib, "初版正文。")
    first = path.read_text(encoding="utf-8")
    _edit(old="初版正文。", new="二版正文。")
    _edit(old="二版正文。", new="三版正文。")
    assert "三版正文。" in path.read_text(encoding="utf-8")

    notelib.undo(notelib.library("T"), "n.md")

    assert "二版正文。" in path.read_text(encoding="utf-8"), "撤一步 = 回到上一次改动前"
    assert "三版正文。" not in path.read_text(encoding="utf-8")
    assert first != path.read_text(encoding="utf-8"), "不该一路退回开头"


def test_lib_can_be_omitted(lib) -> None:  # noqa: ANN001
    """`lib` 可省 —— 省了取第一个。

    这条是**回归**：工具说明里一直写着"可省"，但 `read_note` 走的是
    `notelib.library("")`，而它会抛"没有这个库"，于是**不传就报错**。
    三个工具现在共用同一个解析函数，这里把"可省"钉成真的。
    """
    path = _make(lib, "可省库名也要能改。")
    assert tools.edit_note(None, None, {"path": "n.md", "old": "可省库名也要能改。", "new": "改了。"})["ok"]
    assert not tools.read_note(None, None, {"path": "n.md"}).get("error")
    assert "改了。" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 注册表


def test_registry_shapes() -> None:
    """挂载表里的位置：写在 `notes` 组、要 `write` 档，且只读档里必须没有它。"""
    spec = tools.REGISTRY["edit_note"]
    assert spec["group"] == "notes"
    assert spec["access"] == "write"
    assert set(spec["parameters"]["required"]) == {"path", "old", "new"}

    read_only = [one["function"]["name"] for one in tools.specs(set(tools.ALL_GROUPS), ("read",))]
    assert "edit_note" not in read_only, "只读档不该看见它"
    assert "write_note" not in read_only
