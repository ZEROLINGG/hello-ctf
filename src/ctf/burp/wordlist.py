from __future__ import annotations

import array
import hashlib
import json
import mmap
import os
import struct
from pathlib import Path
from typing import Iterator, Optional, BinaryIO

import xxhash

from ctf import debug_log

_DEFAULT_CACHE_DIR = Path.home() / ".cache" / "wordlist"


def _build_line_index(file: BinaryIO, index_file: BinaryIO) -> mmap.mmap:
    """构建文件的行首字节偏移量索引"""
    file.seek(0)
    index_file.seek(0)
    index_file.truncate(0)

    offset = 0
    buffer = array.array("Q")  # 无符号64位整型
    BUFFER_SIZE = 100_000

    for line in file:
        buffer.append(offset)
        offset += len(line)

        if len(buffer) >= BUFFER_SIZE:
            index_file.write(buffer.tobytes())
            del buffer[:]

    # 写入最后一行之后的 EOF 偏移量
    buffer.append(offset)
    index_file.write(buffer.tobytes())
    index_file.flush()

    return mmap.mmap(index_file.fileno(), 0, access=mmap.ACCESS_READ)


def _large_file_fast_hash(
    path: Path, sample_size: int = 1024 * 1024, sample_interval: int = 20
) -> str:
    """使用稀疏采样和 xxhash 极速计算超大文件的哈希"""
    hasher = xxhash.xxh3_64()
    try:
        stat = path.stat()
        hasher.update(stat.st_size.to_bytes(8, "little"))
        with path.open("rb") as f:
            while True:
                chunk = f.read(sample_size)
                if not chunk:
                    break
                hasher.update(chunk)
                if sample_interval > 0:
                    try:
                        f.seek(sample_size * sample_interval, os.SEEK_CUR)
                    except OSError:
                        break
    except OSError as e:
        debug_log(f"大文件哈希计算异常 {path}: {e}")
    return hasher.hexdigest()


def _fast_hash(data: list[str | bytes | Path]) -> str:
    """计算元数据或小文件的组合哈希"""
    hasher = xxhash.xxh3_64()
    for item in data:
        if isinstance(item, str):
            hasher.update(item.encode("utf-8", errors="ignore"))
        elif isinstance(item, bytes):
            hasher.update(item)
        elif isinstance(item, Path) and item.is_file():
            try:
                with item.open("rb") as f:
                    for chunk in iter(lambda: f.read(8192), b""):
                        hasher.update(chunk)
            except OSError as e:
                debug_log(f"哈希计算跳过文件 {item}: {e}")
                continue
    return hasher.hexdigest()


# ==========================================
# WordlistLoader 核心类
# ==========================================


