import asyncio

import httpx

from .config import settings


class SupabaseError(RuntimeError):
    pass


RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2


class SupabaseLedger:
    def __init__(self) -> None:
        if not settings.supabase_url or not settings.supabase_key:
            raise SupabaseError("SUPABASE_URL and SUPABASE_KEY are not configured")
        self.endpoint = f"{settings.supabase_url.rstrip('/')}/rest/v1/{settings.supabase_table}"
        self.headers = {
            "apikey": settings.supabase_key,
            "Authorization": f"Bearer {settings.supabase_key}",
            "Content-Type": "application/json",
        }

    async def was_contacted(self, talent_slug: str) -> bool:
        last_error: SupabaseError | None = None
        for attempt in range(RETRY_ATTEMPTS):
            if attempt:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(
                    self.endpoint,
                    headers=self.headers,
                    params={"talent_slug": f"eq.{talent_slug}", "select": "id", "limit": 1},
                )
            if not response.is_error:
                return bool(response.json())
            last_error = SupabaseError(f"Contact lookup failed ({response.status_code}): {response.text}")
        raise last_error

    async def record_contact(self, candidate: dict, message_id: int, body: str) -> None:
        payload = {
            "talent_slug": candidate["external_id"],
            "candidate_name": candidate["name"],
            "profile_url": candidate.get("profile_url", ""),
            "summary": candidate.get("summary", ""),
            "category": candidate.get("category", "unknown"),
            "stack": candidate.get("stack", []),
            "message_id": message_id,
            "message_body": body,
            "sent_at": candidate.get("sent_at"),
        }
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                self.endpoint,
                headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
                params={"on_conflict": "talent_slug"},
                json=payload,
            )
        if response.is_error:
            raise SupabaseError(f"Contact ledger write failed ({response.status_code}): {response.text}")