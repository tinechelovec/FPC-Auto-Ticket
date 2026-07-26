from __future__ import annotations

import base64
import copy
import hashlib
import html
import io
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import urllib.request
from contextlib import suppress
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from telebot.apihelper import ApiTelegramException
from telebot.types import InlineKeyboardButton as B
from telebot.types import InlineKeyboardMarkup as K

import tg_bot.CBT as CBT

NAME = "Auto Ticket"
VERSION = "1.0.0"
DESCRIPTION = ""
CREDITS = "@tinechelovec"
UUID = "741dfd61-b890-4af7-91bf-021cbe421b66"
SETTINGS_PAGE = False

CREATOR_URL = "https://t.me/tinechelovec"
GROUP_URL = "https://t.me/dev_thc_chat"
CHANNEL_URL = "https://t.me/by_thc"
CHANNEL_MESSAGES_URL = "https://t.me/by_thc?direct"
GITHUB_URL = "https://github.com/tinechelovec/FPC-Auto-Ticket"
INSTRUCTION_URL = "https://teletype.in/@tinechelovec/Auto-Ticket"
ONLINE_UPDATE_URL = "https://raw.githubusercontent.com/tinechelovec/FPC-Auto-Ticket/main/AutoTicket.py"

PLUGIN_DIR = Path("storage/plugins/AutoTicket")
SETTINGS_FILE = PLUGIN_DIR / "settings.json"
SETTINGS_BACKUP = PLUGIN_DIR / "settings.json.bak"
ORDERS_FILE = PLUGIN_DIR / "orders.json"
ORDERS_BACKUP = PLUGIN_DIR / "orders.json.bak"
LOG_FILE = PLUGIN_DIR / "log.txt"

IO_CHAT_COMPLETIONS_URL = "https://api.intelligence.io.solutions/api/v1/chat/completions"
IO_MODELS_URL = "https://api.intelligence.io.solutions/api/v1/models"
MAX_TICKET_CHARS = 10000
AI_MODELS_PER_PAGE = 7
IO_MODEL_FALLBACKS = [
    "meta-llama/Llama-3.3-70B-Instruct",
    "deepseek-ai/DeepSeek-R1-0528",
    "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
    "gpt-oss-120b",
    "gpt-oss-20b",
    "Qwen3-Next-80B-A3B-Instruct",
    "mistralai/Mistral-Large-Instruct-2411",
    "Mistral-Nemo-Instruct-2407",
    "zai-org/GLM-4.7",
    "moonshotai/Kimi-K2-Instruct-0905",
]
PLUGIN_DIR.mkdir(parents=True, exist_ok=True)

LOGGER_PREFIX = "[AUTO TICKET]"
logger = logging.getLogger("FPC.AutoTicket")
logger.setLevel(logging.INFO)
if not any(isinstance(item, RotatingFileHandler) and getattr(item, "baseFilename", "") == str(LOG_FILE.resolve()) for item in logger.handlers):
    try:
        handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=4, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
    except Exception:
        pass
logger.propagate = True

DEFAULT_SINGLE_TEMPLATE = (
    "Здравствуйте!\n\n"
    "Прошу подтвердить выполнение следующих заказов:\n"
    "{orders}\n\n"
    "Заранее благодарю.\n"
    "С уважением, {username}."
)
DEFAULT_EASY_TEMPLATE = (
    "Заказы, которые нужно подтвердить:\n"
    "{orders}"
)
DEFAULT_HARD_TEMPLATE = (
    "Заказы, по которым нужна проверка поддержки:\n"
    "{orders}\n"
    "Пожалуйста, проверьте обстоятельства по этим заказам."
)

DEFAULT_SETTINGS: Dict[str, Any] = {
    "schema": 3,
    "plugin_enabled": True,
    "owner_chat_id": None,
    "auto_fetch_phpsessid": True,
    "phpsessid": "",
    "message_template": DEFAULT_SINGLE_TEMPLATE,
    "easy_template": DEFAULT_EASY_TEMPLATE,
    "hard_template": DEFAULT_HARD_TEMPLATE,
    "classification_mode": "none",
    "local_hard_keywords": [
        "спор", "проблем", "ошиб", "не работает", "возврат", "жалоб",
        "заблок", "не получил", "не приш", "обман", "отмен", "refund",
        "dispute", "error", "failed", "blocked",
    ],
    "ai_api_key": "",
    "ai_model": "meta-llama/Llama-3.3-70B-Instruct",
    "scan_interval_hours": 1,
    "send_interval_hours": 24,
    "order_age_hours": 24,
    "max_orders_in_ticket": 650,
    "notify_enabled": True,
    "startup_action": "continue",
    "next_scan_at": 0,
    "next_send_at": 0,
    "last_scan_at": 0,
    "last_send_at": 0,
    "online_update_url": ONLINE_UPDATE_URL,
}

_IO_LOCK = threading.RLock()
_SETTINGS_LOCK = threading.RLock()
_ORDERS_LOCK = threading.RLock()
_FSM_LOCK = threading.RLock()
_RUN_LOCK = threading.Lock()
_SETTINGS: Dict[str, Any] = {}
_ORDERS: Dict[str, Dict[str, Any]] = {}
_FSM: Dict[int, Dict[str, Any]] = {}
_CARDINAL: Any = None
_STOP_EVENT = threading.Event()
_BACKGROUND_THREAD: Optional[threading.Thread] = None
_AI_MODELS_LOCK = threading.RLock()
_AI_MODELS_CACHE: List[str] = []
_AI_MODELS_CACHE_AT = 0.0
_AI_MODEL_LISTS: Dict[int, List[str]] = {}

def _h(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)

def _short_error(value: Any, limit: int = 260) -> str:
    return re.sub(r"\s+", " ", str(value or "неизвестная ошибка")).strip()[:limit]

def _log_event(event: str, level: int = logging.INFO, **details: Any) -> None:
    parts = [f"СОБЫТИЕ={str(event).upper()}"]
    for key, value in details.items():
        if value in (None, "", [], ()):
            continue
        clean = re.sub(r"\s+", " ", str(value)).strip()
        parts.append(f"{key}={clean[:500]}")
    logger.log(level, "%s %s", LOGGER_PREFIX, " | ".join(parts))

def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)

def _load_json(path: Path, default: Any, backup: Optional[Path] = None) -> Any:
    with _IO_LOCK:
        for candidate in (path, backup):
            if not candidate or not candidate.exists():
                continue
            try:
                with candidate.open("r", encoding="utf-8") as handle:
                    return json.load(handle)
            except Exception:
                logger.exception("%s Не удалось прочитать %s", LOGGER_PREFIX, candidate)
        return copy.deepcopy(default)

def _save_json(path: Path, payload: Any, backup: Optional[Path] = None) -> None:
    with _IO_LOCK:
        if backup and path.exists():
            with suppress(Exception):
                shutil.copy2(path, backup)
        _atomic_json(path, payload)

def _merge_settings(raw: Any) -> Dict[str, Any]:
    result = copy.deepcopy(DEFAULT_SETTINGS)
    if isinstance(raw, dict):
        for key in result:
            if key in raw:
                result[key] = raw[key]
        if "auto_fetch_phpsessid" not in raw and "auth_mode" in raw:
            result["auto_fetch_phpsessid"] = str(raw.get("auth_mode")) == "auto"

    if result.get("classification_mode") not in {"none", "local", "ai"}:
        result["classification_mode"] = "none"
    if result.get("startup_action") not in {"continue", "send_now"}:
        result["startup_action"] = "continue"

    for key in ("plugin_enabled", "notify_enabled", "auto_fetch_phpsessid"):
        result[key] = bool(result.get(key))

    numeric_limits = {
        "scan_interval_hours": (1, 720),
        "send_interval_hours": (1, 720),
        "order_age_hours": (1, 2160),
        "max_orders_in_ticket": (1, 650),
    }
    for key, (minimum, maximum) in numeric_limits.items():
        try:
            value = int(result.get(key))
            if not minimum <= value <= maximum:
                raise ValueError(key)
            result[key] = value
        except (TypeError, ValueError, OverflowError):
            result[key] = DEFAULT_SETTINGS[key]

    for key in ("next_scan_at", "next_send_at", "last_scan_at", "last_send_at"):
        try:
            result[key] = float(result.get(key) or 0)
        except (TypeError, ValueError):
            result[key] = 0

    for key in ("phpsessid", "ai_api_key", "ai_model"):
        result[key] = str(result.get(key) or "").strip()
    result["online_update_url"] = ONLINE_UPDATE_URL

    for key, fallback in (
        ("message_template", DEFAULT_SINGLE_TEMPLATE),
        ("easy_template", DEFAULT_EASY_TEMPLATE),
        ("hard_template", DEFAULT_HARD_TEMPLATE),
    ):
        value = str(result.get(key) or "").strip()
        result[key] = value or fallback

    keywords = result.get("local_hard_keywords")
    if not isinstance(keywords, list):
        keywords = copy.deepcopy(DEFAULT_SETTINGS["local_hard_keywords"])
    result["local_hard_keywords"] = [str(item).strip().lower() for item in keywords if str(item).strip()][:200]
    result["schema"] = DEFAULT_SETTINGS["schema"]
    return result

def _load_state() -> None:
    global _SETTINGS, _ORDERS
    with _SETTINGS_LOCK:
        _SETTINGS = _merge_settings(_load_json(SETTINGS_FILE, DEFAULT_SETTINGS, SETTINGS_BACKUP))
        now = time.time()
        if not _SETTINGS["next_scan_at"]:
            _SETTINGS["next_scan_at"] = now
        if not _SETTINGS["next_send_at"]:
            _SETTINGS["next_send_at"] = now + _SETTINGS["send_interval_hours"] * 3600
        _save_json(SETTINGS_FILE, _SETTINGS, SETTINGS_BACKUP)
    raw_orders = _load_json(ORDERS_FILE, {}, ORDERS_BACKUP)
    with _ORDERS_LOCK:
        _ORDERS = raw_orders if isinstance(raw_orders, dict) else {}

def _cfg() -> Dict[str, Any]:
    with _SETTINGS_LOCK:
        return copy.deepcopy(_SETTINGS)

def _set_cfg(**updates: Any) -> Dict[str, Any]:
    global _SETTINGS
    with _SETTINGS_LOCK:
        current = copy.deepcopy(_SETTINGS)
        current.update(updates)
        _SETTINGS = _merge_settings(current)
        _save_json(SETTINGS_FILE, _SETTINGS, SETTINGS_BACKUP)
        return copy.deepcopy(_SETTINGS)

def _save_orders() -> None:
    with _ORDERS_LOCK:
        _save_json(ORDERS_FILE, _ORDERS, ORDERS_BACKUP)

def _order_record(order_id: Any) -> Dict[str, Any]:
    key = str(order_id or "").lstrip("#").upper()
    with _ORDERS_LOCK:
        return copy.deepcopy(_ORDERS.get(key) or {})

def _update_order(order_id: Any, **updates: Any) -> Dict[str, Any]:
    key = str(order_id or "").lstrip("#").upper()
    if not key:
        return {}
    with _ORDERS_LOCK:
        record = dict(_ORDERS.get(key) or {})
        record.update(updates)
        record["order_id"] = key
        record["updated_at"] = int(time.time())
        _ORDERS[key] = record
        _save_json(ORDERS_FILE, _ORDERS, ORDERS_BACKUP)
        return copy.deepcopy(record)

def _bulk_update_orders(patches: Sequence[Tuple[str, Dict[str, Any]]]) -> None:
    if not patches:
        return
    now = int(time.time())
    with _ORDERS_LOCK:
        for raw_id, updates in patches:
            key = str(raw_id or "").lstrip("#").upper()
            if not key:
                continue
            record = dict(_ORDERS.get(key) or {})
            record.update(updates)
            record["order_id"] = key
            record["updated_at"] = now
            _ORDERS[key] = record
        _save_json(ORDERS_FILE, _ORDERS, ORDERS_BACKUP)

def _bool_label(value: Any) -> str:
    return "🟢 Включено" if bool(value) else "🔴 Выключено"

def _masked(value: str, visible: int = 5) -> str:
    value = str(value or "")
    if not value:
        return "не задан"
    if len(value) <= visible:
        return "•" * len(value)
    return value[:visible] + "•" * min(18, len(value) - visible)

def _format_dt(timestamp: Any) -> str:
    try:
        value = float(timestamp)
        if value <= 0:
            return "не было"
        return datetime.fromtimestamp(value).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "неизвестно"

def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"

def _message_id(message: Any) -> int:
    return int(getattr(message, "message_id", None) or getattr(message, "id", 0) or 0)

def _safe_edit(bot: Any, call: Any, text: str, keyboard: Optional[K] = None) -> None:
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            _message_id(call.message),
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except ApiTelegramException as exc:
        if "message is not modified" not in str(exc).lower():
            bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)
    except Exception:
        logger.debug("%s Не удалось изменить сообщение меню", LOGGER_PREFIX, exc_info=True)

def _edit_by_id(bot: Any, chat_id: int, message_id: int, text: str, keyboard: Optional[K] = None) -> None:
    try:
        bot.edit_message_text(
            text, chat_id, message_id, parse_mode="HTML", reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception:
        bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)

def _answer(bot: Any, call: Any, text: str = "", alert: bool = False) -> None:
    with suppress(Exception):
        bot.answer_callback_query(call.id, text=text, show_alert=alert)

def _notify(text: str) -> None:
    settings = _cfg()
    if not settings.get("notify_enabled") or _CARDINAL is None:
        return
    chat_id = settings.get("owner_chat_id")
    if not chat_id:
        return
    with suppress(Exception):
        _CARDINAL.telegram.bot.send_message(chat_id, text, parse_mode="HTML")

class SupportAPIError(Exception):
    pass

class AuthenticationError(SupportAPIError):
    pass

def _extract_phpsessid(account: Any) -> str:
    session = requests.Session()
    if getattr(account, "user_agent", None):
        session.headers["User-Agent"] = account.user_agent
    timeout = getattr(account, "requests_timeout", 20)
    golden_key = str(getattr(account, "golden_key", "") or "")
    if not golden_key:
        raise AuthenticationError("golden_key отсутствует")

    response = session.get(
        "https://funpay.com/support/sso?return_to=%2Ftickets%2Fnew",
        headers={"cookie": f"golden_key={golden_key}; cookie_prefs=1"},
        allow_redirects=False,
        timeout=timeout,
    )
    if response.status_code not in (301, 302, 303, 307, 308):
        raise AuthenticationError(f"SSO вернул HTTP {response.status_code}")
    jwt_url = response.headers.get("Location", "")
    if not jwt_url:
        raise AuthenticationError("SSO не вернул адрес авторизации")
    if not jwt_url.startswith("http"):
        jwt_url = urljoin("https://funpay.com", jwt_url)
    if "jwt=" not in jwt_url:
        raise AuthenticationError("JWT не найден в ответе SSO")

    response = session.get(jwt_url, allow_redirects=False, timeout=timeout)
    for source in (response.cookies, session.cookies):
        for cookie in source:
            if cookie.name == "PHPSESSID" and cookie.value:
                _log_event("PHPSESSID_ПОЛУЧЕН", источник="SSO FunPay")
                return cookie.value
    raise AuthenticationError("PHPSESSID не получен; проверьте golden_key")

