from dataclasses import dataclass

import httpx
import resend

from .config import settings

resend.api_key = settings.resend_api_key


@dataclass
class HealthResult:
    service: str
    ok: bool
    detail: str


async def check_resend() -> HealthResult:
    try:
        resp = await resend.InboxThreads.list_async(settings.resend_inbox_id)
        count = len(resp.get("data", []))
        return HealthResult("Resend", True, f"inbox reachable · {count} thread(s)")
    except Exception as exc:
        return HealthResult("Resend", False, str(exc))


async def check_inference() -> HealthResult:
    url = settings.inference_base_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
        if resp.status_code == 200:
            data = resp.json()
            models = [m.get("id", "") for m in data.get("data", [])]
            active = settings.inference_model
            found = active in models
            model_note = f"[bold]{active}[/bold]" if found else f"[yellow]{active} not listed[/yellow]"
            return HealthResult("LLM", True, f"endpoint up · {model_note}")
        return HealthResult("LLM", False, f"HTTP {resp.status_code}")
    except Exception as exc:
        return HealthResult("LLM", False, str(exc))


async def run_checks() -> list[HealthResult]:
    import asyncio
    return await asyncio.gather(check_resend(), check_inference())
