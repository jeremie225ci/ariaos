from __future__ import annotations

from typing import Any, Callable

from .bootstrap import endpoint_path


JsonLoader = Callable[[Any], dict[str, Any]]
JsonSaver = Callable[[Any, dict[str, Any]], None]
HttpJson = Callable[..., tuple[int, dict[str, Any], str | None]]
HttpUploadBinary = Callable[..., tuple[int, str]]


_OP_LOGIN = "l"
_OP_SIGNUP = "n"
_OP_SESSION = "m"
_OP_CLAIM = "c"
_OP_ENTITLEMENT = "e"
_OP_PREPARE = "p"
_OP_DEVICE = "d"
_OP_SCREENSHOT = "k"
_OP_SYNC = "s"
_OP_USAGE = "u"


def _plan_status_from_payload(plan: dict[str, Any], session: dict[str, Any]) -> tuple[str, str, str]:
    plan_key = str(plan.get("k") or plan.get("key") or session.get("plan_key") or "free")
    plan_name = str(plan.get("n") or plan.get("name") or session.get("plan_name") or "Free")
    plan_status = str(plan.get("s") or plan.get("status") or session.get("plan_status") or "trialing")
    return plan_key, plan_name, plan_status



def _merge_plan_state(*, session: dict[str, Any], data: dict[str, Any], canonical: str, save_json: JsonSaver, session_file) -> None:
    plan = data.get("p") or data.get("plan") or {}
    plan_key, plan_name, plan_status = _plan_status_from_payload(plan if isinstance(plan, dict) else {}, session)
    if plan_status == "active" and plan_key in {"monthly_pro", "annual_pro"}:
        if str(session.get("activation_notice_seen_for") or "") != plan_key:
            session["activation_notice_pending"] = plan_key
    session.update(
        {
            "account_uid": str(data.get("u") or data.get("userId") or session.get("account_uid") or ""),
            "plan_status": plan_status,
            "plan_key": plan_key,
            "plan_name": plan_name,
            "prompts_remaining": data.get("r") if "r" in data else data.get("remainingPrompts"),
            "hub_url": canonical,
        }
    )
    save_json(session_file, session)



def _runtime_call(
    *,
    canonical: str,
    http_json: HttpJson,
    op: str,
    auth_token: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any], str | None]:
    body = {"o": op}
    if isinstance(payload, dict) and payload:
        body.update(payload)
    return http_json(
        endpoint_path(canonical),
        method="POST",
        auth_token=auth_token,
        payload=body,
    )



def login_access(
    *,
    base_url: str,
    email: str,
    password: str,
    session_file,
    save_json: JsonSaver,
    canonical_hub_url: Callable[[str | None], str],
    http_json: HttpJson,
    refresh_session: Callable[[str | None], tuple[bool, str]],
) -> tuple[bool, str]:
    canonical = canonical_hub_url(base_url)
    status, body, auth_token = _runtime_call(
        canonical=canonical,
        http_json=http_json,
        op=_OP_LOGIN,
        payload={"e": str(email or "").strip(), "p": str(password or "")},
    )
    if status >= 400 or not body.get("ok") or not auth_token:
        return False, str(body.get("error") or "Control Tower sign in failed.")

    user = dict((body.get("d") or {}).get("u") or {}) if isinstance(body.get("d"), dict) else {}
    save_json(
        session_file,
        {
            "account_email": str(user.get("email") or email),
            "account_uid": str(user.get("uid") or ""),
            "plan_status": "trialing",
            "plan_key": "free",
            "hub_url": canonical,
            "auth_token": auth_token,
            "plan_name": "AriaOS",
            "activation_notice_pending": "",
            "activation_notice_seen_for": "",
            "prompts_remaining": None,
        },
    )
    return refresh_session(canonical)



