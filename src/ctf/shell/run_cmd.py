import os
import signal
import subprocess
import threading
from dataclasses import dataclass

from ctf.utils.log import debug_log


@dataclass
class CommandResult:
    ok: bool
    output: str
    error: str


def run_cmd(command: str, timeout: int = 120) -> CommandResult:
    try:
        result = subprocess.run(  # noqa: S603
            command,
            shell=True,  # noqa: S602
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        cmd_result = CommandResult(
            ok=(result.returncode == 0),
            output=result.stdout.strip(),
            error=result.stderr.strip(),
        )
        debug_log(f"执行命令: command={command}, timeout={timeout}, ok={cmd_result.ok}, returncode={result.returncode}")
        return cmd_result
    except Exception as e:
        debug_log(f"命令执行异常:  command={command}, timeout={timeout} error={e}")
        return CommandResult(ok=False, output="", error=f"[run_cmd] Exception: {e}")


class RunCmd:
    """
    非阻塞命令执行器，支持自动资源清理。

    用法::

        cmd = RunCmd("sleep 5 && echo done")
        ok, msg = cmd.run()
        result = cmd.join()   # 等待完成
        result = cmd.stop()   # 提前终止

    推荐使用上下文管理器，离开时自动 stop() + 清理管道::

        with RunCmd("long_task") as cmd:
            cmd.run()
            result = cmd.join()

    进程以独立 session 启动（start_new_session=True），stop()/join() 超时时
    使用 os.killpg 杀掉整个进程组，确保 shell=True 下的子进程树（如 nc -e bash）
    也能被完整清理。
    """

    def __init__(self, command: str, timeout: int = 300) -> None:
        self.command = command
        self.timeout = timeout

        self._process: subprocess.Popen[str] | None = None
        self._result: CommandResult | None = None
        self._lock = threading.Lock()

        debug_log(f"初始化 RunCmd: command={command}, timeout={timeout}")

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def run(self) -> tuple[bool, str]:
        """启动命令（非阻塞）。返回 (是否成功启动, 消息)。"""
        with self._lock:
            if self._process is not None:
                debug_log("进程已在运行")
                return False, "[RunCmd] 已有进程在运行，请先 stop() 或等待完成"
            try:
                self._process = subprocess.Popen(  # noqa: S603
                    self.command,
                    shell=True,  # noqa: S602
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                assert isinstance(self._process, subprocess.Popen)
                debug_log(f"命令启动成功，PID: {self._process.pid}")
                return True, f"[RunCmd] 命令已启动，PID: {self._process.pid}"
            except Exception as e:
                debug_log(f"命令启动失败: {e}")
                return False, f"[RunCmd] 启动失败: {e}"

    def join(self) -> CommandResult:
        """阻塞等待命令完成，返回结果。可安全多次调用。"""
        debug_log("join() 开始等待命令完成")
        with self._lock:
            if self._process is None:
                debug_log("进程未启动")
                return CommandResult(ok=False, output="", error="[RunCmd] 进程未启动")
            if self._result is not None:
                debug_log("返回已缓存的结果")
                return self._result
            proc = self._process

        try:
            debug_log(f"等待进程完成，timeout={self.timeout}")
            stdout, stderr = proc.communicate(timeout=self.timeout)
            self._result = CommandResult(
                ok=(proc.returncode == 0),
                output=stdout.strip(),
                error=stderr.strip(),
            )
            debug_log(f"进程完成: returncode={proc.returncode}")
        except subprocess.TimeoutExpired:
            debug_log(f"进程超时 ({self.timeout}s)，开始终止")
            self._kill_and_drain(proc)
            self._result = CommandResult(
                ok=False,
                output="",
                error=f"[RunCmd] 命令执行超时 ({self.timeout}s)",
            )
        except Exception as e:
            debug_log(f"join() 异常: {e}")
            self._result = CommandResult(
                ok=False,
                output="",
                error=f"[RunCmd] Exception: {e}",
            )
        assert isinstance(self._result, CommandResult)
        return self._result

    def stop(self) -> CommandResult:
        """
        终止进程组并返回已收集到的输出。

        - 进程已自然结束 → 返回真实结果（含 returncode）
        - 进程仍在运行  → SIGTERM 整个进程组，5 s 后仍存活则 SIGKILL

        可安全多次调用。
        """
        debug_log("stop() 开始终止进程")
        with self._lock:
            if self._process is None:
                return CommandResult(ok=False, output="", error="[RunCmd] 进程未启动")
            if self._result is not None:
                return self._result
            proc = self._process

            if proc.poll() is not None:
                debug_log(
                    f"进程已自然结束，returncode={proc.returncode}"
                )
                self._result = self._collect_finished(proc)
                assert isinstance(self._result, CommandResult)
                return self._result

        result = self._terminate_and_collect(proc)
        self._result = result
        return result

    def reset(self) -> None:
        """停止当前进程并重置状态，允许重新 run()。"""
        debug_log("reset() 重置状态")
        self.stop()
        with self._lock:
            self._process = None
            self._result = None

    def __enter__(self) -> "RunCmd":
        return self

    def __exit__(self, *_: object) -> None:
        """离开 with 块时自动终止进程并释放管道资源。"""
        self.stop()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _pgid(proc: subprocess.Popen[str]) -> int | None:
        """安全获取进程组 ID，进程已消失时返回 None。"""
        try:
            return os.getpgid(proc.pid)
        except OSError:
            return None

    @staticmethod
    def _killpg(pgid: int, sig: signal.Signals) -> None:
        """向进程组发送信号，忽略 ESRCH（进程组已不存在）。"""
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass

    @staticmethod
    def _collect_finished(proc: subprocess.Popen[str]) -> CommandResult:
        """进程已结束但管道尚未读取时调用，排空缓冲区并返回结果。"""
        try:
            stdout, stderr = proc.communicate()
            return CommandResult(
                ok=(proc.returncode == 0),
                output=stdout.strip() if stdout else "",
                error=stderr.strip() if stderr else "",
            )
        except Exception as e:
            return CommandResult(
                ok=False,
                output="",
                error=f"[RunCmd] 读取输出失败: {e}",
            )

    @staticmethod
    def _terminate_and_collect(proc: subprocess.Popen[str]) -> CommandResult:
        """
        向整个进程组发送 SIGTERM，等待 5 s；
        超时后发 SIGKILL 并排空管道。
        """
        stdout_str = ""
        stderr_str = ""
        try:
            pgid = RunCmd._pgid(proc)
            if pgid is not None:
                RunCmd._killpg(pgid, signal.SIGTERM)
            else:
                proc.terminate()

            try:
                stdout, stderr = proc.communicate(timeout=5)
                stdout_str = stdout.strip() if stdout else ""
                stderr_str = stderr.strip() if stderr else ""
            except subprocess.TimeoutExpired:
                RunCmd._kill_and_drain(proc)
        except Exception as e:
            return CommandResult(
                ok=False,
                output="",
                error=f"[RunCmd] 终止失败: {e}",
            )

        error_msg = "[RunCmd] 进程已被终止"
        if stderr_str:
            error_msg = f"{error_msg}\n{stderr_str}"
        return CommandResult(ok=False, output=stdout_str, error=error_msg)

    @staticmethod
    def _kill_and_drain(proc: subprocess.Popen[str]) -> None:
        """
        向整个进程组发送 SIGKILL，然后排空管道。
        防止僵尸进程或管道缓冲区阻塞。
        """
        pgid = RunCmd._pgid(proc)
        if pgid is not None:
            RunCmd._killpg(pgid, signal.SIGKILL)
        else:
            try:
                proc.kill()
            except Exception as e:
                debug_log(str(e))
                pass
        try:
            proc.communicate()
        except Exception as e:
            debug_log(str(e))
            pass