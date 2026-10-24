from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class Citation(BaseModel):
    url: str | None = None
    title: str | None = None


class NewsletterItem(BaseModel):
    category: str
    headline: str
    summary: str
    citations: list[Citation] = []


class ClassifiedEmail(BaseModel):
    email_id: int
    source: str
    received_at: datetime
    items: list[NewsletterItem]


class DigestEntry(BaseModel):
    category: str
    items: list[NewsletterItem]


class Digest(BaseModel):
    period_start: datetime
    period_end: datetime
    entries: list[DigestEntry]


class NarratorMode(str, Enum):
    single = "single"
    dual = "dual"


class ScriptLine(BaseModel):
    speaker: str | None = None  # None for single; "ALEX" or "SARA" for dual
    text: str


class PodcastSegment(BaseModel):
    lines: list[ScriptLine]


class PodcastScript(BaseModel):
    title: str
    mode: NarratorMode
    segments: list[PodcastSegment]
    citations: list[Citation] = []
