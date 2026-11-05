"""Shibboleth (SAML) login without a browser, for Open WebUI behind a Shibboleth SP (EDU-KI-RLP).

The way a browser goes, done with plain HTTP:

    SP  /Shibboleth.sso/Login?entityID=<university IdP>   ->  IdP (redirects, maybe a storage check page)
    IdP login form (username + password)                  ->  IdP answers with an auto-submit SAML form
    SAML form posted to the SP                             ->  SP session cookie (_shibsession_...)
    Open WebUI /api/v1/auths/signin                        ->  token (the SP passes the user in a trusted header)

Pages that need a person (attribute release consent, terms of use, a one-time code) are not
answered here: they are described as a form (``Ask``) for the app to show, and the answer is sent
back with ``Flow.answer``.  The IdP's cookies are kept, so a later login while the university still
knows the person needs no password (``refresh``).  The password itself is never stored.
"""
import html
import http.cookiejar
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlparse

import httpx

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0 EDU-KI-Code"
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
           "Accept-Language": "de-DE,de;q=0.9,en;q=0.6"}


class ShibError(Exception):
    """Login failed; the message is for the person."""


class BadLogin(ShibError):
    """The IdP showed its login form again after the password was sent."""


class NeedPassword(ShibError):
    """The IdP wants the password (its session is gone): no silent login possible."""


# ---- pages and forms ---------------------------------------------------------------------------
@dataclass
class Field:
    name: str
    type: str                  # text, password, hidden, checkbox, radio, email, number, tel, ...
    value: str = ""
    checked: bool = False
    id: str = ""
    label: str = ""


@dataclass
class Form:
    action: str
    method: str
    fields: list = field(default_factory=list)       # Field
    buttons: list = field(default_factory=list)      # (name, value, label)
    rows: list = field(default_factory=list)         # table rows inside the form: [cell texts]
    text: list = field(default_factory=list)         # paragraphs inside the form
    items: list = field(default_factory=list)        # what the page shows, in order: ("text", str), ("table", rows), ("field", name)

    def has(self, kind: str) -> bool:
        return any(f.type == kind for f in self.fields)

    def names(self) -> set:
        return {f.name for f in self.fields} | {b[0] for b in self.buttons}

    def submit(self, button: str | None, values: dict | None = None) -> dict:
        """The fields a browser would send: hidden and filled ones, checked boxes, one button."""
        values = values or {}
        data: dict = {}
        for f in self.fields:
            if f.type in ("checkbox", "radio"):
                on = values[f.name] == f.value if f.type == "radio" and f.name in values else \
                     bool(values[f.name]) if f.type == "checkbox" and f.name in values else f.checked
                if on:
                    data.setdefault(f.name, []).append(f.value or "on")
            elif f.type not in ("submit", "button", "image", "reset", "file"):
                data.setdefault(f.name, []).append(str(values.get(f.name, f.value)))
        if button:
            b = next((b for b in self.buttons if b[0] == button), None)
            data.setdefault(button, []).append(b[1] if b else "")
        return data


