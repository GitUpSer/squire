"""Private control-to-messaging contract. Never log responses with PII/codes."""
import httpx
from fastapi import HTTPException
from control_api.config import get_settings


class MessagingClient:
    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        settings = get_settings()
        if not settings.messaging_base_url or not settings.messaging_service_token:
            raise HTTPException(503, "messaging is not configured")
        try:
            with httpx.Client(timeout=15) as client:
                response = client.request(method, settings.messaging_base_url.rstrip("/")+path,
                    json=payload, headers={"Authorization": "Bearer "+settings.messaging_service_token})
        except httpx.HTTPError as exc:
            raise HTTPException(503, "messaging temporarily unavailable") from exc
        if response.status_code >= 400:
            status = response.status_code if response.status_code in {400,403,404,409,410,422,429} else 503
            raise HTTPException(status, "messaging could not complete this operation")
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except ValueError as exc:
            raise HTTPException(503, "invalid messaging response") from exc


def send_auth_email(email: str, code: str, operation_id: str):
    settings = get_settings()
    if not settings.resend_api_key or not settings.resend_from:
        raise HTTPException(503, "email delivery is not configured")
    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(settings.resend_api_url.rstrip("/")+"/emails",
                headers={"Authorization": "Bearer "+settings.resend_api_key,
                         "Idempotency-Key": "login-"+operation_id},
                json={"from": settings.resend_from, "to": [email], "subject": "Your Squire sign-in code",
                      "text": f"Your Squire code is {code}. It expires in 10 minutes. If you did not request this, ignore this email."})
        if response.status_code >= 400:
            raise HTTPException(503, "email delivery temporarily unavailable")
    except httpx.HTTPError as exc:
        raise HTTPException(503, "email delivery temporarily unavailable") from exc
