"""Twilio helpers: TwiML for ConversationRelay and outbound feedback calls.

ConversationRelay does speech-to-text and text-to-speech on Twilio's side and talks
to our WebSocket with JSON text messages. Message shapes used here (check against
Twilio's current ConversationRelay docs before production):
  from Twilio: {"type": "setup", "callSid", "from", "to", "customParameters"},
               {"type": "prompt", "voicePrompt", "last"}, {"type": "interrupt"},
               {"type": "dtmf", "digit"}, {"type": "error", "description"}
  to Twilio:   {"type": "text", "token", "last"}, {"type": "end"}
"""

from xml.sax.saxutils import quoteattr

from app.config import get_settings


def ws_url(path: str) -> str:
    base = get_settings().public_base_url.rstrip("/")
    return base.replace("https://", "wss://").replace("http://", "ws://") + path


def conversation_relay_twiml(path: str, greeting: str, params: dict[str, str] | None = None,
                             language: str = "en-IN") -> str:
    param_xml = "".join(f"<Parameter name={quoteattr(k)} value={quoteattr(str(v))}/>" for k, v in (params or {}).items())
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f"<ConversationRelay url={quoteattr(ws_url(path))} welcomeGreeting={quoteattr(greeting)} "
        f"language={quoteattr(language)} interruptible=\"true\">{param_xml}</ConversationRelay>"
        "</Connect></Response>"
    )


def transfer_twiml(number: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Dial>{number}</Dial></Response>'


def place_feedback_call(to_number: str, from_number: str, twiml: str) -> str:
    """Start an outbound call; returns the Twilio call SID."""
    from twilio.rest import Client  # imported lazily so tests don't need credentials

    s = get_settings()
    client = Client(s.twilio_account_sid, s.twilio_auth_token)
    call = client.calls.create(to=to_number, from_=from_number, twiml=twiml)
    return call.sid
