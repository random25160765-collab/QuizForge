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


def test_every_group_has_a_prompt_a_label_and_its_tools():
    """每个工具组都得配齐三样，而且**工具清单要和注册表对得上**。

    `tools.GROUPS` 才是唯一出处，而这三份是手写的 —— 这正是会漂的地方：
    加一组忘了写提示词，模型就是"图标亮着、但它不知道有这回事"；
    清单里抄错或抄漏一个工具名，提示词会念出一个不存在的名字
    （模型照着去调，然后被 `call()` 拒掉，用户看到的是一次莫名其妙的失败）。
    """
    from app import tools
    from app.routers.chat import GROUP_LABELS, GROUP_PROMPTS, GROUP_TOOLS

    for key, _label, _hint in tools.GROUPS:
        assert key in GROUP_PROMPTS, "这一组没有提示词片段：" + key
        assert key in GROUP_LABELS, "这一组没有中文名：" + key
        assert key in GROUP_TOOLS, "这一组没有工具清单：" + key
        actual = {name for name in tools.REGISTRY if tools.group_of(name) == key}
        assert set(GROUP_TOOLS[key]) == actual, key + " 的工具清单和注册表对不上"

    # 反向也要对：提示词里不许出现注册表里没有的组（删掉一组时最容易漏这一头）
    assert set(GROUP_PROMPTS) == set(tools.GROUP_LABELS)


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
