from __future__ import annotations

import math
import time

import gi

HAS_GI_CAIRO = False
try:
    gi.require_foreign("cairo")
    import cairo  # type: ignore
    HAS_GI_CAIRO = True
except Exception:  # pragma: no cover
    cairo = None

from gi.repository import GLib, Gtk, Pango


class VoiceOrb(Gtk.DrawingArea):
    def __init__(self) -> None:
        super().__init__()
        self.set_content_width(340)
        self.set_content_height(340)
        self.set_hexpand(False)
        self.set_vexpand(False)
        self._state = "idle"
        self._phase = 0.0
        self._pulse = 0.0
        self._burst = 0.0
        self._last_tick = time.monotonic()
        self.add_css_class("voice-orb")
        if HAS_GI_CAIRO and cairo is not None:
            self.set_draw_func(self._draw)
        else:
            self.add_css_class("voice-orb-fallback")
            self._sync_fallback_classes()
        GLib.timeout_add(16, self._tick)

    def set_state(self, state: str) -> None:
        value = str(state or "idle").strip().lower() or "idle"
        if value != self._state:
            self._state = value
            self._burst = max(self._burst, 0.55 if value == "speaking" else 0.28)
            self._sync_fallback_classes()
            self.queue_draw()

    def pulse(self, amount: float = 0.3) -> None:
        self._burst = max(self._burst, min(1.0, max(0.0, float(amount))))
        if not HAS_GI_CAIRO:
            self.set_opacity(min(1.0, 0.88 + self._burst * 0.12))

    def _sync_fallback_classes(self) -> None:
        if HAS_GI_CAIRO:
            return
        for name in (
            "voice-orb-state-idle",
            "voice-orb-state-listening",
            "voice-orb-state-thinking",
            "voice-orb-state-speaking",
            "voice-orb-state-error",
        ):
            self.remove_css_class(name)
        self.add_css_class(f"voice-orb-state-{self._state}")

    def _tick(self) -> bool:
        now = time.monotonic()
        delta = min(0.06, max(0.008, now - self._last_tick))
        self._last_tick = now
        speed = {
            "idle": 0.6,
            "listening": 2.2,
            "thinking": 1.1,
            "speaking": 3.1,
            "error": 1.5,
        }.get(self._state, 1.0)
        target = {
            "idle": 0.06,
            "listening": 0.34,
            "thinking": 0.18,
            "speaking": 0.62,
            "error": 0.28,
        }.get(self._state, 0.12)
        self._phase += delta * speed * 3.4
        self._pulse += (target - self._pulse) * min(1.0, delta * 5.5)
        self._burst *= 0.92
        if not HAS_GI_CAIRO:
            self.set_opacity(min(1.0, 0.84 + self._pulse * 0.18 + self._burst * 0.08))
        self.queue_draw()
        return True

    def _palette(self) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        if self._state == "error":
            return (1.0, 0.36, 0.52), (1.0, 0.61, 0.68)
        if self._state == "speaking":
            return (0.03, 0.93, 1.0), (0.45, 0.95, 1.0)
        if self._state == "thinking":
            return (0.06, 0.80, 0.94), (0.37, 0.88, 0.98)
        return (0.06, 0.83, 0.96), (0.35, 0.94, 1.0)

    def _draw(self, _area: Gtk.DrawingArea, cr, width: int, height: int) -> None:
        if not HAS_GI_CAIRO:
            return
        if width <= 0 or height <= 0:
            return
        core_rgb, glow_rgb = self._palette()
        cx = width / 2.0
        cy = height / 2.0
        base = min(width, height) * 0.18
        beat = math.sin(self._phase * (2.6 if self._state == "listening" else 1.9))
        thinking_wave = math.sin(self._phase * 0.7 + 1.4)
        radius = base * (1.0 + self._pulse * 0.11 + self._burst * 0.08 + beat * 0.025)

        cr.set_source_rgba(0.01, 0.04, 0.08, 0.45)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        for idx in range(4):
            spread = 1.2 + idx * 0.22 + self._pulse * 0.16
            alpha = 0.22 - idx * 0.045 + self._burst * 0.04
            cr.set_source_rgba(glow_rgb[0], glow_rgb[1], glow_rgb[2], max(0.02, alpha))
            cr.arc(cx, cy, radius * spread, 0, math.tau)
            cr.fill()

        for ring_idx in range(3):
            scale = 1.16 + ring_idx * 0.18
            wobble = 1.0 + math.sin(self._phase * (0.9 + ring_idx * 0.33) + ring_idx) * 0.04
            rx = radius * scale * wobble
            ry = radius * (0.88 + ring_idx * 0.08) * wobble
            cr.set_source_rgba(glow_rgb[0], glow_rgb[1], glow_rgb[2], 0.18 - ring_idx * 0.03)
            cr.set_line_width(1.2)
            cr.save()
            cr.translate(cx, cy)
            cr.rotate(self._phase * (0.25 + ring_idx * 0.11) + ring_idx * 0.8)
            cr.scale(rx, ry)
            cr.arc(0, 0, 1.0, 0, math.tau)
            cr.restore()
            cr.stroke()

            orbit_angle = self._phase * (1.2 + ring_idx * 0.33) + ring_idx * 1.9
            dot_x = cx + math.cos(orbit_angle) * rx
            dot_y = cy + math.sin(orbit_angle) * ry
            cr.set_source_rgba(glow_rgb[0], glow_rgb[1], glow_rgb[2], 0.72)
            cr.arc(dot_x, dot_y, 3.4 + ring_idx * 0.8, 0, math.tau)
            cr.fill()

        cr.set_source_rgba(core_rgb[0], core_rgb[1], core_rgb[2], 0.95)
        cr.arc(cx, cy, radius, 0, math.tau)
        cr.fill()

        cr.set_source_rgba(0.88, 1.0, 1.0, 0.52)
        cr.arc(cx - radius * 0.22, cy - radius * 0.28, radius * 0.28, 0, math.tau)
        cr.fill()

        if self._state == "speaking":
            for ripple_idx in range(2):
                ripple_radius = radius * (1.45 + ripple_idx * 0.3 + thinking_wave * 0.05)
                cr.set_source_rgba(glow_rgb[0], glow_rgb[1], glow_rgb[2], 0.22 - ripple_idx * 0.07)
                cr.set_line_width(2.0 - ripple_idx * 0.5)
                cr.arc(cx, cy, ripple_radius, 0, math.tau)
                cr.stroke()


