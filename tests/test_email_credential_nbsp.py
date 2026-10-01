"""Mail credentials copied with NBSP separators must not crash IMAP login."""

import os
import tempfile
from pathlib import Path

_tmp_data = Path(tempfile.mkdtemp(prefix="odysseus_email_cred_nbsp_"))
os.environ.setdefault("DATA_DIR", str(_tmp_data))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp_data / 'app.db'}")

from routes import email_helpers as eh


class FakeImap:
    def __init__(self):
        self.login_args = None

    def login(self, user, password):
        self.login_args = (user, password)


def test_imap_connect_normalizes_nbsp_credentials(monkeypatch):
    fake = FakeImap()
    monkeypatch.setattr(
        eh,
        "_get_email_config",
        lambda account_id=None, owner="": {
            "imap_host": "imap.example.test",
            "imap_port": 993,
            "imap_starttls": False,
            "imap_user": "user\xa0@example.test",
            "imap_password": "abcd\xa0efgh",
        },
    )
    monkeypatch.setattr(eh, "_open_imap_connection", lambda *args, **kwargs: fake)

    assert eh._imap_connect() is fake
    assert fake.login_args == ("user @example.test", "abcdefgh")


def _mcp_cfg(**overrides):
    cfg = {
        "imap_host": "imap.example.test",
        "imap_port": 993,
        "imap_ssl": True,
        "imap_user": "user\xa0@example.test",
        "imap_password": "abcd\xa0efgh",
        "smtp_host": "smtp.example.test",
        "smtp_port": 465,
        "smtp_security": "ssl",
        "smtp_user": "user\xa0@example.test",
        "smtp_password": "ab cd\xa0efgh",
    }
    cfg.update(overrides)
    return cfg


def test_mcp_imap_connect_normalizes_nbsp_credentials(monkeypatch):
    from mcp_servers import email_server as es

    fake = FakeImap()
    monkeypatch.setattr(es, "_load_config", lambda account=None: _mcp_cfg())
    monkeypatch.setattr(es.imaplib, "IMAP4_SSL", lambda *a, **kw: fake)

    assert es._imap_connect() is fake
    assert fake.login_args == ("user @example.test", "abcdefgh")


def test_mcp_smtp_connect_normalizes_nbsp_credentials(monkeypatch):
    from mcp_servers import email_server as es

    class FakeSmtp:
        login_args = None

        def login(self, user, password):
            FakeSmtp.login_args = (user, password)

    monkeypatch.setattr(es, "_load_config", lambda account=None: _mcp_cfg())
    monkeypatch.setattr(es.smtplib, "SMTP_SSL", lambda *a, **kw: FakeSmtp())

    es._smtp_connect(cfg=_mcp_cfg())
    assert FakeSmtp.login_args == ("user @example.test", "abcdefgh")
