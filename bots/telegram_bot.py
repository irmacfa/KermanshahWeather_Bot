import asyncio
import html
import logging
import os
import re
import sys

from collections import deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from apscheduler.triggers.cron import CronTrigger

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.sites import SITES
from core.tracking import (
    clear_refresh,
    create_event,
    delete_event_group,
    get_group_events,
    format_time,
    get_active_groups_data,
    get_admin_report,
    get_admin_stats,
    get_all_events_for_chat,
    get_attendance_users,
    get_attendees,
    get_event,
    get_group_users_unregistered,
    get_latest_event_for_chat,
    get_pending_refreshes,
    get_profile,
    get_report_hours,
    get_settings,
    get_top_sites,
    link_events,
    record_attendance,
    record_group_membership,
    record_interaction,
    record_site_view,
    remove_attendance,
    remove_attendee_from_group,
    set_default_site,
    set_event_active,
    set_event_message,
    set_last_report_message,
    set_profile,
    set_report_chat,
    set_report_hours,
    update_attendee_name_in_group,
    update_event_title,
    update_profile_name,
    delete_profile,
)
from core.weather import (
    FIXED_HOURS,
    format_dual_estimate_daily,
    format_models_daily,
    format_models_message,
    get_multi_model_daily,
    get_multi_model_wind,
)

load_dotenv()

_platform = "telegram"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()],
)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

HTML_OK = True
_TAG_RE = re.compile(r"</?(?:b|strong|i|em|u|s|code|pre|a|span)(?:\s[^>]*)?>", re.IGNORECASE)


def _fmt(text: str) -> str:
    if HTML_OK or not text:
        return text
    return html.unescape(_TAG_RE.sub("", text))


def _html_kwargs() -> dict:
    return {"parse_mode": "HTML"} if HTML_OK else {}

_owner_id_raw = os.getenv("BOT_OWNER_ID")
OWNER_ID = int(_owner_id_raw) if _owner_id_raw else None

TEHRAN_TZ = ZoneInfo("Asia/Tehran")
DAY_LABELS = {"today": "امروز", "tomorrow": "فردا", "dat": "پس‌فردا"}
DAY_OFFSETS = {"today": 0, "tomorrow": 1, "dat": 2}
SITE_KEYWORDS = {"سایت", "سایت‌ها", "سایتها", "لیست سایت‌ها", "لیست سایتها"}


def sites_keyboard(owner_id: int) -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, (name, _, _, _) in enumerate(SITES):
        row.append(InlineKeyboardButton(name, callback_data=f"{owner_id}:s|{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


CLOCK_EMOJIS = ["🕛", "🕐", "🕑", "🕒", "🕓", "🕔", "🕕", "🕖", "🕗", "🕘", "🕙", "🕚"]


def _clock_emoji(hour: int) -> str:
    return CLOCK_EMOJIS[hour % 12]


def time_choice_keyboard(owner_id: int, site_idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⚡ الان", callback_data=f"{owner_id}:t|{site_idx}|now")],
            [
                InlineKeyboardButton("🟢 امروز", callback_data=f"{owner_id}:t|{site_idx}|today"),
                InlineKeyboardButton("🔵 فردا", callback_data=f"{owner_id}:t|{site_idx}|tomorrow"),
                InlineKeyboardButton("🟣 پس‌فردا", callback_data=f"{owner_id}:t|{site_idx}|dat"),
            ],
            [InlineKeyboardButton("🔙 بازگشت به سایت‌ها", callback_data=f"{owner_id}:b|sites")],
        ]
    )


def hour_choice_keyboard(owner_id: int, site_idx: int, day: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for h in FIXED_HOURS:
        row.append(InlineKeyboardButton(f"{_clock_emoji(h)} {h}:00", callback_data=f"{owner_id}:h|{site_idx}|{day}|{h}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("📅 نمای کل روز", callback_data=f"{owner_id}:d|{site_idx}|{day}")])
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"{owner_id}:s|{site_idx}")])
    return InlineKeyboardMarkup(rows)


GROUP_CLEANUP_DELAY = 15 * 60


async def _cleanup_group_messages(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_ids: list[int]) -> None:
    await asyncio.sleep(GROUP_CLEANUP_DELAY)
    for message_id in message_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            logger.debug("Could not delete message %s in chat %s (already gone?)", message_id, chat_id)


async def show_sites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message or update.effective_message
    if message is None:
        return
    owner_id = update.effective_user.id
    sent = await message.chat.send_message("یه سایت رو انتخاب کن 🪂", reply_markup=sites_keyboard(owner_id))

    if update.effective_chat.type in ("group", "supergroup"):
        asyncio.create_task(
            _cleanup_group_messages(
                context, update.effective_chat.id, [message.message_id, sent.message_id]
            )
        )


def _display_name(user) -> str:
    if user.username:
        return f"@{user.username}"
    return user.first_name or str(user.id)


def _valid_full_name(text: str) -> bool:
    if not text or len(text) > 60:
        return False
    if re.search(r"[0-9?؟!.,،:;؛/\\@#$%^&*()\[\]{}<>|+=_~`\"']", text):
        return False
    if "http" in text.lower():
        return False
    parts = text.split()
    return 2 <= len(parts) <= 5


def _confirm_name_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ بله، درست است", callback_data="nc:ok"),
                InlineKeyboardButton("✏️ ویرایش", callback_data="nc:edit"),
            ]
        ]
    )


def _remember_flow_message(
    context: ContextTypes.DEFAULT_TYPE, chat_id, message_id, key: str = "name_flow_msgs"
) -> None:
    if chat_id is None or message_id is None:
        return
    context.user_data.setdefault(key, []).append(
        {"chat_id": chat_id, "message_id": message_id}
    )


async def _delete_flow_messages(context: ContextTypes.DEFAULT_TYPE, entries: list[dict], delay: float = 0.0) -> None:
    if delay:
        await asyncio.sleep(delay)
    for entry in entries:
        try:
            await context.bot.delete_message(chat_id=entry["chat_id"], message_id=entry["message_id"])
        except Exception as exc:
            logger.warning(
                "Could not delete flow message %s in %s: %s",
                entry["message_id"], entry["chat_id"], exc,
            )


def _name_prompt_text(first_name, username=None) -> str:
    name = (str(first_name).strip() if first_name else "") or "دوست عزیز"
    handle = f" (@{username})" if username else ""
    return (
        f"👤 {name} جان{handle}\n\n"
        "نام و نام خانوادگیت رو دقیق بنویس:\n"
        "مثال: علی رضایی"
    )


async def _deliver_name_prompt(
    context: ContextTypes.DEFAULT_TYPE,
    user,
    trigger_chat_id,
    trigger_message_id=None,
    pending: dict | None = None,
    remember_trigger: bool = False,
    prefer_dm: bool = True,
) -> bool:
    # Do not overwrite an unfinished name flow for this user.
    if context.user_data.get("awaiting_name") or context.user_data.get("pending_name") is not None:
        notice = (
            "یه ثبت‌نام نیمه‌کاره داری.\n"
            "اول همون رو با دکمه «تأیید» یا «ویرایش» تموم کن؛ "
            "بعد برای ایونت بعدی ثبت‌نام کن."
        )
        try:
            await context.bot.send_message(chat_id=user.id, text=notice)
        except Exception:
            try:
                await context.bot.send_message(
                    chat_id=trigger_chat_id,
                    text=notice,
                    reply_to_message_id=trigger_message_id,
                )
            except Exception:
                logger.debug("Could not notify user about pending name flow")
        return False
    context.user_data["awaiting_name"] = True
    context.user_data["pending_action"] = pending or {"type": "none"}
    if remember_trigger and trigger_message_id is not None:
        _remember_flow_message(context, trigger_chat_id, trigger_message_id)

    text = _name_prompt_text(getattr(user, "first_name", None), getattr(user, "username", None))

    if prefer_dm and trigger_chat_id != getattr(user, "id", None):
        try:
            dm = await context.bot.send_message(chat_id=user.id, text=text)
            _remember_flow_message(context, dm.chat_id, dm.message_id)
            return True
        except Exception:
            logger.debug("DM name prompt failed for %s, falling back to chat", getattr(user, "id", None))

    try:
        sent = await context.bot.send_message(
            chat_id=trigger_chat_id, text=text, reply_to_message_id=trigger_message_id
        )
        _remember_flow_message(context, sent.chat_id, sent.message_id)
    except Exception:
        logger.warning("Could not send name prompt to chat %s", trigger_chat_id)
    return False


async def prompt_for_name(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict | None = None) -> None:
    user = update.effective_user
    message = update.message or update.effective_message
    if user is None:
        return
    await _deliver_name_prompt(
        context,
        user,
        update.effective_chat.id,
        message.message_id if message else None,
        pending,
        remember_trigger=message is not None,
    )


async def _complete_attendance(context: ContextTypes.DEFAULT_TYPE, event_id, user, name: str) -> bool:
    if not event_id:
        return False
    try:
        eid = int(event_id)
    except (TypeError, ValueError):
        return False
    event = get_event(eid)
    if not event or not event.get("active", 1):
        return False
    record_attendance(eid, user, name=name, platform=_platform)
    attendees = get_attendees(eid)
    chat_id, message_id = event.get("chat_id"), event.get("message_id")
    if chat_id and message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=_fmt(build_event_text(event, attendees)),
                reply_markup=event_keyboard(eid, len(attendees)),
                **_html_kwargs(),
            )
        except Exception:
            logger.debug("Could not refresh event message after name entry", exc_info=True)
    return True


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message or update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if get_profile(user.id, _platform) is None:
        await prompt_for_name(update, context, {"type": "sites"})
        return
    await show_sites(update, context)