class _Parser(HTMLParser):
    """Forms with their fields, buttons, labels, tables and the text of their blocks."""
    SKIP = {"script", "style", "template", "noscript"}
    BLOCKS = {"p", "div", "legend", "h1", "h2", "h3", "h4", "li", "section", "dd", "dt"}

    def __init__(self, url: str):
        super().__init__(convert_charrefs=True)
        self.url, self.forms, self.title = url, [], ""
        self.labels: dict = {}                 # element id -> label text
        self.form: Form | None = None
        self.skip = 0
        self.title_parts: list | None = None
        self.label: list | None = None         # [for, parts, field wrapped by the label]
        self.button: list | None = None        # [name, value, parts]
        self.blocks: list = []                 # open text blocks inside the form (innermost last)
        self.row: list | None = None
        self.cell: list | None = None
        self.table: list | None = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag in self.SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "title":
            self.title_parts = []
        elif tag == "label":
            self.label = [a.get("for", ""), [], None]
        elif tag == "form":
            self.form = Form(urljoin(self.url, html.unescape(a.get("action") or "") or self.url), (a.get("method") or "get").upper())
            self.forms.append(self.form)
            self.blocks = []
        if self.form is None:
            return
        if tag in self.BLOCKS:
            self._flush()
            self.blocks.append([])
        elif tag == "table":
            self._flush()
            self.table = []
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(", ")
        elif tag == "input":
            t = (a.get("type") or "text").lower()
            if t in ("submit", "image"):
                if a.get("name"):
                    self.form.buttons.append((a["name"], a.get("value", ""), a.get("value", "") or a["name"]))
                return
            if a.get("name"):
                f = Field(a["name"], t, a.get("value", ""), "checked" in a, a.get("id", ""))
                self.form.fields.append(f)
                self._field_item(f)
                if self.label is not None and self.label[2] is None:
                    self.label[2] = f
        elif tag == "button" and (a.get("type") or "submit").lower() == "submit" and a.get("name"):
            self.button = [a["name"], a.get("value", ""), []]
        elif tag == "textarea" and a.get("name"):
            self.form.fields.append(Field(a["name"], "textarea", "", False, a.get("id", "")))
            self._field_item(self.form.fields[-1])

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "title" and self.title_parts is not None:
            self.title, self.title_parts = _squash("".join(self.title_parts)), None
        elif tag == "label" and self.label is not None:
            text = _squash("".join(self.label[1]))
            if self.label[0]:
                self.labels[self.label[0]] = text
            if self.label[2] is not None and not self.label[2].label:
                self.label[2].label = text
            self.label = None
        if self.form is None:
            return
        if tag == "form":
            while self.blocks:                 # unclosed <p> and friends
                self._block()
            self.form = None
        elif tag == "button" and self.button is not None:
            name, value, parts = self.button
            self.form.buttons.append((name, value, _squash("".join(parts)) or value or name))
            self.button = None
        elif tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(_squash("".join(self.cell)).strip(" ,"))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if any(self.row):
                self.form.rows.append([c for c in self.row if c])
                if self.table is not None:
                    self.table.append([c for c in self.row if c])
            self.row = None
        elif tag == "table" and self.table is not None:
            if self.table:
                self.form.items.append(("table", self.table))
            self.table = None
        elif tag in self.BLOCKS and self.blocks:
            self._block()

    def _block(self) -> None:
        self._text(self.blocks.pop())

    def _text(self, parts: list) -> None:
        text = _squash("".join(parts))
        parts.clear()
        if text and self.form is not None:
            self.form.text.append(text)
            self.form.items.append(("text", text))

    def _flush(self) -> None:
        """Text read so far in the open blocks comes before what starts now."""
        for parts in self.blocks:
            self._text(parts)

    def _field_item(self, f: Field) -> None:
        if f.type in ("hidden", "submit", "button", "image", "reset"):
            return
        self._flush()
        if ("field", f.name) not in self.form.items:      # a radio group once
            self.form.items.append(("field", f.name))

    def handle_data(self, data):
        if self.skip:
            return
        if self.title_parts is not None:
            self.title_parts.append(data)
        if self.label is not None:
            self.label[1].append(data)         # labels belong to their fields, not to the text
        elif self.button is not None:
            self.button[2].append(data)
        elif self.cell is not None:
            self.cell.append(data)
        elif self.blocks:
            self.blocks[-1].append(data)


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class Page:
    url: str
    status: int
    title: str
    forms: list
    raw: str

    @classmethod
    def parse(cls, resp: httpx.Response) -> "Page":
        p = _Parser(str(resp.url))
        try:
            p.feed(resp.text)
        except Exception:      # broken markup: whatever was read so far
            pass
        for f in p.forms:
            for x in f.fields:
                x.label = x.label or p.labels.get(x.id, "")
        return cls(str(resp.url), resp.status_code, p.title, p.forms, resp.text)

    def error(self) -> str:
        """The IdP's error message on the page, if it shows one."""
        body = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", "", self.raw)
        for m in re.finditer(r'(?is)<(\w+)\b[^>]*class=["\'][^"\']*\b(?:form-error|error|alert|output--error)[^"\']*["\'][^>]*>(.*?)</\1>', body):
            text = _squash(html.unescape(re.sub(r"<[^>]+>", " ", m.group(2))))
            if text and len(text) < 400:
                return text
        return ""

    def summary(self) -> str:
        text = self.error() or self.title
        if not text:
            body = re.sub(r"(?is)<(script|style|noscript|head)\b.*?</\1>", "", self.raw)
            text = _squash(html.unescape(re.sub(r"<[^>]+>", " ", body)))[:300]
        return text


