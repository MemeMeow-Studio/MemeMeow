"""OpenCode 子进程 supervisor。

只有该模块负责进程组终止和 ``waitpid`` 收束判定；executor 业务层据此决定
cancelled、timeout 或 unknown_execution，不自行发送信号。
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProcessTermination:
    """进程收束结果，``reaped`` 表示父进程已被 wait 确认回收。"""

    reaped: bool
    returncode: int | None


class ProcessSupervisor:
    """按进程组管理单个 OpenCode 子进程。"""

    def terminate(self, process: subprocess.Popen[Any], *, grace_seconds: float = 2.0) -> ProcessTermination:
        """先发送 SIGTERM，超时后发送 SIGKILL，并返回可证明的回收状态。"""
        if process.poll() is not None:
            return ProcessTermination(True, process.returncode)
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=grace_seconds)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=grace_seconds)
            except (OSError, subprocess.TimeoutExpired):
                return ProcessTermination(process.poll() is not None, process.returncode)
        return ProcessTermination(process.poll() is not None, process.returncode)

    def wait(self, process: subprocess.Popen[Any], *, timeout_seconds: float) -> ProcessTermination:
        """等待进程终态；超时不擅自吞掉未知状态，由调用方决定是否终止。"""
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            return ProcessTermination(False, process.poll())
        return ProcessTermination(True, process.returncode)


__all__ = ["ProcessSupervisor", "ProcessTermination"]
