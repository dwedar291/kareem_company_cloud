import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from supabase import create_client, Client

# إعدادات التسجيل لمتابعة الأخطاء
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# جلب البيانات من متغيرات البيئة وتنظيف أي مسافات زائدة
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.getenv("SUPABASE_KEY") or "").strip()

if not TELEGRAM_TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("تأكد من ضبط TELEGRAM_TOKEN و SUPABASE_URL و SUPABASE_KEY في Environment على Render")

# تشغيل الاتصال بقاعدة البيانات
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# أمر البداية (/start)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("أهلاً بك! أنا كريم، أرسل لي أي مهمة وسأسجلها مباشرة في جدول المهام.")


# الرد على أي رسالة وتسجيلها في جدول tasks داخل Supabase
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_text = update.message.text.strip()

    try:
        supabase.table("tasks").insert({
            "title": user_text,
            "status": "pending",
        }).execute()

        reply_text = f"تم استلام التوجيه التالي وتسجيله في جدول المهام:\n{user_text}"
    except Exception as e:
        logging.error(f"Supabase error: {e}")
        reply_text = f"حصلت مشكلة في تسجيل المهمة: {e}"

    await update.message.reply_text(reply_text)


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logging.info("Kareem bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