def _kind(f: Form) -> str:
    names = f.names()
    if "SAMLResponse" in names or "SAMLRequest" in names:
        return "saml"                       # the auto-submit form a browser posts by script
    if names and all(n.startswith("shib_idp_ls_") or n in ("csrf_token", "_eventId_proceed") for n in names) \
            and any(n.startswith("shib_idp_ls_") for n in names):
        return "storage"                    # IdP's local storage probe: answered as "no local storage"
    if f.has("password"):
        return "login"
    return "ask"


def _pick(page: Page) -> Form | None:
    """The form that is this step of the login: SAML, storage check, login form, else the IdP's step."""
    rank = {"saml": 0, "storage": 1, "login": 2, "ask": 3}
    useful = [f for f in page.forms if f.method == "POST" or _kind(f) == "saml" or any(b[0].startswith("_eventId") for b in f.buttons)]
    useful.sort(key=lambda f: rank[_kind(f)])
    return useful[0] if useful else None


# ---- the login flow ----------------------------------------------------------------------------
def _cookies_out(jar: httpx.Cookies, keep) -> list:
    now = time.time()
    return [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path, "secure": c.secure,
             **({"expires": c.expires} if c.expires else {})}
            for c in jar.jar if keep(c) and not (c.expires and c.expires < now)]


def _cookie_in(jar: httpx.Cookies, c: dict) -> None:
    jar.jar.set_cookie(http.cookiejar.Cookie(
        0, c["name"], c["value"], None, False, c.get("domain", ""), bool(c.get("domain", "").startswith(".")),
        c.get("domain", "").startswith("."), c.get("path") or "/", True, bool(c.get("secure")), c.get("expires"),
        not c.get("expires"), None, None, {}))


def _host_match(host: str, domain: str) -> bool:
    d = domain.lstrip(".")
    return host == d or host.endswith("." + d)


