import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from supabase import create_client, Client

# إعدادات التسجيل لمتابعة الأخطاء
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# بيانات الاتصال الخاصة بك
TELEGRAM_TOKEN = "9166833763:AAGvM7gXmepGJwQPEXLYIag5MjuOPcof69s"
SUPABASE_URL = "https://dxpizqltrwdtsheqmuan.supabase.co"
SUPABASE_KEY = "Sb_secret_MCaLK2XKvrM0SmN86tXchg_Gw73H0C-"

# تشغيل الاتصال بقاعدة البيانات
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# أمر البداية (لما تكلم كريم أول مرة)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    welcome_message = (
        f"أهلاً بحضرتك يا باشمهندس {user_name}! 👔\n"
        "أنا **كريم**، المدير العام لشركتك السحابية.\n"
        "مستعد أدير الشغل، أوزع المهام، وأتابع مع فريق العمل من خلال تليجرام.\n\n"
        "أمرني، إيه المهمة الأولى اللي تحب نبدأ بيها اليوم؟"
    )
    await update.message.reply_text(welcome_message, parse_mode="Markdown")

# الرد على أي رسالة أو أمر توجيهي ترسله لكريم
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    
    # تسجيل المهمة مباشرة في قاعدة بيانات Supabase (جدول tasks)
    try:
        supabase.table("tasks").insert({
            "title": user_text,
            "status": "pending"
        }).execute()
        
        reply_text = f"✅ يا فندم، استلمت التوجيه التالي وسجلته في جدول المهام:\n\"{user_text}\"\n\nجارِ دراسته وتوزيعه على الموظفين فوراً!"
    except Exception as e:
        reply_text = f"⚠️ حصلت مشكلة صغيرة في تسجيل المهمة بقاعدة البيانات، بس ولا يهمك يا باشا: {e}"

    await update.message.reply_text(reply_text)

if __name__ == '__main__':
    # بناء وتفعيل بوت تليجرام
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # ربط الأوامر والرسائل بالوظائف البرمجية
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    print("🤖 نظام 'كريم' يعمل الآن وجاهز لاستقبال الأوامر عبر تليجرام...")
    app.run_polling()