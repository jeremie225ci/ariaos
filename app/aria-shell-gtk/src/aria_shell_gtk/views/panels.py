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
    del state, initial_mode
    _log_debug("show_access_dialog redirected to show_key_dialog")
    return show_key_dialog(parent, service, on_success)


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
