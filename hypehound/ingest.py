import resend
from datetime import datetime

from .config import settings
from .log import log
from .parse import html_to_text, extract_urls, resolve_urls
from .store import is_processed

resend.api_key = settings.resend_api_key

_INBOX = settings.resend_inbox_id


async def fetch_new_emails() -> list[dict]:
    """Fetch unprocessed inbound emails from all threads in the Resend inbox."""
    try:
        threads_resp = await resend.InboxThreads.list_async(_INBOX)
    except resend.exceptions.ResendError as exc:
        log.error(f"Resend API error listing threads: {exc}")
        raise
    except Exception as exc:
        log.error(f"Unexpected error listing threads: {exc}")
        raise

    threads = threads_resp.get("data", [])
    log.debug(f"Resend returned {len(threads)} thread(s)")

    n = len(threads)
    new: list[dict] = []
    for i, thread in enumerate(threads, 1):
        thread_id = thread.get("id", "")
        log.info(f"[dim]Thread {i}/{n}[/dim] fetching emails…")
        try:
            emails_resp = await resend.InboxThreadEmails.list_async(_INBOX, thread_id)
        except Exception as exc:
            log.warning(f"[yellow]Failed to list emails for thread {thread_id}: {exc}[/yellow]")
            continue

        for email in emails_resp.get("data", []):
            if email.get("direction") != "inbound":
                continue

            dedup_key = email.get("message_id") or email.get("id", "")
            if is_processed(dedup_key):
                log.debug(f"Skipping already-processed: {email.get('subject') or dedup_key}")
                continue

            subject = email.get("subject") or "(no subject)"
            try:
                log.info(f"  [dim]Resolving URLs for[/dim] {subject}")
                new.append(await _parse_email(email, thread_id, dedup_key))
            except Exception as exc:
                log.warning(f"[yellow]Failed to parse email '{subject}': {exc}[/yellow]")
                continue

    return new


async def archive_thread(thread_id: str) -> None:
    """Move a processed thread to archive."""
    try:
        await resend.InboxThreads.update_async({"inbox_id": _INBOX, "id": thread_id, "folder": "archive"})
        log.debug(f"Archived thread {thread_id}")
    except Exception as exc:
        log.warning(f"[yellow]Failed to archive thread {thread_id}: {exc}[/yellow]")


async def _parse_email(email: dict, thread_id: str, message_id: str) -> dict:
    html_body = email.get("html") or ""
    text_body = html_to_text(html_body) if html_body else (email.get("text") or "")

    raw_urls = extract_urls(html_body)
    resolved = await resolve_urls(raw_urls)

    received_at = _parse_date(email.get("received_at") or "")

    return {
        "message_id": message_id,
        "sender": email.get("from", ""),
        "subject": email.get("subject") or "",
        "received_at": received_at,
        "text": text_body,
        "resolved_urls": resolved,
        "resend_id": email.get("id", ""),
        "thread_id": thread_id,
    }


def _parse_date(value: str) -> datetime:
    if not value:
        return datetime.now()
    try:
        from datetime import timezone
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(value)
        except Exception:
            return datetime.now()
