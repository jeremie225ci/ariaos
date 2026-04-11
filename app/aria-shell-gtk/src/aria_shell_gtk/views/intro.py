from __future__ import annotations

from gi.repository import Gtk


class IntroView(Gtk.Box):
    SLIDES = (
        {
            "kicker": "WELCOME",
            "title": "Welcome to AriaOS Community",
            "body": (
                "Aria runs inside a dedicated virtual machine on your computer. "
                "This edition stays local-first and removes the hosted account flow."
            ),
            "points": (
                "Dedicated VM workspace",
                "Host OS stays outside the working perimeter",
                "No account creation inside the VM",
            ),
        },
        {
            "kicker": "LOCAL",
            "title": "Your OpenAI key stays on this VM",
            "body": (
                "After onboarding, AriaOS Community only asks for your OpenAI API key. "
                "It is stored locally on the VM and used to start the local runtime."
            ),
            "points": (
                "OpenAI key stored locally",
                "GPT-5.4 and GPT-5.4 mini supported",
                "No Control Tower dependency",
            ),
        },
        {
            "kicker": "WORKSPACE",
            "title": "One VM, direct access",
            "body": (
                "Use Terminal Aria inside the VM, keep your files local, and work from a simpler "
                "community edition focused on the on-device experience."
            ),
            "points": (
                "Terminal Aria inside the VM",
                "Local files and memory stay accessible",
                "Browser and desktop tools remain available",
            ),
        },
        {
            "kicker": "READY",
            "title": "Connect your OpenAI key to start",
            "body": (
                "Finish onboarding, save your OpenAI key locally, and AriaOS Community will open "
                "Aria Home with the local runtime flow."
            ),
            "points": (
                "No hosted sign-in step",
                "Key is saved locally on this VM",
                "Terminal unlocks after local key setup",
            ),
        },
    )

    def __init__(self, on_complete, logo_picture: Gtk.Picture | None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("intro-shell")
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._on_complete = on_complete
        self._index = 0

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_hexpand(True)
        outer.set_vexpand(True)
        outer.set_valign(Gtk.Align.CENTER)
        outer.set_halign(Gtk.Align.CENTER)
        self.append(outer)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        card.add_css_class("intro-card")
        card.set_size_request(960, 580)
        card.set_halign(Gtk.Align.CENTER)
        card.set_valign(Gtk.Align.CENTER)
        for margin_name in ("top", "bottom", "start", "end"):
            getattr(card, f"set_margin_{margin_name}")(36)
        outer.append(card)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        inner.set_valign(Gtk.Align.FILL)
        inner.set_halign(Gtk.Align.FILL)
        inner.set_vexpand(True)
        for margin_name in ("top", "bottom", "start", "end"):
            getattr(inner, f"set_margin_{margin_name}")(42)
        card.append(inner)

        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=28)
        hero.set_hexpand(True)
        inner.append(hero)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        left.set_hexpand(True)
        left.set_vexpand(True)
        hero.append(left)

        if logo_picture is not None:
            logo = Gtk.Picture.new_for_paintable(logo_picture.get_paintable())
            logo.set_size_request(220, 160)
            logo.set_content_fit(Gtk.ContentFit.CONTAIN)
            logo.set_halign(Gtk.Align.START)
            left.append(logo)

        self._step_label = Gtk.Label()
        self._step_label.add_css_class("intro-step")
        self._step_label.set_halign(Gtk.Align.START)
        left.append(self._step_label)

        self._kicker = Gtk.Label()
        self._kicker.add_css_class("intro-kicker")
        self._kicker.set_halign(Gtk.Align.START)
        left.append(self._kicker)

        self._title = Gtk.Label()
        self._title.add_css_class("intro-title")
        self._title.set_wrap(True)
        self._title.set_xalign(0)
        self._title.set_halign(Gtk.Align.FILL)
        left.append(self._title)

        self._body = Gtk.Label()
        self._body.add_css_class("intro-copy")
        self._body.set_wrap(True)
        self._body.set_xalign(0)
        self._body.set_halign(Gtk.Align.FILL)
        left.append(self._body)

        self._point_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._point_box.set_hexpand(True)
        left.append(self._point_box)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        right.add_css_class("intro-side-panel")
        right.set_size_request(260, -1)
        right.set_valign(Gtk.Align.FILL)
        hero.append(right)

        panel_title = Gtk.Label(label="COMMUNITY EDITION")
        panel_title.add_css_class("intro-panel-kicker")
        panel_title.set_halign(Gtk.Align.START)
        right.append(panel_title)

        panel_body = Gtk.Label(
            label=(
                "A local-first AriaOS build centered on the VM itself: onboarding, local OpenAI key, "
                "Terminal Aria, files, memory, and a simpler public distribution."
            )
        )
        panel_body.add_css_class("intro-panel-copy")
        panel_body.set_wrap(True)
        panel_body.set_xalign(0)
        right.append(panel_body)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        actions.set_halign(Gtk.Align.FILL)
        inner.append(actions)

        self._back_button = Gtk.Button(label="Back")
        self._back_button.add_css_class("dock-button")
        self._back_button.connect("clicked", self._show_previous)
        actions.append(self._back_button)

        self._next_button = Gtk.Button(label="Next")
        self._next_button.add_css_class("dock-send")
        self._next_button.connect("clicked", self._show_next)
        actions.append(self._next_button)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        actions.append(spacer)

        self._skip_button = Gtk.Button(label="Skip")
        self._skip_button.add_css_class("dock-button")
        self._skip_button.connect("clicked", lambda *_args: self._on_complete())
        actions.append(self._skip_button)

        self._render()

    def _render(self) -> None:
        slide = self.SLIDES[self._index]
        self._step_label.set_label(f"STEP {self._index + 1} / {len(self.SLIDES)}")
        self._kicker.set_label(str(slide["kicker"]))
        self._title.set_label(str(slide["title"]))
        self._body.set_label(str(slide["body"]))

        child = self._point_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self._point_box.remove(child)
            child = next_child

        for point in slide["points"]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.add_css_class("intro-point-row")
            bullet = Gtk.Label(label="•")
            bullet.add_css_class("intro-point-bullet")
            bullet.set_valign(Gtk.Align.START)
            row.append(bullet)

            label = Gtk.Label(label=str(point))
            label.add_css_class("intro-point-text")
            label.set_wrap(True)
            label.set_xalign(0)
            label.set_hexpand(True)
            row.append(label)
            self._point_box.append(row)

        last_slide = self._index == len(self.SLIDES) - 1
        self._back_button.set_sensitive(self._index > 0)
        self._next_button.set_label("Connect OpenAI Key" if last_slide else "Next")

    def _show_previous(self, *_args) -> None:
        if self._index <= 0:
            return
        self._index -= 1
        self._render()

    def _show_next(self, *_args) -> None:
        if self._index >= len(self.SLIDES) - 1:
            self._on_complete()
            return
        self._index += 1
        self._render()
