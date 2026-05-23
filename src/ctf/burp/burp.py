import asyncio
import inspect
import time
import traceback
from collections import deque, Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Generic, TypeVar

from rich.columns import Columns
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.rule import Rule
from rich.table import Table
from rich.text import Text


# ══════════════════════════════════════════════════════════════════════════════
# 耗时统计工具
# ══════════════════════════════════════════════════════════════════════════════

def _percentile(data: list[float], p: int) -> float:
    """计算百分位数（线性插值）。data 无需预先排序。"""
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


@dataclass
class StepLatencyStat:
    """单个 step 的耗时累积数据（单位 ms）。"""
    count: int = 0
    total_ms: float = 0.0
    min_ms: float = float("inf")
    max_ms: float = 0.0

    def record(self, ms: float) -> None:
        self.count += 1
        self.total_ms += ms
        if ms < self.min_ms:
            self.min_ms = ms
        if ms > self.max_ms:
            self.max_ms = ms

    def merge(self, other: "StepLatencyStat") -> None:
        """将另一个 stat 合并到自身（用于 Pool 跨 worker 聚合）。"""
        self.count += other.count
        self.total_ms += other.total_ms
        self.min_ms = min(self.min_ms, other.min_ms)
        self.max_ms = max(self.max_ms, other.max_ms)

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "avg_ms": round(self.avg_ms, 3),
            "min_ms": round(self.min_ms, 3) if self.count else 0.0,
            "max_ms": round(self.max_ms, 3),
            "total_ms": round(self.total_ms, 3),
        }


@dataclass
class PayloadLatencyStat:
    """payload 端到端耗时统计（单位 ms），保留原始样本用于百分位计算。"""
    _samples: list[float] = field(default_factory=list)

    def record(self, ms: float) -> None:
        self._samples.append(ms)

    def extend(self, samples: list[float]) -> None:
        self._samples.extend(samples)

    @property
    def count(self) -> int:
        return len(self._samples)

    def to_dict(self) -> dict[str, Any]:
        s = self._samples
        if not s:
            return {}
        return {
            "count": len(s),
            "avg_ms": round(sum(s) / len(s), 3),
            "min_ms": round(min(s), 3),
            "max_ms": round(max(s), 3),
            "p50_ms": round(_percentile(s, 50), 3),
            "p95_ms": round(_percentile(s, 95), 3),
            "p99_ms": round(_percentile(s, 99), 3),
        }

    def samples(self) -> list[float]:
        return list(self._samples)


# ══════════════════════════════════════════════════════════════════════════════
# TypeVar
# ══════════════════════════════════════════════════════════════════════════════

BurpRuntimeT = TypeVar("BurpRuntimeT")
BurpRuntimeStateT = TypeVar("BurpRuntimeStateT")
BurpPayloadStateT = TypeVar("BurpPayloadStateT")


# ══════════════════════════════════════════════════════════════════════════════
# StepAction
# ══════════════════════════════════════════════════════════════════════════════

class StepActionType(Enum):
    SUCCESS = "success"
    SUCCESS_AND_NEXT = "success_and_next"
    FAIL = "fail"
    FAIL_AND_NEXT = "fail_and_next"
    NEXT = "next"
    RETRY = "retry"
    ABORT = "abort"
    STOP = "stop"
    GOTO = "goto"
    RESET_RUNTIME = "reset_runtime"
    RESET_STATE = "reset_state"
    RESET_RUNTIME_AND_STATE = "reset_runtime_and_state"


@dataclass
class StepAction:
    action: StepActionType
    target: int | str | None = None
    message: str = "" # 用于传递错误和崩溃信息

    @classmethod
    def success(cls) -> "StepAction":
        return cls(StepActionType.SUCCESS)

    @classmethod
    def success_and_next(cls) -> "StepAction":
        return cls(StepActionType.SUCCESS_AND_NEXT)

    @classmethod
    def fail(cls, msg: str = "") -> "StepAction":
        return cls(StepActionType.FAIL, message=msg)

    @classmethod
    def fail_and_next(cls, msg: str = "") -> "StepAction":
        return cls(StepActionType.FAIL_AND_NEXT, message=msg)

    @classmethod
    def next(cls) -> "StepAction":
        return cls(StepActionType.NEXT)

    @classmethod
    def retry(cls) -> "StepAction":
        return cls(StepActionType.RETRY)

    @classmethod
    def abort(cls, msg: str = "") -> "StepAction":
        return cls(StepActionType.ABORT, message=msg)

    # 新增stop用于表示安全停止
    @classmethod
    def stop(cls) -> "StepAction":
        return cls(StepActionType.STOP)

    @classmethod
    def goto(cls, target: int | str) -> "StepAction":
        if not isinstance(target, (int, str)):
            raise TypeError(f"goto target must be int or str, got {type(target)!r}")
        return cls(StepActionType.GOTO, target=target)

    @classmethod
    def reset_session(cls) -> "StepAction":
        return cls(StepActionType.RESET_RUNTIME)

    @classmethod
    def reset_state(cls) -> "StepAction":
        return cls(StepActionType.RESET_STATE)

    @classmethod
    def reset_session_and_state(cls) -> "StepAction":
        return cls(StepActionType.RESET_RUNTIME_AND_STATE)

    def __str__(self) -> str:
        if self.action is StepActionType.GOTO:
            return f"StepAction(GOTO → {self.target!r})"
        return f"StepAction({self.action})"


