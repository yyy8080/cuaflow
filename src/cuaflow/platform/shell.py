"""Cross-platform command execution (``ShellBackend``).

* Windows: Windows PowerShell (``powershell.exe``) or PowerShell 7 (``pwsh``), UTF-8 forced.
* elsewhere: ``/bin/bash -lc``.

Timeouts kill the whole process tree. Output is decoded as UTF-8 with ``errors="replace"``.
A configurable regex blocklist (:data:`DEFAULT_BLOCKED_PATTERNS`) refuses obviously
destructive commands before they are started.
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

ENV_SHELL = "CUAFLOW_SHELL"  # "powershell" | "pwsh" | "bash" | "sh" | "cmd" | absolute path

POWERSHELL_FLAGS = ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
POWERSHELL_UTF8_PREFIX = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $OutputEncoding=[Text.Encoding]::UTF8; "
    "$ProgressPreference='SilentlyContinue';"
)
# Propagate failure of the last statement / native exit code as the process exit code.
_POWERSHELL_EXIT_SUFFIX = (
    "\nif (-not $?) { if ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 } }"
)

BLOCKED_EXIT_CODE = 126
TIMEOUT_EXIT_CODE = 124

_SEP = r"(?=\s|$|[;&|)'\"])"  # end of a token
_DEL = r"(?:remove-item|ri|rm|rmdir|rd|del|erase)"

#: Case-insensitive regexes for commands that are refused by default.
DEFAULT_BLOCKED_PATTERNS: list[str] = [
    # disks / volumes / partitions
    r"\bformat-volume\b",
    r"\b(?:clear-disk|initialize-disk|remove-partition)\b",
    r"\bformat(?:\.com)?\s+['\"]?[a-z]:",
    r"\bdiskpart\b",
    r"\bmkfs(?:\.\w+)?\b",
    r"\bdd\b[^\n;|&]*\bof=/dev/(?!null\b|zero\b|stdout\b|stderr\b)",
    r"\bwipefs\b",
    # recursive deletion of filesystem roots / home / system dirs
    r"\brm\s+(?:-{1,2}[\w-]+\s+)*['\"]?(?:/\*?|~/?|\$home/?|/(?:bin|boot|etc|lib|lib64|usr|var|home|root)/?)['\"]?" + _SEP,
    rf"\b{_DEL}\b[^\n;|&]*?\s(?:-(?:literal)?path\s+)?['\"]?[a-z]:\\?\*?(?:\.\*)?['\"]?{_SEP}",
    rf"\b{_DEL}\b[^\n;|&]*?\$env:systemdrive\\?\*?['\"]?{_SEP}",
    rf"\b{_DEL}\b[^\n;|&]*?[a-z]:\\(?:windows|users|program files(?: \(x86\))?)(?:\\system32)?\\?\*?['\"]?{_SEP}",
    # boot config / registry / shadow copies / secure wipe
    r"\bbcdedit\b",
    r"\breg(?:\.exe)?\s+delete\s+['\"]?(?:hklm|hkey_local_machine)\b",
    r"\bremove-item\b[^\n;|&]*\b(?:hklm|registry::hkey_local_machine):",
    r"\bvssadmin(?:\.exe)?\s+delete\s+shadows\b",
    r"\bwmic\b[^\n]*\bshadowcopy\s+delete\b",
    r"\bcipher(?:\.exe)?\s+/w\b",
    # power state
    r"(?:^|[\s;&|(])shutdown(?:\.exe)?(?:\s+[/-]|\s*$|\s*[;&|)])",
    r"\b(?:restart|stop)-computer\b",
    r"(?:^|[\s;&|(])(?:reboot|poweroff|halt)(?:\s|$|[;&|)])",
    # fork bomb
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
]


def check_command_safety(command: str, patterns: list[str]) -> str | None:
    """Return the first pattern (case-insensitive regex) matching ``command``, else ``None``."""
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
    """Turn PowerShell's ``#< CLIXML`` stderr serialization back into plain text."""
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
    """Run shell commands on the local host.

    Args:
        shell: ``"powershell"`` (Windows default), ``"pwsh"``, ``"bash"`` (POSIX default),
            ``"sh"``, ``"cmd"`` or an executable path. Defaults to ``$CUAFLOW_SHELL``.
        cwd: default working directory.
        env: extra environment variables applied to every command.
        blocked_patterns: regex blocklist; ``None`` → :data:`DEFAULT_BLOCKED_PATTERNS`,
            ``[]`` disables the check. Blocked commands are not run and return
            ``ShellResult(exit_code=126, stderr="cuaflow: command blocked ...")``.

    Note: on Windows the command is passed with ``-EncodedCommand`` (base64 UTF-16LE of the
    UTF-8 prefix + command) instead of ``-Command`` to avoid all command-line quoting problems;
    semantics are otherwise identical to ``-Command``.
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
        """The argv used to execute ``command`` with the configured shell."""
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
        """Matched blocklist pattern for ``command`` (or None)."""
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
                # A detached grandchild still holds the pipes; give up on the remaining output.
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
