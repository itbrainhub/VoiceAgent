import asyncio

from sqlalchemy import select

from app.agent.session import new_feedback_session, new_order_session
from app.db import session_scope
from app.models import Feedback, LineSource, Order, OrderItem
from tests.conftest import FakeClient, reply, text, tool
from tests.test_orders_and_api import place


def run(coro):
    return asyncio.run(coro)


def test_full_order_call_with_upsell(restaurant, item_id):
    dal, rice = item_id("Dal Makhani"), item_id("Jeera Rice")
    client = FakeClient([
        reply(tool("add_to_cart", item_id=dal, quantity=1)),
        reply(tool("get_upsell_suggestions")),
        reply(text("Got it, one dal makhani. Most people pair it with jeera rice for one fifty. Want some?")),
        reply(tool("add_to_cart", item_id=rice, quantity=1, from_upsell=True),
              tool("set_fulfillment", order_type="delivery", delivery_address="12 MG Road", customer_name="Asha")),
        reply(tool("view_cart")),
        reply(text("That's one dal makhani and one jeera rice, total four hundred and seventeen rupees. Shall I place it?")),
        reply(tool("place_order", caller_confirmed=True)),
        reply(text("Done! Your order number is 1."), tool("end_call")),
        reply(text("Bye!")),
    ])
    sess = new_order_session(restaurant.id, caller_phone="+15550009999", client=client)

    run(sess.respond("One dal makhani please, that's all"))
    run(sess.respond("Sure, add rice. Delivery to 12 MG Road, I'm Asha"))
    out = run(sess.respond("Yes, place it"))
    assert "order number" in out
    assert sess.ended and sess.state.placed_order_id

    with session_scope() as s:
        order = s.get(Order, sess.state.placed_order_id)
        assert order.upsell_offered and order.upsell_accepted
        assert {i.name_snapshot: i.source for i in order.items} == {
            "Dal Makhani": LineSource.CUSTOMER, "Jeera Rice": LineSource.UPSELL}
        assert order.customer.name == "Asha" and order.delivery_address == "12 MG Road"

    req = client.requests[0]
    assert req["model"] == "claude-haiku-5-5"
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Dal Makhani" in req["system"][0]["text"]  # menu is in the prompt


def test_place_order_refused_without_fresh_readback(restaurant, item_id):
    client = FakeClient([
        reply(tool("add_to_cart", item_id=item_id("Butter Naan"), quantity=2),
              tool("set_fulfillment", order_type="pickup"),
              tool("place_order", caller_confirmed=True)),
        reply(text("Let me read that back first.")),
    ])
    sess = new_order_session(restaurant.id, client=client)
    run(sess.respond("Two naan for pickup, place it"))
    assert sess.state.placed_order_id is None
    # The tool error went back to Claude, all three results in one user message.
    results = client.requests[1]["messages"][-1]["content"]
    assert [r.get("is_error", False) for r in results] == [False, False, True]
    assert "read back" in results[2]["content"]
    with session_scope() as s:
        assert s.scalar(select(OrderItem)) is None


def test_cart_error_is_returned_not_raised(restaurant, item_id):
    client = FakeClient([
        reply(tool("add_to_cart", item_id=item_id("Kulfi"), quantity=1)),
        reply(text("Sorry, kulfi is sold out. Would gulab jamun work?")),
    ])
    sess = new_order_session(restaurant.id, client=client)
    out = run(sess.respond("One kulfi"))
    assert "sold out" in out
    assert "sold out" in client.requests[1]["messages"][-1]["content"][0]["content"]


def test_api_failure_hands_off_to_human(restaurant):
    import anthropic
    import httpx

    class Boom:
        def __init__(self):
            self.messages = self

        async def create(self, **_):
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))

    sess = new_order_session(restaurant.id, client=Boom())
    out = run(sess.respond("hello"))
    assert "team member" in out
    assert sess.state.transfer_reason
    assert sess.messages[-1]["role"] == "assistant"  # history stays valid


def test_feedback_call_saves_ratings_and_flags_low_scores(db, restaurant, item_id):
    order_id = place(db, restaurant, item_id).id
    db.commit()
    client = FakeClient([
        reply(tool("save_feedback", overall_rating=2, food_rating=4, delivery_rating=1,
                   comments="Food good, delivery was an hour late"),),
        reply(text("I'm really sorry about the delay. The team will follow up. Thank you!"), tool("end_call")),
        reply(text("Bye")),
    ])
    sess, greeting = new_feedback_session(order_id, client=client)
    assert "Asha" in greeting and "three quick questions" in greeting
    run(sess.respond("Overall 2, food was 4, delivery 1, it came an hour late"))
    assert sess.ended
    with session_scope() as s:
        fb = s.scalar(select(Feedback).where(Feedback.order_id == order_id))
        assert (fb.overall_rating, fb.food_rating, fb.delivery_rating) == (2, 4, 1)
        assert fb.needs_followup is True


def test_conversation_relay_websocket(monkeypatch, restaurant, item_id):
    import json

    from fastapi.testclient import TestClient

    import app.main as main_mod

    client = FakeClient([reply(text("Butter naan is sixty rupees. How many would you like?"))])
    real = main_mod.new_order_session
    monkeypatch.setattr(main_mod, "new_order_session", lambda rid, **kw: real(rid, client=client, **kw))

    with TestClient(main_mod.app) as http, http.websocket_connect("/ws/relay") as ws:
        ws.send_text(json.dumps({"type": "setup", "callSid": "CA9", "from": "+15550003333",
                                 "customParameters": {"purpose": "order", "restaurant_id": str(restaurant.id)}}))
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "How much is naan?", "last": True}))
        msg = json.loads(ws.receive_text())
    assert msg == {"type": "text", "token": "Butter naan is sixty rupees. How many would you like?", "last": True}
