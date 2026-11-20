import os
import json
import logging
import threading
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from supabase import create_client, Client
from google import genai
from google.genai import types

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

DEFAULT_MODEL = "gemini-3.5-flash-lite"
KARIM_ID = "380ebfbd-0c61-464e-8ff7-a1380bef8b03"
DEFAULT_MAKE_WEBHOOK_URL = "https://hook.eu1.make.com/lcbffny8nz5471m3wbng26ye7nb6va6b"

TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.getenv("SUPABASE_KEY") or "").strip()
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
KARIM_AGENT_ID = (os.getenv("KARIM_AGENT_ID") or KARIM_ID).strip()
MAKE_WEBHOOK_URL = (os.getenv("MAKE_WEBHOOK_URL") or DEFAULT_MAKE_WEBHOOK_URL).strip()

if not TELEGRAM_TOKEN or not SUPABASE_URL or not SUPABASE_KEY or not GEMINI_API_KEY:
    raise ValueError("تأكد من ضبط TELEGRAM_TOKEN و SUPABASE_URL و SUPABASE_KEY و GEMINI_API_KEY في Render")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

sys_config = types.GenerateContentConfig(
    system_instruction=(
        "أنت كريم، مساعد ذكي، ودود، وبتتكلم بأسلوب طبيعي وبسيط زي البشر في الشات. "
        "جاوب مباشرة وبساطة من غير تكلف أو ألقاب رسمية مبالغ فيها، "
        "ولو حد سألك عامل إيه رد عادي وبود."
    )
)