class WordlistLoader:
    version: int = 2

    def __init__(
        self,
        path: str | Path,
        encoding: str = "utf-8",
        strip: bool = True,
        tag: str | None = None,
        continue_: bool = False,
        cache_dir: Path | None = None,
        checkpoint_interval: int = 1000,
        insert: list[str] | None = None,
        rollback: int = 1,
    ) -> None:
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise FileNotFoundError(f"字典文件不存在: {self.path}")

        self.encoding = encoding
        self.errors = "ignore"
        self.strip = strip
        self.continue_ = continue_
        self.checkpoint_interval = checkpoint_interval
        self.insert = insert or []
        self._rollback = max(0, rollback)

        # 内部状态
        self._insert_idx: int = 0
        self._file_idx: int = 0
        self._dirty: int = 0

        self._fp: BinaryIO | None = None
        self._index_fp: BinaryIO | None = None
        self._mmap: mmap.mmap | None = None

        try:
            # 1. 初始化缓存目录
            base_cache_dir = Path(cache_dir) if cache_dir else _DEFAULT_CACHE_DIR
            base_cache_dir.mkdir(parents=True, exist_ok=True)

            cache_key = (
                hashlib.md5(tag.encode()).hexdigest()[:12]
                if tag
                else hashlib.md5(str(self.path).encode()).hexdigest()[:12]
            )

            self.cache_dir = base_cache_dir / cache_key
            self.cache_dir.mkdir(parents=True, exist_ok=True)

            self.meta_file = self.cache_dir / "meta.json"
            self.index_file = self.cache_dir / "index.bin"
            self.checkpoint_file = self.cache_dir / "checkpoint.idx"
            self.insert_checkpoint_file = self.cache_dir / "insert.idx"
            self.loader_hash_file = self.cache_dir / "loader.hash"

            # 2. 校验与构建索引
            self._init_index_and_cache(tag)

            # 3. 打开文件句柄
            self._fp = open(self.path, "rb")

            # 4. 断点续传处理
            if self.continue_:
                self._restore_checkpoint()

        except Exception as e:
            self.close()
            raise RuntimeError(f"WordlistLoader 初始化失败: {e}") from e

    # ------------------------------------------------------------------
    # 初始化与缓存逻辑
    # ------------------------------------------------------------------

    def _init_index_and_cache(self, tag: str | None) -> None:
        """检查缓存是否有效，无效则重新构建 MMap 索引"""
        current_file_hash = _large_file_fast_hash(self.path)
        current_meta = {
            "tag": tag,
            "insert": self.insert,
            "wordlist_hash": current_file_hash,
            "version": self.version,
            "encoding": self.encoding,
            "strip": self.strip,
        }

        meta_str = json.dumps(current_meta, sort_keys=True)
        rebuild_needed = True

        if self.meta_file.exists() and self.index_file.exists():
            try:
                cached_meta = json.loads(self.meta_file.read_text("utf-8"))
                if cached_meta == current_meta:
                    rebuild_needed = False
            except (json.JSONDecodeError, OSError) as e:
                debug_log(f"缓存元数据读取失败，将重建索引: {e}")

        self._index_fp = open(self.index_file, "a+b")

        if rebuild_needed:
            debug_log(f"正在为 {self.path.name} 构建极速访问索引，请稍候...")
            with open(self.path, "rb") as f:
                assert self._index_fp is not None
                self._mmap = _build_line_index(f, self._index_fp)

            self.meta_file.write_text(meta_str, "utf-8")

            # 清理旧的断点文件
            for file_path in (
                self.checkpoint_file,
                self.insert_checkpoint_file,
                self.loader_hash_file,
            ):
                file_path.unlink(missing_ok=True)
        else:
            # 索引复用
            assert self._index_fp is not None
            self._mmap = mmap.mmap(self._index_fp.fileno(), 0, access=mmap.ACCESS_READ)

    # ------------------------------------------------------------------
    # 断点续传逻辑
    # ------------------------------------------------------------------

    def save_checkpoint(self) -> None:
        try:
            self.checkpoint_file.write_text(str(self._file_idx))
            self.insert_checkpoint_file.write_text(str(self._insert_idx))

            meta_content = self.meta_file.read_text("utf-8")
            check_hash = _fast_hash(
                [meta_content, self.checkpoint_file, self.insert_checkpoint_file]
            )
            self.loader_hash_file.write_text(check_hash)
            self._dirty = 0
        except OSError as e:
            debug_log(f"磁盘可能已满或无权限): {e}")

    def _restore_checkpoint(self) -> None:
        if not (
            self.loader_hash_file.exists()
            and self.checkpoint_file.exists()
            and self.insert_checkpoint_file.exists()
        ):
            return

        try:
            meta_content = self.meta_file.read_text("utf-8")
            expected_hash = _fast_hash(
                [meta_content, self.checkpoint_file, self.insert_checkpoint_file]
            )

            if expected_hash != self.loader_hash_file.read_text().strip():
                debug_log("断点文件哈希校验失败，可能被篡改，放弃恢复。")
                self.reset()
                return

            c_file_idx = int(self.checkpoint_file.read_text().strip())
            c_insert_idx = int(self.insert_checkpoint_file.read_text().strip())

            # 应用 Rollback
            total_progress = c_insert_idx + c_file_idx
            rolled_progress = max(0, total_progress - self._rollback)

            if rolled_progress < len(self.insert):
                self._insert_idx = rolled_progress
                self._file_idx = 0
            else:
                self._insert_idx = len(self.insert)
                self._file_idx = rolled_progress - len(self.insert)

            # Seek 到物理位置
            if self._file_idx > 0:
                self._seek_file_to(self._file_idx)

            debug_log(f"成功恢复断点，当前位置: {self.current_index}")

        except Exception as e:
            debug_log(f"读取断点数据发生异常，将重置进度: {e}")
            self.reset()

    # ------------------------------------------------------------------
    # 核心迭代器逻辑
    # ------------------------------------------------------------------

    def next_word(self) -> Optional[str]:
        # 1. 优先消耗 insert 列表
        while self._insert_idx < len(self.insert):
            word = self.insert[self._insert_idx]
            self._insert_idx += 1
            self._check_dirty()
            return word

        # 2. 从文件顺序读取 (readline 最快)
        if self._fp is None or self._fp.closed:
            return None

        while True:
            raw = self._fp.readline()
            if not raw:
                self.save_checkpoint()  # EOF 触发最后一次保存
                return None

            self._file_idx += 1
            line = raw.decode(self.encoding, errors=self.errors)
            word = line.strip() if self.strip else line.rstrip("\r\n")

            if not word:
                continue  # 跳过空行

            self._check_dirty()
            return word

    def _check_dirty(self) -> None:
        self._dirty += 1
        if self._dirty >= self.checkpoint_interval:
            self.save_checkpoint()

    # ------------------------------------------------------------------
    # 随机访问与控制逻辑
    # ------------------------------------------------------------------

    def _seek_file_to(self, line_index: int) -> None:
        """纯内部方法：利用 mmap 查表，将文件指针 O(1) 定位到指定行"""
        if self._mmap is None or self._fp is None:
            return

        total_file_lines = self.file_line_count
        target_idx = min(line_index, total_file_lines)

        start_byte = target_idx * 8
        offset_bytes = self._mmap[start_byte : start_byte + 8]

        # 边界检查：防止 mmap 损坏或截断
        if len(offset_bytes) < 8:
            debug_log("索引文件损坏或读取越界")
            return

        offset = struct.unpack("<Q", offset_bytes)[0]

        self._fp.seek(offset)
        self._file_idx = target_idx

    def seek_to(self, index: int) -> None:
        """跳转到全局索引位置 (包含 insert 的数量)，并持久化 checkpoint"""
        if index < len(self.insert):
            self._insert_idx = index
            self._file_idx = 0
            self._seek_file_to(0)
        else:
            self._insert_idx = len(self.insert)
            self._seek_file_to(index - len(self.insert))

        self.save_checkpoint()

    def reset(self) -> None:
        """重置到起点并清除 checkpoint"""
        self._insert_idx = 0
        self._file_idx = 0
        self._seek_file_to(0)

        for f in (
            self.checkpoint_file,
            self.insert_checkpoint_file,
            self.loader_hash_file,
        ):
            f.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # 属性提取
    # ------------------------------------------------------------------

    @property
    def file_line_count(self) -> int:
        """大文件的物理行数"""
        if self._mmap is None:
            return 0
        return max(0, len(self._mmap) // 8 - 1)

    @property
    def total_count(self) -> int:
        """预置名单 + 文件总行数"""
        return len(self.insert) + self.file_line_count

    @property
    def current_index(self) -> int:
        """当前全局进度索引"""
        return self._insert_idx + self._file_idx

    @property
    def remaining_count(self) -> int:
        return max(0, self.total_count - self.current_index)

    # ------------------------------------------------------------------
    # Context & Iterator
    # ------------------------------------------------------------------

    def close(self) -> None:
        """安全释放所有资源"""
        if self._fp is not None and not self._fp.closed:
            self.save_checkpoint()
            self._fp.close()

        if self._mmap is not None:
            self._mmap.close()
            self._mmap = None

        if self._index_fp is not None and not self._index_fp.closed:
            self._index_fp.close()

    def __enter__(self) -> "WordlistLoader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.close()
        except Exception:
            pass

    def __iter__(self) -> Iterator[str]:
        while (word := self.next_word()) is not None:
            yield word

    def __next__(self) -> str:
        word = self.next_word()
        if word is None:
            raise StopIteration
        return word

    def __del__(self) -> None:
        self.close()

    def __repr__(self) -> str:
        status = (
            "closed"
            if (self._fp is None or self._fp.closed)
            else f"pos={self.current_index}/{self.total_count}"
        )
        return (
            f"<WordlistLoader path={self.path.name!r} "
            f"total={self.total_count} {status}>"
        )


# ==========================================
# PayloadLoader 核心类
# ==========================================


class PayloadLoader:

    def __init__(
        self,
        fields: list[tuple[str, str | Path, list[str]] | tuple[str, str | Path]],
        parallel: bool = False,
        *,
        encoding: str = "utf-8",
        strip: bool = True,
        tag: str | None = None,
        continue_: bool = False,
        cache_dir: Path | None = None,
        checkpoint_interval: int = 1000,
        rollback: int = 1,
    ) -> None:
        if not fields:
            raise ValueError("fields 列表不能为空")

        self.fields = fields
        self.parallel = parallel
        self.continue_ = continue_
        self.checkpoint_interval = checkpoint_interval
        self.rollback = max(0, rollback)

        if tag:
            self.tag = tag
        else:
            tag_src = str([(name, str(Path(path).resolve())) for name, path, *_ in fields])
            self.tag = hashlib.md5(tag_src.encode()).hexdigest()[:12]

        self._names: list[str] = []
        self._loaders: list[WordlistLoader] = []
        self._values: list[str] = []


        for i, field in enumerate(fields):
            if len(field) == 3:
                name, path, insert_list = field  # type: ignore[misc]
            else:
                name, path = field  # type: ignore[misc]
                insert_list = None

            self._names.append(name)
            sub_tag = f"{self.tag}_{i}_{name}"

            if parallel:
                # 全排列
                loader = WordlistLoader(
                    path=path,
                    encoding=encoding,
                    strip=strip,
                    tag=sub_tag,
                    continue_=True,
                    cache_dir=cache_dir,
                    checkpoint_interval=checkpoint_interval,
                    insert=insert_list,
                    rollback=1 if i != len(fields) - 1 else rollback, # 非最内层取1用于恢复时便于.next_word()恢复self._values
                )
                if not self.continue_:
                    loader.reset()
            else:
                loader = WordlistLoader(
                    path=path,
                    encoding=encoding,
                    strip=strip,
                    tag=sub_tag,
                    continue_=continue_,
                    cache_dir=cache_dir,
                    checkpoint_interval=checkpoint_interval,
                    insert=insert_list,
                    rollback=rollback,
                )

            self._loaders.append(loader)

    # ------------------------------------------------------------------
    # Parallel 模式（zip）
    # ------------------------------------------------------------------

    def _next_parallel(self) -> Optional[dict[str, str]]:
        """
        Parallel/zip 模式：所有字段同步推进，任一耗尽即停止。
        """
        result: dict[str, str] = {}
        for name, loader in zip(self._names, self._loaders):
            word = loader.next_word()
            if word is None:
                return None
            result[name] = word
        return result

    # ------------------------------------------------------------------
    # Product 模式（全排列）核心逻辑
    # ------------------------------------------------------------------

    def _next_product(self) -> Optional[dict[str, str]]:
        if not self._values:
            for loader in self._loaders:
                word = loader.next_word()
                if word is None:
                    return None
                self._values.append(word)
            return dict(zip(self._names, self._values))

        for i in range(len(self._loaders) - 1, -1, -1):
            word = self._loaders[i].next_word()
            if word is not None:
                self._values[i] = word
                return dict(zip(self._names, self._values)) # 当前维度取到值就返回

            # 当前维度溢出，进位
            self._loaders[i].reset()
            word = self._loaders[i].next_word()
            if word is None:
                return None  # 空字典文件
            self._values[i] = word
            # 每层操作都会设置实际的self._values[i]，能够确保进位后非i层的缓存值都是有效的

            if i == 0:
                return None  # 最高位溢出，遍历完毕

        return None  # 不可达，保持类型完整性

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def next(self) -> Optional[dict[str, str]]:
        if self.parallel:
            return self._next_parallel()
        else:
            return self._next_product()

    def reset(self) -> None:
        self._values = []
        for loader in self._loaders:
            loader.reset()

    def save_checkpoint(self):
        for loader in self._loaders:
            loader.save_checkpoint()


    @property
    def names(self) -> list[str]:
        """所有字段名称"""
        return list(self._names)

    @property
    def remaining_count(self) -> int:
        if self.parallel:
            return min(
                (loader.remaining_count for loader in self._loaders),
                default=0,
            )

        counts = [loader.total_count for loader in self._loaders]
        n = len(counts)

        suffix = [1] * (n + 1)
        for i in range(n - 1, -1, -1):
            suffix[i] = suffix[i + 1] * counts[i]

        consumed = sum(
            loader.current_index * suffix[i + 1]
            for i, loader in enumerate(self._loaders)
        )
        return max(0, suffix[0] - consumed)



    @property
    def total_count(self) -> int:
        counts = [loader.total_count for loader in self._loaders]
        if self.parallel:
            return min(counts) if counts else 0
        else:
            result = 1
            for c in counts:
                result *= c
            return result

    # ------------------------------------------------------------------
    # Context Manager & Iterator Protocol
    # ------------------------------------------------------------------

    def close(self) -> None:
        """安全释放所有 loader 资源"""
        for loader in self._loaders:
            try:
                loader.save_checkpoint()
                loader.close()
            except Exception as e:
                debug_log(f"PayloadLoader close 异常: {e}")

    def __enter__(self) -> "PayloadLoader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __iter__(self) -> Iterator[dict[str, str]]:
        """支持 for payload in loader: ... 用法"""
        while (payload := self.next()) is not None:
            yield payload

    def __next__(self) -> dict[str, str]:
        payload = self.next()
        if payload is None:
            raise StopIteration
        return payload

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def __repr__(self) -> str:
        mode = "parallel" if self.parallel else "product"
        fields = ", ".join(
            f"{name}={loader!r}"
            for name, loader in zip(self._names, self._loaders)
        )
        return (
            f"<PayloadLoader mode={mode!r} "
            f"total≈{self.total_count} remaining≈{self.remaining_count} "
            f"fields=[{fields}]>"
        )
