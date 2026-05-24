
from __future__ import annotations

import time
from pathlib import Path

import pytest

from ctf import set_debug
from ctf.burp.wordlist import WordlistLoader

set_debug()

# ──────────────────────────────────────────────────────────────────────────────
# 通用 fixture
# ──────────────────────────────────────────────────────────────────────────────

ROCKYOU = Path("/home/zz/Documents/passworld/rockyou.txt")
pytestmark = pytest.mark.skipif(
    not ROCKYOU.exists(), reason="rockyou.txt 不存在，跳过集成测试"
)


def make_wordlist(tmp_path: Path, lines: list[str], name: str = "words.txt") -> Path:
    """在临时目录创建一个词表文件，返回其路径"""
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def loader(
    path: Path,
    tmp_path: Path,
    *,
    tag: str | None = None,
    continue_: bool = False,
    rollback: int = 1,
    insert: list[str] | None = None,
    checkpoint_interval: int = 1,   # 测试中每步立即落盘
    strip: bool = True,
) -> WordlistLoader:
    """快捷构造，将 cache_dir 指向临时目录，避免污染用户缓存"""
    return WordlistLoader(
        path,
        tag=tag,
        continue_=continue_,
        rollback=rollback,
        insert=insert,
        cache_dir=tmp_path / "cache",
        checkpoint_interval=checkpoint_interval,
        strip=strip,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 1. 基础迭代
# ──────────────────────────────────────────────────────────────────────────────

class TestBasicIteration:
    def test_read_all_words(self, tmp_path):
        words = ["alpha", "beta", "gamma", "delta"]
        p = make_wordlist(tmp_path, words)
        with loader(p, tmp_path) as wl:
            result = list(wl)
        assert result == words

    def test_empty_lines_are_skipped(self, tmp_path):
        p = tmp_path / "w.txt"
        p.write_text("foo\n\nbar\n\n\nbaz\n", encoding="utf-8")
        with loader(p, tmp_path) as wl:
            assert list(wl) == ["foo", "bar", "baz"]

    def test_strip_default(self, tmp_path):
        p = tmp_path / "w.txt"
        p.write_text("  hello  \n  world  \n", encoding="utf-8")
        with loader(p, tmp_path, strip=True) as wl:
            assert list(wl) == ["hello", "world"]

    def test_no_strip(self, tmp_path):
        p = tmp_path / "w.txt"
        p.write_text("  hello  \n  world  \n", encoding="utf-8")
        with loader(p, tmp_path, strip=False) as wl:
            result = list(wl)
        # strip=False 只去除行尾换行符，保留内部空格
        assert result == ["  hello  ", "  world  "]

    def test_next_word_returns_none_at_eof(self, tmp_path):
        p = make_wordlist(tmp_path, ["only"])
        with loader(p, tmp_path) as wl:
            assert wl.next_word() == "only"
            assert wl.next_word() is None
            assert wl.next_word() is None  # 幂等

    def test_stopiteration_from_dunder_next(self, tmp_path):
        p = make_wordlist(tmp_path, ["x"])
        with loader(p, tmp_path) as wl:
            assert next(wl) == "x"
            with pytest.raises(StopIteration):
                next(wl)

    def test_file_not_found(self, tmp_path):
        with pytest.raises((FileNotFoundError, RuntimeError)):
            loader(tmp_path / "nonexistent.txt", tmp_path)


# ──────────────────────────────────────────────────────────────────────────────
# 2. insert 前置列表
# ──────────────────────────────────────────────────────────────────────────────

class TestInsert:
    def test_insert_comes_before_file(self, tmp_path):
        p = make_wordlist(tmp_path, ["file1", "file2"])
        with loader(p, tmp_path, insert=["pre1", "pre2"]) as wl:
            assert list(wl) == ["pre1", "pre2", "file1", "file2"]

    def test_total_count_includes_insert(self, tmp_path):
        p = make_wordlist(tmp_path, ["a", "b", "c"])
        with loader(p, tmp_path, insert=["x", "y"]) as wl:
            assert wl.total_count == 5

    def test_current_index_during_insert(self, tmp_path):
        p = make_wordlist(tmp_path, ["file"])
        with loader(p, tmp_path, insert=["ins1", "ins2"]) as wl:
            assert wl.current_index == 0
            wl.next_word()  # ins1
            assert wl.current_index == 1
            wl.next_word()  # ins2
            assert wl.current_index == 2
            wl.next_word()  # file
            assert wl.current_index == 3

    def test_empty_insert_list(self, tmp_path):
        p = make_wordlist(tmp_path, ["w1"])
        with loader(p, tmp_path, insert=[]) as wl:
            assert list(wl) == ["w1"]

    def test_insert_only_no_file_lines(self, tmp_path):
        """文件为空，仅消耗 insert"""
        p = tmp_path / "empty.txt"
        p.write_text("", encoding="utf-8")
        with loader(p, tmp_path, insert=["only"]) as wl:
            assert list(wl) == ["only"]


# ──────────────────────────────────────────────────────────────────────────────
# 3. 属性
# ──────────────────────────────────────────────────────────────────────────────

class TestProperties:
    def test_file_line_count(self, tmp_path):
        words = ["a", "b", "c", "d", "e"]
        p = make_wordlist(tmp_path, words)
        with loader(p, tmp_path) as wl:
            assert wl.file_line_count == len(words)

    def test_total_count(self, tmp_path):
        p = make_wordlist(tmp_path, ["x"] * 7)
        with loader(p, tmp_path, insert=["i"] * 3) as wl:
            assert wl.total_count == 10

    def test_remaining_count_decreases(self, tmp_path):
        p = make_wordlist(tmp_path, ["a", "b", "c"])
        with loader(p, tmp_path) as wl:
            total = wl.total_count
            for consumed in range(1, 4):
                wl.next_word()
                assert wl.remaining_count == total - consumed

    def test_remaining_at_eof_is_zero(self, tmp_path):
        p = make_wordlist(tmp_path, ["only"])
        with loader(p, tmp_path) as wl:
            list(wl)
            assert wl.remaining_count == 0

    def test_repr_contains_filename(self, tmp_path):
        p = make_wordlist(tmp_path, ["x"], name="mylist.txt")
        with loader(p, tmp_path) as wl:
            assert "mylist.txt" in repr(wl)

    def test_repr_after_close(self, tmp_path):
        p = make_wordlist(tmp_path, ["x"])
        wl = loader(p, tmp_path)
        wl.close()
        assert "closed" in repr(wl)


# ──────────────────────────────────────────────────────────────────────────────
# 4. seek_to / reset
# ──────────────────────────────────────────────────────────────────────────────

class TestSeekReset:
    def test_reset_reads_from_beginning(self, tmp_path):
        words = ["a", "b", "c"]
        p = make_wordlist(tmp_path, words)
        with loader(p, tmp_path) as wl:
            list(wl)             # 读完
            wl.reset()
            assert list(wl) == words

    def test_reset_clears_checkpoint_files(self, tmp_path):
        p = make_wordlist(tmp_path, ["x"])
        with loader(p, tmp_path, continue_=True) as wl:
            wl.next_word()
            wl.reset()
            # checkpoint 文件应已被删除
            assert not wl.checkpoint_file.exists()
            assert not wl.insert_checkpoint_file.exists()
            assert not wl.loader_hash_file.exists()

    def test_seek_to_file_portion(self, tmp_path):
        words = ["w0", "w1", "w2", "w3", "w4"]
        p = make_wordlist(tmp_path, words)
        with loader(p, tmp_path) as wl:
            wl.seek_to(2)
            assert wl.current_index == 2
            assert wl.next_word() == "w2"

    def test_seek_to_insert_portion(self, tmp_path):
        p = make_wordlist(tmp_path, ["file0", "file1"])
        with loader(p, tmp_path, insert=["ins0", "ins1", "ins2"]) as wl:
            wl.seek_to(1)
            assert wl.current_index == 1
            assert wl.next_word() == "ins1"

    def test_seek_to_boundary_insert_file(self, tmp_path):
        """seek_to 恰好指向 insert 末尾，下一个应为文件第一个词"""
        p = make_wordlist(tmp_path, ["file0", "file1"])
        with loader(p, tmp_path, insert=["ins0"]) as wl:
            wl.seek_to(1)   # insert 长度=1，所以指向 file 起点
            assert wl.next_word() == "file0"

    def test_seek_to_zero_same_as_reset(self, tmp_path):
        words = ["a", "b"]
        p = make_wordlist(tmp_path, words)
        with loader(p, tmp_path) as wl:
            wl.next_word()
            wl.seek_to(0)
            assert wl.current_index == 0
            assert wl.next_word() == "a"


# ──────────────────────────────────────────────────────────────────────────────
# 5. 索引缓存（cache）
# ──────────────────────────────────────────────────────────────────────────────

class TestIndexCache:
    def test_cache_is_reused(self, tmp_path):
        """第二次构造不应重建索引（meta.json 内容不变）"""
        words = ["x"] * 100
        p = make_wordlist(tmp_path, words)
        cache_dir = tmp_path / "cache"

        with loader(p, tmp_path) as wl:
            mtime1 = wl.index_file.stat().st_mtime

        time.sleep(0.05)

        with loader(p, tmp_path) as wl:
            mtime2 = wl.index_file.stat().st_mtime

        assert mtime1 == mtime2, "缓存应被复用，index.bin 不应重写"

    def test_cache_invalidated_on_content_change(self, tmp_path):
        """文件内容变化后缓存应失效"""
        p = make_wordlist(tmp_path, ["old"])
        cache_dir = tmp_path / "cache"

        with loader(p, tmp_path) as wl:
            mtime1 = wl.index_file.stat().st_mtime

        # 修改文件内容
        p.write_text("new\ncontent\n", encoding="utf-8")
        time.sleep(0.05)

        with loader(p, tmp_path) as wl:
            mtime2 = wl.index_file.stat().st_mtime
            assert list(wl) == ["new", "content"]

        assert mtime2 >= mtime1

    def test_tag_creates_separate_cache(self, tmp_path):
        """不同 tag 使用不同缓存目录"""
        p = make_wordlist(tmp_path, ["w"])
        with loader(p, tmp_path, tag="tag_a") as wl_a:
            cache_a = wl_a.cache_dir
        with loader(p, tmp_path, tag="tag_b") as wl_b:
            cache_b = wl_b.cache_dir
        assert cache_a != cache_b

    def test_corrupted_meta_triggers_rebuild(self, tmp_path):
        """损坏 meta.json 后应自动重建索引"""
        p = make_wordlist(tmp_path, ["a", "b"])
        with loader(p, tmp_path) as wl:
            meta_path = wl.meta_file

        meta_path.write_text("{ invalid json }", encoding="utf-8")

        with loader(p, tmp_path) as wl:
            assert list(wl) == ["a", "b"]


# ──────────────────────────────────────────────────────────────────────────────
# 6. 断点续传（checkpoint）
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckpoint:
    """复用现有的 test_checkpoint 逻辑，并扩充边界情形"""

    def test_basic_resume_no_rollback(self, tmp_path):
        words = ["w0", "w1", "w2", "w3", "w4"]
        p = make_wordlist(tmp_path, words)
        cache = tmp_path / "cache"

        last_word = None
        with WordlistLoader(p, continue_=True, rollback=0, cache_dir=cache,
                            checkpoint_interval=1) as wl:
            wl.reset()
            for _ in range(3):
                last_word = wl.next_word()
            remaining = wl.remaining_count
            idx = wl.current_index

        # 续传：rollback=0 应精确继续
        with WordlistLoader(p, continue_=True, rollback=0, cache_dir=cache,
                            checkpoint_interval=1) as wl:
            assert wl.current_index == idx
            assert wl.next_word() == "w3"

    def test_resume_with_rollback_1(self, tmp_path):
        words = [f"w{i}" for i in range(10)]
        p = make_wordlist(tmp_path, words)
        cache = tmp_path / "cache"

        last_word = ""
        with WordlistLoader(p, continue_=True, rollback=0, cache_dir=cache,
                            checkpoint_interval=1) as wl:
            wl.reset()
            for _ in range(5):
                last_word = wl.next_word()
            remaining = wl.remaining_count

        with WordlistLoader(p, continue_=True, rollback=1, cache_dir=cache,
                            checkpoint_interval=1) as wl:
            # rollback=1：应重放最后一个词
            assert wl.next_word() == last_word
            assert wl.remaining_count == remaining

    def test_checkpoint_files_exist_after_iteration(self, tmp_path):
        p = make_wordlist(tmp_path, ["a", "b"])
        cache = tmp_path / "cache"
        with WordlistLoader(p, continue_=True, cache_dir=cache,
                            checkpoint_interval=1) as wl:
            wl.next_word()
            wl.next_word()
        assert wl.checkpoint_file.exists()
        assert wl.insert_checkpoint_file.exists()
        assert wl.loader_hash_file.exists()

    def test_corrupted_checkpoint_resets(self, tmp_path):
        """篡改 checkpoint 哈希，应从头开始"""
        words = ["a", "b", "c", "d"]
        p = make_wordlist(tmp_path, words)
        cache = tmp_path / "cache"

        with WordlistLoader(p, continue_=True, rollback=0, cache_dir=cache,
                            checkpoint_interval=1) as wl:
            wl.reset()
            wl.next_word(); wl.next_word()

        wl.loader_hash_file.write_text("badbadbad", encoding="utf-8")

        with WordlistLoader(p, continue_=True, rollback=0, cache_dir=cache,
                            checkpoint_interval=1) as wl:
            assert wl.current_index == 0

    def test_checkpoint_with_insert(self, tmp_path):
        """insert + file 混合场景的断点续传"""
        p = make_wordlist(tmp_path, ["f0", "f1", "f2"])
        cache = tmp_path / "cache"
        ins = ["i0", "i1"]

        last_word = ""
        with WordlistLoader(p, continue_=True, rollback=0, cache_dir=cache,
                            checkpoint_interval=1, insert=ins) as wl:
            wl.reset()
            for _ in range(4):           # i0, i1, f0, f1
                last_word = wl.next_word()
            remaining = wl.remaining_count

        with WordlistLoader(p, continue_=True, rollback=1, cache_dir=cache,
                            checkpoint_interval=1, insert=ins) as wl:
            assert wl.next_word() == last_word
            assert wl.remaining_count == remaining

    def test_resume_after_full_exhaustion(self, tmp_path):
        """读完所有词后，续传应从头（或 EOF 位置）开始，remaining=0"""
        p = make_wordlist(tmp_path, ["only"])
        cache = tmp_path / "cache"
        with WordlistLoader(p, continue_=True, rollback=0, cache_dir=cache,
                            checkpoint_interval=1) as wl:
            wl.reset()
            list(wl)

        with WordlistLoader(p, continue_=True, rollback=0, cache_dir=cache,
                            checkpoint_interval=1) as wl:
            assert wl.remaining_count == 0
            assert wl.next_word() is None

    def test_rollback_larger_than_progress_clamps_to_zero(self, tmp_path):
        """rollback 超过已有进度，应钳制到 0"""
        p = make_wordlist(tmp_path, ["a", "b", "c"])
        cache = tmp_path / "cache"

        with WordlistLoader(p, continue_=True, rollback=0, cache_dir=cache,
                            checkpoint_interval=1) as wl:
            wl.reset()
            wl.next_word()  # progress=1

        with WordlistLoader(p, continue_=True, rollback=999, cache_dir=cache,
                            checkpoint_interval=1) as wl:
            assert wl.current_index == 0


class TestCheckpointRockyou:
    """直接使用 rockyou.txt 的集成测试，复现原始 test_checkpoint 并扩展"""

    def test_original_checkpoint_scenario(self):
        """复现原始通过的场景"""
        word = ""
        remaining_count = 0
        with WordlistLoader(ROCKYOU, tag="test1", continue_=True, rollback=0) as loader:
            loader.reset()
            assert loader.current_index == 0
            for _ in range(10):
                word = loader.next_word()
            assert loader.current_index == 10
            remaining_count = loader.remaining_count
        with WordlistLoader(ROCKYOU, tag="test1", continue_=True, rollback=1) as loader:
            assert word == loader.next_word()
            assert remaining_count == loader.remaining_count

    def test_checkpoint_with_insert_rockyou(self):
        """insert 混合 rockyou 的断点续传"""
        word = ""
        remaining_count = 0
        with WordlistLoader(ROCKYOU, tag="test1", continue_=True, rollback=0,
                            insert=[""]) as loader:
            loader.reset()
            assert loader.current_index == 0
            for _ in range(10):
                word = loader.next_word()
            assert loader.current_index == 10
            remaining_count = loader.remaining_count
        with WordlistLoader(ROCKYOU, tag="test1", continue_=True, rollback=1,
                            insert=[""]) as loader:
            assert word == loader.next_word()
            assert remaining_count == loader.remaining_count

    def test_seek_and_resume_rockyou(self):
        """seek_to 后续传保持位置"""
        with WordlistLoader(ROCKYOU, tag="seek_test", continue_=True, rollback=0) as wl:
            wl.seek_to(100)
            word_at_100 = wl.next_word()

        with WordlistLoader(ROCKYOU, tag="seek_test", continue_=True, rollback=1) as wl:
            # rollback=1，应重放 word_at_100
            assert wl.next_word() == word_at_100

    def test_large_rollback_clamps(self):
        """大 rollback 在 rockyou 上也应钳制到 0"""
        with WordlistLoader(ROCKYOU, tag="rollback_test", continue_=True,
                            rollback=0) as wl:
            wl.reset()
            wl.next_word()  # progress=1

        with WordlistLoader(ROCKYOU, tag="rollback_test", continue_=True,
                            rollback=10_000_000) as wl:
            assert wl.current_index == 0