"""跨平台命令执行后端（``ShellBackend`` 实现）。

* Windows：使用 Windows PowerShell（``powershell.exe``）或 PowerShell 7（``pwsh``），强制启用 UTF-8 编码。
* 其他系统：使用 ``/bin/bash -lc``。

命令超时会终止整个进程树；标准输出与错误流均以 UTF-8（``errors="replace"``）解码。
内置可配置的正则黑名单（:data:`DEFAULT_BLOCKED_PATTERNS`），在启动前拦截明显具有破坏性的高危命令。
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path

from cuaflow.platform._proc import CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, IS_WINDOWS, kill_tree
from cuaflow.platform.base import ShellResult

ENV_SHELL = "CUAFLOW_SHELL"  # 可选值："powershell" | "pwsh" | "bash" | "sh" | "cmd" 或可执行文件绝对路径

POWERSHELL_FLAGS = ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
POWERSHELL_UTF8_PREFIX = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $OutputEncoding=[Text.Encoding]::UTF8; "
    "$ProgressPreference='SilentlyContinue';"
)
# 将最后一条语句的执行失败状态或原生进程退出码传递为 PowerShell 进程退出码。
_POWERSHELL_EXIT_SUFFIX = (
    "\nif (-not $?) { if ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 } }"
)

BLOCKED_EXIT_CODE = 126
TIMEOUT_EXIT_CODE = 124

_SEP = r"(?=\s|$|[;&|)'\"])"  # 词元结束边界匹配
_DEL = r"(?:remove-item|ri|rm|rmdir|rd|del|erase)"

#: 默认拦截的高危命令正则表达式列表（忽略大小写）。
DEFAULT_BLOCKED_PATTERNS: list[str] = [
    # 磁盘 / 卷 / 分区破坏操作
    r"\bformat-volume\b",
    r"\b(?:clear-disk|initialize-disk|remove-partition)\b",
    r"\bformat(?:\.com)?\s+['\"]?[a-z]:",
    r"\bdiskpart\b",
    r"\bmkfs(?:\.\w+)?\b",
    r"\bdd\b[^\n;|&]*\bof=/dev/(?!null\b|zero\b|stdout\b|stderr\b)",
    r"\bwipefs\b",
    # 递归删除文件系统根目录 / 用户主目录 / 系统关键目录
    r"\brm\s+(?:-{1,2}[\w-]+\s+)*['\"]?(?:/\*?|~/?|\$home/?|/(?:bin|boot|etc|lib|lib64|usr|var|home|root)/?)['\"]?" + _SEP,
    rf"\b{_DEL}\b[^\n;|&]*?\s(?:-(?:literal)?path\s+)?['\"]?[a-z]:\\?\*?(?:\.\*)?['\"]?{_SEP}",
    rf"\b{_DEL}\b[^\n;|&]*?\$env:systemdrive\\?\*?['\"]?{_SEP}",
    rf"\b{_DEL}\b[^\n;|&]*?[a-z]:\\(?:windows|users|program files(?: \(x86\))?)(?:\\system32)?\\?\*?['\"]?{_SEP}",
    # 启动配置 / 注册表 / 卷影副本 / 磁盘安全擦除
    r"\bbcdedit\b",
    r"\breg(?:\.exe)?\s+delete\s+['\"]?(?:hklm|hkey_local_machine)\b",
    r"\bremove-item\b[^\n;|&]*\b(?:hklm|registry::hkey_local_machine):",
    r"\bvssadmin(?:\.exe)?\s+delete\s+shadows\b",
    r"\bwmic\b[^\n]*\bshadowcopy\s+delete\b",
    r"\bcipher(?:\.exe)?\s+/w\b",
    # 系统电源状态（关机 / 重启）
    r"(?:^|[\s;&|(])shutdown(?:\.exe)?(?:\s+[/-]|\s*$|\s*[;&|)])",
    r"\b(?:restart|stop)-computer\b",
    r"(?:^|[\s;&|(])(?:reboot|poweroff|halt)(?:\s|$|[;&|)])",
    # Fork 炸弹
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
]


def check_command_safety(command: str, patterns: list[str]) -> str | None:
    """返回 ``command`` 命中的第一条黑名单正则模式（忽略大小写），未命中则返回 ``None``。"""
    for pat in patterns:
        try:
            if re.search(pat, command, flags=re.IGNORECASE | re.MULTILINE):
                return pat
        except re.error:
            if pat.lower() in command.lower():
                return pat
    return None


def _decode(data: bytes | None) -> str:
    if not data:
        return ""
    text = data.decode("utf-8", errors="replace")
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n")


def _decode_clixml(text: str) -> str:
    """将 PowerShell 标准错误输出中的 ``#< CLIXML`` XML 序列化内容还原为纯文本。"""
    if not text.lstrip().startswith("#< CLIXML"):
        return text
    parts = re.findall(r'<S S="Error">(.*?)</S>', text, flags=re.DOTALL)
    if not parts:
        return text
    out = "".join(parts)
    out = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m.group(1), 16)), out)
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&")):
        out = out.replace(a, b)
    return out.replace("\r\n", "\n")


