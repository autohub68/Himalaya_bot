import asyncio
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from .ai import classify, generate_message
from .config import settings
from .db import connection, init_db, utc_now
from .mcp_client import HimalayasMCP, MCPError, authorization_url, exchange_code, oauth_status
from .supabase_client import SupabaseError, SupabaseLedger

app = FastAPI(title="Himalayas Hiring Assistant", version="0.1.0")
automation_task: asyncio.Task | None = None
reply_monitor_task: asyncio.Task | None = None
automation_state = {"running": False, "page": None, "status": "idle", "queued": 0}
reply_monitor_state = {"running": False, "status": "idle", "detected": 0}
delivery_state = {"running": False, "status": "idle", "current_id": None, "current_name": None}
dashboard_subscribers: set[asyncio.Queue] = set()


class CampaignRequest(BaseModel):
    candidate_ids: list[int] = Field(default_factory=list)
    page: int | None = Field(default=None, ge=1)


class ReplyRequest(BaseModel):
    body: str = Field(min_length=1, max_length=10000)


class GitHubError(RuntimeError):
    pass


async def invite_to_repository(username: str) -> None:
    if not settings.github_token or not settings.github_owner or not settings.github_repo:
        raise GitHubError("GitHub token, owner, and repository are required")
    url = f"{settings.github_api_url.rstrip('/')}/repos/{settings.github_owner}/{settings.github_repo}/collaborators/{username}"
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {settings.github_token}", "X-GitHub-Api-Version": "2022-11-28"}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.put(url, headers=headers, json={"permission": "pull"})
    if response.status_code not in {201, 204, 422}:
        raise GitHubError(f"GitHub invitation failed ({response.status_code}): {response.text}")


class SettingsUpdate(BaseModel):
    openrouter_api_key: str | None = None
    openrouter_model: str | None = None
    openrouter_base_url: str | None = None
    himalayas_mcp_url: str | None = None
    himalayas_mcp_token: str | None = None
    supabase_url: str | None = None
    supabase_key: str | None = None
    supabase_table: str | None = None
    github_token: str | None = None
    github_api_url: str | None = None
    github_owner: str | None = None
    github_repo: str | None = None
    auto_send: bool | None = None
    min_message_delay_seconds: int | None = Field(default=None, ge=5, le=86400)
    max_message_delay_seconds: int | None = Field(default=None, ge=5, le=86400)
    reply_poll_interval_seconds: int | None = Field(default=None, ge=10, le=3600)
    delivery_poll_interval_seconds: int | None = Field(default=None, ge=1, le=300)
    profile_fetch_concurrency: int | None = Field(default=None, ge=1, le=20)
    message_generation_concurrency: int | None = Field(default=None, ge=1, le=20)
    reply_processing_concurrency: int | None = Field(default=None, ge=1, le=20)


SECRET_SETTINGS = {"openrouter_api_key", "himalayas_mcp_token", "supabase_key", "github_token"}


def masked(value: str) -> str:
    return f"{value[:4]}...{value[-4:]}" if len(value) > 8 else ("••••••••" if value else "")


def public_settings() -> dict:
    fields = SettingsUpdate.model_fields
    result = {}
    for name in fields:
        value = getattr(settings, name)
        result[name] = masked(value) if name in SECRET_SETTINGS else value
    return result


def update_env_file(values: dict) -> None:
    configured_path = settings.model_config.get("env_file", ".env")
    if isinstance(configured_path, tuple):
        configured_path = configured_path[-1]
    env_path = Path(configured_path)
    if not env_path.is_absolute():
        env_path = Path.cwd() / env_path
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = existing.splitlines()
    updated = set()
    for index, line in enumerate(lines):
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        field = key.lower()
        if field in values:
            lines[index] = f"{key.upper()}={values[field]}"
            updated.add(field)
    for field, value in values.items():
        if field not in updated:
            lines.append(f"{field.upper()}={value}")
    env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