async def name_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message or update.effective_message
    if message is None:
        return
    await prompt_for_name(update, context, {"type": "none"})


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message or update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    text = (message.text or "").strip()

    admin_target = context.user_data.get("admin_event_create_target")
    if admin_target is not None:
        if OWNER_ID is None or user.id != OWNER_ID or message.chat.type != "private":
            return
        if not text or len(text) < 3 or len(text) > 80 or "\n" in text or text.startswith("/"):
            await message.reply_text("عنوان باید یک خط و بین ۳ تا ۸۰ نویسه باشد. دوباره عنوان ایونت را بفرست:")
            return
        context.user_data.pop("admin_event_create_target", None)
        context.user_data["admin_event_draft"] = {"title": text, "chat_id": int(admin_target), "user_id": user.id, "platform": _platform}
        preview = ("📋 <b>پیش‌نمایش ایونت جدید</b>\n\n" f"عنوان: <b>{_esc(text)}</b>\n" f"شناسه گروه مقصد: <code>{int(admin_target)}</code>\n\n" "برای ارسال به گروه، انتشار را تأیید کن.")
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ تأیید و انتشار در گروه", callback_data="ad:evpublish")], [InlineKeyboardButton("❌ لغو", callback_data="ad:evcancel")]])
        await message.reply_text(_fmt(preview), reply_markup=keyboard, **_html_kwargs())
        return

    if (
        context.user_data.get("awaiting_event_title")
        and context.user_data.get("awaiting_event_title_chat_id") == message.chat_id
    ):
        if not text or len(text) < 3 or len(text) > 80 or "\n" in text or text.startswith("/"):
            await message.reply_text("عنوان باید یک خط و بین ۳ تا ۸۰ نویسه باشد. فقط عنوان برنامه را بفرست:")
            return
        context.user_data.pop("awaiting_event_title", None)
        context.user_data.pop("awaiting_event_title_chat_id", None)
        await _show_event_draft(update, context, text)
        return

    me_action = context.user_data.get("me_action")
    if me_action and me_action.get("chat_id", message.chat_id) == message.chat_id:
        context.user_data.pop("me_action", None)
    else:
        me_action = None
    if me_action:
        if me_action.get("admin_panel"):
            if OWNER_ID is None or user.id != OWNER_ID or message.chat.type != "private":
                return
        elif not await _can_manage_event(update, context):
            return
        target_chat_id = me_action.get("target_chat_id", message.chat_id)
        if me_action.get("type") == "rename_event":
            eid = me_action.get("event_id")
            ev = get_event(eid)
            if not text or len(text) > 100:
                context.user_data["me_action"] = me_action
                await message.reply_text("عنوان باید بین ۱ تا ۱۰۰ نویسه باشد. دوباره بنویس:")
                return
            if not ev or ev.get("chat_id") != target_chat_id or ev.get("platform") != _platform:
                await message.reply_text("این برنامه دیگر در دسترس نیست یا متعلق به این گروه نیست.")
                return
            try:
                update_event_title(eid, text)
            except Exception:
                logger.exception("Could not save event title for %s", eid)
                await message.reply_text("ذخیره عنوان برنامه انجام نشد.")
                return

            await message.reply_text("عنوان برنامه ویرایش شد.")
            manage_id = me_action.get("manage_message_id")
            if manage_id:
                try:
                    attendees = get_attendees(eid)
                    updated = get_event(eid)
                    view = (
                        f"🛠 <b>مدیریت برنامه</b>\n\n📅 <b>{_esc(updated['title'])}</b>"
                        f"\n👥 حاضرین: <b>{len(attendees)}</b>"
                        f"\nوضعیت: {'فعال' if updated.get('active',1) else 'بسته'}"
                        f"\n<code>#{eid}</code>"
                    )
                    await context.bot.edit_message_text(
                        chat_id=message.chat_id,
                        message_id=manage_id,
                        text=_fmt(view),
                        reply_markup=(
                            admin_manage_event_keyboard(eid, target_chat_id, updated.get("active", 1))
                            if me_action.get("admin_panel")
                            else manage_event_keyboard(eid, updated.get("active", 1))
                        ),
                        **_html_kwargs(),
                    )
                except Exception:
                    logger.exception("Event %s was renamed but admin view could not refresh", eid)
                    await message.reply_text("عنوان ثبت شد؛ اما صفحه مدیریت به‌روزرسانی نشد.")
            return
        if me_action.get("type") == "rename_att":
            eid = me_action.get("event_id")
            uid = me_action.get("user_id")
            page = me_action.get("page") or 0
            platform = me_action.get("platform") or _platform
            if not _valid_full_name(text):
                context.user_data["me_action"] = me_action
                await message.reply_text("نام باید شامل نام و نام خانوادگی و بدون عدد یا علامت باشد. دوباره بنویس:")
                return
            ev = get_event(eid)
            if not ev or ev.get("chat_id") != target_chat_id or ev.get("platform") != _platform:
                await message.reply_text("این برنامه دیگر در دسترس نیست یا متعلق به این گروه نیست.")
                return
            partsn = text.strip().split(None, 1)
            fn = partsn[0]
            ln = partsn[1] if len(partsn) > 1 else None
            if not any(
                a["user_id"] == uid and (a.get("platform") or _platform) == platform
                for a in get_attendees(eid)
            ):
                await message.reply_text("این شخص دیگر در فهرست حاضرین نیست.")
                return
            try:
                update_attendee_name_in_group(eid, uid, fn, ln, platform=platform)
            except Exception:
                logger.exception("Could not save attendee %s name in event %s", uid, eid)
                await message.reply_text("ذخیره نام حاضر انجام نشد.")
                return

            await message.reply_text("نام حاضر ثبت شد.")
            manage_id = me_action.get("manage_message_id")
            if manage_id:
                try:
                    attendees = get_attendees(eid)
                    await context.bot.edit_message_text(
                        chat_id=message.chat_id,
                        message_id=manage_id,
                        text="👥 حاضرین:\n(با دکمه‌ها میشه نام ویرایش یا حذف شه)",
                        reply_markup=(
                            admin_manage_attendees_keyboard(eid, target_chat_id, attendees, page)
                            if me_action.get("admin_panel")
                            else manage_attendees_keyboard(eid, attendees, page)
                        ),
                    )
                except Exception:
                    logger.exception("Attendee %s was renamed but admin list could not refresh", uid)
                    await message.reply_text("نام ثبت شد؛ اما فهرست مدیریت به‌روزرسانی نشد.")
            return

    admin_edit = context.user_data.pop("admin_edit", None)
    if admin_edit:
        if OWNER_ID is None or user.id != OWNER_ID:
            return
        if admin_edit.get("type") == "user":
            uid = admin_edit.get("user_id")
            plat = admin_edit.get("platform") or _platform
            pg = admin_edit.get("page") or 0
            update_profile_name(uid, text, plat)
            await message.reply_text("نام کاربر ویرایش شد.")
            try:
                textu, keyboardu = build_users_page(pg)
                await message.chat.send_message(_fmt(textu), reply_markup=keyboardu, **_html_kwargs())
            except Exception:
                pass
            return
        if admin_edit.get("type") == "ureg":
            uid = admin_edit.get("user_id")
            pg = admin_edit.get("page") or 0
            update_profile_name(uid, text, _platform)
            await message.reply_text("نام کاربر ثبت شد.")
            try:
                textu, keyboardu = build_unreg_page(pg)
                await message.chat.send_message(_fmt(textu), reply_markup=keyboardu, **_html_kwargs())
            except Exception:
                pass
            return

    create_title = _extract_event_title(text)
    if create_title is not None and not context.user_data.get("awaiting_name"):
        await event_create_flow(update, context, create_title)
        return

    if context.user_data.get("awaiting_name"):
        if not _valid_full_name(text):
            _remember_flow_message(context, message.chat_id, message.message_id)
            hint = await message.chat.send_message(
                "⚠️ نام و نام خانوادگیت رو دقیق بنویس (بدون عدد و علامت).\n"
                "مثال: علی رضایی"
            )
            _remember_flow_message(context, hint.chat_id, hint.message_id)
            return
        context.user_data.pop("awaiting_name", None)
        context.user_data["pending_name"] = text.strip()
        context.user_data["pending_name_msg"] = {"chat_id": message.chat_id, "message_id": message.message_id}
        confirm = await message.chat.send_message(
            f"ایا اسم «{text.strip()}» درست هست؟",
            reply_markup=_confirm_name_keyboard(),
        )
        _remember_flow_message(context, confirm.chat_id, confirm.message_id)
        return

    if text in MANAGE_EVENT_KEYWORDS:
        if not await _can_manage_event(update, context):
            await message.reply_text("فقط ادمین می‌تونه برنامه‌ها رو مدیریت کنه.")
            return
        events = get_all_events_for_chat(update.effective_chat.id, _platform)
        if not events:
            await message.reply_text("هیچ برنامه‌ای در این چت نیست.")
            return
        await message.reply_text("برنامه‌ای رو برای مدیریت انتخاب کن:", reply_markup=manage_events_keyboard(events))
        return

    if text in SITE_KEYWORDS:
        if get_profile(user.id, _platform) is None:
            await prompt_for_name(update, context, {"type": "sites"})
        else:
            await show_sites(update, context)
    elif text in EVENT_KEYWORDS:
        if get_profile(user.id, _platform) is None:
            await prompt_for_name(update, context, {"type": "events"})
        else:
            await show_events(update, context)


EVENT_KEYWORDS = {
    "برنامه",
    "برنامه‌ها",
    "برنامه ها",
    "برنامهها",
    "لیست برنامه‌ها",
    "لیست برنامه ها",
    "لیست برنامهها",
    "سفر",
    "سفرها",
    "ایونت",
    "ایونت ها",
    "ایونتها",
    "ایونت‌ها",
}
EVENT_MAX_LIST = 50
MANAGE_EVENT_KEYWORDS = {"مدیریت برنامه", "مدیریت برنامه‌ها", "مدیریت برنامهها", "مدیریت ایونت", "مدیریت ایونتها"}


def _event_display_name(first_name, last_name, username=None) -> str:
    name = " ".join(p for p in (first_name, last_name) if p).strip() or "بدون نام"
    return f"{name} (@{username})" if username else name


PLATFORM_TAG = {"telegram": "تلگرام"}


def build_event_text(event: dict, attendees: list[dict]) -> str:
    title = html.escape(str(event.get("title") or "برنامه"))
    own_platform = event.get("platform")
    lines = [f"📅 <b>{title}</b>", "", f"👥 تعداد حاضرین: <b>{len(attendees)}</b>", ""]
    if attendees:
        lines.append("✅ <b>لیست حاضرین:</b>")
        for i, a in enumerate(attendees[:EVENT_MAX_LIST], 1):
            label = _event_display_name(a["first_name"], a["last_name"], a["username"])
            tag = ""
            if a.get("platform") and own_platform and a["platform"] != own_platform:
                tag = f" — {PLATFORM_TAG.get(a['platform'], a['platform'])}"
            lines.append(f"{i}. {html.escape(label)}{tag}")
        if len(attendees) > EVENT_MAX_LIST:
            lines.append(f"… و {len(attendees) - EVENT_MAX_LIST} نفر دیگر")
    else:
        lines.append("هنوز کسی حضور خودش رو ثبت نکرده.")
    lines.append("")
    if not event.get("active", 1):
        lines.append("🔒 <b>این برنامه بسته شده و ثبت حضور جدید امکان‌پذیر نیست.</b>")
    else:
        lines.append("🙋 اگه در این برنامه شرکت می‌کنی روی «✅ حضور» بزن؛ اسمت خودکار ثبت می‌شه.")
    lines.append(f"<code>#{event.get('event_id')}</code>")
    return "\n".join(lines)


def manage_events_keyboard(events: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for ev in events:
        title = ev["title"][:40]
        status = "" if ev.get("active", 1) else " [بسته]"
        buttons.append([InlineKeyboardButton(f"#{ev['event_id']} {title}{status}", callback_data=f"me:view|{ev['event_id']}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="me:list")])
    return InlineKeyboardMarkup(buttons)


def event_keyboard(event_id: int, attendee_count: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ حضور", callback_data=f"ev:a|{event_id}"),
                InlineKeyboardButton("❌ لغو حضور", callback_data=f"ev:c|{event_id}"),
            ],
            [
                InlineKeyboardButton(
                    f"👥 حاضرین ({attendee_count})", callback_data=f"ev:v|{event_id}|0"
                ),
                InlineKeyboardButton("✏️ ثبت نام", callback_data=f"ev:n|{event_id}"),
            ],
        ]
    )


def manage_event_keyboard(event_id: int, active: int = 1) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("👥 مدیریت حاضرین", callback_data=f"me:att|{event_id}|0")],
        [InlineKeyboardButton("✏️ ویرایش عنوان", callback_data=f"me:ren|{event_id}")],
    ]
    if active:
        buttons.append([InlineKeyboardButton("🔒 بستن برنامه", callback_data=f"me:close|{event_id}")])
    else:
        buttons.append([InlineKeyboardButton("🔓 باز کردن برنامه", callback_data=f"me:open|{event_id}")])
    buttons.append([InlineKeyboardButton("🗑 حذف برنامه", callback_data=f"me:del|{event_id}")])
    buttons.append([InlineKeyboardButton("🔙 لیست برنامه‌ها", callback_data="me:list")])
    return InlineKeyboardMarkup(buttons)


def event_attendees_keyboard(event_id: int, page: int, total: int) -> InlineKeyboardMarkup:
    per_page = 15
    last_page = max(0, (total - 1) // per_page)
    page = max(0, min(page, last_page))
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton("⬅️ قبلی", callback_data=f"ev:v|{event_id}|{page - 1}")
        )
    if page < last_page:
        navigation.append(
            InlineKeyboardButton("بعدی ➡️", callback_data=f"ev:v|{event_id}|{page + 1}")
        )
    rows = [navigation] if navigation else []
    rows.append([InlineKeyboardButton("🔙 بازگشت به برنامه", callback_data=f"ev:b|{event_id}")])
    return InlineKeyboardMarkup(rows)