class LocalShell:
    """在本地主机上执行 Shell 命令。

    参数：
        shell: 可取 ``"powershell"``（Windows 默认）、``"pwsh"``、``"bash"``（POSIX 默认）、
            ``"sh"``、``"cmd"`` 或可执行文件路径。默认优先读取环境变量 ``$CUAFLOW_SHELL``。
        cwd: 默认工作目录。
        env: 注入到每次命令执行环境中的附加环境变量。
        blocked_patterns: 命令黑名单正则列表；``None`` 表示使用 :data:`DEFAULT_BLOCKED_PATTERNS`，
            空列表 ``[]`` 表示禁用安全拦截。被拦截的命令不会启动，直接返回
            ``ShellResult(exit_code=126, stderr="cuaflow: command blocked ...")``。

    注意：在 Windows 下使用 PowerShell 时，命令会拼接 UTF-8 编码前缀与退出码后缀后编码为
    UTF-16LE Base64，并通过 ``-EncodedCommand`` 传递而非 ``-Command``，以彻底规避命令行引号转义问题；
    执行语义与 ``-Command`` 完全一致。
    """

    def __init__(
        self,
        shell: str | None = None,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        blocked_patterns: list[str] | None = None,
    ) -> None:
        self.shell = (shell or os.environ.get(ENV_SHELL) or ("powershell" if IS_WINDOWS else "bash")).strip()
        self.cwd = str(cwd) if cwd else None
        self.env = dict(env or {})
        self.blocked_patterns = list(DEFAULT_BLOCKED_PATTERNS if blocked_patterns is None else blocked_patterns)

    # ------------------------------------------------------------------
    def argv(self, command: str) -> list[str]:
        """构造使用当前配置的 Shell 执行 ``command`` 所需的参数列表 argv。"""
        kind = Path(self.shell).name.lower()
        if kind.endswith(".exe"):
            kind = kind[:-4]
        if kind in ("powershell", "pwsh"):
            exe = self.shell
            if exe in ("powershell", "pwsh"):
                exe = shutil.which(exe) or (exe + ".exe" if IS_WINDOWS else exe)
            script = POWERSHELL_UTF8_PREFIX + "\n" + command + _POWERSHELL_EXIT_SUFFIX
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            return [exe, *POWERSHELL_FLAGS, "-OutputFormat", "Text", "-EncodedCommand", encoded]
        if kind == "cmd":
            return [self.shell if Path(self.shell).is_absolute() else "cmd.exe", "/d", "/s", "/c", command]
        if kind in ("bash", "sh", "zsh"):
            exe = self.shell if Path(self.shell).is_absolute() else (shutil.which(kind) or f"/bin/{kind}")
            if not Path(exe).exists() and kind == "bash":
                exe = "/bin/sh"
                return [exe, "-c", command]
            return [exe, "-lc", command] if kind != "sh" else [exe, "-c", command]
        return [self.shell, "-c", command]

    def check(self, command: str) -> str | None:
        """检查 ``command`` 是否命中安全黑名单，返回命中的正则模式或 ``None``。"""
        return check_command_safety(command, self.blocked_patterns) if self.blocked_patterns else None

    def run(
        self,
        command: str,
        timeout: float = 120,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
    ) -> ShellResult:
        hit = self.check(command)
        if hit is not None:
            return ShellResult(
                exit_code=BLOCKED_EXIT_CODE,
                stderr=f"cuaflow: command blocked by safety policy (pattern: {hit})",
            )

        import sys

        full_env = dict(os.environ)
        if getattr(sys, "frozen", False):
            exe_dir = str(Path(sys.executable).resolve().parent)
            cur_path = full_env.get("PATH", "")
            if exe_dir not in cur_path.split(os.pathsep):
                full_env["PATH"] = f"{exe_dir}{os.pathsep}{cur_path}" if cur_path else exe_dir
        full_env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        full_env.update(self.env)
        if env:
            full_env.update(env)

        kwargs: dict = dict(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd else self.cwd,
            env=full_env,
        )
        if IS_WINDOWS:
            kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(self.argv(command), **kwargs)  # noqa: S603
        except (OSError, ValueError) as exc:
            return ShellResult(exit_code=127, stderr=f"cuaflow: failed to start shell {self.shell!r}: {exc}")

        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(proc.pid)
            try:
                proc.kill()
            except OSError:
                pass
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                # 分离的孙进程仍持有标准流管道句柄；放弃等待剩余输出并主动关闭流。
                for stream in (proc.stdout, proc.stderr):
                    try:
                        stream and stream.close()
                    except OSError:
                        pass
                out, err = b"", b""
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass

        stdout, stderr = _decode(out), _decode_clixml(_decode(err))
        if timed_out:
            stderr = (stderr + ("\n" if stderr and not stderr.endswith("\n") else "")
                      + f"cuaflow: command timed out after {timeout:g}s; process tree killed")
            code = proc.returncode if proc.returncode not in (None, 0) else TIMEOUT_EXIT_CODE
            return ShellResult(exit_code=code, stdout=stdout, stderr=stderr, timed_out=True)
        return ShellResult(exit_code=proc.returncode, stdout=stdout, stderr=stderr)
