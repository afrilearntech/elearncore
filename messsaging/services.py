import logging
import requests

from elearncore import settings

logger = logging.getLogger(__name__)


def send_sms(message: str, recipients, sender: str = settings.SENDER_ID):
    """Send an SMS with bounded network time and explicit error handling."""
    if not settings.ARKESEL_API_KEY:
        raise RuntimeError("ARKESEL_SMS_API_KEY is not configured")
    header = {"api-key": settings.ARKESEL_API_KEY, 'Content-Type': 'application/json',
              'Accept': 'application/json'}
    send_sms_url = "https://sms.arkesel.com/api/v2/sms/send"
    payload = {
        "sender": sender,
        "message": message,
        "recipients": list(recipients),
    }
    response = requests.post(
        send_sms_url,
        headers=header,
        json=payload,
        timeout=(5, 15),
    )
    response.raise_for_status()
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("SMS provider returned invalid JSON") from exc
    logger.info("SMS accepted by provider for %d recipient(s)", len(payload["recipients"]))
    return result
