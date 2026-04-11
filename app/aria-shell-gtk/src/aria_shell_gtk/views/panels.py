from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
import os

from gi.repository import GLib, Gtk

from ..services.bootstrap import default_model_api
from ..services.core import RuntimeState, RuntimeStore

GTK_LOG_PATH = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "ariaos" / "gtk_terminal.log"


def _log_debug(message: str) -> None:
    try:
        GTK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with GTK_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.utcnow().isoformat()}Z {message}\n")
    except Exception:
        pass


def show_access_dialog(
    parent: Gtk.Widget,
    state: RuntimeState,
    service: RuntimeStore,
    on_success,
    *,
    initial_mode: str | None = None,
):
    _log_debug("show_access_dialog start")
    root = parent.get_root()
    win = Gtk.Window(title="Unlock Terminal Aria")
    if isinstance(root, Gtk.Window):
        win.set_transient_for(root)
        application = root.get_application()
        if application is not None:
            win.set_application(application)
    _log_debug(f"show_access_dialog root={type(root).__name__ if root is not None else 'None'}")
    win.set_modal(True)
    win.set_default_size(460, 560)
    win.set_resizable(True)

    shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    shell.add_css_class("overlay-card")
    shell.set_margin_top(18)
    shell.set_margin_bottom(18)
    shell.set_margin_start(18)
    shell.set_margin_end(18)

    inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    inner.set_margin_top(16)
    inner.set_margin_bottom(16)
    inner.set_margin_start(16)
    inner.set_margin_end(16)

    title = Gtk.Label(label="CONTROL TOWER ACCESS")
    title.add_css_class("brand-label")
    title.set_halign(Gtk.Align.START)
    inner.append(title)

    subtitle = Gtk.Label(
        label="Continue with Google to unlock this VM. Email and password remain available as a fallback."
    )
    subtitle.add_css_class("overlay-text")
    subtitle.set_wrap(True)
    subtitle.set_xalign(0)
    inner.append(subtitle)

    status = Gtk.Label(label="Sign in or create an account to unlock this VM.")
    status.add_css_class("overlay-meta")
    status.set_wrap(True)
    status.set_xalign(0)
    inner.append(status)

    google = Gtk.Button(label="Continue with Google")
    google.add_css_class("dock-send")
    google.set_halign(Gtk.Align.FILL)
    google.set_hexpand(True)
    inner.append(google)

    fallback_toggle = Gtk.Button(label="Use email and password instead")
    fallback_toggle.add_css_class("dock-button")
    fallback_toggle.set_halign(Gtk.Align.START)
    inner.append(fallback_toggle)

    fallback_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    fallback_box.set_visible(False)

    url_entry = Gtk.Entry()
    url_entry.add_css_class("dock-input")
    url_entry.set_placeholder_text("Control Tower URL")
    url_entry.set_text(state.hub_url)
    fallback_box.append(_with_label("Control Tower URL", url_entry))

    email_entry = Gtk.Entry()
    email_entry.add_css_class("dock-input")
    email_entry.set_placeholder_text("Email")
    email_entry.set_text("" if state.account_email == "Not connected" else state.account_email)
    fallback_box.append(_with_label("Email", email_entry))

    password_entry = Gtk.Entry()
    password_entry.add_css_class("dock-input")
    password_entry.set_placeholder_text("Password")
    password_entry.set_visibility(False)
    fallback_box.append(_with_label("Password", password_entry))

    display_name_entry = Gtk.Entry()
    display_name_entry.add_css_class("dock-input")
    display_name_entry.set_placeholder_text("Display name")
    fallback_box.append(_with_label("Display name (signup)", display_name_entry))

    company_entry = Gtk.Entry()
    company_entry.add_css_class("dock-input")
    company_entry.set_placeholder_text("Company")
    fallback_box.append(_with_label("Company (signup)", company_entry))

    fallback_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    sign_in = Gtk.Button(label="Sign In")
    sign_in.add_css_class("dock-send")
    sign_up = Gtk.Button(label="Create Account")
    sign_up.add_css_class("dock-stop")
    billing = Gtk.Button(label="Open Billing")
    billing.add_css_class("dock-button")
    fallback_actions.append(sign_in)
    fallback_actions.append(sign_up)
    fallback_actions.append(billing)
    fallback_box.append(fallback_actions)
    inner.append(fallback_box)

    shell.append(inner)
    win.set_child(shell)

    def close_window(*_args):
        _log_debug("show_access_dialog close_window")
        win.destroy()
        return False

    def common_values():
        return (
            url_entry.get_text().strip().rstrip("/"),
            email_entry.get_text().strip(),
            password_entry.get_text().strip(),
            display_name_entry.get_text().strip(),
            company_entry.get_text().strip(),
        )

    def set_busy(is_busy: bool, message: str | None = None) -> None:
        google.set_sensitive(not is_busy)
        fallback_toggle.set_sensitive(not is_busy)
        sign_in.set_sensitive(not is_busy)
        sign_up.set_sensitive(not is_busy)
        billing.set_sensitive(not is_busy)
        url_entry.set_sensitive(not is_busy)
        email_entry.set_sensitive(not is_busy)
        password_entry.set_sensitive(not is_busy)
        display_name_entry.set_sensitive(not is_busy)
        company_entry.set_sensitive(not is_busy)
        if message is not None:
            status.set_label(message)

    def _show_trial_notice() -> None:
        current = service.read()
        if current.plan_status == "active":
            return
        remaining = current.prompts_remaining
        if remaining is None:
            remaining = 4
        notice = Gtk.Window(title="Free Access", modal=True, transient_for=parent.get_root())
        notice.set_default_size(420, 220)
        notice.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.add_css_class("overlay-card")
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        inner.set_margin_top(16)
        inner.set_margin_bottom(16)
        inner.set_margin_start(16)
        inner.set_margin_end(16)

        title_label = Gtk.Label(label="FREE ACCESS ENABLED")
        title_label.add_css_class("brand-label")
        title_label.set_halign(Gtk.Align.START)
        inner.append(title_label)

        body_label = Gtk.Label(
            label=(
                f"This account can use Terminal Aria for {remaining} free prompt(s). "
                "For unlimited access, activate the monthly subscription in Control Tower."
            )
        )
        body_label.add_css_class("overlay-text")
        body_label.set_wrap(True)
        body_label.set_xalign(0)
        inner.append(body_label)

        close_btn = Gtk.Button(label="Continue")
        close_btn.add_css_class("dock-send")
        close_btn.set_halign(Gtk.Align.END)
        close_btn.connect("clicked", lambda *_args: notice.destroy())
        inner.append(close_btn)

        box.append(inner)
        notice.set_child(box)
        notice.present()

    def finish_auth(ok: bool, message: str) -> None:
        _log_debug(f"show_access_dialog finish_auth ok={ok} message={message}")
        set_busy(False, message)
        if not ok:
            return
        _show_trial_notice()
        on_success()
        close_window()

    def signin(_btn=None):
        base_url, email, password, _display_name, _company = common_values()
        if not base_url or not email or not password:
            status.set_label("Control Tower URL, email, and password are required.")
            return
        set_busy(True, "Signing in…")

        def worker() -> None:
            try:
                ok, message = service.login_access(base_url, email, password)
            except Exception as exc:
                ok, message = False, f"Control Tower is unreachable: {exc}"
            GLib.idle_add(finish_auth, ok, message)

        threading.Thread(target=worker, daemon=True).start()

    def signup(_btn=None):
        base_url, email, password, display_name, company = common_values()
        if not base_url or not email or not password:
            status.set_label("Control Tower URL, email, and password are required.")
            return
        set_busy(True, "Creating account…")

        def worker() -> None:
            try:
                ok, message = service.create_access(base_url, email, password, display_name, company)
            except Exception as exc:
                ok, message = False, f"Control Tower is unreachable: {exc}"
            if ok:
                def _finish_signup_only() -> bool:
                    set_busy(False, message)
                    return False
                GLib.idle_add(_finish_signup_only)
                return
            GLib.idle_add(finish_auth, ok, message)

        threading.Thread(target=worker, daemon=True).start()

    def google_signin(_btn=None):
        base_url, _email, _password, _display_name, _company = common_values()
        if not base_url:
            status.set_label("Control Tower URL is required.")
            return
        _log_debug(f"show_access_dialog google_signin base_url={base_url}")
        service.open_google_connect(base_url)
        status.set_label("Continue with Google in the browser. This window will close automatically after the VM syncs.")
        google.set_sensitive(False)
        fallback_toggle.set_sensitive(False)

        def poll_google_sync() -> bool:
            current = service.read()
            if current.auth_ready and current.account_email != "Not connected":
                finish_auth(True, f"Connected as {current.account_email}.")
                return False
            return True

        GLib.timeout_add_seconds(1, poll_google_sync)

    def toggle_fallback(_btn=None):
        visible = fallback_box.get_visible()
        fallback_box.set_visible(not visible)
        fallback_toggle.set_label("Hide email and password" if not visible else "Use email and password instead")

    def apply_mode(mode: str | None) -> None:
        normalized = str(mode or "").strip().lower()
        if normalized not in {"signin", "signup"}:
            return
        fallback_box.set_visible(True)
        fallback_toggle.set_label("Hide email and password")
        if normalized == "signup":
            status.set_label("Create an account to unlock this VM.")
            display_name_entry.grab_focus()
        else:
            status.set_label("Sign in to unlock this VM.")
            email_entry.grab_focus()

    google.connect("clicked", google_signin)
    fallback_toggle.connect("clicked", toggle_fallback)
    sign_in.connect("clicked", signin)
    sign_up.connect("clicked", signup)
    billing.connect("clicked", lambda _btn: service.open_billing())
    win.connect("close-request", close_window)
    apply_mode(initial_mode)
    win.present()
    _log_debug("show_access_dialog presented")
    return win