# ══════════════════════════════════════════════════════════════════════════════
# PayloadAction
# ══════════════════════════════════════════════════════════════════════════════

class PayloadActionType(Enum):
    SUCCESS = "success"
    NEXT = "next"
    RETRY = "retry"
    ABORT = "abort"
    STOP = "stop"


@dataclass
class PayloadAction:
    action: PayloadActionType
    payload: dict[str, Any]
    message: str = "" # 用于传递错误和崩溃信息

    @classmethod
    def success(cls, payload: dict[str, Any]) -> "PayloadAction":
        return cls(PayloadActionType.SUCCESS, payload)

    @classmethod
    def next(cls, msg: str = "") -> "PayloadAction":
        return cls(PayloadActionType.NEXT, {}, msg)

    @classmethod
    def retry(cls) -> "PayloadAction":
        return cls(PayloadActionType.RETRY, {})

    @classmethod
    def abort(cls, msg: str = "") -> "PayloadAction":
        return cls(PayloadActionType.ABORT, {}, msg)
    @classmethod
    def stop(cls) -> "PayloadAction":
        return cls(PayloadActionType.STOP, {})


# ══════════════════════════════════════════════════════════════════════════════
# BurpState
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BurpState(Generic[BurpRuntimeStateT, BurpPayloadStateT]):
    """
    双层状态容器。

    runtime : BurpRuntimeStateT
        长期状态，跨所有 payload 存活（CSRF token、已认证 cookie jar 等）。
    payload : BurpPayloadStateT
        短期状态，每个 payload 开始时重建，结束时丢弃。
    """
    runtime: BurpRuntimeStateT
    payload: BurpPayloadStateT

    def reset_payload(self, new_payload: BurpPayloadStateT) -> None:
        self.payload = new_payload


# ══════════════════════════════════════════════════════════════════════════════
# Step
# ══════════════════════════════════════════════════════════════════════════════

StepHandler = Callable[
    [
        Any,                    # BurpRuntimeT
        BurpState[Any, Any],    # 双层状态
        dict[str, Any],         # 原始 payload
    ],
    Awaitable[tuple[StepAction, str]],  # (动作, 说明)
]


class Step:
    def __init__(
            self,
            name: str,
            handler: StepHandler,
            timeout: float = 5,
            max_retries: int = 5,
            retry_delay: float = 0.01,
            abort_on_max_retries: bool = True,
            abort_on_step_error: bool = True,
    ) -> None:
        self.name = name
        self.handler = handler
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.abort_on_max_retries = abort_on_max_retries
        self.abort_on_step_error = abort_on_step_error


# ══════════════════════════════════════════════════════════════════════════════
# BurpAsync  —  单 worker 顺序执行
# ══════════════════════════════════════════════════════════════════════════════