REPLY_MIN_DELAY_SECONDS = 60


def delay_for(body: str) -> int:
    seconds = REPLY_MIN_DELAY_SECONDS + len(body) * 3
    return min(seconds, settings.max_message_delay_seconds)


def parse_candidate(row) -> dict:
    item = dict(row)
    item["stack"] = json.loads(item.pop("stack_json"))
    return item


def publish_dashboard_update(reason: str) -> None:
    event = {"reason": reason, "at": utc_now()}
    for subscriber in tuple(dashboard_subscribers):
        try:
            subscriber.put_nowait(event)
        except asyncio.QueueFull:
            pass


def compact_scheduled_queue() -> None:
    with connection() as conn:
        rows = conn.execute(
            "SELECT id FROM messages WHERE direction='outbound' AND status='scheduled' ORDER BY send_after, id"
        ).fetchall()
        next_send_at = datetime.now(timezone.utc) + timedelta(seconds=5)
        for row in rows:
            conn.execute(
                "UPDATE messages SET send_after=? WHERE id=?",
                (next_send_at.isoformat(), row["id"]),
            )
            next_send_at += timedelta(seconds=settings.min_message_delay_seconds)


@app.on_event("startup")
async def startup() -> None:
    global reply_monitor_task
    init_db()
    compact_scheduled_queue()
    asyncio.create_task(delivery_loop())
    reply_monitor_task = asyncio.create_task(reply_monitor_loop())


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "auto_send": settings.auto_send, "himalayas_authorized": oauth_status(), "automation": automation_state, "reply_monitor": reply_monitor_state, "delivery": delivery_state}


@app.get("/api/settings")
async def get_settings() -> dict:
    return public_settings()


@app.put("/api/settings")
async def save_settings(update: SettingsUpdate) -> dict:
    values = update.model_dump(exclude_none=True)
    for name, value in list(values.items()):
        if name in SECRET_SETTINGS and (not value or set(value) == {"•"} or value == masked(getattr(settings, name))):
            values.pop(name, None)
            continue
        setattr(settings, name, value)
    if values:
        update_env_file(values)
        publish_dashboard_update("settings_updated")
    return public_settings()


@app.get("/api/auth/start")
async def auth_start() -> RedirectResponse:
    try:
        return RedirectResponse(await authorization_url())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not start Himalayas authorization: {exc}") from exc


@app.get("/api/auth/callback", response_class=HTMLResponse)
async def auth_callback(code: str | None = Query(default=None), state: str | None = Query(default=None), error: str | None = Query(default=None)) -> str:
    if error:
        return f"<h1>Himalayas authorization failed</h1><p>{error}</p>"
    if not code or not state:
        raise HTTPException(status_code=400, detail="OAuth callback requires code and state")
    try:
        await exchange_code(code, state)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not save Himalayas authorization: {exc}") from exc
    return "<h1>Himalayas authorization complete</h1><p>You can close this tab and use the extension.</p>"


@app.get("/api/candidates")
async def candidates(page: int | None = None) -> list[dict]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM candidates ORDER BY name").fetchall()
    items = [parse_candidate(row) for row in rows]
    if page is not None:
        items = [item for item in items if json.loads(item.get("source_json", "{}") or "{}").get("page") == page]
    return items


