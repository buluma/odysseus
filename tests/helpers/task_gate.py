"""Shared setup for the scheduled-task admin-gate tests.

The HTTP-route gate (tests/test_task_cookbook_admin_gate.py) and the
manage_tasks AI-tool gate (tests/test_manage_tasks_admin_gate.py) exercise the
same policy (src/task_action_policy.py) and need the same scaffolding: a real
core.database binding, a file-backed temp sqlite, and a deterministic
AuthManager. One home for that scaffolding so the two files cannot drift.
"""
from tests.helpers.import_state import restore_module_binding
from tests.helpers.sqlite_db import make_temp_sqlite


def bind_real_task_db(monkeypatch, *session_local_targets):
    """Pin the real core.database and bind a temp sqlite onto it.

    Restores the on-disk core.database module binding (another test may have
    left a stub), pins its ORM attributes for the monkeypatch scope, builds a
    file-backed temp sqlite via tests.helpers.sqlite_db.make_temp_sqlite, and
    binds the resulting SessionLocal onto core.database plus any extra
    modules whose module-level `SessionLocal` the code under test reads.
    Returns the sessionmaker.
    """
    import core.database as cdb

    restore_module_binding(monkeypatch, "core.database", cdb)
    for attr in ("Base", "SessionLocal", "ScheduledTask", "TaskRun", "engine"):
        value = getattr(cdb, attr, None)
        if value is not None:
            monkeypatch.setattr(cdb, attr, value, raising=False)

    testing_session, engine, tmpfile = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(cdb, "SessionLocal", testing_session)
    for target in session_local_targets:
        monkeypatch.setattr(target, "SessionLocal", testing_session)
    return testing_session


def install_fake_auth(monkeypatch, admin_users=("admin",)):
    """Pin the real core.auth binding and install a deterministic
    AuthManager: configured, and `admin_users` are the only admins.

    AUTH_ENABLED is pinned to "true" (its default) so an AUTH_ENABLED=false
    leaked from another test or the shell can't short-circuit
    owner_has_admin_task_privileges into granting everyone admin.
    """
    import core.auth as core_auth

    restore_module_binding(monkeypatch, "core.auth", core_auth)
    monkeypatch.setenv("AUTH_ENABLED", "true")

    class FakeAuthManager:
        is_configured = True

        def is_admin(self, user):
            return user in admin_users

    monkeypatch.setattr(core_auth, "AuthManager", FakeAuthManager)
    return FakeAuthManager
