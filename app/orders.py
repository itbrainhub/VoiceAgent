"""Order state machine and persistence.

Only this module changes Order.status, and only along ALLOWED transitions. Moving
an order to DELIVERED writes a feedback-call outbox event in the same transaction.
"""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cart import Cart
from app.config import get_settings
from app.models import (
    Customer,
    LineSource,
    Order,
    OrderItem,
    OrderStatus,
    OrderType,
    OutboxEvent,
    utcnow,
)

S = OrderStatus
ALLOWED: dict[OrderStatus, set[OrderStatus]] = {
    S.DRAFT: {S.CONFIRMED, S.CANCELLED},
    S.CONFIRMED: {S.ACCEPTED, S.CANCELLED},
    S.ACCEPTED: {S.PREPARING, S.CANCELLED},
    S.PREPARING: {S.OUT_FOR_DELIVERY, S.DELIVERED},  # DELIVERED directly = pickup collected
    S.OUT_FOR_DELIVERY: {S.DELIVERED},
    S.DELIVERED: set(),
    S.CANCELLED: set(),
}


class InvalidTransition(ValueError):
    pass


def get_or_create_customer(session: Session, phone_e164: str | None, name: str | None = None) -> Customer | None:
    if not phone_e164:
        return None
    customer = session.scalar(select(Customer).where(Customer.phone_e164 == phone_e164))
    if customer is None:
        customer = Customer(phone_e164=phone_e164, name=name)
        session.add(customer)
        session.flush()
    elif name and not customer.name:
        customer.name = name
    return customer


def place_order(
    session: Session,
    cart: Cart,
    *,
    customer: Customer | None,
    order_type: OrderType,
    delivery_address: str | None,
    call_id: int | None = None,
    upsell_offered: bool = False,
) -> Order:
    """Persist a confirmed cart. Called only after the caller says yes to the read-back."""
    if cart.is_empty:
        raise ValueError("Cannot place an empty order.")
    if order_type == OrderType.DELIVERY and not (delivery_address or "").strip():
        raise ValueError("A delivery address is required for delivery orders.")

    order = Order(
        restaurant_id=cart.restaurant_id,
        customer_id=customer.id if customer else None,
        call_id=call_id,
        status=S.DRAFT,
        order_type=order_type,
        delivery_address=delivery_address,
        subtotal_minor=cart.subtotal_minor,
        tax_minor=cart.tax_minor,
        total_minor=cart.total_minor,
        upsell_offered=upsell_offered,
        upsell_accepted=any(l.source == LineSource.UPSELL for l in cart.lines),
    )
    for l in cart.lines:
        order.items.append(
            OrderItem(
                item_id=l.item_id,
                name_snapshot=l.name,
                unit_price_minor=l.unit_price_minor,
                quantity=l.quantity,
                modifiers=l.modifiers,
                special_instructions=l.special_instructions,
                source=l.source,
            )
        )
    if customer and delivery_address:
        customer.default_address = delivery_address
    session.add(order)
    session.flush()
    transition(session, order, S.CONFIRMED)
    return order


def transition(session: Session, order: Order, new_status: OrderStatus) -> Order:
    new_status = OrderStatus(new_status)
    if new_status == order.status:
        return order  # idempotent: webhooks may retry
    if new_status not in ALLOWED[order.status]:
        raise InvalidTransition(f"Cannot move order {order.id} from {order.status} to {new_status}")
    order.status = new_status
    if new_status == S.DELIVERED:
        order.delivered_at = utcnow()
        _enqueue_feedback_call(session, order)
    session.flush()
    return order


def _enqueue_feedback_call(session: Session, order: Order) -> None:
    if order.customer is not None and order.customer.feedback_opt_out:
        return
    exists = session.scalar(
        select(OutboxEvent).where(OutboxEvent.order_id == order.id, OutboxEvent.kind == "feedback_call")
    )
    if exists:
        return
    delay = timedelta(minutes=get_settings().feedback_delay_minutes)
    session.add(OutboxEvent(kind="feedback_call", order_id=order.id, run_after=utcnow() + delay))
