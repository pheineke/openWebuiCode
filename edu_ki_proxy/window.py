"""Native GNOME window (GTK4 + libadwaita + WebKitGTK) around the local web UI.

The page fills the whole window. Only the window buttons (top right) and an invisible drag
strip are overlaid, so the page's own layout (sidebar divider, panels) runs edge to edge.

Login happens in this same window: it loads the university's Shibboleth login, and once Open WebUI
is reached it reads the session (cookies + token) out of WebKit and hands it to the proxy, so no
second browser window is needed.
"""
import json
import os
import re
import threading
import time
from urllib.parse import urlparse

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("WebKit", "6.0")
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk, WebKit
except (ImportError, AttributeError, ValueError) as e:
    # AttributeError: `gi` is only a folder without PyGObject (another package ships gi/overrides/...).
    # ValueError: PyGObject is there, but GTK 4, libadwaita or WebKitGTK 6 is not.
    # Re-raised as ImportError so the caller falls back to a Chromium window.
    where = ", ".join(getattr(globals().get("gi"), "__path__", []))
    raise ImportError(f"GTK-Fenster nicht verfügbar: {e}" + (f" (gi aus {where})" if where else "")) from e

from . import auth, backends, providers  # noqa: E402

DRAG_HEIGHT = 40        # px: height of the page's top bar, which the drag strip covers
DRAG_DEFAULT = 270      # px: until the page reports where its buttons are, only the sidebar header drags


class LoginFlow:
    """Provider login inside the window: load the provider, wait for its token, capture the session.

    A token is only accepted once the provider confirms it: a stale one left in WebKit's storage
    would otherwise "log in" at once without asking.
    """

    def __init__(self, view: WebKit.WebView, app_url: str, show_login_bar, set_label):
        self.view, self.app_url, self.show_bar, self.set_label = view, app_url, show_login_bar, set_label
        self.provider: dict | None = None
        self.backend = None
        self.base_host = None
        self.active = False
        self.checking = False
        self.rejected: set[str] = set()

    def start(self, pid: str | None = None) -> None:
        self.provider = providers.get(pid) if pid else (providers.default() or (providers.load() or [None])[0])
        self.backend = backends.get(self.provider.get("type")) if self.provider else None
        if not self.backend:
            return
        self.base_host = urlparse(self.provider["base_url"]).hostname
        self.set_label(f"Anmeldung bei {self.provider.get('name') or self.base_host}")
        self.active, self.checking, self.rejected = True, False, set()
        self.show_bar(True)
        self.view.load_uri(self.provider["base_url"])
        GLib.timeout_add(700, self._poll)

    def cancel(self) -> None:
        self.active = False
        self.show_bar(False)
        self.view.load_uri(self.app_url)

    def _poll(self) -> bool:
        if not self.active:
            return False
        if not self.checking and urlparse(self.view.get_uri() or "").hostname == self.base_host and not self.view.is_loading():
            self.view.evaluate_javascript(self.backend.token_js(), -1, None, None, None, self._on_token)
        return True

    def _on_token(self, view, res) -> None:
        try:
            value = view.evaluate_javascript_finish(res)
            token = value.to_string() if value.is_string() else None
        except GLib.Error:
            token = None
        if token and self.active and not self.checking and token not in self.rejected:
            self.checking = True
            self._collect(token)

    def _collect(self, token: str) -> None:
        provider, backend = self.provider, self.backend

        def got(manager, res):
            try:
                jar = manager.get_cookies_finish(res)
            except GLib.Error:
                jar = []
            state = {"token": token, "captured_at": int(time.time()),
                     "user_agent": self.view.get_settings().get_user_agent(),
                     "cookies": [{"name": c.get_name(), "value": c.get_value(), "domain": c.get_domain(),
                                  "path": c.get_path()} for c in jar]}
            # the check is a network round trip: keep it off the UI thread
            threading.Thread(target=lambda: GLib.idle_add(self._checked, provider, state,
                                                          backend.check_session(provider, state)),
                             daemon=True).start()

        self.view.get_network_session().get_cookie_manager().get_cookies(provider["base_url"], None, got)

    def _checked(self, provider: dict, state: dict, ok: bool) -> bool:
        self.checking = False
        if not self.active or provider is not self.provider:
            return False
        if not ok:
            self.rejected.add(state["token"])     # wait for the page to show its login and a new token
            return False
        from . import server
        self.active = False
        server.install_session(provider["id"], state)
        self.show_bar(False)
        self.view.load_uri(self.app_url)
        return False


