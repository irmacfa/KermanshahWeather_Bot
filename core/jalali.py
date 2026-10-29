"""تبدیل تاریخ میلادی (Gregorian) به شمسی (Jalali) — بدون نیاز به کتابخانه‌ی خارجی.

الگوریتم از jalaali-js برداشته شده و با موارد شناخته‌شده تست شده.
"""

import re

PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"

JALALI_MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]


def to_fa_digits(text) -> str:
    """تبدیل ارقام انگلیسی/دهدهی به ارقام فارسی."""
    return re.sub(r"[0-9]", lambda m: PERSIAN_DIGITS[int(m.group(0))], str(text))


def to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """تبدیل تاریخ میلادی به شمسی. خروجی: (سال، ماه، روز)."""
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy <= 1600:
        jy = 0
        gy = gy - 621
    else:
        jy = 979
        gy = gy - 1600
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        365 * gy
        + ((gy2 + 3) // 4)
        - ((gy2 + 99) // 100)
        + ((gy2 + 399) // 400)
        - 80
        + gd
        + g_d_m[gm - 1]
    )
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    jy += (days - 1) // 365 if days > 0 else 0
    if days > 365:
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + (days % 31)
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + ((days - 186) % 30)
    return jy, jm, jd


def format_jalali_date(year: int, month: int, day: int) -> str:
    """نمایش شمسی به‌صورت «۱۴۰۵/۰۶/۲۱» (با ارقام فارسی)."""
    return f"{to_fa_digits(year)}/{to_fa_digits(month)}/{to_fa_digits(day)}"


def format_jalali_datetime(dt, with_month_name: bool = False) -> str:
    """تبدیل یک datetime به رشته‌ی شمسی با ساعت: «۱۴۰۵/۰۶/۲۱ - ۱۱:۰۵».

    اگر with_month_name True باشد: «۱۴۰۵ شهریور ۲۱ - ۱۱:۰۵».
    """
    jy, jm, jd = to_jalali(dt.year, dt.month, dt.day)
    time_str = f"{dt:%H:%M}"
    if with_month_name:
        return f"{to_fa_digits(jy)} {JALALI_MONTHS[jm - 1]} {to_fa_digits(jd)} - {to_fa_digits(time_str)}"
    return f"{to_fa_digits(f'{jy:04d}')}/{to_fa_digits(f'{jm:02d}')}/{to_fa_digits(f'{jd:02d}')} - {to_fa_digits(time_str)}"