def build_event_attendees_text(event: dict, attendees: list[dict], page: int = 0) -> str:
    per_page = 15
    total = len(attendees)
    last_page = max(0, (total - 1) // per_page)
    page = max(0, min(page, last_page))
    start = page * per_page
    selected = attendees[start:start + per_page]
    title = html.escape(str(event.get("title") or "برنامه"))
    lines = [
        f"👥 <b>حاضرین برنامه: {title}</b>",
        f"تعداد کل: <b>{total}</b>",
        f"صفحه {page + 1} از {last_page + 1}",
        "",
    ]
    if not selected:
        lines.append("هنوز کسی حضور خودش رو ثبت نکرده.")
    else:
        for index, attendee in enumerate(selected, start + 1):
            label = _event_display_name(
                attendee.get("first_name"),
                attendee.get("last_name"),
                attendee.get("username"),
            )
            platform = attendee.get("platform")
            own_platform = event.get("platform")
            tag = (
                f" — {PLATFORM_TAG.get(platform, platform)}"
                if platform and own_platform and platform != own_platform
                else ""
            )
            lines.append(f"{index}. {html.escape(label)}{tag}")
    return "\n".join(lines)


def events_list_keyboard(events: list[dict]) -> InlineKeyboardMarkup:
    rows, row = [], []
    for ev in events:
        title = str(ev.get("title") or "برنامه")
        if len(title) > 40:
            title = title[:39] + "…"
        row.append(InlineKeyboardButton(title, callback_data=f"ev:s|{ev['event_id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def show_events(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message or update.effective_message
    if message is None:
        return
    events = get_events_for_chat(update.effective_chat.id, _platform)
    if not events:
        await message.chat.send_message("هنوز برنامه‌ای در این چت ساخته نشده. ادمین با /event می‌سازد.")
        return
    sent = await message.chat.send_message(
        "یه برنامه رو انتخاب کن 📅", reply_markup=events_list_keyboard(events)
    )
    if update.effective_chat.type in ("group", "supergroup"):
        asyncio.create_task(
            _cleanup_group_messages(
                context, update.effective_chat.id, [message.message_id, sent.message_id]
            )
        )


async def _can_manage_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user, chat = update.effective_user, update.effective_chat
    if user is None or chat is None:
        return False
    if OWNER_ID is not None and user.id == OWNER_ID:
        return True
    if chat.type in ("group", "supergroup"):
        try:
            member = await context.bot.get_chat_member(chat.id, user.id)
            return member.status in ("administrator", "creator")
        except Exception:
            return False
    return False


def event_draft_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأیید و انتشار", callback_data="ec:p")],
        [InlineKeyboardButton("✏️ ویرایش عنوان", callback_data="ec:e"),
         InlineKeyboardButton("لغو", callback_data="ec:x")],
    ])


async def _show_event_draft(update: Update, context: ContextTypes.DEFAULT_TYPE, title: str) -> None:
    message = update.message or update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return
    context.user_data["event_draft"] = {
        "title": title.strip(),
        "chat_id": chat.id,
        "user_id": user.id,
        "platform": _platform,
    }
    preview = (
        "📋 <b>پیش‌نمایش ایونت</b>\n\n"
        f"عنوان: <b>{html.escape(title.strip())}</b>\n\n"
        "این ایونت هنوز منتشر نشده است. اطلاعات را بررسی کن؛ "
        "برای انتشار باید دکمه تأیید را بزنی."
    )
    await message.chat.send_message(
        _fmt(preview), reply_markup=event_draft_keyboard(), **_html_kwargs()
    )


async def event_draft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    draft = context.user_data.get("event_draft")
    if not draft:
        await query.answer("پیش‌نویس پیدا نشد؛ دوباره ایونت بساز.", show_alert=True)
        return
    if (
        draft.get("chat_id") != query.message.chat.id
        or draft.get("user_id") != query.from_user.id
        or draft.get("platform") != _platform
    ):
        await query.answer("این پیش‌نویس متعلق به شما یا این گروه نیست.", show_alert=True)
        return
    if not await _can_manage_event(update, context):
        await query.answer("فقط ادمین می‌تواند ایونت را منتشر کند.", show_alert=True)
        return

    action = query.data.split(":", 1)[1]
    if action == "e":
        context.user_data["awaiting_event_title"] = True
        context.user_data["awaiting_event_title_chat_id"] = query.message.chat.id
        context.user_data.pop("event_draft", None)
        await query.answer()
        await query.edit_message_text("✏️ عنوان جدید ایونت را در یک پیام بفرست (۳ تا ۸۰ نویسه):")
        return
    if action == "x":
        context.user_data.pop("event_draft", None)
        await query.answer("پیش‌نویس لغو شد.")
        await query.edit_message_text("ایجاد ایونت لغو شد؛ چیزی در گروه منتشر نشد.")
        return
    if action != "p":
        await query.answer("عملیات نامعتبر است.", show_alert=True)
        return

    title = draft["title"]
    context.user_data.pop("event_draft", None)
    await query.answer("در حال انتشار…")
    await query.edit_message_text("✅ تأیید شد؛ ایونت در حال انتشار است.")
    await _do_create_event(update, context, title)


async def _do_create_event(update: Update, context: ContextTypes.DEFAULT_TYPE, title: str) -> None:
    message = update.message or update.effective_message
    chat_id = update.effective_chat.id
    context.user_data.pop("awaiting_event_title", None)
    context.user_data.pop("awaiting_event_title_chat_id", None)
    event_id = create_event(title, chat_id, update.effective_user.id, _platform)
    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=_fmt(build_event_text(get_event(event_id), [])),
        reply_markup=event_keyboard(event_id, 0),
        **_html_kwargs(),
    )
    set_event_message(event_id, chat_id, sent.message_id)
    group = update.effective_chat.type in ("group", "supergroup")
    if message is not None and group:
        context.user_data["event_created_msg_id"] = sent.message_id
    if group:
        trigger_id = message.message_id if message is not None else None
        if trigger_id is not None:
            _remember_flow_message(context, chat_id, trigger_id, key="event_flow_msgs")
        entries = context.user_data.pop("event_flow_msgs", [])
        if entries:
            asyncio.create_task(_delete_flow_messages(context, entries, delay=3))


EVENT_CREATE_KEYWORDS = (
    "ساخت ایونت",
    "ساخت برنامه",
    "ایونت جدید",
    "برنامه جدید",
    "ساخت رویداد",
)


def _extract_event_title(text: str) -> str | None:
    for kw in EVENT_CREATE_KEYWORDS:
        if text == kw:
            return ""
        if text.startswith(kw):
            rest = text[len(kw):].strip(" :‏-—")
            return rest
    return None


async def event_create_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, title: str) -> None:
    message = update.message or update.effective_message
    if message is None:
        return
    if not await _can_manage_event(update, context):
        await message.reply_text("فقط ادمین می‌تونه برنامه بسازه.")
        return
    title = (title or "").strip()
    if title and (len(title) < 3 or len(title) > 80 or "\n" in title or title.startswith("/")):
        await message.reply_text("عنوان باید یک خط و بین ۳ تا ۸۰ نویسه باشد.")
        return
    if not title:
        context.user_data["awaiting_event_title"] = True
        context.user_data["awaiting_event_title_chat_id"] = message.chat_id
        if update.effective_chat.type in ("group", "supergroup"):
            _remember_flow_message(context, message.chat_id, message.message_id, key="event_flow_msgs")
        prompt = await message.chat.send_message(
            "📝 عنوان برنامه رو بنویس:\nمثال: پرواز جمعه — ویس قرنی"
        )
        _remember_flow_message(context, prompt.chat_id, prompt.message_id, key="event_flow_msgs")
        return
    await _show_event_draft(update, context, title)


async def event_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    title = " ".join(context.args or []).strip()
    await event_create_flow(update, context, title)


async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message or update.effective_message
    if message is None:
        return
    if not await _can_manage_event(update, context):
        await message.reply_text("فقط ادمین می‌تونه برنامه‌ها رو لینک کنه.")
        return
    arg = (context.args or [""])[0].strip().lstrip("#")
    if not arg.isdigit():
        await message.reply_text(
            "برای وصل‌کردن برنامه‌ی همین چت به برنامه‌ی پیام‌رسان دیگر بنویس:\n"
            "/link <شناسهٔ برنامه>\n"
            "شناسه در انتهای پیامِ برنامه نمایش داده می‌شه."
        )
        return
    current = get_latest_event_for_chat(update.effective_chat.id, _platform)
    if current is None:
        await message.reply_text("اول با /event یک برنامه در این چت بساز.")
        return
    target = get_event(int(arg))
    if target is None:
        await message.reply_text("برنامه‌ای با این شناسه پیدا نشد.")
        return
    if not link_events(current["event_id"], target["event_id"]):
        await message.reply_text("لینک نشد.")
        return
    await message.reply_text(
        f"✅ برنامه‌ی «{current['title']}» با «{target['title']}» لینک شد. "
        "از این به بعد حضور هر دو طرف در هر دو نمایش داده می‌شه."
    )


async def process_event_refreshes(context: ContextTypes.DEFAULT_TYPE) -> None:
    for event in get_pending_refreshes(_platform):
        attendees = get_attendees(event["event_id"])
        try:
            await context.bot.edit_message_text(
                chat_id=event["chat_id"],
                message_id=event["message_id"],
                text=_fmt(build_event_text(event, attendees)),
                reply_markup=(
                    event_keyboard(event["event_id"], len(attendees))
                    if event.get("active", 1)
                    else None
                ),
                **_html_kwargs(),
            )
        except Exception:
            # Keep the item queued so transient Telegram/API failures can be retried.
            logger.warning(
                "Refresh failed for event %s; it will be retried",
                event["event_id"],
                exc_info=True,
            )
        else:
            clear_refresh(event["event_id"])