class FunPaySupportAPI:
    BASE_URL = "https://support.funpay.com"
    MAX_RETRIES = 3

    def __init__(self, account: Any):
        self.account = account
        self.session = requests.Session()
        if getattr(account, "user_agent", None):
            self.session.headers["User-Agent"] = account.user_agent
        self.phpsessid = ""
        self.csrf_token = ""

    @property
    def timeout(self) -> int:
        return int(getattr(self.account, "requests_timeout", 20) or 20)

    def _resolve_session(self, force: bool = False) -> None:
        settings = _cfg()
        saved = str(settings.get("phpsessid") or "")
        auto_fetch = bool(settings.get("auto_fetch_phpsessid"))
        if auto_fetch and (force or not saved):
            saved = _extract_phpsessid(self.account)
            _set_cfg(phpsessid=saved)
        if not saved:
            raise AuthenticationError(
                "PHPSESSID не задан; включите автоматическое получение или введите его вручную"
            )
        self.phpsessid = saved

    def _request(self, method: str, url: str, *, headers: Optional[dict] = None, data: Optional[dict] = None) -> requests.Response:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                merged = {"cookie": f"PHPSESSID={self.phpsessid}"}
                merged.update(headers or {})
                response = self.session.request(
                    method, url, headers=merged, data=data or {}, allow_redirects=False, timeout=self.timeout,
                )
                if response.is_redirect or response.status_code in (401, 403):
                    raise AuthenticationError("сессия поддержки недействительна")
                if response.status_code >= 500:
                    raise SupportAPIError(f"support.funpay.com вернул HTTP {response.status_code}")
                return response
            except AuthenticationError:
                raise
            except (requests.RequestException, SupportAPIError) as exc:
                last_error = exc
                logger.warning("%s Запрос %s, попытка %s/%s: %s", LOGGER_PREFIX, url, attempt, self.MAX_RETRIES, exc)
                if attempt < self.MAX_RETRIES:
                    time.sleep(attempt * 2)
        raise SupportAPIError(_short_error(last_error))

    def initialize(self) -> "FunPaySupportAPI":
        self._resolve_session()
        try:
            response = self._request("GET", self.BASE_URL + "/")
        except AuthenticationError:
            if not _cfg().get("auto_fetch_phpsessid"):
                raise
            self._resolve_session(force=True)
            response = self._request("GET", self.BASE_URL + "/")

        soup = BeautifulSoup(response.text, "html.parser")
        body = soup.find("body")
        raw = body.get("data-app-config") if body else None
        if not raw and _cfg().get("auto_fetch_phpsessid"):
            self._resolve_session(force=True)
            response = self._request("GET", self.BASE_URL + "/")
            soup = BeautifulSoup(response.text, "html.parser")
            body = soup.find("body")
            raw = body.get("data-app-config") if body else None
        if not raw:
            raise AuthenticationError("data-app-config не найден; PHPSESSID истёк")
        try:
            app_data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SupportAPIError("не удалось разобрать конфигурацию поддержки") from exc
        self.csrf_token = str(app_data.get("csrfToken") or "")
        if not self.csrf_token:
            raise SupportAPIError("csrfToken отсутствует")
        return self

    def _ticket_token(self) -> str:
        response = self._request(
            "GET", self.BASE_URL + "/tickets/new/1",
            headers={"X-CSRF-Token": self.csrf_token, "Referer": self.BASE_URL + "/"},
        )
        soup = BeautifulSoup(response.text, "html.parser")
        token = soup.find("input", attrs={"name": "ticket[_token]"})
        if token is None or not token.get("value"):
            raise SupportAPIError("ticket[_token] не найден")
        return str(token["value"])

    def create_ticket(self, order_ids: Sequence[str], comment: str) -> Dict[str, Any]:
        if not order_ids:
            raise SupportAPIError("список заказов пуст")
        token = self._ticket_token()
        escaped = html.escape(comment, quote=False).replace("\n", "<br>")
        payload = {
            "ticket[fields][1]": str(getattr(self.account, "username", "")),
            "ticket[fields][2]": ", ".join(str(item).lstrip("#") for item in order_ids),
            "ticket[fields][3]": "2",
            "ticket[fields][5]": "201",
            "ticket[comment][body_html]": f"<p>{escaped}</p>",
            "ticket[comment][attachments]": "",
            "ticket[_token]": token,
        }
        response = self._request(
            "POST", self.BASE_URL + "/tickets/create/1",
            headers={
                "Origin": self.BASE_URL,
                "Referer": self.BASE_URL + "/tickets/new/1",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
            data=payload,
        )
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise SupportAPIError(f"поддержка вернула не JSON, HTTP {response.status_code}") from exc

    def close(self) -> None:
        self.session.close()

def _support_response_success(response: Dict[str, Any]) -> Tuple[bool, str]:
    error = response.get("error")
    if error:
        return False, str(error)
    action = response.get("action")
    if isinstance(action, dict):
        message = str(action.get("message") or "")
        url = str(action.get("url") or "")
        if "заявка отправлена" in message.lower() or "/tickets/" in url:
            return True, url or message
    return False, "неожиданный ответ support.funpay.com"

def _datetime_from_order(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    if isinstance(value, str):
        with suppress(ValueError):
            return datetime.fromisoformat(value)
    return datetime.now()

def _first_attr(obj: Any, names: Iterable[str], default: Any = "") -> Any:
    for name in names:
        value = getattr(obj, name, None)
        if value not in (None, ""):
            return value
    return default

def _order_to_record(order: Any) -> Dict[str, Any]:
    order_id = str(getattr(order, "id", "") or "").lstrip("#").upper()
    order_date = _datetime_from_order(getattr(order, "date", None))
    product = _first_attr(order, ("description", "short_description", "subcategory_name", "title", "lot_name"), "Неизвестный товар")
    buyer = _first_attr(order, ("buyer_username", "buyer", "username"), "неизвестен")
    price = _first_attr(order, ("price", "sum", "amount", "total"), "")
    status = _first_attr(order, ("status", "state"), "paid")
    now = int(time.time())
    old = _order_record(order_id)
    record = {
        "order_id": order_id,
        "product": str(product),
        "buyer": str(buyer),
        "price": str(price),
        "status": str(status),
        "purchased_at": int(order_date.timestamp()),
        "first_seen_at": int(old.get("first_seen_at") or now),
        "last_seen_at": now,
        "is_pending": True,
        "resolved_at": 0,
        "ignored": bool(old.get("ignored", False)),
        "classification": str(old.get("classification") or ""),
        "classification_source": str(old.get("classification_source") or ""),
        "classification_reason": str(old.get("classification_reason") or ""),
        "classification_at": int(old.get("classification_at") or 0),
        "manual_classification": bool(old.get("manual_classification", False)),
        "sent_count": int(old.get("sent_count") or 0),
        "last_ticket_at": int(old.get("last_ticket_at") or 0),
        "last_ticket_result": str(old.get("last_ticket_result") or ""),
        "last_error": str(old.get("last_error") or ""),
    }
    return record

def _get_sales_page(account: Any, start_from: Optional[str], locale: Any, subcategories: Any) -> Tuple[Any, List[Any], Any, Any]:
    common = {"start_from": start_from, "state": "paid", "locale": locale}
    attempts = (
        {**common, "subcategories": subcategories},
        {**common, "sudcategories": subcategories},
        {**common},
    )
    last_error: Optional[Exception] = None
    for kwargs in attempts:
        try:
            result = account.get_sales(**kwargs)
            return result[0], list(result[1] or []), result[2], result[3]
        except TypeError as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    raise RuntimeError("get_sales не вернул данные")

def _fetch_all_paid_orders(account: Any) -> List[Any]:
    result: List[Any] = []
    start_from: Optional[str] = None
    locale = None
    subcategories = None
    seen_pages: set[str] = set()
    while True:
        page_key = str(start_from or "first")
        if page_key in seen_pages:
            logger.warning("%s Остановлена повторяющаяся пагинация get_sales", LOGGER_PREFIX)
            break
        seen_pages.add(page_key)
        page_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                next_id, orders, locale, subcategories = _get_sales_page(account, start_from, locale, subcategories)
                page_error = None
                break
            except Exception as exc:
                page_error = exc
                if attempt < 2:
                    time.sleep(attempt + 1)
        if page_error:
            raise page_error
        result.extend(orders)
        start_from = next_id
        if not start_from:
            break
        time.sleep(0.5)
    return result

def _buyer_history_summary(record: Dict[str, Any]) -> Dict[str, int]:
    buyer = str(record.get("buyer") or "").strip().lower()
    current_id = str(record.get("order_id") or "")
    if not buyer or buyer == "неизвестен":
        return {"orders_seen": 0, "ordinary": 0, "problematic": 0, "resolved": 0, "tickets_sent": 0}
    with _ORDERS_LOCK:
        matching = [
            item for key, item in _ORDERS.items()
            if key != current_id and str(item.get("buyer") or "").strip().lower() == buyer
        ]
    return {
        "orders_seen": len(matching),
        "ordinary": sum(1 for item in matching if item.get("classification") == "easy"),
        "problematic": sum(1 for item in matching if item.get("classification") == "hard"),
        "resolved": sum(1 for item in matching if not bool(item.get("is_pending", True))),
        "tickets_sent": sum(int(item.get("sent_count") or 0) for item in matching),
    }

def _local_classification(record: Dict[str, Any]) -> Tuple[str, str]:
    text = " ".join((
        str(record.get("product") or ""),
        str(record.get("status") or ""),
        str(record.get("last_error") or ""),
    )).lower()
    for keyword in _cfg().get("local_hard_keywords", []):
        if keyword and keyword in text:
            return "hard", f"найдено правило: {keyword}"
    return "easy", "локальные правила не нашли признаков сложного заказа"

def _extract_json_payload(text: str) -> Any:
    clean = str(text or "").strip()
    clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.I)
    clean = re.sub(r"\s*```$", "", clean)
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        match = re.search(r"(\[.*\]|\{.*\})", clean, flags=re.S)
        if match:
            return json.loads(match.group(1))
        raise

def _fetch_io_models(force: bool = False) -> List[str]:
    global _AI_MODELS_CACHE, _AI_MODELS_CACHE_AT
    settings = _cfg()
    api_key = str(settings.get("ai_api_key") or "")
    with _AI_MODELS_LOCK:
        if not force and _AI_MODELS_CACHE and time.time() - _AI_MODELS_CACHE_AT < 900:
            return list(_AI_MODELS_CACHE)
    if not api_key:
        models = list(IO_MODEL_FALLBACKS)
    else:
        models: List[str] = []
        page = 1
        while page <= 20:
            response = requests.get(
                IO_MODELS_URL,
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                params={"page": page, "page_size": 100},
                timeout=30,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"io.net HTTP {response.status_code}: {_short_error(response.text, 160)}")
            payload = response.json()
            raw_models = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(raw_models, list):
                raise RuntimeError("io.net не вернул список моделей")
            for item in raw_models:
                if not isinstance(item, dict):
                    continue
                metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
                if metadata.get("enable_api_chat_completions") is False:
                    continue
                model_name = str(item.get("name") or item.get("id") or "").strip()
                lowered = model_name.lower()
                if not model_name or any(token in lowered for token in ("embedding", "embed", "rerank", "bge-")):
                    continue
                models.append(model_name)
            pagination = payload.get("pagination") if isinstance(payload, dict) else {}
            if not isinstance(pagination, dict) or not pagination.get("has_next"):
                break
            page += 1
        models = sorted(set(models), key=str.lower)
        if not models:
            raise RuntimeError("в ответе io.net не найдено моделей для Chat Completions")
    current = str(settings.get("ai_model") or "")
    if current and current not in models:
        models.insert(0, current)
    with _AI_MODELS_LOCK:
        _AI_MODELS_CACHE = list(models)
        _AI_MODELS_CACHE_AT = time.time()
    return list(models)

def _ai_classify_batch(records: Sequence[Dict[str, Any]]) -> Dict[str, Tuple[str, str]]:
    settings = _cfg()
    api_key = settings.get("ai_api_key")
    if not api_key:
        raise RuntimeError("API-ключ io.net не задан")
    orders_payload = []
    for record in records:
        item = {
            "order_id": record.get("order_id"),
            "product": record.get("product"),
            "status": record.get("status"),
            "price": record.get("price"),
            "age_hours": round((time.time() - float(record.get("purchased_at") or time.time())) / 3600, 1),
            "buyer_history": _buyer_history_summary(record),
        }
        orders_payload.append(item)

    system_prompt = (
        "Ты классификатор заказов FunPay. Для каждого заказа выбери только easy или hard. "
        "easy: заказ нужно лишь подтвердить. hard: есть признаки спора, ошибки, возврата, блокировки, "
        "неполучения товара или иной ситуации, требующей вмешательства поддержки. "
        "buyer_history содержит обезличенную локальную историю этого покупателя. "
        "Верни строго JSON-массив объектов order_id, class, reason. Не добавляй текст вне JSON."
    )
    response = requests.post(
        IO_CHAT_COMPLETIONS_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": settings.get("ai_model"),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(orders_payload, ensure_ascii=False)},
            ],
            "temperature": 0,
            "max_completion_tokens": max(200, len(records) * 80),
            "stream": False,
        },
        timeout=45,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"io.net HTTP {response.status_code}: {_short_error(response.text, 160)}")
    payload = response.json()
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        raise RuntimeError("io.net не вернул choices")
    content = choices[0].get("message", {}).get("content", "")
    parsed = _extract_json_payload(content)
    if not isinstance(parsed, list):
        raise RuntimeError("ответ io.net не является массивом")
    result: Dict[str, Tuple[str, str]] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        order_id = str(item.get("order_id") or "").lstrip("#").upper()
        classification = str(item.get("class") or item.get("classification") or "").lower()
        if order_id and classification in {"easy", "hard"}:
            result[order_id] = (classification, _short_error(item.get("reason") or "решение ИИ", 180))
    return result

def _classify_records(records: Sequence[Dict[str, Any]], force: bool = False) -> None:
    settings = _cfg()
    mode = settings.get("classification_mode")
    pending: List[Dict[str, Any]] = []
    for record in records:
        current_source = str(record.get("classification_source") or "")
        if not force and record.get("classification") in {"easy", "hard"}:
            if mode == "none" and current_source == "none":
                continue
            if mode == "local" and current_source == "local":
                continue
            if mode == "ai" and current_source == "ai":
                continue
        pending.append(record)

    if not pending:
        return
    now = int(time.time())
    patches: List[Tuple[str, Dict[str, Any]]] = []
    if mode == "none":
        for record in pending:
            patches.append((record["order_id"], {
                "classification": "easy", "classification_source": "none",
                "classification_reason": "разделение отключено", "classification_at": now,
                "manual_classification": False,
            }))
        _bulk_update_orders(patches)
        _log_event("КЛАССИФИКАЦИЯ", режим=mode, обработано=len(patches), обычных=len(patches), проблемных=0)
        return
    if mode == "local":
        for record in pending:
            value, reason = _local_classification(record)
            patches.append((record["order_id"], {
                "classification": value, "classification_source": "local",
                "classification_reason": reason, "classification_at": now,
                "manual_classification": False,
            }))
        _bulk_update_orders(patches)
        easy_count = sum(1 for _, patch in patches if patch.get("classification") == "easy")
        hard_count = sum(1 for _, patch in patches if patch.get("classification") == "hard")
        _log_event("КЛАССИФИКАЦИЯ", режим=mode, обработано=len(patches), обычных=easy_count, проблемных=hard_count)
        return

    batch_size = max(1, min(650, int(settings.get("max_orders_in_ticket") or 1)))
    for offset in range(0, len(pending), batch_size):
        batch = pending[offset:offset + batch_size]
        try:
            decisions = _ai_classify_batch(batch)
        except Exception as exc:
            logger.exception("%s Ошибка классификации io.net", LOGGER_PREFIX)
            decisions = {}
            fallback_error = _short_error(exc)
        else:
            fallback_error = ""
        for record in batch:
            order_id = record["order_id"]
            if order_id in decisions:
                value, reason = decisions[order_id]
                source = "ai"
            else:
                value, local_reason = _local_classification(record)
                reason = f"резервное локальное правило: {local_reason}"
                if fallback_error:
                    reason += f"; io.net: {fallback_error}"
                source = "ai_fallback"
            patches.append((order_id, {
                "classification": value, "classification_source": source,
                "classification_reason": reason, "classification_at": now,
                "manual_classification": False,
            }))
    _bulk_update_orders(patches)
    easy_count = sum(1 for _, patch in patches if patch.get("classification") == "easy")
    hard_count = sum(1 for _, patch in patches if patch.get("classification") == "hard")
    _log_event("КЛАССИФИКАЦИЯ", режим=mode, обработано=len(patches), обычных=easy_count, проблемных=hard_count)

