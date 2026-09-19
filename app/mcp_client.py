import base64
import hashlib
import json
import re
import secrets
import time
from urllib.parse import urlencode

import httpx

from .config import settings
from .db import LEGACY_ACCOUNT, connection, utc_now


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def authorization_url(account_id: str) -> str:
    client_id = settings.himalayas_oauth_client_id
    client_secret = settings.himalayas_oauth_client_secret or None
    if not client_id:
        payload = {
            "client_name": "Himalayas Hiring Assistant",
            "redirect_uris": [settings.himalayas_oauth_redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(settings.himalayas_oauth_registration_endpoint, json=payload)
        response.raise_for_status()
        registration = response.json()
        client_id = registration["client_id"]
        client_secret = registration.get("client_secret")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    with connection() as conn:
        conn.execute("DELETE FROM oauth_state WHERE account_id=?", (account_id,))
        conn.execute("INSERT INTO oauth_state (state, account_id, code_verifier, created_at) VALUES (?, ?, ?, ?)", (state, account_id, verifier, utc_now()))
        conn.execute(
            "INSERT INTO oauth_tokens (account_id, access_token, refresh_token, expires_at, client_id, client_secret) VALUES (?, '', NULL, NULL, ?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET client_id=excluded.client_id, client_secret=excluded.client_secret",
            (account_id, client_id, client_secret),
        )
    query = urlencode({
        "response_type": "code", "client_id": client_id,
        "redirect_uri": settings.himalayas_oauth_redirect_uri,
        "scope": "read_messages write_messages", "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256", "state": state,
    })
    return f"{settings.himalayas_oauth_authorization_endpoint}?{query}"


async def exchange_code(code: str, state: str) -> None:
    with connection() as conn:
        oauth_state = conn.execute("SELECT account_id, code_verifier FROM oauth_state WHERE state=?", (state,)).fetchone()
        client = oauth_state and conn.execute("SELECT client_id, client_secret FROM oauth_tokens WHERE account_id=?", (oauth_state["account_id"],)).fetchone()
    if not oauth_state or not client:
        raise RuntimeError("OAuth state is missing or expired")
    payload = {"grant_type": "authorization_code", "code": code, "redirect_uri": settings.himalayas_oauth_redirect_uri, "client_id": client["client_id"], "code_verifier": oauth_state["code_verifier"]}
    if client["client_secret"]:
        payload["client_secret"] = client["client_secret"]
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(settings.himalayas_oauth_token_endpoint, data=payload)
    response.raise_for_status()
    account_id = oauth_state["account_id"]
    save_token(account_id, response.json())
    with connection() as conn:
        conn.execute("DELETE FROM oauth_state WHERE account_id=?", (account_id,))


def save_token(account_id: str, token: dict) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE oauth_tokens SET access_token=?, refresh_token=COALESCE(?, refresh_token), expires_at=? WHERE account_id=?",
            (token["access_token"], token.get("refresh_token"), time.time() + float(token.get("expires_in", 3600)), account_id),
        )


def static_token_applies(account_id: str) -> bool:
    # A fixed token belongs to one Himalayas account, so it must never be shared across accounts.
    return bool(settings.himalayas_mcp_token) and account_id == LEGACY_ACCOUNT


async def access_token(account_id: str) -> str:
    if static_token_applies(account_id):
        return settings.himalayas_mcp_token
    with connection() as conn:
        row = conn.execute("SELECT * FROM oauth_tokens WHERE account_id=?", (account_id,)).fetchone()
    if not row or not row["access_token"]:
        raise RuntimeError("Himalayas authorization required. Open /api/auth/start first.")
    if not row["expires_at"] or row["expires_at"] > time.time() + 60:
        return row["access_token"]
    if not row["refresh_token"]:
        raise RuntimeError("Himalayas access token expired. Open /api/auth/start again.")
    payload = {"grant_type": "refresh_token", "refresh_token": row["refresh_token"], "client_id": row["client_id"]}
    if row["client_secret"]:
        payload["client_secret"] = row["client_secret"]
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(settings.himalayas_oauth_token_endpoint, data=payload)
    response.raise_for_status()
    token = response.json()
    save_token(account_id, token)
    return token["access_token"]


def oauth_status(account_id: str) -> bool:
    if static_token_applies(account_id):
        return True
    with connection() as conn:
        row = conn.execute("SELECT access_token FROM oauth_tokens WHERE account_id=?", (account_id,)).fetchone()
    return bool(row and row["access_token"])


def authorized_accounts() -> list[str]:
    with connection() as conn:
        rows = conn.execute("SELECT account_id FROM oauth_tokens WHERE access_token != ''").fetchall()
    return [row["account_id"] for row in rows]


class MCPError(RuntimeError):
    pass


class HimalayasMCP:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        if not settings.himalayas_mcp_url:
            raise MCPError("HIMALAYAS_MCP_URL is not configured")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.url = settings.himalayas_mcp_url
        self.headers = headers

    async def call(self, tool_name: str, arguments: dict) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        try:
            token = await access_token(self.account_id)
        except Exception as exc:
            raise MCPError(str(exc)) from exc
        headers = {**self.headers, "Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(self.url, headers=headers, json=payload)
        response.raise_for_status()
        data_line = next(
            (line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")),
            response.text,
        )
        data = json.loads(data_line)
        if data.get("error"):
            raise MCPError(str(data["error"]))
        return data.get("result", {})

    async def list_candidates(self, page: int = 1) -> list[dict]:
        result = await self.call("search_talent", {"page": page, "sort": "recent"})
        text = "\n".join(
            item.get("text", "") for item in result.get("content", []) if item.get("type") == "text"
        )
        candidates = []
        for block in text.split("\n---\n"):
            slug_match = re.search(r"himalayas\.app/@([^?\s]+)", block)
            name_match = re.search(r"\*\*([^*]+)\*\*", block)
            if not slug_match or not name_match:
                continue
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            summary = next((line for line in lines if "💡" in line), "").replace("💡", "").strip()
            role = next((line for line in lines if line.startswith("💼")), "").replace("💼", "").strip()
            candidates.append({
                "talent_slug": slug_match.group(1),
                "name": name_match.group(1).strip(),
                "summary": f"{role}. {summary}".strip(". "),
                "profile_url": f"https://himalayas.app/@{slug_match.group(1)}",
                "stack": [],
                "page": page,
            })
        return candidates

    async def get_talent_profile(self, talent_slug: str) -> str:
        result = await self.call("get_talent_profile", {"talent_slug": talent_slug})
        return "\n".join(
            item.get("text", "") for item in result.get("content", []) if item.get("type") == "text"
        )

    async def send_message(self, candidate_slug: str, body: str, first_contact: bool = False) -> dict:
        tool = "start_conversation" if first_contact else settings.mcp_send_message_tool
        arguments = {"talent_slug": candidate_slug, "message": body}
        return await self.call(tool, arguments)

    async def list_messages(self) -> list[dict]:
        result = await self.call("list_conversations", {})
        text = "\n".join(item.get("text", "") for item in result.get("content", []) if item.get("type") == "text")
        messages = []
        for block in text.split("\n---\n"):
            if "New reply" not in block:
                continue
            room_match = re.search(r"Room: `([^`]+)`", block)
            if not room_match:
                continue
            history = await self.call("get_conversation", {"room_name": room_match.group(1)})
            history_text = "\n".join(item.get("text", "") for item in history.get("content", []) if item.get("type") == "text")
            slug_match = re.search(r"himalayas\.app/@([^\s]+)", history_text)
            if not slug_match:
                continue
            headers = list(re.finditer(r"(?m)^(🏢|👤)\s+\*\*(.+?)\*\*.*$", history_text))
            for index, header in enumerate(headers):
                if header.group(1) != "👤":
                    continue
                body_start = header.end()
                body_end = headers[index + 1].start() if index + 1 < len(headers) else len(history_text)
                body = history_text[body_start:body_end].strip()
                body = re.sub(r"\n🔑 Room:.*", "", body, flags=re.DOTALL).strip()
                if body:
                    messages.append({
                        "external_id": hashlib.sha256(f"{room_match.group(1)}\n{body}".encode()).hexdigest(),
                        "talent_slug": slug_match.group(1),
                        "candidate_name": header.group(2).strip(),
                        "body": body,
                        "direction": "inbound",
                        "created_at": utc_now(),
                    })
        return messages