@app.post("/api/candidates/sync")
async def sync_candidates(page: int = 1) -> dict:
    try:
        client = HimalayasMCP()
        imported = await client.list_candidates(page)
        semaphore = asyncio.Semaphore(settings.profile_fetch_concurrency)

        async def enrich(item: dict) -> dict:
            async with semaphore:
                profile = await client.get_talent_profile(item["talent_slug"])
            item["profile"] = profile
            item["summary"] = f"{item.get('summary', '')}\n{profile}"[:12000]
            return item

        imported = await asyncio.gather(*(enrich(item) for item in imported))
    except (MCPError, Exception) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    with connection() as conn:
        for item in imported:
            stack = item.get("stack", item.get("skills", []))
            summary = item.get("summary", item.get("bio", ""))
            category = classify(stack, summary)
            now = utc_now()
            candidate_slug = item.get("talent_slug", item.get("slug", item.get("id", "")))
            if not candidate_slug:
                continue
            conn.execute(
                """INSERT INTO candidates (external_id, name, profile_url, summary, stack_json, category, source_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(external_id) DO UPDATE SET name=excluded.name, profile_url=excluded.profile_url,
                summary=excluded.summary, stack_json=excluded.stack_json, category=excluded.category,
                source_json=excluded.source_json, updated_at=excluded.updated_at""",
                (str(candidate_slug), item.get("name", "Candidate"), item.get("profile_url", ""), summary,
                 json.dumps(stack), category, json.dumps(item), now, now),
            )
    publish_dashboard_update("candidates_synced")
    return {"imported": len(imported)}


async def page_has_pending_messages(page: int) -> bool:
    with connection() as conn:
        row = conn.execute(
            """SELECT 1 FROM messages m JOIN candidates c ON c.id=m.candidate_id
            WHERE json_extract(c.source_json, '$.page') = ?
            AND m.direction='outbound' AND m.status IN ('scheduled', 'approved') LIMIT 1""",
            (page,),
        ).fetchone()
    return row is not None


async def automatic_campaign() -> None:
    global automation_state
    page = 1
    automation_state = {"running": True, "page": page, "status": "syncing", "queued": 0}
    try:
        while True:
            automation_state["page"] = page
            automation_state["status"] = "syncing"
            sync_result = await sync_candidates(page)
            if not sync_result["imported"]:
                automation_state["status"] = "complete"
                break
            automation_state["status"] = "queueing"
            campaign_result = await create_campaign(CampaignRequest(page=page))
            automation_state["queued"] += campaign_result["queued"]
            automation_state["status"] = "sending"
            while await page_has_pending_messages(page):
                await asyncio.sleep(5)
            page += 1
    except asyncio.CancelledError:
        automation_state["status"] = "stopped"
        raise
    except Exception as exc:
        automation_state["status"] = f"failed: {exc}"
    finally:
        automation_state["running"] = False
        automation_state["page"] = page


@app.post("/api/automation/start")
async def start_automation() -> dict:
    global automation_task
    if automation_task and not automation_task.done():
        return automation_state
    automation_task = asyncio.create_task(automatic_campaign())
    publish_dashboard_update("automation_started")
    return {**automation_state, "status": "starting"}


@app.post("/api/automation/stop")
async def stop_automation() -> dict:
    if automation_task and not automation_task.done():
        automation_task.cancel()
        automation_state["status"] = "stopping"
        publish_dashboard_update("automation_stopping")
    return automation_state


