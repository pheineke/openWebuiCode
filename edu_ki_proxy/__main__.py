import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import time

import httpx

from . import auth, backends, providers, trace


def run_app(port: int = 8787) -> None:
    import uvicorn
    from . import server
    server.PORT = port
    url = f"http://127.0.0.1:{port}"
    srv = uvicorn.Server(uvicorn.Config("edu_ki_proxy.server:app", host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    else:
        sys.exit("server did not start (port in use?)")
    try:
        from . import window
    except ImportError as e:                 # no usable GTK stack: fall back to a Chromium --app window
        print(f"{e}\nÖffne stattdessen ein Chromium-Fenster (Pakete für das App-Fenster: README, Voraussetzungen).",
              file=sys.stderr)
        window = None
    if window:
        window.run(url)                      # native GTK/WebKit window; returns when it is closed
    else:
        browser = auth.system_browser()
        if not browser:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.executable_path
        if not os.path.exists(browser):
            sys.exit("Kein Chromium gefunden: chromium installieren oder `.venv/bin/playwright install chromium`.")
        subprocess.run([browser, f"--app={url}", f"--user-data-dir={auth.STATE_DIR / 'app-window'}",
                        "--ozone-platform-hint=auto"], stderr=subprocess.DEVNULL)
    srv.should_exit = True


def _pick(pid: str | None) -> dict:
    p = providers.get(pid) if pid else (providers.default() or (providers.load() or [None])[0])
    if not p:
        sys.exit("kein Anbieter konfiguriert – zuerst 'login' ausführen")
    return p


async def _local(method: str, path: str, body: dict | None = None) -> httpx.Response:
    """A request to the proxy's own routes, in-process: the CLI shares the server's provider code."""
    from . import server
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url=f"http://127.0.0.1:{server.PORT}", timeout=600) as c:
        return await c.request(method, path, json=body)


def _ask(prompt: str, default: str = "") -> str:
    v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return v or default


def _choose(title: str, options: list, default: int | None = None) -> int:
    print(title)
    for i, o in enumerate(options, 1):
        print(f"  {i}) {o}")
    while True:
        v = _ask("Nummer", str(default + 1) if default is not None else "")
        if v.isdigit() and 1 <= int(v) <= len(options):
            return int(v) - 1


async def _shib_login(p: dict) -> dict | None:
    """University login in the terminal (no browser); None when the service has no Shibboleth."""
    import getpass
    from . import shibboleth
    info = await shibboleth.info(p["base_url"])
    if not info["available"]:
        return None
    last = p.get("shib") or {}
    idps = info["idps"]
    names = [i["name"] + {"test": " (im Test)", "aus": " (nicht angebunden)"}.get(i["status"], "") for i in idps] + ["Andere (entityID eingeben)"]
    at = next((n for n, i in enumerate(idps) if i["entity_id"] == last.get("entity_id")), None)
    k = _choose(f"Anmeldung bei {p.get('name') or p['base_url']} – Hochschule:", names, at)
    entity = idps[k]["entity_id"] if k < len(idps) else _ask("entityID", last.get("entity_id", ""))
    user = _ask("Benutzername", last.get("username", "") if entity == last.get("entity_id") else "")
    flow = shibboleth.Flow(p["base_url"], entity, user, getpass.getpass("Passwort: "))
    try:
        result = await flow.start()
        while "ask" in result:                  # consent, terms or a code: the person decides
            a = result["ask"]
            print(f"\n{a['title'] or 'Weiterer Schritt'} ({a['host']})")
            if a["error"]:
                print("!", a["error"])
            values = {}
            for i in a["items"]:
                if i["kind"] == "text":
                    print(i["text"])
                elif i["kind"] == "table":
                    for r in i["rows"]:
                        print("  " + ": ".join(r))
                elif i["type"] == "radio":
                    d = next((n for n, o in enumerate(i["options"]) if o["checked"]), 0)
                    values[i["name"]] = i["options"][_choose("Auswahl:", [o["label"] for o in i["options"]], d)]["value"]
                elif i["type"] == "checkbox":
                    values[i["name"]] = _ask(f"{i['label']} (j/n)", "j" if i["checked"] else "n").lower().startswith("j")
                else:
                    values[i["name"]] = getpass.getpass(f"{i['label']}: ") if i["type"] == "password" else _ask(i["label"])
            b = a["buttons"][_choose("Weiter mit:", [b["label"] for b in a["buttons"]], len(a["buttons"]) - 1)]
            result = await flow.answer(b["name"] or None, values)
    except shibboleth.ShibError as e:
        sys.exit(f"Anmeldung fehlgeschlagen: {e}")
    finally:
        await flow.close()
    providers.update(p["id"], shib={"entity_id": entity, "username": user})
    return result["done"]


def main() -> None:
    ap = argparse.ArgumentParser(prog="edu_ki_proxy")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login", help="log in and store the session (university login in the terminal, else a browser)")
    lg.add_argument("--provider", help="provider id (default: first one)")
    lg.add_argument("--browser", action="store_true", help="log in on the website in Chromium instead")
    s = sub.add_parser("serve", help="run the local OpenAI-compatible proxy")
    s.add_argument("--port", type=int, default=8787)
    ap_app = sub.add_parser("app", help="start the proxy and open the chat app window")
    ap_app.add_argument("--port", type=int, default=8787)
    for sp in (s, ap_app):
        sp.add_argument("--debug", action="store_true", help=f"write a debug log to {trace.FILE}")
    sub.add_parser("providers", help="list configured providers")
    sub.add_parser("models", help="list models from all logged-in providers")
    t = sub.add_parser("tooltest", help="check whether a model returns tool_calls (native or emulated)")
    t.add_argument("model", help="providerId/model, see 'models'")
    a = ap.parse_args()
    if getattr(a, "debug", False):
        trace.enable()

    if a.cmd == "login":
        p = _pick(a.provider)
        b = backends.of(p)
        if not b.login:
            sys.exit(f"{p.get('name')}: kein Login nötig, nur ein API-Schlüssel (in der App unter Anbieter)")
        state = asyncio.run(_shib_login(p)) if p.get("type") == "openwebui" and not a.browser else None
        state = state or auth.interactive_login(p, b)
        providers.set_session(p["id"], state)
        print("Session stored in", providers.FILE)
    elif a.cmd == "serve":
        import uvicorn
        from . import server
        server.PORT = a.port        # the Host/Origin guard must expect the port we really listen on
        uvicorn.run("edu_ki_proxy.server:app", host="127.0.0.1", port=a.port)
    elif a.cmd == "app":
        run_app(a.port)
    elif a.cmd == "providers":
        for p in providers.all_public():
            state = "abgelaufen" if p["expired"] else "angemeldet" if p["logged_in"] else "abgemeldet"
            print(f"{p['id']:10} {p['type']:10} {p['name']:24} {state:11} {p.get('base_url') or ''}")
    elif a.cmd == "models":
        data = asyncio.run(_local("GET", "/v1/models")).json().get("data") or []
        for m in data:
            print(f"{m['id']}  -  {m['name']}")
        if not data:
            print("keine angemeldeten Anbieter – zuerst 'login' oder die App öffnen")
    else:
        tools = [{"type": "function", "function": {"name": "get_weather", "description": "Get weather for a city",
                  "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
        r = asyncio.run(_local("POST", "/v1/chat/completions", {
            "model": a.model, "tools": tools, "stream": False,
            "messages": [{"role": "user", "content": "What's the weather in Mainz? Use the tool."}]}))
        print(r.status_code)
        try:
            print(json.dumps(r.json(), indent=2, ensure_ascii=False)[:2500])
        except ValueError:
            print(r.text[:2500])


main()