async def event_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    kind, _, arg = query.data[len("ev:"):].partition("|")

    if query.message is None:
        await query.answer("این دکمه دیگر معتبر نیست.", show_alert=True)
        return

    if kind == "l":
        await query.answer()
        events = get_events_for_chat(query.message.chat.id, _platform)
        if not events:
            await query.edit_message_text("هنوز برنامه‌ای در این چت ساخته نشده. ادمین با /event می‌سازد.")
            return
        await query.edit_message_text(
            "یه برنامه رو انتخاب کن 📅", reply_markup=events_list_keyboard(events)
        )
        return

    parts = arg.split("|") if arg else []
    try:
        event_id = int(parts[0])
        page = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        await query.answer("شناسه برنامه معتبر نیست.", show_alert=True)
        return

    event = get_event(event_id)
    if event is None:
        await query.answer("این برنامه حذف شده یا دیگر در دسترس نیست.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            logger.debug("Could not clear stale event keyboard", exc_info=True)
        return
    if event.get("chat_id") != query.message.chat.id or event.get("platform") != _platform:
        await query.answer("این دکمه متعلق به این گروه نیست.", show_alert=True)
        return
    if not event.get("active", 1):
        await query.answer("این برنامه بسته شده.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            logger.debug("Could not clear closed event keyboard", exc_info=True)
        return

    if kind == "s":
        await query.answer()
        attendees = get_attendees(event_id)
        await query.edit_message_text(
            _fmt(build_event_text(event, attendees)),
            reply_markup=event_keyboard(event_id, len(attendees)),
            **_html_kwargs(),
        )
        return

    if kind == "v":
        attendees = get_attendees(event_id)
        page_size = 15
        page = max(0, min(page, max(0, (len(attendees) - 1) // page_size)))
        await query.answer()
        await query.edit_message_text(
            _fmt(build_event_attendees_text(event, attendees, page)),
            reply_markup=event_attendees_keyboard(event_id, page, len(attendees)),
            **_html_kwargs(),
        )
        return

    if kind == "b":
        await query.answer()
        attendees = get_attendees(event_id)
        await query.edit_message_text(
            _fmt(build_event_text(event, attendees)),
            reply_markup=event_keyboard(event_id, len(attendees)),
            **_html_kwargs(),
        )
        return

    if kind == "a" and get_profile(query.from_user.id, _platform) is None:
        await query.answer("برای ثبت حضور نامت رو بنویس", show_alert=True)
        if context.user_data.get("awaiting_name"):
            return
        group_id = query.message.chat.id if query.message else None
        if group_id is None:
            return
        context.user_data["awaiting_name"] = True
        context.user_data["pending_action"] = {"type": "attend", "event_id": event_id}
        context.user_data.pop("name_flow_msgs", None)
        name = (getattr(query.from_user, "first_name", None) or "").strip() or "دوست عزیز"
        handle = f"@{query.from_user.username}" if getattr(query.from_user, "username", None) else ""
        text = f"👤 {name} {handle}\n\nنام و نام خانوادگیت رو دقیق بنویس:\nمثال: علی رضایی"
        try:
            sent = await context.bot.send_message(
                chat_id=group_id,
                text=text,
                reply_to_message_id=query.message.message_id if query.message else None,
            )
            _remember_flow_message(context, sent.chat_id, sent.message_id)
        except Exception:
            logger.warning("Could not send name prompt to chat %s", group_id)
        return

    if kind == "n":
        if get_profile(query.from_user.id, _platform) is not None:
            await query.answer("نامت قبلاً ثبت شده ✅ روی «حضور» بزن.", show_alert=True)
            return
        await query.answer()
        await _deliver_name_prompt(
            context,
            query.from_user,
            query.message.chat.id if query.message else query.from_user.id,
            query.message.message_id if query.message else None,
            {"type": "attend", "event_id": event_id},
            remember_trigger=False,
            prefer_dm=False,
        )
        return

    if kind == "a":
        is_new = record_attendance(event_id, query.from_user, platform=_platform)
        await query.answer("حضور شما ثبت شد ✅" if is_new else "شما قبلاً ثبت شده‌اید ✅")
    elif kind == "c":
        removed = remove_attendance(event_id, query.from_user.id, platform=_platform)
        await query.answer("حضور شما لغو شد." if removed else "شما در لیست نبودید.")
    elif kind == "v":
        await query.answer()
    else:
        await query.answer()
        return

    attendees = get_attendees(event_id)
    try:
        await query.edit_message_text(
            _fmt(build_event_text(event, attendees)),
            reply_markup=event_keyboard(event_id, len(attendees)),
            **_html_kwargs(),
        )
    except Exception:
        logger.debug("Could not refresh event message for event %s", event_id, exc_info=True)


async def handle_site_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    owner_id_str, _, data = query.data.partition(":")
    logger.info("SITE_CB user=%s data=%s", query.from_user.id, query.data)

    if str(query.from_user.id) != owner_id_str:
        await query.answer("این دکمه‌ها فقط برای کسیه که سایت رو باز کرده. خودت بنویس «سایت» 🙂", show_alert=True)
        return

    try:
        await query.answer()
    except Exception:
        logger.debug("answerCallbackQuery failed", exc_info=True)
    owner_id = query.from_user.id
    requested_by = _display_name(query.from_user) if query.message.chat.type in ("group", "supergroup") else None
    parts = data.split("|")
    kind = parts[0]

    if kind == "b" and parts[1] == "sites":
        await query.edit_message_text("یه سایت رو انتخاب کن 🪂", reply_markup=sites_keyboard(owner_id))
        return

    if kind == "mv":
        # owner:mv|site_idx|mode|...
        site_idx = int(parts[1])
        m = parts[2]  # now/h/d
        name, lat, lon, _ = SITES[site_idx]
        requested_by = _display_name(query.from_user) if query.message.chat.type in ("group", "supergroup") else None
        if m == "now":
            if len(parts) >= 4 and parts[3] == "menu":
                await query.edit_message_text(f"📍 {name}\nالان (ساعت {datetime.now(TEHRAN_TZ).hour:02d}:00)\n\nنوع نمایش رو انتخاب کن:", reply_markup=model_view_keyboard(owner_id, site_idx, "now"))
                return
            view = parts[3] if len(parts) > 3 else "all"
            if view == "pick":
                await query.edit_message_text(f"📍 {name} — الان\nکدوم مدل رو می‌خوای ببینی؟", reply_markup=pick_model_keyboard(owner_id, site_idx, "now"))
                return
            await query.edit_message_text("⏳ در حال دریافت اطلاعات...")
            now = datetime.now(TEHRAN_TZ)
            target_time = f"{now.date().isoformat()}T{now.hour:02d}:00"
            try:
                models = await get_multi_model_wind(lat, lon, target_time)
                updated_at = datetime.now(TEHRAN_TZ)
            except Exception:
                logger.exception("Failed to fetch current multi-model wind for %s", name)
                await query.edit_message_text("خطا در دریافت اطلاعات هواشناسی.", reply_markup=sites_keyboard(owner_id))
                return
            from core.weather import format_models_message
            message = format_models_message(name, f"الان (ساعت {now.hour:02d}:00)", models, requested_by, updated_at)
            await query.edit_message_text(message, reply_markup=model_view_keyboard(owner_id, site_idx, "now"))
            return
        if m == "h":
            # mv|site_idx|h|day|hour|view
            day = parts[3]
            hour = int(parts[4])
            if len(parts) >= 6 and parts[5] == "menu":
                await query.edit_message_text(f"📍 {name} — {DAY_LABELS[day]} ساعت {hour:02d}:00\n\nنوع نمایش رو انتخاب کن:", reply_markup=model_view_keyboard(owner_id, site_idx, "hour", day=day, hour=hour))
                return
            view = parts[5] if len(parts) > 5 else "all"
            if view == "pick":
                await query.edit_message_text(f"📍 {name} — {DAY_LABELS[day]} ساعت {hour:02d}:00\nکدوم مدل رو می‌خوای ببینی؟", reply_markup=pick_model_keyboard(owner_id, site_idx, "hour", day=day, hour=hour))
                return
            target_date = datetime.now(TEHRAN_TZ).date() + timedelta(days=DAY_OFFSETS[day])
            target_time = f"{target_date.isoformat()}T{hour:02d}:00"
            await query.edit_message_text("⏳ در حال دریافت پیش‌بینی...")
            try:
                models = await get_multi_model_wind(lat, lon, target_time)
                updated_at = datetime.now(TEHRAN_TZ)
            except Exception:
                logger.exception("Failed to fetch multi-model wind for %s at %s", name, target_time)
                await query.edit_message_text("خطا در دریافت پیش‌بینی هواشناسی.", reply_markup=sites_keyboard(owner_id))
                return
            from core.weather import format_models_message
            label = f"{name} — {DAY_LABELS[day]}"
            message = format_models_message(label, f"ساعت {hour:02d}:00", models, requested_by, updated_at)
            await query.edit_message_text(message, reply_markup=model_view_keyboard(owner_id, site_idx, "hour", day=day, hour=hour))
            return
        if m == "d":
            day = parts[3]
            if len(parts) >= 5 and parts[4] == "menu":
                await query.edit_message_text(f"📍 {name} — {DAY_LABELS[day]}\nنوع نمایش رو انتخاب کن:", reply_markup=model_view_keyboard(owner_id, site_idx, "day", day=day))
                return
            view = parts[4] if len(parts) > 4 else "all"
            if view == "pick":
                await query.edit_message_text(f"📍 {name} — {DAY_LABELS[day]}\nکدوم مدل رو می‌خوای ببینی؟", reply_markup=pick_model_keyboard(owner_id, site_idx, "day", day=day))
                return
            target_date = datetime.now(TEHRAN_TZ).date() + timedelta(days=DAY_OFFSETS[day])
            await query.edit_message_text("⏳ در حال دریافت نمای کل روز...")
            try:
                records_by_hour = await get_multi_model_daily(lat, lon, target_date.isoformat())
                updated_at = datetime.now(TEHRAN_TZ)
            except Exception:
                logger.exception("Failed to fetch daily multi-model timeline for %s at %s", name, target_date)
                await query.edit_message_text("خطا در دریافت پیش‌بینی هواشناسی.", reply_markup=sites_keyboard(owner_id))
                return
            from core.weather import format_models_daily
            if view == "all":
                message = format_models_daily(name, DAY_LABELS[day], records_by_hour, requested_by, updated_at)
                parts_msg = split_report_text(message)
                await query.edit_message_text(parts_msg[0], reply_markup=model_view_keyboard(owner_id, site_idx, "day", day=day))
                chat_id = query.message.chat.id
                for part in parts_msg[1:]:
                    await context.bot.send_message(chat_id=chat_id, text=part)
                return
            message = format_models_daily(name, DAY_LABELS[day], records_by_hour, requested_by, updated_at)
            parts_msg = split_report_text(message)
            await query.edit_message_text(parts_msg[0], reply_markup=model_view_keyboard(owner_id, site_idx, "day", day=day))
            chat_id = query.message.chat.id
            for part in parts_msg[1:]:
                await context.bot.send_message(chat_id=chat_id, text=part)
            return
        return

    if kind == "pm":
        # pm|site_idx|mode|day|hour|model_key
        site_idx = int(parts[1])
        m = parts[2]
        if m == "day":
            m = "d"
        elif m == "hour":
            m = "h"
        day = parts[3] if len(parts) > 3 and parts[3] != '' else None
        hour_s = parts[4] if len(parts) > 4 and parts[4] != '' else None
        model_key = parts[5] if len(parts) > 5 else None
        from core.weather import MODELS
        name, lat, lon, _ = SITES[site_idx]
        requested_by = _display_name(query.from_user) if query.message.chat.type in ("group", "supergroup") else None
        info = MODELS.get(model_key, {}) if model_key else {}
        mname = info.get("name", model_key)
        await query.edit_message_text(f"⏳ در حال دریافت {mname}...")
        logger.info("PM_FETCH site=%s mode=%s model=%s target=%s", site_idx, m, mname, locals().get("target_time", "n/a"))
        if m == "now":
            now = datetime.now(TEHRAN_TZ)
            target_time = f"{now.date().isoformat()}T{now.hour:02d}:00"
            try:
                models = await get_multi_model_wind(lat, lon, target_time)
                updated_at = datetime.now(TEHRAN_TZ)
                logger.info("PM_FETCH_DONE site=%s count=%d", site_idx, len(models))
            except Exception:
                logger.exception("PM_FETCH_FAIL site=%s", site_idx)
                await query.edit_message_text("خطا در دریافت اطلاعات هواشناسی.", reply_markup=sites_keyboard(owner_id))
                return
            # filter
            sel = [r for r in models if r.get("model") == mname]
            if not sel:
                await query.edit_message_text("داده‌ای برای این مدل یافت نشد.", reply_markup=model_view_keyboard(owner_id, site_idx, "now"))
                return
            from core.weather import format_models_message
            message = format_models_message(name, f"الان (ساعت {now.hour:02d}:00) — {info.get('theme','')} {info.get('flag','')} {mname}", sel, requested_by, updated_at)
            await query.edit_message_text(message, reply_markup=model_view_keyboard(owner_id, site_idx, "now"))
            return
        if m == "h":
            hour = int(hour_s) if hour_s else datetime.now(TEHRAN_TZ).hour
            target_date = datetime.now(TEHRAN_TZ).date() + timedelta(days=DAY_OFFSETS[day or 'today'])
            target_time = f"{target_date.isoformat()}T{hour:02d}:00"
            try:
                models = await get_multi_model_wind(lat, lon, target_time)
                updated_at = datetime.now(TEHRAN_TZ)
            except Exception:
                await query.edit_message_text("خطا در دریافت پیش‌بینی هواشناسی.", reply_markup=sites_keyboard(owner_id))
                return
            sel = [r for r in models if r.get("model") == mname]
            if not sel:
                await query.edit_message_text("داده‌ای برای این مدل یافت نشد.", reply_markup=model_view_keyboard(owner_id, site_idx, "hour", day=day, hour=hour))
                return
            from core.weather import format_models_message
            label = f"{name} — {DAY_LABELS[day] if day else ''}"
            message = format_models_message(label, f"ساعت {hour:02d}:00 — {info.get('theme','')} {info.get('flag','')} {mname}", sel, requested_by, updated_at)
            await query.edit_message_text(message, reply_markup=model_view_keyboard(owner_id, site_idx, "hour", day=day, hour=hour))
            return
        if m == "d":
            target_date = datetime.now(TEHRAN_TZ).date() + timedelta(days=DAY_OFFSETS[day or 'today'])
            try:
                records_by_hour = await get_multi_model_daily(lat, lon, target_date.isoformat())
                updated_at = datetime.now(TEHRAN_TZ)
            except Exception:
                await query.edit_message_text("خطا در دریافت پیش‌بینی هواشناسی.", reply_markup=sites_keyboard(owner_id))
                return
            # build filtered
            filt = {}
            for h, models in records_by_hour.items():
                sel = [r for r in models if r.get("model") == mname]
                if sel:
                    filt[h] = sel
            from core.weather import format_models_daily
            message = format_models_daily(f"{name} — {info.get('theme','')} {info.get('flag','')} {mname}", DAY_LABELS[day or 'today'], filt, requested_by, updated_at)
            parts_msg = split_report_text(message)
            await query.edit_message_text(parts_msg[0], reply_markup=model_view_keyboard(owner_id, site_idx, "day", day=day))
            chat_id = query.message.chat.id
            for part in parts_msg[1:]:
                await context.bot.send_message(chat_id=chat_id, text=part)
            return
        return

    if kind in ("s", "t", "h", "d"):
        site_idx = int(parts[1])
        record_site_view(query.from_user.id, SITES[site_idx][0])

    if kind == "s":
        site_idx = int(parts[1])
        name, _, _, _ = SITES[site_idx]
        await query.edit_message_text(
            f"📍 {name}\nبرای چه زمانی می‌خوای؟",
            reply_markup=time_choice_keyboard(owner_id, site_idx),
        )
        return

    if kind == "t":
        site_idx, choice = int(parts[1]), parts[2]
        name, lat, lon, _ = SITES[site_idx]

        if choice == "now":
            await query.edit_message_text(
                f"📍 {name}\nالان (ساعت {datetime.now(TEHRAN_TZ).hour:02d}:00)\n\nنوع نمایش رو انتخاب کن:",
                reply_markup=model_view_keyboard(owner_id, site_idx, "now"),
            )
            return

        await query.edit_message_text(
            f"📍 {name} — {DAY_LABELS[choice]}\nنوع نمایش رو انتخاب کن:",
            reply_markup=model_view_keyboard(owner_id, site_idx, "day", day=choice),
        )
        return

    if kind == "h":
        site_idx, day, hour = int(parts[1]), parts[2], int(parts[3])
        name, lat, lon, _ = SITES[site_idx]
        target_date = datetime.now(TEHRAN_TZ).date() + timedelta(days=DAY_OFFSETS[day])
        target_time = f"{target_date.isoformat()}T{hour:02d}:00"

        await query.edit_message_text("⏳ در حال دریافت پیش‌بینی...")
        try:
            models = await get_multi_model_wind(lat, lon, target_time)
            updated_at = datetime.now(TEHRAN_TZ)
        except Exception:
            logger.exception("Failed to fetch multi-model wind for %s at %s", name, target_time)
            await query.edit_message_text("خطا در دریافت پیش‌بینی هواشناسی.", reply_markup=sites_keyboard(owner_id))
            return
        from core.weather import format_models_message
        label = f"{name} — {DAY_LABELS[day]}"
        message = format_models_message(label, f"ساعت {hour:02d}:00", models, requested_by, updated_at)
        await query.edit_message_text(message, reply_markup=model_view_keyboard(owner_id, site_idx, "hour", day=day, hour=hour))
        return

    if kind == "d":
        site_idx, day = int(parts[1]), parts[2]
        name, lat, lon, _ = SITES[site_idx]
        target_date = datetime.now(TEHRAN_TZ).date() + timedelta(days=DAY_OFFSETS[day])
        await query.edit_message_text("⏳ در حال دریافت نمای کل روز...")
        try:
            records_by_hour = await get_multi_model_daily(lat, lon, target_date.isoformat())
            updated_at = datetime.now(TEHRAN_TZ)
        except Exception:
            logger.exception("Failed to fetch daily multi-model timeline for %s at %s", name, target_date)
            await query.edit_message_text("خطا در دریافت پیش‌بینی هواشناسی.", reply_markup=sites_keyboard(owner_id))
            return
        from core.weather import format_models_daily
        message = format_models_daily(name, DAY_LABELS[day], records_by_hour, requested_by, updated_at)
        parts_msg = split_report_text(message)
        await query.edit_message_text(parts_msg[0], reply_markup=model_view_keyboard(owner_id, site_idx, "day", day=day))
        chat_id = query.message.chat.id
        for part in parts_msg[1:]:
            await context.bot.send_message(chat_id=chat_id, text=part)
        return

def model_view_keyboard(owner_id: int, site_idx: int, mode: str, day: str | None = None, hour: int | None = None) -> InlineKeyboardMarkup:
    if mode == "now":
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📊 همه مدل‌ها", callback_data=f"{owner_id}:mv|{site_idx}|now|all"),
                    InlineKeyboardButton("🔍 انتخاب یک مدل", callback_data=f"{owner_id}:mv|{site_idx}|now|pick"),
                ],
                [InlineKeyboardButton("🔙 بازگشت به سایت‌ها", callback_data=f"{owner_id}:b|sites")],
            ]
        )
    if mode == "hour":
        base_day = day or "today"
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📊 همه مدل‌ها", callback_data=f"{owner_id}:mv|{site_idx}|h|{base_day}|{hour}|all"),
                    InlineKeyboardButton("🔍 انتخاب یک مدل", callback_data=f"{owner_id}:mv|{site_idx}|h|{base_day}|{hour}|pick"),
                ],
                [InlineKeyboardButton("🔙 تغییر ساعت", callback_data=f"{owner_id}:t|{site_idx}|{base_day}")],
            ]
        )
    if mode == "day":
        base_day = day or "today"
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📊 همه مدل‌ها", callback_data=f"{owner_id}:mv|{site_idx}|d|{base_day}|all"),
                    InlineKeyboardButton("🔍 انتخاب یک مدل", callback_data=f"{owner_id}:mv|{site_idx}|d|{base_day}|pick"),
                ],
                [InlineKeyboardButton("🔙 تغییر روز", callback_data=f"{owner_id}:s|{site_idx}")],
            ]
        )
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به سایت‌ها", callback_data=f"{owner_id}:b|sites")]])