@app.post("/api/campaigns")
async def create_campaign(request: CampaignRequest) -> dict:
    created = 0
    skipped = 0
    next_send_at = datetime.now(timezone.utc)
    with connection() as conn:
        latest = conn.execute(
            "SELECT send_after FROM messages WHERE direction='outbound' AND status IN ('scheduled', 'approved') AND send_after IS NOT NULL ORDER BY send_after DESC LIMIT 1"
        ).fetchone()
    if latest:
        latest_send_at = datetime.fromisoformat(latest["send_after"])
        next_send_at = max(next_send_at, latest_send_at + timedelta(seconds=settings.min_message_delay_seconds))
    if request.page is not None:
        with connection() as conn:
            rows = conn.execute("SELECT * FROM candidates").fetchall()
        rows = [row for row in rows if json.loads(row["source_json"] or "{}").get("page") == request.page]
    elif request.candidate_ids:
        with connection() as conn:
            rows = conn.execute("SELECT * FROM candidates WHERE id IN ({})".format(",".join("?" * len(request.candidate_ids))), request.candidate_ids).fetchall()
    else:
        raise HTTPException(status_code=400, detail="Provide page or candidate_ids")
    eligible_rows = []
    for row in rows:
        candidate = parse_candidate(row)
        with connection() as conn:
            already_contacted = conn.execute("SELECT 1 FROM messages WHERE candidate_id=? AND direction='outbound' AND status != 'failed' LIMIT 1", (candidate["id"],)).fetchone()
        if already_contacted:
            skipped += 1
            continue
        eligible_rows.append(candidate)

    semaphore = asyncio.Semaphore(settings.message_generation_concurrency)

    async def generate(candidate: dict) -> tuple[dict, str]:
        async with semaphore:
            return candidate, await generate_message(candidate, [], True)

    try:
        generated = await asyncio.gather(*(generate(candidate) for candidate in eligible_rows))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Message generation failed: {exc}") from exc

    for candidate, body in generated:
        with connection() as conn:
            conn.execute("INSERT INTO messages (candidate_id, direction, body, status, send_after, created_at) VALUES (?, 'outbound', ?, 'scheduled', ?, ?)",
                         (candidate["id"], body, next_send_at.isoformat(), utc_now()))
        next_send_at += timedelta(seconds=settings.min_message_delay_seconds)
        created += 1
    publish_dashboard_update("messages_scheduled")
    return {"queued": created, "skipped": skipped, "first_send_at": (next_send_at - timedelta(seconds=settings.min_message_delay_seconds)).isoformat() if created else None}


@app.get("/api/messages")
async def messages(status: str | None = None) -> list[dict]:
    query = "SELECT m.*, c.name FROM messages m JOIN candidates c ON c.id=m.candidate_id"
    params: list[str] = []
    if status:
        query += " WHERE m.status = ?"
        params.append(status)
    query += " ORDER BY m.created_at DESC"
    with connection() as conn:
        return [dict(row) for row in conn.execute(query, params)]


@app.get("/api/progress")
async def progress() -> dict:
    with connection() as conn:
        counts = {
            row["status"]: row["count"]
            for row in conn.execute("SELECT status, COUNT(*) AS count FROM messages GROUP BY status")
        }
        next_message = conn.execute(
            """SELECT c.name, m.send_after FROM messages m JOIN candidates c ON c.id=m.candidate_id
            WHERE m.direction='outbound' AND m.status IN ('scheduled', 'approved')
            ORDER BY m.send_after, m.id LIMIT 1"""
        ).fetchone()
        latest = conn.execute(
            """SELECT c.name, m.status, m.sent_at, m.error FROM messages m JOIN candidates c ON c.id=m.candidate_id
            WHERE m.direction='outbound' ORDER BY COALESCE(m.sent_at, m.created_at) DESC, m.id DESC LIMIT 1"""
        ).fetchone()
    return {
        "total": sum(counts.values()),
        "sent": counts.get("sent", 0),
        "queued": counts.get("scheduled", 0) + counts.get("approved", 0) + counts.get("queued", 0),
        "failed": counts.get("failed", 0),
        "skipped": counts.get("skipped", 0),
        "next_recipient": next_message["name"] if next_message else None,
        "next_send_at": next_message["send_after"] if next_message else None,
        "latest_recipient": latest["name"] if latest else None,
        "latest_status": latest["status"] if latest else None,
        "latest_error": latest["error"] if latest else None,
    }


@app.get("/api/activity")
async def activity(limit: int = Query(default=40, ge=1, le=100)) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT m.id, c.name, m.direction, m.status, m.created_at, m.sent_at,
            m.send_after, m.error FROM messages m JOIN candidates c ON c.id=m.candidate_id
            ORDER BY COALESCE(m.sent_at, m.created_at) DESC, m.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/conversations")