class Flow:
    """One login.  ``start()``/``answer()`` return {"done": session} or {"ask": form description}."""

    def __init__(self, base_url: str, entity_id: str, username: str = "", password: str = "",
                 idp_cookies: list | None = None):
        self.base = base_url.rstrip("/")
        self.host = urlparse(self.base).hostname or ""
        self.entity_id, self.username, self.password = entity_id, username, password
        self.cli = httpx.AsyncClient(headers=HEADERS, follow_redirects=False, timeout=httpx.Timeout(30, connect=15))
        for c in idp_cookies or []:
            _cookie_in(self.cli.cookies, c)
        self.sent_password = False
        self.pending: Form | None = None
        self.started = time.time()

    async def close(self) -> None:
        await self.cli.aclose()

    async def start(self) -> dict:
        url = f"{self.base}/Shibboleth.sso/Login?entityID={quote(self.entity_id, safe='')}&target={quote('/', safe='')}"
        return await self._run("GET", url, None)

    async def answer(self, button: str | None, values: dict | None = None) -> dict:
        form, self.pending = self.pending, None
        if not form:
            raise ShibError("Dieser Anmeldeschritt ist abgelaufen. Bitte noch einmal anmelden.")
        if button and button not in {b[0] for b in form.buttons}:
            raise ShibError("Unbekannte Auswahl.")
        return await self._run(form.method, form.action, form.submit(button, values))

    async def _run(self, method: str, url: str, data: dict | None) -> dict:
        for _ in range(20):
            resp = await self._go(method, url, data)
            if resp is None:
                return {"done": await self._finish()}
            page = Page.parse(resp)
            form = _pick(page)
            where = urlparse(page.url).hostname
            if form is None:
                if resp.status_code >= 400 or not page.forms:
                    raise ShibError(f"{where} hat die Anmeldung abgebrochen: {page.summary() or resp.status_code}")
                raise ShibError(f"Unerwartete Seite bei {where}: {page.summary()}")
            kind = _kind(form)
            if kind in ("saml", "storage"):
                # what the page's script would send; for the storage probe: "no local storage here"
                button = "_eventId_proceed" if any(b[0] == "_eventId_proceed" for b in form.buttons) else None
                values = {f.name: "false" for f in form.fields if f.name == "shib_idp_ls_supported" or f.name.startswith("shib_idp_ls_success.")}
                method, url, data = form.method, form.action, form.submit(button, values)
                continue
            if kind == "login":
                if self.sent_password:
                    raise BadLogin(page.error() or "Benutzername oder Passwort stimmen nicht.")
                if not self.password:
                    raise NeedPassword("Die Hochschule möchte das Passwort.")
                user = next((f for f in form.fields if f.type in ("text", "email") and f.name in ("j_username", "username", "user", "login")), None) \
                    or next((f for f in form.fields if f.type in ("text", "email")), None)
                pw = next(f for f in form.fields if f.type == "password")
                button = next((b[0] for b in form.buttons if b[0] == "_eventId_proceed"), None) \
                    or next((b[0] for b in form.buttons), None)
                values = {pw.name: self.password, **({user.name: self.username} if user else {})}
                self.sent_password = True
                method, url, data = form.method, form.action, form.submit(button, values)
                continue
            self.pending = form
            return {"ask": describe(form, page)}
        raise ShibError("Zu viele Schritte bei der Anmeldung.")

    async def _go(self, method: str, url: str, data: dict | None) -> httpx.Response | None:
        """Follow redirects; None once the SP has handed out its session cookie."""
        for _ in range(15):
            if method == "POST":
                resp = await self.cli.post(url, data=data)
            else:
                resp = await self.cli.get(url, params=data or None)
            if resp.url.host == self.host and any(h.split("=", 1)[0].strip().startswith("_shibsession_")
                                                   for h in resp.headers.get_list("set-cookie")):
                return None
            if resp.is_redirect and "location" in resp.headers:
                method, url, data = "GET", str(resp.url.join(resp.headers["location"])), None
                if urlparse(url).hostname == self.host and urlparse(url).path.rstrip("/") in ("", "/auth"):
                    # back at the service without a session: the SP turned the answer down
                    raise ShibError(f"{self.host} hat die Antwort der Hochschule nicht angenommen.")
                continue
            return resp
        raise ShibError("Zu viele Weiterleitungen bei der Anmeldung.")

    async def _finish(self) -> dict:
        r = await self.cli.post(self.base + "/api/v1/auths/signin", json={"email": "", "password": ""})
        try:
            d = r.json()
        except ValueError:
            d = {}
        if r.status_code != 200 or not d.get("token"):
            why = (d.get("detail") if isinstance(d, dict) else None) or f"HTTP {r.status_code}"
            raise ShibError(f"Die Hochschule hat dich angemeldet, aber der Dienst hat abgelehnt: {why} "
                            "Vermutlich gibt die Hochschule die nötigen Angaben (z. B. die E-Mail) an diesen Dienst noch nicht weiter.")
        on_sp = lambda c: _host_match(self.host, c.domain)
        return {"token": d["token"], "user_agent": UA, "captured_at": int(time.time()),
                "user": {"name": d.get("name"), "email": d.get("email")},
                "cookies": [{k: c[k] for k in ("name", "value", "domain", "path")} for c in _cookies_out(self.cli.cookies, on_sp)],
                "shib": {"entity_id": self.entity_id, "username": self.username,
                         "cookies": _cookies_out(self.cli.cookies, lambda c: not on_sp(c))}}


def describe(form: Form, page: Page) -> dict:
    """A step for the person, in a shape the app can draw: text, tables and fields in page order, buttons."""
    def one(name: str) -> dict | None:
        group = [f for f in form.fields if f.name == name]
        f = group[0]
        if f.type == "radio":
            return {"name": name, "type": "radio",
                    "options": [{"value": x.value, "label": x.label or x.value, "checked": x.checked} for x in group]}
        if f.name.startswith("shib_idp_ls_") or f.name == "csrf_token":
            return None
        return {"name": name, "type": f.type, "label": f.label or name, "checked": f.checked,
                "value": "" if f.type == "password" else f.value}
    items = []
    for kind, value in form.items[:80]:
        if kind == "field":
            if d := one(value):
                items.append({"kind": "field", **d})
        else:
            items.append({"kind": kind, kind if kind == "text" else "rows": value})
    buttons = [{"name": n, "label": label} for n, _, label in form.buttons] or [{"name": "", "label": "Weiter"}]
    return {"host": urlparse(page.url).hostname, "title": page.title, "items": items, "buttons": buttons, "error": page.error()}


async def refresh(base_url: str, shib: dict) -> dict:
    """A new SP session from the IdP's still-valid session, without password.  Raises ShibError."""
    flow = Flow(base_url, shib["entity_id"], shib.get("username", ""), "", shib.get("cookies") or [])
    try:
        result = await flow.start()
    finally:
        await flow.close()
    if "done" not in result:
        raise NeedPassword("Die Hochschule fragt nach einem weiteren Schritt.")
    return result["done"]


