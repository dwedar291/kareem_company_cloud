import os
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from supabase import create_client, Client
from google import genai

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.getenv("SUPABASE_KEY") or "").strip()
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()

if not TELEGRAM_TOKEN or not SUPABASE_URL or not SUPABASE_KEY or not GEMINI_API_KEY:
    raise ValueError("تأكد من ضبط TELEGRAM_TOKEN و SUPABASE_URL و SUPABASE_KEY و GEMINI_API_KEY في Render")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)


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
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    return json.loads(raw)


def get_active_agents_with_tools() -> list[dict]:
    agents_res = supabase.table("agents").select("*").eq("is_active", True).execute()
    agents = agents_res.data or []

    tools_res = supabase.table("tools").select("*").eq("is_enabled", True).execute()
    tools_by_id = {t["id"]: t for t in (tools_res.data or [])}

    links_res = supabase.table("agent_tools").select("*").execute()
    links = links_res.data or []

    agent_tool_codes: dict[str, list[str]] = {}
    for link in links:
        tool = tools_by_id.get(link.get("tool_id"))
        if tool:
            agent_tool_codes.setdefault(link["agent_id"], []).append(tool["code"])

    enriched = []
    for ag in agents:
        enriched.append({
            "id": ag.get("id"),
            "code": ag.get("code"),
            "name": ag.get("name"),
            "role": ag.get("role"),
            "system_prompt": ag.get("system_prompt") or "",
            "model_name": ag.get("model_name") or "gemini-3.5-flash-lite",
            "can_hire": bool(ag.get("can_hire")),
            "can_evolve": bool(ag.get("can_evolve")),
            "version": ag.get("version") or 1,
            "tools": agent_tool_codes.get(ag.get("id"), []),
        })
    return enriched


def log_evolution(actor_code: str, action_type: str, target_code: str, details: dict):
    supabase.table("evolution_log").insert({
        "actor_agent_code": actor_code,
        "action_type": action_type,
        "target_code": target_code,
        "details": details,
    }).execute()


def tool_create_task(title: str, assigned_agent_code: str = "manager", priority: str = "normal", notes: str = "") -> dict:
    res = supabase.table("tasks").insert({
        "title": title.strip(),
        "status": "pending",
        "assigned_agent_code": (assigned_agent_code or "manager").strip(),
        "priority": (priority or "normal").strip(),
        "notes": (notes or "").strip(),
    }).execute()
    return {"ok": True, "task": (res.data or [{}])[0]}


def tool_hire_agent(
    actor_code: str,
    code: str,
    name: str,
    role: str,
    system_prompt: str,
    tool_codes: list[str] | None = None,
    can_hire: bool = False,
    can_evolve: bool = False,
) -> dict:
    clean_code = code.strip().lower().replace(" ", "_")
    existing = supabase.table("agents").select("id, code").eq("code", clean_code).execute().data or []
    if existing:
        return {"ok": False, "error": f"الموظف {clean_code} موجود بالفعل"}

    inserted = supabase.table("agents").insert({
        "code": clean_code,
        "name": name.strip(),
        "role": role.strip(),
        "system_prompt": system_prompt.strip(),
        "model_name": "gemini-3.5-flash-lite",
        "can_hire": bool(can_hire),
        "can_evolve": bool(can_evolve),
        "is_active": True,
        "version": 1,
        "created_by": actor_code,
    }).execute()

    new_agent = (inserted.data or [{}])[0]
    new_agent_id = new_agent.get("id")

    assigned_tools = []
    if new_agent_id and tool_codes:
        valid_tools = supabase.table("tools").select("id, code").in_("code", tool_codes).eq("is_enabled", True).execute().data or []
        for vt in valid_tools:
            supabase.table("agent_tools").insert({
                "agent_id": new_agent_id,
                "tool_id": vt["id"],
            }).execute()
            assigned_tools.append(vt["code"])

    log_evolution(
        actor_code=actor_code,
        action_type="hire_agent",
        target_code=clean_code,
        details={"name": name, "role": role, "tools": assigned_tools},
    )
    return {"ok": True, "agent": new_agent, "assigned_tools": assigned_tools}


