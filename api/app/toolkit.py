"""把仓库根目录下的 ``tools/`` 挂到 import 路径上。

导入器必须复用**构建期那一套**解析器与校验器（`question_parser` /
`topics` / `check`），否则「文件里能过构建、导入却报错」这类不一致
迟早会出现。但这些模块是为脚本组织在 `tools/` 下的，不是可安装的包，
所以这里集中做一次路径注入，其它模块只从本模块取符号。

路径来源是配置项 ``QF_TOOLS_DIR``：仓库布局下默认 ``<root>/tools``，
容器里由 ``QF_TOOLS_DIR`` 指到 ``/app/tools``。
"""

from __future__ import annotations

import sys

from .config import get_settings

TOOLS_DIR = get_settings().tools_dir
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

# 路径注入必须在导入这些模块之前完成，所以 import 语句放在下面
from check import Diagnostic, check_file  # noqa: E402
from question_parser import (  # noqa: E402
    TYPE_LABELS,
    Question,
    QuestionParseError,
    iter_question_files,
    parse_question,
    question_to_dict,
)
from topics import load as load_topic_tree  # noqa: E402

__all__ = [
    "TYPE_LABELS",
    "TOOLS_DIR",
    "Diagnostic",
    "Question",
    "QuestionParseError",
    "check_file",
    "iter_question_files",
    "load_topic_tree",
    "parse_question",
    "question_to_dict",
]
