"""
Tests for the file reference (@) feature — Phase 5 (metadata) and Phase 6 (edge cases).

Test IDs
--------
1.  test_extract_single_ref              — single @path produces one attachment
2.  test_extract_multiple_refs           — two distinct @paths produce two attachments
3.  test_extract_deduplication           — duplicate @path yields only one attachment
4.  test_extract_missing_file_skipped    — @nonexistent path is silently skipped
5.  test_extract_unreadable_file_skipped — OSError on read → skipped gracefully
6.  test_extract_no_refs                 — message with no @ produces empty list
7.  test_extract_inline_ref              — @path embedded mid-sentence is found
8.  test_search_files_empty_query        — empty query returns up to 20 files
9.  test_search_files_substring_match    — partial filename substring match
10. test_search_files_limit              — result capped at 20 items
11. test_search_files_exclude_dirs       — .git / __pycache__ dirs are excluded
12. test_search_files_nonexistent_root   — gracefully returns empty list
13. test_extract_path_with_spaces        — @"path with spaces" not matched (no quotes)
14. test_extract_special_chars_in_name   — file with hyphen/underscore in path
15. test_extract_returns_relative_paths  — paths stored are relative (not absolute)
16. test_payload_files_key_present       — files key added to payload when refs found
17. test_payload_no_files_key_absent     — no files key when message has no valid refs
"""

import os
import sys
import types
import fnmatch
import re
from pathlib import Path
from unittest.mock import patch, mock_open, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub heavy optional dependencies so openparty_tui can be imported in CI
# ---------------------------------------------------------------------------