def create_access(
    *,
    base_url: str,
    email: str,
    password: str,
    display_name: str,
    company: str,
    canonical_hub_url: Callable[[str | None], str],
    http_json: HttpJson,
) -> tuple[bool, str]:
    canonical = canonical_hub_url(base_url)
    status, body, _cookie = _runtime_call(
        canonical=canonical,
        http_json=http_json,
        op=_OP_SIGNUP,
        payload={
            "e": str(email or "").strip(),
            "p": str(password or ""),
            "d": str(display_name or "").strip(),
            "c": str(company or "").strip(),
        },
    )
    if status >= 400 or not body.get("ok"):
        return False, str(body.get("error") or "Control Tower signup failed.")
    return True, "Account created. Verify your email from the link we sent, then sign in."



def refresh_access(
    *,
    base_url: str | None,
    session_file,
    load_json: JsonLoader,
    save_json: JsonSaver,
    hub_url: str,
    canonical_hub_url: Callable[[str | None], str],
    http_json: HttpJson,
    clear_account_session: Callable[[], None],
) -> tuple[bool, str]:
    session = load_json(session_file)
    canonical = canonical_hub_url(base_url or session.get("hub_url") or hub_url)
    auth_token = str(session.get("auth_token") or "").strip()
    if not auth_token:
        return False, "No local Control Tower session found."

    status, body, _cookie = _runtime_call(
        canonical=canonical,
        http_json=http_json,
        op=_OP_SESSION,
        auth_token=auth_token,
    )
    if status >= 400 or not body.get("ok"):
        if status == 401:
            clear_account_session()
        return False, str(body.get("error") or "Failed to refresh Control Tower state.")

    data = dict(body.get("d") or {}) if isinstance(body.get("d"), dict) else {}
    user = dict(data.get("u") or {}) if isinstance(data.get("u"), dict) else {}
    session.update(
        {
            "account_email": str(user.get("e") or user.get("email") or session.get("account_email") or "Connected"),
            "account_uid": str(user.get("i") or user.get("uid") or session.get("account_uid") or ""),
        }
    )
    _merge_plan_state(session=session, data=data, canonical=canonical, save_json=save_json, session_file=session_file)
    return True, "Control Tower session refreshed."



def ensure_access(
    *,
    session_file,
    hub_url: str,
    load_json: JsonLoader,
    save_json: JsonSaver,
    canonical_hub_url: Callable[[str | None], str],
    http_json: HttpJson,
    clear_account_session: Callable[[], None],
) -> tuple[bool, str]:
    session = load_json(session_file)
    canonical = canonical_hub_url(session.get("hub_url") or hub_url)
    auth_token = str(session.get("auth_token") or "").strip()
    if not auth_token:
        return False, "Connect a Control Tower account first."

    status, body, _cookie = _runtime_call(
        canonical=canonical,
        http_json=http_json,
        op=_OP_CLAIM,
        auth_token=auth_token,
    )
    if status >= 400 or not body.get("ok"):
        if status == 401:
            clear_account_session()
        return False, str(body.get("error") or "Prompt access could not be verified.")

    data = dict(body.get("d") or {}) if isinstance(body.get("d"), dict) else {}
    _merge_plan_state(session=session, data=data, canonical=canonical, save_json=save_json, session_file=session_file)
    if not bool(data.get("a")):
        return False, str(data.get("m") or "Prompt access could not be verified.")
    remaining = data.get("r")
    if remaining is None:
        return True, "Prompt access granted."
    return True, f"Prompt access granted. {remaining} free prompt(s) remaining."




