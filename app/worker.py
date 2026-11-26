"""Feedback-call worker. Polls the outbox for due feedback_call events, respects
local calling hours, and places the outbound call through Twilio.

  python -m app.worker
"""

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.agent.session import feedback_greeting
from app.config import get_settings
from app.db import init_db, session_scope
from app.models import Call, Order, OutboxEvent, Restaurant, utcnow
from app.telephony import conversation_relay_twiml, place_feedback_call

log = logging.getLogger(__name__)


def within_calling_hours(now_utc: datetime, tz: str) -> bool:
    s = get_settings()
    local = now_utc.astimezone(ZoneInfo(tz))
    return s.calling_hours_start <= local.hour < s.calling_hours_end


def next_calling_window(now_utc: datetime, tz: str) -> datetime:
    s = get_settings()
    local = now_utc.astimezone(ZoneInfo(tz))
    start = local.replace(hour=s.calling_hours_start, minute=0, second=0, microsecond=0)
    if local >= start:
        start += timedelta(days=1)
    return start.astimezone(ZoneInfo("UTC"))


def process_due(now: datetime | None = None, dial=place_feedback_call) -> int:
    """Handle every due event once. Returns how many calls were placed."""
    s = get_settings()
    now = now or utcnow()
    placed = 0
    with session_scope() as db:
        events = db.scalars(
            select(OutboxEvent).where(
                OutboxEvent.kind == "feedback_call", OutboxEvent.done.is_(False), OutboxEvent.run_after <= now
            )
        ).all()
        for ev in events:
            order = db.get(Order, ev.order_id)
            r = db.get(Restaurant, order.restaurant_id)
            customer = order.customer
            if customer is None or customer.feedback_opt_out:
                ev.done, ev.last_error = True, "no customer phone or opted out"
                continue
            if not within_calling_hours(now, r.timezone):
                ev.run_after = next_calling_window(now, r.timezone)
                continue
            if ev.attempts >= s.feedback_max_attempts:
                ev.done, ev.last_error = True, "max attempts reached"
                continue
            call = Call(direction="outbound", purpose="feedback", from_number=r.phone_number, to_number=customer.phone_e164)
            db.add(call)
            db.flush()
            twiml = conversation_relay_twiml(
                "/ws/relay",
                feedback_greeting(r.name, customer.name),
                {"purpose": "feedback", "order_id": str(order.id), "call_id": str(call.id)},
            )
            ev.attempts += 1
            try:
                call.twilio_call_sid = dial(customer.phone_e164, r.phone_number, twiml)
                placed += 1
                # Mark done now; a no-answer status callback can re-open it (phase 3).
                ev.done = True
            except Exception as e:  # network / Twilio error: retry later
                log.exception("feedback call for order %s failed", order.id)
                ev.last_error = str(e)[:500]
                ev.run_after = now + timedelta(minutes=15)
    return placed


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    init_db()
    while True:
        n = process_due()
        if n:
            log.info("placed %d feedback calls", n)
        time.sleep(30)


if __name__ == "__main__":
    main()