def get_chat_history(chat_id: str, limit: int = 20):
    res = (
        supabase.table("chat_messages")
        .select("role, content")
        .eq("chat_id", str(chat_id))
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    rows = res.data or []
    rows.reverse()
    return rows


def save_message(chat_id: str, role: str, content: str):
    supabase.table("chat_messages").insert({
        "chat_id": str(chat_id),
        "role": role,
        "content": content,
    }).execute()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logging.info(f"Health server running on port {port}")


def parse_json_response(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    return json.loads(raw)


def resolve_karim_agent(agents: list[dict]) -> dict | None:
    if not agents:
        return None
    by_id = next((a for a in agents if str(a.get("id")) == KARIM_AGENT_ID), None)
    if by_id:
        return by_id
    by_code = next((a for a in agents if (a.get("code") or "").lower() in ("kareem", "karim", "manager")), None)
    if by_code:
        return by_code
    return agents[0]


def get_active_agents_with_tools() -> tuple[list[dict], dict[str, dict]]:
    agents_res = supabase.table("agents").select("*").eq("is_active", True).execute()
    agents = agents_res.data or []

    tools_res = supabase.table("tools").select("*").eq("is_enabled", True).execute()
    tools_rows = tools_res.data or []
    tools_by_id = {t["id"]: t for t in tools_rows}
    tools_by_code = {t["code"]: t for t in tools_rows if t.get("code")}

    links_res = supabase.table("agent_tools").select("*").eq("agent_id", KARIM_AGENT_ID).execute()
    links = links_res.data or []

    agent_tool_codes: dict[str, list[str]] = {}
    for link in links:
        tool = tools_by_id.get(link.get("tool_id"))
        if tool and tool.get("code"):
            agent_tool_codes.setdefault(link["agent_id"], []).append(tool["code"])

    enriched = []
    for ag in agents:
        ag_id = ag.get("id")
        enriched.append({
            "id": ag_id,
            "code": ag.get("code") or "karim",
            "name": ag.get("name") or "كريم",
            "role": ag.get("role") or "المساعد الشخصي",
            "system_prompt": ag.get("system_prompt") or "",
            "model_name": ag.get("model_name") or DEFAULT_MODEL,
            "can_hire": bool(ag.get("can_hire")),
            "can_evolve": bool(ag.get("can_evolve")),
            "version": ag.get("version") or 1,
            "tools": agent_tool_codes.get(ag_id, []),
        })

    if not enriched:
        enriched.append({
            "id": KARIM_AGENT_ID,
            "code": "karim",
            "name": "كريم",
            "role": "المساعد الشخصي",
            "system_prompt": "",
            "model_name": DEFAULT_MODEL,
            "can_hire": False,
            "can_evolve": False,
            "version": 1,
            "tools": [t["code"] for t in tools_rows if t.get("code")],
        })

    return enriched, tools_by_code


def log_evolution(actor_code: str, action_type: str, target_code: str, details: dict):
    supabase.table("evolution_log").insert({
        "actor_agent_code": actor_code,
        "action_type": action_type,
        "target_code": target_code,
        "details": details,
    }).execute()


def tool_create_task(
    title: str,
    assigned_agent_code: str = "karim",
    priority: str = "normal",
    notes: str = "",
    chat_id: str | None = None,
    user_prompt: str | None = None,
) -> dict:
    payload_notes = (notes or "").strip()
    extra_meta = {}
    if chat_id:
        extra_meta["chat_id"] = str(chat_id)
    if user_prompt:
        extra_meta["prompt"] = user_prompt

    res = supabase.table("tasks").insert({
        "title": title.strip(),
        "status": "pending",
        "assigned_agent_code": (assigned_agent_code or "karim").strip(),
        "priority": (priority or "normal").strip(),
        "notes": payload_notes,
    }).execute()
    task_row = (res.data or [{}])[0]
    if extra_meta:
        task_row["_meta"] = extra_meta
    return {"ok": True, "task": task_row}


def tool_http_request(
    method: str,
    url: str,
    params: dict | None = None,
    body: dict | None = None,
    extra_headers: dict | None = None,
) -> dict:
    clean_method = (method or "GET").upper().strip()
    clean_url = (url or "").strip()
    if not clean_url.startswith(("http://", "https://")):
        return {"ok": False, "error": "الرابط يجب أن يبدأ بـ http:// أو https://"}

    if params:
        sep = "&" if "?" in clean_url else "?"
        clean_url = f"{clean_url}{sep}{urllib.parse.urlencode(params)}"

    data_bytes = None
    headers = {"User-Agent": "KareemBot/1.0"}
    if extra_headers and isinstance(extra_headers, dict):
        for k, v in extra_headers.items():
            if k and v is not None:
                headers[str(k)] = str(v)

    if clean_method in ("POST", "PUT", "PATCH") and body is not None:
        data_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(clean_url, data=data_bytes, headers=headers, method=clean_method)
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw_body = resp.read().decode("utf-8", errors="replace")
        return {"ok": True, "status": resp.status, "response": raw_body[:2500]}


def send_to_make_webhook(payload: dict, webhook_url: str | None = None) -> dict:
    target_url = (webhook_url or MAKE_WEBHOOK_URL or "").strip()
    if not target_url:
        return {"ok": False, "error": "رابط Make Webhook غير مضبوط"}
    try:
        return tool_http_request(method="POST", url=target_url, body=payload)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run_configured_tool(tool_row: dict, task: dict) -> str:
    tool_code = tool_row.get("code") or "tool"
    config = tool_row.get("config") or {}
    if not isinstance(config, dict):
        config = {}

    target_url = (config.get("script_url") or config.get("url") or config.get("endpoint") or "").strip()
    if not target_url and tool_code in ("make_webhook", "make_video", "make"):
        target_url = MAKE_WEBHOOK_URL

    method = (config.get("method") or ("POST" if config.get("body") or target_url == MAKE_WEBHOOK_URL else "GET")).upper().strip()
    extra_headers = config.get("headers") if isinstance(config.get("headers"), dict) else None

    params = dict(config.get("params") or {}) if isinstance(config.get("params"), dict) else {}
    if task.get("title"):
        params.setdefault("task_title", task.get("title"))
    if task.get("notes"):
        params.setdefault("task_notes", task.get("notes"))

    body = None
    if method in ("POST", "PUT", "PATCH"):
        body = dict(config.get("body") or {}) if isinstance(config.get("body"), dict) else {}
        body.setdefault("task_id", task.get("id"))
        body.setdefault("task_title", task.get("title"))
        body.setdefault("task_notes", task.get("notes"))
        body.setdefault("priority", task.get("priority") or "normal")
        meta = task.get("_meta") if isinstance(task.get("_meta"), dict) else {}
        if meta.get("prompt"):
            body.setdefault("prompt", meta.get("prompt"))
        if meta.get("chat_id"):
            body.setdefault("chat_id", meta.get("chat_id"))

    if target_url:
        try:
            res = tool_http_request(
                method=method,
                url=target_url,
                params=params if method == "GET" else None,
                body=body if method != "GET" else None,
                extra_headers=extra_headers,
            )
            if res.get("ok"):
                return f"[{tool_code}] (HTTP {res.get('status')}): {res.get('response')}"
            return f"[{tool_code}] فشل التنفيذ: {res.get('error')}"
        except Exception as e:
            return f"[{tool_code}] خطأ أثناء الاتصال بالرابط ({target_url}): {e}"

    if config.get("developer_token"):
        token_preview = str(config.get("developer_token"))[:5]
        return f"[{tool_code}] يوجد developer_token ({token_preview}...) لكن لا يوجد رابط تنفيذ (url / script_url) مضبوط في إعدادات الأداة."

    return f"[{tool_code}] الأداة مسجلة لكن إعداداتها (config) لا تحتوي على رابط تنفيذ (url أو script_url) بعد."


def tool_register_tool(
    actor_code: str,
    code: str,
    name: str,
    description: str,
    config: dict | None = None,
) -> dict:
    clean_code = (code or "").strip().lower().replace(" ", "_")
    if not clean_code:
        return {"ok": False, "error": "كود الأداة مطلوب"}

    clean_config = dict(config) if isinstance(config, dict) else {"type": "http", "method": "POST", "url": MAKE_WEBHOOK_URL}
    if not clean_config.get("url") and not clean_config.get("script_url") and not clean_config.get("endpoint"):
        clean_config["type"] = "http"
        clean_config["method"] = "POST"
        clean_config["url"] = MAKE_WEBHOOK_URL

    existing = supabase.table("tools").select("id, code").eq("code", clean_code).execute().data or []
    if existing:
        tool_id = existing[0]["id"]
        supabase.table("tools").update({
            "name": name.strip(),
            "description": description.strip(),
            "is_enabled": True,
            "config": clean_config,
        }).eq("id", tool_id).execute()
    else:
        inserted = supabase.table("tools").insert({
            "code": clean_code,
            "name": name.strip(),
            "description": description.strip(),
            "is_enabled": True,
            "config": clean_config,
        }).execute()
        tool_id = (inserted.data or [{}])[0].get("id")

    if tool_id:
        links = (
            supabase.table("agent_tools")
            .select("id")
            .eq("agent_id", KARIM_AGENT_ID)
            .eq("tool_id", tool_id)
            .execute()
            .data
            or []
        )
        if not links:
            supabase.table("agent_tools").insert({"agent_id": KARIM_AGENT_ID, "tool_id": tool_id}).execute()

    log_evolution(
        actor_code=actor_code,
        action_type="register_tool",
        target_code=clean_code,
        details={"name": name, "assigned_to_agent_id": KARIM_AGENT_ID, "config": clean_config},
    )
    return {"ok": True, "tool_code": clean_code, "assigned_to": "karim"}


def ensure_default_make_tool_registered():
    if not MAKE_WEBHOOK_URL:
        return
    try:
        tool_register_tool(
            actor_code="system",
            code="make_webhook",
            name="Make Automation Webhook",
            description="إرسال المهام والطلبات إلى سيناريو Make لتنفيذ الأتمتة وتوليد المحتوى",
            config={"type": "http", "method": "POST", "url": MAKE_WEBHOOK_URL},
        )
    except Exception as e:
        logging.warning(f"Could not auto-register make_webhook tool: {e}")


def execute_pending_tasks_for_karim(
    karim: dict,
    tools_by_code: dict[str, dict],
    limit: int = 3,
    extra_task_meta: dict | None = None,
) -> list[str]:
    pending_res = (
        supabase.table("tasks")
        .select("*")
        .eq("status", "pending")
        .order("id", desc=False)
        .limit(limit)
        .execute()
    )
    pending_tasks = pending_res.data or []
    summaries = []

    for task in pending_tasks:
        if extra_task_meta:
            task["_meta"] = extra_task_meta

        tool_outputs = []
        for t_code in karim.get("tools") or []:
            if t_code in ("create_task", "register_tool", "assign_tool", "hire_agent", "update_agent_prompt"):
                continue
            tool_row = tools_by_code.get(t_code)
            if tool_row:
                tool_outputs.append(run_configured_tool(tool_row, task))

        if not tool_outputs and MAKE_WEBHOOK_URL:
            mk_res = send_to_make_webhook({
                "task_id": task.get("id"),
                "task_title": task.get("title"),
                "task_notes": task.get("notes") or "",
                "priority": task.get("priority") or "normal",
                **(extra_task_meta or {}),
            })
            if mk_res.get("ok"):
                tool_outputs.append(f"[make_webhook] (HTTP {mk_res.get('status')}): {mk_res.get('response')}")
            else:
                tool_outputs.append(f"[make_webhook] فشل الإرسال: {mk_res.get('error')}")

        tools_context = "\n".join(tool_outputs) if tool_outputs else "لا توجد نتائج أدوات خارجية لهذه المهمة."

        prompt = f"""
{karim.get("system_prompt", "")}

مهمة مسندة إليك:
العنوان: {task.get("title")}
الملاحظات: {task.get("notes") or ""}

نتائج تنفيذ الأدوات المرتبطة بك:
{tools_context}

نفّذ المهمة بأفضل صورة عملية ومختصرة واكتب النتيجة النهائية بناءً على البيانات الفعلية بالأعلى، ولو في أداة ناقصها إعداد وضح ذلك بوضوح.
"""
        res = gemini_client.models.generate_content(
            model=karim.get("model_name") or DEFAULT_MODEL,
            contents=prompt,
            config=sys_config,
        )
        output_text = (res.text or "تم الإنجاز.").strip()
        supabase.table("tasks").update({
            "status": "completed",
            "notes": f"{task.get('notes') or ''}\n[نتيجة كريم]: {output_text}".strip(),
        }).eq("id", task["id"]).execute()
        summaries.append(f"كريم: {output_text}")

    return summaries


def process_user_message(user_text: str, chat_id: str | None = None) -> str:
    ensure_default_make_tool_registered()
    agents, tools_by_code = get_active_agents_with_tools()
    karim = resolve_karim_agent(agents)

    if not karim:
        return "تنبيه: حساب كريم غير متاح في قاعدة البيانات حالياً."

    recent_history = get_chat_history(chat_id, limit=6) if chat_id else []
    history_text = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in recent_history)

    prompt = f"""
{karim.get("system_prompt", "")}

أنت "كريم"، المساعد الشخصي الوحيد في النظام. تكلم بأسلوب طبيعي، ودود، ومباشر بدون تكلف أو ألقاب رسمية زائدة.
الأدوات المتاحة لك حالياً: {json.dumps(karim.get("tools") or [], ensure_ascii=False)}
رابط Make Webhook المتصل بالنظام مضبوط وجاهز لتنفيذ المهام والأتمتة.

آخر رسائل المحادثة:
{history_text or "لا يوجد سجل سابق"}

رسالة المستخدم الحالية:
"{user_text}"

حدد القرار المناسب وأعد JSON فقط بدون أي شرح خارجي بالشكل التالي:
{{
  "decision": "chat" أو "create_task" أو "build_tool",
  "task": {{
    "title": "عنوان المهمة إذا كان القرار create_task",
    "priority": "low أو normal أو high",
    "notes": "ملاحظات مختصرة"
  }},
  "tool": {{
    "code": "كود_الأداة_بالإنجليزية",
    "name": "اسم الأداة",
    "description": "وصف الأداة",
    "config": {{"type": "http", "method": "POST", "url": "{MAKE_WEBHOOK_URL}"}}
  }},
  "reply": "الرد النهائي الطبيعي والمباشر بالعربية بدون رسميات زائدة"
}}
"""
    response = gemini_client.models.generate_content(
        model=karim.get("model_name") or DEFAULT_MODEL,
        contents=prompt,
        config=sys_config,
    )
    data = parse_json_response(response.text or "{}")
    decision = (data.get("decision") or "chat").strip()
    extra_notes = []

    if decision == "create_task":
        task_info = data.get("task") or {}
        if task_info.get("title"):
            tool_create_task(
                title=task_info["title"],
                assigned_agent_code="karim",
                priority=task_info.get("priority") or "normal",
                notes=task_info.get("notes") or "",
                chat_id=chat_id,
                user_prompt=user_text,
            )
            updated_agents, updated_tools_by_code = get_active_agents_with_tools()
            updated_karim = resolve_karim_agent(updated_agents) or karim
            team_outputs = execute_pending_tasks_for_karim(
                updated_karim,
                updated_tools_by_code,
                limit=2,
                extra_task_meta={"chat_id": chat_id, "prompt": user_text},
            )
            extra_notes.extend(team_outputs)

    elif decision == "build_tool":
        tool_info = data.get("tool") or {}
        if tool_info.get("code") and tool_info.get("name"):
            r = tool_register_tool(
                actor_code="karim",
                code=tool_info["code"],
                name=tool_info["name"],
                description=tool_info.get("description") or tool_info["name"],
                config=tool_info.get("config") or {"type": "http", "method": "POST", "url": MAKE_WEBHOOK_URL},
            )
            if r.get("ok"):
                extra_notes.append(f"تم تسجيل الأداة {r['tool_code']} وربطها بكريم مباشرة")

    reply_text = (data.get("reply") or "تمام، أنا معاك.").strip()
    if extra_notes:
        reply_text += "\n\n" + "\n".join(f"• {item}" for item in extra_notes)
    return reply_text


async def analyze_media_with_gemini(
    file_bytes: bytes,
    mime_type: str,
    prompt_text: str,
    model_name: str = DEFAULT_MODEL,
) -> str:
    media_part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
    response = gemini_client.models.generate_content(
        model=model_name,
        contents=[media_part, prompt_text],
        config=sys_config,
    )
    return (response.text or "").strip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً بك! أنا كريم، جاهز أساعدك. قولّي محتاج إيه نبدأ بيه."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_text = update.message.text.strip()
    chat_id = str(update.message.chat_id)
    try:
        save_message(chat_id, "user", user_text)
        reply_text = process_user_message(user_text, chat_id=chat_id)
        save_message(chat_id, "assistant", reply_text)
    except Exception as e:
        logging.error(f"Error in handle message: {e}")
        reply_text = "حصلت مشكلة أثناء تنفيذ القرار، تأكد من إعدادات المفاتيح في Render"

    await update.message.reply_text(reply_text)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return

    chat_id = str(update.message.chat_id)
    caption = (update.message.caption or "").strip()

    try:
        largest_photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(largest_photo.file_id)
        photo_bytes = bytes(await tg_file.download_as_bytearray())

        vision_prompt = (
            f"المستخدم بعت الصورة دي وكتب معاها: '{caption}'. افحص الصورة بدقة واشرح محتواها بالعربية بوضوح."
            if caption
            else "المستخدم بعت الصورة دي بدون تعليق. افحص الصورة بدقة واشرح محتواها وأهم التفاصيل اللي فيها بالعربية."
        )
        photo_description = await analyze_media_with_gemini(
            file_bytes=photo_bytes,
            mime_type="image/jpeg",
            prompt_text=vision_prompt,
        )

        combined_user_input = (
            f"[أرسل المستخدم صورة] وصف/محتوى الصورة: {photo_description}"
            + (f" | تعليق المستخدم: {caption}" if caption else "")
        )

        save_message(chat_id, "user", combined_user_input)
        reply_text = process_user_message(combined_user_input, chat_id=chat_id)
        save_message(chat_id, "assistant", reply_text)
    except Exception as e:
        logging.error(f"Error in handle_photo: {e}")
        reply_text = "حصلت مشكلة وأنا بشوف الصورة، جرّب تبعتها تاني."

    await update.message.reply_text(reply_text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.voice:
        return

    chat_id = str(update.message.chat_id)

    try:
        tg_file = await context.bot.get_file(update.message.voice.file_id)
        voice_bytes = bytes(await tg_file.download_as_bytearray())

        voice_prompt = (
            "اسمع الرسالة الصوتية دي كويس، فرّغ الكلام اللي فيها بدقة، "
            "واكتب التفريغ النصي والملخص أو المقصود من الكلام بالعربية."
        )
        transcript_text = await analyze_media_with_gemini(
            file_bytes=voice_bytes,
            mime_type=update.message.voice.mime_type or "audio/ogg",
            prompt_text=voice_prompt,
        )

        combined_user_input = f"[أرسل المستخدم رسالة صوتية]: {transcript_text}"

        save_message(chat_id, "user", combined_user_input)
        reply_text = process_user_message(combined_user_input, chat_id=chat_id)
        save_message(chat_id, "assistant", reply_text)
    except Exception as e:
        logging.error(f"Error in handle_voice: {e}")
        reply_text = "حصلت مشكلة وأنا بسمع الريكورد، جرّب تبعته تاني."

    await update.message.reply_text(reply_text)


def main():
    start_health_server()
    ensure_default_make_tool_registered()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    logging.info("Kareem bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