def pick_model_keyboard(owner_id: int, site_idx: int, mode: str, day: str | None = None, hour: int | None = None) -> InlineKeyboardMarkup:
    from core.weather import MODELS
    rows = []
    row = []
    for idx_m, (key, info) in enumerate(MODELS.items()):
        btn = InlineKeyboardButton(f"{info['theme']} {info['flag']} {info['name']}", callback_data=f"{owner_id}:pm|{site_idx}|{mode}|{day or ''}|{hour or ''}|{key}")
        row.append(btn)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if mode == "now":
        rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"{owner_id}:mv|{site_idx}|now|menu")])
    elif mode == "hour":
        rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"{owner_id}:mv|{site_idx}|h|{day}|{hour}|menu")])
    elif mode == "day":
        rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"{owner_id}:mv|{site_idx}|d|{day}|menu")])
    rows.append([InlineKeyboardButton("🔙 سایت‌ها", callback_data=f"{owner_id}:b|sites")])
    return InlineKeyboardMarkup(rows)


def split_report_text(text: str, max_len: int = 3900) -> list[str]:
    if not text:
        return []
    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > max_len and current:
            parts.append(current.rstrip())
            current = ""
        while len(line) > max_len:
            if current:
                parts.append(current.rstrip())
                current = ""
            parts.append(line[:max_len].rstrip())
            line = line[max_len:]
        current += line
    if current:
        parts.append(current.rstrip())
    return parts


STALE_UPDATE_AFTER = timedelta(minutes=10)
_SEEN_UPDATE_IDS = set()
_SEEN_UPDATE_ORDER = deque(maxlen=5000)


def _is_duplicate_update(update: Update) -> bool:
    uid = getattr(update, "update_id", None)
    if uid is None:
        return False
    if uid in _SEEN_UPDATE_IDS:
        return True
    if len(_SEEN_UPDATE_ORDER) == _SEEN_UPDATE_ORDER.maxlen:
        oldest = _SEEN_UPDATE_ORDER[0]
        _SEEN_UPDATE_IDS.discard(oldest)
    _SEEN_UPDATE_ORDER.append(uid)
    _SEEN_UPDATE_IDS.add(uid)
    return False


async def drop_stale_updates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _is_duplicate_update(update):
        logger.debug("Ignoring duplicate update %s", getattr(update, "update_id", None))
        raise ApplicationHandlerStop

    message = update.message or update.edited_message
    if message is None:
        return
    msg_date = getattr(message, "date", None)
    if msg_date is None:
        return
    if msg_date.tzinfo is None:
        msg_date = msg_date.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - msg_date
    if age > STALE_UPDATE_AFTER:
        logger.warning("Ignoring stale update (age %s): %s", age, (getattr(message, "text", "") or "")[:40])
        raise ApplicationHandlerStop


async def track_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.my_chat_member:
        result = update.my_chat_member
        new_status = result.new_chat_member.status
        status = "member" if new_status in ("member", "administrator", "creator") else "left"
        record_group_membership(result.chat, result.from_user, status)
        return

    user, chat = update.effective_user, update.effective_chat
    if user and chat:
        record_interaction(user, chat)


ADMIN_PAGE_SIZE = 8


def _esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👥 گروه‌ها", callback_data="ad:groups"),
            ],
            [
                InlineKeyboardButton("📅 مدیریت ایونت‌ها", callback_data="ad:evgroups"),
            ],
            [
                InlineKeyboardButton("👤 حضور ثبت‌شده‌ها", callback_data="ad:users|0"),
                InlineKeyboardButton("🧑‍💼 ثبت‌نام نشده‌ها", callback_data="ad:unreg|0"),
            ],
            [
                InlineKeyboardButton("🌐 سایت‌های محبوب", callback_data="ad:sites|0"),
                InlineKeyboardButton("📋 گزارش کامل", callback_data="ad:report"),
            ],
            [
                InlineKeyboardButton("⚙️ تنظیمات", callback_data="ad:settings"),
                InlineKeyboardButton("🔄 تازه‌سازی", callback_data="ad:main"),
            ],
        ]
    )


def build_admin_home() -> str:
    stats = get_admin_stats()
    top = get_top_sites(1)
    top_line = f"🥇 {_esc(top[0][0])} — {top[0][1]} بازدید" if top else "هنوز بازدیدی ثبت نشده"
    return (
        "🛡️ <b>پنل مدیریت ربات هواشناسی کرمانشاه</b>\n\n"
        "📊 <b>آمار کلی:</b>\n"
        f"👤 کاربران خصوصی: <b>{stats['private_users']}</b>\n"
        f"👥 گروه‌های فعال: <b>{stats['active_groups']}</b>\n"
        f"🌐 بازدید کل سایت‌ها: <b>{stats['total_site_views']}</b>\n"
        f"🔁 کل تعاملات: <b>{stats['total_interactions']}</b>\n"
        f"{top_line}\n\n"
        "👇 یکی از بخش‌ها رو انتخاب کن:"
    )


def _pager_row(cb: str, page: int, total: int) -> list[InlineKeyboardButton]:
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◀️", callback_data=f"ad:{cb}|{page - 1}"))
    row.append(InlineKeyboardButton(f"📄 صفحه {page + 1}", callback_data="ad:noop"))
    if (page + 1) * ADMIN_PAGE_SIZE < total:
        row.append(InlineKeyboardButton("▶️", callback_data=f"ad:{cb}|{page + 1}"))
    return row


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if OWNER_ID is None or update.effective_user.id != OWNER_ID:
        return
    await update.message.reply_text(_fmt(build_admin_home()), reply_markup=admin_main_keyboard(), **_html_kwargs())