async def conversations() -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT c.id, c.name, c.category,
            COUNT(m.id) AS message_count,
            MAX(COALESCE(m.sent_at, m.created_at)) AS last_activity,
            (SELECT direction FROM messages WHERE candidate_id=c.id ORDER BY created_at DESC, id DESC LIMIT 1) AS last_direction,
            (SELECT status FROM messages WHERE candidate_id=c.id ORDER BY created_at DESC, id DESC LIMIT 1) AS last_status,
            (SELECT COUNT(*) FROM messages WHERE candidate_id=c.id AND direction='inbound' AND read_at IS NULL) AS unread_count,
            EXISTS(SELECT 1 FROM messages WHERE candidate_id=c.id AND direction='inbound') AS has_reply,
            EXISTS(SELECT 1 FROM messages WHERE candidate_id=c.id AND direction='outbound' AND status IN ('scheduled', 'approved')) AS response_scheduled
            FROM candidates c JOIN messages m ON m.candidate_id=c.id
            GROUP BY c.id ORDER BY last_activity DESC"""
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["conversation_status"] = "response scheduled" if item["response_scheduled"] else "new reply" if item["has_reply"] and item["last_direction"] == "inbound" else "awaiting reply" if item["last_direction"] == "outbound" and item["last_status"] == "sent" else item["last_status"] or "no activity"
        result.append(item)
    return result


@app.get("/api/conversations/{candidate_id}")
async def conversation(candidate_id: int) -> dict:
    with connection() as conn:
        candidate = conn.execute("SELECT id, name, category, summary, github_username, github_email, github_invited_at FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        messages = conn.execute(
            "SELECT id, direction, body, status, created_at, sent_at, send_after, error FROM messages WHERE candidate_id=? ORDER BY created_at, id",
            (candidate_id,),
        ).fetchall()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return {"candidate": dict(candidate), "messages": [dict(message) for message in messages]}


@app.post("/api/conversations/{candidate_id}/read")
async def mark_conversation_read(candidate_id: int) -> dict:
    with connection() as conn:
        updated = conn.execute(
            "UPDATE messages SET read_at=? WHERE candidate_id=? AND direction='inbound' AND read_at IS NULL",
            (utc_now(), candidate_id),
        ).rowcount
    if updated:
        publish_dashboard_update("conversation_read")
    return {"marked_read": updated}


@app.get("/api/events")
async def events() -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue(maxsize=20)
    dashboard_subscribers.add(queue)

    async def stream():
        try:
            yield "event: connected\ndata: {}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"event: dashboard-update\ndata: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            dashboard_subscribers.discard(queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def deliver(message_id: int) -> str:
    delivery_state["current_id"] = message_id
    delivery_state["status"] = "sending"
    publish_dashboard_update("message_sending")
    with connection() as conn:
        row = conn.execute(
            """SELECT m.*, c.external_id AS candidate_external_id, c.name AS candidate_name,
            c.profile_url AS candidate_profile_url, c.summary AS candidate_summary,
            c.category AS candidate_category, c.stack_json AS candidate_stack_json
            FROM messages m JOIN candidates c ON c.id=m.candidate_id WHERE m.id=?""",
            (message_id,),
        ).fetchone()
    if not row:
        delivery_state["status"] = "missing"
        delivery_state["current_id"] = None
        delivery_state["current_name"] = None
        return "failed"
    delivery_state["current_name"] = row["candidate_name"]
    try:
        first_contact = row["id"] == (await first_outbound_message_id(row["candidate_id"]))
        ledger = SupabaseLedger()
        if first_contact and await ledger.was_contacted(row["candidate_external_id"]):
            with connection() as conn:
                conn.execute("UPDATE messages SET status='skipped' WHERE id=?", (message_id,))
            delivery_state["status"] = "skipped"
            publish_dashboard_update("message_skipped")
            return "skipped"
        await HimalayasMCP().send_message(row["candidate_external_id"], row["body"], first_contact=first_contact)
        candidate = {
            "external_id": row["candidate_external_id"],
            "name": row["candidate_name"],
            "profile_url": row["candidate_profile_url"],
            "summary": row["candidate_summary"],
            "category": row["candidate_category"],
            "stack": json.loads(row["candidate_stack_json"]),
        }
        candidate["sent_at"] = utc_now()
        with connection() as conn:
            conn.execute("UPDATE messages SET status='sent', sent_at=? WHERE id=?", (candidate["sent_at"], message_id))
        if first_contact:
            try:
                await ledger.record_contact(candidate, message_id, row["body"])
            except SupabaseError as exc:
                with connection() as conn:
                    conn.execute("UPDATE messages SET error=? WHERE id=?", (f"Sent, but contact ledger failed: {exc}", message_id))
    except (SupabaseError, MCPError, Exception) as exc:
        with connection() as conn:
            conn.execute("UPDATE messages SET status='failed', error=? WHERE id=?", (str(exc), message_id))
        delivery_state["status"] = "failed"
        delivery_state["current_id"] = None
        delivery_state["current_name"] = None
        return "failed"
    delivery_state["status"] = "sent"
    delivery_state["current_id"] = None
    delivery_state["current_name"] = None
    publish_dashboard_update("message_sent")
    return "sent"


async def first_outbound_message_id(candidate_id: int) -> int | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT id FROM messages WHERE candidate_id=? AND direction='outbound' AND status != 'failed' ORDER BY created_at, id LIMIT 1",
            (candidate_id,),
        ).fetchone()
    return row["id"] if row else None


@app.post("/api/messages/{message_id}/approve")
async def approve(message_id: int) -> dict:
    with connection() as conn:
        updated = conn.execute("UPDATE messages SET status='approved' WHERE id=? AND status='queued'", (message_id,)).rowcount
    if not updated:
        raise HTTPException(status_code=404, detail="Queued message not found")
    return {"approved": True}


@app.post("/api/messages/{message_id}/send")
async def send(message_id: int) -> dict:
    delivery_status = await deliver(message_id)
    if delivery_status != "sent":
        raise HTTPException(status_code=502, detail=f"Message delivery status: {delivery_status}")
    return {"sent": True}


@app.post("/api/candidates/{candidate_id}/replies")
async def reply(candidate_id: int, request: ReplyRequest) -> dict:
    with connection() as conn:
        candidate_row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        history = conn.execute("SELECT direction, body FROM messages WHERE candidate_id=? ORDER BY created_at", (candidate_id,)).fetchall()
    if not candidate_row:
        raise HTTPException(status_code=404, detail="Candidate not found")
    candidate = parse_candidate(candidate_row)
    with connection() as conn:
        conn.execute("INSERT INTO messages (candidate_id, direction, body, status, created_at) VALUES (?, 'inbound', ?, 'received', ?)", (candidate_id, request.body, utc_now()))
    await handle_github_identifier(candidate_id, request.body)
    body = await generate_message(candidate, [dict(item) for item in history] + [{"direction": "inbound", "body": request.body}], False)
    send_after = (datetime.now(timezone.utc) + timedelta(seconds=delay_for(body))).isoformat()
    with connection() as conn:
        conn.execute("INSERT INTO messages (candidate_id, direction, body, status, send_after, created_at) VALUES (?, 'outbound', ?, 'approved', ?, ?)", (candidate_id, body, send_after, utc_now()))
    return {"queued": True, "send_after": send_after}


async def process_inbound_message(item: dict) -> bool:
    if item.get("direction") not in {"inbound", "reply", "received"}:
        return False
    external_id = item.get("external_id") or hashlib.sha256(
        f"{item['talent_slug']}\n{item['body']}".encode()
    ).hexdigest()
    with connection() as conn:
        candidate_row = conn.execute(
            "SELECT * FROM candidates WHERE external_id=? OR lower(name)=lower(?) LIMIT 1",
            (item["talent_slug"], item.get("candidate_name", "")),
        ).fetchone()
        if not candidate_row:
            return False
        duplicate = conn.execute("SELECT 1 FROM messages WHERE external_id=?", (external_id,)).fetchone()
        if duplicate:
            return False
        history = conn.execute(
            "SELECT direction, body FROM messages WHERE candidate_id=? ORDER BY created_at, id",
            (candidate_row["id"],),
        ).fetchall()
        conn.execute(
            "INSERT INTO messages (candidate_id, direction, body, status, external_id, created_at) VALUES (?, 'inbound', ?, 'received', ?, ?)",
            (candidate_row["id"], item["body"], external_id, item.get("created_at") or utc_now()),
        )
    publish_dashboard_update("reply_received")
    candidate = parse_candidate(candidate_row)
    await handle_github_identifier(candidate_row["id"], item["body"])
    conversation = [dict(message) for message in history] + [{"direction": "inbound", "body": item["body"]}]
    body = await generate_message(candidate, conversation, False)
    send_after = (datetime.now(timezone.utc) + timedelta(seconds=delay_for(body))).isoformat()
    with connection() as conn:
        conn.execute(
            "INSERT INTO messages (candidate_id, direction, body, status, send_after, created_at) VALUES (?, 'outbound', ?, 'scheduled', ?, ?)",
            (candidate_row["id"], body, send_after, utc_now()),
        )
    return True


async def handle_github_identifier(candidate_id: int, body: str) -> str | None:
    username_match = re.search(r"(?:github(?:\s+username)?|github\.com/)\s*[:/]?\s*@?([A-Za-z0-9-]{1,39})", body, re.IGNORECASE)
    email_match = re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", body)
    username = username_match.group(1) if username_match else None
    email = email_match.group(0) if email_match else None
    if not username and not email:
        return None
    with connection() as conn:
        if username:
            conn.execute("UPDATE candidates SET github_username=? WHERE id=?", (username, candidate_id))
        if email:
            conn.execute("UPDATE candidates SET github_email=? WHERE id=?", (email, candidate_id))
    if username:
        try:
            await invite_to_repository(username)
        except GitHubError as exc:
            with connection() as conn:
                conn.execute("UPDATE candidates SET github_invited_at=? WHERE id=?", (f"failed: {exc}", candidate_id))
            return username
        with connection() as conn:
            conn.execute("UPDATE candidates SET github_invited_at=? WHERE id=?", (utc_now(), candidate_id))
        publish_dashboard_update("github_invited")
    return username or email


async def reply_monitor_loop() -> None:
    semaphore = asyncio.Semaphore(settings.reply_processing_concurrency)

    async def process_with_limit(item: dict) -> bool:
        async with semaphore:
            return await process_inbound_message(item)

    while True:
        reply_monitor_state["running"] = True
        try:
            inbound_messages = await HimalayasMCP().list_messages()
            results = await asyncio.gather(
                *(process_with_limit(item) for item in inbound_messages),
                return_exceptions=True,
            )
            detected = sum(result is True for result in results)
            failures = sum(isinstance(result, Exception) for result in results)
            reply_monitor_state["detected"] += detected
            reply_monitor_state["status"] = f"monitoring · {detected} new" if not failures else f"monitoring · {failures} failed"
            if detected:
                publish_dashboard_update("reply_received")
        except Exception as exc:
            reply_monitor_state["status"] = f"failed: {exc}"
        await asyncio.sleep(settings.reply_poll_interval_seconds)


async def delivery_loop() -> None:
    delivery_state["running"] = True
    while True:
        query = "SELECT id FROM messages WHERE status='scheduled' AND send_after <= ?"
        params: list[str] = [utc_now()]
        if settings.auto_send:
            query += " OR (status='approved' AND send_after <= ?)"
            params.append(utc_now())
        query += " ORDER BY send_after LIMIT 1"
        with connection() as conn:
            row = conn.execute(query, params).fetchone()
        if row:
            await deliver(row["id"])
        await asyncio.sleep(settings.delivery_poll_interval_seconds)