from __future__ import annotations
import sys
import os
import json
import asyncio
import time
from datetime import datetime, timedelta
import pytz
from threading import Thread, RLock
import textwrap
import shutil
import copy
from colorama import Fore

from playerokapi.account import Account
from playerokapi.enums import *
from playerokapi.types import *
from playerokapi.exceptions import *
from playerokapi.listener.events import *
from playerokapi.listener.listener import EventListener
from playerokapi.types import Chat, Item

from __init__ import ACCENT_COLOR, VERSION
from core.utils import (
    set_title, 
    shutdown, 
    run_async_in_thread, 
    acquire_account_lock
)
from core.handlers import (
    add_bot_event_handler, 
    add_playerok_event_handler, 
    call_bot_event, 
    call_playerok_event
)
from settings import DATA, Settings as sett
from logging import getLogger
from data import Data as data
from utils import (
    get_current_bump_interval,
    get_current_bump_position,
    item_matches_binding,
    item_matches_any_binding,
    binding_key,
    rebind_item,
    escape_html
)
from .bump_schedule import next_slot_time, parse_stamp, pick_staggered_item
from tgbot.telegrambot import (
    get_telegram_bot, 
    get_telegram_bot_loop
)
from tgbot.templates import (
    log_text, 
    log_new_mess_kb, 
    log_new_deal_kb,
    log_new_problem_kb,
    log_item_kb,
    log_deal_kb,
    log_transaction_kb,
    destroy_kb
)

from .msg_types import (
    msg_account,
    msg_user_from_chat,
    msg_item,
    msg_deal,
    msg_chat
)


logger = getLogger("universal.playerok")

_STOCK_MARK = "account_stock"
_STOCK_OUT = "аккаунты закончились, лот не выставлен"
_STOCK_RESERVE_OUT = "резервные аккаунты закончились, лот не выставлен"


def stock_field_key(label: str) -> str | None:
    text = (label or "").strip().lower()
    if not text:
        return None
    # одно поле «данные для входа» со всеми кредами сразу (Steam и т.п.)
    loginish = any(needle in text for needle in ("логин", "login", "username"))
    passish = "парол" in text or "password" in text
    mailish = any(needle in text for needle in ("почт", "email", "e-mail", "mail"))
    mixed = passish or mailish or "код" in text or "code" in text or "инструк" in text
    if "данные для входа" in text or (loginish and mixed):
        return "credentials"
    if loginish:
        return "login"
    if passish and mailish:
        return "email_password"
    if passish:
        return "password"
    if mailish:
        return "email"
    if "код" in text or "code" in text:
        return "email_password"
    if "инструк" in text:
        return "instruction"
    if "комментар" in text or "comment" in text:
        # у Steam это текст выдачи, не почта — не перезаписываем из стока
        return "keep"
    if "информац" in text or "info" in text:
        return "info"
    return None


def same_login(left: str, right: str) -> bool:
    a = (left or "").strip()
    b = (right or "").strip()
    return bool(a) and a.casefold() == b.casefold()


def account_login(account: dict) -> str:
    return str((account or {}).get("login") or "").strip()


def pick_stock_account(accounts: list, pending, active_login: str, current_login: str) -> tuple[dict, str, bool]:
    rows = [row for row in (accounts or []) if isinstance(row, dict)]
    pending = pending if isinstance(pending, dict) else None
    if pending and str(pending.get("next_login") or "").strip():
        next_login = str(pending.get("next_login")).strip()
        chosen = next((
            row for row in rows
            if same_login(account_login(row), next_login) and row.get("status") != "sold"
        ), None)
        if not chosen:
            raise Exception("аккаунт из незавершённой выдачи пропал из очереди")
        return chosen, str(pending.get("sold_login") or "").strip(), True

    sold_login = (active_login or "").strip() or (current_login or "").strip()
    chosen = next((
        row for row in rows
        if (row.get("status") or "free") == "free" and not same_login(account_login(row), sold_login)
    ), None)
    if not chosen:
        raise Exception(_STOCK_OUT)
    if not account_login(chosen):
        raise Exception("в очереди есть аккаунт без логина")
    return chosen, sold_login, False


def stock_credentials_blob(account: dict) -> str:
    row = account or {}
    parts = [
        account_login(row),
        str(row.get("password") or "").strip(),
        str(row.get("email") or "").strip(),
        str(row.get("email_password") or row.get("code") or "").strip(),
    ]
    mail_url = str(row.get("mail_url") or "").strip()
    if mail_url:
        parts.append(mail_url)
    return "----".join(parts)