class BurpAsync(Generic[BurpRuntimeT, BurpRuntimeStateT, BurpPayloadStateT]):
    """
    单 worker 顺序爆破框架。

    耗时统计：
        burp.step_stats         → dict[step_name, StepLatencyStat]
        burp.payload_stat       → PayloadLatencyStat
        burp.step_stats_dict()  → dict[step_name, dict]  （可序列化）
        burp.payload_stat_dict()→ dict                   （含百分位）

    lifecycle:
        await burp.start(stop_on_first=True)
        await burp.join()
        burp.results()

        # 或一步到位：
        results = await burp.run(stop_on_first=True)
    """

    def __init__(
            self,
            payload: Callable[[], Awaitable[dict[str, Any] | None]],
            build_runtime: Callable[[], Awaitable[BurpRuntimeT]],
            build_state: Callable[[], tuple[
                Callable[[], Awaitable[BurpRuntimeStateT]],
                Callable[[], Awaitable[BurpPayloadStateT]]
            ]],
            steps: list[Step],
            payload_on_next: Callable[[], Awaitable[None]] | None = None,
            payload_max_retries: int = 3,
            payload_retry_delay: float = 0.5,
            payload_abort_on_max_retries: bool = True,
            on_log: Callable[[str], None] | None = None,
            debug_log: bool = False,
            MAX_RESET_PER_PAYLOAD: int = 16,
            MAX_GOTO_PER_PAYLOAD: int = 30,
    ) -> None:
        if not steps:
            raise ValueError("steps must not be empty")

        self._payload_gen = payload
        self._build_runtime = build_runtime
        self._build_runtime_state, self._build_payload_state = build_state()

        self._steps: list[Step] = steps
        self._steps_by_index: dict[int, Step] = {
            i: s for i, s in enumerate(steps, start=1)
        }
        self._steps_by_name: dict[str, Step] = {s.name: s for s in steps}

        self._payload_on_next = payload_on_next
        self._payload_max_retries = payload_max_retries
        self._payload_retry_delay = payload_retry_delay
        self._payload_abort_on_max_retries = payload_abort_on_max_retries
        self._on_log = on_log
        self._debug_log = debug_log
        self._MAX_RESET_PER_PAYLOAD = MAX_RESET_PER_PAYLOAD
        self._MAX_GOTO_PER_PAYLOAD = MAX_GOTO_PER_PAYLOAD

        # 运行时（懒初始化）
        self._runtime: BurpRuntimeT | None = None
        self._state: BurpState[BurpRuntimeStateT, BurpPayloadStateT] | None = None

        # 控制
        self._results: list[dict[str, Any]] = []
        self._stop_event = asyncio.Event()
        self._done_event = asyncio.Event()
        self._task: asyncio.Task | None = None

        # Pool 通过此标志感知 ABORT
        self.stop_triggered: bool = False

        # ── 耗时统计 ──────────────────────────────────────────────────────────
        # key = step.name；每次 handler 调用（含 RETRY 的每一次）各计一次
        self._step_stats: dict[str, StepLatencyStat] = {
            s.name: StepLatencyStat() for s in steps
        }
        # payload 端到端耗时：从 _process_payload_with_retry 进入到返回
        self._payload_stat: PayloadLatencyStat = PayloadLatencyStat()

        # 用于记录当前失败原因和频率
        self.failure_stats: Counter[str] = Counter()

    # ── 耗时统计公开属性 ──────────────────────────────────────────────────────

    @property
    def step_stats(self) -> dict[str, StepLatencyStat]:
        """各 step 耗时统计对象（直接引用，实时可读）。"""
        return self._step_stats

    @property
    def payload_stat(self) -> PayloadLatencyStat:
        """payload 端到端耗时统计对象（直接引用，实时可读）。"""
        return self._payload_stat

    def step_stats_dict(self) -> dict[str, dict[str, Any]]:
        """返回可序列化的 step 耗时快照。"""
        return {name: stat.to_dict() for name, stat in self._step_stats.items()}

    def payload_stat_dict(self) -> dict[str, Any]:
        """返回可序列化的 payload 耗时快照（含 p50/p95/p99）。"""
        return self._payload_stat.to_dict()

    # ── 日志 ──────────────────────────────────────────────────────────────────

    def _info(self, msg: str) -> None:
        if self._on_log:
            self._on_log(msg)

    def _debug(self, msg: str) -> None:
        if self._on_log and self._debug_log:
            self._on_log(msg)

    # ── 初始化 / 重置 ─────────────────────────────────────────────────────────

    async def _ensure_runtime(self) -> None:
        if self._runtime is None and self._build_runtime:
            self._runtime = await self._build_runtime()

    async def _ensure_state(self) -> None:
        if self._state is None:
            self._state = BurpState(
                runtime=await self._build_runtime_state(),
                payload=await self._build_payload_state(),
            )

    async def _reset_runtime(self) -> None:
        await self._close_runtime()
        if self._build_runtime:
            self._runtime = await self._build_runtime()

    async def _reset_runtime_state(self) -> None:
        assert self._state is not None
        self._state = BurpState(
            runtime=await self._build_runtime_state(),
            payload=self._state.payload,
        )

    async def _reset_payload_state(self) -> None:
        assert self._state is not None
        self._state.reset_payload(await self._build_payload_state())

    async def _close_runtime(self) -> None:
        if self._runtime is not None and hasattr(self._runtime, "close"):
            try:
                close_fn = self._runtime.close  # type: ignore[union-attr]
                if inspect.iscoroutinefunction(close_fn):
                    await close_fn()
                else:
                    close_fn()
            except Exception as e:
                self._debug(f"Error closing runtime: {e}")
        self._runtime = None

    # ── step 索引解析 ─────────────────────────────────────────────────────────

    def _resolve_step_index(self, target: int | str | None) -> int:
        if isinstance(target, int):
            if target not in self._steps_by_index:
                raise ValueError(f"GOTO index {target} out of range")
            return target
        if isinstance(target, str):
            if target not in self._steps_by_name:
                raise ValueError(f"GOTO name {target!r} not found")
            for idx, step in self._steps_by_index.items():
                if step.name == target:
                    return idx
        raise ValueError(f"Invalid GOTO target: {target!r}")

    # ── 单步执行（含 step 级重试）────────────────────────────────────────────
    #
    # 计时策略：
    #   - 每次 handler 调用单独计时（t0 在循环体内），RETRY 的每次调用独立记录。
    #   - 这样 avg_ms 反映的是"单次 handler 调用的平均耗时"，
    #     count 包含所有尝试次数，可用来衡量重试率。
    #   - sleep 时间不计入：sleep 发生在本次计时结束后、下次 t0 之前。

    async def _execute_step(
            self,
            step: Step,
            payload: dict[str, Any],
    ) -> tuple[StepAction, str]:
        assert self._runtime is not None
        assert self._state is not None

        _last_action = StepAction.abort("异常step执行")
        _last_msg = ""
        stat = self._step_stats[step.name]

        for attempt in range(1, step.max_retries + 1):
            t0 = time.perf_counter()
            try:
                action, msg = await asyncio.wait_for(
                    step.handler(self._runtime, self._state, payload),
                    timeout=step.timeout
                )
                elapsed_ms = (time.perf_counter() - t0) * 1000
                stat.record(elapsed_ms)
                if not action.message.strip():
                    action.message = msg

                _last_action, _last_msg = action, msg

                if action.action != StepActionType.RETRY:
                    return action, msg

                self._debug(f"[{step.name}] RETRY #{attempt}/{step.max_retries}: {msg}")

            except asyncio.TimeoutError:
                elapsed_ms = (time.perf_counter() - t0) * 1000
                stat.record(elapsed_ms)

                self._debug(
                    f"[{step.name}] Timeout attempt {attempt}/{step.max_retries}"
                )
                _last_msg = "timeout"

                if step.abort_on_step_error:
                    return StepAction.abort(f"step[{step.name}] timeout"), f"step[{step.name}] timeout"
                _last_action = StepAction.retry()

            except Exception as e:
                elapsed_ms = (time.perf_counter() - t0) * 1000
                stat.record(elapsed_ms)

                self._debug(
                    f"[{step.name}] Exception attempt {attempt}/{step.max_retries}: "
                    f"{e}\n{traceback.format_exc()}"
                )
                _last_msg = str(e)
                if step.abort_on_step_error:
                    return StepAction.abort(f"step error: {e}"), f"step error: {e}"
                _last_action = StepAction.retry()

            await asyncio.sleep(step.retry_delay)

        self._info(f"[{step.name}] max_retries exhausted")
        if step.abort_on_max_retries:
            return StepAction.abort(f"max retries exhausted: {_last_msg}"), f"max retries exhausted: {_last_msg}"
        return StepAction.next(), f"retries exhausted, skipping: {_last_msg}"

    # ── payload 状态机 ────────────────────────────────────────────────────────

    async def _process_payload(self, payload: dict[str, Any]) -> PayloadAction:
        assert self._state is not None
        await self._reset_payload_state()

        step_idx = 1
        hit = False
        action_msg = "step down,unknown fail,may be max_retries"
        reset_count = 0
        goto_count = 0

        while step_idx <= len(self._steps):
            if self._stop_event.is_set():
                return PayloadAction.stop()

            step = self._steps_by_index[step_idx]
            action, msg = await self._execute_step(step, payload)

            self._debug(f"[{step.name}] → {action} | {msg}")
            at = action.action

            if at in (
                    StepActionType.RESET_RUNTIME,
                    StepActionType.RESET_STATE,
                    StepActionType.RESET_RUNTIME_AND_STATE,
            ):
                reset_count += 1
                if reset_count > self._MAX_RESET_PER_PAYLOAD:
                    msg = (
                        f"[{step.name}] RESET loop detected "
                        f"(>{self._MAX_RESET_PER_PAYLOAD} resets), aborting payload"
                    )
                    self._info(msg)
                    return PayloadAction.abort(msg)
                if at in (StepActionType.RESET_RUNTIME, StepActionType.RESET_RUNTIME_AND_STATE):
                    await self._reset_runtime()
                if at in (StepActionType.RESET_STATE, StepActionType.RESET_RUNTIME_AND_STATE):
                    await self._reset_runtime_state()
                continue

            elif at == StepActionType.ABORT:
                return PayloadAction.abort(action.message)

            elif at == StepActionType.GOTO:
                goto_count += 1
                if goto_count > self._MAX_GOTO_PER_PAYLOAD:
                    msg = (
                        f"[{step.name}] GOTO loop detected "
                        f"(>{self._MAX_GOTO_PER_PAYLOAD} jumps), aborting payload"
                    )
                    self._info(msg)
                    return PayloadAction.abort(msg)
                try:
                    step_idx = self._resolve_step_index(action.target)
                except ValueError as e:
                    msg = f"[{step.name}] Invalid GOTO target: {e}"
                    self._info(msg)
                    return PayloadAction.abort(msg)
                continue

            elif at == StepActionType.SUCCESS:
                return PayloadAction.success(payload)

            elif at == StepActionType.SUCCESS_AND_NEXT:
                hit = True
                step_idx += 1

            elif at == StepActionType.FAIL:
                return PayloadAction.next(action.message)

            elif at == StepActionType.FAIL_AND_NEXT:
                action_msg = action.message
                step_idx += 1

            elif at == StepActionType.NEXT:
                step_idx += 1

            elif at == StepActionType.STOP:
                return PayloadAction.stop()

            else:
                self._info(f"Unknown action {at}, treating as NEXT")
                step_idx += 1

        return PayloadAction.success(payload) if hit else PayloadAction.next(action_msg)

    # ── payload 级重试 ────────────────────────────────────────
    #
    # 计时点选在此处（而非 _process_payload），是为了把 payload 级重试
    # 和 _reset_payload_state 的开销也纳入端到端耗时，更接近用户视角。

    async def _process_payload_with_retry(
            self, payload: dict[str, Any]
    ) -> PayloadAction:
        t0 = time.perf_counter()
        try:
            for attempt in range(1, self._payload_max_retries + 1):
                at = await self._process_payload(payload)
                if at.action != PayloadActionType.RETRY:
                    return at
                self._info(f"Payload retry #{attempt}/{self._payload_max_retries}")
                await asyncio.sleep(self._payload_retry_delay)
            if self._payload_abort_on_max_retries:
                msg = "Payload max retries exhausted"
                self._info(msg)
                return PayloadAction.abort(msg)
            return PayloadAction.next("未知Payload级错误")
        finally:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            self._payload_stat.record(elapsed_ms)

    # ── 主循环 ────────────────────────────────────────────────────────────────

    async def _run_loop(self, stop_on_first: bool) -> None:
        try:
            await self._ensure_runtime()
            await self._ensure_state()

            while not self._stop_event.is_set():
                payload = await self._payload_gen()
                if payload is None:
                    self._debug("Payload exhausted.")
                    break

                start = time.time()
                at = await self._process_payload_with_retry(payload)

                # ==========================================
                # 统计 NEXT 和 ABORT 的失败原因频率
                # ==========================================
                if at.action in (PayloadActionType.NEXT, PayloadActionType.ABORT):
                    fail_msg = at.message.strip() if at.message.strip() else "Unknown Error"
                    self.failure_stats[fail_msg] += 1
                # ==========================================

                status_icon = '!' if at.action.name == 'SUCCESS' else '*'
                elapsed_time = f"{(time.time() - start):.2f}s"
                payload_str = ", ".join([f"{k}={v}" for k, v in payload.items()])


                self._info(
                    f"[{status_icon}]{elapsed_time} {payload_str[:50]:<50} {at.message}"
                )

                if self._payload_on_next:
                    await self._payload_on_next()

                if at.action == PayloadActionType.SUCCESS:
                    self._results.append(at.payload)
                    if stop_on_first:
                        break

                if at.action == PayloadActionType.ABORT:
                    self._debug("Task aborted.")
                    self.stop_triggered = True
                    break

                if at.action == PayloadActionType.STOP:
                    self.stop_triggered = True
                    break

        except Exception as e:
            self._debug(f"Unhandled error in run loop: {e}\n{traceback.format_exc()}")
        finally:
            await self._close_runtime()
            self._done_event.set()

    # ── 公开接口 ──────────────────────────────────────────────────────────────

    async def start(self, stop_on_first: bool = True) -> None:
        """后台启动，立即返回。"""
        if self._task and not self._task.done():
            raise RuntimeError("BurpAsync is already running")
        self._results.clear()
        self.stop_triggered = False
        self._stop_event.clear()
        self._done_event.clear()
        self._task = asyncio.create_task(self._run_loop(stop_on_first))

    async def stop(self) -> None:
        """请求停止，不阻塞。"""
        self._stop_event.set()

    async def join(self) -> None:
        """等待运行完成。"""
        await self._done_event.wait()

    async def run(self, stop_on_first: bool = True) -> list[dict[str, Any]]:
        """启动并阻塞到完成，返回结果列表。"""
        await self.start(stop_on_first)
        await self.join()
        return list(self._results)

    def results(self) -> list[dict[str, Any]]:
        """随时可调用，返回当前已收集结果的快照。"""
        return list(self._results)


