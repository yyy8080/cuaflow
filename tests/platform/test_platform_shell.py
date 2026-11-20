"""Tests for LocalShell and the command safety blocklist."""

from __future__ import annotations

import sys
import time

import pytest

from cuaflow.platform import DEFAULT_BLOCKED_PATTERNS, LocalShell, ShellBackend, check_command_safety
from cuaflow.platform.shell import BLOCKED_EXIT_CODE, _decode_clixml

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="bash semantics")


def test_protocol_conformance():
    assert isinstance(LocalShell(), ShellBackend)


@posix_only
def test_echo_and_exit_code(tmp_path):
    sh = LocalShell()
    r = sh.run("echo hello")
    assert r.ok and r.exit_code == 0
    assert r.stdout.strip() == "hello"

    r = sh.run("echo oops >&2; exit 3")
    assert r.exit_code == 3 and not r.ok
    assert "oops" in r.stderr

    r = sh.run("pwd", cwd=tmp_path)
    assert r.stdout.strip() == str(tmp_path)

    r = sh.run("echo $CUAFLOW_T", env={"CUAFLOW_T": "x1"})
    assert r.stdout.strip() == "x1"


@posix_only
def test_unicode_round_trip():
    r = LocalShell().run("printf '%s\\n' '中文 测试 ✓'")
    assert r.ok
    assert r.stdout.strip() == "中文 测试 ✓"
    # invalid UTF-8 is replaced, not raised
    r = LocalShell().run("printf '\\xff\\xfe ok'")
    assert "ok" in r.stdout and "\ufffd" in r.stdout


@posix_only
def test_timeout_kills_process_tree(tmp_path):
    marker = tmp_path / "child_survived"
    # background grandchild would create the marker after 3s if not killed with the group
    cmd = f"(sleep 3; touch {marker}) & sleep 30"
    t0 = time.monotonic()
    r = LocalShell().run(cmd, timeout=1)
    elapsed = time.monotonic() - t0
    assert r.timed_out and not r.ok
    assert elapsed < 10
    assert "timed out" in r.stderr
    time.sleep(3.5)
    assert not marker.exists(), "grandchild survived the timeout kill"


@posix_only
def test_timeout_keeps_partial_output():
    r = LocalShell().run("echo before; sleep 20", timeout=1)
    assert r.timed_out
    assert "before" in r.stdout


def test_text_rendering():
    r = LocalShell().run("echo hi" if sys.platform != "win32" else "Write-Output hi")
    assert "exit_code=0" in r.text()
    assert "hi" in r.text()


@pytest.mark.parametrize(
    "cmd",
    [
        "Format-Volume -DriveLetter D",
        "format c: /q",
        "FORMAT D:",
        "rm -rf /",
        "rm -rf / --no-preserve-root",
        "rm -fr ~",
        "sudo rm -rf /*",
        "Remove-Item -Recurse -Force C:\\",
        "Remove-Item C:\\* -Recurse",
        "Remove-Item -Path 'C:\\' -Recurse -Force",
        "rd /s /q C:\\",
        "del /f /s /q C:\\Windows\\System32",
        "Remove-Item -Recurse $env:SystemDrive\\",
        "diskpart /s script.txt",
        "bcdedit /set {default} recoveryenabled No",
        "reg delete HKLM\\Software\\Foo /f",
        "reg.exe delete \"HKEY_LOCAL_MACHINE\\SYSTEM\" /f",
        "Remove-Item -Path HKLM:\\SOFTWARE\\Foo -Recurse",
        "vssadmin delete shadows /all /quiet",
        "cipher /w:C:\\",
        "shutdown /s /t 0",
        "shutdown -h now",
        "Restart-Computer -Force",
        "Stop-Computer",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda bs=1M",
        "Clear-Disk -Number 1 -RemoveData",
    ],
)
def test_blocked_patterns_match(cmd):
    assert check_command_safety(cmd, DEFAULT_BLOCKED_PATTERNS) is not None, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "echo hello",
        "rm -rf /tmp/cuaflow-test",
        "rm -rf ./build",
        "Remove-Item -Recurse -Force C:\\temp\\out",
        "Remove-Item .\\file.txt",
        "Get-ChildItem C:\\",
        "dd if=/dev/zero of=/tmp/file bs=1k count=1",
        "dd if=in.img of=/dev/null",
        "python -c \"browser.shutdown()\"",
        "Get-Content shutdown.log",
        "reg query HKLM\\Software",
        "format-table -AutoSize",
        "git format-patch HEAD~1",
        "Start-Process notepad",
    ],
)
def test_safe_commands_not_blocked(cmd):
    assert check_command_safety(cmd, DEFAULT_BLOCKED_PATTERNS) is None, cmd


def test_custom_patterns_case_insensitive():
    assert check_command_safety("DROP TABLE users", [r"drop\s+table"]) == r"drop\s+table"
    assert check_command_safety("select 1", [r"drop\s+table"]) is None
    # invalid regex falls back to substring match
    assert check_command_safety("a ( b", ["("]) == "("


def test_blocked_command_not_executed(tmp_path):
    sh = LocalShell()
    r = sh.run("rm -rf / ; echo ran")
    assert r.exit_code == BLOCKED_EXIT_CODE
    assert "blocked" in r.stderr and not r.stdout
    # disabled blocklist
    assert LocalShell(blocked_patterns=[]).check("rm -rf /") is None


def test_powershell_argv_shape():
    sh = LocalShell(shell="powershell")
    argv = sh.argv("Write-Output 'hi'")
    assert argv[1:6] == ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
    assert "-EncodedCommand" in argv
    import base64

    script = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert script.startswith("[Console]::OutputEncoding=[Text.Encoding]::UTF8; $OutputEncoding=[Text.Encoding]::UTF8;")
    assert "Write-Output 'hi'" in script
    assert LocalShell(shell="pwsh").argv("x")[0].lower().endswith(("pwsh", "pwsh.exe"))


def test_decode_clixml():
    raw = ('#< CLIXML\r\n<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
           '<S S="Error">boom &lt;x&gt;_x000D__x000A_</S></Objs>')
    assert _decode_clixml(raw) == "boom <x>\n"
    assert _decode_clixml("plain") == "plain"


@pytest.mark.windows
def test_powershell_unicode_and_exit_code():
    sh = LocalShell()
    r = sh.run("Write-Output '中文 ✓'")
    assert r.ok and r.stdout.strip() == "中文 ✓"
    r = sh.run("cmd /c exit 5")
    assert r.exit_code == 5
    r = sh.run("Start-Sleep -Seconds 30", timeout=2)
    assert r.timed_out
