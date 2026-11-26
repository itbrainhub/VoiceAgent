"""One phone conversation with Claude: holds the message history and runs the tool
loop for each caller turn. Transport-agnostic, so the Twilio WebSocket handler and
the text simulator drive exactly the same code.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import anthropic
from sqlalchemy.orm import Session, sessionmaker

from app.agent import prompts
from app.agent.tools import (
    FEEDBACK_TOOLS,
    ORDER_TOOLS,
    FeedbackCallState,
    OrderCallState,
    ToolError,
    record_transcript,
    run_feedback_tool,
    run_order_tool,
    tool_result_block,
)
from app.cart import Cart
from app.config import get_settings
from app.db import SessionLocal, session_scope
from app.menu import menu_overview, money
from app.models import Order, Restaurant

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8  # per caller turn; stops a runaway loop
FALLBACK_LINE = "Sorry, I'm having trouble on my end. Let me connect you to a team member."

ToolRunner = Callable[[Session, Any, str, dict], Any]


class ConversationSession:
    def __init__(
        self,
        *,
        system: str,
        tools: list[dict],
        state: Any,
        run_tool: ToolRunner,
        client: Any | None = None,
        db: sessionmaker = SessionLocal,
        model: str | None = None,
        effort: str | None = None,
    ) -> None:
        settings = get_settings()
        self.client = client or anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self.model = model or settings.live_model
        self.effort = effort or settings.live_effort
        # The system prompt (with the menu) is identical for every turn of every call
        # to this restaurant, so it is cached.
        self.system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        self.tools = tools
        self.state = state
        self.run_tool = run_tool
        self.db = db
        self.messages: list[dict] = []

    @property
    def ended(self) -> bool:
        return bool(getattr(self.state, "ended", False))

    def seed_assistant_greeting(self, greeting: str) -> None:
        """The greeting Twilio speaks on pickup is part of the conversation history."""
        self.messages = [
            {"role": "user", "content": "(call connected)"},
            {"role": "assistant", "content": greeting},
        ]

    async def respond(self, caller_text: str) -> str:
        """Handle one caller utterance; return what the agent says back."""
        self.messages.append({"role": "user", "content": caller_text})
        self._log("caller", caller_text)
        spoken: list[str] = []

        for _ in range(MAX_TOOL_ROUNDS):
            try:
                response = await self.client.messages.create(
                    model=self.model,
                    max_tokens=2048,
                    system=self.system,
                    tools=self.tools,
                    messages=self.messages,
                    output_config={"effort": self.effort},
                )
            except anthropic.RateLimitError:
                log.warning("Claude rate limited")
                return self._fail()
            except anthropic.APIStatusError as e:
                log.error("Claude API error %s: %s", e.status_code, e.message)
                return self._fail()
            except anthropic.APIConnectionError:
                log.error("Claude connection error")
                return self._fail()

            if response.stop_reason == "refusal":
                return self._fail()

            # Keep the full content (thinking + tool_use blocks) in history, unchanged.
            self.messages.append({"role": "assistant", "content": response.content})
            text = " ".join(b.text.strip() for b in response.content if b.type == "text" and b.text.strip())
            if text:
                spoken.append(text)

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                break

            results = []
            for block in tool_uses:
                results.append(await asyncio.to_thread(self._exec_tool, block.id, block.name, dict(block.input)))
            # All results go back in one user message.
            self.messages.append({"role": "user", "content": results})
        else:
            log.error("Tool loop hit MAX_TOOL_ROUNDS")
            spoken.append("Sorry, give me just a moment.")

        reply = " ".join(spoken).strip()
        self._log("agent", reply)
        return reply

    def _exec_tool(self, tool_use_id: str, name: str, args: dict) -> dict:
        try:
            with session_scope(self.db) as s:
                result = self.run_tool(s, self.state, name, args)
            log.info("tool %s(%s) -> ok", name, args)
            return tool_result_block(tool_use_id, result)
        except ToolError as e:
            log.info("tool %s(%s) -> error: %s", name, args, e)
            return tool_result_block(tool_use_id, error=str(e))
        except Exception:
            log.exception("tool %s crashed", name)
            return tool_result_block(tool_use_id, error="Internal error; apologise and offer to transfer.")

    def _fail(self) -> str:
        # Drop the unanswered caller turn so history stays valid, then hand off.
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()
        if hasattr(self.state, "transfer_reason"):
            self.state.transfer_reason = "assistant unavailable"
        self._log("agent", FALLBACK_LINE)
        return FALLBACK_LINE

    def _log(self, role: str, text: str) -> None:
        call_id = getattr(self.state, "call_id", None)
        if call_id is None or not text:
            return
        with session_scope(self.db) as s:
            record_transcript(s, call_id, role, text)


def order_greeting(restaurant_name: str) -> str:
    return f"Hi, thanks for calling {restaurant_name}! What can I get for you today?"


def new_order_session(
    restaurant_id: int,
    *,
    caller_phone: str | None = None,
    call_id: int | None = None,
    client: Any | None = None,
    db: sessionmaker = SessionLocal,
) -> ConversationSession:
    with session_scope(db) as s:
        r = s.get(Restaurant, restaurant_id)
        system = prompts.render_order_system(
            r.name,
            {"hours": r.hours, "currency": r.currency, "payment": "cash or card on delivery or at pickup"},
            menu_overview(s, r.id, r.currency),
        )
        cart = Cart(restaurant_id=r.id, currency=r.currency, tax_rate_bp=r.tax_rate_bp)
        name = r.name
    state = OrderCallState(restaurant_id=restaurant_id, cart=cart, caller_phone=caller_phone, call_id=call_id)
    sess = ConversationSession(system=system, tools=ORDER_TOOLS, state=state, run_tool=run_order_tool, client=client, db=db)
    sess.seed_assistant_greeting(order_greeting(name))
    return sess


def feedback_greeting(restaurant_name: str, customer_name: str | None) -> str:
    who = f" {customer_name}" if customer_name else ""
    return (
        f"Hi{who}, this is the virtual assistant from {restaurant_name}, calling about your order today. "
        "Do you have a minute for three quick questions?"
    )


def new_feedback_session(
    order_id: int,
    *,
    call_id: int | None = None,
    client: Any | None = None,
    db: sessionmaker = SessionLocal,
) -> tuple[ConversationSession, str]:
    with session_scope(db) as s:
        order = s.get(Order, order_id)
        r = s.get(Restaurant, order.restaurant_id)
        items = ", ".join(f"{i.quantity} {i.name_snapshot}" for i in order.items)
        summary = f"order {order.id}: {items}, total {money(order.total_minor, r.currency)}, {order.order_type.value}"
        system = prompts.FEEDBACK_SYSTEM.format(restaurant=r.name, order_summary=summary)
        greeting = feedback_greeting(r.name, order.customer.name if order.customer else None)
    state = FeedbackCallState(order_id=order_id, call_id=call_id)
    sess = ConversationSession(system=system, tools=FEEDBACK_TOOLS, state=state, run_tool=run_feedback_tool, client=client, db=db)
    sess.seed_assistant_greeting(greeting)
    return sess, greeting