def tool_update_agent_prompt(actor_code: str, target_code: str, new_system_prompt: str, reason: str = "") -> dict:
    rows = supabase.table("agents").select("*").eq("code", target_code.strip()).execute().data or []
    if not rows:
        return {"ok": False, "error": f"لم يتم العثور على الموظف {target_code}"}

    current = rows[0]
    next_version = int(current.get("version") or 1) + 1

    updated = supabase.table("agents").update({
        "system_prompt": new_system_prompt.strip(),
        "version": next_version,
        "updated_at": "now()",
    }).eq("id", current["id"]).execute()

    log_evolution(
        actor_code=actor_code,
        action_type="update_prompt",
        target_code=target_code.strip(),
        details={"new_version": next_version, "reason": reason},
    )
    return {"ok": True, "agent": (updated.data or [{}])[0], "version": next_version}


def tool_assign_tool(actor_code: str, target_agent_code: str, tool_code: str) -> dict:
    ag_rows = supabase.table("agents").select("id, code").eq("code", target_agent_code.strip()).execute().data or []
    tl_rows = supabase.table("tools").select("id, code").eq("code", tool_code.strip()).eq("is_enabled", True).execute().data or []

    if not ag_rows:
        return {"ok": False, "error": f"الموظف {target_agent_code} غير موجود"}
    if not tl_rows:
        return {"ok": False, "error": f"الأداة {tool_code} غير موجودة أو غير مفعلة"}

    agent_id = ag_rows[0]["id"]
    tool_id = tl_rows[0]["id"]

    existing = (
        supabase.table("agent_tools")
        .select("id")
        .eq("agent_id", agent_id)
        .eq("tool_id", tool_id)
        .execute()
        .data
        or []
    )
    if not existing:
        supabase.table("agent_tools").insert({
            "agent_id": agent_id,
            "tool_id": tool_id,
        }).execute()

    log_evolution(
        actor_code=actor_code,
        action_type="assign_tool",
        target_code=target_agent_code.strip(),
        details={"tool_code": tool_code.strip()},
    )
    return {"ok": True, "agent_code": target_agent_code.strip(), "tool_code": tool_code.strip()}


def run_hr_execution(hr_agent: dict, hr_instruction: str, available_tool_codes: list[str]) -> list[dict]:
    prompt = f"""
{hr_agent.get("system_prompt", "")}

أنت تنفذ طلباً صادراً من المدير العام لتطوير الفريق أو الأدوات تلقائياً.
الأدوات المتاحة لك حالياً: {json.dumps(available_tool_codes, ensure_ascii=False)}

الطلب من المدير:
"{hr_instruction}"

أعد JSON فقط بدون أي نص خارجه بالشكل التالي:
{{
  "actions": [
    {{
      "action": "hire_agent" أو "update_prompt" أو "assign_tool",
      "code": "كود الموظف الجديد بالإنجليزية بحروف صغيرة (مثال: sales_agent)",
      "name": "اسم الموظف الجديد",
      "role": "دور الموظف الجديد",
      "system_prompt": "تعليمات واضحة للموظف الجديد",
      "tools": ["create_task"],
      "target_code": "كود الموظف المراد تطويره في حالة update_prompt أو assign_tool",
      "new_system_prompt": "التعليمات المحدثة في حالة update_prompt",
      "tool_code": "كود الأداة في حالة assign_tool",
      "reason": "سبب التطوير أو التعيين"
    }}
  ],
  "hr_summary": "ملخص مختصر لما نفذه الـ HR"
}}
"""
    res = gemini_client.models.generate_content(
        model=hr_agent.get("model_name") or "gemini-3.5-flash-lite",
        contents=prompt,
    )
    parsed = parse_json_response(res.text or "{}")
    results = []

    for act in parsed.get("actions") or []:
        kind = (act.get("action") or "").strip()
        if kind == "hire_agent" and "hire_agent" in available_tool_codes:
            r = tool_hire_agent(
                actor_code="hr",
                code=act.get("code") or "specialist_agent",
                name=act.get("name") or "موظف متخصص",
                role=act.get("role") or "متابعة المهام المتخصصة",
                system_prompt=act.get("system_prompt") or "أنت موظف متخصص تنفذ المهام المسندة إليك بدقة.",
                tool_codes=act.get("tools") or ["create_task"],
            )
            results.append(r)
        elif kind == "update_prompt" and "update_agent_prompt" in available_tool_codes:
            r = tool_update_agent_prompt(
                actor_code="hr",
                target_code=act.get("target_code") or "manager",
                new_system_prompt=act.get("new_system_prompt") or "",
                reason=act.get("reason") or hr_instruction,
            )
            results.append(r)
        elif kind == "assign_tool" and "assign_tool" in available_tool_codes:
            r = tool_assign_tool(
                actor_code="hr",
                target_agent_code=act.get("target_code") or "manager",
                tool_code=act.get("tool_code") or "create_task",
            )
            results.append(r)

    return results


