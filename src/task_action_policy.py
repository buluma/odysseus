"""Shared privilege policy for scheduled task actions."""

from __future__ import annotations

ADMIN_ONLY_TASK_ACTIONS = frozenset({
    "run_local",
    "run_script",
    "ssh_command",
    "cookbook_serve",
})


def is_admin_only_task_action(task_type: str | None, action: str | None) -> bool:
    return (task_type or "llm") == "action" and (action or "") in ADMIN_ONLY_TASK_ACTIONS


def admin_violation_for_task_action(
    owner: str | None, task_type: str | None, action: str | None
) -> str | None:
    """Return the denial message when `owner` may not use this task action,
    else None.

    Single composition point for the HTTP task routes and the manage_tasks
    AI tool — both gates must stay identical, so neither hand-rolls the
    predicate pair.
    """
    if is_admin_only_task_action(task_type, action) and not owner_has_admin_task_privileges(owner):
        return f"Action '{action}' requires admin privileges"
    return None


def resolve_task_action_edit(
    new_task_type: str | None,
    new_action: str | None,
    current_task_type: str | None,
    current_action: str | None,
) -> tuple[str | None, str | None]:
    """Return the (task_type, action) an edit leaves in effect.

    None means "field unchanged" and inherits the current value — the gate
    must judge the post-edit state, or a task could be edited into an
    admin-only action field by field.
    """
    return (
        new_task_type if new_task_type is not None else current_task_type,
        new_action if new_action is not None else current_action,
    )


def owner_has_admin_task_privileges(owner: str | None) -> bool:
    try:
        from src.auth_helpers import _auth_disabled
        if _auth_disabled():
            return True
    except Exception:
        pass

    if owner:
        try:
            from core.middleware import INTERNAL_TOOL_USER
            if owner == INTERNAL_TOOL_USER:
                return True
        except Exception:
            pass

    try:
        from core.auth import AuthManager
        auth = AuthManager()
        if not auth.is_configured:
            return True
        if not owner:
            return False
        return bool(auth.is_admin(owner))
    except Exception:
        pass

    if not owner:
        return False

    return False
