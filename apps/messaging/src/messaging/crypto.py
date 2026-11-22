"""Domain-separated hashes and authenticated encryption; no plaintext routing storage."""
import base64
import hashlib
import hmac
import json
import secrets
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .config import settings


def identifier() -> str:
    return secrets.token_hex(16)


def digest(domain: str, value: str) -> str:
    return hmac.new(settings().hmac_key.encode(), (domain + "\0" + value).encode(), hashlib.sha256).hexdigest()


def seal(value: object, context: str) -> str:
    key = hashlib.sha256(settings().encryption_key.encode()).digest()
    nonce = secrets.token_bytes(12)
    body = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(nonce + AESGCM(key).encrypt(nonce, body, context.encode())).decode()


def open_sealed(value: str, context: str) -> object:
    key = hashlib.sha256(settings().encryption_key.encode()).digest()
    raw = base64.urlsafe_b64decode(value)
    return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], context.encode()))
