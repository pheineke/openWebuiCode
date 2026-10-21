"""Native GNOME window (GTK4 + libadwaita + WebKitGTK) around the local web UI.

The page fills the whole window. Only the window buttons (top right) and an invisible drag
strip are overlaid, so the page's own layout (sidebar divider, panels) runs edge to edge.

Login happens in this same window: it loads the university's Shibboleth login, and once Open WebUI
is reached it reads the session (cookies + token) out of WebKit and hands it to the proxy, so no
second browser window is needed.
"""
import json
import re
import time
from urllib.parse import urlparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Adw, Gdk, GLib, Gtk, WebKit  # noqa: E402

from . import auth  # noqa: E402

DRAG_HEIGHT = 40        # px: height of the page's top bar, which the drag strip covers
DRAG_DEFAULT = 270      # px: until the page reports where its buttons are, only the sidebar header drags


class LoginFlow:
    """Shibboleth login inside the window: load the login page, wait for Open WebUI's token, capture the session."""

    def __init__(self, view: WebKit.WebView, app_url: str, show_login_bar):
        self.view, self.app_url, self.show_bar = view, app_url, show_login_bar
        self.base_host = urlparse(auth.BASE).hostname
        self.active = False

    def start(self) -> None:
        self.active = True
        self.show_bar(True)
        self.view.load_uri(auth.BASE)          # redirects to the Hochschule picker / Shibboleth IdP
        GLib.timeout_add(700, self._poll)

    def cancel(self) -> None:
        self.active = False
        self.show_bar(False)
        self.view.load_uri(self.app_url)

    def _poll(self) -> bool:
        if not self.active:
            return False
        if urlparse(self.view.get_uri() or "").hostname == self.base_host and not self.view.is_loading():
            self.view.evaluate_javascript("localStorage.getItem('token')", -1, None, None, None, self._on_token)
        return True

    def _on_token(self, view, res) -> None:
        try:
            value = view.evaluate_javascript_finish(res)
            token = value.to_string() if value.is_string() else None
        except GLib.Error:
            token = None
        if token and self.active:
            self.finish(token)

    def finish(self, token: str) -> None:
        self.active = False
        cookies = self.view.get_network_session().get_cookie_manager()

        def got(manager, res):
            try:
                jar = manager.get_cookies_finish(res)
            except GLib.Error:
                jar = []
            self._done(token, jar)

        cookies.get_cookies(auth.BASE, None, got)

    def _done(self, token: str, jar) -> None:
        from . import server
        server.install_session({
            "token": token, "captured_at": int(time.time()),
            "cookies": [{"name": c.get_name(), "value": c.get_value(), "domain": c.get_domain(), "path": c.get_path()} for c in jar]})
        self.show_bar(False)
        self.view.load_uri(self.app_url)


def build_window(app: Adw.Application, url: str):
    app_netloc = urlparse(url).netloc
    # persistent storage so localStorage (chosen model/folder/theme) and the Shibboleth cookies survive restarts
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

    # Drag strip over the page's header: moves the window, double-click maximises. It is an overlay, so it
    # swallows pointer events in its area: its width is set by the page so it never covers the page's buttons.
    handle = Gtk.WindowHandle(halign=Gtk.Align.START, valign=Gtk.Align.START, height_request=DRAG_HEIGHT, width_request=DRAG_DEFAULT)

    # Bar shown only while logging in (the page is then a foreign site, so it has no header of its own).
    login_bar = Gtk.WindowHandle(visible=False)
    cancel = Gtk.Button(label="← Zurück zur App", valign=Gtk.Align.CENTER)
    row = Gtk.Box(spacing=12, margin_start=8, height_request=DRAG_HEIGHT)
    row.append(cancel)
    row.append(Gtk.Label(label="Anmeldung bei EDU-KI-RLP", css_classes=["dim-label"]))
    login_bar.set_child(row)

    def show_login_bar(on: bool) -> None:
        login_bar.set_visible(on)
        handle.set_visible(not on)

    flow = LoginFlow(view, url, show_login_bar)
    cancel.connect("clicked", lambda *_: flow.cancel())

    manager = view.get_user_content_manager()
    manager.register_script_message_handler("shell", None)   # page -> native

    def on_message(_manager, value):
        # Only our own page may steer the window: during login this view shows foreign sites too.
        if urlparse(view.get_uri() or "").netloc != app_netloc:
            return
        try:
            msg = json.loads(value.to_string())
        except (ValueError, AttributeError):
            return
        if msg.get("login"):
            flow.start()
        if msg.get("logout"):      # forget the university's login cookie too, so the next login asks again
            session.get_website_data_manager().clear(WebKit.WebsiteDataTypes.COOKIES, 0, None, lambda m, r: m.clear_finish(r))
        if "dragEnd" in msg:       # draggable area ends where the page's buttons begin
            handle.set_size_request(max(0, int(msg["dragEnd"])), DRAG_HEIGHT)
        if "bg" in msg:
            chrome(str(msg.get("bg", "")), str(msg.get("mode", "auto")))

    manager.connect("script-message-received::shell", on_message)
    view.load_uri(url)

    inner = Gtk.Overlay(child=view, hexpand=True, vexpand=True)
    inner.add_overlay(handle)
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    content.append(login_bar)
    content.append(inner)

    # Window buttons live in a bar exactly as tall as the page's header and are centred in it, so they
    # line up with the page's own header buttons whatever size the theme draws them.
    controls = Gtk.WindowControls(side=Gtk.PackType.END, valign=Gtk.Align.CENTER, margin_end=10)
    bar = Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.START, height_request=DRAG_HEIGHT)
    bar.append(controls)
    outer = Gtk.Overlay(child=content)
    outer.add_overlay(bar)

    win = Adw.ApplicationWindow(application=app, title="EDU-KI Code", default_width=1200, default_height=800, content=outer)
    win.login_flow = flow

    keys = Gtk.ShortcutController()
    keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>r|F5"),
                                   action=Gtk.CallbackAction.new(lambda *_: view.reload() or True)))
    win.add_controller(keys)
    return win, view


def run(url: str) -> None:
    app = Adw.Application(application_id="de.edu_ki_rlp.code")
    app.connect("activate", lambda a: build_window(a, url)[0].present())
    app.run(None)