def process_user_message(user_text: str) -> str:
    agents = get_active_agents_with_tools()
    manager = next((a for a in agents if a["code"] == "manager"), None)
    hr = next((a for a in agents if a["code"] == "hr"), None)

    if not manager:
        return "تنبيه: وكيل المدير (manager) غير مفعل في قاعدة البيانات حالياً."

    agents_summary = [
        {
            "code": a["code"],
            "name": a["name"],
            "role": a["role"],
            "tools": a["tools"],
        }
        for a in agents
    ]

    prompt = f"""
{manager.get("system_prompt", "")}

الفريق الحالي في قاعدة البيانات:
{json.dumps(agents_summary, ensure_ascii=False, indent=2)}

رسالة المستخدم:
"{user_text}"

حدد القرار المناسب وأعد JSON فقط بدون أي شرح خارجي بالشكل التالي:
{{
  "decision": "chat" أو "create_task" أو "hire_or_evolve",
  "task": {{
    "title": "عنوان المهمة إذا كان القرار create_task",
    "assigned_agent_code": "كود أنسب موظف من الفريق الحالي أو manager",
    "priority": "low أو normal أو high",
    "notes": "ملاحظات مختصرة"
  }},
  "hr_instruction": "تعليمات للـ HR إذا كان القرار hire_or_evolve لتعيين موظف جديد أو تطوير موظف/أداة",
  "reply": "الرد النهائي الواضح والمباشر للمستخدم بالعربية"
}}
"""
    response = gemini_client.models.generate_content(
        model=manager.get("model_name") or "gemini-3.5-flash-lite",
        contents=prompt,
    )
    data = parse_json_response(response.text or "{}")
    decision = (data.get("decision") or "chat").strip()
    extra_notes = []

    if decision == "create_task" and "create_task" in manager.get("tools", []):
        task_info = data.get("task") or {}
        if task_info.get("title"):
            tool_create_task(
                title=task_info["title"],
                assigned_agent_code=task_info.get("assigned_agent_code") or "manager",
                priority=task_info.get("priority") or "normal",
                notes=task_info.get("notes") or "",
            )

    elif decision == "hire_or_evolve" and hr:
        hr_instruction = (data.get("hr_instruction") or user_text).strip()
        hr_results = run_hr_execution(hr, hr_instruction, hr.get("tools") or [])
        for r in hr_results:
            if r.get("ok") and "agent" in r and "assigned_tools" in r:
                ag_new = r["agent"]
                extra_notes.append(f"تم تعيين موظف جديد: {ag_new.get('name')} ({ag_new.get('code')})")
            elif r.get("ok") and "version" in r:
                extra_notes.append(f"تم تحديث تعليمات الموظف إلى النسخة v{r.get('version')}")
            elif r.get("ok") and "tool_code" in r:
                extra_notes.append(f"تم ربط الأداة {r.get('tool_code')} بالموظف {r.get('agent_code')}")

    reply_text = (data.get("reply") or "تم استلام رسالتك.").strip()
    if extra_notes:
        reply_text += "\n\n" + "\n".join(f"• {item}" for item in extra_notes)
    return reply_text


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً بك! أنا كريم، والنظام شغال الآن بالمدير العام والـ HR من قاعدة البيانات مباشرة. أرسل توجيهاتك أو مهامك أو طلبات تطوير الفريق."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_text = update.message.text.strip()
    try:
        reply_text = process_user_message(user_text)
    except Exception as e:
        logging.error(f"Error in handle_message: {e}")
        reply_text = "حصلت مشكلة أثناء تنفيذ القرار، تأكد من إعدادات المفاتيح في Render وجرّب مرة أخرى."

    await update.message.reply_text(reply_text)


def main():
    start_health_server()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logging.info("Kareem office bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