def stock_login_from_value(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for sep in ("----", ":", ";", "|", "\n"):
        if sep in text:
            return text.split(sep, 1)[0].strip()
    return text


def stock_account_value(account: dict, key: str) -> str:
    row = account or {}
    email = str(row.get("email") or "").strip()
    email_password = str(row.get("email_password") or row.get("code") or "").strip()
    mail_url = str(row.get("mail_url") or "").strip()
    if key == "credentials":
        return stock_credentials_blob(row)
    if key == "login":
        return account_login(row)
    if key == "password":
        return str(row.get("password") or "")
    if key in ("email_password", "code"):
        return email_password
    if key in ("email", "comment"):
        return email
    if key == "instruction":
        lines = [f"Почта: {email}", f"Пароль почты: {email_password}"]
        if mail_url:
            lines.append(f"Сайт: {mail_url}")
        return "\n".join(lines)
    if key == "info":
        return mail_url or email
    return str(row.get(key) or "")


def _pool_role(pool: dict, index: int) -> str:
    role = str((pool or {}).get("role") or "").strip()
    if role in ("primary", "reserve"):
        return role
    return "primary" if index == 0 else "reserve"


def _role_indexes(pools: list) -> tuple[int | None, int | None]:
    primary = None
    reserve = None
    for index, pool in enumerate(pools or []):
        if not isinstance(pool, dict):
            continue
        role = _pool_role(pool, index)
        if role == "reserve" and reserve is None:
            reserve = index
        elif role == "primary" and primary is None:
            primary = index
    return primary, reserve


def _listed_login(pools: list) -> str:
    for pool in pools or []:
        if not isinstance(pool, dict):
            continue
        login = str(pool.get("active_login") or "").strip()
        if login:
            return login
    return ""


def _stock_needs_reserve(stock: dict, pools: list, primary_i: int | None) -> bool:
    if isinstance(stock, dict) and stock.get("need_reserve"):
        return True
    if primary_i is None:
        return False
    pool = pools[primary_i]
    return bool(isinstance(pool, dict) and pool.get("need_reserve"))


def _account_item_id(account: dict) -> str:
    return str((account or {}).get("item_id") or "").strip()


def _accounts_use_lots(pools: list) -> bool:
    for pool in pools or []:
        if not isinstance(pool, dict):
            continue
        for row in pool.get("accounts") or []:
            if isinstance(row, dict) and _account_item_id(row):
                return True
    return False


def _item_identity(item) -> str:
    return str(getattr(item, "id", "") or "").strip()


def _pool_has_item_account(pool: dict, item_id: str) -> bool:
    if not item_id or not isinstance(pool, dict):
        return False
    return any(
        isinstance(row, dict) and _account_item_id(row) == item_id
        for row in (pool.get("accounts") or [])
    )


def _stock_handles_item(pools: list, item) -> bool:
    item_id = _item_identity(item)
    for pool in pools or []:
        if not isinstance(pool, dict) or pool.get("enabled", True) is False:
            continue
        if item_matches_binding(item, pool) or _pool_has_item_account(pool, item_id):
            return True
    return False


def _listed_login_for_item(pools: list, item_id: str) -> str:
    if not item_id:
        return ""
    for pool in pools or []:
        if not isinstance(pool, dict):
            continue
        for row in pool.get("accounts") or []:
            if (
                isinstance(row, dict)
                and row.get("status") == "listed"
                and _account_item_id(row) == item_id
            ):
                return account_login(row)
    return ""


def _rows_for_item(pool: dict, item_id: str) -> list:
    return [
        row for row in ((pool or {}).get("accounts") or [])
        if isinstance(row, dict) and _account_item_id(row) == item_id
    ]


def _pending_for_item(pool: dict, item_id: str):
    if not isinstance(pool, dict) or not item_id:
        return None
    by_item = pool.get("pending_by_item")
    if isinstance(by_item, dict):
        entry = by_item.get(item_id)
        if isinstance(entry, dict) and str(entry.get("next_login") or "").strip():
            return entry
    pending = pool.get("pending") if isinstance(pool.get("pending"), dict) else None
    if not pending or not str(pending.get("next_login") or "").strip():
        return None
    if str(pending.get("item_id") or "").strip() == item_id:
        return pending
    return None


def _select_lot_issue(stock: dict, pools: list, current_login: str, item_id: str) -> tuple[int, dict, str, bool]:
    """Общая очередь: обычный и премиум берут следующий свободный аккаунт по порядку."""
    primary_i, reserve_i = _role_indexes(pools)
    # живые данные лота важнее стока: после офлайн-продажи item_id в JSON часто «уплывает»
    listed_stock = _listed_login_for_item(pools, item_id)
    current = (current_login or "").strip()
    if current and listed_stock and not same_login(current, listed_stock):
        listed = current
    else:
        listed = current or listed_stock

    for index, pool in enumerate(pools):
        if not isinstance(pool, dict):
            continue
        pending = _pending_for_item(pool, item_id)
        if not pending:
            continue
        chosen, sold_login, reused = pick_stock_account(
            pool.get("accounts"),
            pending,
            listed,
            current_login,
        )
        return index, chosen, sold_login, reused

    if _stock_needs_reserve(stock, pools, primary_i):
        if reserve_i is None:
            raise Exception(_STOCK_RESERVE_OUT)
        try:
            chosen, sold_login, reused = pick_stock_account(
                pools[reserve_i].get("accounts"),
                None,
                listed,
                current_login,
            )
        except Exception as exc:
            if str(exc) == _STOCK_OUT:
                raise Exception(_STOCK_RESERVE_OUT) from None
            raise
        return reserve_i, chosen, sold_login, reused

    if primary_i is None:
        raise Exception(_STOCK_OUT)
    chosen, sold_login, reused = pick_stock_account(
        pools[primary_i].get("accounts"),
        None,
        listed,
        current_login,
    )
    return primary_i, chosen, sold_login, reused


def select_stock_issue(stock: dict, current_login: str = "", item_id: str = "") -> tuple[int, dict, str, bool]:
    pools = stock.get("pools") if isinstance(stock, dict) else None
    if not isinstance(pools, list):
        pools = []
    item_id = str(item_id or "").strip()
    if item_id and _accounts_use_lots(pools):
        return _select_lot_issue(stock, pools, current_login, item_id)
    primary_i, reserve_i = _role_indexes(pools)

    for index, pool in enumerate(pools):
        if not isinstance(pool, dict):
            continue
        pending = pool.get("pending")
        if isinstance(pending, dict) and str(pending.get("next_login") or "").strip():
            chosen, sold_login, reused = pick_stock_account(
                pool.get("accounts"),
                pending,
                str(pool.get("active_login") or ""),
                current_login,
            )
            return index, chosen, sold_login, reused

    listed = _listed_login(pools)
    if _stock_needs_reserve(stock, pools, primary_i):
        if reserve_i is None:
            raise Exception(_STOCK_RESERVE_OUT)
        try:
            chosen, sold_login, reused = pick_stock_account(
                pools[reserve_i].get("accounts"),
                None,
                listed,
                current_login,
            )
        except Exception as exc:
            if str(exc) == _STOCK_OUT:
                raise Exception(_STOCK_RESERVE_OUT) from None
            raise
        return reserve_i, chosen, sold_login, reused

    if primary_i is None:
        raise Exception(_STOCK_OUT)
    chosen, sold_login, reused = pick_stock_account(
        pools[primary_i].get("accounts"),
        None,
        listed or str(pools[primary_i].get("active_login") or ""),
        current_login,
    )
    return primary_i, chosen, sold_login, reused


def data_replaced_console(replaced) -> str:
    if not replaced:
        return ""
    if len(replaced) > 2 and replaced[2]:
        return (
            f"{Fore.WHITE}, выставлен аккаунт "
            f"{Fore.LIGHTWHITE_EX}{replaced[2]} "
            f"{Fore.WHITE}(осталось: {Fore.LIGHTWHITE_EX}{replaced[1]}{Fore.WHITE})"
        )
    return (
        f"{Fore.WHITE}, данные заменены "
        f"{Fore.WHITE}(осталось: {Fore.LIGHTWHITE_EX}{replaced[1]}{Fore.WHITE})"
    )


def data_replaced_tg(replaced) -> str:
    if not replaced:
        return ""
    if len(replaced) > 2 and replaced[2]:
        login = escape_html(str(replaced[2]))
        return f"🔄 Выставлен аккаунт <code>{login}</code>\n💽 Осталось аккаунтов: {replaced[1]}"
    return f"🔄 Данные товара заменены\n💽 Осталось данных: {replaced[1]}"


def get_playerok_bot() -> PlayerokBot | None:
    if hasattr(PlayerokBot, "instance"):
        return getattr(PlayerokBot, "instance")


class PlayerokBot:
    def __new__(cls, *args, **kwargs) -> PlayerokBot:
        if not hasattr(cls, "instance"):
            cls.instance = super(PlayerokBot, cls).__new__(cls)
        return getattr(cls, "instance")

    def __init__(self):
        self.config = sett.get("config")
        self.messages = sett.get("messages")
        self.custom_commands = sett.get("custom_commands")
        self.auto_deliveries = sett.get("auto_deliveries")
        self.auto_restore_items = sett.get("auto_restore_items")
        self.auto_complete_deals = sett.get("auto_complete_deals")
        self.auto_bump_items = sett.get("auto_bump_items")
        self.snipe_targets = sett.get("snipe_targets") or {"sellers": []}
        self.greetings = sett.get("greetings") or {"items": []}
        self.data_replacement = sett.get("data_replacement")

        self.initialized_users = data.get("initialized_users")
        self.saved_items = data.get("saved_items")
        self.cached_orders = data.get("cached_orders")
        self.bumped_items = data.get("bumped_items")
        self.snipe_watch = data.get("snipe_watch") or {"links": {}}
        self.snipe_journal_lock = RLock()

        self.item_positions: dict[str, dict] = {}
        self.recreated_items: set[str] = set()
        self.bump_lock = RLock()
        self.snipe_lock = RLock()
        self._bump_cache_at = None
        self._bump_cache_items: list = []
        self._bump_roster_ids: list[str] = []
        self._bump_empty_logged = False
        self._bump_twins_warned_at = None
        self._bumping_ids: set[str] = set()
        self._bump_fail_until: dict[str, datetime] = {}
        self._snipe_seller_cache: dict[str, tuple[float, list]] = {}

        self.account = self.playerok_account = Account(
            cookies=self.config["playerok"]["api"]["cookies"],
            user_agent=self.config["playerok"]["api"]["user_agent"],
            proxy=self.config["playerok"]["api"]["proxy"] or None,
            requests_timeout=self.config["playerok"]["api"]["requests_timeout"]
        ).get()

        self.__saved_chats: dict[str, Chat] = {}

    def get_chat_by_id(self, chat_id: str) -> Chat:
        if chat_id in self.__saved_chats:
            return self.__saved_chats[chat_id]
        self.__saved_chats[chat_id] = self.account.get_chat(chat_id)
        return self.get_chat_by_id(chat_id)

    def get_chat_by_username(self, username: str) -> Chat:
        if username in self.__saved_chats:
            return self.__saved_chats[username]
        if username.lower() == "поддержка":
            chat = self.account.get_chat(self.account.support_chat_id)
        elif username.lower() == "уведомления":
            chat = self.account.get_chat(self.account.system_chat_id)
        else:
            chat = self.account.get_chat_by_username(username)
        self.__saved_chats[username] = chat
        return self.get_chat_by_username(username)
    
    
    def save_account_snapshot(self, telegram_username: str | None = None):
        """Пишет баланс/ник в bot_data — дашборд читает отсюда, а не только хвост лога."""
        try:
            profile = self.account.profile
            balance = profile.balance if profile else None
            sales = ""
            purchases = ""
            if profile and profile.stats and profile.stats.deals:
                sales = str(profile.stats.deals.outgoing.total - profile.stats.deals.outgoing.finished)
                purchases = str(profile.stats.deals.incoming.total - profile.stats.deals.incoming.finished)
            payload = {
                "updated_at": time.time(),
                "username": self.account.username or "",
                "balance": f"{balance.value}₽" if balance else "",
                "available": f"{balance.available}₽" if balance else "",
                "pending": f"{balance.pending_income}₽" if balance else "",
                "frozen": f"{balance.frozen}₽" if balance else "",
                "sales": sales,
                "purchases": purchases,
                "telegram": (telegram_username or "").lstrip("@"),
            }
            path = os.path.join("bot_data", "account_snapshot.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        except Exception as e:
            logger.debug(f"Не удалось сохранить снимок аккаунта для дашборда: {e}")

    def refresh_account(self):
        try: 
            self.account = self.playerok_account = self.account.get()
            self.save_account_snapshot()
        except Exception as e:
            logger.error(f"{Fore.LIGHTRED_EX}Ошибка при обновлении аккаунта: {Fore.WHITE}{e}")

    def check_banned(self):
        try:
            user = self.account.get_user(self.account.id)
            if user.is_blocked:
                logger.critical("")
                logger.critical(f"{Fore.LIGHTRED_EX}Ваш Playerok аккаунт был заблокирован! К сожалению, я не могу продолжать работу на заблокированном аккаунте...")
                logger.critical(f"Напишите в тех. поддержку Playerok, чтобы узнать причину бана и как можно быстрее решить эту проблему.")
                logger.critical("")
                shutdown()
        except Exception as e:
            logger.error(f"{Fore.LIGHTRED_EX}Ошибка при проверке на блокировку: {Fore.WHITE}{e}")

    def _format_msg_text(self, text: str, **kwargs):

        class SafeDict(dict):
            def __missing__(self, key):
                if "." in key:
                    parts = key.split(".")
                    obj = self.get(parts[0])
                    if obj is None:
                        return "{" + key + "}"
                    try:
                        for part in parts[1:]:
                            obj = getattr(obj, part) if hasattr(obj, part) else obj[part]
                        return obj
                    except (AttributeError, KeyError, TypeError):
                        return "{" + key + "}"
                return "{" + key + "}"
            
        return text.format_map(SafeDict(**kwargs))

    def msg(self, message_name: str, messages_config_name: str = "messages", 
            messages_data: dict = DATA, **kwargs) -> str | None:
        kwargs["account"] = msg_account(self.account.profile)

        messages = sett.get(messages_config_name, messages_data) or {}
        mess = messages.get(message_name, {})
        if not mess.get("enabled"):
            return None
        
        message_lines: list[str] = mess.get("text", [])
        if not message_lines:
            return f"Сообщение {message_name} пустое"
        
        try:
            msg = self._format_msg_text("\n".join(message_lines), **kwargs)
            return msg
        except:
            pass
        
        return f"Не удалось получить сообщение {message_name}"

    def _format_greeting_text(self, lines: list[str], **kwargs) -> str | None:
        if not lines:
            return None
        kwargs = dict(kwargs)
        kwargs["account"] = msg_account(self.account.profile)
        try:
            return self._format_msg_text("\n".join(str(line) for line in lines), **kwargs)
        except Exception:
            return "\n".join(str(line) for line in lines)

    def pick_deal_greeting(self, item) -> dict | None:
        """Первое включённое приветствие, привязанное к лоту сделки."""
        greetings = sett.get("greetings") or self.greetings or {"items": []}
        self.greetings = greetings
        for entry in greetings.get("items") or []:
            if not isinstance(entry, dict) or not entry.get("enabled"):
                continue
            lots = entry.get("lots") or []
            if not lots:
                continue
            # каждый lot — объект {id,slug,name}; совместимо с binding
            binding = {"items": [lot for lot in lots if isinstance(lot, dict)]}
            if item_matches_binding(item, binding):
                return entry
        return None

    def send_deal_greeting(self, chat, deal, item, *, first_contact: bool = False) -> bool:
        """Своё приветствие на каждую сделку по лоту; иначе first_message только новым."""
        kwargs = dict(
            user=msg_user_from_chat(chat),
            chat=msg_chat(chat),
            deal=msg_deal(deal),
            item=msg_item(item),
        )
        picked = self.pick_deal_greeting(item)
        if picked is not None:
            text = self._format_greeting_text(picked.get("text") or [], **kwargs)
            if text:
                mess = self.send_message(chat.id, text)
                if mess:
                    title = str(picked.get("title") or "приветствие")[:48]
                    logger.info(
                        f"{Fore.YELLOW}Приветствие «{Fore.LIGHTYELLOW_EX}{title}{Fore.YELLOW}» "
                        f"для лота «{Fore.LIGHTYELLOW_EX}{(getattr(item, 'name', None) or '?')[:32]}»"
                    )
                return bool(mess)
            return False
        if not first_contact:
            return False
        text = self.msg("first_message", **kwargs)
        if not text:
            return False
        return bool(self.send_message(chat.id, text))
        
    def _do_call_event(self, last_time, interval):
        if not last_time:
            return True
        
        if datetime.now() >= (
            datetime.fromisoformat(last_time) 
            + timedelta(seconds=interval)
        ):
            return True
        
        return False


    def send_message(self, chat_id: str, text: str | None = None, images: list[str | bytes] = [],
                     mark_chat_as_read: bool = None, exclude_watermark: bool = False, max_attempts: int = 3) -> ChatMessage | None:
        """
        Кастомный метод отправки сообщения в чат Playerok.
        Пытается отправить за N попыток, если не удалось - выдаёт ошибку в консоль.
        """
        
        if not any((text, images)): 
            return
        
        for _ in range(max_attempts):
            try:
                read_chat_enabled = self.config["playerok"]["read_chat"]
                watermark_enabled = self.config["playerok"]["watermark"]["enabled"]
                watermark = self.config["playerok"]["watermark"]["value"]
                
                if text and watermark_enabled and watermark and not exclude_watermark:
                    text += f"\n{watermark}"
                
                mark_chat_as_read = (read_chat_enabled or False) if mark_chat_as_read is None else mark_chat_as_read
                mess = self.account.send_message(
                    chat_id=chat_id, 
                    text=text, 
                    images=images, 
                    mark_chat_as_read=mark_chat_as_read
                )
                
                return mess
            except Exception as e:
                err = e
        else:
            err = "Не удалось отправить сообщение"
        
        msg = ""
        if text: 
            msg += text.replace('\n', ' ').strip()
        if images:
            msg += f", Изображения ({len(images)})"
        
        logger.error(
            f"{Fore.LIGHTRED_EX}Ошибка при отправке сообщения «{msg}» "
            f"{Fore.LIGHTRED_EX}в чат {chat_id}{Fore.LIGHTRED_EX}: {Fore.WHITE}{err}"
        )

    def _serealize_item(self, item: ItemProfile) -> dict:
        return {
            "id": item.id,
            "slug": item.slug,
            "priority": item.priority.name if item.priority else None,
            "status": item.status.name if item.status else None,
            "name": item.name,
            "price": item.price,
            "raw_price": item.raw_price,
            "seller_type": item.seller_type.name if item.seller_type else None,
            "attachment": {
                "id": item.attachment.id,
                "url": item.attachment.url,
                "filename": item.attachment.filename,
                "mime": item.attachment.mime,
            },
            "user": {
                "id": item.user.id,
                "username": item.user.username,
                "role": item.user.role.name if item.user.role else None,
                "avatar_url": item.user.avatar_url,
                "is_online": item.user.is_online,
                "is_blocked": item.user.is_blocked,
                "rating": item.user.rating,
                "reviews_count": item.user.reviews_count,
                "support_chat_id": item.user.support_chat_id,
                "system_chat_id": item.user.system_chat_id,
                "created_at": item.user.created_at
            },
            "approval_date": item.approval_date,
            "priority_position": item.priority_position,
            "views_counter": item.views_counter,
            "fee_multiplier": item.fee_multiplier,
            "created_at": item.created_at
        }
    
    def _deserealize_item(self, item_data: dict) -> ItemProfile:
        item_data = copy.deepcopy(item_data)
        user_data = item_data.pop("user")
        user_data["role"] = UserTypes.__members__.get(user_data["role"]) if user_data["role"] else None
        user = UserProfile(**user_data)
        user.__account = self.account
        item_data["user"] = user
        
        attachment_data = item_data.pop("attachment")
        attachment = FileObject(**attachment_data)
        item_data["attachment"] = attachment

        item_data["priority"] = PriorityTypes.__members__.get(item_data["priority"]) if item_data["priority"] else None
        item_data["status"] = ItemStatuses.__members__.get(item_data["status"]) if item_data["status"] else None
        item_data["seller_type"] = UserTypes.__members__.get(item_data["seller_type"]) if item_data["seller_type"] else None

        item = ItemProfile(**item_data)
        return item

    def get_my_items(
        self, 
        statuses: list[ItemStatuses] = [ItemStatuses.APPROVED],
        game_id: str | None = None, 
        category_id: str | None = None,
        obtaining_type_id: str | None = None,
        count: int = -1
    ) -> list[ItemProfile]:
        """
        Получает все товары аккаунта и сохраняет их.
        Берёт из сохранённых товары, которые не удалось получить
        """
        
        my_items: list[ItemProfile] = []
        svd_items: list[dict] = []
        next_cursor = None
        
        try:
            while True:
                itm_list = self.account.get_my_items(
                    after_cursor=next_cursor, 
                    game_id=game_id, 
                    category_id=category_id,
                    obtaining_type_id=obtaining_type_id,
                    statuses=statuses,
                    count=count if count != -1 else 24
                )
                
                for itm in itm_list.items:
                    svd_items.append(self._serealize_item(itm))
                    my_items.append(itm)
                    
                    if len(my_items) >= count and count != -1:
                        return my_items
                
                if not itm_list.page_info.has_next_page:
                    break
                
                next_cursor = itm_list.page_info.end_cursor
                time.sleep(0.5)
            
            self.saved_items = svd_items
        except (RequestPlayerokError, RequestFailedError):
            for itm_dict in list(self.saved_items):
                itm = self._deserealize_item(itm_dict)
                
                if statuses is None or itm.status in statuses:
                    my_items.append(itm)
                    if len(my_items) >= count and count != -1:
                        return my_items
                    
            if not my_items: 
                raise
            
        return my_items

    def log_to_tg(self, text, kb=None):
        asyncio.run_coroutine_threadsafe(
            get_telegram_bot().log_event(text=text, kb=kb), 
            get_telegram_bot_loop()
        )


    def is_bump_item_matched(self, item: ItemProfile | MyItem) -> bool:
        included = item_matches_any_binding(item, self.auto_bump_items["included"])
        excluded = item_matches_any_binding(item, self.auto_bump_items["excluded"])

        return (
            self.config["playerok"]["auto_bump_items"]["all"]
            and not excluded
        ) or (
            not self.config["playerok"]["auto_bump_items"]["all"]
            and included
        )

    def _is_free_item(self, item) -> bool:
        priority = getattr(item, "priority", None)
        return priority is None or priority == PriorityTypes.DEFAULT

    def _is_premium_item(self, item) -> bool:
        return getattr(item, "priority", None) is PriorityTypes.PREMIUM

    @staticmethod
    def _lot_name_key(name: str) -> str:
        return " ".join((name or "").casefold().split())

    def _iter_my_active_items(self):
        statuses = [
            ItemStatuses.APPROVED,
            ItemStatuses.PENDING_APPROVAL,
            ItemStatuses.PENDING_MODERATION,
        ]
        seen: set[str] = set()
        for status in statuses:
            try:
                rows = self.get_my_items(statuses=[status])
            except Exception:
                continue
            for item in rows or []:
                item_id = getattr(item, "id", None)
                if item_id and item_id in seen:
                    continue
                if item_id:
                    seen.add(item_id)
                yield item

    def _count_premium_twins(self, name: str, exclude_id: str | None = None) -> int:
        """Сколько активных премиум-лотов с тем же названием (без exclude_id)."""

        key = self._lot_name_key(name)
        total = 0
        for item in self._iter_my_active_items():
            if exclude_id and getattr(item, "id", None) == exclude_id:
                continue
            if getattr(item, "priority", None) is not PriorityTypes.PREMIUM:
                continue
            if self._lot_name_key(getattr(item, "name", "") or "") == key:
                total += 1
        return total

    def _assert_premium_twin_limit(self, name: str, exclude_id: str | None = None, limit: int = 2):
        count = self._count_premium_twins(name, exclude_id=exclude_id)
        if count >= limit:
            raise Exception(
                f"уже есть {count} премиум-лота «{name}» (лимит {limit}); "
                f"новый премиум для этой позиции создавать нельзя"
            )

    def _warn_premium_twin_overflow(self, limit: int = 2):
        """Логирует группы премиум-лотов, где копий больше лимита."""

        groups: dict[str, list] = {}
        for item in self._iter_my_active_items():
            if getattr(item, "priority", None) is not PriorityTypes.PREMIUM:
                continue
            key = self._lot_name_key(getattr(item, "name", "") or "")
            if not key:
                continue
            groups.setdefault(key, []).append(item)
        for key, rows in groups.items():
            if len(rows) <= limit:
                continue
            title = getattr(rows[0], "name", key)
            logger.warning(
                f"{Fore.LIGHTYELLOW_EX}Премиум-дубли: «{title[:48]}» — {len(rows)} шт "
                f"(лимит {limit}). Лишние не создаём и не апгрейдим обычные лоты."
            )

    def _journal_mut(self, mutator):
        with self.bump_lock:
            journal = data.get("bump_journal")
            if not isinstance(journal, dict):
                journal = {}
            journal.setdefault("entries", [])
            journal.setdefault("plan", [])
            journal.setdefault("anchor", "")
            journal.setdefault("last_slot_at", "")
            journal.setdefault("slot", 0)
            mutator(journal)
            entries = journal.get("entries")
            if isinstance(entries, list) and len(entries) > 300:
                journal["entries"] = entries[-300:]
            data.set("bump_journal", journal)

    def _record_bump(self, item, kind: str, ok: bool, detail: str):
        entry = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "id": getattr(item, "id", "") or "",
            "name": getattr(item, "name", "") or "",
            "slug": getattr(item, "slug", "") or "",
            "kind": kind,
            "ok": bool(ok),
            "detail": detail,
        }

        def add(journal):
            rows = journal.get("entries")
            if not isinstance(rows, list):
                rows = []
            rows.append(entry)
            journal["entries"] = rows

        self._journal_mut(add)

    def _item_price(self, item) -> int:
        price = getattr(item, "raw_price", None)
        if price is None:
            price = getattr(item, "price", None)
        return int(price or 0)

    def _free_priority_status(self, item_id: str, price: int):
        statuses = self.account.get_item_priority_statuses(item_id, price)
        free_status = next(
            (
                status for status in statuses
                if status.type == PriorityTypes.DEFAULT and (status.price or 0) == 0
            ),
            None,
        )
        if free_status is None:
            free_status = next((status for status in statuses if (status.price or 0) == 0), None)
        return free_status

    def _wait_item_status(self, item_id: str, tries: int = 8, delay: float = 1.5):
        last = None
        for _ in range(max(tries, 1)):
            try:
                last = self.account.get_item(item_id)
            except Exception:
                last = None
            if last is not None and last.status in (
                ItemStatuses.APPROVED,
                ItemStatuses.PENDING_APPROVAL,
                ItemStatuses.PENDING_MODERATION,
            ):
                return last
            time.sleep(delay)
        return last

    def _wait_item_discontinued(self, item_id: str, tries: int = 10, delay: float = 1.2):
        """Ждёт реального снятия: GraphQL иногда ещё секунды показывает APPROVED."""

        last = None
        for _ in range(max(tries, 1)):
            try:
                last = self.account.get_item(item_id)
            except Exception:
                last = None
            if last is not None and last.status in (
                ItemStatuses.DISCONTINUED,
                ItemStatuses.DRAFT,
                ItemStatuses.EXPIRED,
            ):
                return last
            if last is not None and last.status is None:
                # старый парсер мог не знать DISCONTINUED
                return last
            time.sleep(delay)
        return last

    def _publish_free_again(self, item_id: str, price: int, previous_error=None):
        free_status = self._free_priority_status(item_id, price)
        if free_status is None or (free_status.price or 0) > 0:
            raise Exception(
                f"лот снят с продажи, но бесплатно вернуть его не удалось: {previous_error}"
            )
        time.sleep(1)
        return self.account.publish_item(item_id, free_status.id)

    def _republish_free_item(self, item: ItemProfile | MyItem):
        """Бесплатный перевыпуск: discontinue → ждать DISCONTINUED → republish.

        Так успешно обновлялся бесплатный аккаунт (Казахстан). Премиум здесь запрещён.
        """

        if self._is_premium_item(item):
            raise Exception("это премиум-лот — бесплатный перевыпуск к нему не применяется")
        if getattr(item, "may_be_published", None) is False:
            raise Exception("Playerok не даёт выставить этот товар повторно")

        price = self._item_price(item)
        time.sleep(1)
        disc = self.account.discontinue_item(item.id)
        if isinstance(disc, dict) and disc.get("status") and disc.get("status") != "DISCONTINUED":
            if disc.get("success") is not True:
                raise Exception(f"discontinue отклонен: {disc}")

        # REST отвечает сразу, GraphQL ещё секунды может показывать APPROVED
        time.sleep(2)
        stopped = self._wait_item_discontinued(item.id)
        if stopped is None or stopped.status is ItemStatuses.APPROVED:
            raise Exception("не удалось снять лот с продажи (статус не сменился)")

        republish_error = None
        try:
            rep = self.account.republish_item(item.id)
            if isinstance(rep, dict) and rep.get("success") is False:
                raise Exception(f"republish отклонён: {rep}")
        except Exception as exc:
            republish_error = exc

        live = self._wait_item_status(item.id, tries=8, delay=1.2)
        if live is None or live.status not in (
            ItemStatuses.APPROVED,
            ItemStatuses.PENDING_APPROVAL,
            ItemStatuses.PENDING_MODERATION,
        ):
            try:
                published = self._publish_free_again(item.id, price, republish_error)
            except Exception as exc:
                raise Exception(f"лот снят, но бесплатно обратно не выставился: {exc}") from exc
            live = self._wait_item_status(item.id, tries=6, delay=1.2) or published

        if live is None or live.status not in (
            ItemStatuses.APPROVED,
            ItemStatuses.PENDING_APPROVAL,
            ItemStatuses.PENDING_MODERATION,
        ):
            status_name = getattr(getattr(live, "status", None), "name", None) or "неизвестен"
            raise Exception(f"после бесплатного обновления лот не в продаже (статус: {status_name})")

        if self._is_premium_item(live):
            raise Exception("после бесплатного перевыпуска лот стал PREMIUM — этого быть не должно")
        return live

    def _raise_with_premium(self, item: ItemProfile | MyItem, max_pay: int | None = None):
        """Платное поднятие только для лотов, которые уже в премиуме."""

        if self._is_free_item(item):
            raise Exception("обычный лот нельзя поднимать премиумом — для премиума есть отдельный лот")
        if not self._is_premium_item(item):
            raise Exception("лот не в премиуме — платное поднятие пропущено")

        statuses = self.account.get_item_priority_statuses(item.id, self._item_price(item))
        prem_status = next(
            (
                st for st in statuses
                if getattr(st, "type", None) is PriorityTypes.PREMIUM and (st.price or 0) > 0
            ),
            None,
        )
        if prem_status is None:
            prem_status = next(
                (st for st in statuses if (st.price or 0) > 0),
                None,
            )
        if not prem_status:
            raise Exception("PREMIUM статус не найден")

        pay = int(prem_status.price or 0)
        if max_pay is None:
            max_pay = int(self.config["playerok"]["auto_bump_items"].get("max_pay_price") or 0)
        else:
            max_pay = int(max_pay or 0)
        if max_pay > 0 and pay > max_pay:
            raise Exception(f"цена поднятия {pay}₽ выше лимита {max_pay}₽")

        time.sleep(1)
        raised = self.account.increase_item_priority_status(item.id, prem_status.id)
        live = self._wait_item_status(item.id, tries=5, delay=1.0) or raised
        return live or raised, pay

    def bump_item(self, item: ItemProfile | MyItem, reason: str | None = None):
        name_frmtd = item.name[:32] + ("..." if len(item.name) > 32 else "")
        kind = "free" if self._is_free_item(item) else "premium"
        item_id = getattr(item, "id", "") or ""
        try:
            if not self.is_bump_item_matched(item):
                return False
            # Автоподнятие только для премиум-лотов (за баланс). Free-перевыпуск позицию не даёт.
            if self._is_free_item(item):
                return False

            if item_id:
                with self.bump_lock:
                    if item_id in self._bumping_ids:
                        return False
                    until = self._bump_fail_until.get(item_id)
                    if until is not None and datetime.now() < until:
                        return False
                    self._bumping_ids.add(item_id)

            if not isinstance(item, MyItem):
                try:
                    item = self.account.get_item(item.id)
                except Exception:
                    with self.bump_lock:
                        if item_id:
                            self._bump_fail_until[item_id] = datetime.now() + timedelta(seconds=30)
                    return False
            kind = "free" if self._is_free_item(item) else "premium"
            name_frmtd = item.name[:32] + ("..." if len(item.name) > 32 else "")
            if kind != "premium":
                return False

            live, pay = self._raise_with_premium(item)
            if live is not None:
                item = live
            detail = f"премиум поднят (−{pay}₽)" if pay else "премиум поднят"

            with self.bump_lock:
                self.bumped_items[item.id] = datetime.now().isoformat()
                self._bump_fail_until.pop(item.id, None)

            note = f"{detail}{f' ({reason})' if reason else ''}"
            self._record_bump(item, kind, True, note)

            try:
                logger.info(
                    f"{Fore.LIGHTWHITE_EX}«{name_frmtd}» {Fore.WHITE}— {Fore.YELLOW}{note}"
                )
                if (
                    self.config["playerok"]["notifications"]["enabled"]
                    and self.config["playerok"]["notifications"]["events"]["item_bumped"]
                ):
                    self.log_to_tg(
                        log_text(
                            f'⬆️ Товар <a href="https://playerok.com/products/{item.slug}">«{item.name}»</a>: {note}'
                        ),
                        log_item_kb(item.id)
                    )
            except Exception:
                pass

            return True
        except Exception as e:
            if item_id:
                with self.bump_lock:
                    self._bump_fail_until[item_id] = datetime.now() + timedelta(seconds=75)
            self._record_bump(item, kind, False, str(e))
            logger.error(f"{Fore.LIGHTRED_EX}Ошибка при поднятии товара «{name_frmtd}»: {Fore.WHITE}{e}")
            if (
                self.config["playerok"]["notifications"]["enabled"]
                and self.config["playerok"]["notifications"]["events"]["item_bumped"]
            ):
                self.log_to_tg(
                    log_text(
                        f'❌ Ошибка при поднятии товара <a href="https://playerok.com/products/{item.slug}">«{item.name}»</a>',
                        f"<blockquote>{e}</blockquote>"
                    ),
                    log_item_kb(item.id)
                )
            return False
        finally:
            if item_id:
                with self.bump_lock:
                    self._bumping_ids.discard(item_id)
            self._bump_cache_at = None

    @staticmethod
    def _snipe_norm_name(name: str | None) -> str:
        return " ".join(str(name or "").lower().split())

    @staticmethod
    def _snipe_link_key(competitor_item_id: str, my_item_id: str) -> str:
        return f"{competitor_item_id}::{my_item_id}"

    def _iter_snipe_links(self) -> list[dict]:
        sellers = (self.snipe_targets or {}).get("sellers") or []
        out: list[dict] = []
        for seller in sellers:
            if not isinstance(seller, dict):
                continue
            username = str(seller.get("username") or "")
            user_id = str(seller.get("user_id") or "")
            for link in seller.get("links") or []:
                if not isinstance(link, dict):
                    continue
                competitor_item_id = str(link.get("competitor_item_id") or "")
                my_item_id = str(link.get("my_item_id") or "")
                if not competitor_item_id or not my_item_id:
                    continue
                out.append({
                    "username": username,
                    "user_id": user_id,
                    "competitor_item_id": competitor_item_id,
                    "competitor_slug": str(link.get("competitor_slug") or ""),
                    "competitor_name": str(link.get("competitor_name") or ""),
                    "my_item_id": my_item_id,
                    "my_name": str(link.get("my_name") or ""),
                })
        return out

    def _snipe_watch_mut(self, mutator) -> None:
        with self.snipe_lock:
            watch = self.snipe_watch if isinstance(self.snipe_watch, dict) else {"links": {}}
            if not isinstance(watch.get("links"), dict):
                watch["links"] = {}
            mutator(watch)
            self.snipe_watch = watch

    def _record_snipe(self, event: str, *, competitor_name: str = "", my_name: str = "",
                      detail: str = "", ok: bool = True) -> None:
        entry = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            "competitor_name": competitor_name or "",
            "my_name": my_name or "",
            "detail": detail or "",
            "ok": bool(ok),
        }
        with self.snipe_journal_lock:
            journal = data.get("snipe_journal")
            if not isinstance(journal, dict):
                journal = {"entries": []}
            rows = journal.get("entries")
            if not isinstance(rows, list):
                rows = []
            rows.append(entry)
            if len(rows) > 200:
                rows = rows[-200:]
            journal["entries"] = rows
            data.set("snipe_journal", journal)

    def _update_snipe_competitor_id(self, old_id: str, new_item) -> None:
        """Когда конкурент пересоздал лот — обновляем id в targets."""
        new_id = getattr(new_item, "id", "") or ""
        if not old_id or not new_id or old_id == new_id:
            return
        targets = sett.get("snipe_targets") or {"sellers": []}
        changed = False
        for seller in targets.get("sellers") or []:
            if not isinstance(seller, dict):
                continue
            for link in seller.get("links") or []:
                if not isinstance(link, dict):
                    continue
                if str(link.get("competitor_item_id") or "") != old_id:
                    continue
                link["competitor_item_id"] = new_id
                link["competitor_slug"] = getattr(new_item, "slug", None) or link.get("competitor_slug") or ""
                link["competitor_name"] = getattr(new_item, "name", None) or link.get("competitor_name") or ""
                changed = True
        if changed:
            sett.set("snipe_targets", targets)
            self.snipe_targets = targets

    def _fetch_seller_approved_items(self, username: str | None, user_id: str | None) -> list:
        cache_key = user_id or username or ""
        now = time.time()
        cached = self._snipe_seller_cache.get(cache_key)
        if cached and now - cached[0] < 20:
            return cached[1]

        profile = None
        if user_id:
            try:
                profile = self.account.get_user(id=user_id)
            except Exception:
                profile = None
        if profile is None and username:
            profile = self.account.get_user(username=username)
        if profile is None:
            raise Exception("продавец не найден")

        items = []
        after = None
        for _ in range(8):
            page = profile.get_items(
                statuses=[ItemStatuses.APPROVED],
                count=24,
                after_cursor=after,
            )
            batch = list(getattr(page, "items", None) or [])
            items.extend(batch)
            info = getattr(page, "page_info", None)
            if not info or not getattr(info, "has_next_page", False):
                break
            after = getattr(info, "end_cursor", None)
            if not after:
                break
            time.sleep(0.25)

        self._snipe_seller_cache[cache_key] = (now, items)
        return items

    def _snipe_find_item(self, link: dict, seller_items: list | None):
        competitor_id = link["competitor_item_id"]
        name_key = self._snipe_norm_name(link.get("competitor_name"))

        if seller_items is not None:
            by_id = next((it for it in seller_items if getattr(it, "id", None) == competitor_id), None)
            if by_id is not None:
                return by_id, False
            if name_key:
                twin = next(
                    (
                        it for it in seller_items
                        if self._snipe_norm_name(getattr(it, "name", None)) == name_key
                        and getattr(it, "id", None) != competitor_id
                    ),
                    None,
                )
                if twin is not None:
                    return twin, True
            return None, False

        try:
            item = self.account.get_item(id=competitor_id)
        except Exception:
            item = None
        if item is None:
            return None, False
        status = getattr(item, "status", None)
        if status is not ItemStatuses.APPROVED:
            return None, False
        return item, False

    def snipe_bump_item(self, my_item_id: str, reason: str) -> bool:
        """Платный подъём своего лота для перебива — без списка auto_bump."""
        item_id = my_item_id or ""
        name_frmtd = my_item_id[:32]
        kind = "premium"
        item = None
        try:
            if not item_id:
                return False
            with self.bump_lock:
                if item_id in self._bumping_ids:
                    return False
                until = self._bump_fail_until.get(item_id)
                if until is not None and datetime.now() < until:
                    return False
                self._bumping_ids.add(item_id)

            item = self.account.get_item(item_id)
            kind = "free" if self._is_free_item(item) else "premium"
            name_frmtd = item.name[:32] + ("..." if len(item.name) > 32 else "")
            if kind != "premium":
                raise Exception("для перебива нужен премиум-лот")

            max_pay = int((self.config.get("playerok") or {}).get("auto_snipe", {}).get("max_pay_price") or 0)
            live, pay = self._raise_with_premium(item, max_pay=max_pay)
            if live is not None:
                item = live
            detail = f"премиум поднят (−{pay}₽)" if pay else "премиум поднят"
            note = f"{detail} ({reason})"

            with self.bump_lock:
                self.bumped_items[item.id] = datetime.now().isoformat()
                self._bump_fail_until.pop(item.id, None)

            self._record_bump(item, kind, True, note)
            logger.info(
                f"{Fore.LIGHTMAGENTA_EX}Перебив: {Fore.LIGHTWHITE_EX}«{name_frmtd}» "
                f"{Fore.WHITE}— {Fore.YELLOW}{note}"
            )
            self._record_snipe(
                "bumped",
                competitor_name="",
                my_name=getattr(item, "name", "") or name_frmtd,
                detail=note,
                ok=True,
            )
            if (
                self.config["playerok"]["notifications"]["enabled"]
                and self.config["playerok"]["notifications"]["events"]["item_bumped"]
            ):
                self.log_to_tg(
                    log_text(
                        f'🎯 Перебив <a href="https://playerok.com/products/{item.slug}">«{item.name}»</a>: {note}'
                    ),
                    log_item_kb(item.id)
                )
            return True
        except Exception as e:
            if item_id:
                with self.bump_lock:
                    self._bump_fail_until[item_id] = datetime.now() + timedelta(seconds=75)
            if item is not None:
                try:
                    self._record_bump(item, kind, False, str(e))
                except Exception:
                    pass
            self._record_snipe(
                "failed",
                competitor_name="",
                my_name=name_frmtd,
                detail=str(e),
                ok=False,
            )
            logger.error(
                f"{Fore.LIGHTRED_EX}Ошибка перебива «{name_frmtd}»: {Fore.WHITE}{e}"
            )
            return False
        finally:
            if item_id:
                with self.bump_lock:
                    self._bumping_ids.discard(item_id)
            self._bump_cache_at = None

    def _snipe_schedule(self, key: str, link: dict, competitor, recreated: bool) -> None:
        delay = int((self.config.get("playerok") or {}).get("auto_snipe", {}).get("delay_seconds") or 60)
        delay = max(5, min(delay, 3600))
        approval = getattr(competitor, "approval_date", None) or ""
        pending_at = (datetime.now() + timedelta(seconds=delay)).isoformat()
        competitor_name = getattr(competitor, "name", None) or link.get("competitor_name") or ""

        my_name = str(link.get("my_name") or link.get("my_item_id") or "")

        def mutate(watch: dict):
            links = watch["links"]
            prev = links.get(key) if isinstance(links.get(key), dict) else {}
            links[key] = {
                "competitor_item_id": getattr(competitor, "id", None) or link["competitor_item_id"],
                "competitor_name": competitor_name,
                "my_item_id": link["my_item_id"],
                "my_name": my_name or prev.get("my_name") or "",
                "approval_date": approval,
                "pending_bump_at": pending_at,
                "last_seen_status": "APPROVED",
                "trigger": "recreate" if recreated else "approval",
            }
            # если ключ сменился из-за нового id — убрать старый
            old_key = self._snipe_link_key(link["competitor_item_id"], link["my_item_id"])
            if old_key != key and old_key in links:
                links.pop(old_key, None)
            if prev.get("pending_bump_at") and not recreated and prev.get("approval_date") == approval:
                links[key]["pending_bump_at"] = prev["pending_bump_at"]

        self._snipe_watch_mut(mutate)
        short = competitor_name[:32] + ("..." if len(competitor_name) > 32 else "")
        my_short = my_name[:32]
        why = "пересоздан" if recreated else "перевыставлен"
        logger.info(
            f"{Fore.LIGHTMAGENTA_EX}Перебив: {Fore.WHITE}чужой «{short}» {why} — "
            f"свой лот через {delay}с"
        )
        self._record_snipe(
            "detected",
            competitor_name=competitor_name,
            my_name=link.get("my_name") or "",
            detail=f"Чужой лот {why}. Свой «{my_short}» поднимется через {delay}с",
            ok=True,
        )

    def _snipe_remember(self, key: str, link: dict, competitor) -> None:
        approval = getattr(competitor, "approval_date", None) or ""
        my_name = str(link.get("my_name") or "")

        def mutate(watch: dict):
            links = watch["links"]
            prev = links.get(key) if isinstance(links.get(key), dict) else {}
            links[key] = {
                "competitor_item_id": getattr(competitor, "id", None) or link["competitor_item_id"],
                "competitor_name": getattr(competitor, "name", None) or link.get("competitor_name") or "",
                "my_item_id": link["my_item_id"],
                "my_name": my_name or prev.get("my_name") or "",
                "approval_date": approval,
                "pending_bump_at": prev.get("pending_bump_at"),
                "last_seen_status": "APPROVED",
                "trigger": prev.get("trigger") or "",
            }

        self._snipe_watch_mut(mutate)

    def check_snipe_targets(self) -> None:
        snipe = (self.config.get("playerok") or {}).get("auto_snipe") or {}
        if not snipe.get("enabled"):
            return

        links = self._iter_snipe_links()
        if not links:
            return

        # группируем по продавцу, чтобы один раз сходить в API
        by_seller: dict[str, list[dict]] = {}
        for link in links:
            seller_key = link.get("user_id") or link.get("username") or "_"
            by_seller.setdefault(seller_key, []).append(link)

        seller_items_map: dict[str, list | None] = {}
        for seller_key, group in by_seller.items():
            sample = group[0]
            try:
                seller_items_map[seller_key] = self._fetch_seller_approved_items(
                    sample.get("username"),
                    sample.get("user_id"),
                )
            except Exception as e:
                logger.debug(f"Перебив: не удалось загрузить лоты продавца {seller_key}: {e}")
                seller_items_map[seller_key] = None

        for link in links:
            seller_key = link.get("user_id") or link.get("username") or "_"
            seller_items = seller_items_map.get(seller_key)
            key = self._snipe_link_key(link["competitor_item_id"], link["my_item_id"])
            try:
                competitor, recreated = self._snipe_find_item(link, seller_items)
            except Exception as e:
                logger.debug(f"Перебив: ошибка проверки {key}: {e}")
                continue

            if competitor is None:
                def mark_missing(watch: dict, _key=key, _link=link):
                    links_map = watch["links"]
                    prev = links_map.get(_key) if isinstance(links_map.get(_key), dict) else {}
                    links_map[_key] = {
                        "competitor_item_id": _link["competitor_item_id"],
                        "competitor_name": _link.get("competitor_name") or prev.get("competitor_name") or "",
                        "my_item_id": _link["my_item_id"],
                        "approval_date": prev.get("approval_date") or "",
                        "pending_bump_at": prev.get("pending_bump_at"),
                        "last_seen_status": "MISSING",
                        "trigger": prev.get("trigger") or "",
                    }
                self._snipe_watch_mut(mark_missing)
                continue

            if recreated:
                self._update_snipe_competitor_id(link["competitor_item_id"], competitor)
                link = dict(link)
                link["competitor_item_id"] = competitor.id
                key = self._snipe_link_key(link["competitor_item_id"], link["my_item_id"])
                self._snipe_schedule(key, link, competitor, True)
                continue

            with self.snipe_lock:
                prev = (self.snipe_watch.get("links") or {}).get(key)
            approval = getattr(competitor, "approval_date", None) or ""
            if not isinstance(prev, dict) or not prev.get("approval_date"):
                self._snipe_remember(key, link, competitor)
                continue

            prev_approval = prev.get("approval_date") or ""
            if approval and prev_approval and approval != prev_approval:
                # новая дата одобрения = перевыставление
                try:
                    newer = datetime.fromisoformat(approval.replace("Z", "+00:00")) > datetime.fromisoformat(
                        prev_approval.replace("Z", "+00:00")
                    )
                except Exception:
                    newer = approval > prev_approval
                if newer:
                    self._snipe_schedule(key, link, competitor, False)
                    continue

            self._snipe_remember(key, link, competitor)

        self.config["playerok"]["auto_snipe"]["last_check_time"] = datetime.now().isoformat()
        sett.set("config", self.config)

    def process_pending_snipes(self) -> None:
        now = datetime.now()
        due: list[tuple[str, dict]] = []
        with self.snipe_lock:
            links = dict((self.snipe_watch or {}).get("links") or {})
            for key, row in links.items():
                if not isinstance(row, dict):
                    continue
                stamp = row.get("pending_bump_at")
                if not stamp:
                    continue
                try:
                    when = datetime.fromisoformat(str(stamp))
                except Exception:
                    continue
                if when <= now:
                    due.append((key, dict(row)))

        for key, row in due:
            my_id = str(row.get("my_item_id") or "")
            comp_name = str(row.get("competitor_name") or "лот")
            my_name = str(row.get("my_name") or my_id)
            short = comp_name[:32] + ("..." if len(comp_name) > 32 else "")
            self._record_snipe(
                "raising",
                competitor_name=comp_name,
                my_name=my_name,
                detail=f"Пора поднимать свой лот после «{short}»",
                ok=True,
            )
            ok = self.snipe_bump_item(my_id, reason=f"перебив «{short}»")

            def clear_pending(watch: dict, _key=key, _ok=ok):
                links_map = watch["links"]
                cur = links_map.get(_key)
                if not isinstance(cur, dict):
                    return
                cur = dict(cur)
                cur["pending_bump_at"] = None
                if _ok:
                    cur["trigger"] = ""
                links_map[_key] = cur

            self._snipe_watch_mut(clear_pending)

    def bump_items(self): 
        try:
            items = self.get_my_items(statuses=[ItemStatuses.APPROVED])
            up_items = [
                it for it in items
                if self.is_bump_item_matched(it) and self._is_premium_item(it)
            ]
            
            total = len(up_items)
            cnt = 0
            
            for item in up_items:
                if self.bump_item(item):
                    cnt += 1

            if cnt:
                self.config["playerok"]["auto_bump_items"]["last_time"] = datetime.now().isoformat()
                sett.set("config", self.config)

            self._clean_bumped_items()
            return True, total, cnt, None
        except Exception as e:
            logger.error(f"{Fore.LIGHTRED_EX}Ошибка при поднятии товаров: {Fore.WHITE}{e}")
            return False, 0, 0, e

    def _clean_bumped_items(self):
        border = datetime.now() - timedelta(days=7)
        with self.bump_lock:
            for item_id, bumped_at in list(self.bumped_items.items()):
                try:
                    if datetime.fromisoformat(bumped_at) < border:
                        del self.bumped_items[item_id]
                except:
                    del self.bumped_items[item_id]

    def _set_last_check_time(self):
        now = datetime.now().isoformat()
        self.config["playerok"]["auto_bump_items"]["last_check_time"] = now

        config = sett.get("config")
        config["playerok"]["auto_bump_items"]["last_check_time"] = now
        sett.set("config", config)

    def check_items_positions(self):
        try:
            self._set_last_check_time()

            position = get_current_bump_position(self.config)
            cooldown = self.config["playerok"]["auto_bump_items"]["cooldown"] or 0

            items = self.get_my_items(statuses=[ItemStatuses.APPROVED])
            up_items = [
                it for it in items
                if self.is_bump_item_matched(it) and self._is_premium_item(it)
            ]

            total = len(up_items)
            cnt = 0
            checked_ids = []
            chosen = None
            chosen_reason = ""

            for item in up_items:
                checked_ids.append(item.id)

                try:
                    time.sleep(1)
                    my_item = self.account.get_item(item.id)
                except Exception as e:
                    logger.error(f"{Fore.LIGHTRED_EX}Ошибка при получении позиции товара «{item.name[:32]}»: {Fore.WHITE}{e}")
                    continue

                if my_item.sequence is None:
                    with self.bump_lock:
                        self.item_positions.pop(my_item.id, None)
                    continue

                out_of_range = my_item.sequence > position

                with self.bump_lock:
                    cached = self.item_positions.get(my_item.id) or {}
                    streak = (cached.get("streak", 0) + 1) if out_of_range else 0
                    self.item_positions[my_item.id] = {
                        "name": my_item.name,
                        "slug": my_item.slug,
                        "position": my_item.sequence,
                        "streak": streak,
                        "checked_at": datetime.now().isoformat()
                    }
                    last_bumped = self.bumped_items.get(my_item.id, "")

                if streak < 2:
                    continue
                if not self._do_call_event(last_bumped, cooldown):
                    continue
                if chosen is None or (my_item.sequence or 0) > (chosen.sequence or 0):
                    chosen = my_item
                    chosen_reason = f"выпал на {my_item.sequence} место"

            if chosen and self.bump_item(chosen, reason=chosen_reason):
                cnt += 1
                with self.bump_lock:
                    pos = self.item_positions.get(chosen.id)
                    if pos:
                        pos["streak"] = 0
                        pos["position"] = 1

            with self.bump_lock:
                for item_id in list(self.item_positions):
                    if item_id not in checked_ids:
                        del self.item_positions[item_id]

            self._clean_bumped_items()
            return True, total, cnt, None
        except Exception as e:
            logger.error(f"{Fore.LIGHTRED_EX}Ошибка при проверке позиций товаров: {Fore.WHITE}{e}")
            return False, 0, 0, e

    def _bump_last_map(self) -> dict[str, datetime]:
        with self.bump_lock:
            raw_items = dict(self.bumped_items)
        result = {}
        for item_id, raw in raw_items.items():
            stamp = parse_stamp(raw)
            if stamp is not None:
                result[str(item_id)] = stamp
        return result

    def _bump_included_rows(self) -> list[dict]:
        """Стабильный состав лотов из настроек поднятия (не только APPROVED)."""

        seen: set[str] = set()
        ordered: list[dict] = []
        included = (self.auto_bump_items or {}).get("included") or []
        for entry in included:
            items = entry.get("items") if isinstance(entry, dict) else None
            if not isinstance(items, list):
                continue
            for row in items:
                item_id = ""
                name = ""
                if isinstance(row, dict):
                    item_id = str(row.get("id") or "")
                    name = str(row.get("name") or "")
                elif isinstance(row, str):
                    item_id = row
                if item_id and item_id not in seen:
                    seen.add(item_id)
                    ordered.append({"id": item_id, "name": name})
        return ordered

    def _bump_included_ids(self) -> list[str]:
        return [row["id"] for row in self._bump_included_rows()]

    def _write_bump_plan(self, roster_ids, active_by_id, interval, now, anchor, last_map, slot):
        names = {row["id"]: row.get("name") or "" for row in self._bump_included_rows()}
        plan = []
        count = len(roster_ids)
        for index, item_id in enumerate(roster_ids):
            item = active_by_id.get(item_id)
            when = next_slot_time(index, count, interval, now, anchor, last_map.get(item_id))
            plan.append({
                "id": item_id,
                "name": getattr(item, "name", None) or names.get(item_id) or item_id,
                "kind": ("free" if item is None or self._is_free_item(item) else "premium"),
                "active": item_id in active_by_id,
                "next_at": when.isoformat(timespec="seconds"),
            })

        def apply(journal):
            journal["anchor"] = anchor.isoformat(timespec="seconds")
            journal["slot"] = int(round(slot))
            journal["plan"] = plan

        self._journal_mut(apply)

    def bump_scheduled_once(self, interval: int):
        now = datetime.now()
        refresh = (
            self._bump_cache_at is None
            or (now - self._bump_cache_at).total_seconds() >= 45
        )
        if refresh:
            try:
                found = [
                    it for it in self.get_my_items(statuses=[ItemStatuses.APPROVED])
                    if self.is_bump_item_matched(it) and self._is_premium_item(it)
                ]
            except Exception as exc:
                logger.error(f"{Fore.LIGHTRED_EX}Ошибка при чтении лотов для поднятия: {Fore.WHITE}{exc}")
                return
            found.sort(key=lambda it: it.id)
            found_ids = {it.id for it in found}
            included_ids = self._bump_included_ids()
            if self.config["playerok"]["auto_bump_items"]["all"]:
                roster_ids = [it.id for it in found]
            else:
                # Только отмеченные премиум-лоты. Пустой список = ничего не поднимаем.
                roster_ids = [item_id for item_id in included_ids if item_id in found_ids]
            self._bump_cache_items = found
            self._bump_roster_ids = roster_ids
            self._bump_cache_at = now
            if (
                self._bump_twins_warned_at is None
                or (now - self._bump_twins_warned_at).total_seconds() >= 1800
            ):
                self._warn_premium_twin_overflow(limit=2)
                self._bump_twins_warned_at = now
            if not found:
                if not self._bump_empty_logged:
                    logger.info(
                        f"{Fore.LIGHTYELLOW_EX}Автоподнятие: нет активных премиум-лотов из списка. "
                        f"{Fore.WHITE}Обычные лоты не поднимаем; отметьте премиум-позицию."
                    )
                    self._bump_empty_logged = True
            else:
                self._bump_empty_logged = False

        items = list(self._bump_cache_items)
        roster_ids = list(self._bump_roster_ids) or [it.id for it in items]
        if not roster_ids or interval <= 0:
            return

        journal = data.get("bump_journal")
        if not isinstance(journal, dict):
            journal = {}
        anchor = parse_stamp(journal.get("anchor")) or now
        if not journal.get("anchor"):
            def save_anchor(state):
                state["anchor"] = anchor.isoformat(timespec="seconds")
            self._journal_mut(save_anchor)

        last_slot = parse_stamp(journal.get("last_slot_at"))
        last_map = self._bump_last_map()
        with self.bump_lock:
            blocked = {
                item_id
                for item_id, until in self._bump_fail_until.items()
                if until is not None and now < until
            }
            blocked.update(self._bumping_ids)
        active_by_id = {it.id: it for it in items}
        active_ids = set(active_by_id) - blocked
        chosen_id = pick_staggered_item(
            roster_ids,
            interval,
            now,
            anchor,
            last_map,
            last_slot,
            active_ids=active_ids,
        )
        slot = interval / max(len(roster_ids), 1)
        if refresh or chosen_id:
            self._write_bump_plan(roster_ids, active_by_id, interval, now, anchor, last_map, slot)
        if not chosen_id:
            return

        item = active_by_id.get(chosen_id)
        if item is None:
            return
        ok = self.bump_item(item, reason="по очереди")
        if not ok:
            return

        acted_at = datetime.now().isoformat(timespec="seconds")

        def mark_slot(state):
            state["last_slot_at"] = acted_at

        self._journal_mut(mark_slot)
        self.config["playerok"]["auto_bump_items"]["last_time"] = acted_at
        sett.set("config", self.config)
        self._write_bump_plan(
            roster_ids,
            active_by_id,
            interval,
            datetime.now(),
            anchor,
            self._bump_last_map(),
            slot,
        )

    def _stock_pools(self) -> tuple[dict, list]:
        stock = sett.get("account_stock")
        if not isinstance(stock, dict):
            return {"pools": []}, []
        pools = stock.get("pools")
        if not isinstance(pools, list):
            stock["pools"] = []
            pools = stock["pools"]
        return stock, pools

    def _is_stock_prepared(self, prepared) -> bool:
        return isinstance(prepared, tuple) and bool(prepared) and prepared[0] == _STOCK_MARK

    def prepare_account_stock(self, item: Item | MyItem):
        stock, pools = self._stock_pools()
        if not _stock_handles_item(pools, item):
            return None

        fields = [
            field for field in (getattr(item, "data_fields", None) or [])
            if field.type is GameCategoryDataFieldTypes.ITEM_DATA
        ]
        if not fields:
            raise Exception("у товара нет полей с данными для входа")

        unknown = []
        pairs = []
        current_login = ""
        has_login = False
        for field in fields:
            key = stock_field_key(getattr(field, "label", "") or "")
            if not key:
                unknown.append(getattr(field, "label", "") or "без названия")
                continue
            pairs.append((field, key))
            if key in ("login", "credentials"):
                has_login = True
                if not current_login:
                    current_login = stock_login_from_value(getattr(field, "value", "") or "")
        if unknown:
            names = ", ".join(f"«{name}»" for name in unknown)
            raise Exception(f"нет данных для полей {names}")
        if not has_login:
            raise Exception("у товара нет поля логина")

        item_id = _item_identity(item)
        index, chosen, sold_login, reused = select_stock_issue(stock, current_login, item_id)
        next_login = account_login(chosen)
        if not reused:
            # запоминаем пару до запроса: если публикация сорвётся, повтор возьмёт тот же аккаунт
            entry = {"sold_login": sold_login, "next_login": next_login, "item_id": item_id}
            pool = pools[index]
            if item_id:
                by_item = pool.get("pending_by_item")
                if not isinstance(by_item, dict):
                    by_item = {}
                by_item[item_id] = entry
                pool["pending_by_item"] = by_item
            pool["pending"] = entry
            sett.set("account_stock", stock)

        for field, key in pairs:
            if key == "keep":
                continue
            field.value = stock_account_value(chosen, key)
        return _STOCK_MARK, index, next_login, fields

    def commit_account_stock(self, index: int, next_login: str) -> int:
        stock, pools = self._stock_pools()
        if index < 0 or index >= len(pools) or not isinstance(pools[index], dict):
            return 0
        pool = pools[index]
        pending = pool.get("pending") if isinstance(pool.get("pending"), dict) else {}
        issued = str(pending.get("next_login") or next_login).strip()
        entry = pending if isinstance(pending, dict) else {}
        by_item = pool.get("pending_by_item")
        if isinstance(by_item, dict):
            for candidate in by_item.values():
                if isinstance(candidate, dict) and same_login(str(candidate.get("next_login") or ""), issued):
                    entry = candidate
                    break
        sold_login = str(entry.get("sold_login") or "").strip()
        issued_item = str(entry.get("item_id") or "").strip()
        for other in pools:
            if not isinstance(other, dict):
                continue
            owns_issued = False
            for row in other.get("accounts") or []:
                if not isinstance(row, dict):
                    continue
                login = account_login(row)
                row_item = _account_item_id(row)
                if sold_login and same_login(login, sold_login):
                    # логин уникален в очереди — помечаем sold даже если item_id в стоке устарел
                    row["status"] = "sold"
                    row["item_id"] = ""
                elif issued and same_login(login, issued):
                    row["status"] = "listed"
                    if issued_item:
                        row["item_id"] = issued_item
                    owns_issued = True
                    if not issued_item:
                        issued_item = row_item
            if owns_issued:
                other["active_login"] = issued
            elif sold_login and same_login(str(other.get("active_login") or ""), sold_login):
                other["active_login"] = ""
            stored = other.get("pending_by_item")
            if isinstance(stored, dict) and issued_item:
                stored.pop(issued_item, None)
            current = other.get("pending") if isinstance(other.get("pending"), dict) else None
            if current and (
                same_login(str(current.get("next_login") or ""), issued)
                or (issued_item and str(current.get("item_id") or "") == issued_item)
            ):
                other["pending"] = None
            if _pool_role(pool, index) == "reserve":
                other["need_reserve"] = False
        if _pool_role(pool, index) == "reserve":
            stock["need_reserve"] = False
        sett.set("account_stock", stock)
        return sum(
            1 for row in (pool.get("accounts") or [])
            if isinstance(row, dict) and (row.get("status") or "free") == "free"
        )

    def mark_stock_need_reserve(self, item) -> bool:
        stock, pools = self._stock_pools()
        if not _stock_handles_item(pools, item):
            return False
        if stock.get("need_reserve"):
            return True
        stock["need_reserve"] = True
        sett.set("account_stock", stock)
        logger.info(
            f"{Fore.YELLOW}Покупатель открыл проблему: следующая выдача возьмёт один аккаунт из резерва"
        )
        return True

    def finish_prepared_data(self, prepared):
        if not prepared:
            return None
        if self._is_stock_prepared(prepared):
            left = self.commit_account_stock(prepared[1], prepared[2])
            return True, left, prepared[2]
        return True, self.consume_item_data(*prepared[:2])

    def prepare_item_data(self, item: Item | MyItem):
        stock_prepared = self.prepare_account_stock(item)
        if stock_prepared is not None:
            return stock_prepared

        data_replacement = sett.get("data_replacement") or []
        repl = next((
            r for r in data_replacement
            if r.get("enabled") and item_matches_binding(item, r)
        ), None)
        if not repl:
            return None

        values = repl.get("data") or []
        if not values:
            raise Exception("данные для замены закончились")

        fields = [
            f for f in (getattr(item, "data_fields", None) or [])
            if f.type is GameCategoryDataFieldTypes.ITEM_DATA
        ]
        if not fields:
            raise Exception("у товара нет полей с данными для замены")

        sep = repl.get("separator") or ":"
        new_values = values[0].split(sep)
        if len(new_values) < len(fields):
            raise Exception(
                f"в строке данных «{values[0]}» {len(new_values)} значений, "
                f"а у товара {len(fields)} полей с данными"
            )

        for field, value in zip(fields, new_values):
            field.value = value.strip()

        return repl, values[0], fields

    def consume_item_data(self, binding: dict, value: str) -> int:
        # перечитываем перед списанием: за время запроса тг-бот мог изменить замены
        data_replacement = sett.get("data_replacement") or []
        key = binding_key(binding)
        repl = next(
            (
                r for r in data_replacement
                if binding_key(r) == key and value in (r.get("data") or [])
            ),
            None
        )
        if repl is None:
            return 0

        repl["data"].remove(value)
        self.data_replacement = data_replacement
        sett.set("data_replacement", data_replacement)
        return len(repl.get("data", []))

    def replace_item_data(self, item: Item | MyItem):
        prepared = self.prepare_item_data(item)
        if not prepared:
            return None

        fields = prepared[3] if self._is_stock_prepared(prepared) else prepared[2]
        self.account.update_item(item.id, data_fields=fields)
        if self._is_stock_prepared(prepared):
            return prepared
        return True, self.consume_item_data(prepared[0], prepared[1])

    def publish_restored_item(self, item_id: str, raw_price: int, is_premium: bool, name_frmtd: str):
        time.sleep(1)
        statuses = self.account.get_item_priority_statuses(item_id, raw_price)
        if not statuses:
            raise Exception("Статусы приоритета не найдены")

        prem_status = next(
            (st for st in statuses if st.type is PriorityTypes.PREMIUM or st.price > 0),
            None
        )
        free_status = next(
            (st for st in statuses if st.type is PriorityTypes.DEFAULT or st.price == 0),
            None
        )

        publish_status = self.config["playerok"]["auto_restore_items"]["publish_status"]
        want_premium = bool(is_premium) or publish_status == "premium"
        # обычные лоты никогда не апгрейдим до премиума при восстановлении
        if not is_premium and publish_status != "premium":
            want_premium = False
        if want_premium and not self.config["playerok"]["auto_restore_items"].get("premium", False):
            want_premium = False

        if want_premium:
            self._assert_premium_twin_limit(name_frmtd, exclude_id=item_id, limit=2)
            pr_status = prem_status
            if not pr_status:
                raise Exception("PREMIUM статус приоритета не найден")
        else:
            pr_status = free_status or statuses[0]
            if pr_status is not None and pr_status.type is PriorityTypes.PREMIUM:
                raise Exception("для обычного лота недоступен бесплатный статус — премиум ставить нельзя")

        time.sleep(1)
        new_item = self.account.publish_item(item_id, pr_status.id)
        if want_premium and getattr(new_item, "priority", None) is not PriorityTypes.PREMIUM:
            raise Exception("не удалось восстановить как премиум")
        if not want_premium and getattr(new_item, "priority", None) is PriorityTypes.PREMIUM:
            raise Exception("обычный лот восстановился как PREMIUM — отклонено")
        return new_item, pr_status

    def create_item_copy(self, item: MyItem) -> Item:
        if not item.category or not item.obtaining_type:
            raise Exception("у товара нет категории или способа получения")

        attachments = []
        for att in (item.attachments or []):
            if not att.url:
                continue
            attachments.append(self.account.download_file(att.url))
            time.sleep(0.5)

        data_fields = [
            f for f in (item.data_fields or [])
            if f.type is GameCategoryDataFieldTypes.ITEM_DATA
        ]

        time.sleep(1)
        return self.account.create_item(
            game_category_id=item.category.id,
            obtaining_type_id=item.obtaining_type.id,
            name=item.name,
            price=item.raw_price,
            description=item.description or "",
            options=item.attributes or {},
            data_fields=data_fields,
            attachments=attachments
        )

    def remove_item_quietly(self, item_id: str, name_frmtd: str, what: str) -> bool:
        err = None
        for _ in range(3):
            try:
                time.sleep(1)
                self.account.remove_item(item_id)
                return True
            except Exception as e:
                err = e

        logger.warning(
            f"{Fore.LIGHTWHITE_EX}«{name_frmtd}» {Fore.WHITE}— "
            f"{Fore.LIGHTYELLOW_EX}не удалось удалить {what}: {Fore.WHITE}{err}"
        )
        return False

    def restore_item(self, item: Item | MyItem | ItemProfile):
        try:
            name_frmtd = item.name[:32] + ("..." if len(item.name) > 32 else "")
            item_id = getattr(item, "id", "") or ""
            with self.bump_lock:
                if item_id and item_id in self._bumping_ids:
                    return
            
            included = item_matches_any_binding(item, self.auto_restore_items["included"])
            excluded = item_matches_any_binding(item, self.auto_restore_items["excluded"])

            if (
                self.config["playerok"]["auto_restore_items"]["all"]
                and not excluded
            ) or (
                not self.config["playerok"]["auto_restore_items"]["all"]
                and included
            ):
                is_premium = item.priority is PriorityTypes.PREMIUM
                if is_premium and not self.config["playerok"]["auto_restore_items"]["premium"]:
                    return
                if not is_premium and not self.config["playerok"]["auto_restore_items"]["free"]:
                    return

                if not isinstance(item, MyItem):
                    try: item = self.account.get_item(item.id)
                    except: return
                    # приоритет мог обновиться, перепроверяем — иначе премиум спишется вопреки настройке
                    is_premium = item.priority is PriorityTypes.PREMIUM
                    if is_premium and not self.config["playerok"]["auto_restore_items"]["premium"]:
                        return
                    if not is_premium and not self.config["playerok"]["auto_restore_items"]["free"]:
                        return

                # playerok больше не даёт выставить повторно товары некоторых категорий (аккаунты)
                recreate = item.may_be_published is False
                if recreate:
                    if not self.config["playerok"]["auto_restore_items"]["recreate"]:
                        logger.warning(
                            f"{Fore.LIGHTWHITE_EX}«{name_frmtd}» {Fore.WHITE}— "
                            f"{Fore.LIGHTYELLOW_EX}Playerok не даёт выставить этот товар повторно. "
                            f"{Fore.WHITE}Включите «Пересоздание» в меню авто-восстановления, "
                            f"чтобы бот пересоздавал такие товары"
                        )
                        return
                    # товар уже пересоздавали — иначе наплодим дублей
                    if item.id in self.recreated_items:
                        return

                    # данные списываем только после успешной публикации, иначе сгорят впустую
                    prepared = self.prepare_item_data(item)
                    draft = self.create_item_copy(item)
                    try:
                        new_item, pr_status = self.publish_restored_item(
                            draft.id, item.raw_price, is_premium, name_frmtd
                        )
                    except Exception:
                        # иначе на каждой неудачной попытке будет копиться новый черновик
                        self.remove_item_quietly(draft.id, name_frmtd, "черновик после неудачной публикации")
                        raise

                    self.recreated_items.add(item.id)
                    replaced = self.finish_prepared_data(prepared)

                    rebound = rebind_item(item.id, {
                        "id": new_item.id,
                        "slug": new_item.slug or item.slug,
                        "name": new_item.name or item.name
                    })
                    if rebound:
                        logger.info(
                            f"{Fore.LIGHTWHITE_EX}«{name_frmtd}» {Fore.WHITE}— "
                            f"{Fore.YELLOW}привязки перенесены на пересозданный товар "
                            f"{Fore.WHITE}(записей: {Fore.LIGHTWHITE_EX}{rebound}{Fore.WHITE})"
                        )

                    old_removed = self.remove_item_quietly(item.id, name_frmtd, "старый товар после пересоздания")
                    if old_removed:
                        with self.bump_lock:
                            self.bumped_items.pop(item.id, None)
                            self.item_positions.pop(item.id, None)
                else:
                    old_removed = True
                    prepared = self.replace_item_data(item)
                    new_item, pr_status = self.publish_restored_item(
                        item.id, item.raw_price, is_premium, name_frmtd
                    )
                    replaced = self.finish_prepared_data(prepared) if self._is_stock_prepared(prepared) else prepared

                status_frmtd = "премиум" if pr_status.type is PriorityTypes.PREMIUM else "бесплатный"
                action_frmtd = "пересоздан" if recreate else "восстановлен"

                logger.info(
                    f"{Fore.LIGHTWHITE_EX}«{name_frmtd}» "
                    f"{Fore.WHITE}— {Fore.YELLOW}товар {action_frmtd} "
                    f"{Fore.WHITE}(статус: {Fore.LIGHTWHITE_EX}{status_frmtd}{Fore.WHITE})"
                    + data_replaced_console(replaced)
                    + (
                        f"{Fore.WHITE}, но {Fore.LIGHTYELLOW_EX}старый товар остался — удалите его вручную"
                        if not old_removed else ""
                    )
                )
                if (
                    self.config["playerok"]["notifications"]["enabled"]
                    and self.config["playerok"]["notifications"]["events"]["item_restored"]
                ):
                    self.log_to_tg(
                        log_text(
                            f'{"🆕" if recreate else "♻️"} Товар <a href="https://playerok.com/products/{new_item.slug}">«{item.name}»</a> {action_frmtd} (статус: {status_frmtd})',
                            "\n".join(filter(None, [
                                data_replaced_tg(replaced),
                                "⚠️ Старый товар не удалился — удалите его вручную" if not old_removed else ""
                            ]))
                        ),
                        log_item_kb(new_item.id)
                    )
                return True
        except Exception as e:
            logger.error(f"{Fore.LIGHTRED_EX}Ошибка при восстановлении товара «{name_frmtd}»: {Fore.WHITE}{e}")
            if (
                self.config["playerok"]["notifications"]["enabled"]
                and self.config["playerok"]["notifications"]["events"]["item_restored"]
            ):
                self.log_to_tg(
                    log_text(
                        f'❌ Ошибка при восстановлении товара <a href="https://playerok.com/products/{item.slug}">«{item.name}»</a>',
                        f"<blockquote>{e}</blockquote>"
                    ),
                    log_item_kb(item.id)
                )
            return False
            
    def restore_expired_items(self):
        try:
            restored_items = []
            items = self.get_my_items(statuses=[ItemStatuses.EXPIRED])
            
            for item in items:
                if item.id in restored_items:
                    continue
                restored_items.append(item.id)
                
                time.sleep(0.5)
                self.restore_item(item)
        except Exception as e:
            logger.error(f"{Fore.LIGHTRED_EX}Ошибка при восстановлении истёкших товаров: {Fore.WHITE}{e}")

    def _can_restore_sold_with_new_data(self, item) -> bool:
        """Catch-up только для лотов, где есть откуда взять новые данные (сток / замена)."""
        _, pools = self._stock_pools()
        if _stock_handles_item(pools, item):
            return True
        for repl in (sett.get("data_replacement") or []):
            if isinstance(repl, dict) and repl.get("enabled") and item_matches_binding(item, repl):
                return True
        return False

    def restore_missed_sold_items(self):
        """Восстанавливает SOLD-лоты после офлайна (чат-события ITEM_PAID при старте пропускаются)."""
        try:
            if not self.config["playerok"]["auto_restore_items"].get("sold"):
                return

            items = self.get_my_items(statuses=[ItemStatuses.SOLD])
            targets = [item for item in items if self._can_restore_sold_with_new_data(item)]
            if not targets:
                logger.info(
                    f"{Fore.WHITE}Проверка продаж за офлайн: "
                    f"{Fore.LIGHTWHITE_EX}лотов со стоком/заменой данных на восстановление нет"
                )
                return

            logger.info(
                f"{Fore.YELLOW}Проверка продаж за офлайн: "
                f"{Fore.LIGHTWHITE_EX}{len(targets)} "
                f"{Fore.WHITE}лот(ов) — восстанавливаю с новыми данными"
            )
            seen: set[str] = set()
            for item in targets:
                item_id = getattr(item, "id", "") or ""
                if not item_id or item_id in seen:
                    continue
                seen.add(item_id)
                time.sleep(0.5)
                self.restore_item(item)
        except Exception as e:
            logger.error(
                f"{Fore.LIGHTRED_EX}Ошибка при восстановлении проданных за офлайн товаров: "
                f"{Fore.WHITE}{e}"
            )

    def request_withdrawal(self) -> bool:
        try:
            self.config["playerok"]["auto_withdrawal"]["last_time"] = datetime.now().isoformat()
            sett.set("config", self.config)
            
            self.account = self.account.get()

            balance = self.account.profile.balance.withdrawable if self.account.profile.balance else 0
            if balance <= 500:
                raise Exception("Слишком маленький баланс. Транзакция должна быть на сумму от 500₽")
            
            if self.config["playerok"]["auto_withdrawal"]["credentials_type"] == "card":
                provider = TransactionProviderIds.BANK_CARD_RU
                account = self.config["playerok"]["auto_withdrawal"]["card_id"]
                sbp_bank_member_id = None
            elif self.config["playerok"]["auto_withdrawal"]["credentials_type"] == "sbp":
                provider = TransactionProviderIds.SBP
                account = self.config["playerok"]["auto_withdrawal"]["sbp_phone_number"]
                sbp_bank_member_id = self.config["playerok"]["auto_withdrawal"]["sbp_bank_id"]
            elif self.config["playerok"]["auto_withdrawal"]["credentials_type"] == "usdt":
                provider = TransactionProviderIds.USDT
                account = self.config["playerok"]["auto_withdrawal"]["usdt_address"]
                sbp_bank_member_id = None
            
            trans = self.account.request_withdrawal(
                provider=provider,
                account=account,
                value=balance,
                sbp_bank_member_id=sbp_bank_member_id
            )
            
            logger.info(
                f"{Fore.LIGHTWHITE_EX}{balance or '?'}₽ {Fore.WHITE}"
                f"— {Fore.YELLOW}транзакция на вывод создана"
            )

            if (
                self.config["playerok"]["notifications"]["enabled"]
                and self.config["playerok"]["notifications"]["events"]["withdrawal_requested"]
            ):
                self.log_to_tg(
                    log_text(f'💳 Транзакция на вывод {balance}₽ успешно создана'),
                    log_transaction_kb(trans.id)
                )

            return True, balance, None
        except Exception as e:
            logger.error(f"{Fore.LIGHTRED_EX}Ошибка при создании транзакции на вывод {balance}₽: {Fore.WHITE}{e}")
            if (
                self.config["playerok"]["notifications"]["enabled"]
                and self.config["playerok"]["notifications"]["events"]["withdrawal_requested"]
            ):
                self.log_to_tg(
                    log_text(
                        f'❌ Ошибка при создании транзакции на вывод {balance}₽',
                        f"<blockquote>{e}</blockquote>"
                    ),
                    destroy_kb()
                )
            return False, 0, e


    def log_new_message(self, message: ChatMessage, chat: Chat):
        plbot = get_playerok_bot()
        
        chat_user = next((u.username for u in chat.users if u.id != plbot.account.id), None)
        if not chat_user:
            chat_user = message.user.username
        
        ch_header = f"Новое сообщение в чате с {chat_user}:"
        
        logger.info(f"{ACCENT_COLOR}{ch_header.replace(chat_user, f'{Fore.LIGHTCYAN_EX}{chat_user}')}")
        logger.info(f"{ACCENT_COLOR}│ {Fore.LIGHTWHITE_EX}{message.user.username}:")
        
        max_width = shutil.get_terminal_size((80, 20)).columns - 40
        longest_line_len = 0
        text = ""
        
        if message.text: 
            text = message.text
        if message.images: 
            text = f"{Fore.LIGHTMAGENTA_EX}*Изображения ({len(message.images)})*" + ((f" {Fore.WHITE}" + text) if text else "")
        
        for raw_line in text.split("\n"):
            if not raw_line.strip():
                logger.info(f"{ACCENT_COLOR}│")
                continue
            
            wrapped_lines = textwrap.wrap(raw_line, width=max_width)
            for wrapped in wrapped_lines:
                logger.info(f"{ACCENT_COLOR}│ {Fore.WHITE}{wrapped}")
                longest_line_len = max(longest_line_len, len(wrapped.strip()))
        
        underline_len = max(len(ch_header)-1, longest_line_len+2)
        logger.info(f"{ACCENT_COLOR}└{'─'*underline_len}")

    def log_new_deal(self, deal: ItemDeal):
        logger.info(f"{Fore.YELLOW}───────────────────────────────────────")
        logger.info(f"{Fore.YELLOW}Новая сделка {deal.id}:")
        logger.info(f" · Покупатель: {Fore.LIGHTWHITE_EX}{deal.user.username}")
        logger.info(f" · Товар: {Fore.LIGHTWHITE_EX}{deal.item.name}")
        logger.info(f" · Сумма: {Fore.LIGHTWHITE_EX}{deal.item.price}₽")
        logger.info(f"{Fore.YELLOW}───────────────────────────────────────")

    def log_new_review(self, deal: ItemDeal):
        logger.info(f"{Fore.YELLOW}───────────────────────────────────────")
        logger.info(f"{Fore.YELLOW}Новый отзыв по сделке {deal.id}:")
        logger.info(f" · Оценка: {Fore.LIGHTYELLOW_EX}{'⭐' * deal.review.rating or 5} ({deal.review.rating or 5})")
        logger.info(f" · Текст: {Fore.LIGHTWHITE_EX}{deal.review.text or '-'}")
        logger.info(f" · Оставил: {Fore.LIGHTWHITE_EX}{deal.review.creator.username}")
        logger.info(f" · Дата: {Fore.LIGHTWHITE_EX}{datetime.fromisoformat(deal.review.created_at).strftime('%d.%m.%Y %H:%M:%S')}")
        logger.info(f"{Fore.YELLOW}───────────────────────────────────────")

    def log_deal_status_changed(self, deal: ItemDeal, status_frmtd: str = "Неизвестный"):
        logger.info(f"{Fore.WHITE}───────────────────────────────────────")
        logger.info(f"{Fore.WHITE}Статус сделки {Fore.LIGHTWHITE_EX}{deal.id} {Fore.WHITE}изменился:")
        logger.info(f" · Статус: {Fore.LIGHTWHITE_EX}{status_frmtd}")
        logger.info(f" · Покупатель: {Fore.LIGHTWHITE_EX}{deal.user.username}")
        logger.info(f" · Товар: {Fore.LIGHTWHITE_EX}{deal.item.name}")
        logger.info(f" · Сумма: {Fore.LIGHTWHITE_EX}{deal.item.price}₽")
        logger.info(f"{Fore.WHITE}───────────────────────────────────────")

    def log_new_problem(self, deal: ItemDeal):
        logger.info(f"{Fore.YELLOW}───────────────────────────────────────")
        logger.info(f"{Fore.YELLOW}Новая жалоба в сделке {deal.id}:")
        logger.info(f" · Оставил: {Fore.LIGHTWHITE_EX}{deal.user.username}")
        logger.info(f" · Товар: {Fore.LIGHTWHITE_EX}{deal.item.name}")
        logger.info(f" · Сумма: {Fore.LIGHTWHITE_EX}{deal.item.price}₽")
        logger.info(f"{Fore.YELLOW}───────────────────────────────────────")


    def _event_datetime(self, latest_event_time, event_interval):
        if latest_event_time:
            return (
                datetime.fromisoformat(latest_event_time) 
                + timedelta(seconds=event_interval)
            )
        else:
            return datetime.now()

    async def _on_playerok_bot_init(self):
        def endless_loop():
            while True:
                balance = self.account.profile.balance.value if self.account.profile.balance is not None else "?"
                set_title(f"Playerok Universal v{VERSION} | {self.account.username}: {balance}₽")
                
                if sett.get("config") != self.config: 
                    self.config = sett.get("config")
                if sett.get("messages") != self.messages: 
                    self.messages = sett.get("messages")
                if sett.get("custom_commands") != self.custom_commands: 
                    self.custom_commands = sett.get("custom_commands")
                if sett.get("auto_deliveries") != self.auto_deliveries: 
                    self.auto_deliveries = sett.get("auto_deliveries")
                if sett.get("auto_restore_items") != self.auto_restore_items: 
                    self.auto_restore_items = sett.get("auto_restore_items")
                if sett.get("auto_complete_deals") != self.auto_complete_deals: 
                    self.auto_complete_deals = sett.get("auto_complete_deals")
                if sett.get("auto_bump_items") != self.auto_bump_items:
                    self.auto_bump_items = sett.get("auto_bump_items")
                if sett.get("snipe_targets") != self.snipe_targets:
                    self.snipe_targets = sett.get("snipe_targets") or {"sellers": []}
                if sett.get("greetings") != self.greetings:
                    self.greetings = sett.get("greetings") or {"items": []}
                if sett.get("data_replacement") != self.data_replacement:
                    self.data_replacement = sett.get("data_replacement")

                try:
                    from error_cases import pop_pending_gifts
                    for gift in pop_pending_gifts():
                        chat_id = str(gift.get("chat_id") or "")
                        text = str(gift.get("text") or "")
                        if not chat_id or not text:
                            continue
                        mess = self.send_message(chat_id, text)
                        if mess:
                            logger.info(
                                f"{Fore.YELLOW}Покупателю в чате {Fore.LIGHTYELLOW_EX}{chat_id} "
                                f"{Fore.YELLOW}выдан доп. аккаунт {Fore.LIGHTYELLOW_EX}{gift.get('login') or '?'}"
                            )
                        else:
                            logger.error(
                                f"{Fore.LIGHTRED_EX}Не удалось отправить доп. аккаунт в чат "
                                f"{Fore.WHITE}{chat_id}"
                            )
                except Exception as e:
                    logger.error(f"{Fore.LIGHTRED_EX}Ошибка очереди доп. аккаунтов: {Fore.WHITE}{e}")

                if data.get("initialized_users") != self.initialized_users: 
                    data.set("initialized_users", self.initialized_users)
                if data.get("saved_items") != self.saved_items: 
                    data.set("saved_items", self.saved_items)
                if data.get("cached_orders") != self.cached_orders:
                    data.set("cached_orders", self.cached_orders)
                with self.bump_lock:
                    bumped_items = dict(self.bumped_items)
                if data.get("bumped_items") != bumped_items:
                    data.set("bumped_items", bumped_items)
                with self.snipe_lock:
                    snipe_watch = copy.deepcopy(self.snipe_watch)
                if data.get("snipe_watch") != snipe_watch:
                    data.set("snipe_watch", snipe_watch)
                
                time.sleep(3)

        def refresh_account_loop():
            while True:
                time.sleep(1800)
                self.refresh_account()

        def check_banned_loop():
            while True:
                self.check_banned()
                time.sleep(900)

        def restore_expired_items_loop():
            while True:
                if self.config["playerok"]["auto_restore_items"]["expired"]:
                    self.restore_expired_items()
                time.sleep(45)

        def restore_missed_sold_items_once():
            # чат-listener при холодном старте помечает старые {{ITEM_PAID}} как processed
            time.sleep(2)
            self.restore_missed_sold_items()

        def bump_items_loop():
            while True:
                bump_config = self.config["playerok"]["auto_bump_items"]
                if bump_config["enabled"]:
                    if bump_config["mode"] == "position":
                        position = get_current_bump_position(self.config)
                        if (
                            position > 0
                            and self._do_call_event(
                                bump_config["last_check_time"],
                                bump_config["check_interval"] or 120
                            )
                        ):
                            self.check_items_positions()
                    else:
                        interval = get_current_bump_interval(self.config)
                        if interval > 0:
                            self.bump_scheduled_once(interval)
                time.sleep(3)

        def snipe_items_loop():
            while True:
                try:
                    snipe_config = (self.config.get("playerok") or {}).get("auto_snipe") or {}
                    if snipe_config.get("enabled"):
                        self.process_pending_snipes()
                        interval = int(snipe_config.get("check_interval") or 45)
                        if self._do_call_event(
                            snipe_config.get("last_check_time") or "",
                            max(15, interval),
                        ):
                            self.check_snipe_targets()
                except Exception as e:
                    logger.error(f"{Fore.LIGHTRED_EX}Ошибка цикла перебива: {Fore.WHITE}{e}")
                time.sleep(3)

        def withdrawal_loop():
            while True:
                if (
                    self.config["playerok"]["auto_withdrawal"]["enabled"]
                    and self._do_call_event(
                        self.config["playerok"]["auto_withdrawal"]["last_time"],
                        self.config["playerok"]["auto_withdrawal"]["interval"]
                    )
                ):
                    self.request_withdrawal()
                time.sleep(3)

        Thread(target=endless_loop, daemon=True).start()
        Thread(target=refresh_account_loop, daemon=True).start()
        Thread(target=check_banned_loop, daemon=True).start()
        Thread(target=restore_expired_items_loop, daemon=True).start()
        Thread(target=restore_missed_sold_items_once, daemon=True).start()
        Thread(target=bump_items_loop, daemon=True).start()
        Thread(target=snipe_items_loop, daemon=True).start()
        Thread(target=withdrawal_loop, daemon=True).start()

    async def _on_new_message(self, event: NewMessageEvent):
        if not event.message.user:
            return
        
        self.log_new_message(event.message, event.chat)
        
        if event.message.user.id == self.account.id:
            return

        is_support_chat = event.chat.id in (self.account.system_chat_id, self.account.support_chat_id)
        if (
            self.config["playerok"]["notifications"]["enabled"]
            and (self.config["playerok"]["notifications"]["events"]["new_user_message"] 
            or self.config["playerok"]["notifications"]["events"]["new_system_message"])
        ):
            if (
                self.config["playerok"]["notifications"]["events"]["new_user_message"] 
                and not is_support_chat
            ) or (
                self.config["playerok"]["notifications"]["events"]["new_system_message"] 
                and is_support_chat
            ): 
                text = event.message.text or ""
                
                if event.message.images:
                    imgs = []
                    for i, file in enumerate(event.message.images, start=1):
                        imgs.append(f'<a href="{file.url}">*Изображение {i}*</a>')
                    text = ", ".join(imgs) + ((" " + text) if text else "")

                text = text or f"<i>Без сообщения</i>"
                
                self.log_to_tg(
                    log_text(
                        f'💬 Новое сообщение в <a href="https://playerok.com/chats/{event.chat.id}">чате</a>', 
                        f"<b>{event.message.user.username}:</b> <blockquote>{text.strip()}</blockquote>"
                    ),
                    log_new_mess_kb(event.chat.id)
                )

        if (
            not is_support_chat
            and event.message.text is not None
        ):
            if event.message.user.id not in self.initialized_users:
                self.initialized_users.append(event.message.user.id)
            
            elif event.message.text.lower().strip() in ("!ошибка", "ошибка"):
                case = None
                try:
                    from error_cases import register_error
                    case = register_error(
                        event.message.user.username,
                        event.chat.id,
                    )
                except Exception as e:
                    logger.error(f"{Fore.LIGHTRED_EX}Не удалось сохранить ошибку: {Fore.WHITE}{e}")
                asyncio.run_coroutine_threadsafe(
                    get_telegram_bot().call_seller(
                        event.message.user.username,
                        event.chat.id,
                        case_id=(case or {}).get("id") or "",
                    ),
                    get_telegram_bot_loop()
                )
                self.send_message(event.chat.id, self.msg(
                    "cmd_seller", 
                    user=msg_user_from_chat(event.chat),
                    chat=msg_chat(event.chat)
                ))
            
            elif event.message.text.lower() in [key.lower() for key in self.custom_commands.keys()]:
                msg = self._format_msg_text(
                    "\n".join(self.custom_commands[event.message.text]),
                    account=msg_account(self.account.profile),
                    user=msg_user_from_chat(event.chat),
                    chat=msg_chat(event.chat)
                )
                self.send_message(event.chat.id, msg)

    async def _on_new_review(self, event: NewReviewEvent):
        if event.deal.user.id == self.account.id:
            return
        
        self.log_new_review(event.deal)
        
        if (
            self.config["playerok"]["notifications"]["enabled"] 
            and self.config["playerok"]["notifications"]["events"]["new_review"]
        ):
            text_frmtd = f"<blockquote>{event.deal.review.text}</blockquote>" if event.deal.review.text else "<i>Без текста</i>"
            self.log_to_tg(
                log_text(
                    f'🌟 Новый отзыв в <a href="https://playerok.com/deal/{event.deal.id}">сделке</a>', 
                    (f"<b>👤 Оставил:</b> {event.deal.review.creator.username}"
                    f"\n<b>✨ Оценка:</b> {'⭐' * event.deal.review.rating}"
                    f"\n<b>🏷️ Текст:</b> {text_frmtd}")
                ),
                log_new_mess_kb(event.chat.id)
            )

        self.send_message(event.chat.id, self.msg(
            "new_review", 
            user=msg_user_from_chat(event.chat),
            chat=msg_chat(event.chat),
            deal=msg_deal(event.deal),
            item=msg_item(event.deal.item),
            review_rating=event.deal.review.rating
        ))

    async def _on_new_problem(self, event: ItemPaidEvent):
        if event.deal.user.id == self.account.id:
            return

        self.log_new_problem(event.deal)
        self.send_message(event.chat.id, self.msg(
            "deal_has_problem", 
            user=msg_user_from_chat(event.chat),
            chat=msg_chat(event.chat),
            deal=msg_deal(event.deal),
            item=msg_item(event.deal.item)
        ))

        reserve_note = ""
        try:
            if self.mark_stock_need_reserve(event.deal.item):
                reserve_note = "\n<b>Следующая выдача будет из резерва.</b>"
        except Exception as e:
            logger.error(f"{Fore.LIGHTRED_EX}Не удалось отметить резерв по проблеме: {Fore.WHITE}{e}")

        if (
            self.config["playerok"]["notifications"]["enabled"] 
            and self.config["playerok"]["notifications"]["events"]["new_problem"]
        ):
            self.log_to_tg(
                log_text(
                    f'🤬 Новая проблема в <a href="https://playerok.com/deal/{event.deal.id}">сделке</a>', 
                    (f"<b>👤 Покупатель:</b> {event.deal.user.username}"
                    f"\n<b>🛍️ Товар:</b> {event.deal.item.name}"
                    f"{reserve_note}")
                ),
                log_new_problem_kb(event.chat.id)
            )

    async def _on_deal_confirmed(self, event: DealConfirmedEvent):
        if event.deal.user.id == self.account.id:
            return
        
        self.send_message(event.chat.id, self.msg(
            "deal_confirmed", 
            user=msg_user_from_chat(event.chat),
            chat=msg_chat(event.chat),
            deal=msg_deal(event.deal),
            item=msg_item(event.deal.item)
        ))

    async def _on_deal_rolled_back(self, event: DealRolledBackEvent):
        if event.deal.user.id == self.account.id:
            return
        
        self.send_message(event.chat.id, self.msg(
            "deal_refunded", 
            user=msg_user_from_chat(event.chat),
            chat=msg_chat(event.chat),
            deal=msg_deal(event.deal),
            item=msg_item(event.deal.item)
        ))

    async def _on_new_deal(self, event: NewDealEvent):
        if event.deal.user.id == self.account.id:
            return
            
        try: event.deal.item = self.account.get_item(event.deal.item.id)
        except: pass

        self.cached_orders[event.deal.id] = {
            "id": event.deal.id,
            "price": event.deal.item.price,
            "status": event.deal.status.name,
            "date": datetime.now(pytz.timezone("Europe/Moscow")).isoformat(),
            "item_id": event.deal.item.id,
            "item_name": event.deal.item.name
        }
        
        self.log_new_deal(event.deal)
        if (
            self.config["playerok"]["notifications"]["enabled"] 
            and self.config["playerok"]["notifications"]["events"]["new_deal"]
        ):
            self.log_to_tg(
                log_text(
                    f'📋 Новая <a href="https://playerok.com/deal/{event.deal.id}">сделка</a>', 
                    (f"<b>👤 Покупатель:</b> {event.deal.user.username}"
                    f"\n<b>🛍️ Товар:</b> {(event.deal.item.name or '-')}"
                    f"\n<b>💰 Сумма:</b> {event.deal.item.price or '?'}₽")
                ),
                log_new_deal_kb(event.chat.id, event.deal.id)
            )

        self.send_message(event.chat.id, self.msg(
            "new_deal", 
            user=msg_user_from_chat(event.chat),
            chat=msg_chat(event.chat),
            deal=msg_deal(event.deal),
            item=msg_item(event.deal.item)
        ))
        
        is_support_chat = event.chat.id in (self.account.system_chat_id, self.account.support_chat_id)
        if not is_support_chat:
            first_contact = event.deal.user.id not in self.initialized_users
            self.send_deal_greeting(
                event.chat, event.deal, event.deal.item, first_contact=first_contact
            )
            if first_contact:
                self.initialized_users.append(event.deal.user.id)
                
        for i, auto_delivery in enumerate(list(self.auto_deliveries)):
            if not item_matches_binding(event.deal.item, auto_delivery):
                continue

            piece = auto_delivery.get("piece", False)
            if piece:
                goods = auto_delivery.get("goods", [])
                try: good = goods[0]
                except: continue

                mess = self.send_message(event.chat.id, good)
                if mess:
                    logger.info(
                        f"{Fore.YELLOW}Покупателю {Fore.LIGHTYELLOW_EX}{event.deal.user.username or '?'} "
                        f"{Fore.YELLOW}выдан товар {Fore.LIGHTYELLOW_EX}«{good}»{Fore.YELLOW}. "
                        f"Остаток: {Fore.LIGHTYELLOW_EX}{len(goods)-1}"
                    )
                    self.auto_deliveries[i]["goods"].pop(goods.index(good))
                    sett.set("auto_deliveries", self.auto_deliveries)
            else:
                msg = auto_delivery.get("message", "")
                if msg:
                    mess = self.send_message(event.chat.id, "\n".join(msg))
                    if mess:
                        logger.info(
                            f"{Fore.YELLOW}Покупателю {Fore.LIGHTYELLOW_EX}{event.deal.user.username or '?'} "
                            f"{Fore.YELLOW}отправлено сообщение авто-выдачи {Fore.LIGHTYELLOW_EX}«{' '.join(msg)}»"
                        )
        
        if self.config["playerok"]["auto_complete_deals"]["enabled"]:
            if not event.deal.item.name:
                try: event.deal.item = self.account.get_item(event.deal.item.id)
                except: return

            included = item_matches_any_binding(event.deal.item, self.auto_complete_deals["included"])
            excluded = item_matches_any_binding(event.deal.item, self.auto_complete_deals["excluded"])

            if (
                self.config["playerok"]["auto_complete_deals"]["all"]
                and not excluded
            ) or (
                not self.config["playerok"]["auto_complete_deals"]["all"]
                and included
            ):
                self.account.update_deal(event.deal.id, ItemDealStatuses.SENT)
                logger.info(
                    f"{Fore.YELLOW}Сделка подтверждена автоматически "
                    f"{Fore.WHITE}(https://playerok.com/deal/{event.deal.id})"
                )

    async def _on_item_paid(self, event: ItemPaidEvent):
        if event.deal.user.id == self.account.id:
            return
        
        if self.config["playerok"]["auto_restore_items"]["sold"]:
            if not event.deal.item.name:
                event.deal.item = self.account.get_item(event.deal.item.id)
                time.sleep(1)
            
            for _ in range(3):
                try: 
                    items = self.get_my_items(count=12, statuses=[ItemStatuses.SOLD])
                    
                    item = next((
                        i for i in items 
                        if i.name == event.deal.item.name
                        and (event.deal.item.priority and i.priority == event.deal.item.priority)
                    ), None)
                    if not item:
                        raise

                    break
                except: 
                    time.sleep(4)
            else:
                return
            
            self.restore_item(item)

    async def _on_item_sent(self, event: ItemSentEvent):
        if event.deal.user.id == self.account.id:
            return
        
        self.send_message(event.chat.id, self.msg(
            "deal_sent", 
            user=msg_user_from_chat(event.chat),
            chat=msg_chat(event.chat),
            deal=msg_deal(event.deal),
            item=msg_item(event.deal.item)
        ))

    async def _on_deal_status_changed(self, event: DealStatusChangedEvent):
        if event.deal.user.id == self.account.id:
            return
        
        if event.deal.status and event.deal.id in self.cached_orders:
            self.cached_orders[event.deal.id]["status"] = event.deal.status.name
        
        status_frmtd = "Неизвестный"
        if event.deal.status is ItemDealStatuses.PAID: 
            status_frmtd = "Оплачено"
        elif event.deal.status is ItemDealStatuses.PENDING: 
            status_frmtd = "В ожидании"
        elif event.deal.status is ItemDealStatuses.SENT: 
            status_frmtd = "Товар отправлен"
        elif event.deal.status is ItemDealStatuses.CONFIRMED: 
            status_frmtd = "Выполнено"
        elif event.deal.status is ItemDealStatuses.ROLLED_BACK: 
            status_frmtd = "Возврат"

        self.log_deal_status_changed(event.deal, status_frmtd)
        if (
            self.config["playerok"]["notifications"]["enabled"] 
            and self.config["playerok"]["notifications"]["events"]["deal_status_changed"]
        ):
            self.log_to_tg(
                log_text(f'🔃 Статус <a href="https://playerok.com/deal/{event.deal.id}/">сделки</a> изменился на «{status_frmtd}»'),
                log_deal_kb(event.deal.id)
            )


    async def run_bot(self):
        account_pid = acquire_account_lock(self.account.id)
        if account_pid is not None:
            logger.info("")
            logger.error(f"{Fore.LIGHTRED_EX}Бот на этом аккаунте Playerok уже запущен!")
            logger.error(f"{Fore.WHITE}Аккаунт {Fore.LIGHTWHITE_EX}{self.account.username} {Fore.WHITE}уже обслуживает другой бот"
                         f"{f' (процесс {account_pid})' if account_pid else ''} — один аккаунт, один бот.")
            logger.error(f"{Fore.WHITE}Закройте это окно: если оставить два бота на одном аккаунте, заказы и сообщения будут обрабатываться дважды.")
            logger.error(f"{Fore.LIGHTBLACK_EX}Если бот на самом деле не запущен — завершите зависший процесс python и попробуйте снова.")
            logger.info("")
            sys.exit(1)

        logger.info("")
        logger.info(f"{Fore.YELLOW}Playerok бот запущен и активен")
        logger.info("")
        
        logger.info(f"{Fore.YELLOW}───────────────────────────────────────")
        logger.info(f"{Fore.YELLOW}Информация об аккаунте:")
        logger.info(f" · ID: {Fore.LIGHTWHITE_EX}{self.account.id}")
        logger.info(f" · Никнейм: {Fore.LIGHTWHITE_EX}{self.account.username}")

        profile = self.account.profile
        if profile.balance:
            logger.info(f" · Баланс: {Fore.LIGHTWHITE_EX}{profile.balance.value}₽")
            logger.info(f"   · Доступно: {Fore.LIGHTWHITE_EX}{profile.balance.available}₽")
            logger.info(f"   · В ожидании: {Fore.LIGHTWHITE_EX}{profile.balance.pending_income}₽")
            logger.info(f"   · Заморожено: {Fore.LIGHTWHITE_EX}{profile.balance.frozen}₽")
        
        logger.info(f" · Активные продажи: {Fore.LIGHTWHITE_EX}{profile.stats.deals.outgoing.total - profile.stats.deals.outgoing.finished}")
        logger.info(f" · Активные покупки: {Fore.LIGHTWHITE_EX}{profile.stats.deals.incoming.total - profile.stats.deals.incoming.finished}")
        logger.info(f"{Fore.YELLOW}───────────────────────────────────────")
        self.save_account_snapshot()
        
        proxy = self.config["playerok"]["api"]["proxy"]
        if proxy:
            if "@" in proxy:
                user, password = proxy.split("@")[0].split(":")
                ip, port = proxy.split("@")[1].split(":")
            else:
                user, password = None, None
                ip, port = proxy.split(":")
            
            ip = ".".join([("*" * len(nums)) if i >= 3 else nums for i, nums in enumerate(ip.split("."), start=1)])
            port = f"{port[:3]}**"
            user = f"{user[:3]}*****" if user else "-"
            password = f"{password[:3]}*****" if password else "-"

            logger.info("")
            logger.info(f"{Fore.YELLOW}───────────────────────────────────────")
            logger.info(f"{Fore.YELLOW}Информация о прокси:")
            logger.info(f" · IP: {Fore.LIGHTWHITE_EX}{ip}:{port}")
            logger.info(f" · Юзер: {Fore.LIGHTWHITE_EX}{user}")
            logger.info(f" · Пароль: {Fore.LIGHTWHITE_EX}{password}")
            logger.info(f"{Fore.YELLOW}───────────────────────────────────────")
            logger.info("")

        add_bot_event_handler("ON_PLAYEROK_BOT_INIT", PlayerokBot._on_playerok_bot_init, 0)
        add_playerok_event_handler(EventTypes.NEW_MESSAGE, PlayerokBot._on_new_message, 0)
        add_playerok_event_handler(EventTypes.NEW_REVIEW, PlayerokBot._on_new_review, 0)
        add_playerok_event_handler(EventTypes.NEW_DEAL, PlayerokBot._on_new_deal, 0)
        add_playerok_event_handler(EventTypes.ITEM_PAID, PlayerokBot._on_item_paid, 0)
        add_playerok_event_handler(EventTypes.ITEM_SENT, PlayerokBot._on_item_sent, 0)
        add_playerok_event_handler(EventTypes.DEAL_HAS_PROBLEM, PlayerokBot._on_new_problem, 0)
        add_playerok_event_handler(EventTypes.DEAL_CONFIRMED, PlayerokBot._on_deal_confirmed, 0)
        add_playerok_event_handler(EventTypes.DEAL_ROLLED_BACK, PlayerokBot._on_deal_rolled_back, 0)
        add_playerok_event_handler(EventTypes.DEAL_STATUS_CHANGED, PlayerokBot._on_deal_status_changed, 0)

        async def listener_loop():
            listener = EventListener(self.account)
            for event in listener.listen():
                await call_playerok_event(event.type, [self, event])

        run_async_in_thread(listener_loop)
        await call_bot_event("ON_PLAYEROK_BOT_INIT", [self])