def configure_application(app: Application) -> None:
    """Register the Telegram-only handlers; shared by polling and webhook runtimes."""
    app.add_handler(TypeHandler(Update, drop_stale_updates), group=-2)
    app.add_handler(TypeHandler(Update, track_update), group=-1)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("sites", start_command))
    app.add_handler(CommandHandler("name", name_command))
    app.add_handler(CommandHandler("event", event_command))
    app.add_handler(CommandHandler("link", link_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^ad:"))
    app.add_handler(CallbackQueryHandler(name_confirm_callback, pattern=r"^nc:"))
    app.add_handler(CallbackQueryHandler(event_draft_callback, pattern=r"^ec:"))
    app.add_handler(CallbackQueryHandler(event_callback, pattern=r"^ev:"))
    app.add_handler(CallbackQueryHandler(manage_event_callback, pattern=r"^me:"))
    app.add_handler(CallbackQueryHandler(admin_manage_event_callback, pattern=r"^ma:"))
    app.add_handler(CallbackQueryHandler(handle_site_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)


def main() -> None:
    """Legacy polling entrypoint, retained for local development only."""
    global OWNER_ID
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN در فایل .env تنظیم نشده")
    owner_raw = os.getenv("BOT_OWNER_ID")
    OWNER_ID = int(owner_raw) if owner_raw else None
    app = Application.builder().token(token).build()
    configure_application(app)
    if app.job_queue is not None:
        trigger = CronTrigger(hour="*", minute=0, second=10, timezone=TEHRAN_TZ)
        app.job_queue.run_custom(hourly_report_job, job_kwargs={"trigger": trigger})
        app.job_queue.run_repeating(process_event_refreshes, interval=5, first=5)
    logger.info("Telegram bot starting (polling; local development only)...")
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


async def hourly_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(_platform)
    site_idx = settings["default_site_idx"]
    chat_id = settings["report_chat_id"]
    if site_idx is None or chat_id is None:
        return

    name, lat, lon, _ = SITES[site_idx]
    now = datetime.now(TEHRAN_TZ)
    date_str = now.date().isoformat()

    enabled_hours = set(get_report_hours())
    if now.hour not in enabled_hours:
        logger.info("Report hour %02d is disabled, skipping report for %s", now.hour, name)
        return
    remaining_hours = [h for h in sorted(enabled_hours) if h >= now.hour]
    if not remaining_hours:
        logger.info("No remaining report hours now, skipping report for %s", name)
        return

    try:
        records_by_hour = await get_multi_model_daily(lat, lon, date_str, hours=remaining_hours)
        updated_at = datetime.now(TEHRAN_TZ)
    except Exception:
        logger.exception("Hourly report fetch failed for %s", name)
        return

    day_label = f"امروز ({remaining_hours[0]:02d} تا {remaining_hours[-1]:02d})"
    message = format_dual_estimate_daily(name, day_label, records_by_hour, updated_at=updated_at)
    parts = split_report_text(message)

    sent_ids: list[int] = []
    try:
        for part in parts:
            sent = await context.bot.send_message(chat_id=chat_id, text=part)
            sent_ids.append(sent.message_id)
    except Exception:
        logger.exception("Failed to send hourly report to chat %s", chat_id)
        return

    prev_ids = settings["last_report_msg_id"] or []
    prev_chat = settings["last_report_chat_id"]
    if prev_ids and prev_chat is not None:
        for mid in prev_ids:
            try:
                await context.bot.delete_message(chat_id=prev_chat, message_id=mid)
            except Exception:
                logger.debug("Previous report message %s in %s already gone", mid, prev_chat)

    set_last_report_message(sent_ids, chat_id, _platform)
    logger.info("Hourly report sent for %s to chat %s (%d msg, deleted %d previous)", name, chat_id, len(sent_ids), len(prev_ids))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unexpected error while handling an update", exc_info=context.error)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if OWNER_ID is None or query.from_user.id != OWNER_ID:
        await query.answer("این پنل فقط برای ادمینه!", show_alert=True)
        return

    data = query.data[len("ad:"):]
    kind, _, arg = data.partition("|")

    if kind == "noop":
        await query.answer()
        return

    await query.answer()

    if kind == "main":
        await query.edit_message_text(
            _fmt(build_admin_home()), reply_markup=admin_main_keyboard(), **_html_kwargs()
        )
        return

    if kind == "groups":
        text, keyboard = build_groups_text()
        await query.edit_message_text(_fmt(text), reply_markup=keyboard, **_html_kwargs())
        return

    if kind == "evgroups":
        text, keyboard = build_admin_event_groups_page()
        await query.edit_message_text(_fmt(text), reply_markup=keyboard, **_html_kwargs())
        return

    if kind == "evgroup":
        group_id = int(arg) if arg.lstrip("-").isdigit() else None
        if group_id is None:
            await query.edit_message_text("شناسه گروه معتبر نیست.")
            return
        text, keyboard = build_admin_group_events_page(group_id)
        await query.edit_message_text(_fmt(text), reply_markup=keyboard, **_html_kwargs())
        return

    if kind == "evnew":
        group_id = int(arg) if arg.lstrip("-").isdigit() else None
        if group_id is None or group_id not in {int(row[0]) for row in _unique_active_group_rows()}:
            await query.edit_message_text("گروه معتبر نیست یا دیگر فعال نیست.")
            return
        context.user_data["admin_event_create_target"] = group_id
        context.user_data.pop("admin_event_draft", None)
        await query.message.reply_text("📝 عنوان ایونت جدید را بفرست (۳ تا ۸۰ نویسه):")
        return

    if kind == "evcancel":
        context.user_data.pop("admin_event_draft", None)
        context.user_data.pop("admin_event_create_target", None)
        await query.edit_message_text("ایجاد ایونت لغو شد؛ چیزی در گروه منتشر نشد.")
        return

    if kind == "evpublish":
        draft = context.user_data.get("admin_event_draft")
        if not draft or draft.get("user_id") != query.from_user.id or draft.get("platform") != _platform:
            await query.edit_message_text("پیش‌نویس معتبر نیست؛ دوباره ایونت بساز.")
            return
        target_chat_id = int(draft["chat_id"])
        try:
            target_chat = await context.bot.get_chat(target_chat_id)
            if target_chat.type not in ("group", "supergroup"):
                raise ValueError("target is not a group")
            event_id = create_event(draft["title"], target_chat_id, OWNER_ID, _platform)
            try:
                sent = await context.bot.send_message(chat_id=target_chat_id, text=_fmt(build_event_text(get_event(event_id), [])), reply_markup=event_keyboard(event_id, 0), **_html_kwargs())
            except Exception:
                delete_event_group(event_id)
                raise
            set_event_message(event_id, target_chat_id, sent.message_id)
        except Exception:
            logger.exception("Private admin event publish failed for group %s", target_chat_id)
            await query.edit_message_text("❌ انتشار انجام نشد. مطمئن شو ربات هنوز عضو گروه است و اجازه ارسال پیام دارد.")
            return
        context.user_data.pop("admin_event_draft", None)
        await query.edit_message_text(f"✅ ایونت در گروه «{_esc(target_chat.title or target_chat_id)}» منتشر شد.\n\n📅 {_esc(draft['title'])}\nشناسه ایونت: <code>#{event_id}</code>", **_html_kwargs())
        return

    if kind == "sites":
        page = int(arg) if arg.isdigit() else 0
        text, keyboard = build_sites_page(page)
        await query.edit_message_text(_fmt(text), reply_markup=keyboard, **_html_kwargs())
        return

    if kind == "report":
        parts = split_report_text(get_admin_report())
        await query.edit_message_text(
            parts[0],
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 خانه", callback_data="ad:main")]]
            ),
            **_html_kwargs(),
        )
        for part in parts[1:]:
            await query.message.chat.send_message(part, **_html_kwargs())
        return

    if kind == "settings":
        text, keyboard = build_settings_page()
        await query.edit_message_text(_fmt(text), reply_markup=keyboard, **_html_kwargs())
        return

    if kind == "ssite":
        text, keyboard = build_default_site_page()
        await query.edit_message_text(_fmt(text), reply_markup=keyboard, **_html_kwargs())
        return

    if kind == "dsite":
        site_idx = int(arg) if arg.isdigit() else None
        if site_idx is not None and 0 <= site_idx < len(SITES):
            set_default_site(site_idx)
        text, keyboard = build_settings_page()
        await query.edit_message_text(_fmt(text), reply_markup=keyboard, **_html_kwargs())
        return

    if kind == "srchat":
        set_report_chat(query.message.chat.id, _platform)
        text, keyboard = build_settings_page()
        await query.edit_message_text(_fmt(text), reply_markup=keyboard, **_html_kwargs())
        return

    if kind == "rchatclear":
        set_report_chat(None, _platform)
        text, keyboard = build_settings_page()
        await query.edit_message_text(_fmt(text), reply_markup=keyboard, **_html_kwargs())
        return

    if kind == "rhours":
        text, keyboard = build_hours_editor()
        await query.edit_message_text(_fmt(text), reply_markup=keyboard, **_html_kwargs())
        return

    if kind == "rht":
        hour = int(arg) if arg.isdigit() else None
        if hour is not None and 0 <= hour <= 23:
            enabled = set(get_report_hours())
            if hour in enabled:
                enabled.remove(hour)
            else:
                enabled.add(hour)
            set_report_hours(sorted(enabled))
        text, keyboard = build_hours_editor()
        await query.edit_message_text(_fmt(text), reply_markup=keyboard, **_html_kwargs())
        return

    if kind == "users":
        page = int(arg) if arg.isdigit() else 0
        text, keyboard = build_users_page(page)
        await query.edit_message_text(_fmt(text), reply_markup=keyboard, **_html_kwargs())
        return
    if kind == "unreg":
        page = int(arg) if arg.isdigit() else 0
        text, keyboard = build_unreg_page(page)
        await query.edit_message_text(_fmt(text), reply_markup=keyboard, **_html_kwargs())
        return
    if kind == "ureg":
        p2 = arg.split("|")
        uid = int(p2[0]) if p2 and p2[0].isdigit() else None
        pg = int(p2[1]) if len(p2) > 1 and p2[1].isdigit() else 0
        context.user_data["admin_edit"] = {"type": "ureg", "user_id": uid, "page": pg}
        await query.message.reply_text("✏️ نام و نام خانوادگی کامل رو بنویس برای این کاربر:")
        return
    if kind == "uedit":
        p2 = arg.split("|")
        uid = int(p2[0]) if p2 and p2[0].isdigit() else None
        plat = p2[1] if len(p2) > 1 else _platform
        pg = int(p2[2]) if len(p2) > 2 and p2[2].isdigit() else 0
        context.user_data["admin_edit"] = {"type": "user", "user_id": uid, "platform": plat, "page": pg}
        cur = get_profile(uid, plat) or ""
        await query.edit_message_text(
            _fmt(f"✏️ نام و نام خانوادگی جدید رو بنویس برای این کاربر:\n{_esc(cur) or '(خالی)'}"),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 حذف نام و خواستن ثبت‌نام دوباره", callback_data=f"ad:udel|{uid}|{plat}|{pg}")],[InlineKeyboardButton("🔙 بازگشت", callback_data=f"ad:users|{pg}")]]),
            **_html_kwargs(),
        )
        return
    if kind == "udel":
        p2 = arg.split("|")
        uid = int(p2[0]) if p2 and p2[0].isdigit() else None
        plat = p2[1] if len(p2) > 1 else _platform
        pg = int(p2[2]) if len(p2) > 2 and p2[2].isdigit() else 0
        if uid:
            delete_profile(uid, plat)
        text, keyboard = build_users_page(pg)
        await query.edit_message_text(_fmt(text), reply_markup=keyboard, **_html_kwargs())
        return


def admin_unreg_keyboard(users: list, page: int = 0) -> InlineKeyboardMarkup:
    per_page = 8
    start = page * per_page
    slice_ = users[start:start+per_page]
    buttons = []
    for u in slice_:
        name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x).strip() or u.get("username") or str(u.get("user_id"))
        buttons.append([InlineKeyboardButton(f"➕ {name[:32]}", callback_data=f"ad:ureg|{u['user_id']}|{page}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"ad:unreg|{page-1}"))
    if start + per_page < len(users):
        nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"ad:unreg|{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🏠 خانه", callback_data="ad:main")])
    return InlineKeyboardMarkup(buttons)


def build_unreg_page(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    # Keep the current group as the default, but allow configuration per deployment.
    raw_chat_id = os.getenv("UNREGISTERED_USERS_CHAT_ID", "-1002297819434").strip()
    try:
        chat_id = int(raw_chat_id)
    except ValueError:
        logger.error("UNREGISTERED_USERS_CHAT_ID must be an integer")
        return (
            "⚠️ شناسه گروه ثبت‌نام‌نشده‌ها در تنظیمات معتبر نیست.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 خانه", callback_data="ad:main")]]),
        )

    try:
        users = get_group_users_unregistered(chat_id, _platform)
    except Exception:
        logger.exception("Could not load unregistered users for chat %s", chat_id)
        return (
            "⚠️ دریافت فهرست کاربران ثبت‌نام‌نشده با خطا مواجه شد.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 خانه", callback_data="ad:main")]]),
        )

    if not users:
        return "👥 <b>ثبت‌نام نشده‌های گروه</b>\n\nهمهٔ اعضای فعال ثبت‌نام شده‌اند.", InlineKeyboardMarkup([[InlineKeyboardButton("🏠 خانه", callback_data="ad:main")]])
    return f"👥 <b>ثبت‌نام نشده‌های گروه</b> ({len(users)})\n\nبا کلیک روی هر نفر میشه اسمش رو ثبت کنی:", admin_unreg_keyboard(users, page)


def users_list_keyboard(users: list, page: int = 0) -> InlineKeyboardMarkup:
    per_page = 8
    start = page * per_page
    slice_ = users[start:start+per_page]
    buttons = []
    for u in slice_:
        label = u.get("full_name") or u.get("first_name") or u.get("username") or str(u.get("user_id"))
        label = label[:32]
        buttons.append([InlineKeyboardButton(f"👤 {label}", callback_data=f"ad:uedit|{u['user_id']}|{u.get('platform','telegram')}|{page}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"ad:users|{page-1}"))
    if start + per_page < len(users):
        nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"ad:users|{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🏠 خانه", callback_data="ad:main")])
    return InlineKeyboardMarkup(buttons)


