"""ثبت ساده‌ی کاربران/گروه‌هایی که با ربات تعامل داشتن، برای گزارش ادمین."""

import html
import json
import os
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.jalali import format_jalali_datetime

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot_data.db")
TEHRAN_TZ = ZoneInfo("Asia/Tehran")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            first_seen TEXT,
            last_seen TEXT,
            interaction_count INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            chat_type TEXT,
            title TEXT,
            username TEXT,
            added_by_id INTEGER,
            added_by_name TEXT,
            first_seen TEXT,
            last_seen TEXT,
            status TEXT DEFAULT 'member'
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS user_chat (
            user_id INTEGER,
            chat_id INTEGER,
            last_seen TEXT,
            interaction_count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, chat_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS site_views (
            user_id INTEGER,
            site_name TEXT,
            view_count INTEGER DEFAULT 0,
            last_seen TEXT,
            PRIMARY KEY (user_id, site_name)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            chat_id INTEGER,
            message_id INTEGER,
            created_by INTEGER,
            created_at TEXT,
            active INTEGER DEFAULT 1,
            platform TEXT NOT NULL DEFAULT 'telegram',
            group_id INTEGER
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS event_refresh_queue (
            event_id INTEGER PRIMARY KEY,
            created_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS event_attendance (
            event_id INTEGER,
            user_id INTEGER,
            first_name TEXT,
            last_name TEXT,
            username TEXT,
            responded_at TEXT,
            PRIMARY KEY (event_id, user_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS user_profiles (
            platform TEXT NOT NULL DEFAULT 'telegram',
            user_id INTEGER NOT NULL,
            full_name TEXT,
            updated_at TEXT,
            PRIMARY KEY (platform, user_id)
        )"""
    )
    with conn:
        event_cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        if "platform" not in event_cols:
            conn.execute("ALTER TABLE events ADD COLUMN platform TEXT NOT NULL DEFAULT 'telegram'")
        if "group_id" not in event_cols:
            conn.execute("ALTER TABLE events ADD COLUMN group_id INTEGER")
        conn.execute("UPDATE events SET group_id = event_id WHERE group_id IS NULL")
        profile_cols = {row[1] for row in conn.execute("PRAGMA table_info(user_profiles)")}
        if "platform" not in profile_cols:
            conn.execute(
                """CREATE TABLE user_profiles_new (
                    platform TEXT NOT NULL DEFAULT 'telegram',
                    user_id INTEGER NOT NULL,
                    full_name TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (platform, user_id)
                )"""
            )
            conn.execute(
                """INSERT OR IGNORE INTO user_profiles_new (platform, user_id, full_name, updated_at)
                   SELECT 'telegram', user_id, full_name, updated_at FROM user_profiles"""
            )
            conn.execute("DROP TABLE user_profiles")
            conn.execute("ALTER TABLE user_profiles_new RENAME TO user_profiles")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _display_name(user) -> str:
    if user.username:
        return f"@{user.username}"
    return user.first_name or str(user.id)


def _fmt_time(iso_str: str | None) -> str:
    if not iso_str:
        return "-"
    dt = datetime.fromisoformat(iso_str).astimezone(TEHRAN_TZ)
    return format_jalali_datetime(dt)


def format_time(iso_str: str | None) -> str:
    return _fmt_time(iso_str)


def record_interaction(user, chat) -> None:
    now = _now()
    conn = _get_conn()
    with conn:
        conn.execute(
            """INSERT INTO users (user_id, username, first_name, last_name, first_seen, last_seen, interaction_count)
               VALUES (?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(user_id) DO UPDATE SET
                   username=excluded.username,
                   first_name=excluded.first_name,
                   last_name=excluded.last_name,
                   last_seen=excluded.last_seen,
                   interaction_count=interaction_count + 1""",
            (user.id, user.username, user.first_name, user.last_name, now, now),
        )
        conn.execute(
            """INSERT INTO chats (chat_id, chat_type, title, username, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   chat_type=excluded.chat_type,
                   title=excluded.title,
                   username=excluded.username,
                   last_seen=excluded.last_seen""",
            (chat.id, chat.type, chat.title, chat.username, now, now),
        )
        conn.execute(
            """INSERT INTO user_chat (user_id, chat_id, last_seen, interaction_count)
               VALUES (?, ?, ?, 1)
               ON CONFLICT(user_id, chat_id) DO UPDATE SET
                   last_seen=excluded.last_seen,
                   interaction_count=interaction_count + 1""",
            (user.id, chat.id, now),
        )
    conn.close()


def record_group_membership(chat, added_by, status: str) -> None:
    now = _now()
    added_by_id = added_by.id if added_by else None
    added_by_name = _display_name(added_by) if added_by else None
    conn = _get_conn()
    with conn:
        conn.execute(
            """INSERT INTO chats (chat_id, chat_type, title, username, added_by_id, added_by_name, first_seen, last_seen, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   chat_type=excluded.chat_type,
                   title=excluded.title,
                   username=excluded.username,
                   status=excluded.status,
                   last_seen=excluded.last_seen,
                   added_by_id=COALESCE(excluded.added_by_id, chats.added_by_id),
                   added_by_name=COALESCE(excluded.added_by_name, chats.added_by_name)""",
            (chat.id, chat.type, chat.title, chat.username, added_by_id, added_by_name, now, now, status),
        )
    conn.close()


def record_site_view(user_id: int, site_name: str) -> None:
    now = _now()
    conn = _get_conn()
    with conn:
        conn.execute(
            """INSERT INTO site_views (user_id, site_name, view_count, last_seen)
               VALUES (?, ?, 1, ?)
               ON CONFLICT(user_id, site_name) DO UPDATE SET
                   view_count=view_count + 1,
                   last_seen=excluded.last_seen""",
            (user_id, site_name, now),
        )
    conn.close()


def get_user_site_views(user_id: int) -> list[tuple]:
    conn = _get_conn()
    cur = conn.execute(
        "SELECT site_name, view_count FROM site_views WHERE user_id = ? ORDER BY view_count DESC, site_name",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_private_users_detailed() -> list[dict]:
    conn = _get_conn()
    cur = conn.execute(
        """SELECT DISTINCT u.user_id, u.username, u.first_name, u.interaction_count, u.last_seen
           FROM users u
           JOIN user_chat uc ON uc.user_id = u.user_id
           JOIN chats c ON c.chat_id = uc.chat_id
           WHERE c.chat_type = 'private'
           ORDER BY u.last_seen DESC"""
    )
    rows = cur.fetchall()
    conn.close()
    result = []
    for uid, username, first_name, count, last_seen in rows:
        result.append(
            {
                "user_id": uid,
                "username": username,
                "first_name": first_name,
                "count": count,
                "last_seen": last_seen,
                "sites": dict(get_user_site_views(uid)),
            }
        )
    return result


def get_top_sites(limit: int = 10) -> list[tuple]:
    conn = _get_conn()
    cur = conn.execute(
        "SELECT site_name, SUM(view_count) AS total FROM site_views "
        "GROUP BY site_name ORDER BY total DESC, site_name LIMIT ?",
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_active_groups_data() -> list[tuple]:
    conn = _get_conn()
    cur = conn.execute(
        """SELECT chat_id, title, added_by_name, last_seen
           FROM chats WHERE chat_type IN ('group', 'supergroup') AND status = 'member'
           ORDER BY last_seen DESC"""
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_total_interactions() -> int:
    conn = _get_conn()
    val = conn.execute("SELECT COALESCE(SUM(interaction_count), 0) FROM users").fetchone()[0]
    conn.close()
    return int(val)


def get_admin_stats() -> dict:
    conn = _get_conn()
    private_users = conn.execute(
        """SELECT COUNT(DISTINCT u.user_id) FROM users u
           JOIN user_chat uc ON uc.user_id = u.user_id
           JOIN chats c ON c.chat_id = uc.chat_id WHERE c.chat_type = 'private'"""
    ).fetchone()[0]
    active_groups = conn.execute(
        """SELECT COUNT(*) FROM chats WHERE chat_type IN ('group', 'supergroup') AND status = 'member'"""
    ).fetchone()[0]
    total_site_views = conn.execute(
        "SELECT COALESCE(SUM(view_count), 0) FROM site_views"
    ).fetchone()[0]
    conn.close()
    return {
        "private_users": int(private_users),
        "active_groups": int(active_groups),
        "total_site_views": int(total_site_views),
        "total_interactions": get_total_interactions(),
    }


def _setting_key(suffix: str, platform: str | None) -> str:
    if not platform:
        return suffix
    return f"{suffix}_{platform}"


def get_settings(platform: str | None = None) -> dict:
    conn = _get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    values = {k: v for k, v in rows}

    # سایت پیش‌فرض بین هر دو پلتفرم مشترکه؛ اگه هنوز کلید مشترک ثبت نشده از نسخه‌ی قبلی براتون
    shared_default = values.get("default_site_idx")
    if shared_default is None:
        for old_key in ("default_site_idx_telegram", "default_site_idx_bale"):
            if values.get(old_key):
                shared_default = values[old_key]
                break

    key_report = _setting_key("report_chat_id", platform)
    key_last_msg = _setting_key("last_report_msg_id", platform)
    key_last_chat = _setting_key("last_report_chat_id", platform)
    default_site = int(shared_default) if shared_default else None
    report_chat = int(values[key_report]) if values.get(key_report) else None
    last_report_msg = _parse_msg_ids(values.get(key_last_msg))
    last_report_chat = int(values[key_last_chat]) if values.get(key_last_chat) else None
    return {
        "default_site_idx": default_site,
        "report_chat_id": report_chat,
        "last_report_msg_id": last_report_msg,
        "last_report_chat_id": last_report_chat,
    }


def _parse_msg_ids(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, int):
            return [parsed]
        if isinstance(parsed, list):
            return [int(x) for x in parsed]
    except (ValueError, TypeError, json.JSONDecodeError):
        try:
            return [int(raw)]
        except ValueError:
            return None
    return None


def set_default_site(site_idx: int, platform: str | None = None) -> None:
    _set_setting("default_site_idx", str(site_idx))
    # پاکسازی کلیدهای قدیمی پلتفرم‌محور تا همیشه یه منبع واحد داشته باشیم
    _delete_setting("default_site_idx_telegram")
    _delete_setting("default_site_idx_bale")


def set_report_chat(chat_id: int | None, platform: str | None = None) -> None:
    if chat_id is None:
        _delete_setting(_setting_key("report_chat_id", platform))
        _delete_setting(_setting_key("last_report_msg_id", platform))
        _delete_setting(_setting_key("last_report_chat_id", platform))
    else:
        _set_setting(_setting_key("report_chat_id", platform), str(chat_id))


def set_last_report_message(message_ids, chat_id: int, platform: str | None = None) -> None:
    if isinstance(message_ids, int):
        message_ids = [message_ids]
    _set_setting(_setting_key("last_report_msg_id", platform), json.dumps(list(message_ids)))
    _set_setting(_setting_key("last_report_chat_id", platform), str(chat_id))


DEFAULT_REPORT_HOURS = list(range(7, 20))


def get_report_hours(platform: str | None = None) -> list[int]:
    # ساعت‌های اعلامی بین هر دو پلتفرم مشترکه
    key = "report_hours"
    conn = _get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    if not row:
        return list(DEFAULT_REPORT_HOURS)
    try:
        hours = sorted({int(h) for h in json.loads(row[0]) if 0 <= int(h) <= 23})
    except (ValueError, TypeError, json.JSONDecodeError):
        return list(DEFAULT_REPORT_HOURS)
    return hours


def set_report_hours(hours: list[int], platform: str | None = None) -> None:
    key = "report_hours"
    clean = sorted({int(h) for h in hours if 0 <= int(h) <= 23})
    if clean == DEFAULT_REPORT_HOURS:
        _delete_setting(key)
        return
    _set_setting(key, json.dumps(clean))


def _set_setting(key: str, value: str) -> None:
    conn = _get_conn()
    with conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
    conn.close()


def _delete_setting(key: str) -> None:
    conn = _get_conn()
    with conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    conn.close()


def get_recent_private_users(limit: int = 10) -> list[tuple]:
    conn = _get_conn()
    cur = conn.execute(
        """SELECT u.user_id, u.username, u.first_name, u.interaction_count
           FROM users u
           JOIN user_chat uc ON uc.user_id = u.user_id
           JOIN chats c ON c.chat_id = uc.chat_id
           WHERE c.chat_type = 'private'
           ORDER BY u.last_seen DESC LIMIT ?""",
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_admin_report() -> str:
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute(
        """SELECT DISTINCT u.user_id, u.username, u.first_name, u.interaction_count, u.last_seen
           FROM users u
           JOIN user_chat uc ON uc.user_id = u.user_id
           JOIN chats c ON c.chat_id = uc.chat_id
           WHERE c.chat_type = 'private'
           ORDER BY u.last_seen DESC"""
    )
    private_users = cur.fetchall()

    cur.execute(
        """SELECT chat_id, title, status, added_by_name, last_seen
           FROM chats WHERE chat_type IN ('group', 'supergroup')
           ORDER BY last_seen DESC"""
    )
    groups = cur.fetchall()

    cur.execute(
        """SELECT DISTINCT u.user_id, u.username, u.first_name
           FROM users u
           JOIN user_chat uc ON uc.user_id = u.user_id
           JOIN chats c ON c.chat_id = uc.chat_id
           WHERE c.chat_type IN ('group', 'supergroup')"""
    )
    group_users = cur.fetchall()

    conn.close()

    def esc(s) -> str:
        return html.escape(str(s)) if s is not None else ""

    def user_label(uid, username, first_name) -> str:
        name = f"@{username}" if username else (first_name or "بدون نام")
        return f"{esc(name)} <code>{uid}</code>"

    active_groups = [g for g in groups if g[2] == "member"]
    left_groups = [g for g in groups if g[2] != "member"]

    sections: list[str] = []
    sections.append(
        f"📊 <b>گزارش ربات</b>\n"
        f"🕐 {esc(_fmt_time(datetime.now(timezone.utc).isoformat(timespec='seconds')))}"
    )

    part = [f"👤 <b>کاربران خصوصی</b> ({len(private_users)})"]
    if private_users:
        for i, (uid, username, first_name, count, last_seen) in enumerate(private_users[:30], 1):
            site_rows = get_user_site_views(uid)
            sites_str = "، ".join(f"{esc(s)} ×{v}" for s, v in site_rows)
            part.append(
                f"{i}. {user_label(uid, username, first_name)}\n"
                f"    🔁 {count} بار — 🕐 {esc(_fmt_time(last_seen))}"
                + (f"\n    🌐 {sites_str}" if sites_str else "")
            )
    else:
        part.append("هنوز کسی به‌صورت خصوصی از ربات استفاده نکرده.")
    sections.append("\n".join(part))

    part = [f"👥 <b>گروه‌های فعال</b> ({len(active_groups)})"]
    if active_groups:
        for i, (chat_id, title, status, added_by, last_seen) in enumerate(active_groups, 1):
            part.append(
                f"{i}. <b>{esc(title or chat_id)}</b> — <code>{chat_id}</code>\n"
                f"    ➕ اضافه‌کننده: {esc(added_by or '-')}"
            )
    else:
        part.append("ربات هنوز به هیچ گروهی اضافه نشده.")
    sections.append("\n".join(part))

    if left_groups:
        part = [f"🚫 <b>گروه‌های حذف‌شده</b> ({len(left_groups)})"]
        for i, (chat_id, title, status, added_by, last_seen) in enumerate(left_groups, 1):
            part.append(f"{i}. {esc(title or chat_id)} — <code>{chat_id}</code>")
        sections.append("\n".join(part))

    part = [f"👥 <b>اعضای فعال در گروه‌ها</b> ({len(group_users)})"]
    if group_users:
        for i, (uid, username, first_name) in enumerate(group_users[:30], 1):
            part.append(f"{i}. {user_label(uid, username, first_name)}")
    else:
        part.append("هنوز کسی داخل گروه‌ها با ربات تعامل نداشته.")
    sections.append("\n".join(part))

    return "\n\n〰️〰️〰️〰️〰️\n\n".join(sections)


def create_event(title: str, chat_id: int, created_by: int, platform: str = "telegram") -> int:
    conn = _get_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO events (title, chat_id, created_by, created_at, active, platform) VALUES (?, ?, ?, ?, 1, ?)",
            (title, chat_id, created_by, _now(), platform),
        )
        event_id = cur.lastrowid
        conn.execute("UPDATE events SET group_id = ? WHERE event_id = ?", (event_id, event_id))
    conn.close()
    return int(event_id)


def set_event_message(event_id: int, chat_id: int, message_id: int) -> None:
    conn = _get_conn()
    with conn:
        conn.execute(
            "UPDATE events SET chat_id = ?, message_id = ? WHERE event_id = ?",
            (chat_id, message_id, event_id),
        )
    conn.close()


def get_event(event_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT event_id, title, chat_id, message_id, created_by, created_at, active, platform, group_id "
        "FROM events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    keys = ("event_id", "title", "chat_id", "message_id", "created_by", "created_at", "active", "platform", "group_id")
    return dict(zip(keys, row))


def get_all_events_for_chat(chat_id: int, platform: str = "telegram") -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT event_id, title, created_at, active FROM events WHERE chat_id = ? AND platform = ? ORDER BY event_id DESC",
        (chat_id, platform),
    ).fetchall()
    conn.close()
    keys = ("event_id", "title", "created_at", "active")
    return [dict(zip(keys, r)) for r in rows]


def get_events_for_chat(chat_id: int, platform: str = "telegram") -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT event_id, title, created_at FROM events "
        "WHERE chat_id = ? AND platform = ? AND active = 1 ORDER BY event_id DESC",
        (chat_id, platform),
    ).fetchall()
    conn.close()
    keys = ("event_id", "title", "created_at")
    return [dict(zip(keys, r)) for r in rows]


def get_latest_event_for_chat(chat_id: int, platform: str = "telegram") -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT event_id, title, chat_id, message_id, created_by, created_at, active, platform, group_id "
        "FROM events WHERE chat_id = ? AND platform = ? AND active = 1 ORDER BY event_id DESC LIMIT 1",
        (chat_id, platform),
    ).fetchone()
    conn.close()
    if not row:
        return None
    keys = ("event_id", "title", "chat_id", "message_id", "created_by", "created_at", "active", "platform", "group_id")
    return dict(zip(keys, row))


def update_event_title(event_id: int, title: str) -> None:
    conn = _get_conn()
    with conn:
        conn.execute(
            "UPDATE events SET title = ? WHERE event_id = ?",
            ((title or "").strip(), event_id),
        )
    conn.close()


def set_event_active(event_id: int, active: bool) -> None:
    conn = _get_conn()
    with conn:
        conn.execute(
            "UPDATE events SET active = ? WHERE event_id = ?",
            (1 if active else 0, event_id),
        )
    conn.close()


def delete_event_group(event_id: int) -> None:
    event = get_event(event_id)
    group_id = event.get("group_id") if event else event_id
    conn = _get_conn()
    with conn:
        conn.execute("DELETE FROM event_attendance WHERE event_id IN (SELECT event_id FROM events WHERE group_id = ?)", (group_id,))
        conn.execute("DELETE FROM events WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM event_refresh_queue WHERE event_id IN (SELECT event_id FROM events WHERE group_id = ?)", (group_id,))
    conn.close()


def update_attendee_name_in_group(event_id: int, user_id: int, first_name: str, last_name: str | None, platform: str = "telegram") -> None:
    event = get_event(event_id)
    group_id = event.get("group_id") if event else event_id
    full_name = " ".join(p for p in [(first_name or "").strip(), (last_name or "").strip()] if p).strip() or None
    conn = _get_conn()
    with conn:
        conn.execute(
            "UPDATE event_attendance SET first_name = ?, last_name = ? WHERE user_id = ? AND event_id IN (SELECT event_id FROM events WHERE group_id = ?)",
            ((first_name or "").strip() or None, (last_name or "").strip() or None, user_id, group_id),
        )
        if full_name:
            conn.execute(
                "INSERT INTO user_profiles (platform, user_id, full_name, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(platform, user_id) DO UPDATE SET full_name = excluded.full_name, updated_at = excluded.updated_at",
                (platform, user_id, full_name, _now()),
            )
    conn.close()
    enqueue_group_refresh(group_id)


def remove_attendee_from_group(event_id: int, user_id: int) -> bool:
    event = get_event(event_id)
    group_id = event.get("group_id") if event else event_id
    conn = _get_conn()
    with conn:
        cur = conn.execute(
            "DELETE FROM event_attendance WHERE user_id = ? AND event_id IN (SELECT event_id FROM events WHERE group_id = ?)",
            (user_id, group_id),
        )
        removed = cur.rowcount > 0
    conn.close()
    if group_id:
        enqueue_group_refresh(group_id)
    return removed


def get_group_events(event_id: int) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT event_id, title, chat_id, message_id, created_by, created_at, active, platform, group_id "
        "FROM events WHERE group_id = (SELECT group_id FROM events WHERE event_id = ?)",
        (event_id,),
    ).fetchall()
    conn.close()
    keys = ("event_id", "title", "chat_id", "message_id", "created_by", "created_at", "active", "platform", "group_id")
    return [dict(zip(keys, r)) for r in rows]


def link_events(event_id_a: int, event_id_b: int) -> bool:
    a, b = get_event(event_id_a), get_event(event_id_b)
    if not a or not b:
        return False
    conn = _get_conn()
    target = min(a["group_id"] or event_id_a, b["group_id"] or event_id_b)
    with conn:
        conn.execute(
            "UPDATE events SET group_id = ? WHERE group_id IN (?, ?)",
            (target, a["group_id"] or event_id_a, b["group_id"] or event_id_b),
        )
    conn.close()
    enqueue_group_refresh(target)
    return True


def get_profile(user_id: int, platform: str = "telegram") -> str | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT full_name FROM user_profiles WHERE platform = ? AND user_id = ?",
        (platform, user_id),
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def delete_profile(user_id: int, platform: str = "telegram") -> None:
    conn = _get_conn()
    with conn:
        conn.execute("DELETE FROM user_profiles WHERE platform = ? AND user_id = ?", (platform, user_id))
    conn.close()


def update_profile_name(user_id: int, full_name: str, platform: str = "telegram") -> None:
    full_name_clean = (full_name or "").strip() or None
    conn = _get_conn()
    with conn:
        if full_name_clean:
            conn.execute(
                "INSERT INTO user_profiles (platform, user_id, full_name, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(platform, user_id) DO UPDATE SET full_name = excluded.full_name, updated_at = excluded.updated_at",
                (platform, user_id, full_name_clean, _now()),
            )
        else:
            conn.execute("DELETE FROM user_profiles WHERE platform = ? AND user_id = ?", (platform, user_id))
    conn.close()


def set_profile(user_id: int, full_name: str, platform: str = "telegram") -> None:
    update_profile_name(user_id, full_name, platform)


def record_attendance(event_id: int, user, name: str | None = None, platform: str = "telegram") -> bool:
    full_name = (name or get_profile(user.id, platform) or "").strip()
    if full_name:
        parts = full_name.split(None, 1)
        first_name = parts[0]
        last_name = parts[1].strip() if len(parts) > 1 else None
    else:
        first_name, last_name = user.first_name, user.last_name
    conn = _get_conn()
    with conn:
        existing = conn.execute(
            "SELECT 1 FROM event_attendance WHERE event_id = ? AND user_id = ?",
            (event_id, user.id),
        ).fetchone()
        conn.execute(
            """INSERT INTO event_attendance (event_id, user_id, first_name, last_name, username, responded_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(event_id, user_id) DO UPDATE SET
                   first_name=excluded.first_name,
                   last_name=excluded.last_name,
                   username=excluded.username,
                   responded_at=excluded.responded_at""",
            (event_id, user.id, first_name, last_name, user.username, _now()),
        )
    conn.close()
    enqueue_group_refresh(get_event(event_id).get("group_id") if get_event(event_id) else None)
    return existing is None


def remove_attendance(event_id: int, user_id: int) -> bool:
    event = get_event(event_id)
    conn = _get_conn()
    with conn:
        cur = conn.execute(
            "DELETE FROM event_attendance WHERE user_id = ? AND event_id IN "
            "(SELECT event_id FROM events WHERE group_id = ?)",
            (user_id, event["group_id"] if event else event_id),
        )
        removed = cur.rowcount > 0
    conn.close()
    if event:
        enqueue_group_refresh(event["group_id"])
    return removed


def get_attendees(event_id: int) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT ea.user_id, ea.first_name, ea.last_name, ea.username, ea.responded_at, e.platform "
        "FROM event_attendance ea JOIN events e ON e.event_id = ea.event_id "
        "WHERE e.group_id = (SELECT group_id FROM events WHERE event_id = ?) "
        "ORDER BY ea.responded_at, ea.user_id",
        (event_id,),
    ).fetchall()
    conn.close()
    keys = ("user_id", "first_name", "last_name", "username", "responded_at", "platform")
    return [dict(zip(keys, r)) for r in rows]


def enqueue_group_refresh(group_id) -> None:
    if group_id is None:
        return
    conn = _get_conn()
    with conn:
        conn.execute(
            """INSERT INTO event_refresh_queue (event_id, created_at)
               SELECT event_id, ? FROM events WHERE group_id = ? AND message_id IS NOT NULL
               ON CONFLICT(event_id) DO UPDATE SET created_at=excluded.created_at""",
            (_now(), group_id),
        )
    conn.close()


def get_pending_refreshes(platform: str, limit: int = 20) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT e.event_id, e.title, e.chat_id, e.message_id, e.created_by, e.created_at, e.active, e.platform, e.group_id "
        "FROM event_refresh_queue q JOIN events e ON e.event_id = q.event_id "
        "WHERE e.platform = ? AND e.message_id IS NOT NULL LIMIT ?",
        (platform, limit),
    ).fetchall()
    conn.close()
    keys = ("event_id", "title", "chat_id", "message_id", "created_by", "created_at", "active", "platform", "group_id")
    return [dict(zip(keys, r)) for r in rows]


def clear_refresh(event_id: int) -> None:
    conn = _get_conn()
    with conn:
        conn.execute("DELETE FROM event_refresh_queue WHERE event_id = ?", (event_id,))
    conn.close()


def get_group_users_unregistered(chat_id: int, platform: str = "telegram") -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        """SELECT u.user_id, u.username, u.first_name, u.last_name
           FROM users u
           JOIN user_chat uc ON uc.user_id = u.user_id
           WHERE uc.chat_id = ? AND NOT EXISTS (
               SELECT 1 FROM user_profiles p WHERE p.user_id = u.user_id AND p.platform = ?
           )
           ORDER BY u.user_id""",
        (chat_id, platform),
    ).fetchall()
    conn.close()
    keys = ("user_id", "username", "first_name", "last_name")
    return [dict(zip(keys, r)) for r in rows]


def get_attendance_users() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        """SELECT ea.user_id, ea.first_name, ea.last_name, ea.username, p.full_name, e.platform,
                  MAX(ea.responded_at) as last_res
           FROM event_attendance ea
           JOIN events e ON e.event_id = ea.event_id
           LEFT JOIN user_profiles p ON p.user_id = ea.user_id AND p.platform = e.platform
           GROUP BY e.platform, ea.user_id
           ORDER BY last_res DESC"""
    ).fetchall()
    profiles = conn.execute(
        "SELECT platform, user_id, full_name FROM user_profiles"
    ).fetchall()
    conn.close()
    out = []
    seen = set()
    for uid, fn, ln, un, pf, plat, _lr in rows:
        key = (plat, uid)
        seen.add(key)
        full = pf or " ".join(x for x in (fn, ln) if x).strip() or None
        out.append({"user_id": uid, "first_name": fn, "last_name": ln, "username": un, "full_name": full, "platform": plat})
    for plat, uid, full in profiles:
        key = (plat, uid)
        if key in seen:
            continue
        seen.add(key)
        out.append({"user_id": uid, "first_name": None, "last_name": None, "username": None, "full_name": full, "platform": plat})
    return out


def close_event(event_id: int) -> None:
    conn = _get_conn()
    with conn:
        conn.execute("UPDATE events SET active = 0 WHERE event_id = ?", (event_id,))
    conn.close()
