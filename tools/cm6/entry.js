/* CodeMirror 6 的打包入口：把要用的那几件挂到 `QF.cm6` 上。
 *
 * 为什么要有这个入口：仓库的前端走全局 `QF` 命名空间 + 经典 <script>（没有打包链）。
 * CM6 是 ESM-only 的多个包，必须**打成一份 IIFE** 才能这么加载 —— 这里就是那份产物
 * 的入口。构建脚本见 `tools/build_cm6.mjs`（只在升级内核时跑，日常构建不需要 node）。
 */
import { EditorState, RangeSetBuilder } from '@codemirror/state';
import { EditorView, keymap, drawSelection, highlightActiveLine, lineNumbers, highlightSpecialChars, placeholder, Decoration, ViewPlugin, WidgetType } from '@codemirror/view';
import { defaultKeymap, history, historyKeymap, indentWithTab, undo, redo } from '@codemirror/commands';
import { markdown } from '@codemirror/lang-markdown';
import { syntaxHighlighting, HighlightStyle, defaultHighlightStyle, bracketMatching, indentOnInput, foldGutter, foldKeymap } from '@codemirror/language';
import { searchKeymap, highlightSelectionMatches, search } from '@codemirror/search';
import { autocompletion, completionKeymap, closeBrackets, closeBracketsKeymap } from '@codemirror/autocomplete';
import { tags as t } from '@lezer/highlight';

const api = {
  EditorState, EditorView, keymap, drawSelection, highlightActiveLine, lineNumbers,
  Decoration, ViewPlugin, WidgetType, RangeSetBuilder,
  highlightSpecialChars, placeholder, history, defaultKeymap, historyKeymap,
  indentWithTab, undo, redo, markdown, syntaxHighlighting, HighlightStyle,
  defaultHighlightStyle, bracketMatching, indentOnInput, foldGutter, foldKeymap,
  searchKeymap, highlightSelectionMatches, search, autocompletion, completionKeymap,
  closeBrackets, closeBracketsKeymap,
  tags: t,          // `@lezer/highlight` 的 tags（导入时改了名，这里要还回去）
};

window.QF = window.QF || {};
window.QF.cm6 = api;
window.QF.cm6.ready = true;
