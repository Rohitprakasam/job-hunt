from unittest.mock import MagicMock, patch

from agent import live_view


def test_generate_password():
    pwd1 = live_view.generate_password(8)
    pwd2 = live_view.generate_password(8)
    assert len(pwd1) == 8 and len(pwd2) == 8
    assert pwd1 != pwd2


def test_open_view_fails_without_secret_link(monkeypatch):
    monkeypatch.delenv("JOB_AGENT_SECRET_LIVE_VIEW_LINK", raising=False)
    session = live_view.open_view("linkedin", "login")
    assert session is None


def test_open_view_fails_when_whatsapp_window_closed(monkeypatch):
    monkeypatch.setenv("JOB_AGENT_SECRET_LIVE_VIEW_LINK", "https://6080-test.instabox.tech")
    wa = MagicMock()
    wa.window_open.return_value = False
    session = live_view.open_view("linkedin", "login", wa_client=wa)
    assert session is None
    wa.send_text.assert_not_called()


def test_open_view_lifecycle_with_mocks(monkeypatch):
    monkeypatch.setenv("JOB_AGENT_SECRET_LIVE_VIEW_LINK", "https://6080-test.instabox.tech")
    wa = MagicMock()
    wa.window_open.return_value = True
    wa.send_text.return_value = "sent"

    mock_vnc_proc = MagicMock()
    mock_ws_proc = MagicMock()

    with (
        patch("shutil.which", return_value="/usr/bin/tool"),
        patch("agent.live_view.is_port_open", return_value=True),
        patch("subprocess.Popen", side_effect=[mock_vnc_proc, mock_ws_proc]),
    ):
        with live_view.live_view("linkedin", "captcha", wa_client=wa) as session:
            assert session is not None
            assert session.site == "linkedin"
            assert len(session.password) == 8
            wa.send_text.assert_called_once()
            msg = wa.send_text.call_args[0][0]
            assert "🔐 Linkedin needs you (captcha)" in msg
            assert session.password in msg
            assert "https://6080-test.instabox.tech" in msg

        # Context manager exit should terminate both processes
        mock_ws_proc.terminate.assert_called()
        mock_vnc_proc.terminate.assert_called()


def test_wait_for_resolution():
    calls = []

    def check():
        calls.append(1)
        return len(calls) >= 3

    session = live_view.LiveViewSession(
        site="test",
        reason="test",
        password="pwd",  # pragma: allowlist secret
        link="http://link",
    )
    ok = live_view.wait_for_resolution(session, check, timeout_seconds=5, poll_interval_s=0.01)
    assert ok is True
    assert len(calls) >= 3