def show_key_dialog(
    parent: Gtk.Widget,
    service: RuntimeStore,
    on_success,
):
    secrets = service.user_secrets()
    root = parent.get_root()
    win = Gtk.Window(title="Connect OpenAI Key")
    if isinstance(root, Gtk.Window):
        win.set_transient_for(root)
        application = root.get_application()
        if application is not None:
            win.set_application(application)
    win.set_modal(True)
    win.set_default_size(460, 360)
    win.set_resizable(True)

    shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    shell.add_css_class("overlay-card")
    shell.set_margin_top(18)
    shell.set_margin_bottom(18)
    shell.set_margin_start(18)
    shell.set_margin_end(18)

    inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    inner.set_margin_top(16)
    inner.set_margin_bottom(16)
    inner.set_margin_start(16)
    inner.set_margin_end(16)

    title = Gtk.Label(label="OPENAI BYOK")
    title.add_css_class("brand-label")
    title.set_halign(Gtk.Align.START)
    inner.append(title)

    subtitle = Gtk.Label(label="Supported models: GPT-5.4 and GPT-5.4 mini. Your key stays locally on this VM.")
    subtitle.add_css_class("overlay-text")
    subtitle.set_wrap(True)
    subtitle.set_xalign(0)
    inner.append(subtitle)

    key_entry = Gtk.Entry()
    key_entry.add_css_class("dock-input")
    key_entry.set_placeholder_text("OpenAI API key")
    key_entry.set_visibility(False)
    key_entry.set_text(str(secrets.get("openai_api_key") or ""))
    inner.append(_with_label("OpenAI API key", key_entry))

    base_entry = Gtk.Entry()
    base_entry.add_css_class("dock-input")
    base_entry.set_placeholder_text("Base URL")
    base_entry.set_text(str(secrets.get("openai_base_url") or default_model_api()))
    inner.append(_with_label("Base URL", base_entry))

    model_entry = Gtk.Entry()
    model_entry.add_css_class("dock-input")
    model_entry.set_placeholder_text("Model")
    model_entry.set_text(str(secrets.get("openai_model") or "gpt-5.4"))
    inner.append(_with_label("Model", model_entry))

    status = Gtk.Label(label="The key stays on this VM and restarts the local backend.")
    status.add_css_class("overlay-meta")
    status.set_wrap(True)
    status.set_xalign(0)
    inner.append(status)

    actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    save = Gtk.Button(label="Save Key")
    save.add_css_class("dock-send")
    cancel = Gtk.Button(label="Cancel")
    cancel.add_css_class("dock-button")
    actions.append(save)
    actions.append(cancel)
    inner.append(actions)

    shell.append(inner)
    win.set_child(shell)

    def close_window(*_args):
        win.destroy()
        return False

    def save_key(_btn=None):
        api_key = key_entry.get_text().strip()
        if not api_key:
            status.set_label("OpenAI API key is required.")
            return
        status.set_label("Restarting the local backend...")
        ok = service.save_openai_settings(
            api_key=api_key,
            base_url=base_entry.get_text().strip(),
            model=(model_entry.get_text().strip() or "gpt-5.4"),
        )
        if not ok:
            status.set_label("Failed to restart the backend with the saved key.")
            return
        on_success()
        close_window()

    save.connect("clicked", save_key)
    cancel.connect("clicked", lambda _btn: close_window())
    win.connect("close-request", close_window)
    win.present()
    return win


