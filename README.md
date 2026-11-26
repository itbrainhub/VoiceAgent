# Restaurant Voice Agent

An AI phone agent for restaurants and food delivery:

- **Inbound order calls**: answers menu questions (prices, ingredients, allergens, availability), takes the order with items, quantities and options, reads it back and places it only after the caller confirms.
- **Smart upselling**: before confirming, suggests one relevant add-on (staff-defined pairings first, then missing drink / side / dessert).
- **Post-delivery feedback call**: when an order is marked Delivered, a worker calls the customer after a delay and records overall, food and delivery ratings plus comments. Low scores are flagged for staff.

Architecture and plan: [design doc](https://claude.ai/artifact/BiKUYsVucWhZn8ie6v24xs).

## Stack

| Layer | Choice |
| --- | --- |
| Phone calls | Twilio Programmable Voice |
| Speech to text / text to speech | Twilio ConversationRelay (our service only sees text over a WebSocket) |
| Conversation | Claude Haiku 5.5 (`claude-haiku-5-5`) with tool use; Sonnet 5.5 for offline analysis |
| Backend | Python 3.12, FastAPI |
| Data | SQLite for local dev, Postgres in production (SQLAlchemy) |

Claude never states a menu fact or price on its own: it calls backend tools (`search_menu`, `get_item_details`, `add_to_cart`, `view_cart`, `get_upsell_suggestions`, `place_order`, ...). The backend validates every change, and `place_order` is refused unless the order was read back after the last change and the caller confirmed.

## Layout

```
app/
  main.py          FastAPI: Twilio webhook, ConversationRelay WebSocket, order/menu/status API
  agent/session.py One call with Claude: history + tool loop (shared by phone and simulator)
  agent/tools.py   Tool schemas and their implementations
  agent/prompts.py System prompts for order and feedback calls
  cart.py          In-call cart with menu validation and totals
  upsell.py        Cross-sell engine
  orders.py        Order state machine; Delivered queues the feedback call (transactional outbox)
  worker.py        Places due feedback calls within local calling hours
  menu.py, seed.py Menu search / formatting, JSON menu loader
  simulator.py     Text-mode call simulator
  data/demo_menu.json  Demo restaurant menu
tests/             pytest suite (Claude is replaced by a scripted fake client)
```

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # add ANTHROPIC_API_KEY
python -m app.seed              # load the demo menu (or: python -m app.seed my_menu.json)
pytest -q                       # no API key needed

python -m app.simulator         # talk to the ordering agent in your terminal
python -m app.simulator --feedback 1   # feedback call for order 1
```

### Real phone calls

1. `uvicorn app.main:app --port 8000` and expose it over HTTPS (e.g. `ngrok http 8000`); set `PUBLIC_BASE_URL` to that URL.
2. In Twilio, set your number's voice webhook to `POST {PUBLIC_BASE_URL}/twilio/voice`, and set the restaurant's `phone_number` in the menu JSON to that number.
3. Run `python -m app.worker` for feedback calls.
4. Mark orders through the API: `POST /orders/{id}/status {"status": "delivered"}`.

Or run everything with `docker compose up` (Postgres, API and worker).

## API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/twilio/voice` | Twilio inbound webhook, returns ConversationRelay TwiML |
| WS | `/ws/relay` | ConversationRelay session for order and feedback calls |
| GET | `/restaurants/{id}/menu` | Menu with options |
| GET | `/restaurants/{id}/orders?status=` | Recent orders (staff page / POS) |
| POST | `/orders/{id}/status` | Move an order along the state machine; `delivered` queues the feedback call |

Order states: `draft → confirmed → accepted → preparing → out_for_delivery → delivered`, with `cancelled` allowed up to `accepted`. Invalid moves return 409; repeating the current state is a no-op so webhook retries are safe.

## Status

Phase 0 (foundations) and the core of Phase 1 (inbound ordering) and Phase 3 (feedback call) are in place and tested with a fake Claude client. Not yet done:

- Live test against the Claude API and a Twilio number
- Warm transfer to staff (`transfer_to_human` currently ends the AI leg)
- Twilio request signature validation, call recording consent, PII retention rules
- Redis-backed call state for multiple service instances; no-answer retries via Twilio status callbacks
- Feedback sentiment / topic tagging with Sonnet; staff dashboard
- Database migrations (Alembic)
