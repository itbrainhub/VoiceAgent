from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import orders, worker
from app.cart import Cart
from app.main import app
from app.models import Call, Order, OrderStatus, OrderType, OutboxEvent
from app.db import session_scope


def place(db, restaurant, item_id, phone="+15550001111"):
    cart = Cart(restaurant_id=restaurant.id, currency=restaurant.currency, tax_rate_bp=restaurant.tax_rate_bp)
    cart.add(db, item_id("Dal Makhani"), 1)
    customer = orders.get_or_create_customer(db, phone, "Asha")
    return orders.place_order(db, cart, customer=customer, order_type=OrderType.DELIVERY,
                              delivery_address="12 MG Road")


def test_place_order_snapshots_prices_and_confirms(db, restaurant, item_id):
    order = place(db, restaurant, item_id)
    assert order.status == OrderStatus.CONFIRMED
    assert order.items[0].name_snapshot == "Dal Makhani"
    assert order.total_minor == 24000 + 1200


def test_delivery_requires_address(db, restaurant, item_id):
    cart = Cart(restaurant_id=restaurant.id)
    cart.add(db, item_id("Dal Makhani"), 1)
    with pytest.raises(ValueError, match="address"):
        orders.place_order(db, cart, customer=None, order_type=OrderType.DELIVERY, delivery_address="")


def test_state_machine_blocks_skips_and_queues_one_feedback_call(db, restaurant, item_id):
    order = place(db, restaurant, item_id)
    with pytest.raises(orders.InvalidTransition):
        orders.transition(db, order, OrderStatus.DELIVERED)
    for st in (OrderStatus.ACCEPTED, OrderStatus.PREPARING, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED):
        orders.transition(db, order, st)
    orders.transition(db, order, OrderStatus.DELIVERED)  # webhook retry: idempotent
    events = db.scalars(select(OutboxEvent).where(OutboxEvent.order_id == order.id)).all()
    assert len(events) == 1 and events[0].kind == "feedback_call"
    assert order.delivered_at is not None
    with pytest.raises(orders.InvalidTransition):
        orders.transition(db, order, OrderStatus.CANCELLED)


def test_opted_out_customer_gets_no_feedback_call(db, restaurant, item_id):
    order = place(db, restaurant, item_id)
    order.customer.feedback_opt_out = True
    for st in (OrderStatus.ACCEPTED, OrderStatus.PREPARING, OrderStatus.DELIVERED):
        orders.transition(db, order, st)
    assert db.scalars(select(OutboxEvent)).all() == []


def test_status_api_and_order_list(db, restaurant, item_id):
    order_id = place(db, restaurant, item_id).id
    db.commit()
    with TestClient(app) as client:
        assert client.post(f"/orders/{order_id}/status", json={"status": "delivered"}).status_code == 409
        for st in ("accepted", "preparing", "out_for_delivery", "delivered"):
            r = client.post(f"/orders/{order_id}/status", json={"status": st})
            assert r.status_code == 200, r.text
        listed = client.get(f"/restaurants/{restaurant.id}/orders").json()
        assert listed[0]["status"] == "delivered"
        assert listed[0]["items"][0]["name"] == "Dal Makhani"
        assert client.get(f"/restaurants/{restaurant.id}/menu").status_code == 200


def test_inbound_webhook_returns_conversation_relay_twiml(restaurant):
    with TestClient(app) as client:
        r = client.post("/twilio/voice", data={"To": restaurant.phone_number, "From": "+15550002222", "CallSid": "CA1"})
    assert r.status_code == 200
    assert "<ConversationRelay" in r.text and "/ws/relay" in r.text and 'name="purpose" value="order"' in r.text
    with session_scope() as s:
        assert s.scalar(select(Call)).twilio_call_sid == "CA1"


def _deliver(db, restaurant, item_id):
    order = place(db, restaurant, item_id)
    for st in (OrderStatus.ACCEPTED, OrderStatus.PREPARING, OrderStatus.DELIVERED):
        orders.transition(db, order, st)
    db.commit()
    return order.id


def test_worker_waits_for_delay_and_calling_hours(db, restaurant, item_id):
    order_id = _deliver(db, restaurant, item_id)
    dialled = []
    dial = lambda to, frm, twiml: dialled.append((to, twiml)) or "CA_out"  # noqa: E731

    # Right after delivery the 30-minute delay hasn't passed: nothing happens.
    assert worker.process_due(dial=dial) == 0 and dialled == []
    # 02:00 IST (20:30 UTC) is outside calling hours -> rescheduled, not dialled.
    with session_scope() as s:
        s.scalar(select(OutboxEvent)).run_after = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert worker.process_due(now=datetime(2026, 1, 1, 20, 30, tzinfo=timezone.utc), dial=dial) == 0
    # 12:00 IST (06:30 UTC next day) -> dialled with feedback TwiML.
    assert worker.process_due(now=datetime(2026, 1, 2, 6, 30, tzinfo=timezone.utc), dial=dial) == 1
    to, twiml = dialled[0]
    assert to == "+15550001111"
    assert 'value="feedback"' in twiml and f'value="{order_id}"' in twiml
    with session_scope() as s:
        assert s.scalar(select(OutboxEvent)).done is True
        assert s.scalar(select(Call).where(Call.purpose == "feedback")).twilio_call_sid == "CA_out"