def _scan_orders(account: Any, force_reclassify: bool = False) -> Tuple[int, int]:
    if not _auto_target_allowed():
        raise RuntimeError(_AUTHOR_META_REASON or "Auto Target запретил сканирование")
    _log_event("СКАНИРОВАНИЕ_НАЧАТО")
    orders = _fetch_all_paid_orders(account)
    records: List[Dict[str, Any]] = []
    patches: List[Tuple[str, Dict[str, Any]]] = []
    new_count = 0
    with _ORDERS_LOCK:
        known_ids = set(_ORDERS)
        previously_pending = {
            key for key, item in _ORDERS.items() if bool(item.get("is_pending", True))
        }
    current_ids: set[str] = set()
    for order in orders:
        record = _order_to_record(order)
        if not record["order_id"]:
            continue
        current_ids.add(record["order_id"])
        if record["order_id"] not in known_ids:
            new_count += 1
        patches.append((record["order_id"], {k: v for k, v in record.items() if k != "order_id"}))
        records.append(record)
    resolved_ids = sorted(previously_pending - current_ids)
    now_int = int(time.time())
    for order_id in resolved_ids:
        patches.append((order_id, {"is_pending": False, "resolved_at": now_int}))
    _bulk_update_orders(patches)
    if force_reclassify:
        _classify_records(records, force=True)
    now = time.time()
    _set_cfg(last_scan_at=now, next_scan_at=now + _cfg()["scan_interval_hours"] * 3600)
    _log_event(
        "СКАНИРОВАНИЕ_ЗАВЕРШЕНО",
        заказов=len(records), новых=new_count, завершённых=len(resolved_ids),
    )
    return len(records), new_count

def _all_records() -> List[Dict[str, Any]]:
    with _ORDERS_LOCK:
        records = [copy.deepcopy(item) for item in _ORDERS.values()]
    return sorted(records, key=lambda item: (int(item.get("purchased_at") or 0), str(item.get("order_id") or "")), reverse=True)

def _eligible_records() -> List[Dict[str, Any]]:
    settings = _cfg()
    cutoff = time.time() - settings["order_age_hours"] * 3600
    result = []
    for record in _all_records():
        if record.get("ignored"):
            continue
        if not bool(record.get("is_pending", True)):
            continue
        if float(record.get("purchased_at") or time.time()) > cutoff:
            continue
        if int(record.get("sent_count") or 0) > 0:
            continue
        result.append(record)
    return sorted(result, key=lambda item: int(item.get("purchased_at") or 0))

def _ignored_records() -> List[Dict[str, Any]]:
    return [item for item in _all_records() if item.get("ignored")]

def _format_orders(records: Sequence[Dict[str, Any]]) -> str:
    return ", ".join(f"#{item['order_id']}" for item in records)

def _render_template(template: str, records: Sequence[Dict[str, Any]], username: str, classification: str = "") -> str:
    values = {
        "orders": _format_orders(records),
        "username": username,
        "count": str(len(records)),
        "classification": classification,
    }
    try:
        return str(template).format(**values).strip()
    except Exception as exc:
        raise ValueError(f"ошибка шаблона: {_short_error(exc)}") from exc

def _render_ticket(records: Sequence[Dict[str, Any]], username: str, classification: str = "all") -> str:
    settings = _cfg()
    if classification == "all" or settings.get("classification_mode") == "none":
        return _render_template(settings["message_template"], records, username, "all")
    template = settings["hard_template"] if classification == "hard" else settings["easy_template"]
    body = _render_template(template, records, username, classification)
    return (
        "Здравствуйте!\n\n" + body +
        f"\n\nЗаранее благодарю.\nС уважением, {username}."
    ).strip()

def _build_batches(records: Sequence[Dict[str, Any]], username: str, classification: str = "all") -> Tuple[List[List[Dict[str, Any]]], List[str]]:
    settings = _cfg()
    max_count = int(settings["max_orders_in_ticket"])
    batches: List[List[Dict[str, Any]]] = []
    errors: List[str] = []
    current: List[Dict[str, Any]] = []
    for record in records:
        candidate = current + [record]
        try:
            text = _render_ticket(candidate, username, classification)
        except Exception as exc:
            errors.append(f"#{record['order_id']}: {_short_error(exc)}")
            continue
        if len(candidate) <= max_count and len(text) <= MAX_TICKET_CHARS:
            current = candidate
            continue
        if current:
            batches.append(current)
            current = []
        try:
            single_text = _render_ticket([record], username, classification)
        except Exception as exc:
            errors.append(f"#{record['order_id']}: {_short_error(exc)}")
            continue
        if len(single_text) > MAX_TICKET_CHARS:
            errors.append(f"#{record['order_id']}: шаблон превышает {MAX_TICKET_CHARS} символов")
            continue
        current = [record]
    if current:
        batches.append(current)
    return batches, errors

