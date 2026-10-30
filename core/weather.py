"""هسته‌ی مشترک گرفتن اطلاعات باد از Windy — هم تلگرام و هم بله از این استفاده می‌کنند.

نمایش پیش‌بینی به‌صورت برآورد احتمالی با تمرکز روی دو مدل اصلی (GFS و ICON) انجام می‌شه.
"""

import asyncio
import math
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from core.jalali import format_jalali_datetime

WINDY_API_URL = "https://api.windy.com/api/point-forecast/v2"
KERMANSHAH_LAT = 34.3142
KERMANSHAH_LON = 47.0650
TEHRAN_TZ = ZoneInfo("Asia/Tehran")
CACHE_TTL_SECONDS = 600
_CACHE: dict[tuple, tuple[float, dict]] = {}

# مدل‌هایی که کلید فعلی Windy برای منطقه‌ی ایران پشتیبانی می‌کند
MODELS = {
    "gfs": {"name": "GFS", "country": "آمریکا", "flag": "🇺🇸", "theme": "🦅"},
    "icon": {"name": "ICON", "country": "آلمان", "flag": "🇩🇪", "theme": "⚙️"},
}

PRIMARY_MODELS = ("GFS", "ICON")

WINDY_PARAMETERS = [
    "temp",
    "dewpoint",
    "precip",
    "convPrecip",
    "snowPrecip",
    "wind",
    "windGust",
    "cape",
    "ptype",
    "lclouds",
    "mclouds",
    "hclouds",
    "rh",
    "pressure",
]

PTYPE_DESCRIPTIONS = {
    0: "بدون بارش ☀️",
    1: "باران 🌧",
    3: "باران یخ‌زده 🌧❄️",
    5: "برف ❄️",
    7: "مخلوط برف و باران 🌧❄️",
    8: "تگرگ / گوی‌های یخی 🌨",
}

COMPASS_POINTS = [
    "شمال", "شمال شرقی", "شرق", "جنوب شرقی",
    "جنوب", "جنوب غربی", "غرب", "شمال غربی",
]

# فلش‌ها جهتی که باد بهش می‌وزه رو نشون می‌دن (نه جهتی که ازش میاد) — مثل بادنما
WIND_ARROWS = ["⬆️", "↗️", "➡️", "↘️", "⬇️", "↙️", "⬅️", "↖️"]

FIXED_HOURS = [6, 8, 10, 12, 14, 16, 18]

FOOTER_NOTE = (
    "💼 داده‌های این پیش‌بینی از سرویس Windy دریافت شده است."
)


def degrees_to_compass(deg: float) -> str:
    idx = round(deg / 45) % 8
    return COMPASS_POINTS[idx]


def wind_arrow(deg: float) -> str:
    """فلش نشون‌دهنده‌ی جهتی که باد به سمتش می‌وزه (deg همون جهتیه که باد ازش میاد)."""
    to_deg = (deg + 180) % 360
    idx = round(to_deg / 45) % 8
    return WIND_ARROWS[idx]


WMO_CONDITIONS = {
    0: ("آفتابی", "☀️"),
    1: ("عمدتاً آفتابی", "🌤"),
    2: ("نیمه‌ابری", "⛅"),
    3: ("ابری", "☁️"),
    45: ("مه‌آلود", "🌫"),
    48: ("مه یخ‌زده", "🌫"),
    51: ("نم‌نم باران سبک", "🌦"),
    53: ("نم‌نم باران", "🌦"),
    55: ("نم‌نم باران شدید", "🌧"),
    56: ("باران یخ‌زننده", "🌧"),
    57: ("باران یخ‌زننده شدید", "🌧"),
    61: ("باران سبک", "🌧"),
    63: ("بارانی", "🌧"),
    65: ("باران شدید", "🌧"),
    66: ("باران یخ‌زننده", "🌧"),
    67: ("باران یخ‌زننده شدید", "🌧"),
    71: ("برف سبک", "🌨"),
    73: ("برفی", "❄️"),
    75: ("برف سنگین", "❄️"),
    77: ("دانه‌های برف", "🌨"),
    80: ("رگبار سبک", "🌦"),
    81: ("رگبار", "🌧"),
    82: ("رگبار شدید", "⛈"),
    85: ("رگبار برف", "🌨"),
    86: ("رگبار برف شدید", "❄️"),
    95: ("رعدوبرق", "⛈"),
    96: ("رعدوبرق با تگرگ", "⛈"),
    99: ("رعدوبرق شدید با تگرگ", "⛈"),
}