def show_model_dialog(
    parent: Gtk.Widget,
    service: RuntimeStore,
    on_success,
):
    root = parent.get_root()
    runtime = service.remote_openai_runtime()
    current_model = str(runtime.get("model") or "gpt-5.4")
    win = Gtk.Window(title="Choose model")
    if isinstance(root, Gtk.Window):
        win.set_transient_for(root)
        application = root.get_application()
        if application is not None:
            win.set_application(application)
    win.set_modal(True)
    win.set_default_size(420, 240)
    win.set_resizable(False)

    shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    shell.add_css_class("overlay-card")
    shell.set_margin_top(18)
    shell.set_margin_bottom(18)
    shell.set_margin_start(18)
    shell.set_margin_end(18)

    inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    inner.set_margin_top(16)
    inner.set_margin_bottom(16)
    inner.set_margin_start(16)
    inner.set_margin_end(16)

    title = Gtk.Label(label="CHOOSE MODEL")
    title.add_css_class("brand-label")
    title.set_halign(Gtk.Align.START)
    inner.append(title)

    subtitle = Gtk.Label(label="Pick the OpenAI model used by Terminal Aria and remote tasks started from this VM.")
    subtitle.add_css_class("overlay-text")
    subtitle.set_wrap(True)
    subtitle.set_xalign(0)
    inner.append(subtitle)

    status = Gtk.Label(label=f"Current model: {current_model}")
    status.add_css_class("overlay-meta")
    status.set_wrap(True)
    status.set_xalign(0)
    inner.append(status)

    actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

    def close_window(*_args):
        win.destroy()
        return False

    def choose(model_name: str):
        ok = service.save_openai_model(model_name)
        if not ok:
            status.set_label("Failed to save the selected model.")
            return
        on_success()
        close_window()

    for model_name, description in (
        ("gpt-5.4", "Higher accuracy for harder computer-use tasks."),
        ("gpt-5.4-mini", "Lower cost and faster responses with slightly lower accuracy."),
    ):
        button = Gtk.Button(label=model_name)
        button.add_css_class("dock-button" if model_name != current_model else "dock-send")
        button.connect("clicked", lambda _btn, value=model_name: choose(value))
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        row.append(button)
        meta = Gtk.Label(label=description)
        meta.add_css_class("overlay-meta")
        meta.set_wrap(True)
        meta.set_xalign(0)
        row.append(meta)
        actions.append(row)

    cancel = Gtk.Button(label="Cancel")
    cancel.add_css_class("dock-button")
    cancel.connect("clicked", lambda _btn: close_window())
    actions.append(cancel)
    inner.append(actions)

    shell.append(inner)
    win.set_child(shell)
    win.connect("close-request", close_window)
    win.present()
    return win


def _with_label(label_text: str, widget: Gtk.Widget) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    label = Gtk.Label(label=label_text)
    label.add_css_class("overlay-meta")
    label.set_halign(Gtk.Align.START)
    box.append(label)
    box.append(widget)
    return box