def _send_record_batches(account: Any, records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    settings = _cfg()
    mode = str(settings.get("classification_mode") or "none")
    selected_ids = [str(item.get("order_id") or "") for item in records if item.get("order_id")]
    result = {
        "selected": len(records),
        "sent_ids": [],
        "easy_sent_ids": [],
        "hard_sent_ids": [],
        "all_sent_ids": [],
        "easy_selected_ids": [],
        "hard_selected_ids": [],
        "tickets": 0,
        "easy_tickets": 0,
        "hard_tickets": 0,
        "all_tickets": 0,
        "classification_mode": mode,
        "errors": [],
    }
    if not records:
        return result
    if mode in {"local", "ai"}:
        _log_event("ПРОВЕРКА_ПЕРЕД_ОТПРАВКОЙ", режим=mode, заказов=len(records), список=", ".join("#" + item for item in selected_ids))
        _classify_records(records, force=True)
        refreshed = [_order_record(order_id) for order_id in selected_ids]
        records = [item for item in refreshed if item]
        easy_records = [item for item in records if item.get("classification") != "hard"]
        hard_records = [item for item in records if item.get("classification") == "hard"]
        result["easy_selected_ids"] = [str(item["order_id"]) for item in easy_records]
        result["hard_selected_ids"] = [str(item["order_id"]) for item in hard_records]
        groups = [("easy", easy_records), ("hard", hard_records)]
        _log_event(
            "ЗАКАЗЫ_РАЗДЕЛЕНЫ",
            обычных=len(easy_records),
            обычные=", ".join("#" + item for item in result["easy_selected_ids"]),
            требуют_разбирательства=len(hard_records),
            проблемные=", ".join("#" + item for item in result["hard_selected_ids"]),
        )
    else:
        groups = [("all", list(records))]
    username = str(getattr(account, "username", "") or "")
    plans: List[Tuple[str, List[Dict[str, Any]]]] = []
    for classification, group_records in groups:
        if not group_records:
            continue
        batches, build_errors = _build_batches(group_records, username, classification)
        label = "обычные" if classification == "easy" else "требуют разбирательства" if classification == "hard" else "без разделения"
        result["errors"].extend(f"{label}: {item}" for item in build_errors)
        plans.extend((classification, batch) for batch in batches)
    if not plans:
        return result
    api = FunPaySupportAPI(account)
    try:
        try:
            api.initialize()
        except Exception as exc:
            result["errors"].append("авторизация поддержки: " + _short_error(exc))
            return result
        abort = False
        for index, (classification, batch) in enumerate(plans):
            if index:
                time.sleep(2)
            comment = _render_ticket(batch, username, classification)
            ids = [str(item["order_id"]) for item in batch]
            type_label = "обычные" if classification == "easy" else "требуют разбирательства" if classification == "hard" else "без разделения"
            _log_event(
                "ТИКЕТ_ОТПРАВКА",
                номер=index + 1,
                тип=type_label,
                заказов=len(ids),
                список=", ".join("#" + item for item in ids),
            )
            try:
                response = api.create_ticket(ids, comment)
                success, detail = _support_response_success(response)
            except Exception as exc:
                success, detail = False, _short_error(exc)
                logger.exception("%s Ошибка создания тикета для %s", LOGGER_PREFIX, ids)
            if success:
                now = int(time.time())
                patches: List[Tuple[str, Dict[str, Any]]] = []
                for record in batch:
                    previous = _order_record(record["order_id"])
                    patches.append((record["order_id"], {
                        "sent_count": int(previous.get("sent_count") or 0) + 1,
                        "last_ticket_at": now,
                        "last_ticket_result": detail,
                        "last_error": "",
                    }))
                _bulk_update_orders(patches)
                result["sent_ids"].extend(ids)
                result[f"{classification}_sent_ids"].extend(ids)
                result["tickets"] += 1
                result[f"{classification}_tickets"] += 1
                _log_event(
                    "ТИКЕТ_ОТПРАВЛЕН",
                    тип=type_label,
                    заказов=len(ids),
                    список=", ".join("#" + item for item in ids),
                    ответ=detail,
                )
            else:
                result["errors"].append(f"{type_label}: {', '.join('#' + item for item in ids)}: {detail}")
                _log_event(
                    "ТИКЕТ_ОШИБКА",
                    logging.ERROR,
                    тип=type_label,
                    заказов=len(ids),
                    список=", ".join("#" + item for item in ids),
                    причина=detail,
                )
                _bulk_update_orders([(record["order_id"], {"last_error": detail, "last_ticket_result": ""}) for record in batch])
                if any(token in detail.lower() for token in ("авториз", "сесс", "лимит", "сут", "слишком много")):
                    abort = True
            if abort:
                break
    finally:
        api.close()
    return result

def _format_ids_for_notice(ids: Sequence[str], limit: int = 80) -> str:
    values = ["#" + str(item).lstrip("#") for item in ids]
    shown = values[:limit]
    text = ", ".join(shown) if shown else "нет"
    if len(values) > limit:
        text += f" … и ещё {len(values) - limit}"
    return text

def _send_result_distribution(result: Dict[str, Any], limit: int = 80) -> str:
    mode = str(result.get("classification_mode") or "none")
    if mode in {"local", "ai"}:
        easy_ids = list(result.get("easy_sent_ids") or [])
        hard_ids = list(result.get("hard_sent_ids") or [])
        return (
            f"✅ <b>Обычные заказы: {len(easy_ids)}</b>\n"
            f"<code>{_h(_format_ids_for_notice(easy_ids, limit))}</code>\n\n"
            f"⚠️ <b>Требуют разбирательства: {len(hard_ids)}</b>\n"
            f"<code>{_h(_format_ids_for_notice(hard_ids, limit))}</code>"
        )
    ids = list(result.get("all_sent_ids") or result.get("sent_ids") or [])
    return (
        f"📦 <b>Заказы без разделения: {len(ids)}</b>\n"
        f"<code>{_h(_format_ids_for_notice(ids, limit))}</code>"
    )

def _run_ticket_cycle(account: Any, *, rescan: bool = True) -> Dict[str, Any]:
    if not _auto_target_allowed():
        return _auto_target_error_result()
    if not _RUN_LOCK.acquire(blocking=False):
        return {
            "selected": 0, "sent_ids": [], "easy_sent_ids": [], "hard_sent_ids": [],
            "all_sent_ids": [], "tickets": 0, "classification_mode": _cfg().get("classification_mode"),
            "errors": ["другая отправка уже выполняется"],
        }
    try:
        if rescan:
            _scan_orders(account)
        records = _eligible_records()
        if not records:
            result = {
                "selected": 0, "sent_ids": [], "easy_sent_ids": [], "hard_sent_ids": [],
                "all_sent_ids": [], "tickets": 0, "classification_mode": _cfg().get("classification_mode"),
                "errors": [],
            }
            _log_event("ОТПРАВКА_ПРОПУЩЕНА", причина="нет подходящих заказов")
        else:
            result = _send_record_batches(account, records)
            _log_event(
                "ЦИКЛ_ОТПРАВКИ_ЗАВЕРШЁН",
                выбрано=result.get("selected"),
                отправлено=len(result.get("sent_ids", [])),
                обычных=len(result.get("easy_sent_ids", [])),
                требуют_разбирательства=len(result.get("hard_sent_ids", [])),
                тикетов=result.get("tickets"),
                ошибок=len(result.get("errors", [])),
            )
        now = time.time()
        _set_cfg(last_send_at=now, next_send_at=now + _cfg()["send_interval_hours"] * 3600)
        return result
    finally:
        _RUN_LOCK.release()

def _send_single_order(account: Any, order_id: str) -> Dict[str, Any]:
    if not _auto_target_allowed():
        return _auto_target_error_result(1)
    record = _order_record(order_id)
    if not record:
        return {
            "selected": 0, "sent_ids": [], "easy_sent_ids": [], "hard_sent_ids": [],
            "all_sent_ids": [], "tickets": 0, "classification_mode": _cfg().get("classification_mode"),
            "errors": ["заказ отсутствует в базе; сначала выполните сканирование"],
        }
    if not _RUN_LOCK.acquire(blocking=False):
        return {
            "selected": 1, "sent_ids": [], "easy_sent_ids": [], "hard_sent_ids": [],
            "all_sent_ids": [], "tickets": 0, "classification_mode": _cfg().get("classification_mode"),
            "errors": ["другая отправка уже выполняется"],
        }
    try:
        return _send_record_batches(account, [record])
    finally:
        _RUN_LOCK.release()

def _apply_startup_behavior() -> None:
    settings = _cfg()
    now = time.time()
    updates: Dict[str, Any] = {"next_scan_at": now}
    if settings.get("startup_action") == "send_now":
        updates["next_send_at"] = now
        action = "отправка при запуске"
    else:
        next_send = float(settings.get("next_send_at") or 0)
        if next_send <= now:
            next_send = now + int(settings.get("send_interval_hours") or 24) * 3600
        updates["next_send_at"] = next_send
        action = "продолжение таймера"
    _set_cfg(**updates)
    _log_event("ПОВЕДЕНИЕ_ПРИ_ЗАПУСКЕ", режим=action, следующая_отправка=_format_dt(updates["next_send_at"]))

def _refresh_phpsessid_on_start(account: Any) -> None:
    if not _cfg().get("auto_fetch_phpsessid"):
        _log_event("PHPSESSID_ПРИ_ЗАПУСКЕ", режим="автополучение выключено")
        return
    try:
        value = _extract_phpsessid(account)
        _set_cfg(phpsessid=value)
        _log_event("PHPSESSID_ПРИ_ЗАПУСКЕ", результат="успешно")
    except Exception as exc:
        _log_event("PHPSESSID_ПРИ_ЗАПУСКЕ", logging.ERROR, результат="ошибка", причина=_short_error(exc))
        _notify(f"❌ <b>Auto Ticket: PHPSESSID не получен при запуске</b>\n\n{_h(_short_error(exc))}")

def _background_loop(account: Any) -> None:
    _log_event("ФОНОВЫЙ_ЦИКЛ", статус="запущен")
    while not _STOP_EVENT.wait(20):
        settings = _cfg()
        if not _auto_target_allowed() or not settings.get("plugin_enabled"):
            continue
        now = time.time()
        if now >= float(settings.get("next_scan_at") or 0):
            try:
                total, new_count = _scan_orders(account)
                if new_count:
                    _notify(
                        f"🔎 <b>Auto Ticket: сканирование завершено</b>\n\n"
                        f"Заказов в базе: <b>{total}</b>\nНовых заказов: <b>{new_count}</b>"
                    )
            except Exception as exc:
                logger.exception("%s Ошибка фонового сканирования", LOGGER_PREFIX)
                _set_cfg(next_scan_at=time.time() + 900)
                _notify(f"❌ <b>Auto Ticket: ошибка сканирования</b>\n\n{_h(_short_error(exc))}")
        settings = _cfg()
        now = time.time()
        if now >= float(settings.get("next_send_at") or 0):
            try:
                result = _run_ticket_cycle(account, rescan=True)
                sent_ids = result.get("sent_ids", [])
                errors = result.get("errors", [])
                if sent_ids:
                    text = (
                        f"✅ <b>Auto Ticket: отправка завершена</b>\n\n"
                        f"Тикетов: <b>{result.get('tickets', 0)}</b>\n"
                        f"Всего заказов: <b>{len(sent_ids)}</b>\n\n"
                        f"{_send_result_distribution(result)}"
                    )
                    if errors:
                        text += "\n\n⚠️ Часть не отправлена:\n" + "\n".join(f"• {_h(item)}" for item in errors[:5])
                    _notify(text)
                elif result.get("selected"):
                    reason = "\n".join(f"• {_h(item)}" for item in errors[:8]) or "неизвестная причина"
                    _notify(
                        f"❌ <b>Auto Ticket: тикеты не отправлены</b>\n\n"
                        f"Подходящих заказов: <b>{result.get('selected')}</b>\n{reason}"
                    )
            except Exception as exc:
                logger.exception("%s Ошибка фоновой отправки", LOGGER_PREFIX)
                _set_cfg(next_send_at=time.time() + 3600)
                _notify(f"❌ <b>Auto Ticket: ошибка отправки</b>\n\n{_h(_short_error(exc))}")
    _log_event("ФОНОВЫЙ_ЦИКЛ", статус="остановлен")

def _start_background(account: Any) -> None:
    global _BACKGROUND_THREAD
    if _BACKGROUND_THREAD and _BACKGROUND_THREAD.is_alive():
        return
    _STOP_EVENT.clear()
    _BACKGROUND_THREAD = threading.Thread(
        target=_background_loop, args=(account,), name="AutoTicket-Worker", daemon=True,
    )
    _BACKGROUND_THREAD.start()

def _stop_background() -> None:
    global _BACKGROUND_THREAD
    _STOP_EVENT.set()
    if _BACKGROUND_THREAD and _BACKGROUND_THREAD.is_alive():
        _BACKGROUND_THREAD.join(timeout=5)
    _BACKGROUND_THREAD = None

CB_HOME = "at2:home"
CB_SETTINGS = "at2:settings"
CB_INFO = "at2:info"
CB_UPDATE = "at2:update"
CB_UPDATE_LOCAL = "at2:update:local"
CB_UPDATE_ONLINE = "at2:update:online"
CB_UPDATE_URL = "at2:update:url"
CB_DELETE_ASK = "at2:delete:ask"
CB_DELETE_YES = "at2:delete:yes"
CB_DELETE_NO = "at2:delete:no"

CB_AUTH = "at2:auth"
CB_AUTH_MODE = "at2:auth:mode"
CB_AUTH_SET = "at2:auth:set"
CB_AUTH_CLEAR = "at2:auth:clear"
CB_AUTH_TEST = "at2:auth:test"

CB_STATUS = "at2:status"
CB_STATUS_TOGGLE = "at2:status:toggle"
CB_STATUS_CHECK = "at2:status:check"

CB_TEXT = "at2:text"
CB_CLASS_MODE = "at2:class:mode"
CB_TEMPLATE_SINGLE = "at2:tpl:single"
CB_TEMPLATE_EASY = "at2:tpl:easy"
CB_TEMPLATE_HARD = "at2:tpl:hard"
CB_KEYWORDS = "at2:keywords"
CB_AI = "at2:ai"
CB_AI_KEY = "at2:ai:key"
CB_AI_MODELS = "at2:ai:models"
CB_AI_MODELS_PAGE = "at2:ai:models:p:"
CB_AI_MODEL_SELECT = "at2:ai:models:s:"
CB_AI_MODELS_REFRESH = "at2:ai:models:refresh"
CB_AI_TEST = "at2:ai:test"
CB_RECLASSIFY = "at2:reclassify"

CB_INTERVALS = "at2:intervals"
CB_TOGGLE_NOTIFY = "at2:toggle:notify"
CB_EXTRA = "at2:extra"
CB_STARTUP_ACTION = "at2:startup:action"
CB_SET_SCAN = "at2:set:scan"
CB_SET_SEND = "at2:set:send"
CB_SET_AGE = "at2:set:age"
CB_SET_COUNT = "at2:set:count"

CB_ORDERS = "at2:orders"
CB_ORDERS_PAGE = "at2:orders:p:"
CB_ORDER = "at2:order:"
CB_ORDER_SEND = "at2:order:send:"
CB_ORDER_IGNORE = "at2:order:ignore:"
CB_IGNORED = "at2:ignored"
CB_IGNORED_PAGE = "at2:ignored:p:"
CB_IGNORED_ORDER = "at2:ignored:o:"
CB_ORDER_UNIGNORE = "at2:order:unignore:"

CB_MAINTENANCE = "at2:maintenance"
CB_SCAN_NOW = "at2:scan"
CB_SEND_NOW = "at2:send"
CB_LOGS = "at2:logs"
CB_EXPORT = "at2:export"
CB_CANCEL = "at2:cancel"

CB_PLUGINS_LIST_OPEN = f"{getattr(CBT, 'PLUGINS_LIST', '44')}:0"

def _home_text() -> str:
    return (
        f"🧩 <b>Плагин:</b> {NAME}\n"
        f"📦 <b>Версия:</b> <code>{VERSION}</code>\n"
        f"👤 <b>Автор:</b> <a href=\"{CREATOR_URL}\">{_h(CREDITS)}</a>\n\n"
        "Выберите раздел ниже."
    )

def _home_keyboard() -> K:
    keyboard = K()
    keyboard.row(B("⚙️ Настройки", callback_data=CB_SETTINGS), B("ℹ️ Информация", callback_data=CB_INFO))
    keyboard.row(B("⬆️ Обновить плагин", callback_data=CB_UPDATE), B("🗑 Удалить", callback_data=CB_DELETE_ASK))
    keyboard.row(B("🔙 К списку плагинов", callback_data=CB_PLUGINS_LIST_OPEN))
    return keyboard

def _settings_text() -> str:
    settings = _cfg()
    eligible = len(_eligible_records())
    ignored = len(_ignored_records())
    total = len(_all_records())
    mode_labels = {"none": "без разделения", "local": "локальные правила", "ai": "io.net AI"}
    return (
        "<b>⚙️ Настройки Auto Ticket</b>\n\n"
        f"• Статус: <b>{_bool_label(settings.get('plugin_enabled'))}</b>\n"
        f"• PHPSESSID при запуске: <b>{'получать' if settings.get('auto_fetch_phpsessid') else 'не получать'}</b>\n"
        f"• Определение заказов: <b>{_h(mode_labels.get(settings.get('classification_mode'), 'неизвестно'))}</b>\n"
        f"• В базе: <b>{total}</b>, готовы к тикету: <b>{eligible}</b>, игнор: <b>{ignored}</b>\n"
        f"• Следующая отправка: <b>{_h(_format_duration(float(settings.get('next_send_at') or 0) - time.time()))}</b>\n\n"
        "Выберите категорию:"
    )

def _settings_keyboard() -> K:
    keyboard = K()
    keyboard.row(B("📊 Статус", callback_data=CB_STATUS))
    keyboard.row(B("🔐 Авторизация", callback_data=CB_AUTH))
    keyboard.row(B("📝 Тексты", callback_data=CB_TEXT))
    keyboard.row(B("⏱ Интервалы", callback_data=CB_INTERVALS))
    keyboard.row(B("📦 Заказы", callback_data=CB_ORDERS), B("🚫 Игнор заказов", callback_data=CB_IGNORED))
    keyboard.row(B("🎛 Дополнительно", callback_data=CB_EXTRA))
    keyboard.row(B("🧰 Обслуживание", callback_data=CB_MAINTENANCE))
    keyboard.row(B("◀️ Назад", callback_data=CB_HOME))
    return keyboard

def _status_text() -> str:
    settings = _cfg()
    worker = bool(_BACKGROUND_THREAD and _BACKGROUND_THREAD.is_alive())
    return (
        "<b>📊 Статус Auto Ticket</b>\n\n"
        f"• Плагин: <b>{_bool_label(settings.get('plugin_enabled'))}</b>\n"
        f"• Фоновый обработчик: <b>{_bool_label(worker)}</b>\n"
        f"• PHPSESSID сохранён: <b>{'да' if settings.get('phpsessid') else 'нет'}</b>\n"
        f"• Последнее сканирование: <code>{_h(_format_dt(settings.get('last_scan_at')))}</code>\n"
        f"• Последняя отправка: <code>{_h(_format_dt(settings.get('last_send_at')))}</code>\n\n"
        "Проверка плагина тестирует хранилище, фоновый обработчик и авторизацию поддержки, но не создаёт тикет."
    )

def _status_keyboard() -> K:
    settings = _cfg()
    keyboard = K()
    keyboard.row(B(f"🧩 Плагин: {'ВКЛ' if settings.get('plugin_enabled') else 'ВЫКЛ'}", callback_data=CB_STATUS_TOGGLE))
    keyboard.row(B("🩺 Проверить плагин", callback_data=CB_STATUS_CHECK))
    keyboard.row(B("◀️ Назад", callback_data=CB_SETTINGS))
    return keyboard

def _auth_text() -> str:
    settings = _cfg()
    return (
        "<b>🔐 Авторизация поддержки</b>\n\n"
        f"• Получать PHPSESSID при каждом запуске Cardinal: <b>{_bool_label(settings.get('auto_fetch_phpsessid'))}</b>\n"
        f"• Сохранённый PHPSESSID: <code>{_h(_masked(settings.get('phpsessid', '')))}</code>"
    )

def _auth_keyboard() -> K:
    settings = _cfg()
    keyboard = K()
    keyboard.row(B(f"🔄 Брать при запуске: {'ВКЛ' if settings.get('auto_fetch_phpsessid') else 'ВЫКЛ'}", callback_data=CB_AUTH_MODE))
    keyboard.row(B("✏️ Ввести PHPSESSID", callback_data=CB_AUTH_SET), B("🧹 Очистить", callback_data=CB_AUTH_CLEAR))
    keyboard.row(B("🩺 Проверить авторизацию", callback_data=CB_AUTH_TEST))
    keyboard.row(B("◀️ Назад", callback_data=CB_SETTINGS))
    return keyboard

def _text_settings_text() -> str:
    settings = _cfg()
    mode_labels = {"none": "1. Все заказы одним списком", "local": "2. Определять локально", "ai": "3. Определять через io.net AI"}
    return (
        "<b>📝 Тексты тикета и определение заказов</b>\n\n"
        f"• Режим: <b>{_h(mode_labels.get(settings.get('classification_mode')))}</b>\n"
        f"• Текст без разделения: <code>{len(settings.get('message_template', ''))} симв.</code>\n"
        f"• Текст обычных заказов: <code>{len(settings.get('easy_template', ''))} симв.</code>\n"
        f"• Текст проблемных заказов: <code>{len(settings.get('hard_template', ''))} симв.</code>\n"
        f"• Локальных признаков проблемы: <code>{len(settings.get('local_hard_keywords', []))}</code>\n\n"
        "Переменные: <code>{orders}</code>, <code>{username}</code>, <code>{count}</code>, <code>{classification}</code>."
    )

def _text_settings_keyboard() -> K:
    settings = _cfg()
    mode_labels = {"none": "ОДИН СПИСОК", "local": "ЛОКАЛЬНО", "ai": "IO.NET AI"}
    keyboard = K()
    keyboard.row(B(f"🧠 Режим: {mode_labels.get(settings.get('classification_mode'))}", callback_data=CB_CLASS_MODE))
    keyboard.row(B("📝 Текст тикета без разделения", callback_data=CB_TEMPLATE_SINGLE))
    keyboard.row(B("✅ Текст обычных заказов", callback_data=CB_TEMPLATE_EASY))
    keyboard.row(B("⚠️ Текст проблемных заказов", callback_data=CB_TEMPLATE_HARD))
    keyboard.row(B("🔎 Признаки проблемного заказа", callback_data=CB_KEYWORDS), B("🤖 io.net AI", callback_data=CB_AI))
    keyboard.row(B("♻️ Обновить определение в базе", callback_data=CB_RECLASSIFY))
    keyboard.row(B("◀️ Назад", callback_data=CB_SETTINGS))
    return keyboard

def _ai_text() -> str:
    settings = _cfg()
    return (
        "<b>🤖 io.net AI</b>\n\n"
        f"• API-ключ: <code>{_h(_masked(settings.get('ai_api_key', ''), 4))}</code>\n"
        f"• Выбранная модель: <code>{_h(settings.get('ai_model'))}</code>\n"
    )

def _ai_keyboard() -> K:
    keyboard = K()
    keyboard.row(B("🔑 API-ключ", callback_data=CB_AI_KEY))
    keyboard.row(B("🧠 Выбрать модель", callback_data=CB_AI_MODELS))
    keyboard.row(B("🩺 Проверить API", callback_data=CB_AI_TEST))
    keyboard.row(B("◀️ Назад", callback_data=CB_TEXT))
    return keyboard

def _ai_models_text(models: Sequence[str], page: int, error: str = "") -> str:
    total_pages = max(1, (len(models) + AI_MODELS_PER_PAGE - 1) // AI_MODELS_PER_PAGE)
    text = (
        "<b>🧠 Модели io.net</b>\n\n"
        f"Доступно: <b>{len(models)}</b> · Страница <b>{page + 1}/{total_pages}</b>\n"
        f"Выбрана: <code>{_h(_cfg().get('ai_model'))}</code>\n\n"
        "Нажмите на модель, чтобы выбрать её."
    )
    if error:
        text += f"\n\n⚠️ {_h(error)}"
    return text

def _ai_models_keyboard(models: Sequence[str], page: int) -> K:
    total_pages = max(1, (len(models) + AI_MODELS_PER_PAGE - 1) // AI_MODELS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * AI_MODELS_PER_PAGE
    selected = str(_cfg().get("ai_model") or "")
    keyboard = K()
    for index in range(start, min(start + AI_MODELS_PER_PAGE, len(models))):
        model = str(models[index])
        prefix = "✅ " if model == selected else ""
        keyboard.row(B(prefix + _short_error(model, 48), callback_data=CB_AI_MODEL_SELECT + str(index)))
    nav: List[B] = []
    if page > 0:
        nav.append(B("⬅️", callback_data=CB_AI_MODELS_PAGE + str(page - 1)))
    if page + 1 < total_pages:
        nav.append(B("➡️", callback_data=CB_AI_MODELS_PAGE + str(page + 1)))
    if nav:
        keyboard.row(*nav)
    keyboard.row(B("🔄 Обновить список", callback_data=CB_AI_MODELS_REFRESH))
    keyboard.row(B("◀️ Назад", callback_data=CB_AI))
    return keyboard

def _intervals_text() -> str:
    settings = _cfg()
    return (
        "<b>⏱ Интервалы и лимиты</b>\n\n"
        f"• Сканировать каждые: <code>{settings.get('scan_interval_hours')} ч.</code>\n"
        f"• Отправлять каждые: <code>{settings.get('send_interval_hours')} ч.</code>\n"
        f"• Заказ готов через: <code>{settings.get('order_age_hours')} ч.</code>\n"
        f"• Заказов в одном тикете: <code>{settings.get('max_orders_in_ticket')}</code>\n"
    )

def _intervals_keyboard() -> K:
    settings = _cfg()
    keyboard = K()
    keyboard.row(B(f"🔎 Сканирование: {settings.get('scan_interval_hours')} ч.", callback_data=CB_SET_SCAN))
    keyboard.row(B(f"📨 Отправка: {settings.get('send_interval_hours')} ч.", callback_data=CB_SET_SEND))
    keyboard.row(B(f"⌛ Возраст заказа: {settings.get('order_age_hours')} ч.", callback_data=CB_SET_AGE))
    keyboard.row(B(f"📦 Заказов в тикете: {settings.get('max_orders_in_ticket')}", callback_data=CB_SET_COUNT))
    keyboard.row(B("◀️ Назад", callback_data=CB_SETTINGS))
    return keyboard

def _extra_text() -> str:
    settings = _cfg()
    startup = "отправить подходящие заказы сразу" if settings.get("startup_action") == "send_now" else "продолжить таймер без немедленной отправки"
    return (
        "<b>🎛 Дополнительные настройки</b>\n\n"
        f"• Уведомления: <b>{_bool_label(settings.get('notify_enabled'))}</b>\n"
        f"• После запуска Cardinal: <b>{_h(startup)}</b>\n"
        f"• Следующее сканирование: <b>{_h(_format_duration(float(settings.get('next_scan_at') or 0) - time.time()))}</b>\n"
        f"• Следующая отправка: <b>{_h(_format_duration(float(settings.get('next_send_at') or 0) - time.time()))}</b>\n\n"
        "Ручные действия ниже не меняют выбранные интервалы."
    )

def _extra_keyboard() -> K:
    settings = _cfg()
    startup_label = "ОТПРАВИТЬ СРАЗУ" if settings.get("startup_action") == "send_now" else "ПРОДОЛЖИТЬ ТАЙМЕР"
    keyboard = K()
    keyboard.row(B(f"🔔 Уведомления: {'ВКЛ' if settings.get('notify_enabled') else 'ВЫКЛ'}", callback_data=CB_TOGGLE_NOTIFY))
    keyboard.row(B(f"🔁 После запуска: {startup_label}", callback_data=CB_STARTUP_ACTION))
    keyboard.row(B("🔎 Сканировать сейчас", callback_data=CB_SCAN_NOW))
    keyboard.row(B("🎫 Отправить тикеты сейчас", callback_data=CB_SEND_NOW))
    keyboard.row(B("◀️ Назад", callback_data=CB_SETTINGS))
    return keyboard

def _record_state_label(record: Dict[str, Any]) -> str:
    if record.get("ignored"):
        return "🚫"
    if not bool(record.get("is_pending", True)):
        return "🏁"
    if int(record.get("sent_count") or 0) > 0:
        return "✅"
    cutoff = time.time() - _cfg()["order_age_hours"] * 3600
    if float(record.get("purchased_at") or time.time()) <= cutoff:
        return "🟡"
    return "⚪"

def _orders_text(records: Sequence[Dict[str, Any]], page: int, ignored: bool = False) -> str:
    total_pages = max(1, (len(records) + 9) // 10)
    title = "🚫 Игнорируемые заказы" if ignored else "📦 Заказы"
    return (
        f"<b>{title}</b>\n\n"
        f"Всего: <b>{len(records)}</b> · Страница <b>{page + 1}/{total_pages}</b>\n\n"
        "Одна кнопка соответствует одному заказу. Откройте заказ для подробностей и действий."
    )

def _orders_keyboard(records: Sequence[Dict[str, Any]], page: int, ignored: bool = False) -> K:
    total_pages = max(1, (len(records) + 9) // 10)
    page = max(0, min(page, total_pages - 1))
    start = page * 10
    keyboard = K()
    for record in records[start:start + 10]:
        order_id = record.get("order_id")
        classification = "2️⃣" if record.get("classification") == "hard" else "1️⃣"
        prefix = _record_state_label(record)
        callback = (CB_IGNORED_ORDER if ignored else CB_ORDER) + str(order_id)
        keyboard.row(B(f"{prefix}{classification} #{order_id} · {_short_error(record.get('product'), 28)}", callback_data=callback))
    nav: List[B] = []
    page_prefix = CB_IGNORED_PAGE if ignored else CB_ORDERS_PAGE
    if page > 0:
        nav.append(B("⬅️", callback_data=page_prefix + str(page - 1)))
    if page + 1 < total_pages:
        nav.append(B("➡️", callback_data=page_prefix + str(page + 1)))
    if nav:
        keyboard.row(*nav)
    keyboard.row(B("🔄 Обновить список", callback_data=CB_SCAN_NOW))
    keyboard.row(B("◀️ Назад", callback_data=CB_SETTINGS))
    return keyboard

def _order_detail_text(record: Dict[str, Any]) -> str:
    purchased = _format_dt(record.get("purchased_at"))
    age = _format_duration(time.time() - float(record.get("purchased_at") or time.time()))
    class_label = "проблемный" if record.get("classification") == "hard" else "обычный"
    return (
        f"<b>📦 Заказ #{_h(record.get('order_id'))}</b>\n\n"
        f"• Товар: <b>{_h(record.get('product') or 'неизвестен')}</b>\n"
        f"• Покупатель: <code>{_h(record.get('buyer') or 'неизвестен')}</code>\n"
        f"• Сумма: <code>{_h(record.get('price') or 'не указана')}</code>\n"
        f"• Куплен: <code>{_h(purchased)}</code>\n"
        f"• Возраст: <code>{_h(age)}</code>\n"
        f"• Тип заказа: <b>{class_label}</b>\n"
        f"• Игнорируется: <b>{'да' if record.get('ignored') else 'нет'}</b>\n"
        f"• Тикет отправлен: <b>{'да' if int(record.get('sent_count') or 0) else 'нет'}</b>\n"
        f"• Последний тикет: <code>{_h(_format_dt(record.get('last_ticket_at')))}</code>"
    )

def _order_detail_keyboard(record: Dict[str, Any], ignored_view: bool = False) -> K:
    order_id = str(record.get("order_id"))
    keyboard = K()
    if record.get("ignored"):
        keyboard.row(B("♻️ Убрать из игнора", callback_data=CB_ORDER_UNIGNORE + order_id))
    else:
        keyboard.row(B("🎫 Отправить один тикет", callback_data=CB_ORDER_SEND + order_id))
        keyboard.row(B("🚫 Добавить в игнор", callback_data=CB_ORDER_IGNORE + order_id))
    keyboard.row(B("◀️ Назад", callback_data=CB_IGNORED if ignored_view else CB_ORDERS))
    return keyboard

def _maintenance_text() -> str:
    settings = _cfg()
    file_size = lambda path: path.stat().st_size if path.exists() else 0
    return (
        "<b>🧰 Обслуживание</b>\n\n"
        f"• settings.json: <code>{file_size(SETTINGS_FILE)} байт</code>\n"
        f"• orders.json: <code>{file_size(ORDERS_FILE)} байт</code>\n"
        f"• log.txt: <code>{file_size(LOG_FILE)} байт</code>\n"
        f"• Последнее сканирование: <code>{_h(_format_dt(settings.get('last_scan_at')))}</code>\n"
        f"• Последняя отправка: <code>{_h(_format_dt(settings.get('last_send_at')))}</code>\n\n"
        "Логи записывают запуск, сканирование, классификацию, попытки отправки, отправленные заказы и причины ошибок."
    )

def _maintenance_keyboard() -> K:
    keyboard = K()
    keyboard.row(B("📄 Скачать логи", callback_data=CB_LOGS), B("💾 Резервная копия", callback_data=CB_EXPORT))
    keyboard.row(B("◀️ Назад", callback_data=CB_SETTINGS))
    return keyboard

def _info_text() -> str:
    return (
        "<b>ℹ️ Информация</b>\n\n"
        "Здесь находятся официальные ссылки FTG-Plugin.\n\n"
        "• Чат - помощь и общение.\n"
        "• Канал - новости и обновления.\n"
        "• Инструкция - настройка и использование плагина.\n"
        "• Telegram автора - связь с разработчиком."
    )

def _info_keyboard() -> K:
    keyboard = K()
    keyboard.row(B("💬 Чат", url=GROUP_URL), B("📢 Канал", url=CHANNEL_URL))
    keyboard.row(B("📖 Инструкция", url=INSTRUCTION_URL), B("💻 GitHub", url=GITHUB_URL))
    keyboard.row(B("👤 Telegram автора", url=CREATOR_URL))
    keyboard.row(B("✉️ ТГ-канал сообщений", url=CHANNEL_MESSAGES_URL))
    keyboard.row(B("◀️ Назад", callback_data=CB_HOME))
    return keyboard

def _update_text() -> str:
    return (
        "<b>⬆️ Обновление Auto Ticket</b>\n\n"
        f"Текущая версия: <code>{VERSION}</code>\n\n"
        "Локальное обновление принимает файл .py и проверяет его перед установкой. "
        "Онлайн-обновление загружает актуальную версию из официального GitHub."
    )

def _update_keyboard() -> K:
    keyboard = K()
    keyboard.row(B("📥 Обновить локально", callback_data=CB_UPDATE_LOCAL))
    keyboard.row(B("🌐 Обновить онлайн", callback_data=CB_UPDATE_ONLINE))
    keyboard.row(B("◀️ Назад", callback_data=CB_HOME))
    return keyboard

def _delete_confirm_text() -> str:
    return (
        "⚠️ <b>Удаление Auto Ticket</b>\n\n"
        "Будут удалены файл плагина, настройки, база заказов и логи из "
        "<code>storage/plugins/AutoTicket</code>. Действие необратимо."
    )

def _delete_confirm_keyboard() -> K:
    keyboard = K()
    keyboard.row(B("✅ Да, удалить", callback_data=CB_DELETE_YES), B("❌ Нет", callback_data=CB_DELETE_NO))
    return keyboard

def _cancel_keyboard() -> K:
    keyboard = K()
    keyboard.row(B("❌ Отменить ввод", callback_data=CB_CANCEL))
    return keyboard

_JOBS_LOCK = threading.RLock()
_ACTIVE_JOBS: set[str] = set()

def _schedule_job(key: str, function: Any, *args: Any) -> bool:
    with _JOBS_LOCK:
        if key in _ACTIVE_JOBS:
            return False
        _ACTIVE_JOBS.add(key)

    def runner() -> None:
        try:
            function(*args)
        except Exception:
            logger.exception("%s Фоновая задача %s завершилась ошибкой", LOGGER_PREFIX, key)
        finally:
            with _JOBS_LOCK:
                _ACTIVE_JOBS.discard(key)

    threading.Thread(target=runner, name=f"AutoTicket-{key}", daemon=True).start()
    return True

def _remember_owner(chat_id: Any) -> None:
    try:
        value = int(chat_id)
    except (TypeError, ValueError):
        return
    if _cfg().get("owner_chat_id") != value:
        _set_cfg(owner_chat_id=value)

def _open_home(bot: Any, call: Any) -> None:
    _remember_owner(call.message.chat.id)
    _safe_edit(bot, call, _home_text(), _home_keyboard())
    _answer(bot, call)

def _open_settings(bot: Any, call: Any) -> None:
    _remember_owner(call.message.chat.id)
    _safe_edit(bot, call, _settings_text(), _settings_keyboard())
    _answer(bot, call)

def _open_status(bot: Any, call: Any) -> None:
    _safe_edit(bot, call, _status_text(), _status_keyboard())
    _answer(bot, call)

def _open_auth(bot: Any, call: Any) -> None:
    _safe_edit(bot, call, _auth_text(), _auth_keyboard())
    _answer(bot, call)

def _open_text_settings(bot: Any, call: Any) -> None:
    _safe_edit(bot, call, _text_settings_text(), _text_settings_keyboard())
    _answer(bot, call)

def _open_ai(bot: Any, call: Any) -> None:
    _safe_edit(bot, call, _ai_text(), _ai_keyboard())
    _answer(bot, call)

def _open_intervals(bot: Any, call: Any) -> None:
    _safe_edit(bot, call, _intervals_text(), _intervals_keyboard())
    _answer(bot, call)

def _open_extra(bot: Any, call: Any) -> None:
    _safe_edit(bot, call, _extra_text(), _extra_keyboard())
    _answer(bot, call)

def _open_orders(bot: Any, call: Any, page: int = 0) -> None:
    records = _all_records()
    total_pages = max(1, (len(records) + 9) // 10)
    page = max(0, min(page, total_pages - 1))
    _safe_edit(bot, call, _orders_text(records, page), _orders_keyboard(records, page))
    _answer(bot, call)

def _open_ignored(bot: Any, call: Any, page: int = 0) -> None:
    records = _ignored_records()
    total_pages = max(1, (len(records) + 9) // 10)
    page = max(0, min(page, total_pages - 1))
    _safe_edit(bot, call, _orders_text(records, page, ignored=True), _orders_keyboard(records, page, ignored=True))
    _answer(bot, call)

def _open_order_detail(bot: Any, call: Any, order_id: str, ignored_view: bool = False) -> None:
    record = _order_record(order_id)
    if not record:
        _answer(bot, call, "Заказ не найден в базе.", True)
        return
    _safe_edit(bot, call, _order_detail_text(record), _order_detail_keyboard(record, ignored_view=ignored_view))
    _answer(bot, call)

def _open_maintenance(bot: Any, call: Any) -> None:
    _safe_edit(bot, call, _maintenance_text(), _maintenance_keyboard())
    _answer(bot, call)

def _open_info(bot: Any, call: Any) -> None:
    _safe_edit(bot, call, _info_text(), _info_keyboard())
    _answer(bot, call)

def _open_update(bot: Any, call: Any) -> None:
    _safe_edit(bot, call, _update_text(), _update_keyboard())
    _answer(bot, call)

def _open_delete_confirm(bot: Any, call: Any) -> None:
    _safe_edit(bot, call, _delete_confirm_text(), _delete_confirm_keyboard())
    _answer(bot, call)

def _render_route(bot: Any, chat_id: int, message_id: int, route: str) -> None:
    if route == "home":
        _edit_by_id(bot, chat_id, message_id, _home_text(), _home_keyboard())
    elif route == "status":
        _edit_by_id(bot, chat_id, message_id, _status_text(), _status_keyboard())
    elif route == "auth":
        _edit_by_id(bot, chat_id, message_id, _auth_text(), _auth_keyboard())
    elif route == "text":
        _edit_by_id(bot, chat_id, message_id, _text_settings_text(), _text_settings_keyboard())
    elif route == "ai":
        _edit_by_id(bot, chat_id, message_id, _ai_text(), _ai_keyboard())
    elif route == "intervals":
        _edit_by_id(bot, chat_id, message_id, _intervals_text(), _intervals_keyboard())
    elif route == "extra":
        _edit_by_id(bot, chat_id, message_id, _extra_text(), _extra_keyboard())
    elif route == "update":
        _edit_by_id(bot, chat_id, message_id, _update_text(), _update_keyboard())
    elif route == "maintenance":
        _edit_by_id(bot, chat_id, message_id, _maintenance_text(), _maintenance_keyboard())
    else:
        _edit_by_id(bot, chat_id, message_id, _settings_text(), _settings_keyboard())

def _prompt(bot: Any, call: Any, text: str, state: Dict[str, Any]) -> None:
    chat_id = int(call.message.chat.id)
    message_id = _message_id(call.message)
    state = dict(state)
    state["message_id"] = message_id
    state.setdefault("return_route", "settings")
    with _FSM_LOCK:
        _FSM[chat_id] = state
    _safe_edit(bot, call, text, _cancel_keyboard())
    _answer(bot, call)

def _pop_fsm(chat_id: int) -> Dict[str, Any]:
    with _FSM_LOCK:
        return dict(_FSM.pop(chat_id, {}) or {})

def _fsm_state(chat_id: int) -> Dict[str, Any]:
    with _FSM_LOCK:
        return dict(_FSM.get(chat_id, {}) or {})

def _cancel_fsm(bot: Any, call: Any) -> None:
    state = _pop_fsm(int(call.message.chat.id))
    route = str(state.get("return_route") or "settings")
    _render_route(bot, int(call.message.chat.id), _message_id(call.message), route)
    _answer(bot, call, "Ввод отменён.")

def _start_text_input(bot: Any, call: Any, step: str, title: str, current: str, return_route: str, **extra: Any) -> None:
    prompt = (
        f"<b>{_h(title)}</b>\n\n"
        f"Текущее значение:\n<code>{_h(current)}</code>\n\n"
        "Пришлите новое значение одним сообщением."
    )
    state = {"step": step, "return_route": return_route}
    state.update(extra)
    _prompt(bot, call, prompt, state)

def _start_number_input(bot: Any, call: Any, key: str, title: str, minimum: int, maximum: int) -> None:
    current = _cfg().get(key)
    _prompt(
        bot, call,
        f"<b>{_h(title)}</b>\n\nТекущее значение: <code>{current}</code>\n"
        f"Введите целое число от <b>{minimum}</b> до <b>{maximum}</b>.",
        {
            "step": "number", "key": key, "minimum": minimum, "maximum": maximum,
            "return_route": "intervals",
        },
    )

def _handle_admin_text(message: Any, cardinal: Any) -> None:
    bot = cardinal.telegram.bot
    chat_id = int(message.chat.id)
    state = _fsm_state(chat_id)
    if not state:
        return
    text = str(getattr(message, "text", "") or "").strip()
    with suppress(Exception):
        bot.delete_message(chat_id, _message_id(message))
    message_id = int(state.get("message_id") or 0)
    step = state.get("step")
    route = str(state.get("return_route") or "settings")

    try:
        if step == "phpsessid":
            if not text:
                raise ValueError("PHPSESSID не может быть пустым")
            _set_cfg(phpsessid=text)
        elif step == "template_single":
            if "{orders}" not in text:
                raise ValueError("шаблон должен содержать {orders}")
            _set_cfg(message_template=text)
        elif step == "template_easy":
            if "{orders}" not in text:
                raise ValueError("шаблон должен содержать {orders}")
            _set_cfg(easy_template=text)
        elif step == "template_hard":
            if "{orders}" not in text:
                raise ValueError("шаблон должен содержать {orders}")
            _set_cfg(hard_template=text)
        elif step == "keywords":
            values = [item.strip().lower() for item in re.split(r"[,\n;]+", text) if item.strip()]
            if not values:
                raise ValueError("нужно указать хотя бы одно ключевое слово")
            _set_cfg(local_hard_keywords=values[:200])
        elif step == "ai_key":
            if not text:
                raise ValueError("API-ключ не может быть пустым")
            _set_cfg(ai_api_key=text)
        elif step == "online_update_url":
            if text and not re.match(r"^https://", text, flags=re.I):
                raise ValueError("URL должен начинаться с https://")
            _set_cfg(online_update_url=text)
        elif step == "number":
            value = int(text)
            minimum = int(state.get("minimum"))
            maximum = int(state.get("maximum"))
            if not minimum <= value <= maximum:
                raise ValueError(f"нужно число от {minimum} до {maximum}")
            key = str(state.get("key"))
            updates = {key: value}
            now = time.time()
            if key == "scan_interval_hours":
                updates["next_scan_at"] = now + value * 3600
            elif key == "send_interval_hours":
                updates["next_send_at"] = now + value * 3600
            _set_cfg(**updates)
        elif step == "local_update":
            raise ValueError("ожидается файл .py, а не текст")
        else:
            raise ValueError("неизвестный режим ввода")
    except Exception as exc:
        _edit_by_id(
            bot, chat_id, message_id,
            f"❌ <b>Значение не сохранено.</b>\n\n{_h(_short_error(exc))}\n\nПришлите исправленное значение.",
            _cancel_keyboard(),
        )
        return

    _pop_fsm(chat_id)
    _render_route(bot, chat_id, message_id, route)
    with suppress(Exception):
        bot.send_message(chat_id, "✅ Настройка сохранена.")

def _toggle_auth_mode(bot: Any, call: Any) -> None:
    enabled = not bool(_cfg().get("auto_fetch_phpsessid"))
    _set_cfg(auto_fetch_phpsessid=enabled)
    _safe_edit(bot, call, _auth_text(), _auth_keyboard())
    _answer(bot, call, "Автополучение PHPSESSID включено." if enabled else "Автополучение PHPSESSID выключено.")

def _clear_phpsessid(bot: Any, call: Any) -> None:
    _set_cfg(phpsessid="")
    _safe_edit(bot, call, _auth_text(), _auth_keyboard())
    _answer(bot, call, "PHPSESSID очищен.")

def _auth_test_worker(bot: Any, chat_id: int, message_id: int, account: Any) -> None:
    try:
        api = FunPaySupportAPI(account).initialize()
        api.close()
        text = "✅ <b>Авторизация работает.</b>\n\nСтраница поддержки и CSRF-токен получены успешно."
    except Exception as exc:
        logger.exception("%s Проверка авторизации не пройдена", LOGGER_PREFIX)
        text = f"❌ <b>Авторизация не работает.</b>\n\n{_h(_short_error(exc))}"
    keyboard = K()
    keyboard.row(B("◀️ Назад", callback_data=CB_AUTH))
    _edit_by_id(bot, chat_id, message_id, text, keyboard)

def _start_auth_test(bot: Any, call: Any, account: Any) -> None:
    chat_id = int(call.message.chat.id)
    message_id = _message_id(call.message)
    _safe_edit(bot, call, "⏳ <b>Проверяю авторизацию поддержки...</b>", None)
    if not _schedule_job(f"auth-test:{chat_id}", _auth_test_worker, bot, chat_id, message_id, account):
        _answer(bot, call, "Проверка уже выполняется.", True)
        return
    _answer(bot, call)

def _cycle_classification_mode(bot: Any, call: Any) -> None:
    modes = ["none", "local", "ai"]
    current = _cfg().get("classification_mode")
    mode = modes[(modes.index(current) + 1) % len(modes)] if current in modes else "none"
    _set_cfg(classification_mode=mode)
    _safe_edit(bot, call, _text_settings_text(), _text_settings_keyboard())
    _answer(bot, call, "Режим изменён. Для старых заказов запустите переклассификацию.")

def _ai_models_worker(bot: Any, chat_id: int, message_id: int, force: bool = False) -> None:
    error = ""
    try:
        models = _fetch_io_models(force=force)
    except Exception as exc:
        logger.exception("%s Не удалось получить список моделей io.net", LOGGER_PREFIX)
        models = list(IO_MODEL_FALLBACKS)
        error = "Не удалось обновить список через API; показан резервный список. " + _short_error(exc, 120)
    with _AI_MODELS_LOCK:
        _AI_MODEL_LISTS[chat_id] = list(models)
    _edit_by_id(bot, chat_id, message_id, _ai_models_text(models, 0, error), _ai_models_keyboard(models, 0))

def _start_ai_models(bot: Any, call: Any, force: bool = False) -> None:
    chat_id = int(call.message.chat.id)
    message_id = _message_id(call.message)
    _safe_edit(bot, call, "⏳ <b>Получаю список моделей io.net...</b>", None)
    if not _schedule_job(f"ai-models:{chat_id}", _ai_models_worker, bot, chat_id, message_id, force):
        _answer(bot, call, "Список моделей уже загружается.", True)
        return
    _answer(bot, call)

def _open_ai_models_page(bot: Any, call: Any, page: int) -> None:
    chat_id = int(call.message.chat.id)
    with _AI_MODELS_LOCK:
        models = list(_AI_MODEL_LISTS.get(chat_id) or _AI_MODELS_CACHE or IO_MODEL_FALLBACKS)
    total_pages = max(1, (len(models) + AI_MODELS_PER_PAGE - 1) // AI_MODELS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    _safe_edit(bot, call, _ai_models_text(models, page), _ai_models_keyboard(models, page))
    _answer(bot, call)

def _select_ai_model(bot: Any, call: Any, index: int) -> None:
    chat_id = int(call.message.chat.id)
    with _AI_MODELS_LOCK:
        models = list(_AI_MODEL_LISTS.get(chat_id) or _AI_MODELS_CACHE or IO_MODEL_FALLBACKS)
    if not 0 <= index < len(models):
        _answer(bot, call, "Модель не найдена. Обновите список.", True)
        return
    model = models[index]
    _set_cfg(ai_model=model)
    _safe_edit(bot, call, _ai_models_text(models, index // AI_MODELS_PER_PAGE), _ai_models_keyboard(models, index // AI_MODELS_PER_PAGE))
    _answer(bot, call, "Модель выбрана.")
    _log_event("AI_МОДЕЛЬ_ВЫБРАНА", модель=model)

def _toggle_plugin_status(bot: Any, call: Any) -> None:
    enabled = not bool(_cfg().get("plugin_enabled"))
    if enabled and not _auto_target_allowed():
        _answer(bot, call, "Auto Target запретил включение.", True)
        return
    updates: Dict[str, Any] = {"plugin_enabled": enabled}
    if enabled:
        updates["next_scan_at"] = time.time()
    _set_cfg(**updates)
    _safe_edit(bot, call, _status_text(), _status_keyboard())
    _answer(bot, call, "Плагин включён." if enabled else "Плагин выключен.")
    _log_event("СТАТУС_ПЛАГИНА", статус="включён" if enabled else "выключен")

def _plugin_check_worker(bot: Any, chat_id: int, message_id: int, account: Any) -> None:
    checks: List[str] = []
    try:
        test_path = PLUGIN_DIR / ".write-test"
        test_path.write_text("ok", encoding="utf-8")
        test_path.unlink(missing_ok=True)
        checks.append("✅ Хранилище доступно")
    except Exception as exc:
        checks.append("❌ Хранилище: " + _short_error(exc, 100))
    checks.append("✅ Фоновый обработчик запущен" if _BACKGROUND_THREAD and _BACKGROUND_THREAD.is_alive() else "❌ Фоновый обработчик не запущен")
    if _auto_target_allowed():
        checks.append("✅ Auto Target: данные автора подтверждены")
    else:
        checks.append("❌ Auto Target: " + _short_error(_AUTHOR_META_REASON, 120))
    try:
        api = FunPaySupportAPI(account).initialize()
        api.close()
        checks.append("✅ Авторизация поддержки работает")
    except Exception as exc:
        checks.append("❌ Авторизация поддержки: " + _short_error(exc, 120))
    settings = _cfg()
    if settings.get("classification_mode") == "ai":
        checks.append("✅ io.net настроен" if settings.get("ai_api_key") and settings.get("ai_model") else "❌ Для io.net не хватает API-ключа или модели")
    text = "<b>🩺 Проверка Auto Ticket</b>\n\n" + "\n".join(_h(item) for item in checks)
    keyboard = K()
    keyboard.row(B("◀️ Назад", callback_data=CB_STATUS))
    _edit_by_id(bot, chat_id, message_id, text, keyboard)
    _log_event("ПРОВЕРКА_ПЛАГИНА", результат="; ".join(checks))

def _start_plugin_check(bot: Any, call: Any, account: Any) -> None:
    chat_id = int(call.message.chat.id)
    message_id = _message_id(call.message)
    _safe_edit(bot, call, "⏳ <b>Проверяю Auto Ticket...</b>", None)
    if not _schedule_job(f"plugin-check:{chat_id}", _plugin_check_worker, bot, chat_id, message_id, account):
        _answer(bot, call, "Проверка уже выполняется.", True)
        return
    _answer(bot, call)

def _toggle_startup_action(bot: Any, call: Any) -> None:
    value = "send_now" if _cfg().get("startup_action") == "continue" else "continue"
    _set_cfg(startup_action=value)
    _safe_edit(bot, call, _extra_text(), _extra_keyboard())
    _answer(bot, call, "Поведение после запуска изменено.")

def _ai_test_worker(bot: Any, chat_id: int, message_id: int) -> None:
    sample = {
        "order_id": "TEST1234", "product": "Тестовый заказ, покупатель не получил товар",
        "status": "paid", "price": "100", "purchased_at": int(time.time() - 86400),
    }
    try:
        decision = _ai_classify_batch([sample]).get("TEST1234")
        if not decision:
            raise RuntimeError("модель не вернула решение для тестового заказа")
        text = "<b>🤖 Проверка io.net API</b>\n\nОтвет: <b>Да</b>"
    except Exception as exc:
        logger.exception("%s Проверка io.net не пройдена", LOGGER_PREFIX)
        text = "<b>🤖 Проверка io.net API</b>\n\nОтвет: <b>Нет</b>"
    keyboard = K()
    keyboard.row(B("◀️ Назад", callback_data=CB_AI))
    _edit_by_id(bot, chat_id, message_id, text, keyboard)

def _start_ai_test(bot: Any, call: Any) -> None:
    chat_id = int(call.message.chat.id)
    message_id = _message_id(call.message)
    _safe_edit(bot, call, "⏳ <b>Проверяю io.net API...</b>", None)
    if not _schedule_job(f"ai-test:{chat_id}", _ai_test_worker, bot, chat_id, message_id):
        _answer(bot, call, "Проверка уже выполняется.", True)
        return
    _answer(bot, call)

def _reclassify_worker(bot: Any, chat_id: int, message_id: int) -> None:
    records = _all_records()
    try:
        _classify_records(records, force=True)
        text = f"✅ <b>Переклассификация завершена.</b>\n\nОбработано заказов: <b>{len(records)}</b>."
    except Exception as exc:
        logger.exception("%s Ошибка полной переклассификации", LOGGER_PREFIX)
        text = f"❌ <b>Переклассификация не завершена.</b>\n\n{_h(_short_error(exc))}"
    keyboard = K()
    keyboard.row(B("◀️ Назад", callback_data=CB_TEXT))
    _edit_by_id(bot, chat_id, message_id, text, keyboard)

def _start_reclassify(bot: Any, call: Any) -> None:
    chat_id = int(call.message.chat.id)
    message_id = _message_id(call.message)
    _safe_edit(bot, call, "⏳ <b>Переклассифицирую сохранённые заказы...</b>", None)
    if not _schedule_job(f"reclassify:{chat_id}", _reclassify_worker, bot, chat_id, message_id):
        _answer(bot, call, "Переклассификация уже выполняется.", True)
        return
    _answer(bot, call)

def _toggle_setting(bot: Any, call: Any, key: str, route: str = "extra") -> None:
    _set_cfg(**{key: not bool(_cfg().get(key))})
    if route == "extra":
        _safe_edit(bot, call, _extra_text(), _extra_keyboard())
    else:
        _safe_edit(bot, call, _settings_text(), _settings_keyboard())
    _answer(bot, call)

def _ignore_order(bot: Any, call: Any, order_id: str, ignored: bool) -> None:
    record = _order_record(order_id)
    if not record:
        _answer(bot, call, "Заказ не найден.", True)
        return
    _update_order(order_id, ignored=ignored)
    _log_event("ЗАКАЗ_ИГНОР", заказ="#" + order_id, статус="добавлен" if ignored else "убран")
    updated = _order_record(order_id)
    _safe_edit(bot, call, _order_detail_text(updated), _order_detail_keyboard(updated, ignored_view=ignored))
    _answer(bot, call, "Заказ добавлен в игнор." if ignored else "Заказ возвращён в обработку.")

def _send_single_worker(bot: Any, chat_id: int, message_id: int, account: Any, order_id: str) -> None:
    result = _send_single_order(account, order_id)
    if result.get("sent_ids"):
        if order_id in result.get("hard_sent_ids", []):
            destination = "требует разбирательства"
        elif order_id in result.get("easy_sent_ids", []):
            destination = "обычный заказ"
        else:
            destination = "без разделения"
        text = (
            f"✅ <b>Тикет по заказу #{_h(order_id)} отправлен.</b>\n\n"
            f"Категория: <b>{_h(destination)}</b>"
        )
    else:
        errors = result.get("errors", [])
        text = f"❌ <b>Тикет по заказу #{_h(order_id)} не отправлен.</b>\n\n" + "\n".join(f"• {_h(item)}" for item in errors[:8])
    keyboard = K()
    keyboard.row(B("📦 К заказу", callback_data=CB_ORDER + order_id))
    _edit_by_id(bot, chat_id, message_id, text, keyboard)

def _start_send_single(bot: Any, call: Any, account: Any, order_id: str) -> None:
    chat_id = int(call.message.chat.id)
    message_id = _message_id(call.message)
    _safe_edit(bot, call, f"⏳ <b>Отправляю тикет по заказу #{_h(order_id)}...</b>", None)
    if not _schedule_job(f"single:{order_id}", _send_single_worker, bot, chat_id, message_id, account, order_id):
        _answer(bot, call, "Этот заказ уже отправляется.", True)
        return
    _answer(bot, call)

def _scan_worker(bot: Any, chat_id: int, message_id: int, account: Any) -> None:
    try:
        total, new_count = _scan_orders(account)
        text = (
            "✅ <b>Сканирование завершено.</b>\n\n"
            f"Получено оплаченных заказов: <b>{total}</b>\n"
            f"Новых в базе: <b>{new_count}</b>\n"
            f"Готовы к тикету: <b>{len(_eligible_records())}</b>"
        )
    except Exception as exc:
        logger.exception("%s Ручное сканирование завершилось ошибкой", LOGGER_PREFIX)
        text = f"❌ <b>Сканирование не выполнено.</b>\n\n{_h(_short_error(exc))}"
    keyboard = K()
    keyboard.row(B("📦 Открыть заказы", callback_data=CB_ORDERS))
    keyboard.row(B("◀️ В дополнительные настройки", callback_data=CB_EXTRA))
    _edit_by_id(bot, chat_id, message_id, text, keyboard)

def _start_scan(bot: Any, call: Any, account: Any) -> None:
    chat_id = int(call.message.chat.id)
    message_id = _message_id(call.message)
    _safe_edit(bot, call, "⏳ <b>Получаю и сохраняю заказы...</b>", None)
    if not _schedule_job(f"scan:{chat_id}", _scan_worker, bot, chat_id, message_id, account):
        _answer(bot, call, "Сканирование уже выполняется.", True)
        return
    _answer(bot, call)

def _send_cycle_worker(bot: Any, chat_id: int, message_id: int, account: Any) -> None:
    try:
        result = _run_ticket_cycle(account, rescan=True)
        sent_ids = result.get("sent_ids", [])
        errors = result.get("errors", [])
        if sent_ids:
            text = (
                "✅ <b>Отправка завершена.</b>\n\n"
                f"Создано тикетов: <b>{result.get('tickets')}</b>\n"
                f"Отправлено заказов: <b>{len(sent_ids)}</b>\n\n"
                f"{_send_result_distribution(result, 100)}"
            )
            if errors:
                text += "\n\n⚠️ Ошибки:\n" + "\n".join(f"• {_h(item)}" for item in errors[:8])
        elif result.get("selected"):
            text = "❌ <b>Подходящие заказы найдены, но тикеты не отправлены.</b>\n\n" + "\n".join(f"• {_h(item)}" for item in errors[:10])
        else:
            text = "ℹ️ <b>Нет заказов, готовых к отправке.</b>"
    except Exception as exc:
        logger.exception("%s Ручной цикл отправки завершился ошибкой", LOGGER_PREFIX)
        text = f"❌ <b>Отправка не выполнена.</b>\n\n{_h(_short_error(exc))}"
    keyboard = K()
    keyboard.row(B("📦 Открыть заказы", callback_data=CB_ORDERS))
    keyboard.row(B("◀️ В дополнительные настройки", callback_data=CB_EXTRA))
    _edit_by_id(bot, chat_id, message_id, text, keyboard)

def _start_send_cycle(bot: Any, call: Any, account: Any) -> None:
    chat_id = int(call.message.chat.id)
    message_id = _message_id(call.message)
    _safe_edit(bot, call, "⏳ <b>Сканирую заказы и формирую тикеты...</b>", None)
    if not _schedule_job(f"send-cycle:{chat_id}", _send_cycle_worker, bot, chat_id, message_id, account):
        _answer(bot, call, "Отправка уже выполняется.", True)
        return
    _answer(bot, call)

def _send_logs(bot: Any, call: Any) -> None:
    _answer(bot, call)
    try:
        if not LOG_FILE.exists():
            LOG_FILE.touch()
        with LOG_FILE.open("rb") as handle:
            bot.send_document(call.message.chat.id, handle, caption="📄 Логи Auto Ticket")
    except Exception as exc:
        bot.send_message(call.message.chat.id, f"❌ Не удалось отправить логи: {_h(_short_error(exc))}", parse_mode="HTML")

def _export_backup(bot: Any, call: Any) -> None:
    _answer(bot, call)
    try:
        payload = {
            "format": "AutoTicket-backup",
            "backup_version": 1,
            "plugin_version": VERSION,
            "created_at": int(time.time()),
            "settings": _cfg(),
            "orders": {item["order_id"]: item for item in _all_records()},
        }
        document = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        document.name = f"AutoTicket-backup-{time.strftime('%Y%m%d-%H%M%S')}.json"
        bot.send_document(
            call.message.chat.id, document,
            caption="💾 Резервная копия Auto Ticket. В файле есть PHPSESSID и API-ключ, не передавайте его посторонним.",
        )
    except Exception as exc:
        bot.send_message(call.message.chat.id, f"❌ Не удалось создать резервную копию: {_h(_short_error(exc))}", parse_mode="HTML")

def _version_key(value: Any) -> Tuple[int, int, int, int]:
    numbers = [int(item) for item in re.findall(r"\d+", str(value or ""))[:4]]
    numbers.extend([0] * (4 - len(numbers)))
    return tuple(numbers[:4])

def _version_from_source(source: str) -> Optional[str]:
    match = re.search(r"(?m)^\s*VERSION\s*=\s*[\"']([^\"']+)[\"']", source or "")
    return match.group(1).strip() if match else None

def _validate_update(payload: bytes) -> Tuple[str, str]:
    if not payload or len(payload) < 5000:
        raise RuntimeError("файл обновления слишком маленький")
    if len(payload) > 5 * 1024 * 1024:
        raise RuntimeError("файл обновления больше 5 МБ")
    try:
        source = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RuntimeError("файл должен быть в UTF-8") from exc
    required = (NAME, UUID, "BIND_TO_PRE_INIT", "def init_cardinal")
    missing = [item for item in required if item not in source]
    if missing:
        raise RuntimeError("это не Auto Ticket или UUID не совпадает: нет " + ", ".join(missing))
    version = _version_from_source(source)
    if not version:
        raise RuntimeError("VERSION не найдена")
    if _version_key(version) <= _version_key(VERSION):
        raise RuntimeError(f"версия {version} не новее установленной {VERSION}")
    compile(source, str(Path(__file__).resolve()), "exec")
    return source, version

def _install_update(payload: bytes) -> Dict[str, Any]:
    plugin_file = Path(__file__).resolve()
    temporary = plugin_file.with_name(plugin_file.name + ".update.tmp")
    backup = plugin_file.with_name(plugin_file.name + ".pre-update.bak")
    try:
        _, version = _validate_update(payload)
        if SETTINGS_FILE.exists():
            shutil.copy2(SETTINGS_FILE, SETTINGS_BACKUP)
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        with suppress(Exception):
            os.chmod(temporary, plugin_file.stat().st_mode)
        shutil.copy2(plugin_file, backup)
        os.replace(temporary, plugin_file)
        return {"ok": True, "version": version, "backup": backup.name}
    except Exception as exc:
        with suppress(Exception):
            temporary.unlink()
        logger.exception("%s Обновление не установлено", LOGGER_PREFIX)
        return {"ok": False, "error": _short_error(exc)}

def _start_local_update(bot: Any, call: Any) -> None:
    _prompt(
        bot, call,
        "<b>📥 Локальное обновление</b>\n\nПришлите новый файл <code>AutoTicket.py</code>. "
        "Будут проверены UUID, версия и синтаксис. Текущий файл будет сохранён как резервная копия.",
        {"step": "local_update", "return_route": "update"},
    )

def _handle_update_document(message: Any, cardinal: Any) -> None:
    bot = cardinal.telegram.bot
    chat_id = int(message.chat.id)
    state = _fsm_state(chat_id)
    if state.get("step") != "local_update":
        return
    document = getattr(message, "document", None)
    filename = str(getattr(document, "file_name", "") or "")
    with suppress(Exception):
        bot.delete_message(chat_id, _message_id(message))
    message_id = int(state.get("message_id") or 0)
    if not filename.lower().endswith(".py"):
        _edit_by_id(bot, chat_id, message_id, "❌ Нужен файл с расширением <code>.py</code>.", _cancel_keyboard())
        return
    try:
        file_info = bot.get_file(document.file_id)
        payload = bytes(bot.download_file(file_info.file_path))
    except Exception as exc:
        _edit_by_id(bot, chat_id, message_id, f"❌ Файл не скачан: {_h(_short_error(exc))}", _cancel_keyboard())
        return
    result = _install_update(payload)
    _pop_fsm(chat_id)
    keyboard = K()
    keyboard.row(B("◀️ В меню", callback_data=CB_HOME))
    if result.get("ok"):
        text = (
            f"✅ <b>Плагин обновлён до версии {result['version']}.</b>\n\n"
            f"Резервная копия: <code>{_h(result['backup'])}</code>.\n"
            "Выполните <code>/restart</code>, чтобы загрузить новую версию."
        )
    else:
        text = f"❌ <b>Обновление отклонено.</b>\n\n{_h(result.get('error'))}\nТекущий файл не изменён."
    _edit_by_id(bot, chat_id, message_id, text, keyboard)

def _online_update_worker(bot: Any, chat_id: int, message_id: int) -> None:
    url = ONLINE_UPDATE_URL
    keyboard = K()
    keyboard.row(B("◀️ В меню обновления", callback_data=CB_UPDATE))
    if not url:
        _edit_by_id(bot, chat_id, message_id, "❌ URL онлайн-обновления не настроен.", keyboard)
        return
    try:
        request = urllib.request.Request(url, headers={"User-Agent": f"AutoTicket/{VERSION}"})
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read(5 * 1024 * 1024 + 1)
        result = _install_update(payload)
    except Exception as exc:
        result = {"ok": False, "error": _short_error(exc)}
        logger.exception("%s Онлайн-обновление не загружено", LOGGER_PREFIX)
    if result.get("ok"):
        text = (
            f"✅ <b>Плагин обновлён до версии {result['version']}.</b>\n\n"
            "Выполните <code>/restart</code>."
        )
    elif "не новее установленной" in str(result.get("error")):
        text = f"✅ Установлена актуальная версия <code>{VERSION}</code>."
    else:
        text = f"❌ <b>Онлайн-обновление не установлено.</b>\n\n{_h(result.get('error'))}"
    _edit_by_id(bot, chat_id, message_id, text, keyboard)

def _start_online_update(bot: Any, call: Any) -> None:
    chat_id = int(call.message.chat.id)
    message_id = _message_id(call.message)
    _safe_edit(bot, call, "⏳ <b>Скачиваю и проверяю обновление...</b>", None)
    if not _schedule_job(f"online-update:{chat_id}", _online_update_worker, bot, chat_id, message_id):
        _answer(bot, call, "Обновление уже проверяется.", True)
        return
    _answer(bot, call)

def _delete_plugin_from_disk(cardinal: Any, call: Any) -> None:
    bot = cardinal.telegram.bot
    _answer(bot, call, "Удаляю...")
    _stop_background()
    errors: List[str] = []
    plugin_file = Path(__file__).resolve()
    data_dir = PLUGIN_DIR.resolve()
    try:
        for path in (
            plugin_file,
            plugin_file.with_name(plugin_file.name + ".pre-update.bak"),
            plugin_file.with_name(plugin_file.name + ".update.tmp"),
        ):
            if path.is_file() and path.parent == plugin_file.parent:
                path.unlink()
    except Exception as exc:
        errors.append("файл плагина: " + _short_error(exc))
    try:
        if tuple(data_dir.parts[-3:]) != ("storage", "plugins", "AutoTicket"):
            raise RuntimeError("небезопасный путь каталога данных")
        if data_dir.exists():
            shutil.rmtree(data_dir)
    except Exception as exc:
        errors.append("данные: " + _short_error(exc))
    keyboard = K()
    keyboard.row(B("🔙 К списку плагинов", callback_data=CB_PLUGINS_LIST_OPEN))
    if errors:
        text = "⚠️ <b>Удаление выполнено частично.</b>\n\n" + "\n".join(f"• {_h(item)}" for item in errors)
    else:
        text = "✅ <b>Auto Ticket удалён.</b>\n\nВыполните <code>/restart</code>."
    _safe_edit(bot, call, text, keyboard)

def _unpack_author_marker(values: Tuple[int, ...], seed: int) -> str:
    return "".join(
        chr(value ^ ((seed + index * 29 + index % 3 * 7) & 255))
        for index, value in enumerate(values)
    )

_AUTHOR_META_AT_LOAD = {
    "CREDITS": CREDITS,
    "UUID": UUID,
    "CREATOR_URL": CREATOR_URL,
}
_AUTHOR_META_EXPECTED = {
    "CREDITS": _unpack_author_marker(
        (19, 3, 242, 196, 171, 145, 105, 64, 37, 55, 10, 197, 204),
        83,
    ),
    "UUID": _unpack_author_marker(
        (
            144, 255, 222, 154, 68, 34, 99, 72, 176, 206, 232, 205,
            51, 10, 127, 59, 24, 149, 156, 236, 200, 106, 74, 125,
            111, 177, 150, 213, 184, 155, 57, 3, 100, 6, 190, 154,
        ),
        167,
    ),
    "CREATOR_URL": base64.b64decode(
        "aHR0cHM6Ly90Lm1lL3RpbmVjaGVsb3ZlYw=="
    ).decode("utf-8"),
}
_AUTHOR_META_KEYS = ("CREDITS", "UUID", "CREATOR_URL")
_AUTHOR_META_FIELDS = (
    "schema",
    "plugin",
    "credits",
    "uuid",
    "creatorUrl",
    "issuedAt",
    "expiresAt",
)
_AUTHOR_META_API_URL = os.getenv(
    "AUTO_TICKET_AUTHOR_META_API_URL",
    "https://fts-transfer-token.vercel.app/api/plugin-meta?uuid=" + UUID,
).strip()
_AUTHOR_META_RSA_N = int(
    "c0014461db95102dfd52198bb728c80fe31064cdbc8dc4bda004e9603fea7e1c"
    "8f108a11dd44ce07feb44ccbc4077edba3185d305770105caeb7db57e4aafac3"
    "8917306fe9e439349f7349bb767d321dd902e7d829a780dc355daf6c139ead2d"
    "3d48eece29e1ee28bcccd99f7be5a0ac37d6682f1d3fe692531ad543f036fe7a"
    "ba837b436843edf4f565c05c2dab0a1950d5f671b411e254def8c9c08d2d7564"
    "750d1cb38283c4ae6ca1135dbf27266bbe4fd0b6d6dea72e4c7852bfe550b22c"
    "68a170b9fc2f3967617ef4cc5f374a66fc72e89565e7d91d0aa92cc16485514c"
    "0d63ba57bfb100646a828a897469ee4f77d88ca1f32d6d82489b369287a472e7",
    16,
)
_AUTHOR_META_RSA_E = 65537
_AUTHOR_META_SHA256_PREFIX = bytes.fromhex(
    "3031300d060960864801650304020105000420"
)
_AUTHOR_META_CHECK_INTERVAL = max(
    60,
    int(os.getenv("AUTO_TICKET_AUTHOR_META_CHECK_INTERVAL_SEC", "300")),
)
_AUTHOR_META_TIMEOUT = max(
    3.0,
    float(os.getenv("AUTO_TICKET_AUTHOR_META_TIMEOUT_SEC", "10")),
)
_AUTHOR_META_LOCK = threading.RLock()
_AUTHOR_META_WATCH_STARTED = False
_AUTHOR_META_OK = True
_AUTHOR_META_REASON = ""
_AUTO_TARGET_ALLOWED = True
_TAMPER_STATE_FILE = PLUGIN_DIR / ".anti_tamper.json"
_TAMPER_LOCK = threading.RLock()
_TAMPER_WORKER_STARTED = False

def _set_auto_target_state(ok: bool, reason: str = "") -> None:
    global _AUTHOR_META_OK, _AUTHOR_META_REASON, _AUTO_TARGET_ALLOWED
    _AUTHOR_META_OK = bool(ok)
    _AUTO_TARGET_ALLOWED = bool(ok)
    _AUTHOR_META_REASON = "" if ok else str(
        reason or "проверка данных автора не пройдена"
    )

def _meta_guard() -> bool:
    if not _AUTHOR_META_OK:
        return False
    for key in _AUTHOR_META_KEYS:
        expected = _AUTHOR_META_EXPECTED[key]
        loaded = _AUTHOR_META_AT_LOAD.get(key)
        current = globals().get(key)
        if loaded == expected and current == expected:
            continue
        _set_auto_target_state(False, f"изменены данные автора: {key}")
        return False
    return True

def _auto_target_allowed() -> bool:
    return bool(_AUTO_TARGET_ALLOWED and _meta_guard())

def _auto_target_error_result(selected: int = 0) -> Dict[str, Any]:
    reason = _AUTHOR_META_REASON or "проверка Auto Target не пройдена"
    return {
        "selected": selected,
        "sent_ids": [],
        "easy_sent_ids": [],
        "hard_sent_ids": [],
        "all_sent_ids": [],
        "tickets": 0,
        "classification_mode": _cfg().get("classification_mode"),
        "errors": [f"Auto Target запретил действие: {reason}"],
    }

def _author_meta_message(payload: Dict[str, Any]) -> bytes:
    return "\n".join(
        str(payload.get(key, "")) for key in _AUTHOR_META_FIELDS
    ).encode("utf-8")

def _verify_author_meta_signature(
    payload: Dict[str, Any], signature_text: Any
) -> bool:
    try:
        signature = base64.b64decode(
            str(signature_text or ""), validate=True
        )
        size = (_AUTHOR_META_RSA_N.bit_length() + 7) // 8
        if len(signature) != size:
            return False
        encoded = pow(
            int.from_bytes(signature, "big"),
            _AUTHOR_META_RSA_E,
            _AUTHOR_META_RSA_N,
        ).to_bytes(size, "big")
        separator = encoded.find(b"\0", 2)
        digest = _AUTHOR_META_SHA256_PREFIX + hashlib.sha256(
            _author_meta_message(payload)
        ).digest()
        return (
            encoded.startswith(b"\0\1")
            and separator >= 10
            and encoded[2:separator] == b"\xff" * (separator - 2)
            and encoded[separator + 1:] == digest
        )
    except Exception:
        return False

def _fetch_author_meta() -> Tuple[bool, str]:
    request = urllib.request.Request(
        _AUTHOR_META_API_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": f"{NAME}/{VERSION}",
        },
    )
    with urllib.request.urlopen(
        request, timeout=_AUTHOR_META_TIMEOUT
    ) as response:
        envelope = json.loads(response.read().decode("utf-8"))

    if (
        not isinstance(envelope, dict)
        or envelope.get("ok") is not True
        or not isinstance(envelope.get("payload"), dict)
    ):
        return False, "некорректный ответ сервера"

    payload = envelope["payload"]
    if not _verify_author_meta_signature(
        payload, envelope.get("signature")
    ):
        return False, "недействительная серверная подпись"

    now = int(time.time())
    try:
        issued = int(payload.get("issuedAt"))
        expires = int(payload.get("expiresAt"))
    except (TypeError, ValueError):
        return False, "некорректный срок подписи"

    if (
        issued > now + 120
        or expires < now - 30
        or expires <= issued
        or expires - issued > 3600
    ):
        return False, "подпись устарела"

    expected = {
        "schema": 1,
        "plugin": NAME,
        "credits": CREDITS,
        "uuid": UUID,
        "creatorUrl": CREATOR_URL,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        return False, "данные автора не совпадают"
    return True, ""

def _mark_tamper(reason: str) -> None:
    _set_auto_target_state(
        False,
        reason or "проверка данных автора не пройдена",
    )
    _set_cfg(plugin_enabled=False)
    _log_event(
        "AUTO_TARGET_БЛОКИРОВКА",
        logging.ERROR,
        причина=_AUTHOR_META_REASON,
    )

def _load_tamper_state() -> Dict[str, Any]:
    with _TAMPER_LOCK:
        try:
            with _TAMPER_STATE_FILE.open(
                "r", encoding="utf-8"
            ) as handle:
                state = json.load(handle)
            return state if isinstance(state, dict) else {}
        except Exception:
            return {}

def _save_tamper_state(count: int, reason: str = "") -> None:
    with _TAMPER_LOCK:
        _atomic_json(
            _TAMPER_STATE_FILE,
            {
                "restart_count": max(0, int(count)),
                "reason": str(reason or ""),
                "updated_at": int(time.time()),
            },
        )

def _reset_tamper_state() -> None:
    with suppress(Exception):
        _TAMPER_STATE_FILE.unlink()

def _restart_for_tamper(cardinal: Any) -> None:
    restart = getattr(cardinal, "restart", None)
    if callable(restart):
        with suppress(Exception):
            restart()
            time.sleep(2)
    with suppress(Exception):
        args = list(sys.argv) or ["-m", "FunPayCardinal"]
        os.execv(sys.executable, [sys.executable] + args)

def _tamper_restart_worker(
    cardinal: Any, immediate: bool = False
) -> None:
    base = max(
        10,
        int(
            os.getenv(
                "AUTO_TICKET_TAMPER_RESTART_INTERVAL_SEC", "3600"
            )
        ),
    )
    limit = max(
        1,
        int(os.getenv("AUTO_TICKET_TAMPER_MAX_RESTARTS", "1000")),
    )
    state = _load_tamper_state()
    completed = max(0, int(state.get("restart_count", 0) or 0))
    if completed >= limit:
        return

    delay = max(10, int(base / (2 ** completed)))
    if not immediate and _STOP_EVENT.wait(delay):
        return

    state = _load_tamper_state()
    completed = max(0, int(state.get("restart_count", 0) or 0))
    if completed >= limit:
        return

    _save_tamper_state(completed + 1, _AUTHOR_META_REASON)
    _restart_for_tamper(cardinal)

def _start_tamper_restart(
    cardinal: Any, immediate: bool = False
) -> None:
    global _TAMPER_WORKER_STARTED
    with _TAMPER_LOCK:
        if _TAMPER_WORKER_STARTED:
            return
        _TAMPER_WORKER_STARTED = True
    threading.Thread(
        target=_tamper_restart_worker,
        args=(cardinal, immediate),
        daemon=True,
        name="AutoTicket-TAMPER-RESTART",
    ).start()

def _author_meta_watch(cardinal: Any) -> None:
    while not _STOP_EVENT.is_set():
        try:
            ok, reason = _fetch_author_meta()
        except Exception as exc:
            _log_event(
                "AUTO_TARGET_СЕРВЕР_НЕДОСТУПЕН",
                logging.WARNING,
                причина=_short_error(exc),
            )
            if _STOP_EVENT.wait(15):
                return
            continue

        if not ok:
            _mark_tamper(reason)
            _start_tamper_restart(cardinal)
            return

        _set_auto_target_state(True)
        if _STOP_EVENT.wait(_AUTHOR_META_CHECK_INTERVAL):
            return

def _start_author_meta_watch(cardinal: Any) -> None:
    global _AUTHOR_META_WATCH_STARTED
    with _AUTHOR_META_LOCK:
        if _AUTHOR_META_WATCH_STARTED:
            return
        _AUTHOR_META_WATCH_STARTED = True
    threading.Thread(
        target=_author_meta_watch,
        args=(cardinal,),
        daemon=True,
        name="AutoTicket-META-SYNC",
    ).start()

def init_cardinal(cardinal: Any) -> None:
    global _CARDINAL
    local_meta_ok = _meta_guard()
    _CARDINAL = cardinal
    _load_state()
    bot = cardinal.telegram.bot
    account = cardinal.account
    _apply_startup_behavior()
    _refresh_phpsessid_on_start(account)

    def command_home(message: Any) -> None:
        _remember_owner(message.chat.id)
        bot.send_message(
            message.chat.id, _home_text(), parse_mode="HTML", reply_markup=_home_keyboard(),
            disable_web_page_preview=True,
        )

    cardinal.add_telegram_commands(UUID, [
        ("autoticket", "🎫 Открыть Auto Ticket", True),
    ])
    cardinal.telegram.msg_handler(command_home, commands=["autoticket"])
    cardinal.telegram.msg_handler(
        lambda message: _handle_admin_text(message, cardinal),
        func=lambda message: int(message.chat.id) in _FSM,
        content_types=["text"],
    )
    cardinal.telegram.msg_handler(
        lambda message: _handle_update_document(message, cardinal),
        func=lambda message: _fsm_state(int(message.chat.id)).get("step") == "local_update",
        content_types=["document"],
    )

    tg = cardinal.telegram
    tg.cbq_handler(
        lambda call: _open_home(bot, call),
        func=lambda call: call.data in {CB_HOME, f"{UUID}:0"}
        or call.data.startswith(f"{getattr(CBT, 'EDIT_PLUGIN', '45')}:{UUID}")
        or call.data.startswith(f"{getattr(CBT, 'PLUGIN_SETTINGS', '46')}:{UUID}"),
    )
    tg.cbq_handler(lambda call: _open_settings(bot, call), func=lambda call: call.data == CB_SETTINGS)
    tg.cbq_handler(lambda call: _open_status(bot, call), func=lambda call: call.data == CB_STATUS)
    tg.cbq_handler(lambda call: _toggle_plugin_status(bot, call), func=lambda call: call.data == CB_STATUS_TOGGLE)
    tg.cbq_handler(lambda call: _start_plugin_check(bot, call, account), func=lambda call: call.data == CB_STATUS_CHECK)
    tg.cbq_handler(lambda call: _open_info(bot, call), func=lambda call: call.data == CB_INFO)
    tg.cbq_handler(lambda call: _open_update(bot, call), func=lambda call: call.data == CB_UPDATE)
    tg.cbq_handler(lambda call: _start_local_update(bot, call), func=lambda call: call.data == CB_UPDATE_LOCAL)
    tg.cbq_handler(lambda call: _start_online_update(bot, call), func=lambda call: call.data == CB_UPDATE_ONLINE)
    tg.cbq_handler(
        lambda call: _start_text_input(
            bot, call, "online_update_url", "URL онлайн-обновления",
            _cfg().get("online_update_url", ""), "update",
        ),
        func=lambda call: call.data == CB_UPDATE_URL,
    )
    tg.cbq_handler(lambda call: _open_delete_confirm(bot, call), func=lambda call: call.data == CB_DELETE_ASK)
    tg.cbq_handler(lambda call: _delete_plugin_from_disk(cardinal, call), func=lambda call: call.data == CB_DELETE_YES)
    tg.cbq_handler(lambda call: _open_home(bot, call), func=lambda call: call.data == CB_DELETE_NO)

    tg.cbq_handler(lambda call: _open_auth(bot, call), func=lambda call: call.data == CB_AUTH)
    tg.cbq_handler(lambda call: _toggle_auth_mode(bot, call), func=lambda call: call.data == CB_AUTH_MODE)
    tg.cbq_handler(
        lambda call: _start_text_input(
            bot, call, "phpsessid", "Введите PHPSESSID",
            _masked(_cfg().get("phpsessid", "")), "auth",
        ),
        func=lambda call: call.data == CB_AUTH_SET,
    )
    tg.cbq_handler(lambda call: _clear_phpsessid(bot, call), func=lambda call: call.data == CB_AUTH_CLEAR)
    tg.cbq_handler(lambda call: _start_auth_test(bot, call, account), func=lambda call: call.data == CB_AUTH_TEST)

    tg.cbq_handler(lambda call: _open_text_settings(bot, call), func=lambda call: call.data == CB_TEXT)
    tg.cbq_handler(lambda call: _cycle_classification_mode(bot, call), func=lambda call: call.data == CB_CLASS_MODE)
    tg.cbq_handler(
        lambda call: _start_text_input(
            bot, call, "template_single", "Текст тикета без разделения", _cfg().get("message_template", ""), "text",
        ), func=lambda call: call.data == CB_TEMPLATE_SINGLE,
    )
    tg.cbq_handler(
        lambda call: _start_text_input(
            bot, call, "template_easy", "Текст обычных заказов", _cfg().get("easy_template", ""), "text",
        ), func=lambda call: call.data == CB_TEMPLATE_EASY,
    )
    tg.cbq_handler(
        lambda call: _start_text_input(
            bot, call, "template_hard", "Текст проблемных заказов", _cfg().get("hard_template", ""), "text",
        ), func=lambda call: call.data == CB_TEMPLATE_HARD,
    )
    tg.cbq_handler(
        lambda call: _start_text_input(
            bot, call, "keywords", "Признаки проблемного заказа",
            ", ".join(_cfg().get("local_hard_keywords", [])), "text",
        ), func=lambda call: call.data == CB_KEYWORDS,
    )
    tg.cbq_handler(lambda call: _open_ai(bot, call), func=lambda call: call.data == CB_AI)
    tg.cbq_handler(
        lambda call: _start_text_input(bot, call, "ai_key", "API-ключ io.net", _masked(_cfg().get("ai_api_key", ""), 4), "ai"),
        func=lambda call: call.data == CB_AI_KEY,
    )
    tg.cbq_handler(lambda call: _start_ai_models(bot, call), func=lambda call: call.data == CB_AI_MODELS)
    tg.cbq_handler(lambda call: _start_ai_models(bot, call, force=True), func=lambda call: call.data == CB_AI_MODELS_REFRESH)
    tg.cbq_handler(
        lambda call: _open_ai_models_page(bot, call, int(call.data[len(CB_AI_MODELS_PAGE):])),
        func=lambda call: call.data.startswith(CB_AI_MODELS_PAGE),
    )
    tg.cbq_handler(
        lambda call: _select_ai_model(bot, call, int(call.data[len(CB_AI_MODEL_SELECT):])),
        func=lambda call: call.data.startswith(CB_AI_MODEL_SELECT),
    )
    tg.cbq_handler(lambda call: _start_ai_test(bot, call), func=lambda call: call.data == CB_AI_TEST)
    tg.cbq_handler(lambda call: _start_reclassify(bot, call), func=lambda call: call.data == CB_RECLASSIFY)

    tg.cbq_handler(lambda call: _open_intervals(bot, call), func=lambda call: call.data == CB_INTERVALS)
    tg.cbq_handler(lambda call: _open_extra(bot, call), func=lambda call: call.data == CB_EXTRA)
    tg.cbq_handler(lambda call: _toggle_setting(bot, call, "notify_enabled", "extra"), func=lambda call: call.data == CB_TOGGLE_NOTIFY)
    tg.cbq_handler(lambda call: _toggle_startup_action(bot, call), func=lambda call: call.data == CB_STARTUP_ACTION)
    tg.cbq_handler(lambda call: _start_number_input(bot, call, "scan_interval_hours", "Интервал сканирования, часы", 1, 720), func=lambda call: call.data == CB_SET_SCAN)
    tg.cbq_handler(lambda call: _start_number_input(bot, call, "send_interval_hours", "Интервал отправки, часы", 1, 720), func=lambda call: call.data == CB_SET_SEND)
    tg.cbq_handler(lambda call: _start_number_input(bot, call, "order_age_hours", "Возраст заказа для тикета, часы", 1, 2160), func=lambda call: call.data == CB_SET_AGE)
    tg.cbq_handler(lambda call: _start_number_input(bot, call, "max_orders_in_ticket", "Заказов в одном тикете", 1, 650), func=lambda call: call.data == CB_SET_COUNT)

    tg.cbq_handler(lambda call: _open_orders(bot, call), func=lambda call: call.data == CB_ORDERS)
    tg.cbq_handler(lambda call: _open_orders(bot, call, int(call.data[len(CB_ORDERS_PAGE):])), func=lambda call: call.data.startswith(CB_ORDERS_PAGE))
    tg.cbq_handler(lambda call: _open_order_detail(bot, call, call.data[len(CB_ORDER):]), func=lambda call: call.data.startswith(CB_ORDER) and not any(call.data.startswith(prefix) for prefix in (CB_ORDER_SEND, CB_ORDER_IGNORE, CB_ORDER_UNIGNORE)))
    tg.cbq_handler(lambda call: _start_send_single(bot, call, account, call.data[len(CB_ORDER_SEND):]), func=lambda call: call.data.startswith(CB_ORDER_SEND))
    tg.cbq_handler(lambda call: _ignore_order(bot, call, call.data[len(CB_ORDER_IGNORE):], True), func=lambda call: call.data.startswith(CB_ORDER_IGNORE))
    tg.cbq_handler(lambda call: _ignore_order(bot, call, call.data[len(CB_ORDER_UNIGNORE):], False), func=lambda call: call.data.startswith(CB_ORDER_UNIGNORE))

    tg.cbq_handler(lambda call: _open_ignored(bot, call), func=lambda call: call.data == CB_IGNORED)
    tg.cbq_handler(lambda call: _open_ignored(bot, call, int(call.data[len(CB_IGNORED_PAGE):])), func=lambda call: call.data.startswith(CB_IGNORED_PAGE))
    tg.cbq_handler(lambda call: _open_order_detail(bot, call, call.data[len(CB_IGNORED_ORDER):], ignored_view=True), func=lambda call: call.data.startswith(CB_IGNORED_ORDER))

    tg.cbq_handler(lambda call: _open_maintenance(bot, call), func=lambda call: call.data == CB_MAINTENANCE)
    tg.cbq_handler(lambda call: _start_scan(bot, call, account), func=lambda call: call.data == CB_SCAN_NOW)
    tg.cbq_handler(lambda call: _start_send_cycle(bot, call, account), func=lambda call: call.data == CB_SEND_NOW)
    tg.cbq_handler(lambda call: _send_logs(bot, call), func=lambda call: call.data == CB_LOGS)
    tg.cbq_handler(lambda call: _export_backup(bot, call), func=lambda call: call.data == CB_EXPORT)
    tg.cbq_handler(lambda call: _cancel_fsm(bot, call), func=lambda call: call.data == CB_CANCEL)

    _STOP_EVENT.clear()
    if local_meta_ok:
        _reset_tamper_state()
    else:
        _mark_tamper(_AUTHOR_META_REASON)
        _start_tamper_restart(cardinal)
    _start_author_meta_watch(cardinal)
    _start_background(account)
    _log_event(
        "ПЛАГИН_ЗАПУЩЕН",
        версия=VERSION,
        статус="включён" if _cfg().get("plugin_enabled") else "выключен",
        auto_target="разрешён" if _auto_target_allowed() else "заблокирован",
    )

def new_order_handler(cardinal: Any, event: Any) -> None:
    if not _auto_target_allowed():
        return
    try:
        order = getattr(event, "order", None) or event
        record = _order_to_record(order)
        if not record.get("order_id"):
            return
        _bulk_update_orders([(record["order_id"], {k: v for k, v in record.items() if k != "order_id"})])
        _classify_records([record])
        _log_event("НОВЫЙ_ЗАКАЗ", заказ="#" + record["order_id"], товар=record.get("product"), покупатель=record.get("buyer"))
    except Exception:
        logger.exception("%s Не удалось сохранить новый заказ из события", LOGGER_PREFIX)

def delete_handler(cardinal: Any, *args: Any) -> None:
    global _CARDINAL
    _stop_background()
    _CARDINAL = None

BIND_TO_PRE_INIT = [init_cardinal]
BIND_TO_NEW_ORDER = [new_order_handler]
BIND_TO_DELETE = [delete_handler]
