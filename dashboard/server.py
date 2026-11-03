"""Локальный пульт второго бота. Слушает только этот компьютер."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SETTINGS_DIR = os.path.join(ROOT, "bot_settings")
DATA_DIR = os.path.join(ROOT, "bot_data")
LOG_PATH = os.path.join(ROOT, "logs", "latest.log")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
HOST = "127.0.0.1"
PORT = 8770
MAX_BODY = 200_000
LOCK = threading.Lock()
STOCK_FILE = "account_stock.json"
ACCOUNT_FIELDS = ("login", "password", "email", "email_password", "mail_url")
ACCOUNT_REQUIRED = {"login", "password", "email", "email_password"}
ACCOUNT_LIMITS = {
    "login": 120,
    "password": 200,
    "email": 200,
    "email_password": 200,
    "mail_url": 500,
}
QUEUE_ROLES = ("primary", "reserve")

FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

LIST_FILES = {
    "bump": "auto_bump_items.json",
    "restore": "auto_restore_items.json",
    "complete": "auto_complete_deals.json",
}

MESSAGE_IDS = (
    "first_message",
    "cmd_error",
    "cmd_seller",
    "new_deal",
    "deal_sent",
    "deal_confirmed",
    "deal_refunded",
    "deal_has_problem",
    "new_review",
)

# path -> ("bool",) | ("text", max) | ("int", min, max) | ("enum", {...}) | ("time",)
RULES = {
    "playerok.watermark.enabled": ("bool",),
    "playerok.watermark.value": ("text", 80),
    "playerok.read_chat": ("bool",),
    "playerok.auto_restore_items.sold": ("bool",),
    "playerok.auto_restore_items.expired": ("bool",),
    "playerok.auto_restore_items.all": ("bool",),
    "playerok.auto_restore_items.free": ("bool",),
    "playerok.auto_restore_items.premium": ("bool",),
    "playerok.auto_restore_items.recreate": ("bool",),
    "playerok.auto_restore_items.publish_status": ("enum", {"free", "premium", "last"}),
    "playerok.auto_complete_deals.enabled": ("bool",),
    "playerok.auto_complete_deals.all": ("bool",),
    "playerok.auto_bump_items.enabled": ("bool",),
    "playerok.auto_bump_items.mode": ("enum", {"interval", "position"}),
    "playerok.auto_bump_items.interval": ("int", 300, 86400),
    "playerok.auto_bump_items.position": ("int", 1, 500),
    "playerok.auto_bump_items.cooldown": ("int", 60, 7200),
    "playerok.auto_bump_items.check_interval": ("int", 30, 600),
    "playerok.auto_bump_items.all": ("bool",),
    "playerok.auto_bump_items.night.enabled": ("bool",),
    "playerok.auto_bump_items.night.interval": ("int", 300, 86400),
    "playerok.auto_bump_items.night.position": ("int", 1, 500),
    "playerok.auto_bump_items.night.time_from": ("time",),
    "playerok.auto_bump_items.night.time_to": ("time",),
    "playerok.auto_snipe.enabled": ("bool",),
    "playerok.auto_snipe.delay_seconds": ("int", 5, 3600),
    "playerok.auto_snipe.check_interval": ("int", 15, 600),
    "playerok.auto_snipe.max_pay_price": ("int", 1, 5000),
    "playerok.auto_withdrawal.enabled": ("bool",),
    "playerok.auto_withdrawal.interval": ("int", 3600, 604800),
    "playerok.notifications.enabled": ("bool",),
    "playerok.api.requests_timeout": ("int", 5, 120),
    "updates.auto_update": ("bool",),
    "updates.notify": ("bool",),
    "logs.max_file_size": ("int", 64, 4096),
}
SNIPE_TARGETS_FILE = "snipe_targets.json"
SELLER_URL_RE = re.compile(
    r"playerok\.com/(?:user|profile|products/user)/([^/?#]+)",
    re.I,
)
_ACCOUNT_CACHE = {"at": 0.0, "account": None}
_ACCOUNT_CACHE_LOCK = threading.Lock()
_MY_ITEMS_CACHE = {"at": 0.0, "items": None}
_MY_ITEMS_CACHE_LOCK = threading.Lock()
for _event in (
    "new_user_message",
    "new_system_message",
    "new_deal",
    "new_review",
    "new_problem",
    "deal_status_changed",
    "item_restored",
    "item_bumped",
    "withdrawal_requested",
):
    RULES[f"playerok.notifications.events.{_event}"] = ("bool",)

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _read_json(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: str, payload) -> None:
    directory = os.path.dirname(path)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, delete=False
    ) as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=4)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        temporary = tmp.name
    os.replace(temporary, path)


def _version() -> str:
    path = os.path.join(ROOT, "__init__.py")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            match = re.search(r'VERSION\s*=\s*"([^"]+)"', handle.read())
        return match.group(1) if match else ""
    except OSError:
        return ""


ACCOUNT_SNAPSHOT = os.path.join(DATA_DIR, "account_snapshot.json")


def _account_from_snapshot() -> dict | None:
    try:
        raw = _read_json(ACCOUNT_SNAPSHOT)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    import time
    try:
        stamp = float(raw.get("updated_at") or 0)
    except (TypeError, ValueError):
        stamp = 0
    # снимок старше суток считаем устаревшим — тогда читаем лог
    if stamp and (time.time() - stamp) > 86400:
        return None
    return {
        "username": str(raw.get("username") or ""),
        "balance": str(raw.get("balance") or ""),
        "available": str(raw.get("available") or ""),
        "pending": str(raw.get("pending") or ""),
        "frozen": str(raw.get("frozen") or ""),
        "sales": str(raw.get("sales") or ""),
        "purchases": str(raw.get("purchases") or ""),
        "telegram": str(raw.get("telegram") or ""),
    }


def _account_from_log() -> dict:
    empty = {
        "username": "",
        "balance": "",
        "available": "",
        "pending": "",
        "frozen": "",
        "sales": "",
        "purchases": "",
        "telegram": "",
    }
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as handle:
            # раньше брали 12Кб — блок старта быстро уезжал из хвоста из‑за debug WS
            text = handle.read()[-524288:]
    except OSError:
        return empty
    index = text.rfind("Информация об аккаунте")
    chunk = text[index:index + 1200] if index >= 0 else ""

    def grab(label: str) -> str:
        match = re.search(rf"{label}:\s*(\S.*)", chunk)
        return match.group(1).strip() if match else ""

    names = re.findall(r"Telegram бот @([A-Za-z0-9_]+)", text)
    empty.update({
        "username": grab("Никнейм"),
        "balance": grab("Баланс"),
        "available": grab("Доступно"),
        "pending": grab("В ожидании"),
        "frozen": grab("Заморожено"),
        "sales": grab("Активные продажи"),
        "purchases": grab("Активные покупки"),
        "telegram": names[-1] if names else "",
    })
    return empty


def _account() -> dict:
    empty = {
        "username": "",
        "balance": "",
        "available": "",
        "pending": "",
        "frozen": "",
        "sales": "",
        "purchases": "",
        "telegram": "",
        "alive": False,
    }
    import time
    try:
        age = os.path.getmtime(LOG_PATH)
        empty["alive"] = (time.time() - age) < 600
    except OSError:
        empty["alive"] = False

    snap = _account_from_snapshot()
    parsed = snap if snap and (snap.get("balance") or snap.get("username")) else _account_from_log()
    empty.update(parsed)
    return empty


def _binding_items(entry) -> list[dict]:
    if not isinstance(entry, dict):
        return []
    items = entry.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict) and item.get("id")]


def _phrase_only(entries: list) -> list:
    kept = []
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("keyphrases") and not _binding_items(entry):
            kept.append(entry)
        elif isinstance(entry, list):
            kept.append({"keyphrases": [str(phrase) for phrase in entry]})
    return kept


def _flatten_ids(entries) -> list[str]:
    seen = []
    for entry in entries or []:
        for item in _binding_items(entry):
            item_id = str(item.get("id"))
            if item_id not in seen:
                seen.append(item_id)
    return seen


def _known_items() -> dict[str, dict]:
    known = {}
    path = os.path.join(DATA_DIR, "saved_items.json")
    try:
        raw = _read_json(path)
    except (OSError, json.JSONDecodeError):
        raw = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            image = ""
            attachment = item.get("attachment")
            if isinstance(attachment, dict) and str(attachment.get("url") or "").startswith("https://"):
                image = attachment["url"]
            known[item["id"]] = {
                "id": item["id"],
                "slug": item.get("slug") or "",
                "name": item.get("name") or item.get("slug") or item["id"],
                "price": item.get("price"),
                "status": item.get("status") or "",
                "priority": item.get("priority") or "",
                "image": image,
            }
    for filename in LIST_FILES.values():
        try:
            payload = _read_json(os.path.join(SETTINGS_DIR, filename))
        except (OSError, json.JSONDecodeError):
            continue
        for side in ("included", "excluded"):
            for entry in payload.get(side) or []:
                for item in _binding_items(entry):
                    if item["id"] in known:
                        continue
                    known[item["id"]] = {
                        "id": item["id"],
                        "slug": item.get("slug") or "",
                        "name": item.get("name") or item.get("slug") or item["id"],
                        "price": None,
                        "status": "",
                        "priority": "",
                        "image": "",
                    }
    try:
        stock_raw = _read_json(os.path.join(SETTINGS_DIR, STOCK_FILE))
    except (OSError, json.JSONDecodeError):
        stock_raw = {}
    pools = stock_raw.get("pools") if isinstance(stock_raw, dict) else []
    for pool in pools or []:
        for item in _binding_items(pool):
            if item["id"] in known:
                continue
            known[item["id"]] = {
                "id": item["id"],
                "slug": item.get("slug") or "",
                "name": item.get("name") or item.get("slug") or item["id"],
                "price": None,
                "status": "",
                "priority": "",
                "image": "",
            }
    return known


def _public_lists() -> dict:
    result = {}
    for key, filename in LIST_FILES.items():
        try:
            payload = _read_json(os.path.join(SETTINGS_DIR, filename))
        except (OSError, json.JSONDecodeError):
            payload = {"included": [], "excluded": []}
        result[key] = {
            "included": _flatten_ids(payload.get("included")),
            "excluded": _flatten_ids(payload.get("excluded")),
        }
    return result


def _public_messages(raw: dict) -> dict:
    result = {}
    for message_id in MESSAGE_IDS:
        entry = raw.get(message_id) or {}
        lines = entry.get("text") if isinstance(entry.get("text"), list) else []
        result[message_id] = {
            "label": entry.get("label") or message_id,
            "enabled": bool(entry.get("enabled")),
            "text": "\n".join(str(line) for line in lines),
        }
    return result


def _public_journal() -> dict:
    path = os.path.join(DATA_DIR, "bump_journal.json")
    try:
        raw = _read_json(path)
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    entries = raw.get("entries") if isinstance(raw.get("entries"), list) else []
    plan = raw.get("plan") if isinstance(raw.get("plan"), list) else []
    public_entries = []
    for entry in entries[-40:]:
        if not isinstance(entry, dict):
            continue
        public_entries.append({
            "at": entry.get("at") or "",
            "name": entry.get("name") or "",
            "kind": entry.get("kind") or "",
            "ok": bool(entry.get("ok")),
            "detail": entry.get("detail") or "",
        })
    public_entries.reverse()
    public_plan = []
    for row in plan:
        if not isinstance(row, dict):
            continue
        public_plan.append({
            "name": row.get("name") or "",
            "kind": row.get("kind") or "",
            "next_at": row.get("next_at") or "",
        })
    return {
        "slot": int(raw.get("slot") or 0),
        "plan": public_plan,
        "entries": public_entries,
    }


def _public_snipe_journal() -> dict:
    path = os.path.join(DATA_DIR, "snipe_journal.json")
    try:
        raw = _read_json(path)
    except (OSError, json.JSONDecodeError):
        raw = {"entries": []}
    entries = raw.get("entries") if isinstance(raw, dict) else []
    public = []
    for entry in (entries or [])[-60:]:
        if not isinstance(entry, dict):
            continue
        public.append({
            "at": entry.get("at") or "",
            "event": entry.get("event") or "",
            "competitor_name": entry.get("competitor_name") or "",
            "my_name": entry.get("my_name") or "",
            "detail": entry.get("detail") or "",
            "ok": bool(entry.get("ok", True)),
        })
    public.reverse()
    return {"entries": public}


def _public_greetings() -> list:
    path = os.path.join(SETTINGS_DIR, "greetings.json")
    try:
        raw = _read_json(path)
    except (OSError, json.JSONDecodeError):
        raw = {"items": []}
    items = []
    for entry in (raw.get("items") if isinstance(raw, dict) else []) or []:
        if not isinstance(entry, dict):
            continue
        gid = str(entry.get("id") or "").strip()
        if not gid:
            continue
        lots = []
        for lot in entry.get("lots") or []:
            if not isinstance(lot, dict) or not lot.get("id"):
                continue
            lots.append({
                "id": str(lot.get("id") or ""),
                "slug": str(lot.get("slug") or ""),
                "name": str(lot.get("name") or lot.get("slug") or lot.get("id") or ""),
            })
        lines = entry.get("text") if isinstance(entry.get("text"), list) else []
        items.append({
            "id": gid,
            "enabled": bool(entry.get("enabled")),
            "title": str(entry.get("title") or "Приветствие")[:80],
            "text": "\n".join(str(line) for line in lines),
            "lots": lots,
        })
    return items


def _normalize_greeting_lots(raw_lots, known: dict[str, dict]) -> list[dict]:
    lots = []
    seen = set()
    if not isinstance(raw_lots, list):
        return lots
    for item in raw_lots:
        item_id = ""
        if isinstance(item, str):
            item_id = item.strip()
        elif isinstance(item, dict):
            item_id = str(item.get("id") or "").strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        known_item = known.get(item_id) or {}
        if isinstance(item, dict):
            name = str(item.get("name") or known_item.get("name") or item_id)
            slug = str(item.get("slug") or known_item.get("slug") or "")
        else:
            name = str(known_item.get("name") or item_id)
            slug = str(known_item.get("slug") or "")
        lots.append({"id": item_id, "slug": slug[:200], "name": name[:200]})
    if len(lots) > 100:
        raise ValueError("слишком много лотов в одном приветствии")
    return lots


def apply_greetings(body: dict) -> dict:
    action = str(body.get("action") or "").strip().lower()
    path = os.path.join(SETTINGS_DIR, "greetings.json")
    known = _known_items()
    try:
        for row in fetch_my_active_items(force=False):
            if row.get("id"):
                known[row["id"]] = {
                    "id": row["id"],
                    "slug": row.get("slug") or "",
                    "name": row.get("name") or row["id"],
                    "price": row.get("price"),
                    "status": row.get("status") or "",
                    "priority": row.get("priority") or "",
                    "image": "",
                }
    except Exception:
        pass

    with LOCK:
        try:
            current = _read_json(path)
        except (OSError, json.JSONDecodeError):
            current = {"items": []}
        if not isinstance(current, dict):
            current = {"items": []}
        items = [row for row in (current.get("items") or []) if isinstance(row, dict)]

        if action == "add":
            title = str(body.get("title") or "Приветствие").strip()[:80] or "Приветствие"
            text = body.get("text")
            if not isinstance(text, str):
                raise ValueError("нужен текст")
            if len(text) > 4000:
                raise ValueError("слишком длинный текст")
            lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            if len(lines) > 40:
                raise ValueError("слишком много строк")
            if len(items) >= 30:
                raise ValueError("слишком много приветствий")
            items.append({
                "id": str(uuid.uuid4()),
                "enabled": True,
                "title": title,
                "text": lines,
                "lots": _normalize_greeting_lots(body.get("lots") or [], known),
            })
        elif action == "edit":
            gid = str(body.get("id") or "").strip()
            target = next((row for row in items if str(row.get("id") or "") == gid), None)
            if target is None:
                raise ValueError("приветствие не найдено")
            if "title" in body:
                title = str(body.get("title") or "").strip()[:80]
                if title:
                    target["title"] = title
            if "enabled" in body:
                if not isinstance(body.get("enabled"), bool):
                    raise ValueError("нужен переключатель")
                target["enabled"] = body["enabled"]
            if "text" in body:
                text = body.get("text")
                if not isinstance(text, str):
                    raise ValueError("нужен текст")
                if len(text) > 4000:
                    raise ValueError("слишком длинный текст")
                lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                if len(lines) > 40:
                    raise ValueError("слишком много строк")
                target["text"] = lines
            if "lots" in body:
                target["lots"] = _normalize_greeting_lots(body.get("lots"), known)
        elif action == "delete":
            gid = str(body.get("id") or "").strip()
            items = [row for row in items if str(row.get("id") or "") != gid]
        else:
            raise ValueError("неизвестное действие")

        current = {"items": items}
        _write_json(path, current)
    return snapshot()


def _public_snipe_targets() -> dict:
    path = os.path.join(SETTINGS_DIR, SNIPE_TARGETS_FILE)
    try:
        raw = _read_json(path)
    except (OSError, json.JSONDecodeError):
        raw = {"sellers": []}
    sellers = []
    for seller in raw.get("sellers") or []:
        if not isinstance(seller, dict):
            continue
        links = []
        for link in seller.get("links") or []:
            if not isinstance(link, dict):
                continue
            competitor_id = str(link.get("competitor_item_id") or "")
            my_id = str(link.get("my_item_id") or "")
            if not competitor_id or not my_id:
                continue
            links.append({
                "competitor_item_id": competitor_id,
                "competitor_slug": str(link.get("competitor_slug") or ""),
                "competitor_name": str(link.get("competitor_name") or ""),
                "my_item_id": my_id,
                "my_name": str(link.get("my_name") or ""),
            })
        if not links:
            continue
        sellers.append({
            "username": str(seller.get("username") or ""),
            "user_id": str(seller.get("user_id") or ""),
            "links": links,
        })
    return {"sellers": sellers}


def _parse_seller_ref(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("укажите ссылку или ник продавца")
    match = SELLER_URL_RE.search(raw)
    if match:
        return unquote(match.group(1)).strip()
    cleaned = raw.lstrip("@").strip()
    if "://" in cleaned or "/" in cleaned or " " in cleaned:
        raise ValueError("нужна ссылка playerok.com/profile/ник, /user/ник или сам ник")
    if not cleaned or len(cleaned) > 64:
        raise ValueError("неверный ник продавца")
    return cleaned


def _playerok_account():
    import time
    from playerokapi.account import Account

    with _ACCOUNT_CACHE_LOCK:
        now = time.time()
        cached = _ACCOUNT_CACHE.get("account")
        if cached is not None and now - float(_ACCOUNT_CACHE.get("at") or 0) < 90:
            return cached
        config = _read_json(os.path.join(SETTINGS_DIR, "config.json"))
        api = config["playerok"]["api"]
        cookies = (api.get("cookies") or "").strip()
        if not cookies:
            raise ValueError("в конфиге нет cookies Playerok")
        account = Account(
            cookies=cookies,
            user_agent=api.get("user_agent") or "",
            proxy=api.get("proxy") or None,
            requests_timeout=int(api.get("requests_timeout") or 30),
        ).get()
        _ACCOUNT_CACHE["account"] = account
        _ACCOUNT_CACHE["at"] = now
        return account


def _seller_username_candidates(username: str) -> list[str]:
    """В URL профиль часто с дефисом (Iama-shop), а ник в API с пробелом (Iama shop)."""
    raw = (username or "").strip()
    if not raw:
        return []
    variants = [raw]
    if "-" in raw:
        variants.append(raw.replace("-", " "))
    if "_" in raw:
        variants.append(raw.replace("_", " "))
    # уникальные с сохранением порядка
    seen = set()
    out = []
    for value in variants:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _item_public_row(item) -> dict:
    priority = getattr(getattr(item, "priority", None), "name", None) or ""
    status = getattr(getattr(item, "status", None), "name", None) or ""
    return {
        "id": getattr(item, "id", "") or "",
        "slug": getattr(item, "slug", "") or "",
        "name": getattr(item, "name", "") or "",
        "price": getattr(item, "price", None),
        "priority": priority,
        "status": status,
        "approval_date": getattr(item, "approval_date", None) or "",
    }


def fetch_my_active_items(force: bool = False) -> list[dict]:
    """Актуальные лоты в продаже (не старый кэш из bump excluded)."""
    import time
    from playerokapi.enums import ItemStatuses

    with _MY_ITEMS_CACHE_LOCK:
        cached = _MY_ITEMS_CACHE.get("items")
        if (
            not force
            and cached is not None
            and time.time() - float(_MY_ITEMS_CACHE.get("at") or 0) < 45
        ):
            return list(cached)

    account = _playerok_account()
    items = []
    after = None
    for _ in range(12):
        page = account.get_my_items(
            statuses=[ItemStatuses.APPROVED],
            count=24,
            after_cursor=after,
        )
        for item in list(getattr(page, "items", None) or []):
            row = _item_public_row(item)
            if row["id"]:
                items.append(row)
        info = getattr(page, "page_info", None)
        if not info or not getattr(info, "has_next_page", False):
            break
        after = getattr(info, "end_cursor", None)
        if not after:
            break
        time.sleep(0.2)

    # премиум сверху, внутри — по имени; старые/битые id из списков не мешают
    items.sort(
        key=lambda row: (
            0 if str(row.get("priority") or "").upper() == "PREMIUM" else 1,
            (row.get("name") or "").lower(),
        )
    )
    with _MY_ITEMS_CACHE_LOCK:
        _MY_ITEMS_CACHE["items"] = list(items)
        _MY_ITEMS_CACHE["at"] = time.time()
    return items


def resolve_snipe_seller(query: str) -> dict:
    from playerokapi.enums import ItemStatuses

    username = _parse_seller_ref(query)
    account = _playerok_account()
    profile = None
    last_error = None
    for candidate in _seller_username_candidates(username):
        try:
            profile = account.get_user(username=candidate)
            break
        except Exception as exc:
            last_error = exc
            profile = None
    if profile is None:
        raise ValueError(
            str(last_error)
            or f"продавец «{username}» не найден — проверьте ссылку или ник"
        )
    items = []
    after = None
    for _ in range(8):
        page = profile.get_items(
            statuses=[ItemStatuses.APPROVED],
            count=24,
            after_cursor=after,
        )
        for item in list(getattr(page, "items", None) or []):
            row = _item_public_row(item)
            if row["id"]:
                items.append(row)
        info = getattr(page, "page_info", None)
        if not info or not getattr(info, "has_next_page", False):
            break
        after = getattr(info, "end_cursor", None)
        if not after:
            break
    try:
        my_items = fetch_my_active_items(force=True)
    except Exception:
        my_items = list(_MY_ITEMS_CACHE.get("items") or [])
    return {
        "username": getattr(profile, "username", None) or username,
        "user_id": getattr(profile, "id", "") or "",
        "items": items,
        "my_items": my_items,
    }


def apply_snipe_targets(body: dict) -> dict:
    action = str(body.get("action") or "save").strip().lower()
    path = os.path.join(SETTINGS_DIR, SNIPE_TARGETS_FILE)
    with LOCK:
        try:
            current = _read_json(path)
        except (OSError, json.JSONDecodeError):
            current = {"sellers": []}
        if not isinstance(current, dict):
            current = {"sellers": []}
        sellers = list(current.get("sellers") or [])

        if action == "save":
            incoming = body.get("sellers")
            if not isinstance(incoming, list):
                raise ValueError("нужен список sellers")
            cleaned = []
            for seller in incoming:
                if not isinstance(seller, dict):
                    continue
                username = str(seller.get("username") or "").strip()
                user_id = str(seller.get("user_id") or "").strip()
                links = []
                for link in seller.get("links") or []:
                    if not isinstance(link, dict):
                        continue
                    competitor_id = str(link.get("competitor_item_id") or "").strip()
                    my_id = str(link.get("my_item_id") or "").strip()
                    if not competitor_id or not my_id:
                        continue
                    links.append({
                        "competitor_item_id": competitor_id,
                        "competitor_slug": str(link.get("competitor_slug") or "")[:200],
                        "competitor_name": str(link.get("competitor_name") or "")[:200],
                        "my_item_id": my_id,
                        "my_name": str(link.get("my_name") or "")[:200],
                    })
                if not links:
                    continue
                cleaned.append({
                    "username": username[:64],
                    "user_id": user_id[:64],
                    "links": links,
                })
            current = {"sellers": cleaned}
        elif action == "add":
            username = str(body.get("username") or "").strip()
            user_id = str(body.get("user_id") or "").strip()
            links_in = body.get("links")
            if not username and not user_id:
                raise ValueError("нужен продавец")
            if not isinstance(links_in, list) or not links_in:
                raise ValueError("выберите хотя бы одну связку")
            new_links = []
            for link in links_in:
                if not isinstance(link, dict):
                    continue
                competitor_id = str(link.get("competitor_item_id") or "").strip()
                my_id = str(link.get("my_item_id") or "").strip()
                if not competitor_id or not my_id:
                    continue
                new_links.append({
                    "competitor_item_id": competitor_id,
                    "competitor_slug": str(link.get("competitor_slug") or "")[:200],
                    "competitor_name": str(link.get("competitor_name") or "")[:200],
                    "my_item_id": my_id,
                    "my_name": str(link.get("my_name") or "")[:200],
                })
            if not new_links:
                raise ValueError("нет валидных связок")
            seller = None
            for row in sellers:
                if not isinstance(row, dict):
                    continue
                if user_id and str(row.get("user_id") or "") == user_id:
                    seller = row
                    break
                if username and str(row.get("username") or "").lower() == username.lower():
                    seller = row
                    break
            if seller is None:
                seller = {"username": username, "user_id": user_id, "links": []}
                sellers.append(seller)
            else:
                seller["username"] = username or seller.get("username") or ""
                seller["user_id"] = user_id or seller.get("user_id") or ""
            existing = {
                (str(l.get("competitor_item_id")), str(l.get("my_item_id")))
                for l in (seller.get("links") or [])
                if isinstance(l, dict)
            }
            for link in new_links:
                pair = (link["competitor_item_id"], link["my_item_id"])
                if pair in existing:
                    continue
                seller.setdefault("links", []).append(link)
                existing.add(pair)
            current = {"sellers": sellers}
        elif action == "remove":
            competitor_id = str(body.get("competitor_item_id") or "").strip()
            my_id = str(body.get("my_item_id") or "").strip()
            if not competitor_id or not my_id:
                raise ValueError("укажите связку для удаления")
            next_sellers = []
            for seller in sellers:
                if not isinstance(seller, dict):
                    continue
                kept = [
                    link for link in (seller.get("links") or [])
                    if isinstance(link, dict)
                    and not (
                        str(link.get("competitor_item_id") or "") == competitor_id
                        and str(link.get("my_item_id") or "") == my_id
                    )
                ]
                if not kept:
                    continue
                seller = dict(seller)
                seller["links"] = kept
                next_sellers.append(seller)
            current = {"sellers": next_sellers}
        else:
            raise ValueError("неизвестное действие")

        _write_json(path, current)
    return snapshot()


def snapshot() -> dict:
    config = _read_json(os.path.join(SETTINGS_DIR, "config.json"))
    messages = _read_json(os.path.join(SETTINGS_DIR, "messages.json"))
    playerok = config["playerok"]
    bump = playerok["auto_bump_items"]
    night = bump["night"]
    snipe = playerok.get("auto_snipe") or {
        "enabled": False,
        "delay_seconds": 60,
        "check_interval": 45,
        "max_pay_price": 50,
        "last_check_time": "",
    }
    withdrawal = playerok["auto_withdrawal"]
    notifications = playerok["notifications"]
    account = _account()
    catalog = list(_known_items().values())
    catalog.sort(key=lambda item: (item.get("name") or "").lower())
    return {
        "version": _version(),
        "alive": account.pop("alive"),
        "account": account,
        "catalog": catalog,
        "journal": _public_journal(),
        "lists": _public_lists(),
        "messages": _public_messages(messages),
        "stock": _public_stock(),
        "errors": _public_errors(),
        "snipe": _public_snipe_targets(),
        "snipe_journal": _public_snipe_journal(),
        "greetings": _public_greetings(),
        "settings": {
            "watermark": {
                "enabled": bool(playerok["watermark"]["enabled"]),
                "value": playerok["watermark"].get("value") or "",
            },
            "read_chat": bool(playerok["read_chat"]),
            "restore": {
                "sold": bool(playerok["auto_restore_items"]["sold"]),
                "expired": bool(playerok["auto_restore_items"]["expired"]),
                "all": bool(playerok["auto_restore_items"]["all"]),
                "free": bool(playerok["auto_restore_items"]["free"]),
                "premium": bool(playerok["auto_restore_items"]["premium"]),
                "recreate": bool(playerok["auto_restore_items"]["recreate"]),
                "publish_status": playerok["auto_restore_items"]["publish_status"],
            },
            "complete": {
                "enabled": bool(playerok["auto_complete_deals"]["enabled"]),
                "all": bool(playerok["auto_complete_deals"]["all"]),
            },
            "bump": {
                "enabled": bool(bump["enabled"]),
                "mode": bump["mode"],
                "interval": int(bump["interval"]),
                "position": int(bump["position"]),
                "cooldown": int(bump["cooldown"]),
                "check_interval": int(bump["check_interval"]),
                "all": bool(bump["all"]),
                "last_check": bump.get("last_check_time") or "",
                "last_bump": bump.get("last_time") or "",
                "night": {
                    "enabled": bool(night["enabled"]),
                    "interval": int(night["interval"]),
                    "position": int(night["position"]),
                    "time_from": night.get("time_from") or "23:00",
                    "time_to": night.get("time_to") or "07:00",
                },
            },
            "snipe": {
                "enabled": bool(snipe.get("enabled")),
                "delay_seconds": int(snipe.get("delay_seconds") or 60),
                "check_interval": int(snipe.get("check_interval") or 45),
                "max_pay_price": int(snipe.get("max_pay_price") or 50),
                "last_check": snipe.get("last_check_time") or "",
            },
            "withdrawal": {
                "enabled": bool(withdrawal["enabled"]),
                "interval": int(withdrawal["interval"]),
                "ready": bool(withdrawal.get("credentials_type")),
            },
            "notifications": {
                "enabled": bool(notifications["enabled"]),
                "events": {
                    key: bool(notifications["events"].get(key))
                    for key in (
                        "new_user_message",
                        "new_system_message",
                        "new_deal",
                        "new_review",
                        "new_problem",
                        "deal_status_changed",
                        "item_restored",
                        "item_bumped",
                        "withdrawal_requested",
                    )
                },
            },
            "timeout": int(playerok["api"]["requests_timeout"]),
            "updates": {
                "auto_update": bool(config["updates"]["auto_update"]),
                "notify": bool(config["updates"]["notify"]),
            },
            "logs": {"max_file_size": int(config["logs"]["max_file_size"])},
        },
    }


def _check_value(rule, value):
    kind = rule[0]
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("нужен переключатель")
        return value
    if kind == "text":
        if not isinstance(value, str):
            raise ValueError("нужен текст")
        text = value.strip()
        if len(text) > rule[1]:
            raise ValueError("слишком длинный текст")
        return text
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("нужно число")
        number = int(value)
        if number < rule[1] or number > rule[2]:
            raise ValueError(f"диапазон {rule[1]}–{rule[2]}")
        return number
    if kind == "enum":
        if value not in rule[1]:
            raise ValueError("неизвестное значение")
        return value
    if kind == "time":
        if not isinstance(value, str) or not TIME_RE.match(value):
            raise ValueError("время в формате ЧЧ:ММ")
        return value
    raise ValueError("нельзя изменить это поле")


def _set_path(config: dict, path: str, value) -> None:
    cursor = config
    parts = path.split(".")
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            raise ValueError("поле не найдено")
        cursor = cursor[part]
    leaf = parts[-1]
    if not isinstance(cursor, dict) or leaf not in cursor:
        raise ValueError("поле не найдено")
    cursor[leaf] = value


def apply_setting(path: str, value) -> dict:
    rule = RULES.get(path)
    if not rule:
        raise ValueError("это поле отсюда не меняется")
    checked = _check_value(rule, value)
    target = os.path.join(SETTINGS_DIR, "config.json")
    with LOCK:
        config = _read_json(target)
        _set_path(config, path, checked)
        _write_json(target, config)
    return snapshot()


def apply_message(message_id: str, enabled, text: str) -> dict:
    if message_id not in MESSAGE_IDS:
        raise ValueError("неизвестное сообщение")
    if not isinstance(enabled, bool):
        raise ValueError("нужен переключатель")
    if not isinstance(text, str):
        raise ValueError("нужен текст")
    if len(text) > 4000:
        raise ValueError("слишком длинный текст")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(lines) > 40:
        raise ValueError("слишком много строк")
    target = os.path.join(SETTINGS_DIR, "messages.json")
    with LOCK:
        messages = _read_json(target)
        if message_id not in messages or not isinstance(messages[message_id], dict):
            raise ValueError("сообщение не найдено")
        messages[message_id]["enabled"] = enabled
        messages[message_id]["text"] = lines
        _write_json(target, messages)
    return snapshot()


def apply_list(kind: str, included_ids, excluded_ids) -> dict:
    filename = LIST_FILES.get(kind)
    if not filename:
        raise ValueError("неизвестный список")
    if not isinstance(included_ids, list) or not isinstance(excluded_ids, list):
        raise ValueError("нужен список товаров")
    if len(included_ids) > 200 or len(excluded_ids) > 200:
        raise ValueError("слишком много товаров")
    known = _known_items()
    target = os.path.join(SETTINGS_DIR, filename)

    def resolve(ids: list) -> list[dict]:
        resolved = []
        seen = set()
        for item_id in ids:
            if not isinstance(item_id, str) or item_id not in known or item_id in seen:
                raise ValueError("в списке есть неизвестный товар")
            seen.add(item_id)
            source = known[item_id]
            resolved.append({
                "id": source["id"],
                "slug": source["slug"],
                "name": source["name"],
            })
        return resolved

    included = resolve(included_ids)
    excluded = resolve(excluded_ids)
    with LOCK:
        payload = _read_json(target)
        if not isinstance(payload, dict):
            payload = {"included": [], "excluded": []}
        next_included = _phrase_only(payload.get("included"))
        next_excluded = _phrase_only(payload.get("excluded"))
        if included:
            next_included.append({"items": included})
        if excluded:
            next_excluded.append({"items": excluded})
        payload["included"] = next_included
        payload["excluded"] = next_excluded
        _write_json(target, payload)
    return snapshot()


def _empty_pool(role: str = "primary") -> dict:
    return {
        "enabled": True,
        "role": role,
        "title": "Дорогие" if role == "reserve" else "Дешёвые",
        "items": [],
        "active_login": "",
        "pending": None,
        "accounts": [],
    }


def _read_stock() -> dict:
    path = os.path.join(SETTINGS_DIR, STOCK_FILE)
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError):
        payload = {"need_reserve": False, "pools": []}
    if not isinstance(payload, dict):
        payload = {"need_reserve": False, "pools": []}
    pools = payload.get("pools")
    if not isinstance(pools, list):
        pools = []
    payload["need_reserve"] = bool(payload.get("need_reserve"))
    primary = next((pool for pool in pools if isinstance(pool, dict) and pool.get("role") == "primary"), None)
    reserve = next((pool for pool in pools if isinstance(pool, dict) and pool.get("role") == "reserve"), None)
    plain = [
        pool for pool in pools
        if isinstance(pool, dict) and pool.get("role") not in QUEUE_ROLES
    ]
    if primary is None and plain:
        primary = plain.pop(0)
        primary["role"] = "primary"
    if primary is None:
        primary = _empty_pool("primary")
    if reserve is None:
        reserve = _empty_pool("reserve")
        reserve["items"] = list(primary.get("items") or [])
    for pool, role in ((primary, "primary"), (reserve, "reserve")):
        pool["role"] = role
        pool.setdefault("enabled", True)
        pool.setdefault("title", "Дорогие" if role == "reserve" else "Дешёвые")
        pool.setdefault("items", [])
        pool.setdefault("active_login", "")
        pool.setdefault("pending", None)
        if not isinstance(pool.get("accounts"), list):
            pool["accounts"] = []
    payload["pools"] = [primary, reserve]
    return payload


def _queue(stock: dict, role: str) -> dict:
    if role not in QUEUE_ROLES:
        raise ValueError("неизвестная очередь")
    for pool in stock["pools"]:
        if pool.get("role") == role:
            return pool
    raise ValueError("очередь не найдена")


def _public_queue(pool: dict) -> dict:
    accounts = []
    for account in pool.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        status = account.get("status")
        if status not in ("free", "listed", "sold"):
            status = "free"
        accounts.append({
            "login": str(account.get("login") or ""),
            "password": str(account.get("password") or ""),
            "email": str(account.get("email") or ""),
            "email_password": str(account.get("email_password") or account.get("code") or ""),
            "mail_url": str(account.get("mail_url") or ""),
            "status": status,
            "item_id": str(account.get("item_id") or ""),
        })
    pending = pool.get("pending") if isinstance(pool.get("pending"), dict) else {}
    return {
        "role": pool.get("role") or "primary",
        "title": pool.get("title") or ("Дорогие" if pool.get("role") == "reserve" else "Дешёвые"),
        "accounts": accounts,
        "active_login": str(pool.get("active_login") or ""),
        "pending_login": str(pending.get("next_login") or ""),
    }


def _public_errors() -> list:
    try:
        from error_cases import public_cases
        return public_cases()
    except Exception:
        return []


def apply_error(body: dict) -> dict:
    action = str((body or {}).get("action") or "").strip()
    if action == "issue":
        case_id = str((body or {}).get("id") or "").strip()
        if not case_id:
            raise ValueError("не выбрана ошибка")
        from error_cases import issue_extra_account
        issue_extra_account(case_id, send_now=False)
        return snapshot()
    if action == "close":
        case_id = str((body or {}).get("id") or "").strip()
        if not case_id:
            raise ValueError("не выбрана ошибка")
        from error_cases import resolve_case
        if not resolve_case(case_id):
            raise ValueError("ошибка не найдена")
        return snapshot()
    raise ValueError("неизвестное действие")


def _public_stock() -> dict:
    stock = _read_stock()
    items = []
    for pool in stock["pools"]:
        for item in _binding_items(pool):
            if item["id"] not in items:
                items.append(item["id"])
    return {
        "items": items,
        "need_reserve": bool(stock.get("need_reserve")),
        "queues": [_public_queue(pool) for pool in stock["pools"]],
    }


def _account_text(value, key: str, required: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError("нужен текст")
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    labels = {
        "login": "логин",
        "password": "пароль",
        "email": "почта",
        "email_password": "пароль почты",
        "mail_url": "сайт почты",
    }
    if required and not text:
        raise ValueError(f"заполните {labels[key]}")
    if len(text) > ACCOUNT_LIMITS[key]:
        raise ValueError(f"{labels[key]} слишком длинный")
    return text


def _same_login(left: str, right: str) -> bool:
    a = (left or "").strip()
    b = (right or "").strip()
    return bool(a) and a.casefold() == b.casefold()


def _account_at(stock: dict, role: str, index) -> tuple[dict, list, dict]:
    pool = _queue(stock, role)
    accounts = pool["accounts"]
    if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= len(accounts):
        raise ValueError("аккаунт не найден")
    account = accounts[index]
    if not isinstance(account, dict):
        raise ValueError("аккаунт не найден")
    return pool, accounts, account


def _refresh_marks(stock: dict) -> None:
    for pool in stock["pools"]:
        listed = [
            account for account in (pool.get("accounts") or [])
            if isinstance(account, dict) and account.get("status") == "listed"
        ]
        if not listed:
            pool["active_login"] = ""
            continue
        current = str(pool.get("active_login") or "")
        match = next(
            (account for account in listed if _same_login(str(account.get("login") or ""), current)),
            listed[0],
        )
        pool["active_login"] = str(match.get("login") or "")


def _pending_ok(stock: dict, pending: dict) -> bool:
    login = str(pending.get("next_login") or "")
    lot = str(pending.get("item_id") or "")
    if not login:
        return False
    for pool in stock["pools"]:
        for account in pool.get("accounts") or []:
            if not isinstance(account, dict) or account.get("status") == "sold":
                continue
            if not _same_login(str(account.get("login") or ""), login):
                continue
            bound = str(account.get("item_id") or "")
            if lot and bound and bound != lot:
                return False
            return True
    return False


def _drop_broken_pending(stock: dict) -> None:
    for pool in stock["pools"]:
        pending = pool.get("pending") if isinstance(pool.get("pending"), dict) else None
        if pending and not _pending_ok(stock, pending):
            pool["pending"] = None
        by_item = pool.get("pending_by_item")
        if isinstance(by_item, dict):
            for key in list(by_item):
                entry = by_item.get(key)
                if not isinstance(entry, dict) or not _pending_ok(stock, entry):
                    by_item.pop(key, None)


def _rename_one_pending(pending: dict, old: str, new: str) -> None:
    if _same_login(str(pending.get("next_login") or ""), old):
        pending["next_login"] = new
    if _same_login(str(pending.get("sold_login") or ""), old):
        pending["sold_login"] = new


def _rename_pending(stock: dict, old: str, new: str) -> None:
    for pool in stock["pools"]:
        pending = pool.get("pending") if isinstance(pool.get("pending"), dict) else None
        if pending:
            _rename_one_pending(pending, old, new)
        by_item = pool.get("pending_by_item")
        if isinstance(by_item, dict):
            for entry in by_item.values():
                if isinstance(entry, dict):
                    _rename_one_pending(entry, old, new)


def _mark_listed(stock: dict, role: str, index: int) -> None:
    target = None
    for pool in stock["pools"]:
        if pool.get("role") != role:
            continue
        accounts = pool.get("accounts") or []
        if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(accounts):
            if isinstance(accounts[index], dict):
                target = accounts[index]
        break
    if target is None:
        raise ValueError("аккаунт не найден")
    lot = str(target.get("item_id") or "")
    for pool in stock["pools"]:
        for account in pool.get("accounts") or []:
            if not isinstance(account, dict):
                continue
            if account is target:
                account["status"] = "listed"
            elif account.get("status") == "listed" and str(account.get("item_id") or "") == lot:
                account["status"] = "sold"
    _refresh_marks(stock)
    _drop_broken_pending(stock)


def _login_taken(stock: dict, login: str, role: str, index: int) -> bool:
    for pool in stock["pools"]:
        for position, account in enumerate(pool.get("accounts") or []):
            if pool.get("role") == role and position == index:
                continue
            if isinstance(account, dict) and _same_login(login, str(account.get("login") or "")):
                return True
    return False


def apply_stock(body: dict) -> dict:
    action = body.get("action")
    path = os.path.join(SETTINGS_DIR, STOCK_FILE)
    with LOCK:
        stock = _read_stock()
        role = body.get("role") or "primary"
        if action == "bind":
            ids = body.get("items")
            if not isinstance(ids, list) or len(ids) > 50:
                raise ValueError("нужен список товаров")
            known = _known_items()
            resolved = []
            seen = set()
            for item_id in ids:
                if not isinstance(item_id, str) or item_id not in known or item_id in seen:
                    raise ValueError("в списке есть неизвестный товар")
                seen.add(item_id)
                source = known[item_id]
                resolved.append({
                    "id": source["id"],
                    "slug": source["slug"],
                    "name": source["name"],
                })
            for pool in stock["pools"]:
                pool["items"] = [dict(item) for item in resolved]
        elif action == "add":
            pool = _queue(stock, role)
            accounts = pool["accounts"]
            if len(accounts) >= 100:
                raise ValueError("в очереди уже 100 аккаунтов")
            row = {
                key: _account_text(body.get(key), key, key in ACCOUNT_REQUIRED)
                for key in ACCOUNT_FIELDS
            }
            for other in stock["pools"]:
                if any(
                    _same_login(row["login"], str(account.get("login") or ""))
                    for account in (other.get("accounts") or [])
                    if isinstance(account, dict)
                ):
                    raise ValueError("такой логин уже есть в очереди")
            row["status"] = "free"
            accounts.append(row)
        elif action == "edit":
            pool, accounts, account = _account_at(stock, role, body.get("index"))
            row = {
                key: _account_text(body.get(key), key, key in ACCOUNT_REQUIRED)
                for key in ACCOUNT_FIELDS
            }
            if _login_taken(stock, row["login"], role, body.get("index")):
                raise ValueError("такой логин уже есть в очереди")
            old_login = str(account.get("login") or "")
            for key in ACCOUNT_FIELDS:
                account[key] = row[key]
            _rename_pending(stock, old_login, row["login"])
            if account.get("status") == "listed":
                _mark_listed(stock, role, body.get("index"))
            else:
                _refresh_marks(stock)
                _drop_broken_pending(stock)
        elif action == "status":
            _pool, _accounts, account = _account_at(stock, role, body.get("index"))
            status = body.get("status")
            if status not in ("free", "listed", "sold"):
                raise ValueError("неизвестная отметка")
            if status == "listed":
                _mark_listed(stock, role, body.get("index"))
            else:
                account["status"] = status
                _refresh_marks(stock)
                _drop_broken_pending(stock)
        elif action == "transfer":
            pool, accounts, account = _account_at(stock, role, body.get("index"))
            target_role = "reserve" if role == "primary" else "primary"
            target = _queue(stock, target_role)
            if len(target["accounts"]) >= 100:
                raise ValueError("в очереди уже 100 аккаунтов")
            login = str(account.get("login") or "")
            accounts.pop(body.get("index"))
            target["accounts"].append(account)
            pending = pool.get("pending") if isinstance(pool.get("pending"), dict) else None
            if pending and _same_login(login, str(pending.get("next_login") or "")):
                pool["pending"] = None
            _refresh_marks(stock)
            _drop_broken_pending(stock)
        elif action == "clear_pending":
            pool = _queue(stock, role)
            pool["pending"] = None
            pool["pending_by_item"] = {}
        elif action == "lot":
            _pool, _accounts, account = _account_at(stock, role, body.get("index"))
            raw = body.get("item_id")
            if raw in ("", None):
                account["item_id"] = ""
            elif not isinstance(raw, str) or raw.strip() not in _known_items():
                raise ValueError("неизвестный товар")
            else:
                account["item_id"] = raw.strip()
            if account.get("status") == "listed":
                _mark_listed(stock, role, body.get("index"))
            else:
                _drop_broken_pending(stock)
        elif action == "reorder":
            pool = _queue(stock, role)
            accounts = pool["accounts"]
            index = body.get("index")
            dest = body.get("to")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= len(accounts):
                raise ValueError("аккаунт не найден")
            if isinstance(dest, bool) or not isinstance(dest, int) or dest < 0 or dest >= len(accounts):
                raise ValueError("некуда двигать")
            if index != dest:
                account = accounts.pop(index)
                accounts.insert(dest, account)
        elif action == "reserve":
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("нужен переключатель")
            stock["need_reserve"] = enabled
            for pool in stock["pools"]:
                if pool.get("role") == "primary":
                    pool["need_reserve"] = enabled
        elif action == "delete":
            pool, accounts, account = _account_at(stock, role, body.get("index"))
            accounts.pop(body.get("index"))
            _refresh_marks(stock)
            _drop_broken_pending(stock)
        elif action == "move":
            pool = _queue(stock, role)
            accounts = pool["accounts"]
            index = body.get("index")
            direction = body.get("direction")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= len(accounts):
                raise ValueError("аккаунт не найден")
            if direction not in (-1, 1):
                raise ValueError("некуда двигать")
            target = index + direction
            if target < 0 or target >= len(accounts):
                raise ValueError("некуда двигать")
            accounts[index], accounts[target] = accounts[target], accounts[index]
        else:
            raise ValueError("неизвестное действие")
        _write_json(path, stock)
    return snapshot()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            raise ValueError("пустой или слишком большой запрос")
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("нужен объект")
        return data

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            try:
                self._json(200, snapshot())
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            return
        spec = FILES.get(parsed.path)
        if not spec:
            self._json(404, {"error": "не найдено"})
            return
        try:
            with open(os.path.join(STATIC_DIR, spec[0]), "rb") as handle:
                self._send(200, handle.read(), spec[1])
        except OSError:
            self._json(404, {"error": "файл не найден"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            body = self._read_body()
            if parsed.path == "/api/setting":
                payload = apply_setting(body.get("path"), body.get("value"))
            elif parsed.path == "/api/message":
                payload = apply_message(body.get("id"), body.get("enabled"), body.get("text"))
            elif parsed.path == "/api/list":
                payload = apply_list(body.get("kind"), body.get("included"), body.get("excluded"))
            elif parsed.path == "/api/stock":
                payload = apply_stock(body)
            elif parsed.path == "/api/errors":
                payload = apply_error(body)
            elif parsed.path == "/api/snipe/resolve":
                payload = resolve_snipe_seller(body.get("query") or body.get("url") or "")
            elif parsed.path == "/api/snipe/my-items":
                payload = {
                    "my_items": fetch_my_active_items(force=bool(body.get("force"))),
                }
            elif parsed.path == "/api/snipe/targets":
                payload = apply_snipe_targets(body)
            elif parsed.path == "/api/greetings":
                payload = apply_greetings(body)
            else:
                self._json(404, {"error": "не найдено"})
                return
            self._json(200, payload)
        except (ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": str(exc)})


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Пульт Playerok: http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
