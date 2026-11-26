"""HTTP + WebSocket service.

  uvicorn app.main:app --reload
"""

import json
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import orders as order_svc
from app.agent.session import new_feedback_session, new_order_session, order_greeting
from app.db import init_db, session_scope
from app.menu import all_items, describe_item, money
from app.models import Call, Order, OrderStatus, Restaurant, utcnow
from app.telephony import conversation_relay_twiml

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Restaurant Voice Agent", lifespan=lifespan)


def get_db():
    with session_scope() as s:
        yield s


@app.get("/health")
def health():
    return {"ok": True}


# ---------- menu and orders (for the staff page / POS integration) ----------

@app.get("/restaurants/{rid}/menu")
def get_menu(rid: int, db: Session = Depends(get_db)):
    r = db.get(Restaurant, rid) or _404("restaurant")
    return [describe_item(i, r.currency) for i in all_items(db, rid)]


@app.get("/restaurants/{rid}/orders")
def list_orders(rid: int, status: OrderStatus | None = None, db: Session = Depends(get_db)):
    r = db.get(Restaurant, rid) or _404("restaurant")
    q = select(Order).where(Order.restaurant_id == rid).order_by(Order.id.desc()).limit(100)
    if status:
        q = q.where(Order.status == status)
    return [_order_json(o, r.currency) for o in db.scalars(q)]


class StatusUpdate(BaseModel):
    status: OrderStatus


@app.post("/orders/{order_id}/status")
def update_status(order_id: int, body: StatusUpdate, db: Session = Depends(get_db)):
    """Called by the staff page, a POS, or a delivery-platform webhook. Setting
    `delivered` queues the feedback call (same transaction)."""
    order = db.get(Order, order_id) or _404("order")
    try:
        order_svc.transition(db, order, body.status)
    except order_svc.InvalidTransition as e:
        raise HTTPException(409, str(e)) from e
    r = db.get(Restaurant, order.restaurant_id)
    return _order_json(order, r.currency)


# ---------- Twilio ----------

@app.post("/twilio/voice")
async def twilio_inbound(request: Request):
    """Twilio number webhook: answer and hand the call to ConversationRelay.
    TODO(phase 4): validate the X-Twilio-Signature header."""
    form = await request.form()
    to, frm, sid = form.get("To"), form.get("From"), form.get("CallSid")
    with session_scope() as db:
        r = db.scalar(select(Restaurant).where(Restaurant.phone_number == to)) or db.scalar(select(Restaurant).limit(1))
        if r is None:
            raise HTTPException(503, "No restaurant configured")
        call = Call(twilio_call_sid=sid, direction="inbound", purpose="order", from_number=frm, to_number=to)
        db.add(call)
        db.flush()
        twiml = conversation_relay_twiml(
            "/ws/relay", order_greeting(r.name),
            {"purpose": "order", "restaurant_id": str(r.id), "call_id": str(call.id)},
        )
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/ws/relay")
async def relay(ws: WebSocket):
    """One ConversationRelay WebSocket per call. Twilio sends recognised speech as
    text; we reply with text that Twilio speaks."""
    await ws.accept()
    sess = None
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            kind = msg.get("type")
            if kind == "setup":
                p = msg.get("customParameters") or {}
                call_id = int(p["call_id"]) if p.get("call_id") else None
                if p.get("purpose") == "feedback":
                    sess, _ = new_feedback_session(int(p["order_id"]), call_id=call_id)
                else:
                    sess = new_order_session(int(p["restaurant_id"]), caller_phone=msg.get("from"), call_id=call_id)
            elif kind == "prompt" and sess is not None:
                if not msg.get("last", True):
                    continue
                reply = await sess.respond(msg.get("voicePrompt", ""))
                if reply:
                    await ws.send_text(json.dumps({"type": "text", "token": reply, "last": True}))
                if sess.ended or getattr(sess.state, "transfer_reason", None):
                    # TODO(phase 1b): real transfer via <Dial> to the restaurant's staff line.
                    await ws.send_text(json.dumps({"type": "end"}))
            elif kind == "interrupt":
                pass  # caller barged in; the next prompt carries what they said
            elif kind == "error":
                log.error("ConversationRelay error: %s", msg.get("description"))
    except WebSocketDisconnect:
        pass
    finally:
        call_id = getattr(getattr(sess, "state", None), "call_id", None)
        if call_id:
            with session_scope() as db:
                call = db.get(Call, call_id)
                if call and call.ended_at is None:
                    call.ended_at = utcnow()
                    st = sess.state
                    call.outcome = (
                        "order_placed" if getattr(st, "placed_order_id", None)
                        else "feedback_saved" if getattr(st, "saved", False)
                        else "transferred" if getattr(st, "transfer_reason", None)
                        else "ended"
                    )


def _order_json(o: Order, currency: str) -> dict:
    return {
        "id": o.id,
        "status": o.status.value,
        "type": o.order_type.value,
        "address": o.delivery_address,
        "customer_phone": o.customer.phone_e164 if o.customer else None,
        "items": [
            {"name": i.name_snapshot, "quantity": i.quantity, "options": [m["name"] for m in i.modifiers],
             "instructions": i.special_instructions, "source": i.source.value}
            for i in o.items
        ],
        "total": money(o.total_minor, currency),
        "created_at": o.created_at.isoformat(),
    }


def _404(what: str):
    raise HTTPException(404, f"{what} not found")
