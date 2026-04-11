from __future__ import annotations

from gi.repository import Gtk

from ..services.core import RuntimeState, RuntimeStore
from .panels import show_key_dialog


class ShellView(Gtk.Box):
    def __init__(self, state: RuntimeState, service: RuntimeStore, show_terminal):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("app-shell")
        self.set_hexpand(True)
        self.set_vexpand(True)

        self.state = state
        self.service = service
        self.show_terminal = show_terminal
        self.key_window = None

        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        frame.add_css_class("home-window")
        frame.set_hexpand(True)
        frame.set_vexpand(True)
        frame.set_margin_top(18)
        frame.set_margin_bottom(18)
        frame.set_margin_start(18)
        frame.set_margin_end(18)
        self.append(frame)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        header.set_margin_top(18)
        header.set_margin_start(18)
        header.set_margin_end(18)
        frame.append(header)

        logo_tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        logo_tile.add_css_class("home-logo-tile")
        logo_tile.set_size_request(164, 164)
        logo_tile.set_halign(Gtk.Align.START)
        header.append(logo_tile)

        if state.logo_path.exists():
            logo = Gtk.Image.new_from_file(str(state.logo_path))
            logo.set_pixel_size(136)
            logo.set_halign(Gtk.Align.CENTER)
            logo.set_valign(Gtk.Align.CENTER)
            logo.add_css_class("home-logo")
            logo_tile.append(logo)

        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        hero.set_hexpand(True)
        hero.set_valign(Gtk.Align.CENTER)
        header.append(hero)

        kicker = Gtk.Label(label="ARIAOS")
        kicker.add_css_class("shell-kicker")
        kicker.set_halign(Gtk.Align.START)
        hero.append(kicker)

        title = Gtk.Label(label="Aria Home")
        title.add_css_class("home-headline")
        title.set_xalign(0)
        title.set_halign(Gtk.Align.START)
        hero.append(title)

        subtitle = Gtk.Label(
            label=(
                "AI operating system for agents. Launch Terminal Aria, store your OpenAI key on this VM, "
                "open files, and keep the local-first experience focused on the machine itself."
            )
        )
        subtitle.add_css_class("home-copy")
        subtitle.set_wrap(True)
        subtitle.set_xalign(0)
        hero.append(subtitle)

        primary_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        primary_actions.set_margin_start(18)
        primary_actions.set_margin_end(18)
        frame.append(primary_actions)

        launch_button = Gtk.Button(label="Launch Terminal Aria")
        launch_button.add_css_class("nav-button-accent")
        launch_button.connect("clicked", lambda *_args: show_terminal())
        primary_actions.append(launch_button)

        key_label = "Replace OpenAI Key" if state.api_key_ready else "Connect OpenAI Key"
        key_button = Gtk.Button(label=key_label)
        key_button.add_css_class("dock-button")
        key_button.connect("clicked", self._show_api_key_dialog)
        primary_actions.append(key_button)

        files_button = Gtk.Button(label="Files")
        files_button.add_css_class("dock-button")
        files_button.connect("clicked", lambda *_args: service.open_files())
        primary_actions.append(files_button)

        browser_button = Gtk.Button(label="Browser")
        browser_button.add_css_class("dock-button")
        browser_button.connect("clicked", lambda *_args: service.open_browser())
        primary_actions.append(browser_button)

        scroller = Gtk.ScrolledWindow()
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(False)
        frame.append(scroller)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        body.set_margin_top(8)
        body.set_margin_bottom(18)
        body.set_margin_start(18)
        body.set_margin_end(18)
        scroller.set_child(body)

        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        status_row.add_css_class("home-status-strip")
        body.append(status_row)
        version_label = f"v{state.app_build}" if state.app_build else (state.app_version or "dev")
        runtime_label = "READY" if state.api_key_ready else "KEY REQUIRED"
        status_row.append(self._build_status_card("MODE", f"AriaOS · {version_label}"))
        status_row.append(self._build_status_card("MODEL", state.openai_model.upper()))
        status_row.append(self._build_status_card("RUNTIME", runtime_label))

        section = Gtk.Label(label="WORKSPACE")
        section.add_css_class("sidebar-section")
        section.set_halign(Gtk.Align.START)
        body.append(section)

        actions = Gtk.Grid()
        actions.add_css_class("home-actions-grid")
        actions.set_column_spacing(14)
        actions.set_row_spacing(14)
        body.append(actions)

        action_specs = [
            ("Terminal Aria", "Open the local AI workspace window.", lambda *_: show_terminal()),
            ("OpenAI Key", "Connect or replace the local OpenAI key and choose the model.", self._show_api_key_dialog),
            ("Files", "Open the local workspace and exported files.", lambda *_: service.open_files()),
            ("Browser", "Open the browser already available in the VM.", lambda *_: service.open_browser()),
        ]

        for index, (label, description, callback) in enumerate(action_specs):
            actions.attach(self._build_action_card(label, description, callback), index % 2, index // 2, 1, 1)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        footer.set_halign(Gtk.Align.FILL)
        body.append(footer)

        footer_text = Gtk.Label(
            label="AriaOS removes the hosted account flow and keeps the experience centered on the VM."
        )
        footer_text.add_css_class("home-footer-text")
        footer_text.set_hexpand(True)
        footer_text.set_xalign(0)
        footer.append(footer_text)

        close_button = Gtk.Button(label="Close")
        close_button.add_css_class("home-close-button")
        close_button.connect("clicked", self._close_window)
        footer.append(close_button)

    def _build_status_card(self, label: str, value: str) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("home-status-card")
        card.set_hexpand(True)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        inner.set_margin_top(14)
        inner.set_margin_bottom(14)
        inner.set_margin_start(14)
        inner.set_margin_end(14)
        card.append(inner)

        kicker = Gtk.Label(label=label)
        kicker.add_css_class("home-status-kicker")
        kicker.set_halign(Gtk.Align.START)
        inner.append(kicker)

        body = Gtk.Label(label=value)
        body.add_css_class("home-status-value")
        body.set_wrap(True)
        body.set_xalign(0)
        inner.append(body)
        return card

    def _build_action_card(self, title: str, body: str, callback) -> Gtk.Widget:
        button = Gtk.Button()
        button.add_css_class("home-action-card")
        button.set_hexpand(True)
        button.set_vexpand(True)
        button.connect("clicked", callback)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        inner.set_margin_top(18)
        inner.set_margin_bottom(18)
        inner.set_margin_start(18)
        inner.set_margin_end(18)

        title_label = Gtk.Label(label=title)
        title_label.add_css_class("home-action-title")
        title_label.set_xalign(0)
        inner.append(title_label)

        body_label = Gtk.Label(label=body)
        body_label.add_css_class("home-action-body")
        body_label.set_wrap(True)
        body_label.set_xalign(0)
        inner.append(body_label)

        button.set_child(inner)
        return button

    @staticmethod
    def _close_window(button: Gtk.Button, *_args) -> None:
        window = button.get_root()
        if isinstance(window, Gtk.Window):
            window.close()

    def _show_api_key_dialog(self, *_args) -> None:
        if self.key_window is not None:
            self.key_window.present()
            return

        def on_success():
            self.key_window = None

        self.key_window = show_key_dialog(self, self.service, on_success)
        self.key_window.connect("close-request", lambda *_args: self._clear_key_window())
        self.key_window.connect("destroy", lambda *_args: self._clear_key_window())

    def _clear_key_window(self, *_args):
        self.key_window = None
        return False
