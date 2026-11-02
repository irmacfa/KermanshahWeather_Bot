# Kermanshah Weather Bot

ربات پیش‌بینی باد و وضعیت هوا برای سایت‌های پرواز استان کرمانشاه، با پشتیبانی از تلگرام و بله.

## ساختار پروژه
- bots/telegram_bot.py: نقطه ورود ربات، فرمان‌ها، callbackها، پنل مدیریت و زمان‌بندی گزارش‌ها.
- core/weather.py: دریافت داده از Windy، پردازش مدل‌های GFS و ICON، محاسبات باد و قالب‌بندی گزارش.
- core/tracking.py: ذخیره تعامل کاربران، گروه‌ها، برنامه‌ها و حضورها در SQLite.
- core/sites.py: مختصات و جهت باد مناسب سایت‌های پرواز.
- core/jalali.py: تبدیل تاریخ میلادی به شمسی.
- tests/: تست‌های رگرسیون محاسبات و قالب‌بندی.
- .env.example: نمونه متغیرهای محیطی؛ اطلاعات واقعی فقط در فایل خصوصی سرور قرار گیرد.

## پیش‌نیازها
- Python 3.10 یا بالاتر
- دسترسی شبکه به Telegram/Bale API و Windy
- کلید API معتبر Windy و توکن ربات

## نصب و اجرا
```bash
git clone https://github.com/irmacfa/KermanshahWeather_Bot.git
cd KermanshahWeather_Bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

در فایل .env حداقل TELEGRAM_BOT_TOKEN و WINDY_API_KEY را تنظیم کنید. BOT_OWNER_ID برای پنل مدیریت، TELEGRAM_PROXY_URLS برای پروکسی‌های اختیاری و UNREGISTERED_USERS_CHAT_ID برای گروه فهرست ثبت‌نام‌نشده‌ها استفاده می‌شود.

اجرای دستی: python -m bots.telegram_bot

## تست
```bash
python -m unittest discover -s tests -v
python -m py_compile bots/telegram_bot.py core/*.py
```

## امنیت و عملیات
- فایل .env، دیتابیس SQLite، لاگ‌ها و محیط مجازی نباید وارد Git شوند.
- توکن ربات، کلید Windy و اعتبارنامه پروکسی را در Issue، Commit یا پیام عمومی قرار ندهید.
- تغییرات را ابتدا در Branch جداگانه تست و سپس از طریق Pull Request وارد main کنید.
- پیش از ارتقای نسخه روی سرور از bot_data.db و .env نسخه پشتیبان بگیرید.
- تغییرات دیتابیس باید با داده‌های قبلی سازگار باشند.