# ---- which universities ------------------------------------------------------------------------
# EDU-KI-RLP's portal (where the service sends visitors without a session) lists the universities per
# service; it is read live, and this copy (from 2026-11) is used when that fails.
_EDUKI_OWUI = [
    ("Hochschulen", "Hochschule für Wirtschaft und Gesellschaft Ludwigshafen", "https://idp.hwg-lu.de/idp/shibboleth", "test"),
    ("Hochschulen", "Hochschule Kaiserslautern", "https://shibboleth.hs-kl.de/idp/shibboleth", "ok"),
    ("Hochschulen", "Hochschule Koblenz", "https://idp.hs-koblenz.de/idp/shibboleth", "ok"),
    ("Hochschulen", "Hochschule Mainz", "https://srv-idp-001.hs-mainz.de/idp/shibboleth", "test"),
    ("Hochschulen", "Hochschule Trier", "https://idp.fh-trier.de/idp/shibboleth", "ok"),
    ("Hochschulen", "Hochschule Worms", "https://ssoserver.hs-worms.de/idp/shibboleth", "ok"),
    ("Hochschulen", "Technische Hochschule Bingen", "https://login.th-bingen.de/idp/shibboleth", "test"),
    ("Hochschulen", "Katholische Hochschule Mainz", "https://shibboleth.kh-mz.de/idp/shibboleth", "ok"),
    ("Universitäten", "RPTU Kaiserslautern-Landau", "https://idptest.rhrk.uni-kl.de/idp/shibboleth", "ok"),
    ("Universitäten", "Universität Koblenz", "https://shibboleth2.uni-koblenz.de/idp/shibboleth", "ok"),
    ("Universitäten", "Universität Speyer", "https://aai.dhv-speyer.de/idp/shibboleth", "ok"),
    ("Universitäten", "Universität Trier", "https://shibboleth.uni-trier.de/idp/shibboleth", "test"),
    ("Sonstige", "VCRP", "https://kraftwerk.vcrp.de/idp/shibboleth", "ok"),
]
BUILTIN = {"chat-test.edu-ki-rlp.de": _EDUKI_OWUI}
_cache: dict = {}        # base url -> (time, info)


def _portal_list(page: str, host: str) -> list:
    """The universities for ``host`` from EDU-KI's portal script (DIENSTE + HOCHSCHULEN)."""
    key = next((m.group(1) for m in re.finditer(r'(\w+)\s*:\s*\{[^{}]*?host:\s*"([^"]+)"', page) if m.group(2) == host), None)
    i = page.find("HOCHSCHULEN = [")
    if not key or i < 0:
        return []
    block = page[i:page.find("];", i)]
    groups = dict(re.findall(r'var\s+(\w+)\s*=\s*"([^"]+)"', page))
    out = []
    for chunk in re.split(r"\{\s*gruppe:", block)[1:]:            # one university each
        m = re.match(r'\s*(\w+),\s*name:\s*"([^"]+)"', chunk)
        e = re.search(r"\b" + key + r':\s*\{\s*id:\s*"([^"]+)",\s*status:\s*"(\w+)"', chunk)
        if m and e:
            out.append((groups.get(m.group(1), m.group(1)), html.unescape(m.group(2)), e.group(1), e.group(2)))
    return out


async def info(base_url: str) -> dict:
    """{"available": bool, "idps": [{group, name, entity_id, status}]} for a service, cached for an hour."""
    base = base_url.rstrip("/")
    hit = _cache.get(base)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    host = urlparse(base).hostname or ""
    available, idps = False, []
    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=False) as cli:
        try:
            r = await cli.get(base + "/Shibboleth.sso/Session")
            available = r.status_code == 200 and "session" in r.text.lower()
        except httpx.HTTPError:
            available = False
        if available:
            try:
                r = await cli.get(base + "/")
                if r.is_redirect:
                    portal = await cli.get(str(r.url.join(r.headers["location"])), follow_redirects=True)
                    idps = _portal_list(portal.text, host)
            except httpx.HTTPError:
                pass
    idps = idps or BUILTIN.get(host, [])
    result = {"available": available, "idps": [{"group": g, "name": n, "entity_id": e, "status": s} for g, n, e, s in idps]}
    if available:
        _cache[base] = (time.time(), result)
    return result
