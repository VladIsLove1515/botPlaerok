"""Активные ошибки покупателей и выдача доп. аккаунта из дорогой очереди."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(ROOT, "bot_data", "error_cases.json")
STOCK_PATH = os.path.join(ROOT, "bot_settings", "account_stock.json")
LOCK = threading.Lock()

DEFAULT = {"cases": [], "pending_gifts": []}


def _read(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(default, dict) and isinstance(payload, dict):
            return payload
        if isinstance(default, list) and isinstance(payload, list):
            return payload
        return default
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def _write(path: str, payload) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, delete=False
    ) as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=4)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        temporary = tmp.name
    os.replace(temporary, path)


def load_cases() -> dict:
    payload = _read(PATH, DEFAULT)
    if not isinstance(payload, dict):
        payload = {"cases": [], "pending_gifts": []}
    if not isinstance(payload.get("cases"), list):
        payload["cases"] = []
    if not isinstance(payload.get("pending_gifts"), list):
        payload["pending_gifts"] = []
    return payload


def save_cases(payload: dict) -> None:
    _write(PATH, payload)


def open_cases() -> list[dict]:
    return [
        case for case in load_cases().get("cases") or []
        if isinstance(case, dict) and case.get("status") == "open"
    ]


def find_case(case_id: str) -> dict | None:
    case_id = str(case_id or "").strip()
    if not case_id:
        return None
    for case in load_cases().get("cases") or []:
        if isinstance(case, dict) and str(case.get("id") or "") == case_id:
            return case
    return None


def register_error(
    username: str,
    chat_id: str,
    *,
    deal_id: str = "",
    item_id: str = "",
    item_name: str = "",
) -> dict:
    username = str(username or "").strip() or "без ника"
    chat_id = str(chat_id or "").strip()
    if not chat_id:
        raise ValueError("нет чата")
    with LOCK:
        payload = load_cases()
        for case in payload["cases"]:
            if (
                isinstance(case, dict)
                and case.get("status") == "open"
                and str(case.get("chat_id") or "") == chat_id
            ):
                case["username"] = username
                if deal_id:
                    case["deal_id"] = deal_id
                if item_id:
                    case["item_id"] = item_id
                if item_name:
                    case["item_name"] = item_name
                case["updated_at"] = datetime.now().isoformat(timespec="seconds")
                save_cases(payload)
                return dict(case)
        case = {
            "id": uuid.uuid4().hex[:12],
            "username": username,
            "chat_id": chat_id,
            "deal_id": str(deal_id or ""),
            "item_id": str(item_id or ""),
            "item_name": str(item_name or ""),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "open",
            "issued_login": "",
        }
        payload["cases"].insert(0, case)
        save_cases(payload)
        return dict(case)


def resolve_case(case_id: str, issued_login: str = "") -> dict | None:
    with LOCK:
        payload = load_cases()
        for case in payload["cases"]:
            if not isinstance(case, dict) or str(case.get("id") or "") != str(case_id):
                continue
            case["status"] = "resolved"
            case["updated_at"] = datetime.now().isoformat(timespec="seconds")
            if issued_login:
                case["issued_login"] = issued_login
            save_cases(payload)
            return dict(case)
    return None


def format_account_message(account: dict) -> str:
    row = account or {}
    lines = [
        f"Логин: {str(row.get('login') or '').strip()}",
        f"Пароль: {str(row.get('password') or '').strip()}",
        f"Почта: {str(row.get('email') or '').strip()}",
        f"Пароль почты: {str(row.get('email_password') or row.get('code') or '').strip()}",
    ]
    mail_url = str(row.get("mail_url") or "").strip()
    if mail_url:
        lines.append(f"Сайт почты: {mail_url}")
    lines.append("")
    lines.append("Это дополнительный аккаунт по вашей ошибке.")
    return "\n".join(lines)


def _take_reserve_account() -> dict:
    stock = _read(STOCK_PATH, {"need_reserve": False, "pools": []})
    if not isinstance(stock, dict):
        raise ValueError("очередь аккаунтов повреждена")
    pools = stock.get("pools")
    if not isinstance(pools, list):
        raise ValueError("нет очередей аккаунтов")
    reserve = next(
        (
            pool for index, pool in enumerate(pools)
            if isinstance(pool, dict) and (
                str(pool.get("role") or "") == "reserve"
                or (not pool.get("role") and index == 1)
            )
        ),
        None,
    )
    if not isinstance(reserve, dict):
        raise ValueError("дорогая очередь не найдена")
    accounts = reserve.get("accounts")
    if not isinstance(accounts, list):
        raise ValueError("в дорогой очереди нет аккаунтов")
    chosen = next(
        (
            row for row in accounts
            if isinstance(row, dict)
            and (row.get("status") or "free") == "free"
            and str(row.get("login") or "").strip()
        ),
        None,
    )
    if not chosen:
        raise ValueError("дорогие аккаунты закончились")
    chosen["status"] = "sold"
    chosen["item_id"] = ""
    stock["need_reserve"] = False
    for pool in pools:
        if isinstance(pool, dict):
            pool["need_reserve"] = False
    _write(STOCK_PATH, stock)
    return {
        "login": str(chosen.get("login") or "").strip(),
        "password": str(chosen.get("password") or ""),
        "email": str(chosen.get("email") or ""),
        "email_password": str(chosen.get("email_password") or chosen.get("code") or ""),
        "mail_url": str(chosen.get("mail_url") or ""),
    }


def pop_pending_gifts() -> list[dict]:
    with LOCK:
        payload = load_cases()
        gifts = [gift for gift in payload.get("pending_gifts") or [] if isinstance(gift, dict)]
        if not gifts:
            return []
        payload["pending_gifts"] = []
        save_cases(payload)
        return gifts


def issue_extra_account(case_id: str, *, send_now: bool = False) -> dict:
    """Берёт аккаунт из дорогой очереди и отдаёт покупателю."""
    case = find_case(case_id)
    if not case or case.get("status") != "open":
        raise ValueError("ошибка не найдена или уже закрыта")
    chat_id = str(case.get("chat_id") or "").strip()
    if not chat_id:
        raise ValueError("у ошибки нет чата")

    with LOCK:
        account = _take_reserve_account()
        text = format_account_message(account)
        payload = load_cases()
        gift = {
            "id": uuid.uuid4().hex[:10],
            "case_id": str(case_id),
            "chat_id": chat_id,
            "text": text,
            "login": account["login"],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        for row in payload["cases"]:
            if isinstance(row, dict) and str(row.get("id") or "") == str(case_id):
                row["status"] = "resolved"
                row["issued_login"] = account["login"]
                row["updated_at"] = datetime.now().isoformat(timespec="seconds")
                break
        payload["pending_gifts"].append(gift)
        save_cases(payload)

    result = {
        "case_id": case_id,
        "chat_id": chat_id,
        "username": case.get("username") or "",
        "login": account["login"],
        "text": text,
        "gift_id": gift["id"],
        "sent": False,
    }
    if not send_now:
        return result

    try:
        from plbot.playerokbot import get_playerok_bot
        bot = get_playerok_bot()
        if bot is None:
            raise RuntimeError("бот Playerok не запущен")
        mess = bot.send_message(chat_id, text)
        result["sent"] = bool(mess)
        if result["sent"]:
            with LOCK:
                payload = load_cases()
                payload["pending_gifts"] = [
                    row for row in payload.get("pending_gifts") or []
                    if not (isinstance(row, dict) and row.get("id") == gift["id"])
                ]
                save_cases(payload)
    except Exception as exc:
        result["send_error"] = str(exc)
    return result


def public_cases() -> list[dict]:
    rows = []
    for case in open_cases():
        rows.append({
            "id": str(case.get("id") or ""),
            "username": str(case.get("username") or ""),
            "chat_id": str(case.get("chat_id") or ""),
            "deal_id": str(case.get("deal_id") or ""),
            "item_id": str(case.get("item_id") or ""),
            "item_name": str(case.get("item_name") or ""),
            "created_at": str(case.get("created_at") or ""),
            "status": "open",
        })
    return rows