# ══════════════════════════════════════════════════════════════════════════════
# BurpAsyncPool  —  多 worker 并发调度 + rich UI
# ══════════════════════════════════════════════════════════════════════════════

class BurpAsyncPool(Generic[BurpRuntimeT, BurpRuntimeStateT, BurpPayloadStateT]):
    """
    并发调度器。多个 worker 共享同一 payload 队列，各自独立持有
    runtime / state / step 执行上下文。

    运行时展示 rich TUI：
      ┌─ Log ──────────────────────────────────────┐
      │  (最近 N 行日志)                            │
      └────────────────────────────────────────────┘
      ┌─ Latency ──────────────────────────────────┐
      │  Step    N    Avg ms  Min ms  Max ms        │
      │  ...                                        │
      │  payload e2e  avg  min  max  p50  p95  p99  │
      └────────────────────────────────────────────┘
      [进度条 / 速度]

    耗时统计（运行结束后可查）：
        pool.pool_step_stats         → dict[step_name, StepLatencyStat]
        pool.pool_payload_stat       → PayloadLatencyStat
        pool.pool_step_stats_dict()  → dict（可序列化）
        pool.pool_payload_stat_dict()→ dict（含 p50/p95/p99）
    """

    def __init__(
            self,
            payload: Callable[[], Awaitable[dict[str, Any] | None]],
            build_runtime: Callable[[], Awaitable[BurpRuntimeT]],
            build_state: Callable[[], tuple[
                Callable[[], Awaitable[BurpRuntimeStateT]],
                Callable[[], Awaitable[BurpPayloadStateT]]
            ]],
            steps: list[Step],
            workers: int = 5,
            total_payload: int | None = None,
            payload_on_complete: Callable[[], Awaitable[None]] | None = None,
            payload_max_retries: int = 5,
            payload_retry_delay: float = 0.01,
            payload_abort_on_max_retries: bool = True,
            on_log: Callable[[str], None] | None = None,
            debug_log: bool = False,
            MAX_RESET_PER_PAYLOAD: int = 16,
            MAX_GOTO_PER_PAYLOAD: int = 30,
    ) -> None:
        self._payload_gen = payload
        self._build_runtime = build_runtime
        self._build_state = build_state
        self._steps = steps
        self._workers_count = workers
        self._total_payload = total_payload

        self._user_payload_on_complete = payload_on_complete
        self._payload_max_retries = payload_max_retries
        self._payload_retry_delay = payload_retry_delay
        self._payload_abort_on_max_retries = payload_abort_on_max_retries
        self._external_on_log = on_log
        self._debug_log = debug_log
        self._MAX_RESET_PER_PAYLOAD = MAX_RESET_PER_PAYLOAD
        self._MAX_GOTO_PER_PAYLOAD = MAX_GOTO_PER_PAYLOAD
        self._log_lines: int = 12

        # 运行时状态（每次 _reset 重建）
        self._payload_lock: asyncio.Lock
        self._results_lock: asyncio.Lock
        self._stop_event: asyncio.Event
        self._done_event: asyncio.Event

        self._payload_exhausted: bool = False
        self._results: list[dict[str, Any]] = []
        self._task: asyncio.Task | None = None

        # rich TUI 状态
        self._console = Console()
        self._log_queue: deque[str] = deque(maxlen=self._log_lines)
        self._progress: Progress | None = None
        self._progress_task_id: TaskID | None = None

        # 速度统计
        self._speed_timestamps: deque[float] = deque()
        self._processed_count: int = 0

        # ── Pool 级耗时统计（跨所有 worker 聚合）─────────────────────────────
        self._pool_step_stats: dict[str, StepLatencyStat] = {}
        self._pool_payload_stat: PayloadLatencyStat = PayloadLatencyStat()
        self._pool_failure_stats: Counter[str] = Counter()
        # 用于 TUI 实时刷新：保存各活跃 worker 的 BurpAsync 引用
        self._active_workers: list[BurpAsync] = []
        self._active_workers_lock: asyncio.Lock = asyncio.Lock()

    # ── Pool 级耗时统计公开属性 ───────────────────────────────────────────────

    @property
    def pool_step_stats(self) -> dict[str, StepLatencyStat]:
        """各 step 的聚合耗时统计（运行结束后完整）。"""
        return self._pool_step_stats

    @property
    def pool_payload_stat(self) -> PayloadLatencyStat:
        """payload 端到端耗时聚合统计（运行结束后完整）。"""
        return self._pool_payload_stat

    @property
    def pool_failure_stats(self) -> Counter[str]:
        """失败类型频率统计（运行结束后完整）。"""
        return self._pool_failure_stats

    def pool_step_stats_dict(self) -> dict[str, dict[str, Any]]:
        """返回可序列化的聚合 step 耗时快照。"""
        return {name: stat.to_dict() for name, stat in self._pool_step_stats.items()}

    def pool_payload_stat_dict(self) -> dict[str, Any]:
        """返回可序列化的聚合 payload 耗时快照（含 p50/p95/p99）。"""
        return self._pool_payload_stat.to_dict()

    def _snapshot_step_stats(self) -> dict[str, StepLatencyStat]:
        """
        实时快照：合并所有活跃 worker 当前的 step_stats（不加锁，TUI 刷新用）。
        仅在 _build_layout 中调用，轻微不一致可接受。
        """
        merged: dict[str, StepLatencyStat] = {}

        # 已完成 worker 的聚合数据
        for name, stat in self._pool_step_stats.items():
            merged[name] = StepLatencyStat(
                count=stat.count,
                total_ms=stat.total_ms,
                min_ms=stat.min_ms,
                max_ms=stat.max_ms,
            )

        # 活跃 worker 的实时数据
        for burp in self._active_workers:
            for name, stat in burp.step_stats.items():
                if name not in merged:
                    merged[name] = StepLatencyStat()
                m = merged[name]
                m.count += stat.count
                m.total_ms += stat.total_ms
                m.min_ms = min(m.min_ms, stat.min_ms)
                m.max_ms = max(m.max_ms, stat.max_ms)
        return merged

    def _snapshot_payload_stat(self) -> PayloadLatencyStat:
        """实时快照：合并所有活跃 worker 的 payload_stat。"""
        snap = PayloadLatencyStat()
        snap.extend(self._pool_payload_stat.samples())
        for burp in self._active_workers:
            snap.extend(burp.payload_stat.samples())
        return snap

    def _snapshot_failure_stats(self) -> Counter[str]:
        """实时快照：合并所有活跃 worker 的失败统计。"""
        merged = Counter(self._pool_failure_stats)
        for burp in self._active_workers:
            merged.update(burp.failure_stats)
        return merged

    # ── 重置 ──────────────────────────────────────────────────────────────────

    def _reset(self) -> None:
        self._payload_lock = asyncio.Lock()
        self._results_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._done_event = asyncio.Event()
        self._active_workers_lock = asyncio.Lock()
        self._payload_exhausted = False
        self._results = []
        self._log_queue = deque(maxlen=self._log_lines)
        self._speed_timestamps = deque()
        self._processed_count = 0
        self._pool_step_stats = {}
        self._pool_payload_stat = PayloadLatencyStat()
        self._pool_failure_stats = Counter()
        self._active_workers = []

    # ── 日志 ──────────────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        self._log_queue.append(msg)
        if self._external_on_log:
            self._external_on_log(f"[BurpAsyncPool] {msg}")

    # ── 速度统计 ──────────────────────────────────────────────────────────────

    def _record_tick(self) -> None:
        now = time.monotonic()
        self._speed_timestamps.append(now)
        self._processed_count += 1
        cutoff = now - 5.0
        while self._speed_timestamps and self._speed_timestamps[0] < cutoff:
            self._speed_timestamps.popleft()

    def _current_speed(self) -> float:
        n = len(self._speed_timestamps)
        if n < 2:
            return 0.0
        elapsed = self._speed_timestamps[-1] - self._speed_timestamps[0]
        return (n - 1) / elapsed if elapsed > 0 else 0.0

    # ── rich UI 构建 ──────────────────────────────────────────────────────────

    def _build_latency_panel(self) -> Panel:
        """构建实时耗时统计面板。"""
        step_snap = self._snapshot_step_stats()
        payload_snap = self._snapshot_payload_stat()

        table = Table(
            show_header=True,
            header_style="bold magenta",
            box=None,
            padding=(0, 1),
        )
        table.add_column("Step", style="cyan", min_width=18, no_wrap=True)
        table.add_column("N", justify="right", style="white")
        table.add_column("Avg ms", justify="right", style="green")
        table.add_column("Min ms", justify="right", style="blue")
        table.add_column("Max ms", justify="right", style="red")

        # 按步骤定义顺序排列（而非字典插入顺序）
        step_names_ordered = [s.name for s in self._steps]
        for name in step_names_ordered:
            stat = step_snap.get(name)
            if stat is None or stat.count == 0:
                table.add_row(name, "0", "-", "-", "-")
            else:
                table.add_row(
                    name,
                    str(stat.count),
                    f"{stat.avg_ms:.1f}",
                    f"{stat.min_ms:.1f}",
                    f"{stat.max_ms:.1f}",
                )

        # payload 端到端行
        pd = payload_snap.to_dict()
        if pd:
            table.add_row(
                "[bold yellow]payload e2e[/bold yellow]",
                str(pd["count"]),
                f"[yellow]{pd['avg_ms']:.1f}[/yellow]",
                f"[yellow]{pd['min_ms']:.1f}[/yellow]",
                f"[yellow]{pd['max_ms']:.1f}[/yellow]",
            )
            # p50/p95/p99 附加说明行
            pct_text = (
                f"  [dim]p50={pd['p50_ms']:.1f}ms  "
                f"p95={pd['p95_ms']:.1f}ms  "
                f"p99={pd['p99_ms']:.1f}ms[/dim]"
            )
            table.add_row(pct_text, "", "", "", "")
        else:
            table.add_row("[dim]payload e2e[/dim]", "0", "-", "-", "-")

        return Panel(
            table,
            title="[bold yellow]Latency[/bold yellow]",
            border_style="yellow",
        )

    def _build_failure_stats_panel(self) -> Panel:
        """构建失败类型频率统计面板。"""
        failure_snap = self._snapshot_failure_stats()

        if not failure_snap:
            return Panel(
                Text("No failures yet", style="dim green"),
                title="[bold red]Failure Statistics[/bold red]",
                border_style="red",
            )

        table = Table(
            show_header=True,
            header_style="bold magenta",
            box=None,
            padding=(0, 1),
        )
        table.add_column("Failure Type", style="cyan", no_wrap=False)
        table.add_column("Count", justify="right", style="red")
        table.add_column("Percentage", justify="right", style="yellow")

        total = sum(failure_snap.values())
        # 按频率降序排列，只显示前10个
        for msg, count in failure_snap.most_common(10):
            percentage = (count / total * 100) if total > 0 else 0
            # 截断过长的错误消息
            display_msg = msg[:60] + "..." if len(msg) > 60 else msg
            table.add_row(
                display_msg,
                str(count),
                f"{percentage:.1f}%"
            )

        # 如果有超过10个失败类型，显示统计
        if len(failure_snap) > 10:
            others_count = sum(count for msg, count in failure_snap.items()
                               if msg not in [m for m, _ in failure_snap.most_common(10)])
            table.add_row(
                "[dim]... others ...[/dim]",
                f"[dim]{others_count}[/dim]",
                "[dim]-[/dim]"
            )

        return Panel(
            table,
            title=f"[bold red]Failure Statistics (Total: {total})[/bold red]",
            border_style="red",
        )

    def _build_layout(self) -> Group:
        log_text = Text()
        lines = list(self._log_queue)
        while len(lines) < self._log_lines:
            lines.insert(0, "")

        for line in lines:
            # 去掉 \t，直接换行
            log_text.append(line + "\n", style="dim white")

        log_panel = Panel(
            log_text,
            title=f"[bold cyan]Running {self._workers_count} Worker [/bold cyan]",
            border_style="cyan",
            height=self._log_lines + 2,
            expand=True
        )

        renderables: list[RenderableType] = [
            Rule("[bold cyan]BurpAsyncPool[/bold cyan]"),
            log_panel,
            self._build_latency_panel(),
            self._build_failure_stats_panel()
        ]

        if self._progress is not None:
            renderables.append(self._progress)

        return Group(*renderables)

    def _make_progress(self) -> Progress:
        if self._total_payload is not None:
            return Progress(
                SpinnerColumn(),
                TextColumn("[bold cyan]{task.description}"),
                BarColumn(bar_width=40),
                MofNCompleteColumn(),
                TextColumn("[green]{task.fields[speed]:.1f} req/s"),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                console=self._console,
                transient=False,
            )
        else:
            return Progress(
                SpinnerColumn(),
                TextColumn("[bold cyan]{task.description}"),
                TextColumn("[yellow]{task.completed} processed"),
                TextColumn("[green]{task.fields[speed]:.1f} req/s"),
                TimeElapsedColumn(),
                console=self._console,
                transient=False,
            )

    # ── 共享 payload 分发 ─────────────────────────────────────────────────────

    async def _next_payload(self) -> dict[str, Any] | None:
        if self._payload_exhausted:
            return None
        if self._stop_event.is_set():
            return None
        async with self._payload_lock:
            if self._payload_exhausted:
                return None
            payload = await self._payload_gen()
            if payload is None:
                self._payload_exhausted = True
                return None
            return payload

    # ── 进度 + 用户回调 ───────────────────────────────────────────────────────

    async def _on_payload_complete(self) -> None:
        self._record_tick()
        if self._progress is not None and self._progress_task_id is not None:
            self._progress.update(
                self._progress_task_id,
                advance=1,
                speed=self._current_speed(),
            )
        if self._user_payload_on_complete:
            await self._user_payload_on_complete()

    # ── worker 耗时聚合 ───────────────────────────────────────────────────────

    def _aggregate_worker_stats(self, burp: BurpAsync) -> None:
        """
        将已完成 worker 的耗时数据合并到 pool 级统计中。
        须在 _results_lock 持有期间调用（与结果收集共用同一把锁）。
        """
        for name, stat in burp.step_stats.items():
            if name not in self._pool_step_stats:
                self._pool_step_stats[name] = StepLatencyStat()
            self._pool_step_stats[name].merge(stat)
        self._pool_payload_stat.extend(burp.payload_stat.samples())
        self._pool_failure_stats.update(burp.failure_stats)

    # ── 单个 worker ───────────────────────────────────────────────────────────

    async def _run_worker(self, worker_id: int, stop_on_first: bool) -> None:
        self._log(f"Worker #{worker_id} starting")

        burp: BurpAsync = BurpAsync(
            payload=self._next_payload,
            build_runtime=self._build_runtime,
            build_state=self._build_state,
            steps=self._steps,
            payload_on_next=self._on_payload_complete,
            payload_max_retries=self._payload_max_retries,
            payload_retry_delay=self._payload_retry_delay,
            payload_abort_on_max_retries=self._payload_abort_on_max_retries,
            on_log=lambda msg: self._log(f"W#{worker_id:<4}{msg}"),
            debug_log=self._debug_log,
            MAX_RESET_PER_PAYLOAD=self._MAX_RESET_PER_PAYLOAD,
            MAX_GOTO_PER_PAYLOAD=self._MAX_GOTO_PER_PAYLOAD,
        )

        # 注册为活跃 worker，供 TUI 实时读取耗时
        async with self._active_workers_lock:
            self._active_workers.append(burp)

        async def _stop_bridge() -> None:
            await self._stop_event.wait()
            await burp.stop()

        bridge_task = asyncio.create_task(_stop_bridge())

        try:
            await burp.start(stop_on_first=stop_on_first)
            await burp.join()

            worker_results = burp.results()
            async with self._results_lock:
                if worker_results:
                    self._results.extend(worker_results)
                    if stop_on_first:
                        self._stop_event.set()

            if burp.stop_triggered:
                self._stop_event.set()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._log(f"Worker #{worker_id} error: {e}")
        finally:
            bridge_task.cancel()
            try:
                await bridge_task
            except asyncio.CancelledError:
                pass

            # 这样无论 Worker 是正常结束还是被 Ctrl+C 中断，已经跑出的数据都会并入 Pool
            async with self._active_workers_lock:
                async with self._results_lock:
                    self._aggregate_worker_stats(burp)
                # 从活跃列表移除
                try:
                    self._active_workers.remove(burp)
                except ValueError:
                    pass

    # ── Pool 主调度 ───────────────────────────────────────────────────────────

    async def _run_pool(self, stop_on_first: bool) -> None:
        self._progress = self._make_progress()
        assert self._progress is not None
        self._progress_task_id = self._progress.add_task(
            description="Burping",
            total=self._total_payload,
            speed=0.0,
        )

        with Live(
                self._build_layout(),
                console=self._console,
                refresh_per_second=10,
                screen=False,
        ) as live:
            async def _refresh_loop() -> None:
                while not self._done_event.is_set():
                    live.update(self._build_layout())
                    await asyncio.sleep(0.03)
                live.update(self._build_layout())

            refresh_task = asyncio.create_task(_refresh_loop())

            try:
                worker_tasks = [
                    asyncio.create_task(
                        self._run_worker(worker_id=i, stop_on_first=stop_on_first)
                    )
                    for i in range(1, self._workers_count + 1)
                ]
                await asyncio.gather(*worker_tasks)
            except Exception as e:
                self._log(f"Pool error: {e}")
            finally:
                self._done_event.set()
                await refresh_task

        self._print_results()

    def _print_results(self) -> None:
        results = self._results

        if not results:
            self._console.print("[yellow]No results found.[/yellow]")
        else:
            panels = []
            for i, ctx in enumerate(results, start=1):
                # 内部使用无边框 Table 使 Key-Value 对齐
                res_table = Table(show_header=False, box=None, padding=(0, 2))
                res_table.add_column("Key", style="cyan bold", justify="right")
                res_table.add_column("Value", style="white")

                for k, v in ctx.items():
                    res_table.add_row(str(k), str(v))

                # 外层使用 Panel 包裹成一个 "块(Block)"
                panel = Panel(
                    res_table,
                    title=f"[bold green]Result #{i}[/bold green]",
                    border_style="green",
                    expand=False
                )
                panels.append(panel)
            self._console.print(Panel(
                Columns(panels),
                title=f"[bold green]Found {len(results)} result:[/bold green]",
                border_style="green",
            ))


    # ── 公开接口 ──────────────────────────────────────────────────────────────

    async def start(self, stop_on_first: bool = True) -> None:
        """后台启动，立即返回。"""
        if self._task and not self._task.done():
            raise RuntimeError("BurpAsyncPool is already running")
        self._reset()
        self._task = asyncio.create_task(self._run_pool(stop_on_first))

    async def stop(self) -> None:
        """请求停止所有 worker。"""
        self._stop_event.set()

    async def join(self) -> None:
        """等待 pool 完成。"""
        await self._done_event.wait()

    async def run(self, stop_on_first: bool = True, log_lines: int = 20) -> list[dict[str, Any]]:
        """
        启动并阻塞到完成，返回结果列表。
        运行期间展示 rich TUI（日志 + 实时耗时面板 + 进度/速度），
        结束后打印汇总结果。
        """
        self._log_lines = log_lines
        self._log_queue = deque(maxlen=self._log_lines)
        if self._task and not self._task.done():
            raise RuntimeError("BurpAsyncPool is already running")
        self._reset()
        await self._run_pool(stop_on_first)
        return list(self._results)

    def results(self) -> list[dict[str, Any]]:
        """返回当前已收集结果的快照（可在运行中随时调用）。"""
        return list(self._results)


def demo() -> None:
    pass


if __name__ == "__main__":
    demo()