class VoiceOverlay(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("voice-overlay")
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_halign(Gtk.Align.FILL)
        self.set_valign(Gtk.Align.FILL)
        self.set_visible(False)

        stage = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        stage.add_css_class("voice-overlay-stage")
        stage.set_halign(Gtk.Align.CENTER)
        stage.set_valign(Gtk.Align.CENTER)
        stage.set_hexpand(True)
        stage.set_vexpand(True)
        stage.set_margin_top(36)
        stage.set_margin_bottom(36)
        stage.set_margin_start(36)
        stage.set_margin_end(36)

        self.kicker = Gtk.Label(label="VOICE MODE")
        self.kicker.add_css_class("voice-kicker")
        self.kicker.set_halign(Gtk.Align.CENTER)
        stage.append(self.kicker)

        self.orb = VoiceOrb()
        stage.append(self.orb)

        self.state_label = Gtk.Label(label="Listening")
        self.state_label.add_css_class("voice-state-label")
        self.state_label.set_halign(Gtk.Align.CENTER)
        stage.append(self.state_label)

        self.user_label = Gtk.Label(label="")
        self.user_label.add_css_class("voice-user-label")
        self.user_label.set_wrap(True)
        self.user_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.user_label.set_justify(Gtk.Justification.CENTER)
        self.user_label.set_max_width_chars(56)
        self.user_label.set_halign(Gtk.Align.CENTER)
        self.user_label.set_visible(False)
        stage.append(self.user_label)

        self.assistant_label = Gtk.Label(label="")
        self.assistant_label.add_css_class("voice-assistant-label")
        self.assistant_label.set_wrap(True)
        self.assistant_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.assistant_label.set_justify(Gtk.Justification.CENTER)
        self.assistant_label.set_max_width_chars(64)
        self.assistant_label.set_halign(Gtk.Align.CENTER)
        self.assistant_label.set_visible(False)
        stage.append(self.assistant_label)

        self.hint_label = Gtk.Label(label="Tap Voice Mode again to exit")
        self.hint_label.add_css_class("voice-hint-label")
        self.hint_label.set_halign(Gtk.Align.CENTER)
        stage.append(self.hint_label)

        self.append(stage)
        self._voice_name = "marin"
        self._refresh_kicker()

    def _refresh_kicker(self) -> None:
        self.kicker.set_label(f"VOICE MODE · {self._voice_name.upper()}")

    def set_language(self, language: str) -> None:
        _ = language

    def set_voice_name(self, voice_name: str) -> None:
        self._voice_name = str(voice_name or "marin").strip().lower() or "marin"
        self._refresh_kicker()

    def set_mode_visible(self, visible: bool) -> None:
        self.set_visible(bool(visible))

    def set_state(self, state: str) -> None:
        value = str(state or "idle").strip().lower() or "idle"
        label = {
            "idle": "Ready",
            "listening": "Listening",
            "thinking": "Thinking",
            "speaking": "Speaking",
            "error": "Reconnecting",
        }.get(value, "Listening")
        self.state_label.set_label(label)
        self.orb.set_state(value)

    def set_user_text(self, text: str) -> None:
        cleaned = str(text or "").strip()
        self.user_label.set_label(cleaned)
        self.user_label.set_visible(bool(cleaned))
        if cleaned:
            self.orb.pulse(0.26)

    def set_assistant_text(self, text: str) -> None:
        cleaned = str(text or "").strip()
        self.assistant_label.set_label(cleaned)
        self.assistant_label.set_visible(bool(cleaned))
        if cleaned:
            self.orb.pulse(0.42)

    def set_error(self, text: str) -> None:
        cleaned = str(text or "").strip()
        self.set_state("error")
        self.assistant_label.set_label(cleaned)
        self.assistant_label.set_visible(bool(cleaned))

    def clear_transcript(self) -> None:
        self.user_label.set_label("")
        self.user_label.set_visible(False)
        self.assistant_label.set_label("")
        self.assistant_label.set_visible(False)