def build_users_page(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    users = []
    try:
        users = get_attendance_users()
    except Exception:
        pass
    if not users:
        return "👤 <b>حضور ثبت‌شده‌ها</b>\n\nهیچ‌کس ثبت نشده.", InlineKeyboardMarkup([[InlineKeyboardButton("🏠 خانه", callback_data="ad:main")]])
    lines = [f"👤 <b>حضور ثبت‌شده‌ها</b> ({len(users)})", ""]
    return "\n".join(lines), users_list_keyboard(users, page)


def build_groups_text() -> tuple[str, InlineKeyboardMarkup]:
    groups = get_active_groups_data()
    if not groups:
        text = "👥 <b>گروه‌های فعال</b>\n\nربات هنوز به گروه فعالی اضافه نشده."
    else:
        lines = [f"👥 <b>گروه‌های فعال</b> ({len(groups)})", ""]
        for i, (chat_id, title, added_by, last_seen) in enumerate(groups[:30], 1):
            lines.append(
                f"{i}. <b>{_esc(title or chat_id)}</b>\n"
                f"   <code>{chat_id}</code> — ➕ {_esc(added_by or '-')}"
            )
            if last_seen:
                lines.append(f"   🕐 {_esc(format_time(last_seen))}")
        if len(groups) > 30:
            lines.append(f"… و {len(groups) - 30} گروه دیگر")
        text = "\n".join(lines)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 خانه", callback_data="ad:main")]])
    return text, keyboard


def build_sites_page(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    sites = get_top_sites(1000)
    total = len(sites)
    max_page = max(0, (total - 1) // ADMIN_PAGE_SIZE)
    page = min(max(page, 0), max_page)
    start = page * ADMIN_PAGE_SIZE
    slice_ = sites[start : start + ADMIN_PAGE_SIZE]

    if not sites:
        text = "🌐 <b>سایت‌های محبوب</b>\n\nهنوز بازدیدی از سایت‌ها ثبت نشده."
    else:
        lines = [f"🌐 <b>سایت‌های محبوب</b> ({total})", ""]
        for i, (name, views) in enumerate(slice_, start + 1):
            lines.append(f"{i}. <b>{_esc(name)}</b> — {views} بازدید")
        text = "\n".join(lines)

    rows = []
    if total > ADMIN_PAGE_SIZE:
        rows.append(_pager_row("sites", page, total))
    rows.append([InlineKeyboardButton("🏠 خانه", callback_data="ad:main")])
    return text, InlineKeyboardMarkup(rows)


def build_settings_page() -> tuple[str, InlineKeyboardMarkup]:
    settings = get_settings(_platform)
    site_idx = settings["default_site_idx"]
    site_name = SITES[site_idx][0] if site_idx is not None and 0 <= site_idx < len(SITES) else "تنظیم نشده"
    report_chat = settings["report_chat_id"]
    hours = get_report_hours()
    hours_label = "، ".join(f"{h:02d}" for h in hours) if hours else "هیچ ساعتی فعال نیست"

    text = (
        "⚙️ <b>تنظیمات ربات</b>\n\n"
        f"📍 سایت پیش‌فرض: <b>{_esc(site_name)}</b>\n"
        f"📥 مقصد گزارش: <code>{report_chat if report_chat is not None else 'تنظیم نشده'}</code>\n"
        f"🕐 ساعت‌های گزارش: {_esc(hours_label)}\n\n"
        "یکی از گزینه‌ها رو انتخاب کن:"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📍 انتخاب سایت پیش‌فرض", callback_data="ad:ssite")],
            [InlineKeyboardButton("📥 مقصد گزارش = همین چت", callback_data="ad:srchat")],
            [InlineKeyboardButton("🧹 حذف مقصد گزارش", callback_data="ad:rchatclear")],
            [InlineKeyboardButton("🕐 ساعت‌های گزارش", callback_data="ad:rhours")],
            [InlineKeyboardButton("🏠 خانه", callback_data="ad:main")],
        ]
    )
    return text, keyboard


def build_default_site_page() -> tuple[str, InlineKeyboardMarkup]:
    current = get_settings(_platform)["default_site_idx"]
    rows = []
    for i, site in enumerate(SITES):
        mark = "✅ " if current == i else ""
        rows.append([InlineKeyboardButton(f"{mark}{site[0]}", callback_data=f"ad:dsite|{i}")])
    rows.append([InlineKeyboardButton("🔙 تنظیمات", callback_data="ad:settings")])
    return "📍 <b>سایت پیش‌فرض را انتخاب کن:</b>", InlineKeyboardMarkup(rows)


def build_hours_editor() -> tuple[str, InlineKeyboardMarkup]:
    enabled = set(get_report_hours())
    rows, row = [], []
    for hour in range(24):
        label = f"{'✅' if hour in enabled else '▫️'} {hour:02d}"
        row.append(InlineKeyboardButton(label, callback_data=f"ad:rht|{hour}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 تنظیمات", callback_data="ad:settings")])
    text = (
        "🕐 <b>ساعت‌های ارسال گزارش</b>\n\n"
        "روی هر ساعت بزن تا فعال/غیرفعال شود.\n"
        f"فعال: {len(enabled)} ساعت"
    )
    return text, InlineKeyboardMarkup(rows)


def manage_attendees_keyboard(event_id: int, attendees: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    per_page = 8
    last_page = max(0, (len(attendees) - 1) // per_page)
    page = max(0, min(page, last_page))
    start = page * per_page
    slice_ = attendees[start : start + per_page]
    buttons = []
    for a in slice_:
        label = _event_display_name(a["first_name"], a["last_name"], a["username"])
        platform = a.get("platform") or _platform
        buttons.append(
            [
                InlineKeyboardButton(label[:35], callback_data=f"me:noop|0"),
                InlineKeyboardButton("✏️ نام", callback_data=f"me:an|{event_id}|{a['user_id']}|{page}|{platform}"),
                InlineKeyboardButton("❌ حذف", callback_data=f"me:ar|{event_id}|{a['user_id']}|{page}|{platform}"),
            ]
        )
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"me:att|{event_id}|{page-1}"))
    if start + per_page < len(attendees):
        nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"me:att|{event_id}|{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"me:view|{event_id}")])
    return InlineKeyboardMarkup(buttons)


def admin_manage_events_keyboard(events: list[dict], group_id: int) -> InlineKeyboardMarkup:
    rows = []
    for event in events:
        title = str(event.get("title") or "برنامه")[:36]
        status = "" if event.get("active", 1) else " [بسته]"
        rows.append([InlineKeyboardButton(
            f"#{event['event_id']} {title}{status}",
            callback_data=f"ma:{group_id}|view|{event['event_id']}",
        )])
    rows.append([InlineKeyboardButton("🔙 انتخاب گروه", callback_data="ad:evgroups")])
    return InlineKeyboardMarkup(rows)


def admin_manage_event_keyboard(event_id: int, group_id: int, active: int = 1) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("👥 مدیریت حاضرین", callback_data=f"ma:{group_id}|att|{event_id}|0")],
        [InlineKeyboardButton("✏️ ویرایش عنوان", callback_data=f"ma:{group_id}|ren|{event_id}")],
    ]
    if active:
        rows.append([InlineKeyboardButton("🔒 بستن برنامه", callback_data=f"ma:{group_id}|close|{event_id}")])
    else:
        rows.append([InlineKeyboardButton("🔓 باز کردن برنامه", callback_data=f"ma:{group_id}|open|{event_id}")])
    rows.extend([
        [InlineKeyboardButton("🗑 حذف برنامه", callback_data=f"ma:{group_id}|del|{event_id}")],
        [InlineKeyboardButton("🔙 فهرست ایونت‌های گروه", callback_data=f"ad:evgroup|{group_id}")],
    ])
    return InlineKeyboardMarkup(rows)


def admin_manage_attendees_keyboard(
    event_id: int, group_id: int, attendees: list[dict], page: int = 0
) -> InlineKeyboardMarkup:
    per_page = 8
    last_page = max(0, (len(attendees) - 1) // per_page)
    page = max(0, min(page, last_page))
    start = page * per_page
    rows = []
    for attendee in attendees[start:start + per_page]:
        label = _event_display_name(
            attendee.get("first_name"), attendee.get("last_name"), attendee.get("username")
        )
        platform = attendee.get("platform") or _platform
        rows.append([
            InlineKeyboardButton(label[:28], callback_data="ma:noop"),
            InlineKeyboardButton("✏️ نام", callback_data=f"ma:{group_id}|an|{event_id}|{attendee['user_id']}|{page}|{platform}"),
            InlineKeyboardButton("❌ حذف", callback_data=f"ma:{group_id}|ar|{event_id}|{attendee['user_id']}|{page}|{platform}"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"ma:{group_id}|att|{event_id}|{page-1}"))
    if start + per_page < len(attendees):
        nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"ma:{group_id}|att|{event_id}|{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"ma:{group_id}|view|{event_id}")])
    return InlineKeyboardMarkup(rows)


def _unique_active_group_rows() -> list[tuple]:
    """Collapse same-title records in the admin picker; keep the most recently seen group."""
    unique = {}
    for row in get_active_groups_data():
        chat_id, title, added_by, last_seen = row
        key = " ".join(str(title or chat_id).casefold().split())
        if key not in unique:
            unique[key] = row
    return list(unique.values())


def build_admin_event_groups_page() -> tuple[str, InlineKeyboardMarkup]:
    all_groups = get_active_groups_data()
    groups = _unique_active_group_rows()
    if not groups:
        return ("📅 <b>مدیریت ایونت‌ها</b>\n\nگروه فعالی برای انتخاب وجود ندارد.", InlineKeyboardMarkup([[InlineKeyboardButton("🏠 خانه", callback_data="ad:main")]]))
    rows = []
    lines = [f"📅 <b>انتخاب گروه برای مدیریت ایونت‌ها</b> ({len(groups)})", ""]
    if len(all_groups) > len(groups):
        lines.append(f"ℹ️ {len(all_groups) - len(groups)} رکورد هم‌نام تکراری در فهرست ادغام نمایشی شده است.")
        lines.append("")
    for chat_id, title, added_by, last_seen in groups:
        label = str(title or chat_id)
        rows.append([InlineKeyboardButton(f"👥 {label[:42]}", callback_data=f"ad:evgroup|{chat_id}")])
    rows.append([InlineKeyboardButton("🏠 خانه", callback_data="ad:main")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def build_admin_group_events_page(group_id: int) -> tuple[str, InlineKeyboardMarkup]:
    groups = {int(row[0]): row[1] for row in _unique_active_group_rows()}
    group_title = groups.get(group_id)
    if group_title is None:
        return ("⚠️ این گروه در فهرست گروه‌های فعال نیست.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انتخاب گروه", callback_data="ad:evgroups")]]))
    events = get_all_events_for_chat(group_id, _platform)
    text = f"📅 <b>ایونت‌های گروه {_esc(group_title)}</b>\n\n"
    if events:
        text += "برای مدیریت، ایونت را انتخاب کن:"
        base = admin_manage_events_keyboard(events, group_id)
        rows = [list(row) for row in base.inline_keyboard]
        rows.insert(-1, [InlineKeyboardButton("➕ ساخت ایونت جدید در این گروه", callback_data=f"ad:evnew|{group_id}")])
        keyboard = InlineKeyboardMarkup(rows)
    else:
        text += "هنوز ایونتی در این گروه ثبت نشده.\nمی‌توانی از همین‌جا ایونت بسازی."
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("➕ ساخت ایونت جدید در این گروه", callback_data=f"ad:evnew|{group_id}")], [InlineKeyboardButton("🔙 انتخاب گروه", callback_data="ad:evgroups")]])
    return text, keyboard


