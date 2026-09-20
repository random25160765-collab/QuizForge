"""提示词按"这一版挂了哪几组工具"拼。

来由是一个真实观察：极简模式下模型还在说"我看看你的掌握度"，还会猜
"你最近在看 NOC / circular buffer 吧" —— 因为提示词是一个常量，里面既命令它调
被拿走的工具，又交代了应用领域。用户原话："它根本就不应该知道我在干什么。"
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.routers.chat import GROUP_PROMPTS, MINIMAL_PROMPT, build_prompt  # noqa: E402


def test_minimal_knows_nothing():
    """极简：没有工具、不知道领域、不提任何工具名。"""
    text = build_prompt(set())
    # 极简 = MINIMAL_PROMPT + 无条件那句"当前能力"权威声明（见 capability_line）
    assert text.startswith(MINIMAL_PROMPT)
    assert "你没有任何工具" in text
    for word in ("加速器", "tt-metal", "Tenstorrent", "考研"):
        assert word not in text, "极简不该交代应用领域：" + word
    for name in ("search_knowledge", "push_question", "run_python", "search_notes",
                 "grade_problem", "get_mastery"):
        assert name not in text, "极简不该出现工具名：" + name
    # 也不能出现"工具/检索/收口"这套话术 —— 那等于告诉它"你是有工具的"
    for phrase in ("最后一轮工具", "查到够", "调工具", "工具返回"):
        assert phrase not in text, "极简不该提工具纪律：" + phrase


def test_only_mounted_fragments_enter():
    """只挂一组时，别组的片段一个字都不能进。"""
    for key in GROUP_PROMPTS:
        text = build_prompt({key})
        assert GROUP_PROMPTS[key] in text, key
        for other, frag in GROUP_PROMPTS.items():
            if other != key:
                assert frag not in text, "%s 混进了 %s" % (other, key)


def test_off_groups_are_told_off():
    """没挂的组要明确说"你没有这个"，否则模型会提议去查。"""
    text = build_prompt({"notes"})
    assert "没有挂" in text
    assert "题库与进度" in text


def test_full_mounts_has_no_off_note():
    text = build_prompt(set(GROUP_PROMPTS))
    assert "没有挂" not in text
    assert "加速器" in text            # 挂了工具才交代领域（它要理解材料）


def test_mode_change_up_and_down():
    """一个对话内换模式：**两个方向都要明说**。

    用户实测过两次：切到"查询"它说没有工具（历史惯性），切回"极简"它又继续
    报上一轮的能力。原因有两层，都在这里钉住：
      * `Message` 上没有 created_at，按它排序会静默失败 → 拿不到"上一条的模式"；
      * 极简分支原先**提前 return**，根本不调用 mode_notice。
    """
    from app import tools
    from app.routers.chat import build_prompt

    everything = list(tools.ALL_GROUPS)
    query = ["notes", "library", "graph"]

    up = build_prompt(query, [], [])
    assert "这一轮的变更" in up and "作废" in up, "极简 → 查询 必须明说"

    down = build_prompt([], everything, [])
    assert "这一轮的变更" in down, "学习 → 极简 必须明说"
    assert "从现在起你没有任何工具" in down
    assert "不要再调用任何工具" in down

    same = build_prompt([], [], [])
    assert "这一轮的变更" not in same, "没变就别啰嗦"
