from segunda_tela.ui.deps_error import gi_missing_message


def test_gi_message_has_apt_and_venv_flags():
    msg = gi_missing_message(ModuleNotFoundError("No module named gi"))
    assert "python3-gi" in msg
    assert "gir1.2-gtk-4.0" in msg
    assert "gir1.2-adw-1" in msg
    assert "system-site-packages" in msg or "install.sh" in msg
    assert "rm -rf .venv" in msg
