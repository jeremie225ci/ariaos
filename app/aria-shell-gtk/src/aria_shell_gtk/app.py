from __future__ import annotations

import os
import sys
import json
from pathlib import Path
from typing import TYPE_CHECKING

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, Gdk, Gtk
except Exception as exc:  # pragma: no cover
    print("GTK4/libadwaita is required to run aria-shell-gtk.", file=sys.stderr)
    print(str(exc), file=sys.stderr)
    raise

from .services.core import RuntimeState, RuntimeStore
from .services.loop import LocalLoop
from .views.shell import ShellView
from .views.intro import IntroView
from .views.console import ConsoleView
try:
    from .views.panels import show_key_dialog
except Exception:  # pragma: no cover - packaged client remaps this module
    from .views.cards import show_key_dialog


REQUESTED_VIEW_PATH = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "ariaos" / "requested_start_view"
ONBOARDING_STATE_PATH = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "ariaos" / "onboarding_state.json"


class AriaShellApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id="com.ariaos.Shell")
        self.service = RuntimeStore()
        self.start_view = self._requested_start_view() or os.environ.get("ARIA_SHELL_START_VIEW", "intro").strip().lower()
        self.bridge = None
        self.window = None
        self.stack = None
        self.home_view = None
        self.terminal_view = None
        self.intro_key_window = None
        self._state_stamp = (0.0, 0.0)
        self._state_watch_started = False

    def _restart_terminal_bridge(self, state: RuntimeState) -> None:
        if self.terminal_view is None:
            return
        if getattr(self.terminal_view, "current_running", False):
            return
        if self.bridge is not None:
            try:
                self.bridge.shutdown()
            except Exception:
                pass
        self.bridge = self._build_bridge(state)
        self.bridge.start()
        self.terminal_view.bridge = self.bridge

    def do_activate(self) -> None:  # noqa: N802
        self.service.ensure_local_link_server()
        requested_view = self._requested_start_view()
        env_view = os.environ.get("ARIA_SHELL_START_VIEW", "").strip().lower()
        candidate = requested_view or env_view or self._default_start_view()
        if self._should_force_intro():
            candidate = "intro"
        if candidate in {"intro", "home", "terminal"}:
            self.start_view = candidate
        if self.window is None:
            self.window = Gtk.ApplicationWindow(application=self)
            self.window.set_title("AriaOS")
            self.window.set_default_size(760, 560)
            self.window.set_resizable(True)
            self.window.set_decorated(True)
            self.window.set_deletable(True)
            self._install_css()
            self._build_headerbar()
            self._build_ui()
            self._state_stamp = self.service.state_stamp()
        else:
            if self.start_view == "terminal":
                self.show_terminal()
            elif self.start_view == "home":
                self.show_home()
            else:
                self.window.present()
        self.window.present()
        if not self._state_watch_started:
            try:
                from gi.repository import GLib
                GLib.timeout_add_seconds(1, self._poll_state_files)
                self._state_watch_started = True
            except Exception:
                pass

    def _build_headerbar(self) -> None:
        if self.window is None:
            return
        header = Gtk.HeaderBar()
        header.add_css_class("shell-headerbar")
        header.set_show_title_buttons(True)
        header.set_title_widget(Gtk.Label(label=""))
        self.window.set_titlebar(header)

    def _install_css(self) -> None:
        provider = Gtk.CssProvider()
        client_dir = Path(os.environ.get("ARIA_CLIENT_DIR") or "/opt/aria-client")
        theme_override = str(os.environ.get("ARIA_THEME_PATH") or "").strip()
        theme_path = Path(theme_override) if theme_override else client_dir / "share" / "AriaApp" / "theme.css"
        if not theme_path.exists():
            theme_path = Path(__file__).resolve().parent / "theme.css"
        provider.load_from_path(str(theme_path))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def _load_logo_picture(self):
        state = self.service.read()
        if state.logo_path.exists():
            picture = Gtk.Picture.new_for_filename(str(state.logo_path))
            picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            return picture
        return None

    def _build_ui(self) -> None:
        state = self.service.read()
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)

        if self.start_view == "terminal":
            self._ensure_terminal_view(state)
            self.window.set_child(self.stack)
            self._apply_window_mode("terminal")
            self.terminal_view.prepare_for_display()
            self.stack.set_visible_child_name("terminal")
            return

        if self.start_view == "intro":
            intro_logo = self._load_logo_picture()
            intro = IntroView(
                on_complete=self._complete_intro,
                logo_picture=intro_logo,
            )
            self.stack.add_named(intro, "intro")

        self._replace_home_view(state)

        self.window.set_child(self.stack)
        if self.start_view == "home":
            self._apply_window_mode("home")
            self.stack.set_visible_child_name("home")
        else:
            self._apply_window_mode("intro")
            self.stack.set_visible_child_name("intro")

    def show_home(self, *_args) -> None:
        state = self.service.read()
        self._replace_home_view(state)
        self._apply_window_mode("home")
        self.stack.set_visible_child_name("home")
        self._state_stamp = self.service.state_stamp()

    def show_terminal(self, *_args) -> None:
        if self.terminal_view is None:
            state = self.service.read()
            self._ensure_terminal_view(state)
        self._apply_window_mode("terminal")
        self.terminal_view.prepare_for_display()
        self.stack.set_visible_child_name("terminal")
        self._state_stamp = self.service.state_stamp()

    def launch_home_window(self, *_args) -> None:
        self.service.launch_shell_window("home")

    def launch_terminal_window(self, *_args) -> None:
        self.service.launch_shell_window("terminal")

    def _apply_window_mode(self, view_name: str) -> None:
        if self.window is None:
            return
        if view_name == "terminal":
            self.window.set_title("Terminal Aria")
            self.window.set_resizable(True)
            try:
                self.window.unfullscreen()
            except Exception:
                pass
            try:
                self.window.unmaximize()
            except Exception:
                pass
            self.window.set_default_size(1040, 620)
        elif view_name == "intro":
            self.window.set_title("AriaOS")
            self.window.set_default_size(920, 560)
        else:
            self.window.set_title("Aria Home")
            self.window.set_default_size(920, 640)

    def do_shutdown(self) -> None:  # noqa: N802
        if self.bridge is not None:
            self.bridge.shutdown()
        Gtk.Application.do_shutdown(self)

    def _build_bridge(self, state: RuntimeState):
        return LocalLoop(state)

    def _ensure_terminal_view(self, state: RuntimeState) -> ConsoleView:
        if self.bridge is None:
            self.bridge = self._build_bridge(state)
            self.bridge.start()
        if self.terminal_view is None:
            self.terminal_view = ConsoleView(
                state=state,
                service=self.service,
                bridge=self.bridge,
                go_home=self.show_home,
            )
            self.stack.add_named(self.terminal_view, "terminal")
        return self.terminal_view

    def _replace_home_view(self, state: RuntimeState) -> ShellView:
        refreshed = ShellView(state, self.service, self.show_terminal)
        if self.home_view is not None:
            self.stack.remove(self.home_view)
        self.home_view = refreshed
        self.stack.add_named(self.home_view, "home")
        return self.home_view

    @staticmethod
    def _requested_start_view() -> str:
        try:
            raw = REQUESTED_VIEW_PATH.read_text(encoding="utf-8").strip().lower()
        except Exception:
            return ""
        try:
            REQUESTED_VIEW_PATH.unlink()
        except Exception:
            pass
        return raw if raw in {"intro", "home", "terminal"} else ""

    def _default_start_view(self) -> str:
        try:
            state = self.service.read()
        except Exception:
            return "intro"
        if state.api_key_ready or self._onboarding_done():
            return "home"
        return "intro"

    def _should_force_intro(self) -> bool:
        try:
            state = self.service.read()
        except Exception:
            return True
        return not self._onboarding_done() and not bool(getattr(state, "api_key_ready", False))

    @staticmethod
    def _onboarding_done() -> bool:
        try:
            payload = json.loads(ONBOARDING_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return False
        return bool(payload.get("done"))

    @staticmethod
    def _mark_onboarding_done() -> None:
        try:
            ONBOARDING_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            ONBOARDING_STATE_PATH.write_text(json.dumps({"done": True}, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _complete_intro(self, *_args) -> None:
        try:
            state = self.service.read()
        except Exception:
            state = None
        if not bool(getattr(state, "api_key_ready", False)):
            self._show_intro_key_dialog()
            return
        self._mark_onboarding_done()
        self.show_home()

    def _show_intro_key_dialog(self) -> None:
        if self.intro_key_window is not None:
            self.intro_key_window.present()
            return

        def on_success():
            self.intro_key_window = None
            self._mark_onboarding_done()
            self.show_home()

        self.intro_key_window = show_key_dialog(self.stack, self.service, on_success)
        self.intro_key_window.connect("close-request", lambda *_args: self._clear_intro_key_window())
        self.intro_key_window.connect("destroy", lambda *_args: self._clear_intro_key_window())

    def _clear_intro_key_window(self, *_args):
        self.intro_key_window = None
        return False

    def _poll_state_files(self) -> bool:
        stamp = self.service.state_stamp()
        if stamp != self._state_stamp:
            self._state_stamp = stamp
            state = self.service.read()
            if state.api_key_ready and not self._onboarding_done():
                self._mark_onboarding_done()
            if self.home_view is not None and self.stack is not None and self.stack.get_visible_child_name() == "home":
                self._replace_home_view(state)
            if self.terminal_view is not None:
                self._restart_terminal_bridge(state)
                self.terminal_view.refresh_state(state)
        return True


def main() -> int:
    app = AriaShellApp()
    return app.run(sys.argv)
