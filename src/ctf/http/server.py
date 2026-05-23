import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ctf.utils.log import debug_log


@dataclass
class EchoRequest:
    ip: str
    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    timestamp: float

    def __str__(self) -> str:
        headers_str = "\n".join(f"        {k}: {v}" for k, v in self.headers.items())
        body_repr = self.body.decode(errors="replace") if self.body else ""

        return (
            f"{self.method} {self.path}\n"
            f"    from: {self.ip}\n"
            f"    time: {self.timestamp:.3f}\n"
            f"    headers:\n{headers_str if headers_str else '        (none)'}\n"
            f"    body:\n        {body_repr if body_repr else '(empty)'}"
        )


class _BaseHttpServer:
    def __init__(self, port: int = 0):
        self.host = "0.0.0.0"
        self._port = port  # 请求端口，0 = 由 OS 自动分配
        self._bound_port: int | None = None  # start() 后的实际端口
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def port(self) -> int:
        """
        返回实际监听端口。

        构造时传入 port=0 时 OS 会自动分配端口，
        必须在 start() 之后调用才能取到实际值。

        Raises:
            RuntimeError: 服务器尚未启动。
        """
        if self._bound_port is None:
            raise RuntimeError("服务器尚未启动，请先调用 start()")
        return self._bound_port

    def _make_handler(self) -> type[Any]:
        raise NotImplementedError

    def start(self) -> "_BaseHttpServer":
        if self._server is not None:
            debug_log("服务器已在运行", f"{self.__class__.__name__}.start")
            return self

        handler = self._make_handler()
        self._server = HTTPServer((self.host, self._port), handler)
        addr: tuple[str, int] = self._server.server_address  # type: ignore
        self._bound_port = addr[1]

        thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, # type: ignore
            name=f"{self.__class__.__name__}-serve",
        )
        thread.start()
        self._thread = thread
        debug_log(
            f"服务器启动成功: {self.host}:{self._bound_port}",
            f"{self.__class__.__name__}.start",
        )
        return self

    def stop(self):
        if self._server is None:
            debug_log("服务器未运行", f"{self.__class__.__name__}.stop")
            return

        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join()

        self._server = None
        self._thread = None
        self._bound_port = None
        debug_log("已停止服务器", f"{self.__class__.__name__}.stop")

    def __enter__(self) -> "_BaseHttpServer":
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.stop()
        return False


class HttpEcho(_BaseHttpServer):
    def __init__(self, port: int = 8000):
        super().__init__(port)
        self._requests: list[EchoRequest] = []
        self._lock = threading.Lock()

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        store = self._requests
        lock = self._lock

        class CustomHandler(BaseHTTPRequestHandler):
            def _handle(self):
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length) if content_length else b""

                req = EchoRequest(
                    ip=self.client_address[0],
                    method=self.command,
                    path=self.path,
                    headers=dict(self.headers),
                    body=body,
                    timestamp=time.time(),
                )
                debug_log(str(req), f"HttpEcho.{self.command}")
                with lock:
                    store.append(req)

                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")

            def do_GET(self):    self._handle()

            def do_POST(self):   self._handle()

            def do_PUT(self):    self._handle()

            def do_DELETE(self): self._handle()

            def do_PATCH(self):  self._handle()

            def log_message(self, _format, *args):
                debug_log(f"{_format % args}", "HttpEcho.log")

        return CustomHandler

    def requests(self) -> list[EchoRequest]:
        with self._lock:
            return list(self._requests)

    def echo(self) -> str:
        with self._lock:
            return "\n\n".join(
                f"[{i}] {req}"
                for i, req in enumerate(self._requests)
            )


class HttpFile(_BaseHttpServer):
    def __init__(self, files: dict[str, bytes | Path], port: int = 8001):
        super().__init__(port)
        self.files = files

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        files_to_serve = self.files

        class FileDownloadHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                filename = self.path.lstrip("/")
                debug_log(f"收到 GET 请求: path={self.path}, filename={filename}", "HttpFile.do_GET")

                if filename not in files_to_serve:
                    debug_log(f"文件不存在: {filename}", "HttpFile.do_GET")
                    self.send_error(404, "File Not Found")
                    return

                file_data_or_path = files_to_serve[filename]
                try:
                    if isinstance(file_data_or_path, bytes):
                        content = file_data_or_path
                        debug_log(f"从内存读取文件: {filename}, size={len(content)}", "HttpFile.do_GET")
                    elif isinstance(file_data_or_path, Path):
                        content = file_data_or_path.read_bytes()
                        debug_log(f"从磁盘读取文件: {file_data_or_path}, size={len(content)}", "HttpFile.do_GET")
                    else:
                        raise TypeError("File data must be bytes or a Path.")
                except FileNotFoundError:
                    debug_log(f"磁盘文件不存在: {file_data_or_path}", "HttpFile.do_GET")
                    self.send_error(404, "File Not Found on Server Disk")
                    return
                except Exception as e:
                    debug_log(f"读取文件失败: {filename}, error={e}", "HttpFile.do_GET")
                    self.send_error(500, "Internal Server Error")
                    return

                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                debug_log(f"文件发送成功: {filename}", "HttpFile.do_GET")

            def log_message(self, _format, *args):
                debug_log(f"{_format % args}", "HttpFile.log")

        return FileDownloadHandler

if __name__ == "__main__":
    from ctf.utils.log import set_debug
    from ctf.shell.obf import demo

    #
    # # with 用法
    # print("\n=== 测试 HttpEcho ===")
    # with HttpEcho():
    #     run_cmd("curl http://0.0.0.0:8000 -d 'HttpEcho with debug logs'")
    # print("\n=== 测试 HttpFile ===")
    # with HttpFile({"abc.txt": b"HttpFile content"}):
    #     cr = run_cmd("curl http://0.0.0.0:8001/abc.txt -o /tmp/abc.txt;cat /tmp/abc.txt")
    #     print(f"[下载结果] {cr.output}")




    demo()
    set_debug()
    http_echo = HttpEcho()
    http_echo.start()
    input("服务器运行中，按回车停止...")
    http_echo.stop()