def prepare_task(
    *,
    task_id: str,
    session_id: str,
    goal: str,
    requested_model: str,
    image: dict[str, Any] | None,
    max_budget_usd: float,
    session_file,
    hub_url: str,
    load_json: JsonLoader,
    save_json: JsonSaver,
    canonical_hub_url: Callable[[str | None], str],
    http_json: HttpJson,
    clear_account_session: Callable[[], None],
    device: dict[str, str],
    runtime: dict[str, str],
    account_uid: str,
    active_app: str = "Terminal Aria",
    local_time: str = "",
) -> tuple[bool, dict[str, Any]]:
    session = load_json(session_file)
    canonical = canonical_hub_url(session.get("hub_url") or hub_url)
    auth_token = str(session.get("auth_token") or "").strip()
    if not auth_token:
        return False, {
            "message": "Connect a Control Tower account first.",
            "taskId": task_id,
            "sessionId": session_id,
        }

    task_model = str(requested_model or "").strip() or str(runtime.get("model") or "gpt-5.4")

    status, body, _cookie = _runtime_call(
        canonical=canonical,
        http_json=http_json,
        op=_OP_PREPARE,
        auth_token=auth_token,
        payload={
            "t": str(task_id or "").strip(),
            "s": str(session_id or "").strip(),
            "g": str(goal or "").strip(),
            "x": task_model,
            "i": image if isinstance(image, dict) else None,
            "b": float(max_budget_usd or 0.0),
            "d": str(device.get("device_id") or ""),
            "h": str(device.get("machine_uid") or ""),
            "n": str(device.get("device_name") or "Aria VM"),
            "m": task_model,
            "a": str(active_app or "Terminal Aria"),
            "z": str(local_time or ""),
            "q": str(account_uid or ""),
        },
    )
    if status >= 400 or not body.get("ok"):
        if status == 401:
            clear_account_session()
        return False, {
            "message": str(body.get("error") or "Remote task access denied."),
            "taskId": task_id,
            "sessionId": session_id,
        }

    data = dict(body.get("d") or {}) if isinstance(body.get("d"), dict) else {}
    _merge_plan_state(session=session, data=data, canonical=canonical, save_json=save_json, session_file=session_file)
    if not bool(data.get("a")):
        return False, {
            "message": str(data.get("m") or "Remote task access denied."),
            "taskId": task_id,
            "sessionId": session_id,
        }
    envelope = data.get("x") if isinstance(data.get("x"), dict) else {}
    return True, dict(envelope or {})



def sync_device(
    *,
    current_task_id: str,
    vm_status: str,
    remote_enabled: bool,
    session_file,
    hub_url: str,
    load_json: JsonLoader,
    canonical_hub_url: Callable[[str | None], str],
    http_json: HttpJson,
    clear_account_session: Callable[[], None],
    device: dict[str, str],
) -> tuple[bool, dict[str, Any]]:
    session = load_json(session_file)
    canonical = canonical_hub_url(session.get("hub_url") or hub_url)
    auth_token = str(session.get("auth_token") or "").strip()
    if not auth_token or not str(session.get("account_uid") or "").strip():
        return False, {"message": "No linked Control Tower session."}

    status, body, _cookie = _runtime_call(
        canonical=canonical,
        http_json=http_json,
        op=_OP_DEVICE,
        auth_token=auth_token,
        payload={
            "d": str(device.get("device_id") or ""),
            "h": str(device.get("machine_uid") or ""),
            "n": str(device.get("device_name") or "Aria VM"),
            "s": "gtk_terminal",
            "t": str(current_task_id or "").strip(),
            "v": str(vm_status or "ready").strip() or "ready",
            "r": bool(remote_enabled),
            "c": {
                "remoteTerminal": True,
                "computerUse": True,
                "vmTools": True,
            },
        },
    )
    if status >= 400 or not body.get("ok"):
        if status == 401:
            clear_account_session()
        return False, {"message": str(body.get("error") or "Remote device sync failed.")}
    data = body.get("d") if isinstance(body.get("d"), dict) else {}
    return True, dict(data or {})