def describe_weather(
    code=None,
    cloud=None,
    precip=None,
    snow=None,
    temp=None,
) -> tuple[str, str]:
    """وضعیت آب‌وهوا رو به (برچسب فارسی، ایموجی) تبدیل می‌کنه."""
    if code is not None and code in WMO_CONDITIONS:
        label, emoji = WMO_CONDITIONS[code]
        return label, emoji

    if (snow is not None and snow > 0) or (
        precip is not None and precip > 0 and temp is not None and temp <= 1
    ):
        return "برفی", "❄️"
    if precip is not None and precip >= 2:
        return "رگبار", "🌧"
    if precip is not None and precip > 0:
        return "نم‌نم باران", "🌦"
    if cloud is not None:
        if cloud < 15:
            return "آفتابی", "☀️"
        if cloud < 50:
            return "نیمه‌ابری", "⛅"
        if cloud < 85:
            return "ابری", "☁️"
        return "تمام‌ابری", "☁️"
    return "", ""


def weather_line(code=None, cloud=None, precip=None, snow=None, temp=None) -> str:
    label, emoji = describe_weather(code, cloud, precip, snow, temp)
    if not label:
        return ""
    return f"{emoji} وضعیت: {label}"


def _windy_payload(model_key: str, lat: float, lon: float, api_key: str) -> dict:
    return {
        "lat": lat,
        "lon": lon,
        "model": model_key,
        "parameters": WINDY_PARAMETERS,
        "levels": ["surface"],
        "key": api_key,
    }


def _target_ms(target_time: str) -> int:
    dt = datetime.fromisoformat(target_time)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TEHRAN_TZ)
    return int(dt.timestamp() * 1000)


def _interp(timestamps: list, values: list, target: int):
    if not timestamps or not values:
        return None
    pairs = [(t, v) for t, v in zip(timestamps, values) if v is not None]
    if not pairs:
        return None
    if target <= pairs[0][0]:
        return pairs[0][1]
    if target >= pairs[-1][0]:
        return pairs[-1][1]
    for (t0, v0), (t1, v1) in zip(pairs, pairs[1:]):
        if t0 <= target <= t1:
            if t1 == t0:
                return v0
            ratio = (target - t0) / (t1 - t0)
            return v0 + (v1 - v0) * ratio
    return None


def _nearest(timestamps: list, values: list, target: int):
    if not timestamps or not values:
        return None
    pairs = [(t, v) for t, v in zip(timestamps, values) if v is not None]
    if not pairs:
        return None
    return min(pairs, key=lambda pair: abs(pair[0] - target))[1]


def _wind_direction(u: float, v: float) -> float:
    return (math.degrees(math.atan2(-u, -v)) + 360) % 360


def _wmo_code_from_conditions(ptype, precip, snow, cloud) -> int:
    ptype = int(round(ptype)) if ptype is not None else None
    if ptype == 5 or (snow is not None and snow > 0):
        return 75 if snow is not None and snow >= 1 else 71
    if ptype == 3:
        return 66
    if ptype == 7:
        return 67
    if ptype == 8:
        return 96
    if ptype == 1 or (precip is not None and precip > 0):
        return 63 if precip is not None and precip >= 2 else 61
    if cloud is None:
        return 0
    if cloud < 15:
        return 0
    if cloud < 50:
        return 1
    if cloud < 85:
        return 2
    return 3


