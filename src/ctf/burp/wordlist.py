"""
WordlistLoader  – 大文件词表加载器，支持 O(1) 随机行访问、断点续传、二进制索引缓存。
PayloadLoader   – 多字段组合包装器，支持 PARALLEL / PRODUCT 策略。

"""

from __future__ import annotations

import array
import hashlib
import json
import struct
from enum import Enum, auto
from pathlib import Path
from typing import IO, Iterator, Optional

import xxhash

_DEFAULT_CACHE_DIR = Path.home() / ".cache" / "wordlist"


# ---------------------------------------------------------------------------
# WordlistLoader
# ---------------------------------------------------------------------------


class WordlistLoader:
    """
    大文件词表加载器。

    特性
    ----
    - O(1) 随机行访问（字节偏移索引）
    - 断点续传（持久化 checkpoint）
    - 高效二进制索引缓存，校验 mtime / size / content_hash / encoding / strip
    - 文件耗尽时自动持久化最终 checkpoint

    注意：非线程安全，多线程场景请在外部加锁。
    """

    _CACHE_VERSION = 5          # 版本号随缓存格式变化递增
    _MAGIC = b"WLIDX\x00\x00\x05"
    _SAMPLE_SIZE = 65_536       # 采样哈希的单段大小（64 KiB）

    # ------------------------------------------------------------------
    # 构造 / 析构
    # ------------------------------------------------------------------

    def __init__(
        self,
        path: str | Path,
        encoding: str = "utf-8",
        strip: bool = True,
        tag: str | None = None,
        continue_: bool = False,
        cache_dir: Path | None = None,
        checkpoint_interval: int = 1000,
        use_empty_password: bool = False,
        rollback: int = 1,
    ) -> None:
        self._use_empty_password = use_empty_password
        self._use_empty_password_done = False
        self.path = Path(path).resolve()
        self.encoding = encoding
        self.errors = "ignore"
        self.strip = strip
        self.continue_ = continue_
        self.checkpoint_interval = checkpoint_interval

        cache_dir = Path(cache_dir) if cache_dir else _DEFAULT_CACHE_DIR
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_key = tag if tag else hashlib.md5(str(self.path).encode()).hexdigest()[:12]
        self._index_path = cache_dir / f"{cache_key}.index.bin"
        self._checkpoint_path = cache_dir / f"{cache_key}.checkpoint.json"

        self._offsets: array.array = array.array("Q")
        self._line_index: int = 0
        self._dirty: int = 0
        self._fp: IO[bytes] | None = None

        self._load_or_build_cache()
        self._fp = open(self.path, mode="rb")
        self._rollback = max(0, rollback)

        if continue_:
            cp = self._load_checkpoint()
            if cp and self._checkpoint_valid(cp):
                self._restore_checkpoint(cp)

    def close(self) -> None:
        if self._fp is not None and not self._fp.closed:
            self._save_checkpoint()
            self._fp.close()
        self._fp = None

    def __enter__(self) -> "WordlistLoader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:
        fp = getattr(self, "_fp", None)
        if fp is not None and not fp.closed:
            try:
                fp.close()
            except Exception:
                pass
        self._fp = None

    # ------------------------------------------------------------------
    # 索引缓存
    # ------------------------------------------------------------------

    @classmethod
    def _fast_hash(cls, path: Path) -> str:
        """
        对文件做三段采样哈希（头 / 中 / 尾各 SAMPLE_SIZE 字节）。
        三段区间互不重叠：只有文件足够大时才读中段和尾段。
        """
        h = xxhash.xxh64()
        s = cls._SAMPLE_SIZE
        file_size = path.stat().st_size

        with open(path, "rb") as f:
            # 头部
            h.update(f.read(s))

            # 中部：需要文件 > 2*s 才不与头部重叠
            mid = file_size // 2
            if mid > s:
                f.seek(mid)
                h.update(f.read(s))

            # 尾部：需要文件 > 3*s 才不与中部重叠
            tail = file_size - s
            if tail > mid + s:
                f.seek(tail)
                h.update(f.read(s))

        return h.hexdigest()

    @classmethod
    def _write_index(cls, bin_path: Path, offsets: array.array, meta: dict) -> None:
        meta_bytes = json.dumps(meta, separators=(",", ":")).encode()
        with open(bin_path, "wb") as f:
            f.write(cls._MAGIC)
            f.write(struct.pack(">I", len(meta_bytes)))
            f.write(meta_bytes)
            offsets.tofile(f)

    @classmethod
    def _read_index(cls, bin_path: Path) -> tuple[array.array, dict] | None:
        try:
            with open(bin_path, "rb") as f:
                if f.read(8) != cls._MAGIC:
                    return None
                (meta_len,) = struct.unpack(">I", f.read(4))
                meta = json.loads(f.read(meta_len))
                buf: array.array = array.array("Q")
                buf.fromfile(f, meta["line_count"])
            return buf, meta
        except Exception:
            return None

    def _file_meta(self) -> dict:
        stat = self.path.stat()
        return {
            "version": self._CACHE_VERSION,
            "path": str(self.path),
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "encoding": self.encoding,
            "strip": self.strip,
            "content_hash": self._fast_hash(self.path),
        }

    def _cache_valid(self, cached_meta: dict) -> bool:
        if cached_meta.get("version") != self._CACHE_VERSION:
            return False
        cur = self._file_meta()
        return (
            cached_meta.get("mtime") == cur["mtime"]
            and cached_meta.get("size") == cur["size"]
            and cached_meta.get("content_hash") == cur["content_hash"]
            and cached_meta.get("encoding") == cur["encoding"]
            and cached_meta.get("strip") == cur["strip"]
        )

    def _build_index(self) -> None:
        """
        构建物理行偏移索引（每行起始字节位置）。

        - 空文件：_offsets 为空，line_count = 0。
        - 末尾无换行：最后一行偏移已在循环中正确记录，不额外追加。
        - 末尾有换行：循环会追加一个等于 file_size 的偏移，裁剪掉它
          （该偏移对应的"行"是长度为 0 的幽灵行）。
        """
        file_size = self.path.stat().st_size
        if file_size == 0:
            self._offsets = array.array("Q")
            return

        offsets: list[int] = [0]
        pos = 0
        with open(self.path, "rb") as bf:
            while True:
                chunk = bf.read(1 << 20)  # 1 MiB
                if not chunk:
                    break
                start = 0
                while True:
                    idx = chunk.find(b"\n", start)
                    if idx == -1:
                        break
                    next_line_start = pos + idx + 1
                    offsets.append(next_line_start)
                    start = idx + 1
                pos += len(chunk)

        # 裁剪末尾的幽灵偏移（文件以 \n 结尾时产生）
        if offsets and offsets[-1] == file_size:
            offsets.pop()

        self._offsets = array.array("Q", offsets)

    def _load_or_build_cache(self) -> None:
        result = self._read_index(self._index_path)
        if result is not None:
            offsets, meta = result
            if self._cache_valid(meta):
                self._offsets = offsets
                return
        self._build_index()
        meta = {**self._file_meta(), "line_count": len(self._offsets)}
        try:
            self._write_index(self._index_path, self._offsets, meta)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Checkpoint
    # ------------------------------------------------------------------

    def _save_checkpoint(self) -> None:
        """
        持久化当前物理行号。恢复时通过 _offsets[line_index] 重算字节偏移，
        不依赖存储的 byte_offset，避免索引与偏移不一致的问题。
        """
        try:
            payload = {
                "line_index": self._line_index,
                "empty_password_done": self._use_empty_password_done,
            }
            self._checkpoint_path.write_text(json.dumps(payload, separators=(",", ":")))
            self._dirty = 0
        except Exception:
            pass

    def _load_checkpoint(self) -> dict | None:
        try:
            return json.loads(self._checkpoint_path.read_text())
        except Exception:
            return None

    def _checkpoint_valid(self, cp: dict) -> bool:
        try:
            line_index = cp.get("line_index", -1)
            return isinstance(line_index, int) and 0 <= line_index <= len(self._offsets)
        except Exception:
            return False

    def _restore_checkpoint(self, cp: dict) -> None:
        line_index: int = cp["line_index"]
        empty_done: bool = cp.get("empty_password_done", False) and self._use_empty_password

        if self._rollback > 0:
            rolled = min(line_index, self._rollback)
            line_index -= rolled
            if rolled > 0 and line_index == 0:
                # 只有真正回退到起点才重置空密码状态
                empty_done = False

        self._line_index = line_index
        self._use_empty_password_done = empty_done

        assert self._fp is not None
        if line_index < len(self._offsets):
            self._fp.seek(self._offsets[line_index])
        else:
            self._fp.seek(0, 2)  # 已耗尽，seek 到 EOF

    # ------------------------------------------------------------------
    # 读取接口
    # ------------------------------------------------------------------

    def next_word(self) -> Optional[str]:
        """
        返回下一个非空词，文件耗尽时返回 None 并持久化最终 checkpoint。
        """
        if self._use_empty_password and not self._use_empty_password_done:
            self._use_empty_password_done = True
            return ""

        if self._fp is None or self._fp.closed:
            return None

        while True:
            raw = self._fp.readline()
            if raw == b"":
                # 文件耗尽：保存最终状态后返回
                self._save_checkpoint()
                return None

            self._line_index += 1
            line = raw.decode(self.encoding, errors=self.errors)
            word = line.strip() if self.strip else line.rstrip("\r\n")
            if not word:
                continue  # 空行：不计入 _dirty，不触发 checkpoint

            self._dirty += 1
            if self._dirty >= self.checkpoint_interval:
                self._save_checkpoint()
            return word

    def next(self) -> Optional[str]:
        return self.next_word()

    def seek_to_line(self, n: int) -> None:
        """
        跳转到第 n 物理行（0-based）。
        n == line_count 为合法值，语义为"已耗尽/EOF 位置"。
        """
        if not self._offsets and n != 0:
            raise RuntimeError("Index not built yet.")
        total = len(self._offsets)
        if not (0 <= n <= total):
            raise IndexError(
                f"Line {n} out of range [0, {total}]. "
                f"File has {total} lines (pass n={total} to seek to EOF)."
            )
        if self._fp is None or self._fp.closed:
            raise RuntimeError("File is not open.")
        if n < total:
            self._fp.seek(self._offsets[n])
        else:
            self._fp.seek(0, 2)
        self._line_index = n
        self._dirty = 0
        self._save_checkpoint()

    def reset(self) -> None:
        """重置到文件起点并清除 checkpoint。"""
        self.seek_to_line(0)
        self._use_empty_password_done = False
        try:
            self._checkpoint_path.unlink(missing_ok=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def line_count(self) -> int:
        """文件物理行数（含空行）。"""
        return len(self._offsets)

    @property
    def current_line(self) -> int:
        """下一次 readline() 将读取的物理行号（0-based）。"""
        return self._line_index

    @property
    def remaining_count(self) -> int:
        """
        剩余物理行数（含空行）加上未消耗的空密码计数。
        注意：实际返回的非空词数可能更少。
        """
        remaining = max(0, self.line_count - self._line_index)
        if self._use_empty_password and not self._use_empty_password_done:
            remaining += 1
        return remaining

    # ------------------------------------------------------------------
    # 迭代器协议
    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[str]:
        while (word := self.next_word()) is not None:
            yield word

    def __next__(self) -> str:
        word = self.next_word()
        if word is None:
            raise StopIteration
        return word

    def __repr__(self) -> str:
        status = (
            "closed"
            if (self._fp is None or self._fp.closed)
            else f"line {self._line_index}/{self.line_count}"
        )
        return (
            f"<WordlistLoader path={self.path.name!r} "
            f"lines={self.line_count} {status}>"
        )


# ---------------------------------------------------------------------------
# PayloadLoader
# ---------------------------------------------------------------------------


class CombineStrategy(Enum):
    PARALLEL = auto()   # zip：所有字段同步前进，最短词表耗尽即停
    PRODUCT = auto()    # itertools.product：全排列


class _ProductCheckpoint:
    """
    Fix #8: PRODUCT 模式下的组合状态 checkpoint。

    存储各层 loader 当前的物理行号，恢复时直接 seek_to_line，
    无需销毁内层 loader 的 checkpoint 文件。
    """

    __slots__ = ("path",)

    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, line_indices: list[int]) -> None:
        try:
            self.path.write_text(json.dumps({"indices": line_indices}, separators=(",", ":")))
        except Exception:
            pass

    def load(self) -> list[int] | None:
        try:
            data = json.loads(self.path.read_text())
            indices = data.get("indices")
            if isinstance(indices, list) and all(isinstance(i, int) for i in indices):
                return indices
        except Exception:
            pass
        return None

    def delete(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except Exception:
            pass


class PayloadLoader:
    """
    WordlistLoader 的多字段组合包装器。

    策略
    ----
    PARALLEL : zip 语义，所有字段同步推进，最短词表耗尽即停。
               支持断点续传（各 loader 独立 checkpoint）。
    PRODUCT  : 全排列，第 0 个字段最慢（外层），最后字段最快（内层）。
               Fix #8: 使用独立的组合状态 checkpoint，内层 loader
               reset 时不再销毁其 checkpoint 文件。

    注意：非线程安全，多线程场景请在外部加锁。
    Fix #11: next() 的惰性初始化在多线程下存在竞态，使用方需自行加锁。

    用法示例
    --------
    # 并行模式
    loader = PayloadLoader(
        [("user", "users.txt"), ("pass", "passwords.txt")],
        strategy=CombineStrategy.PARALLEL,
    )

    # 全排列模式（断点续传）
    loader = PayloadLoader(
        [("user", "users.txt"), ("pass", "passwords.txt")],
        strategy=CombineStrategy.PRODUCT,
        continue_=True,
        tag="my_task",
    )

    with loader:
        while (payload := loader.next()) is not None:
            print(payload)  # {"user": "admin", "pass": "123456"}
    """

    def __init__(
        self,
        fields: list[tuple[str, str | Path]],
        strategy: CombineStrategy = CombineStrategy.PRODUCT,
        *,
        encoding: str = "utf-8",
        strip: bool = True,
        continue_: bool = False,
        cache_dir: Path | None = None,
        checkpoint_interval: int = 1000,
        rollback: int = 1,
        tag: str | None = None,
    ) -> None:
        if not fields:
            raise ValueError("fields 不能为空")

        self._names: list[str] = [name for name, _ in fields]
        self._strategy = strategy
        self._continue = continue_

        cache_dir = Path(cache_dir) if cache_dir else _DEFAULT_CACHE_DIR
        cache_dir.mkdir(parents=True, exist_ok=True)


        self._loaders: list[WordlistLoader] = []
        for i, (name, path) in enumerate(fields):
            path_hash = hashlib.md5(str(Path(path).resolve()).encode()).hexdigest()[:8]
            field_tag = f"{tag or 'pl'}_{i}_{name}_{path_hash}" if tag else None
            self._loaders.append(
                WordlistLoader(
                    path,
                    encoding=encoding,
                    strip=strip,
                    tag=field_tag,
                    continue_=continue_ and strategy == CombineStrategy.PARALLEL,
                    cache_dir=cache_dir,
                    checkpoint_interval=checkpoint_interval,
                    rollback=rollback,
                )
            )

        # PRODUCT 模式专用 checkpoint
        product_tag = tag or hashlib.md5(
            "".join(str(Path(p).resolve()) for _, p in fields).encode()
        ).hexdigest()[:12]
        self._product_cp = _ProductCheckpoint(
            cache_dir / f"{product_tag}.product_cp.json"
        )

        # 惰性构建迭代器
        self._iter: Iterator[tuple[str, ...]] | None = None

        # PRODUCT + continue_: 恢复各层行号
        if continue_ and strategy == CombineStrategy.PRODUCT:
            self._restore_product_checkpoint()

    # ------------------------------------------------------------------
    # 上下文管理
    # ------------------------------------------------------------------

    def __enter__(self) -> "PayloadLoader":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def close(self) -> None:
        for loader in self._loaders:
            loader.close()

    # ------------------------------------------------------------------
    # PRODUCT checkpoint 恢复
    # ------------------------------------------------------------------

    def _restore_product_checkpoint(self) -> None:
        indices = self._product_cp.load()
        if indices is None or len(indices) != len(self._loaders):
            return
        for loader, idx in zip(self._loaders, indices):
            if 0 <= idx <= loader.line_count:
                try:
                    loader.seek_to_line(idx)
                except Exception:
                    pass

    def _save_product_checkpoint(self) -> None:
        indices = [loader.current_line for loader in self._loaders]
        self._product_cp.save(indices)

    # ------------------------------------------------------------------
    # 迭代器构建（惰性，首次调用 next() 时初始化）
    # ------------------------------------------------------------------

    def _build_iter(self) -> Iterator[tuple[str, ...]]:
        match self._strategy:
            case CombineStrategy.PARALLEL:
                return self._parallel_iter()
            case CombineStrategy.PRODUCT:
                return self._product_iter()
            case _:
                raise ValueError(f"未知策略: {self._strategy}")

    def _parallel_iter(self) -> Iterator[tuple[str, ...]]:
        """所有 loader 同步推进，任一耗尽即停（zip 语义）。"""
        while True:
            words: list[str] = []
            for loader in self._loaders:
                word = loader.next_word()
                if word is None:
                    return
                words.append(word)
            yield tuple(words)

    def _product_iter(self) -> Iterator[tuple[str, ...]]:
        """
        全排列迭代器
        遍历顺序：第 0 个字段最慢（最外层循环），最后一个字段最快。
        """
        n = len(self._loaders)
        if n == 0:
            return

        # 各层当前词缓存，None 表示该层需要推进
        slots: list[str | None] = [None] * n
        # 先为除最内层外的每层取第一个词
        for i in range(n - 1):
            word = self._loaders[i].next_word()
            if word is None:
                return  # 某层词表为空，无法构成任何组合
            slots[i] = word

        # 最内层始终在最快的内循环推进
        while True:
            word = self._loaders[-1].next_word()
            if word is not None:
                slots[-1] = word
                self._save_product_checkpoint()
                yield tuple(slots)  # type: ignore[arg-type]
                continue

            # 最内层耗尽，向外进位
            carry = n - 1
            while carry >= 0:
                # 重置当前层（不删除其 checkpoint）
                self._loaders[carry].seek_to_line(0)
                carry -= 1
                if carry < 0:
                    return  # 所有层都耗尽
                next_word = self._loaders[carry].next_word()
                if next_word is not None:
                    slots[carry] = next_word
                    break
                # carry 层也耗尽，继续向外进位

            # carry 层成功推进，重置 carry+1 到 n-2 层（已在循环中重置过 n-1 层）
            for i in range(carry + 1, n - 1):
                self._loaders[i].seek_to_line(0)
                word_i = self._loaders[i].next_word()
                if word_i is None:
                    return  # 该层词表为空（理论上不应发生，防御性检查）
                slots[i] = word_i

    # ------------------------------------------------------------------
    # 核心接口
    # ------------------------------------------------------------------

    def next(self) -> Optional[dict[str, str]]:
        """返回下一个组合 payload，耗尽返回 None。"""
        if self._iter is None:
            self._iter = self._build_iter()
        try:
            assert self._iter is not None
            values = next(self._iter)
            return dict(zip(self._names, values))
        except StopIteration:
            return None

    # ------------------------------------------------------------------
    # 迭代器协议
    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[dict[str, str]]:
        while (payload := self.next()) is not None:
            yield payload

    def __next__(self) -> dict[str, str]:
        payload = self.next()
        if payload is None:
            raise StopIteration
        return payload

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    @property
    def strategy(self) -> CombineStrategy:
        return self._strategy

    def __repr__(self) -> str:
        fields = ", ".join(
            f"{name}={loader!r}"
            for name, loader in zip(self._names, self._loaders)
        )
        return f"<PayloadLoader strategy={self._strategy.name} fields=[{fields}]>"