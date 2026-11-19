"""按端口精确停止本项目服务,只结束监听该端口的进程树。

避免 taskkill /IM uvicorn.exe 这种模糊匹配误杀别的项目。
"""

import contextlib
import subprocess
import sys


def _run(cmd: list[str]) -> str:
    """运行命令并容错解码(中文 Windows 输出为 GBK)。"""
    raw = subprocess.run(cmd, capture_output=True, check=False).stdout or b""
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def pids_on_port(port: int) -> set[int]:
    out = _run(["netstat", "-ano"])
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP":
            local, state, pid = parts[1], parts[3].upper(), parts[4]
            if local.endswith(f":{port}") and state == "LISTENING":
                with contextlib.suppress(ValueError):
                    pids.add(int(pid))
    return pids


def child_pids(parent: int) -> set[int]:
    """Windows:找出以 parent 为父进程的子进程。"""
    out = _run([
        "powershell", "-NoProfile", "-Command",
        f"Get-CimInstance Win32_Process -Filter \"ParentProcessId={parent}\" | "
        f"Select-Object -ExpandProperty ProcessId",
    ])
    result = set()
    for token in out.split():
        with contextlib.suppress(ValueError):
            result.add(int(token))
    return result


def stop_port(port: int) -> int:
    roots = pids_on_port(port)
    if not roots:
        print(f"端口 {port} 上没有正在监听的服务")
        return 0
    targets: set[int] = set()
    for pid in roots:
        targets.add(pid)
        targets |= child_pids(pid)
    stopped = 0
    for pid in targets:
        rc = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True, check=False,
        ).returncode
        if rc == 0:
            stopped += 1
    print(f"已停止端口 {port} 上的 {stopped} 个进程")
    return stopped


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    stop_port(port)
