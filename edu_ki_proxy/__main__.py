import argparse
import json
import subprocess
import sys
import threading
import time

import httpx

from . import auth


def run_app(port: int = 8787) -> None:
    import uvicorn
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
        window.run(url)                      # native GTK/WebKit window; returns when it is closed
    except ImportError:                      # no GTK stack: fall back to a Chromium --app window
        browser = auth.system_browser()
        if not browser:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.executable_path
        subprocess.run([browser, f"--app={url}", f"--user-data-dir={auth.STATE_DIR / 'app-window'}",
                        "--ozone-platform-hint=auto"], stderr=subprocess.DEVNULL)
    srv.should_exit = True


def main() -> None:
    ap = argparse.ArgumentParser(prog="edu_ki_proxy")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="open browser, log in via Shibboleth, store session")
    s = sub.add_parser("serve", help="run the local OpenAI-compatible proxy")
    s.add_argument("--port", type=int, default=8787)
    sub.add_parser("app", help="start the proxy and open the chat app window")
    sub.add_parser("models", help="list models visible to your account")
    t = sub.add_parser("tooltest", help="check whether a model returns native tool_calls")
    t.add_argument("model")
    a = ap.parse_args()

    if a.cmd == "login":
        auth.interactive_login()
        print("Session stored in", auth.STATE_FILE)
    elif a.cmd == "serve":
        import uvicorn
        uvicorn.run("edu_ki_proxy.server:app", host="127.0.0.1", port=a.port)
    elif a.cmd == "app":
        run_app()
    else:
        st = auth.load_state() or sys.exit("run login first")
        h = {"Authorization": f"Bearer {st['token']}", "Cookie": auth.cookie_header(st)}
        if a.cmd == "models":
            r = httpx.get(f"{auth.BASE}/api/models", headers=h, timeout=30)
            print(r.status_code)
            for m in r.json().get("data", []):
                print(m["id"], "-", m.get("name"))
        else:
            tools = [{"type": "function", "function": {"name": "get_weather", "description": "Get weather for a city",
                      "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
            r = httpx.post(f"{auth.BASE}/api/chat/completions", headers=h, timeout=120, json={
                "model": a.model, "tools": tools, "stream": False,
                "messages": [{"role": "user", "content": "What's the weather in Mainz? Use the tool."}]})
            print(r.status_code)
            print(json.dumps(r.json(), indent=2, ensure_ascii=False)[:2500])


main()