def _build_model_record(model_key: str, info: dict, data: dict, target: int):
    timestamps = data.get("ts") or []
    if not timestamps:
        return None

    temp_k = _interp(timestamps, data.get("temp-surface", []), target)
    wind_u = _interp(timestamps, data.get("wind_u-surface", []), target)
    wind_v = _interp(timestamps, data.get("wind_v-surface", []), target)
    if temp_k is None or wind_u is None or wind_v is None:
        return None

    gust_ms = _nearest(timestamps, data.get("gust-surface", []), target)
    cloud = _interp(timestamps, data.get("lclouds-surface", []), target)
    precip = _nearest(timestamps, data.get("past3hprecip-surface", []), target)
    snow = _nearest(timestamps, data.get("past3hsnowprecip-surface", []), target)
    ptype = _nearest(timestamps, data.get("ptype-surface", []), target)

    temp_c = temp_k - 273.15 if temp_k > 100 else temp_k
    speed_kmh = math.hypot(wind_u, wind_v) * 3.6

    return {
        "model": info["name"],
        "country": info["country"],
        "flag": info["flag"],
        "theme": info["theme"],
        "speed": round(speed_kmh, 1),
        "direction": round(_wind_direction(wind_u, wind_v), 1),
        "gust": round(gust_ms * 3.6, 1) if gust_ms is not None else None,
        "cloud": round(cloud, 1) if cloud is not None else None,
        "precip": round(precip, 2) if precip is not None else None,
        "snow": round(snow, 2) if snow is not None else None,
        "code": _wmo_code_from_conditions(ptype, precip, snow, cloud),
        "temp": round(temp_c, 1),
    }


def _records_at(data_map: dict[str, dict], target: int) -> list[dict]:
    records = []
    for key, data in data_map.items():
        record = _build_model_record(key, MODELS[key], data, target)
        if record is not None:
            records.append(record)
    return records


async def _fetch_model(client: httpx.AsyncClient, model_key: str, lat: float, lon: float):
    cache_key = (model_key, round(lat, 4), round(lon, 4))
    now = time.monotonic()
    cached = _CACHE.get(cache_key)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    api_key = os.getenv("WINDY_API_KEY")
    if not api_key:
        raise RuntimeError("WINDY_API_KEY تنظیم نشده است.")

    response = await client.post(
        WINDY_API_URL, json=_windy_payload(model_key, lat, lon, api_key)
    )
    if response.status_code == 204:
        return None
    response.raise_for_status()
    data = response.json()
    _CACHE[cache_key] = (now, data)
    return data


async def _fetch_models(lat: float, lon: float) -> dict[str, dict]:
    async with httpx.AsyncClient(timeout=20) as client:
        results = await asyncio.gather(
            *(_fetch_model(client, key, lat, lon) for key in MODELS),
            return_exceptions=True,
        )

    data_map: dict[str, dict] = {}
    errors = []
    for key, result in zip(MODELS, results):
        if isinstance(result, Exception):
            errors.append(result)
        elif result:
            data_map[key] = result
    if not data_map and errors:
        raise errors[0]
    return data_map


def _circular_mean_deg(degrees: list[float]) -> float:
    if not degrees:
        return 0.0
    s_sum = 0.0
    c_sum = 0.0
    for d in degrees:
        rad = math.radians(d % 360)
        s_sum += math.sin(rad)
        c_sum += math.cos(rad)
    if c_sum == 0.0 and s_sum == 0.0:
        return 0.0
    mean_rad = math.atan2(s_sum, c_sum)
    mean_deg = math.degrees(mean_rad) % 360
    return mean_deg


