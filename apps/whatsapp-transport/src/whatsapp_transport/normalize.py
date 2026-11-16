import hashlib
import re
from datetime import datetime

JID = re.compile(r"^[0-9]+(?:-[0-9]+)?@(s\.whatsapp\.net|lid|g\.us)$")
PHONE = re.compile(r"^\+?[1-9][0-9]{6,14}$")


def jid(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("User") and value.get("Server"):
        return f"{value['User']}@{value['Server']}"
    return ""


def recipient(value):
    if PHONE.fullmatch(value):
        return value.lstrip("+") + "@s.whatsapp.net"
    if JID.fullmatch(value):
        return value
    raise ValueError("recipient must be an explicit phone or permitted JID")


def epoch(value):
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    return 0


def chat_type(chat):
    return "group" if chat.endswith("@g.us") else "dm" if chat.endswith(("@lid", "@s.whatsapp.net")) else "other"


def change_event(store_id, change, message, activated_at, verified_live=False):
    sequence = int(change["sequence"])
    chat = change["chat_jid"]
    message_id = change["msg_id"]
    operation = change["operation"]
    timestamp = epoch(change.get("message_time"))
    base = {
        "event_id": hashlib.sha256(f"{store_id}:{sequence}".encode()).hexdigest(),
        "sequence": sequence, "provider_message_id": message_id, "chat_jid": chat,
        "sender_jid": "", "kind": "message" if operation == "upsert" else operation,
        "chat_type": chat_type(chat), "origin": "replay" if verified_live and timestamp >= activated_at else "history",
        "timestamp": timestamp, "from_me": False,
    }
    if operation == "identity_move":
        base["new_chat_jid"] = change["new_chat_jid"]
        return base
    if operation == "delete" or message is None:
        base["kind"] = "delete"
        return base
    actual_chat = message.get("ChatJID", chat)
    base.update(chat_jid=actual_chat, chat_type=chat_type(actual_chat),
                sender_jid=message.get("SenderJID", ""), from_me=bool(message.get("FromMe")),
                timestamp=epoch(message.get("Timestamp", timestamp)),
                text=message.get("Text") or message.get("DisplayText") or "",
                reply_to=message.get("quoted_msg_id"), mentions=[])
    if actual_chat.endswith("@s.whatsapp.net"):
        base["chat_phone_jid"] = actual_chat
    if base["sender_jid"].endswith("@s.whatsapp.net"):
        base["sender_phone_jid"] = base["sender_jid"]
    if message.get("MediaType") in {"image", "audio", "video", "document"}:
        from .media import safe_filename, safe_mime
        base["media"] = {"type": message["MediaType"],
                         "mime_type": safe_mime(message.get("MimeType"), message["MediaType"]),
                         "filename": safe_filename(message.get("Filename"))}
    if message.get("Revoked") or message.get("DeletedForMe") or message.get("deleted_at") or message.get("payload_purged_at"):
        base["kind"] = "delete"
        base.pop("text", None)
        base.pop("media", None)
    return base


def group_info(raw, now):
    return {
        "jid": jid(raw.get("JID")), "fetched_at": int(now),
        "owner_jid": jid(raw.get("OwnerJID")),
        "participants": [
            {"jid": jid(p.get("JID")), "phone_jid": jid(p.get("PhoneNumber")) or None,
             "admin": bool(p.get("IsAdmin") or p.get("IsSuperAdmin"))}
            for p in raw.get("Participants", [])
        ],
    }
