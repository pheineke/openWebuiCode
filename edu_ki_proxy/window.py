"""Native GNOME window (GTK4 + libadwaita + WebKitGTK) around the local web UI.

The page fills the whole window. Only the window buttons (top right) and an invisible drag
strip are overlaid, so the page's own layout (sidebar divider, panels) runs edge to edge.
"""
import json
import re

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Adw, Gdk, Gtk, WebKit  # noqa: E402

from . import auth  # noqa: E402

DRAG_HEIGHT = 40        # px: height of the page's top bar, which the drag strip covers
DRAG_DEFAULT = 270      # px: until the page reports where its buttons are, only the sidebar header drags


def build_window(app: Adw.Application, url: str):
    # persistent storage so localStorage (chosen model/folder/theme) survives restarts
    session = WebKit.NetworkSession.new(str(auth.STATE_DIR / "webkit-data"), str(auth.STATE_DIR / "webkit-cache"))
    view = WebKit.WebView(network_session=session)
    view.get_settings().set_property("enable-developer-extras", False)

    def chrome(bg: str, mode: str) -> None:
        """Window follows the page: scheme for the window buttons, background for the not-yet-painted view."""
        scheme = {"light": Adw.ColorScheme.FORCE_LIGHT, "dark": Adw.ColorScheme.FORCE_DARK, "black": Adw.ColorScheme.FORCE_DARK}
        Adw.StyleManager.get_default().set_color_scheme(scheme.get(mode, Adw.ColorScheme.DEFAULT))
        rgba = Gdk.RGBA()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", bg) and rgba.parse(bg):
            view.set_background_color(rgba)

    chrome("#161616" if Adw.StyleManager.get_default().get_dark() else "#f6f6f4", "auto")

    manager = view.get_user_content_manager()
    manager.register_script_message_handler("shell", None)   # page -> native: theme colours

    handle = Gtk.WindowHandle(halign=Gtk.Align.START, valign=Gtk.Align.START, height_request=DRAG_HEIGHT, width_request=DRAG_DEFAULT)

    def on_message(_manager, value):
        try:
            msg = json.loads(value.to_string())
        except (ValueError, AttributeError):
            return
        if "dragEnd" in msg:     # page -> native: draggable area ends where the page's buttons begin
            handle.set_size_request(max(0, int(msg["dragEnd"])), DRAG_HEIGHT)
        if "bg" in msg:
            chrome(str(msg.get("bg", "")), str(msg.get("mode", "auto")))

    manager.connect("script-message-received::shell", on_message)
    view.load_uri(url)

    overlay = Gtk.Overlay(child=view)
    # Drag strip: moves the window, double-click maximises. It is an overlay, so it swallows pointer events
    # in its area: its width is set by the page so it never covers the page's own buttons.
    overlay.add_overlay(handle)
    # Window buttons live in a bar exactly as tall as the page's header and are centred in it, so they
    # line up with the page's own header buttons whatever size the theme draws them.
    controls = Gtk.WindowControls(side=Gtk.PackType.END, valign=Gtk.Align.CENTER, margin_end=10)
    bar = Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.START, height_request=DRAG_HEIGHT)
    bar.append(controls)
    overlay.add_overlay(bar)

    win = Adw.ApplicationWindow(application=app, title="EDU-KI Code", default_width=1200, default_height=800, content=overlay)

    keys = Gtk.ShortcutController()
    keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>r|F5"),
                                   action=Gtk.CallbackAction.new(lambda *_: view.reload() or True)))
    win.add_controller(keys)
    return win, view


def run(url: str) -> None:
    app = Adw.Application(application_id="de.edu_ki_rlp.code")
    app.connect("activate", lambda a: build_window(a, url)[0].present())
    app.run(None)