def _safe_avg(values: list[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 1)


def compute_dual_estimate(models: list[dict]) -> dict | None:
    """برآورد بر پایه دو مدل اصلی GFS و ICON (میانگین دقیق هرگاه هر دو موجود باشن)."""
    if not models:
        return None

    valid = [m for m in models if m.get("speed") is not None]
    if not valid:
        return None

    main_list = [m for m in valid if m["model"] in PRIMARY_MODELS]

    if len(main_list) >= 2:
        core = main_list[:2]
        method = "gfs_icon_avg"
    elif len(main_list) == 1:
        core = main_list
        method = "single_main"
    else:
        core = valid[:2]
        method = "fallback_top2"

    speeds = [m["speed"] for m in core]
    dirs = [m["direction"] for m in core if m.get("direction") is not None]
    gusts = [m["gust"] for m in core]

    speed_mean = round(sum(speeds) / len(speeds), 1)
    dir_mean = _circular_mean_deg(dirs) if dirs else 0.0
    gust_mean = _safe_avg(gusts)

    models_used = [m["model"] for m in core]

    codes = [m.get("code") for m in core if m.get("code") is not None]
    cloud_mean = _safe_avg([m.get("cloud") for m in core])
    precip_mean = _safe_avg([m.get("precip") for m in core])
    snow_mean = _safe_avg([m.get("snow") for m in core])
    temp_mean = _safe_avg([m.get("temp") for m in core])
    shared_code = codes[0] if len(codes) == len(core) and len(set(codes)) == 1 else None
    w_label, w_emoji = describe_weather(
        shared_code, cloud_mean, precip_mean, snow_mean, temp_mean
    )

    return {
        "speed": speed_mean,
        "direction": dir_mean,
        "gust": gust_mean,
        "count_used": len(core),
        "models_used": models_used,
        "method": method,
        "cloud": cloud_mean,
        "precip": precip_mean,
        "snow": snow_mean,
        "temp": temp_mean,
        "weather_label": w_label,
        "weather_emoji": w_emoji,
    }


async def get_multi_model_wind(lat: float, lon: float, target_time: str) -> list[dict]:
    """پیش‌بینی یک ساعت مشخص رو از مدل‌های Windy می‌گیره. target_time: 'YYYY-MM-DDTHH:00'."""
    data_map = await _fetch_models(lat, lon)
    return _records_at(data_map, _target_ms(target_time))


async def get_multi_model_daily(
    lat: float, lon: float, date_str: str, hours: list[int] | None = None
) -> dict[int, list[dict]]:
    """پیش‌بینی چند ساعت مشخص از یک روز رو از مدل‌های Windy می‌گیره."""
    if hours is None:
        hours = FIXED_HOURS

    data_map = await _fetch_models(lat, lon)
    result: dict[int, list[dict]] = {}
    for h in hours:
        result[h] = _records_at(data_map, _target_ms(f"{date_str}T{h:02d}:00"))
    return result


def format_models_message(
    location_name: str,
    time_label: str,
    models: list[dict],
    requested_by: str | None = None,
    updated_at=None,
) -> str:
    requested_line = f"🙋 به درخواست {requested_by}\n\n" if requested_by else ""
    if not models:
        return (
            f"{requested_line}📍 {location_name} — {time_label}\n\n"
            "هیچ‌کدوم از مدل‌ها پیش‌بینی‌ای برای این ساعت ندارن."
            + _updated_line(updated_at)
        )

    lines = [f"{requested_line}📍 {location_name} — {time_label}\n"]
    for r in models:
        lines.append(f"{r['theme']} {r['flag']} {r['model']} ({r['country']})")
        wl = weather_line(r.get("code"), r.get("cloud"), r.get("precip"), r.get("snow"), r.get("temp"))
        if wl:
            lines.append(wl)
        lines.append(f"💨 باد: {r['speed']} KM/h")
        if r["gust"] is not None:
            lines.append(f"🌬 گاست: {r['gust']} KM/h")
        lines.append(f"🧭 جهت: {degrees_to_compass(r['direction'])} {wind_arrow(r['direction'])}")
        lines.append("〰️〰️〰️〰️〰️")

    lines.append(FOOTER_NOTE)
    lines.append(_updated_line(updated_at))
    return "\n".join(lines).rstrip()


def format_dual_estimate_message(
    location_name: str,
    time_label: str,
    models: list[dict],
    requested_by: str | None = None,
    updated_at=None,
) -> str:
    requested_line = f"🙋 به درخواست {requested_by}\n\n" if requested_by else ""
    best = compute_dual_estimate(models)

    if not best or not models:
        return (
            f"{requested_line}📍 {location_name} — {time_label}\n\n"
            "پیش‌بینی‌ای برای این ساعت در دسترس نیست."
            + _updated_line(updated_at)
        )

    gust_str = f"{best['gust']} KM/h" if best["gust"] is not None else "—"
    used = " + ".join(best["models_used"])

    weather_row = ""
    if best.get("weather_label"):
        weather_row = f"{best['weather_emoji']} وضعیت: {best['weather_label']}"

    lines = [
        f"{requested_line}📍 {location_name} — {time_label}",
        "🎯 برآورد احتمالی (میانگین دقیق GFS و ICON)",
    ]
    if weather_row:
        lines.append(weather_row)
    lines += [
        f"💨 باد: {best['speed']} KM/h",
        f"🌬 گاست: {gust_str}",
        f"🧭 جهت: {degrees_to_compass(best['direction'])} {wind_arrow(best['direction'])}",
        f"📊 مبنا: {used}",
        "〰️〰️〰️〰️〰️",
        "💡 فقط GFS و ICON در نظر گرفته شدن (میانگین ساده و دقیق هر دو مدل).",
        FOOTER_NOTE,
        _updated_line(updated_at),
    ]
    return "\n".join(lines).rstrip()


def _updated_line(updated_at=None) -> str:
    if not updated_at:
        return ""
    return f"\n\n🕐 آخرین بروزرسانی پیش‌بینی از منبع (Windy): {format_jalali_datetime(updated_at)} به‌وقت تهران"


def format_models_daily(
    location_name: str,
    day_label: str,
    records_by_hour: dict[int, list[dict]],
    requested_by: str | None = None,
    updated_at=None,
) -> str:
    requested_line = f"🙋 به درخواست {requested_by}\n\n" if requested_by else ""
    if not records_by_hour or not any(records_by_hour.values()):
        return f"{requested_line}📅 {location_name} — {day_label}\n\nپیش‌بینی‌ای برای این روز در دسترس نیست." + _updated_line(
            updated_at
        )

    lines = [
        f"{requested_line}📅 {location_name} — {day_label}",
        f"نمای کل روز (بر اساس {len(MODELS)} مدل — سرعت/گاست بر حسب KM/h)",
    ]
    for hour, models in records_by_hour.items():
        lines.append("")
        lines.append(f"🕐 {hour:02d}:00")
        lines.append("┄┄┄┄┄┄┄┄┄┄┄┄┄┄")
        if not models:
            lines.append("داده‌ای در دسترس نیست")
            continue
        for r in models:
            gust = f"{r['gust']}" if r["gust"] is not None else "—"
            lines.append("")
            lines.append(f"{r['theme']} {r['flag']} {r['model']} ({r['country']})")
            wl = weather_line(r.get("code"), r.get("cloud"), r.get("precip"), r.get("snow"), r.get("temp"))
            if wl:
                lines.append(wl)
            lines.append(f"💨 باد: {r['speed']} KM/h")
            lines.append(f"🌬 گاست: {gust} KM/h")
            lines.append(f"🧭 جهت: {degrees_to_compass(r['direction'])} {wind_arrow(r['direction'])}")

    lines.append("")
    lines.append(FOOTER_NOTE)
    lines.append(_updated_line(updated_at))
    return "\n".join(lines).rstrip()


def format_dual_estimate_daily(
    location_name: str,
    day_label: str,
    records_by_hour: dict[int, list[dict]],
    requested_by: str | None = None,
    updated_at=None,
) -> str:
    requested_line = f"🙋 به درخواست {requested_by}\n\n" if requested_by else ""
    if not records_by_hour or not any(records_by_hour.values()):
        return f"{requested_line}📅 {location_name} — {day_label}\n\nپیش‌بینی‌ای برای این روز در دسترس نیست." + _updated_line(
            updated_at
        )

    lines = [
        f"{requested_line}📅 {location_name} — {day_label}",
        "🎯 برآورد احتمالی — نمای کل روز (میانگین دقیق GFS و ICON)",
        "سرعت و گاست بر حسب KM/h",
    ]

    for hour, models in records_by_hour.items():
        best = compute_dual_estimate(models)
        lines.append("")
        lines.append(f"🕐 {hour:02d}:00")
        lines.append("┄┄┄┄┄┄┄┄┄┄┄┄┄┄")
        if not best:
            lines.append("داده‌ای در دسترس نیست")
            continue
        gust_str = f"{best['gust']}" if best["gust"] is not None else "—"
        used = " + ".join(best["models_used"])
        if best.get("weather_label"):
            lines.append(f"{best['weather_emoji']} وضعیت: {best['weather_label']}")
        lines.append(f"💨 باد: {best['speed']} KM/h")
        lines.append(f"🌬 گاست: {gust_str} KM/h")
        lines.append(f"🧭 جهت: {degrees_to_compass(best['direction'])} {wind_arrow(best['direction'])} • ({used})")

    lines.append("")
    lines.append(FOOTER_NOTE)
    lines.append(_updated_line(updated_at))
    return "\n".join(lines).rstrip()


def get_kermanshah_weather(api_key: str | None = None) -> dict:
    """دریافت وضعیت فعلی کرمانشاه از Windy برای استفاده‌های تک‌نقطه‌ای."""
    token = api_key or os.getenv("WINDY_API_KEY")
    if not token:
        return {"error": "کلید API مربوط به Windy یافت نشد (.env را بررسی کنید)."}

    try:
        response = httpx.post(
            WINDY_API_URL,
            json=_windy_payload("gfs", KERMANSHAH_LAT, KERMANSHAH_LON, token),
            timeout=20,
        )
        if response.status_code == 204:
            return {"error": "مدل انتخابی پارامترهای درخواست‌شده را پشتیبانی نمی‌کند."}
        if response.status_code != 200:
            return {"error": f"خطا در دریافت اطلاعات از Windy (کد وضعیت: {response.status_code})"}
        data = response.json()
    except Exception as exc:
        return {"error": f"خطا در ارتباط با سرور Windy: {exc}"}

    timestamps = data.get("ts") or []
    target = int(datetime.now(TEHRAN_TZ).timestamp() * 1000)
    temp_k = _interp(timestamps, data.get("temp-surface", []), target)
    wind_u = _interp(timestamps, data.get("wind_u-surface", []), target)
    wind_v = _interp(timestamps, data.get("wind_v-surface", []), target)
    if temp_k is None or wind_u is None or wind_v is None:
        return {"error": "داده‌ای برای زمان فعلی در دسترس نیست."}

    rh = _interp(timestamps, data.get("rh-surface", []), target)
    pressure = _interp(timestamps, data.get("pressure-surface", []), target)
    precip = _nearest(timestamps, data.get("past3hprecip-surface", []), target)
    cloud = _interp(timestamps, data.get("lclouds-surface", []), target)
    ptype = _nearest(timestamps, data.get("ptype-surface", []), target)
    temp_c = temp_k - 273.15 if temp_k > 100 else temp_k
    pressure_hpa = pressure / 100 if pressure is not None and pressure > 10000 else pressure

    return {
        "success": True,
        "city": "کرمانشاه",
        "temp": round(temp_c, 1),
        "rh": round(rh, 1) if rh is not None else None,
        "pressure": round(pressure_hpa, 1) if pressure_hpa is not None else None,
        "wind_speed": round(math.hypot(wind_u, wind_v), 1),
        "precip_3h": round(precip, 2) if precip is not None else 0,
        "ptype": PTYPE_DESCRIPTIONS.get(int(round(ptype)), "نامشخص") if ptype is not None else "نامشخص",
        "clouds": round(cloud, 1) if cloud is not None else None,
    }


def format_weather_message(weather_data: dict) -> str:
    """فرمت‌دهی خروجی وضعیت آب‌وهوا برای نمایش در ربات."""
    if "error" in weather_data or not weather_data.get("success"):
        return f"⚠️ **خطا:** {weather_data.get('error', 'دریافت اطلاعات ناموفق بود.')}"

    return (
        f"🌤 **وضعیت آب و هوای {weather_data['city']}**\n\n"
        f"🌡 **دما:** {weather_data['temp']} درجه سانتی‌گراد\n"
        f"💧 **رطوبت نسبی:** {weather_data['rh']}%\n"
        f"💨 **سرعت باد:** {weather_data['wind_speed']} متر بر ثانیه\n"
        f"📊 **فشار هوا:** {weather_data['pressure']} hPa\n"
        f"🌧 **وضعیت بارش:** {weather_data['ptype']}\n"
        f"☁️ **پوشش ابر:** {weather_data['clouds']}%\n"
    )
