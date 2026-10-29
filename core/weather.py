"""هسته‌ی مشترک گرفتن اطلاعات باد از Open-Meteo — هم تلگرام و هم بله از این استفاده می‌کنند.

نمایش پیش‌بینی به‌صورت برآورد احتمالی با تمرکز روی دو مدل اصلی (ECMWF و GFS) انجام می‌شه.
"""

import httpx
import math

from core.jalali import format_jalali_datetime

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# پنج ماهواره‌ی هواشناسی تجاری که به‌صورت رایگان در اختیار این ربات قرار گرفته
MODELS = {
    "ecmwf_ifs025": {"name": "ECMWF", "country": "اروپا", "flag": "🇪🇺", "theme": "🔷"},
    "gfs_seamless": {"name": "GFS", "country": "آمریکا", "flag": "🇺🇸", "theme": "🦅"},
    "icon_seamless": {"name": "ICON", "country": "آلمان", "flag": "🇩🇪", "theme": "⚙️"},
    "gem_seamless": {"name": "GEM", "country": "کانادا", "flag": "🇨🇦", "theme": "🍁"},
    "meteofrance_seamless": {"name": "Météo-France", "country": "فرانسه", "flag": "🇫🇷", "theme": "⚜️"},
}

COMPASS_POINTS = [
    "شمال", "شمال شرقی", "شرق", "جنوب شرقی",
    "جنوب", "جنوب غربی", "غرب", "شمال غربی",
]

# فلش‌ها جهتی که باد بهش می‌وزه رو نشون می‌دن (نه جهتی که ازش میاد) — مثل بادنما
WIND_ARROWS = ["⬆️", "↗️", "➡️", "↘️", "⬇️", "↙️", "⬅️", "↖️"]

FIXED_HOURS = [6, 8, 10, 12, 14, 16, 18]

FOOTER_NOTE = (
    "💼 این ماهواره‌ها تجاری هستن و به‌صورت رایگان در اختیار شما قرار گرفته‌اند."
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


def _extract_models(hourly: dict, idx: int) -> list[dict]:
    results = []
    for key, info in MODELS.items():
        speeds = hourly.get(f"wind_speed_10m_{key}")
        dirs = hourly.get(f"wind_direction_10m_{key}")
        gusts = hourly.get(f"wind_gusts_10m_{key}")
        if not speeds or speeds[idx] is None:
            continue
        cloud = _at(hourly, f"cloud_cover_{key}", idx)
        precip = _at(hourly, f"precipitation_{key}", idx)
        snow = _at(hourly, f"snowfall_{key}", idx)
        code = _at(hourly, f"weather_code_{key}", idx)
        temp = _at(hourly, f"temperature_2m_{key}", idx)
        results.append(
            {
                "model": info["name"],
                "country": info["country"],
                "flag": info["flag"],
                "theme": info["theme"],
                "speed": speeds[idx],
                "direction": dirs[idx],
                "gust": gusts[idx] if gusts else None,
                "cloud": cloud,
                "precip": precip,
                "snow": snow,
                "code": code,
                "temp": temp,
            }
        )
    return results


def _at(hourly: dict, field: str, idx: int):
    values = hourly.get(field)
    if not values or idx >= len(values):
        return None
    return values[idx]


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
    """برآورد بر پایه دو مدل اصلی ECMWF و GFS (میانگین دقیق هرگاه هر دو موجود باشن)."""
    if not models:
        return None

    valid = [m for m in models if m.get("speed") is not None]
    if not valid:
        return None

    main_list = [m for m in valid if m["model"] in ("ECMWF", "GFS")]

    if len(main_list) == 2:
        core = main_list
        method = "ecmwf_gfs_avg"
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
    """پیش‌بینی یک ساعت مشخص رو از هر پنج ماهواره می‌گیره. target_time: 'YYYY-MM-DDTHH:00'."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m,cloud_cover,precipitation,snowfall,weather_code,temperature_2m",
        "wind_speed_unit": "kmh",
        "timezone": "auto",
        "forecast_days": 3,
        "models": ",".join(MODELS.keys()),
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    hourly = data["hourly"]
    try:
        idx = hourly["time"].index(target_time)
    except ValueError:
        return []

    return _extract_models(hourly, idx)


async def get_multi_model_daily(
    lat: float, lon: float, date_str: str, hours: list[int] | None = None
) -> dict[int, list[dict]]:
    """پیش‌بینی چند ساعت مشخص از یک روز رو از هر پنج ماهواره، با یک درخواست می‌گیره."""
    if hours is None:
        hours = FIXED_HOURS

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m,cloud_cover,precipitation,snowfall,weather_code,temperature_2m",
        "wind_speed_unit": "kmh",
        "timezone": "auto",
        "forecast_days": 3,
        "models": ",".join(MODELS.keys()),
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    hourly = data["hourly"]
    result: dict[int, list[dict]] = {}
    for h in hours:
        target_time = f"{date_str}T{h:02d}:00"
        try:
            idx = hourly["time"].index(target_time)
        except ValueError:
            result[h] = []
            continue
        result[h] = _extract_models(hourly, idx)
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
            "هیچ‌کدوم از ماهواره‌ها پیش‌بینی‌ای برای این ساعت ندارن."
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
        "🎯 برآورد احتمالی (میانگین دقیق ECMWF و GFS)",
    ]
    if weather_row:
        lines.append(weather_row)
    lines += [
        f"💨 باد: {best['speed']} KM/h",
        f"🌬 گاست: {gust_str}",
        f"🧭 جهت: {degrees_to_compass(best['direction'])} {wind_arrow(best['direction'])}",
        f"📊 مبنا: {used}",
        "〰️〰️〰️〰️〰️",
        "💡 فقط ECMWF و GFS در نظر گرفته شدن (میانگین ساده و دقیق هر دو مدل).",
        FOOTER_NOTE,
        _updated_line(updated_at),
    ]
    return "\n".join(lines).rstrip()


def _updated_line(updated_at=None) -> str:
    if not updated_at:
        return ""
    return f"\n\n🕐 آخرین بروزرسانی پیش‌بینی از منبع (Open-Meteo): {format_jalali_datetime(updated_at)} به‌وقت تهران"


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
        f"نمای کل روز (بر اساس {len(MODELS)} ماهواره — سرعت/گاست بر حسب KM/h)",
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
        "🎯 برآورد احتمالی — نمای کل روز (میانگین دقیق ECMWF و GFS)",
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
    lines.append("💼 این ماهواره‌ها تجاری هستن و به‌صورت رایگان در اختیار شما قرار گرفته‌اند.")
    lines.append(_updated_line(updated_at))
    return "\n".join(lines).rstrip()
