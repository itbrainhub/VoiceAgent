"""Tool schemas and their backend implementations for the ordering and feedback calls."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app import menu, orders, upsell
from app.cart import Cart, CartError
from app.models import Call, Feedback, LineSource, MenuItem, Order, OrderType


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


ORDER_TOOLS: list[dict] = [
    {
        "name": "search_menu",
        "description": "Find menu items by name, nickname, category (e.g. 'drinks'), ingredient or 'veg'. "
        "Tolerates speech recognition errors.",
        "input_schema": _obj({"query": {"type": "string"}}, ["query"]),
    },
    {
        "name": "get_item_details",
        "description": "Full details for one item: price, description, ingredients, allergens, spice level, "
        "availability and its options (with modifier ids).",
        "input_schema": _obj({"item_id": {"type": "integer"}}, ["item_id"]),
    },
    {
        "name": "add_to_cart",
        "description": "Add an item to the caller's order. modifier_ids come from get_item_details. "
        "Set from_upsell true only when the caller accepted your suggestion.",
        "input_schema": _obj(
            {
                "item_id": {"type": "integer"},
                "quantity": {"type": "integer", "minimum": 1},
                "modifier_ids": {"type": "array", "items": {"type": "integer"}},
                "special_instructions": {"type": "string"},
                "from_upsell": {"type": "boolean"},
            },
            ["item_id", "quantity"],
        ),
    },
    {
        "name": "update_cart_item",
        "description": "Change quantity, options or instructions of a line already in the order "
        "(line_id from view_cart or add_to_cart). Quantity 0 removes it.",
        "input_schema": _obj(
            {
                "line_id": {"type": "integer"},
                "quantity": {"type": "integer", "minimum": 0},
                "modifier_ids": {"type": "array", "items": {"type": "integer"}},
                "special_instructions": {"type": "string"},
            },
            ["line_id"],
        ),
    },
    {
        "name": "remove_from_cart",
        "description": "Remove a line from the order.",
        "input_schema": _obj({"line_id": {"type": "integer"}}, ["line_id"]),
    },
    {
        "name": "view_cart",
        "description": "Current order with line ids, options, subtotal, tax and total. Call this right before "
        "reading the order back to the caller.",
        "input_schema": _obj({}),
    },
    {
        "name": "get_upsell_suggestions",
        "description": "Add-on suggestions that fit the current order. Call once, when the caller seems done ordering.",
        "input_schema": _obj(
            {"exclude_allergens": {"type": "array", "items": {"type": "string"}}},
        ),
    },
    {
        "name": "set_fulfillment",
        "description": "Record delivery or pickup, the delivery address, and the caller's name.",
        "input_schema": _obj(
            {
                "order_type": {"type": "string", "enum": ["delivery", "pickup"]},
                "delivery_address": {"type": "string"},
                "customer_name": {"type": "string"},
            },
            ["order_type"],
        ),
    },
    {
        "name": "place_order",
        "description": "Submit the order to the kitchen. Only after reading back the full order from view_cart "
        "and the caller clearly confirming it.",
        "input_schema": _obj({"caller_confirmed": {"type": "boolean"}}, ["caller_confirmed"]),
    },
    {
        "name": "transfer_to_human",
        "description": "Hand the call to restaurant staff.",
        "input_schema": _obj({"reason": {"type": "string"}}, ["reason"]),
    },
    {
        "name": "end_call",
        "description": "Hang up after saying goodbye.",
        "input_schema": _obj({}),
    },
]

FEEDBACK_TOOLS: list[dict] = [
    {
        "name": "save_feedback",
        "description": "Save the customer's ratings and comments. Ratings are 1-5; omit any they did not give.",
        "input_schema": _obj(
            {
                "declined": {"type": "boolean"},
                "overall_rating": {"type": "integer", "minimum": 1, "maximum": 5},
                "food_rating": {"type": "integer", "minimum": 1, "maximum": 5},
                "delivery_rating": {"type": "integer", "minimum": 1, "maximum": 5},
                "comments": {"type": "string"},
            },
        ),
    },
    {"name": "end_call", "description": "Hang up after saying goodbye.", "input_schema": _obj({})},
]


class ToolError(Exception):
    """Returned to Claude as an is_error tool_result; message is meant to be said aloud."""


@dataclass
class OrderCallState:
    restaurant_id: int
    cart: Cart
    caller_phone: str | None = None
    call_id: int | None = None
    customer_name: str | None = None
    order_type: OrderType | None = None
    delivery_address: str | None = None
    upsell_offered: bool = False
    # True once view_cart ran after the most recent cart change; place_order requires it.
    readback_current: bool = False
    placed_order_id: int | None = None
    transfer_reason: str | None = None
    ended: bool = False
    events: list[dict] = field(default_factory=list)


def run_order_tool(session: Session, state: OrderCallState, name: str, args: dict[str, Any]) -> Any:
    cart = state.cart
    cur = cart.currency

    if state.placed_order_id and name in {"add_to_cart", "update_cart_item", "remove_from_cart", "place_order"}:
        raise ToolError(f"Order {state.placed_order_id} is already placed; changes need restaurant staff.")

    try:
        match name:
            case "search_menu":
                found = menu.find_items(session, state.restaurant_id, args["query"])
                if not found:
                    return {"matches": [], "note": "Nothing on the menu matches that."}
                return {
                    "matches": [
                        {"item_id": i.id, "name": i.name, "price": menu.money(i.price_minor, cur),
                         "available": i.is_available, "category": i.category.name}
                        for i in found
                    ]
                }
            case "get_item_details":
                item = session.get(MenuItem, args["item_id"])
                if item is None or item.category.restaurant_id != state.restaurant_id:
                    raise ToolError("No item with that id.")
                return menu.describe_item(item, cur)
            case "add_to_cart":
                line = cart.add(
                    session,
                    args["item_id"],
                    args.get("quantity", 1),
                    args.get("modifier_ids"),
                    args.get("special_instructions", ""),
                    LineSource.UPSELL if args.get("from_upsell") else LineSource.CUSTOMER,
                )
                state.readback_current = False
                return {"added": {"line_id": line.line_id, "item": line.name, "quantity": line.quantity,
                                  "options": [m["name"] for m in line.modifiers]},
                        "order_total_so_far": menu.money(cart.total_minor, cur)}
            case "update_cart_item":
                cart.update(session, args["line_id"], args.get("quantity"), args.get("modifier_ids"),
                            args.get("special_instructions"))
                state.readback_current = False
                return cart.summary()
            case "remove_from_cart":
                removed = cart.remove(args["line_id"])
                state.readback_current = False
                return {"removed": removed.name, **cart.summary()}
            case "view_cart":
                state.readback_current = True
                info = cart.summary()
                info["fulfillment"] = state.order_type.value if state.order_type else "not set"
                info["delivery_address"] = state.delivery_address
                return info
            case "get_upsell_suggestions":
                state.upsell_offered = True
                return {"suggestions": upsell.suggest(session, cart, exclude_allergens=args.get("exclude_allergens"))}
            case "set_fulfillment":
                state.order_type = OrderType(args["order_type"])
                addr = (args.get("delivery_address") or "").strip() or None
                if state.order_type == OrderType.DELIVERY and not addr:
                    raise ToolError("I need the full delivery address.")
                state.delivery_address = addr if state.order_type == OrderType.DELIVERY else None
                if args.get("customer_name"):
                    state.customer_name = args["customer_name"].strip()
                state.readback_current = False
                return {"ok": True}
            case "place_order":
                if not args.get("caller_confirmed"):
                    raise ToolError("Ask the caller to confirm the order first.")
                if cart.is_empty:
                    raise ToolError("The order is empty.")
                if state.order_type is None:
                    raise ToolError("Ask whether this is delivery or pickup first.")
                if not state.readback_current:
                    raise ToolError("The order changed since it was last read back. Call view_cart, read it "
                                    "back and get confirmation again.")
                customer = orders.get_or_create_customer(session, state.caller_phone, state.customer_name)
                order = orders.place_order(
                    session, cart, customer=customer, order_type=state.order_type,
                    delivery_address=state.delivery_address, call_id=state.call_id,
                    upsell_offered=state.upsell_offered,
                )
                state.placed_order_id = order.id
                return {"order_number": order.id, "total": menu.money(order.total_minor, cur),
                        "status": order.status.value}
            case "transfer_to_human":
                state.transfer_reason = args.get("reason", "")
                return {"ok": True, "note": "Tell the caller you're connecting them to a team member."}
            case "end_call":
                state.ended = True
                return {"ok": True}
            case _:
                raise ToolError(f"Unknown tool {name}")
    except CartError as e:
        raise ToolError(str(e)) from e
    except (KeyError, TypeError) as e:
        raise ToolError(f"Missing or invalid argument: {e}") from e


@dataclass
class FeedbackCallState:
    order_id: int
    call_id: int | None = None
    saved: bool = False
    ended: bool = False


def run_feedback_tool(session: Session, state: FeedbackCallState, name: str, args: dict[str, Any]) -> Any:
    match name:
        case "save_feedback":
            order = session.get(Order, state.order_id)
            fb = session.query(Feedback).filter_by(order_id=order.id).one_or_none() or Feedback(order_id=order.id)
            fb.call_id = state.call_id
            if args.get("declined"):
                fb.comments = "(customer declined the survey)"
            else:
                for k in ("overall_rating", "food_rating", "delivery_rating"):
                    v = args.get(k)
                    if v is not None and not (1 <= int(v) <= 5):
                        raise ToolError(f"{k} must be 1 to 5")
                    setattr(fb, k, v)
                fb.comments = (args.get("comments") or "").strip()
                ratings = [r for r in (fb.overall_rating, fb.food_rating, fb.delivery_rating) if r is not None]
                fb.needs_followup = any(r <= 2 for r in ratings)
            session.add(fb)
            session.flush()
            state.saved = True
            return {"ok": True, "needs_followup": fb.needs_followup}
        case "end_call":
            state.ended = True
            return {"ok": True}
        case _:
            raise ToolError(f"Unknown tool {name}")


def tool_result_block(tool_use_id: str, result: Any = None, error: str | None = None) -> dict:
    if error is not None:
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": error, "is_error": True}
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": json.dumps(result, ensure_ascii=False, default=str)}


def record_transcript(session: Session, call_id: int | None, role: str, text: str) -> None:
    if call_id is None or not text:
        return
    call = session.get(Call, call_id)
    if call is not None:
        call.transcript = [*call.transcript, {"role": role, "text": text}]
