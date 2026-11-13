from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Status(str, Enum):
    QUEUED = "QUEUED"                    # waiting for its first call
    CALLING = "CALLING"                  # a call is in progress
    RETRY = "RETRY"                      # no answer, retry scheduled
    TRANSFERRED = "TRANSFERRED"          # parent connected to a counselor
    ANSWERED_NOT_TRANSFERRED = "ANSWERED_NOT_TRANSFERRED"  # parent answered, nobody reachable
    NO_RESPONSE = "NO_RESPONSE"          # retries used up, parent never answered
    DUPLICATE = "DUPLICATE"              # same phone seen recently
    INVALID_PHONE = "INVALID_PHONE"      # number could not be cleaned
    NEEDS_REVIEW = "NEEDS_REVIEW"        # campus unknown, or other issue a person must fix
    STALE = "STALE"                      # lead too old to call automatically
    DO_NOT_CALL = "DO_NOT_CALL"          # on the do-not-call list
    RESOLVED = "RESOLVED"                # a person followed up manually


class EventKind(str, Enum):
    ANSWERED = "ANSWERED"                # parent picked up (call continues)
    TRANSFERRED = "TRANSFERRED"          # counselor connected
    TRANSFER_FAILED = "TRANSFER_FAILED"  # parent answered, no counselor reached
    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"
    FAILED = "FAILED"                    # provider/network failure


@dataclass
class RawLead:
    """A lead as read from a source, before cleaning."""
    key: str                  # stable identity within its source
    source: str               # e.g. "meta_sheet"
    row_ref: str              # where it came from (row number, record id)
    created_at: datetime      # timezone-aware
    name: str = ""            # parent / enquirer name
    student_name: str = ""
    phone: str = ""
    campus: str = ""
    grade_text: str = ""
    city: str = ""
    email: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class CallPlan:
    """Everything a provider adapter needs to run one call. Providers differ in HOW they
    do it; this says WHAT should happen."""
    call_ref: str                    # our id for this attempt
    lead_id: int
    campus: str
    parent_number: str               # E.164
    caller_id: str                   # E.164, shown to the parent
    greeting_text: str               # played to the parent on answer
    counselor_name: str
    counselor_numbers: list[str]     # tried in order
    counselor_ring_seconds: int
    whisper_text: str                # played to the counselor before bridging
    fallback_text: str               # played to the parent if nobody is reachable
    max_call_seconds: int
    webhook_url: Optional[str] = None


@dataclass
class CallEvent:
    call_ref: str                    # matches CallPlan.call_ref
    kind: EventKind
    provider_call_id: str = ""
    transferred_to: str = ""
    detail: str = ""