async def admin_manage_event_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        if query:
            await query.answer("این دکمه دیگر معتبر نیست.", show_alert=True)
        return
    if OWNER_ID is None or query.from_user.id != OWNER_ID or query.message.chat.type != "private":
        await query.answer("این بخش فقط در پنل خصوصی مالک ربات در دسترس است.", show_alert=True)
        return

    if query.data == "ma:noop":
        await query.answer()
        return

    payload = query.data[len("ma:"):]
    try:
        group_raw, action, *parts = payload.split("|")
        group_id = int(group_raw)
    except (ValueError, TypeError):
        await query.answer("اطلاعات دکمه معتبر نیست.", show_alert=True)
        return

    if action == "list":
        text, keyboard = build_admin_group_events_page(group_id)
        await query.answer()
        await query.edit_message_text(_fmt(text), reply_markup=keyboard, **_html_kwargs())
        return

    event_id = int(parts[0]) if parts and parts[0].isdigit() else None
    event = get_event(event_id) if event_id else None
    if event is None or event.get("chat_id") != group_id or event.get("platform") != _platform:
        await query.answer("این ایونت حذف شده یا متعلق به گروه انتخاب‌شده نیست.", show_alert=True)
        return

    if action == "view":
        attendees = get_attendees(event_id)
        text = (
            f"🛠 <b>مدیریت ایونت</b>\n\n📅 <b>{_esc(event.get('title'))}</b>"
            f"\n👥 حاضرین: <b>{len(attendees)}</b>"
            f"\nوضعیت: {'فعال' if event.get('active', 1) else 'بسته'}"
            f"\n<code>#{event_id}</code>"
        )
        await query.answer()
        await query.edit_message_text(
            _fmt(text),
            reply_markup=admin_manage_event_keyboard(event_id, group_id, event.get("active", 1)),
            **_html_kwargs(),
        )
        return

    if action == "att":
        page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        attendees = get_attendees(event_id)
        await query.answer()
        if not attendees:
            await query.edit_message_text(
                "هنوز کسی ثبت‌نام نکرده.",
                reply_markup=admin_manage_event_keyboard(event_id, group_id, event.get("active", 1)),
            )
            return
        await query.edit_message_text(
            "👥 حاضرین:\n(برای ویرایش نام یا حذف فرد از دکمه‌های کناری استفاده کن)",
            reply_markup=admin_manage_attendees_keyboard(event_id, group_id, attendees, page),
        )
        return

    if action == "ren":
        context.user_data["me_action"] = {
            "type": "rename_event",
            "event_id": event_id,
            "chat_id": query.message.chat.id,
            "target_chat_id": group_id,
            "admin_panel": True,
            "manage_message_id": query.message.message_id,
        }
        await query.answer()
        await query.message.reply_text("✏️ عنوان جدید ایونت را بفرست (حداکثر ۱۰۰ نویسه):")
        return

    if action in ("close", "open"):
        active = action == "open"
        set_event_active(event_id, active)
        await query.answer("وضعیت ایونت به‌روزرسانی شد.")
        await query.edit_message_text(
            f"{'🔓 ایونت باز شد.' if active else '🔒 ایونت بسته شد.'}\n\n<code>#{event_id}</code>",
            reply_markup=admin_manage_event_keyboard(event_id, group_id, int(active)),
            **_html_kwargs(),
        )
        return

    if action == "del":
        await query.answer("در حال حذف…")
        linked_events = get_group_events(event_id)
        delete_event_group(event_id)
        for linked in linked_events:
            if linked.get("platform") != _platform or not linked.get("message_id"):
                continue
            try:
                await context.bot.delete_message(
                    chat_id=linked["chat_id"], message_id=linked["message_id"]
                )
            except Exception:
                logger.warning(
                    "Could not delete linked event message %s in chat %s",
                    linked.get("message_id"), linked.get("chat_id"), exc_info=True,
                )
        text, keyboard = build_admin_group_events_page(group_id)
        await query.edit_message_text(_fmt("🗑 ایونت حذف شد.\n\n" + text), reply_markup=keyboard, **_html_kwargs())
        return

    if action == "an":
        user_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        platform = parts[3] if len(parts) > 3 and parts[3] else _platform
        attendees = get_attendees(event_id)
        if not any(a["user_id"] == user_id and (a.get("platform") or _platform) == platform for a in attendees):
            await query.answer("این کاربر در فهرست حاضرین نیست.", show_alert=True)
            return
        context.user_data["me_action"] = {
            "type": "rename_att",
            "event_id": event_id,
            "user_id": user_id,
            "page": page,
            "platform": platform,
            "chat_id": query.message.chat.id,
            "target_chat_id": group_id,
            "admin_panel": True,
            "manage_message_id": query.message.message_id,
        }
        await query.answer()
        await query.message.reply_text("✏️ نام و نام خانوادگی جدید را بنویس (مثال: علی رضایی):")
        return

    if action == "ar":
        user_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        platform = parts[3] if len(parts) > 3 and parts[3] else _platform
        attendees_before = get_attendees(event_id)
        if not any(a["user_id"] == user_id and (a.get("platform") or _platform) == platform for a in attendees_before):
            await query.answer("این کاربر در فهرست حاضرین نیست.", show_alert=True)
            return
        remove_attendee_from_group(event_id, user_id, platform=platform)
        attendees = get_attendees(event_id)
        await query.answer("حضور حذف شد.")
        if not attendees:
            await query.edit_message_text(
                "حضور حذف شد؛ فهرست حاضرین خالی است.",
                reply_markup=admin_manage_event_keyboard(event_id, group_id, event.get("active", 1)),
            )
            return
        await query.edit_message_text(
            "حضور حذف شد.",
            reply_markup=admin_manage_attendees_keyboard(event_id, group_id, attendees, page),
        )
        return

    await query.answer("عملیات مدیریت شناخته نشد.", show_alert=True)


async def manage_event_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.message is None:
        await query.answer("این دکمه دیگر معتبر نیست.", show_alert=True)
        return
    if query.message.chat.type in ("group", "supergroup"):
        if not await _can_manage_event(update, context):
            await query.answer("فقط ادمین می‌تونه مدیریت کنه.", show_alert=True)
            return
    elif OWNER_ID is None or query.from_user.id != OWNER_ID:
        await query.answer("فقط ادمین می‌تونه مدیریت کنه.", show_alert=True)
        return

    data = query.data[len("me:"):]
    kind, _, rest = data.partition("|")
    parts = rest.split("|") if rest else []

    if kind == "noop":
        await query.answer()
        return

    if kind == "list":
        await query.answer()
        events = get_all_events_for_chat(query.message.chat.id, _platform)
        if not events:
            await query.edit_message_text("هیچ برنامه‌ای در این چت نیست.")
            return
        await query.edit_message_text("برنامه‌ای رو برای مدیریت انتخاب کن:", reply_markup=manage_events_keyboard(events))
        return

    event_id = int(parts[0]) if parts and parts[0].isdigit() else None
    event = get_event(event_id) if event_id else None
    if event is None and kind not in ("list",):
        await query.answer("این برنامه حذف شده یا دیگر در دسترس نیست.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            logger.debug("Could not clear stale management keyboard", exc_info=True)
        return
    if event is not None and (
        event.get("chat_id") != query.message.chat.id
        or event.get("platform") != _platform
    ):
        await query.answer("این برنامه متعلق به این گروه نیست.", show_alert=True)
        return

    if kind == "view":
        await query.answer()
        attendees = get_attendees(event_id)
        text = f"🛠 <b>مدیریت برنامه</b>\n\n📅 <b>{_esc(event['title'])}</b>\n👥 حاضرین: <b>{len(attendees)}</b>\nوضعیت: {'فعال' if event.get('active',1) else 'بسته'}\n<code>#{event_id}</code>"
        await query.edit_message_text(_fmt(text), reply_markup=manage_event_keyboard(event_id, event.get("active",1)), **_html_kwargs())
        return

    if kind == "att":
        await query.answer()
        page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        attendees = get_attendees(event_id)
        if not attendees:
            await query.edit_message_text("هنوز کسی ثبت‌نام نکرده.", reply_markup=manage_event_keyboard(event_id, event.get("active",1)))
            return
        await query.edit_message_text("👥 حاضرین:\n(با دکمه‌ها میشه نام ویرایش یا حذف شه)", reply_markup=manage_attendees_keyboard(event_id, attendees, page))
        return

    if kind == "ren":
        await query.answer()
        context.user_data["me_action"] = {
            "type": "rename_event",
            "event_id": event_id,
            "chat_id": query.message.chat.id,
            "manage_message_id": query.message.message_id,
        }
        await query.message.reply_text("✏️ عنوان جدید برنامه رو بنویس (حداکثر ۱۰۰ نویسه):")
        return

    if kind == "close":
        await query.answer()
        set_event_active(event_id, False)
        await query.edit_message_text(f"🔒 برنامه بسته شد.\n\n<code>#{event_id}</code>", reply_markup=manage_event_keyboard(event_id, 0))
        return

    if kind == "open":
        await query.answer()
        set_event_active(event_id, True)
        await query.edit_message_text(f"برنامه باز شد.\n\n<code>#{event_id}</code>", reply_markup=manage_event_keyboard(event_id, 1))
        return

    if kind == "del":
        await query.answer("در حال حذف...", show_alert=False)
        linked_events = get_group_events(event_id)
        delete_event_group(event_id)
        # Remove every linked post managed by this bot instance. Posts on the
        # other platform are left harmless: their callbacks detect the missing event.
        for linked in linked_events:
            if linked.get("platform") != _platform or not linked.get("message_id"):
                continue
            try:
                await context.bot.delete_message(
                    chat_id=linked["chat_id"],
                    message_id=linked["message_id"],
                )
            except Exception:
                logger.warning(
                    "Could not delete linked event message %s in chat %s",
                    linked.get("message_id"),
                    linked.get("chat_id"),
                    exc_info=True,
                )
        events = get_all_events_for_chat(query.message.chat.id, _platform)
        if not events:
            await query.edit_message_text("برنامه حذف شد. هیچ برنامه دیگری نیست.")
            return
        await query.edit_message_text("برنامه حذف شد.\n\nبرنامه‌ای رو برای مدیریت انتخاب کن:", reply_markup=manage_events_keyboard(events))
        return

    if kind == "an":
        user_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        platform = parts[3] if len(parts) > 3 and parts[3] else _platform
        attendees = get_attendees(event_id)
        if not any(a["user_id"] == user_id and (a.get("platform") or _platform) == platform for a in attendees):
            await query.answer("این کاربر در فهرست حاضرین این برنامه نیست.", show_alert=True)
            return
        await query.answer()
        context.user_data["me_action"] = {
            "type": "rename_att",
            "event_id": event_id,
            "user_id": user_id,
            "page": page,
            "platform": platform,
            "chat_id": query.message.chat.id,
            "manage_message_id": query.message.message_id,
        }
        await query.message.reply_text("✏️ نام و نام خانوادگی جدید رو بنویس (مثال: علی رضایی):")
        return

    if kind == "ar":
        user_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        platform = parts[3] if len(parts) > 3 and parts[3] else _platform
        attendees_before = get_attendees(event_id)
        if not any(a["user_id"] == user_id and (a.get("platform") or _platform) == platform for a in attendees_before):
            await query.answer("این کاربر در فهرست حاضرین این برنامه نیست.", show_alert=True)
            return
        await query.answer()
        remove_attendee_from_group(event_id, user_id, platform=platform)
        # The refresh queue updates every linked event post on its own platform.
        attendees = get_attendees(event_id)
        if not attendees:
            await query.edit_message_text("حضور آن نفر حذف شد.\n\nهیچ کسی ثبت‌نام نکرده.", reply_markup=manage_event_keyboard(event_id, event.get("active",1)))
            return
        await query.edit_message_text("حضور آن نفر حذف شد.", reply_markup=manage_attendees_keyboard(event_id, attendees, page))
        return

    await query.answer("عملیات مدیریت شناخته نشد.", show_alert=True)


async def name_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    parts = query.data.split(":")
    kind = parts[1] if len(parts) > 1 else ""
    if kind == "edit":
        await query.answer()
        context.user_data["awaiting_name"] = True
        await query.edit_message_text("نام و نام خانوادگیت رو دوباره بنویس:")
        return
    if kind == "ok":
        await query.answer("ثبت شد ✅")
        name = context.user_data.pop("pending_name", None)
        pending = context.user_data.pop("pending_action", None) or {}
        context.user_data.pop("pending_name_msg", None)
        if name and user:
            set_profile(user.id, name, _platform)
        if user:
            _remember_flow_message(context, query.message.chat_id, query.message.message_id)
        entries = context.user_data.pop("name_flow_msgs", [])
        asyncio.create_task(_delete_flow_messages(context, entries, delay=2))
        action = pending.get("type")
        if action == "sites":
            await show_sites(update, context)
        elif action == "events":
            await show_events(update, context)
        elif action == "attend" and name and user:
            completed = await _complete_attendance(context, pending.get("event_id"), user, name)
            if not completed and query.message:
                await query.message.reply_text(
                    "نام شما ثبت شد، اما این برنامه در این فاصله بسته یا حذف شده و حضور ثبت نشد."
                )
        return
    await query.answer()


if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()