def _stub_module(name: str, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


# Override getattr on the metaclass so class-level attribute access works
class _AnyMeta(type):
    def __getattr__(cls, name):
        return _AnyClass


# A permissive base class that accepts any args/kwargs and allows attribute access
class _AnyClass(metaclass=_AnyMeta):
    def __init__(self, *a, **kw):
        pass

    def __init_subclass__(cls, **kw):
        pass

    def __class_getitem__(cls, item):
        return cls

    def __getattr__(self, name):
        return _AnyClass()


for _mod in ("aiohttp", "websockets"):
    if _mod not in sys.modules:
        _stub_module(_mod)

# Textual stubs — only what openparty_tui actually imports at module level
if "textual" not in sys.modules:
    textual = _stub_module("textual")
    textual_app = _stub_module("textual.app", App=_AnyClass, ComposeResult=_AnyClass)
    textual_binding = _stub_module("textual.binding", Binding=_AnyClass)
    textual_containers = _stub_module(
        "textual.containers", Horizontal=_AnyClass, Vertical=_AnyClass
    )
    textual_message = _stub_module("textual.message", Message=_AnyClass)
    textual_screen = _stub_module("textual.screen", ModalScreen=_AnyClass)
    textual_widgets = _stub_module(
        "textual.widgets",
        Input=_AnyClass,
        Label=_AnyClass,
        ListItem=_AnyClass,
        ListView=_AnyClass,
        RichLog=_AnyClass,
        Static=_AnyClass,
        TextArea=_AnyClass,
    )
    textual_events = _stub_module("textual.events", Key=_AnyClass)
    textual_events.__getattr__ = lambda name: _AnyClass  # type: ignore[attr-defined]
    textual_timer = _stub_module("textual.timer", Timer=_AnyClass)

if "rich" not in sys.modules:
    rich = _stub_module("rich")
    _stub_module("rich.markup", escape=lambda x: x)
    _stub_module("rich.style", Style=_AnyClass)
    _stub_module("rich.text", Text=_AnyClass)

# Now we can import the functions under test directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from openparty_tui import _extract_file_attachments, _search_files, FILE_CONTENT_LIMIT  # noqa: E402


# ===========================================================================
# Helpers
# ===========================================================================


def _write_tree(tmp_path: Path, tree: dict) -> None:
    """Recursively write a file tree from a dict {rel_path: content}."""
    for rel, content in tree.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


# ===========================================================================
# _extract_file_attachments — Phase 5 metadata tests
# ===========================================================================


class TestExtractFileAttachments:
    def test_extract_single_ref(self, tmp_path):
        (tmp_path / "hello.txt").write_text("world", encoding="utf-8")
        with patch("openparty_tui.os.getcwd", return_value=str(tmp_path)):
            result = _extract_file_attachments("check @hello.txt please")
        assert len(result) == 1
        assert result[0]["path"] == "hello.txt"
        assert result[0]["content"] == "world"

    def test_extract_multiple_refs(self, tmp_path):
        (tmp_path / "a.txt").write_text("aaa", encoding="utf-8")
        (tmp_path / "b.txt").write_text("bbb", encoding="utf-8")
        with patch("openparty_tui.os.getcwd", return_value=str(tmp_path)):
            result = _extract_file_attachments("@a.txt and @b.txt")
        paths = {r["path"] for r in result}
        assert paths == {"a.txt", "b.txt"}
        contents = {r["content"] for r in result}
        assert contents == {"aaa", "bbb"}

    def test_extract_deduplication(self, tmp_path):
        (tmp_path / "dup.txt").write_text("only once", encoding="utf-8")
        with patch("openparty_tui.os.getcwd", return_value=str(tmp_path)):
            result = _extract_file_attachments("see @dup.txt and also @dup.txt")
        assert len(result) == 1
        assert result[0]["path"] == "dup.txt"

    def test_extract_missing_file_skipped(self, tmp_path):
        with patch("openparty_tui.os.getcwd", return_value=str(tmp_path)):
            result = _extract_file_attachments("@does_not_exist.txt")
        assert result == []

    def test_extract_unreadable_file_skipped(self, tmp_path):
        target = tmp_path / "locked.txt"
        target.write_text("secret", encoding="utf-8")
        with patch("openparty_tui.os.getcwd", return_value=str(tmp_path)):
            with patch.object(Path, "read_text", side_effect=OSError("no perm")):
                result = _extract_file_attachments("@locked.txt")
        assert result == []

    def test_extract_no_refs(self, tmp_path):
        with patch("openparty_tui.os.getcwd", return_value=str(tmp_path)):
            result = _extract_file_attachments("hello world, no at-signs here")
        assert result == []

    def test_extract_inline_ref(self, tmp_path):
        (tmp_path / "inline.py").write_text("# code", encoding="utf-8")
        with patch("openparty_tui.os.getcwd", return_value=str(tmp_path)):
            result = _extract_file_attachments("look at @inline.py for details")
        assert len(result) == 1
        assert result[0]["path"] == "inline.py"

    def test_extract_special_chars_in_name(self, tmp_path):
        fname = "my-module_v2.py"
        (tmp_path / fname).write_text("v2", encoding="utf-8")
        with patch("openparty_tui.os.getcwd", return_value=str(tmp_path)):
            result = _extract_file_attachments(f"using @{fname}")
        assert len(result) == 1
        assert result[0]["path"] == fname

    def test_extract_returns_relative_paths(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "rel.txt").write_text("relative", encoding="utf-8")
        with patch("openparty_tui.os.getcwd", return_value=str(tmp_path)):
            result = _extract_file_attachments("@sub/rel.txt")
        assert len(result) == 1
        # path must be relative, not absolute
        assert not result[0]["path"].startswith("/")
        assert result[0]["path"] == "sub/rel.txt"

    def test_extract_path_with_spaces_not_matched(self, tmp_path):
        """@ regex stops at whitespace, so 'my file.txt' won't be matched as one path."""
        (tmp_path / "my file.txt").write_text("content", encoding="utf-8")
        with patch("openparty_tui.os.getcwd", return_value=str(tmp_path)):
            result = _extract_file_attachments("@my file.txt")
        # "my" alone won't resolve to a file
        assert result == []

    def test_extract_oversized_file_skipped(self, tmp_path):
        """Files larger than FILE_CONTENT_LIMIT bytes are silently skipped."""
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * (FILE_CONTENT_LIMIT + 1))
        with patch("openparty_tui.os.getcwd", return_value=str(tmp_path)):
            result = _extract_file_attachments("@big.bin")
        assert result == []

    def test_extract_exact_limit_file_included(self, tmp_path):
        """Files at exactly FILE_CONTENT_LIMIT bytes are included."""
        edge = tmp_path / "edge.txt"
        edge.write_bytes(b"a" * FILE_CONTENT_LIMIT)
        with patch("openparty_tui.os.getcwd", return_value=str(tmp_path)):
            result = _extract_file_attachments("@edge.txt")
        assert len(result) == 1
        assert result[0]["path"] == "edge.txt"

    def test_extract_oversized_skipped_small_included(self, tmp_path):
        """Mixed refs: oversized file skipped, small file included."""
        (tmp_path / "small.txt").write_text("ok", encoding="utf-8")
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * (FILE_CONTENT_LIMIT + 1))
        with patch("openparty_tui.os.getcwd", return_value=str(tmp_path)):
            result = _extract_file_attachments("@small.txt and @big.bin")
        assert len(result) == 1
        assert result[0]["path"] == "small.txt"