def sync_task(
    *,
    task_id: str,
    status: str,
    session_id: str,
    latest_summary: str,
    latest_result: str,
    error: str,
    spent_usd: float | None,
    usage: dict[str, Any] | None,
    event_text: str,
    event_kind: str,
    ack_message_ids: list[str] | None,
    latest_screenshot_data_url: str,
    latest_screenshot_captured_at: str,
    session_file,
    hub_url: str,
    load_json: JsonLoader,
    canonical_hub_url: Callable[[str | None], str],
    http_json: HttpJson,
    http_upload_binary: HttpUploadBinary,
    decode_data_url: Callable[[str], tuple[str, bytes]],
    clear_account_session: Callable[[], None],
) -> tuple[bool, dict[str, Any]]:
    session = load_json(session_file)
    canonical = canonical_hub_url(session.get("hub_url") or hub_url)
    auth_token = str(session.get("auth_token") or "").strip()
    if not auth_token:
        return False, {"message": "No local Control Tower session found."}

    payload: dict[str, Any] = {}
    if status:
        payload["v"] = str(status).strip()
    if session_id:
        payload["s"] = str(session_id).strip()
    if latest_summary:
        payload["y"] = str(latest_summary).strip()
    if latest_result:
        payload["r"] = str(latest_result).strip()
    if error:
        payload["e"] = str(error).strip()
    if spent_usd is not None:
        payload["$"] = float(spent_usd)
    if isinstance(usage, dict) and usage:
        payload["u"] = dict(usage)
    if event_text:
        payload["x"] = str(event_text).strip()
    if event_kind:
        payload["k"] = str(event_kind).strip()
    if ack_message_ids:
        payload["a"] = [str(value).strip() for value in ack_message_ids if str(value).strip()]
    if latest_screenshot_captured_at:
        payload["z"] = str(latest_screenshot_captured_at).strip()

    if latest_screenshot_data_url:
        try:
            content_type, binary = decode_data_url(latest_screenshot_data_url)
            ticket_status, ticket_body, _cookie = _runtime_call(
                canonical=canonical,
                http_json=http_json,
                op=_OP_SCREENSHOT,
                auth_token=auth_token,
                payload={"t": str(task_id).strip(), "c": content_type},
            )
            ticket = dict(ticket_body.get("d") or {}) if isinstance(ticket_body, dict) else {}
            upload_url = str(ticket.get("u") or "").strip()
            upload_headers = dict(ticket.get("h") or {}) if isinstance(ticket.get("h"), dict) else {}
            if ticket_status < 400 and upload_url:
                upload_status, _upload_body = http_upload_binary(
                    upload_url,
                    method="PUT",
                    data=binary,
                    headers={str(key): str(value) for key, value in upload_headers.items() if str(key).strip()},
                )
                if 200 <= upload_status < 300:
                    payload["i"] = str(ticket.get("r") or "").strip()
                    payload["p"] = str(ticket.get("p") or "").strip()
                else:
                    return False, {"message": f"Remote screenshot upload failed ({upload_status})."}
            else:
                return False, {"message": str(ticket_body.get("error") or "Remote screenshot ticket request failed.")}
        except Exception as exc:
            return False, {"message": f"Remote screenshot upload failed: {exc}"}

    status_code, body, _cookie = _runtime_call(
        canonical=canonical,
        http_json=http_json,
        op=_OP_SYNC,
        auth_token=auth_token,
        payload={"t": str(task_id).strip(), "p": payload},
    )
    if status_code >= 400 or not body.get("ok"):
        if status_code == 401:
            clear_account_session()
        return False, {"message": str(body.get("error") or "Remote task sync failed.")}
    data = dict(body.get("d") or {}) if isinstance(body.get("d"), dict) else {}
    return True, dict(data.get("t") or {})



def report_usage(
    *,
    payload: dict[str, Any],
    session_file,
    hub_url: str,
    load_json: JsonLoader,
    canonical_hub_url: Callable[[str | None], str],
    http_json: HttpJson,
    clear_account_session: Callable[[], None],
) -> tuple[bool, str]:
    session = load_json(session_file)
    canonical = canonical_hub_url(session.get("hub_url") or hub_url)
    auth_token = str(session.get("auth_token") or "").strip()
    if not auth_token:
        return False, "No local Control Tower session found."

    status, body, _cookie = _runtime_call(
        canonical=canonical,
        http_json=http_json,
        op=_OP_USAGE,
        auth_token=auth_token,
        payload={"p": payload},
    )
    if status >= 400 or not body.get("ok"):
        if status == 401:
            clear_account_session()
        return False, str(body.get("error") or "Task usage could not be reported.")
    data = dict(body.get("d") or {}) if isinstance(body.get("d"), dict) else {}
    return True, str(data.get("m") or "Task usage reported.")
