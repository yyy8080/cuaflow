"""Tests for the ``cuaflow-tool`` typer app."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from cuaflow.platform import FakeDesktop, cli_tools, find_ask_user, find_image_markers

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CUAFLOW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CUAFLOW_BROWSER_SESSION", str(tmp_path / "data" / "browser" / "session.json"))
    monkeypatch.setenv("CUAFLOW_HEADLESS", "1")
    return tmp_path


@pytest.fixture
def fake(monkeypatch):
    d = FakeDesktop()
    d.add_window("Untitled - Notepad", "notepad.exe")
    d.add_window("Save changes?", "notepad.exe", modal=True)
    monkeypatch.setattr(cli_tools, "get_desktop", lambda: d)
    return d


def _png(path):
    from PIL import Image

    Image.new("RGB", (4, 4), "red").save(path)
    return path


def test_image_show_marker(env):
    img = _png(env / "x.png")
    r = runner.invoke(cli_tools.app, ["image-show", "--path", str(img)])
    assert r.exit_code == 0, r.output
    assert r.output.strip() == f"[[CUAFLOW_IMAGE:{img.resolve()}]]"
    assert find_image_markers(r.output) == [str(img.resolve())]


def test_image_show_rejects_bad_files(env):
    bad = env / "bad.png"
    bad.write_text("not an image")
    r = runner.invoke(cli_tools.app, ["image-show", "--path", str(bad)])
    assert r.exit_code == 1 and "CUAFLOW_IMAGE" not in r.output
    r = runner.invoke(cli_tools.app, ["image-show", "--path", str(env / "missing.png")])
    assert r.exit_code == 1


def test_ui_tree_and_windows_on_fake(env, fake):
    r = runner.invoke(cli_tools.app, ["ui-tree", "--depth", "3", "--max-nodes", "50"])
    assert r.exit_code == 0, r.output
    assert "Untitled - Notepad" in r.output
    r = runner.invoke(cli_tools.app, ["ui-tree", "--window-title-re", "Untitled"])
    assert r.exit_code == 0
    r = runner.invoke(cli_tools.app, ["ui-tree", "--window-title-re", "NoSuchWindow"])
    assert r.exit_code == 1 and "TimeoutError" in r.output

    r = runner.invoke(cli_tools.app, ["windows"])
    assert r.exit_code == 0
    assert "'Save changes?' process=notepad.exe" in r.output and "modal" in r.output
    r = runner.invoke(cli_tools.app, ["windows", "--json"])
    import json

    data = json.loads(r.output)
    assert len(data["windows"]) == 2 and data["modal_dialogs"][0]["title"] == "Save changes?"


def test_desktop_screenshot_on_fake(env, fake):
    out = env / "shots" / "d.png"
    r = runner.invoke(cli_tools.app, ["screenshot", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert out.exists()
    assert find_image_markers(r.output) == [str(out.resolve())]
    r = runner.invoke(cli_tools.app, ["screenshot"])
    assert r.exit_code == 0
    path = find_image_markers(r.output)[0]
    assert path.startswith(str((env / "data" / "screenshots").resolve()))


def test_ask_user_marker(env):
    r = runner.invoke(cli_tools.app, ["ask-user", "--question", "请提供验证码"])
    assert r.exit_code == 0
    assert r.output.strip() == "[[CUAFLOW_ASK_USER:请提供验证码]]"
    assert find_ask_user(r.output) == "请提供验证码"


def test_aria_without_browser_fails_cleanly(env):
    r = runner.invoke(cli_tools.app, ["aria"])
    assert r.exit_code == 2
    assert "browser-open" in r.output


@pytest.mark.browser
def test_browser_cli_flow(env):
    try:
        r = runner.invoke(cli_tools.app, ["browser-open", "data:text/html;charset=utf-8,<title>T1</title><h1>CLI 你好</h1>"])
        assert r.exit_code == 0, r.output
        assert "title: T1" in r.output

        r = runner.invoke(cli_tools.app, ["aria", "--max-chars", "500"])
        assert r.exit_code == 0, r.output
        assert "CLI 你好" in r.output

        code = env / "snippet.py"
        code.write_text("print('h1 =', page.locator('h1').inner_text())\nprint(type(browser).__name__)\n",
                        encoding="utf-8")
        r = runner.invoke(cli_tools.app, ["browser-exec", "--code-file", str(code)])
        assert r.exit_code == 0, r.output
        assert "h1 = CLI 你好" in r.output and "PersistentBrowser" in r.output

        r = runner.invoke(cli_tools.app, ["browser-exec", "--code", "page.goto('data:text/html,<p>moved</p>')"])
        assert r.exit_code == 0, r.output
        r = runner.invoke(cli_tools.app, ["aria"])
        assert "moved" in r.output

        r = runner.invoke(cli_tools.app, ["browser-exec", "--code", "raise ValueError('boom')"])
        assert r.exit_code == 1
        assert "ValueError: boom" in r.output and "Traceback" in r.output

        r = runner.invoke(cli_tools.app, ["screenshot", "--browser", "--out", str(env / "b.png")])
        assert r.exit_code == 0, r.output
        assert (env / "b.png").exists() and "[[CUAFLOW_IMAGE:" in r.output

        r = runner.invoke(cli_tools.app, ["browser-status"])
        assert '"alive": true' in r.output
    finally:
        r = runner.invoke(cli_tools.app, ["browser-close"])
    assert "browser closed" in r.output
    r = runner.invoke(cli_tools.app, ["browser-status"])
    assert '"alive": false' in r.output
