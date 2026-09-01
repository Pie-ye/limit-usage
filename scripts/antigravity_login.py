#!/usr/bin/env python3
"""One-time Google OAuth login for the Antigravity usage card.

Runs the same loopback OAuth flow the Antigravity ACP uses and writes the
token to ~/.gemini/antigravity-acp/acp_token.json (the file limit-usage
mounts read-only). Run this on the host machine:

    python3 scripts/antigravity_login.py          # browser loopback flow
    python3 scripts/antigravity_login.py --manual # paste redirect URL back

After it succeeds, the Homepage Antigravity card populates on the next poll
(limit-usage reloads the token file automatically).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
CLIENT_SECRET = "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"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/aicode",
]

DEFAULT_TOKEN_PATH = Path.home() / ".gemini" / "antigravity-acp" / "acp_token.json"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def exchange_code(code: str, redirect_uri: str, verifier: str) -> dict:
    body = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }
    ).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_email(access_token: str) -> str | None:
    try:
        req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            info = json.loads(resp.read().decode())
            return info.get("email")
    except Exception:
        return None


def build_auth_url(redirect_uri: str, state: str, verifier: str) -> str:
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def run_loopback(port: int = 0) -> tuple[str, str, threading.Event]:
    """Start a loopback server; returns (redirect_uri, state, received_event)."""
    received: dict = {}
    event = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.urlparse(self.path)
            if q.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            received["query"] = urllib.parse.parse_qs(q.query)
            event.set()
            payload = ("<html><body><h2>授權完成</h2><p>可以關閉此分頁，回到終端機。</p>" "</body></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # noqa: D401
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port_actual = server.server_address[1]
    state = secrets.token_urlsafe(16)
    return f"http://127.0.0.1:{port_actual}/callback", state, event, received, server


def save_token(token_path: Path, tokens: dict, email: str | None) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    info = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": tokens["refresh_token"],
        "token_uri": TOKEN_URL,
        "scopes": SCOPES,
        "email": email,
    }
    token_path.write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n")
    os.chmod(token_path, 0o600)
    print(f"✅ Token 已儲存至 {token_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manual", action="store_true", help="paste the redirect URL back instead of loopback")
    parser.add_argument("--token-path", type=Path, default=DEFAULT_TOKEN_PATH)
    args = parser.parse_args()

    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(16)

    if args.manual:
        redirect_uri = "http://localhost"
        url = build_auth_url(redirect_uri, state, verifier)
        print("請在瀏覽器開啟以下網址並完成 Google 授權：\n")
        print(url)
        print("\n授權完成後，瀏覽器會導向 localhost（可能顯示無法連線，沒關係）。")
        print("請把導向後的完整網址（開頭是 http://localhost/?code=...）整段貼回來：")
        sys.stdout.flush()
        line = sys.stdin.readline().strip()
        code = None
        if "?" in line:
            q = urllib.parse.parse_qs(line.split("?", 1)[1])
            code = (q.get("code") or [None])[0]
            got_state = (q.get("state") or [None])[0]
            if got_state and got_state != state:
                print("❌ state 不符，請重試")
                return 1
        if not code:
            print("❌ 無法從輸入解析 code")
            return 1
        tokens = exchange_code(code, redirect_uri, verifier)
    else:
        redirect_uri, state, event, received, server = run_loopback()
        url = build_auth_url(redirect_uri, state, verifier)
        print("請在瀏覽器完成 Google 授權。如果瀏覽器沒有自動開啟，請手動開啟：\n")
        print(url)
        print("\n等待授權回呼（最多 180 秒）…")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        if not event.wait(timeout=180):
            print("❌ 等待授權逾時")
            return 1
        server.shutdown()
        q = received.get("query") or {}
        if q.get("error"):
            print(f"❌ 授權失敗: {q['error']}")
            return 1
        code = (q.get("code") or [None])[0]
        got_state = (q.get("state") or [None])[0]
        if got_state and got_state != state:
            print("❌ state 不符，請重試")
            return 1
        if not code:
            print("❌ 沒有收到授權 code")
            return 1
        tokens = exchange_code(code, redirect_uri, verifier)

    if "refresh_token" not in tokens:
        print("❌ Google 沒有回傳 refresh_token（帳號可能已授權過，請改用 --manual 並在網址加 &prompt=consent）")
        return 1
    email = fetch_email(tokens.get("access_token", ""))
    save_token(args.token_path, tokens, email)
    if email:
        print(f"✅ 登入帳號: {email}")
    print("完成後 limit-usage 下一次輪詢就會帶出 Antigravity 用量。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
