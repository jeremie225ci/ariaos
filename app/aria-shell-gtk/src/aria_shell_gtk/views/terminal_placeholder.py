from __future__ import annotations

from gi.repository import Gtk


class TerminalPlaceholder(Gtk.Box):
    def __init__(self, launch_legacy_terminal, go_home):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_margin_top(48)
        self.set_margin_bottom(48)
        self.set_margin_start(48)
        self.set_margin_end(48)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        card.add_css_class("placeholder-card")
        for margin_name in ("top", "bottom", "start", "end"):
            getattr(card, f"set_margin_{margin_name}")(28)

        kicker = Gtk.Label(label="WORKSPACE")
        kicker.add_css_class("shell-kicker")
        kicker.set_halign(Gtk.Align.START)
        card.append(kicker)

        title = Gtk.Label(label="Workspace tools are available in this build.")
        title.add_css_class("shell-title")
        title.set_wrap(True)
        title.set_xalign(0)
        card.append(title)

        body = Gtk.Label(label="Use the terminal entrypoint provided in this build.")
        body.add_css_class("shell-body")
        body.set_wrap(True)
        body.set_xalign(0)
        card.append(body)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        launch = Gtk.Button(label="Open Terminal")
        launch.add_css_class("primary-pill")
        launch.connect("clicked", lambda _btn: launch_legacy_terminal())
        back = Gtk.Button(label="Back Home")
        back.add_css_class("secondary-pill")
        back.connect("clicked", lambda _btn: go_home())
        actions.append(launch)
        actions.append(back)
        card.append(actions)

        self.append(card)