def forget_logins(session: WebKit.NetworkSession, keep_host: str) -> None:
    """Drop cookies and storage of every site but the app itself, so the next login really asks.
    (Shibboleth's IdP lives on another domain than Open WebUI, hence all sites.)"""
    T = WebKit.WebsiteDataTypes
    types = (T.COOKIES | T.LOCAL_STORAGE | T.SESSION_STORAGE | T.INDEXEDDB_DATABASES | T.DOM_CACHE
             | T.SERVICE_WORKER_REGISTRATIONS)
    manager = session.get_website_data_manager()

    def fetched(m, res):
        try:
            records = [r for r in m.fetch_finish(res) if r.get_name() != keep_host]
        except GLib.Error:
            return
        if records:
            m.remove(types, records, None, lambda m2, r2: m2.remove_finish(r2))

    manager.fetch(types, None, fetched)


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
    # Two segments: the page's sidebar button sits between them and must stay clickable.
    handles = [Gtk.WindowHandle(halign=Gtk.Align.START, valign=Gtk.Align.START, height_request=DRAG_HEIGHT) for _ in range(2)]
    drag = {"segments": [(0, DRAG_DEFAULT)], "login": False}

    def refresh_handles() -> None:
        for i, h in enumerate(handles):
            seg = drag["segments"][i] if i < len(drag["segments"]) else None
            h.set_visible(bool(seg) and not drag["login"])
            if seg:
                h.set_margin_start(seg[0])
                h.set_size_request(seg[1] - seg[0], DRAG_HEIGHT)

    refresh_handles()

    # Bar shown only while logging in (the page is then a foreign site, so it has no header of its own).
    login_bar = Gtk.WindowHandle(visible=False)
    cancel = Gtk.Button(label="← Zurück zur App", valign=Gtk.Align.CENTER)
    row = Gtk.Box(spacing=12, margin_start=8, height_request=DRAG_HEIGHT)
    row.append(cancel)
    label = Gtk.Label(label="Anmeldung", css_classes=["dim-label"])
    row.append(label)
    login_bar.set_child(row)

    def show_login_bar(on: bool) -> None:
        login_bar.set_visible(on)
        drag["login"] = on
        refresh_handles()

    flow = LoginFlow(view, url, show_login_bar, lambda text: label.set_text(text))
    picker = {"last": None, "chat": None}      # file dialog: folder of the last attachment, the chat's folder
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
            flow.start(msg.get("provider"))
        if msg.get("logout"):      # forget the university's login too, so the next login asks again
            forget_logins(session, urlparse(url).hostname)
        if "drag" in msg:          # draggable areas: everywhere in the header except the page's buttons
            try:
                drag["segments"] = [(max(0, int(a)), int(b)) for a, b in msg["drag"] if int(b) > int(a)][:len(handles)]
            except (TypeError, ValueError):
                return
            refresh_handles()
        if "bg" in msg:
            chrome(str(msg.get("bg", "")), str(msg.get("mode", "auto")))
        if "open" in msg:          # a link in an answer: the desktop's browser, never this window
            uri = str(msg.get("open") or "")
            if re.match(r"(?i)(https?|mailto):", uri):
                Gtk.UriLauncher.new(uri).launch(win, None, None, None)
        if "pickdir" in msg:       # the chat's working folder: second choice for the file dialog's start
            picker["chat"] = str(msg.get("pickdir") or "") or None

    manager.connect("script-message-received::shell", on_message)
    view.load_uri(url)

    inner = Gtk.Overlay(child=view, hexpand=True, vexpand=True)
    for h in handles:
        inner.add_overlay(h)
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    content.append(login_bar)
    content.append(inner)

    # Window buttons live in a bar exactly as tall as the page's header and are centred in it, so they
    # line up with the page's own header buttons whatever size the theme draws them.
    # Both sides: the desktop decides where the buttons go (GNOME: close on the right; KDE: minimize,
    # maximize, close; some put them on the left). The page leaves room for them (report_controls).
    outer = Gtk.Overlay(child=content)
    sides = {}
    for side, align in ((Gtk.PackType.START, Gtk.Align.START), (Gtk.PackType.END, Gtk.Align.END)):
        sides[side] = Gtk.WindowControls(side=side, valign=Gtk.Align.CENTER, margin_start=10, margin_end=10)
        bar = Gtk.Box(halign=align, valign=Gtk.Align.START, height_request=DRAG_HEIGHT)
        bar.append(sides[side])
        outer.add_overlay(bar)
    settings = Gtk.Settings.get_default()

    def apply_layout(*_):
        """The desktop's button layout without "icon"/"menu": KDE's default starts with the window icon,
        which this app does not install, so GTK would draw a broken image there."""
        parts = ((settings.get_property("gtk-decoration-layout") or ":close") + ":").split(":")[:2]
        keep = [",".join(b for b in p.split(",") if b.strip() in ("minimize", "maximize", "close")) for p in parts]
        for c in sides.values():
            c.set_decoration_layout(":".join(keep))
        GLib.timeout_add(100, report_controls)

    reported = {"value": None}

    def report_controls(*_) -> bool:
        """Tell the page how wide the buttons are on each side (CSS --wc-start/--wc-end)."""
        if urlparse(view.get_uri() or "").netloc != app_netloc:
            return False
        if any(not c.get_empty() and c.get_width() == 0 for c in sides.values()):
            return True                                  # not laid out yet: try again shortly
        value = tuple(0 if c.get_empty() else c.get_width() + 10 for c in (sides[Gtk.PackType.START], sides[Gtk.PackType.END]))
        if value != reported["value"]:
            reported["value"] = value
            view.evaluate_javascript(
                f"document.documentElement.style.setProperty('--wc-start','{value[0]}px');"
                f"document.documentElement.style.setProperty('--wc-end','{value[1]}px');"
                "window.reportDrag && reportDrag()", -1, None, None, None, None, None)
        return False

    def page_loaded(_view, event):
        if event == WebKit.LoadEvent.FINISHED:
            reported["value"] = None                     # a reloaded page has lost the values
            GLib.timeout_add(50, report_controls)

    view.connect("load-changed", page_loaded)
    settings.connect("notify::gtk-decoration-layout", apply_layout)
    apply_layout()

    # <input type=file> (attachments): GTK's own dialog, in this process (on screen in ~0.1 s). Through the
    # desktop portal, which WebKit would use, GNOME opens the dialog in Files (Nautilus): seconds, more so
    # when Files is not running yet. GtkFileChooserDialog is deprecated in favour of the portal-based
    # GtkFileDialog, but still part of GTK 4.
    def choose_files(_view, request):
        multiple = request.get_select_multiple()
        dialog = Gtk.FileChooserDialog(title="Dateien anhängen" if multiple else "Datei anhängen", transient_for=win,
                                       modal=True, action=Gtk.FileChooserAction.OPEN)
        dialog.add_button("Abbrechen", Gtk.ResponseType.CANCEL)
        dialog.add_button("Anhängen", Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        dialog.set_select_multiple(multiple)
        start = next((d for d in (picker["last"], picker["chat"], GLib.get_home_dir()) if d and os.path.isdir(d)), None)
        if start:          # also keeps GTK from opening on "Recent", which reads the whole recent-files list
            dialog.set_current_folder(Gio.File.new_for_path(start))
        if (accept := request.get_mime_types_filter()) is not None:
            dialog.set_filter(accept)

        def done(d, response):
            paths = []
            if response == Gtk.ResponseType.ACCEPT:
                files = d.get_files()
                paths = [p for i in range(files.get_n_items()) if (p := files.get_item(i).get_path())]
            if paths:
                picker["last"] = os.path.dirname(paths[0])
                request.select_files(paths)
            else:
                request.cancel()
            d.destroy()

        dialog.connect("response", done)
        dialog.present()
        return True

    view.connect("run-file-chooser", choose_files)

    win = Adw.ApplicationWindow(application=app, title="EDU-KI Code", default_width=1200, default_height=800, content=outer)
    win.connect("map", lambda *_: GLib.timeout_add(100, report_controls))
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
