"""Database schema. Money is stored in minor units (paise / cents).

Order lines copy the item name and price at order time, so later menu edits never
change a past order.
"""

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrderStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    ACCEPTED = "accepted"
    PREPARING = "preparing"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class OrderType(StrEnum):
    DELIVERY = "delivery"
    PICKUP = "pickup"


class LineSource(StrEnum):
    CUSTOMER = "customer"
    UPSELL = "upsell"


class Restaurant(Base):
    __tablename__ = "restaurants"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    phone_number: Mapped[str | None] = mapped_column(String(32), unique=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    tax_rate_bp: Mapped[int] = mapped_column(Integer, default=500)  # basis points, 500 = 5%
    hours: Mapped[dict] = mapped_column(JSON, default=dict)
    categories: Mapped[list["MenuCategory"]] = relationship(
        back_populates="restaurant", order_by="MenuCategory.sort_order"
    )


class MenuCategory(Base):
    __tablename__ = "menu_categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("restaurants.id"))
    name: Mapped[str] = mapped_column(String(100))
    # A coarse kind used by the upsell engine: main, side, drink, dessert, other
    kind: Mapped[str] = mapped_column(String(20), default="other")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    restaurant: Mapped[Restaurant] = relationship(back_populates="categories")
    items: Mapped[list["MenuItem"]] = relationship(back_populates="category")


class MenuItem(Base):
    __tablename__ = "menu_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("menu_categories.id"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    price_minor: Mapped[int] = mapped_column(Integer)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    is_veg: Mapped[bool] = mapped_column(Boolean, default=False)
    dietary_tags: Mapped[list] = mapped_column(JSON, default=list)
    allergens: Mapped[list] = mapped_column(JSON, default=list)
    ingredients: Mapped[list] = mapped_column(JSON, default=list)
    spice_level: Mapped[int] = mapped_column(Integer, default=0)  # 0-3
    synonyms: Mapped[list] = mapped_column(JSON, default=list)  # helps speech matching
    popularity: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[MenuCategory] = relationship(back_populates="items")
    modifier_groups: Mapped[list["ModifierGroup"]] = relationship(back_populates="item")


class ModifierGroup(Base):
    __tablename__ = "modifier_groups"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("menu_items.id"))
    name: Mapped[str] = mapped_column(String(100))
    min_select: Mapped[int] = mapped_column(Integer, default=0)
    max_select: Mapped[int] = mapped_column(Integer, default=1)
    item: Mapped[MenuItem] = relationship(back_populates="modifier_groups")
    modifiers: Mapped[list["Modifier"]] = relationship(back_populates="group")


class Modifier(Base):
    __tablename__ = "modifiers"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("modifier_groups.id"))
    name: Mapped[str] = mapped_column(String(100))
    price_delta_minor: Mapped[int] = mapped_column(Integer, default=0)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    group: Mapped[ModifierGroup] = relationship(back_populates="modifiers")


class UpsellRule(Base):
    """Staff-editable pairing: when the cart holds `trigger_item` (or any item of
    `trigger_category`), suggest `suggest_item`."""

    __tablename__ = "upsell_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("restaurants.id"))
    trigger_item_id: Mapped[int | None] = mapped_column(ForeignKey("menu_items.id"))
    trigger_category_id: Mapped[int | None] = mapped_column(ForeignKey("menu_categories.id"))
    suggest_item_id: Mapped[int] = mapped_column(ForeignKey("menu_items.id"))
    pitch: Mapped[str] = mapped_column(String(200), default="")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    phone_e164: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str | None] = mapped_column(String(200))
    default_address: Mapped[str | None] = mapped_column(Text)
    feedback_opt_out: Mapped[bool] = mapped_column(Boolean, default=False)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("restaurants.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id"))
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.DRAFT)
    order_type: Mapped[OrderType] = mapped_column(Enum(OrderType), default=OrderType.DELIVERY)
    delivery_address: Mapped[str | None] = mapped_column(Text)
    subtotal_minor: Mapped[int] = mapped_column(Integer, default=0)
    tax_minor: Mapped[int] = mapped_column(Integer, default=0)
    total_minor: Mapped[int] = mapped_column(Integer, default=0)
    upsell_offered: Mapped[bool] = mapped_column(Boolean, default=False)
    upsell_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    customer: Mapped[Customer | None] = relationship()


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    item_id: Mapped[int] = mapped_column(ForeignKey("menu_items.id"))
    name_snapshot: Mapped[str] = mapped_column(String(200))
    unit_price_minor: Mapped[int] = mapped_column(Integer)  # includes modifier deltas
    quantity: Mapped[int] = mapped_column(Integer)
    modifiers: Mapped[list] = mapped_column(JSON, default=list)  # [{id, name, price_delta_minor}]
    special_instructions: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[LineSource] = mapped_column(Enum(LineSource), default=LineSource.CUSTOMER)
    order: Mapped[Order] = relationship(back_populates="items")


class Call(Base):
    __tablename__ = "calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    twilio_call_sid: Mapped[str | None] = mapped_column(String(64), unique=True)
    direction: Mapped[str] = mapped_column(String(10))  # inbound / outbound
    purpose: Mapped[str] = mapped_column(String(10))  # order / feedback
    from_number: Mapped[str | None] = mapped_column(String(32))
    to_number: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(40))
    transcript: Mapped[list] = mapped_column(JSON, default=list)


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (UniqueConstraint("order_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id"))
    overall_rating: Mapped[int | None] = mapped_column(Integer)
    food_rating: Mapped[int | None] = mapped_column(Integer)
    delivery_rating: Mapped[int | None] = mapped_column(Integer)
    comments: Mapped[str] = mapped_column(Text, default="")
    sentiment: Mapped[str | None] = mapped_column(String(20))
    tags: Mapped[list] = mapped_column(JSON, default=list)
    needs_followup: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OutboxEvent(Base):
    """Written in the same transaction as a status change, so a Delivered order
    always produces exactly one feedback-call job (transactional outbox)."""

    __tablename__ = "outbox_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))  # e.g. feedback_call
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