# ===========================================================================
# _search_files — Phase 2 / Phase 6 performance & edge-case tests
# ===========================================================================


class TestSearchFiles:
    def test_search_files_empty_query_returns_files(self, tmp_path):
        for i in range(5):
            (tmp_path / f"file{i}.txt").write_text("", encoding="utf-8")
        results = _search_files("", root=str(tmp_path))
        assert len(results) == 5
        for display, rel in results:
            assert display == rel  # display_name == relative path

    def test_search_files_substring_match(self, tmp_path):
        (tmp_path / "readme.md").write_text("", encoding="utf-8")
        (tmp_path / "setup.py").write_text("", encoding="utf-8")
        results = _search_files("read", root=str(tmp_path))
        names = [rel for _, rel in results]
        assert "readme.md" in names
        assert "setup.py" not in names

    def test_search_files_limit_20(self, tmp_path):
        for i in range(30):
            (tmp_path / f"f{i:03d}.txt").write_text("", encoding="utf-8")
        results = _search_files("", root=str(tmp_path))
        assert len(results) <= 20

    def test_search_files_exclude_git(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("", encoding="utf-8")
        (tmp_path / "normal.py").write_text("", encoding="utf-8")
        results = _search_files("", root=str(tmp_path))
        paths = [rel for _, rel in results]
        assert not any(".git" in p for p in paths)
        assert "normal.py" in paths

    def test_search_files_exclude_pycache(self, tmp_path):
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "mod.cpython-311.pyc").write_text("", encoding="utf-8")
        (tmp_path / "mod.py").write_text("", encoding="utf-8")
        results = _search_files("", root=str(tmp_path))
        paths = [rel for _, rel in results]
        assert not any("__pycache__" in p for p in paths)
        assert "mod.py" in paths

    def test_search_files_nonexistent_root(self, tmp_path):
        results = _search_files("", root=str(tmp_path / "nonexistent"))
        assert results == []

    def test_search_files_deep_nested(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "deep_file.txt").write_text("deep", encoding="utf-8")
        results = _search_files("deep_file", root=str(tmp_path))
        assert len(results) == 1
        rel = results[0][1]
        assert rel == str(Path("a/b/c/deep_file.txt"))

    def test_search_files_fnmatch_pattern(self, tmp_path):
        (tmp_path / "foo_bar.py").write_text("", encoding="utf-8")
        (tmp_path / "baz_qux.py").write_text("", encoding="utf-8")
        results = _search_files("foo", root=str(tmp_path))
        names = [rel for _, rel in results]
        assert "foo_bar.py" in names
        assert "baz_qux.py" not in names

    def test_search_files_large_repo_perf(self, tmp_path):
        """1000 files should return quickly (≤ 20 results, no crash)."""
        bulk = tmp_path / "bulk"
        bulk.mkdir()
        for i in range(1000):
            (bulk / f"file_{i:04d}.txt").write_text("", encoding="utf-8")
        import time

        start = time.monotonic()
        results = _search_files("", root=str(tmp_path))
        elapsed = time.monotonic() - start
        assert len(results) <= 20
        assert elapsed < 5.0, f"Search took too long: {elapsed:.2f}s"


# ===========================================================================
# Payload integration — ensure ws.send payload structure is correct
# ===========================================================================


class TestPayloadStructure:
    """Unit-level checks that payload["files"] is set / absent correctly."""

    def test_payload_files_key_present(self, tmp_path):
        (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
        with patch("openparty_tui.os.getcwd", return_value=str(tmp_path)):
            files = _extract_file_attachments("see @note.txt")
        # Simulate what _handle_send does
        payload: dict = {"type": "message", "content": "see @note.txt"}
        if files:
            payload["files"] = files
        assert "files" in payload
        assert payload["files"][0]["path"] == "note.txt"
        assert payload["files"][0]["content"] == "hello"

    def test_payload_no_files_key_absent(self, tmp_path):
        with patch("openparty_tui.os.getcwd", return_value=str(tmp_path)):
            files = _extract_file_attachments("no references here")
        payload: dict = {"type": "message", "content": "no references here"}
        if files:
            payload["files"] = files
        assert "files" not in payload
