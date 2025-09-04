# assistente-aula-infantil/server.py
# Assistente Educacional — Onboarding guiado + Fluxo de Aula (MCQ) + Check-in diário
# Flask + Twilio. Webhook: POST /bot | Cron: GET /admin/cron
import os
import re
import random
from typing import Any, Dict, Optional, Tuple, List
from datetime import datetime, timedelta, time as dtime

from flask import Flask, request, Response, jsonify

# Persistência simples (JSON).
from storage import load_db, save_db

# Opcional: init
try:
    from progress import init_user_if_needed  # type: ignore
except Exception:
    def init_user_if_needed(db: Dict[str, Any], user_key: str) -> None:
        pass

# Twilio
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client

# Timezone
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None

app = Flask(__name__)

# =========================
# Config / Flags do projeto
# =========================
FEATURE_PORTUGUES = os.getenv("FEATURE_PORTUGUES", "True") == "True"
FEATURE_LEITURA   = os.getenv("FEATURE_LEITURA", "False") == "True"
AUTO_SEQUENCE_PT_AFTER_MATH = os.getenv("AUTO_SEQUENCE_PT_AFTER_MATH", "True") == "True"
ROUNDS_PER_DAY = int(os.getenv("ROUNDS_PER_DAY", "5"))
MAX_MATH_DAY   = int(os.getenv("MAX_MATH_DAY", "60"))
MAX_PT_DAY     = int(os.getenv("MAX_PT_DAY", "60"))

PROJECT_TZ = os.getenv("PROJECT_TZ", "America/Bahia")

# Twilio (saídas proativas)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN", "")
# Ex.: "whatsapp:+14155238886" sandbox/validado
TWILIO_FROM = os.getenv("TWILIO_FROM", "")

_twilio_client: Optional[Client] = None
def _get_twilio() -> Client:
    global _twilio_client
    if _twilio_client is None:
        _twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _twilio_client

# ==================
# Helpers de sistema
# ==================
def _tz() -> Optional[ZoneInfo]:
    return ZoneInfo(PROJECT_TZ) if ZoneInfo else None

def _now() -> datetime:
    z = _tz()
    return datetime.now(z) if z else datetime.now()

def _today_str(dt: Optional[datetime] = None) -> str:
    dt = dt or _now()
    return dt.strftime("%Y-%m-%d")

def _digits_only(s: Optional[str]) -> str:
    return re.sub(r"\D+", "", s or "")

def _numbers_match(a: Optional[str], b: Optional[str]) -> bool:
    return _digits_only(a) == _digits_only(b)

def _weekday_key(dt: Optional[datetime] = None) -> str:
    # mon,tue,wed,thu,fri,sat,sun
    dt = dt or _now()
    return ["mon","tue","wed","thu","fri","sat","sun"][dt.weekday()]

def _parse_hhmm_strict(s: str) -> Optional[dtime]:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", s or "")
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return dtime(hour=hh, minute=mm, second=0)
    return None

def _parse_time_loose(s: str) -> Optional[dtime]:
    """Aceita: 8 -> 08:00 | 19h -> 19:00 | 7 pm -> 19:00 | 18:30"""
    s = (s or "").strip().lower()
    t = _parse_hhmm_strict(s)
    if t: return t
    m = re.match(r"^\s*(\d{1,2})\s*(h|pm|am)?\s*$", s)
    if m:
        hh = int(m.group(1))
        suf = (m.group(2) or "").lower()
        if suf == "pm" and 1 <= hh <= 11:
            hh += 12
        if suf == "am" and hh == 12:
            hh = 0
        if 0 <= hh <= 23:
            return dtime(hour=hh, minute=0, second=0)
    return None

def _combine_date_time(date_dt: datetime, hhmm: dtime) -> datetime:
    tz = date_dt.tzinfo
    return datetime(
        year=date_dt.year, month=date_dt.month, day=date_dt.day,
        hour=hhmm.hour, minute=hhmm.minute, second=0, tzinfo=tz
    )

def _mask_phone(p: Optional[str]) -> str:
    d = _digits_only(p)
    if len(d) < 2: return "—"
    return f"+{d[:-2]}**{d[-2:]}"

def _parse_phones_list(s: str) -> List[str]:
    parts = [p.strip() for p in (s or "").replace(" e ", ",").split(",") if p.strip()]
    out: List[str] = []
    for p in parts:
        d = _digits_only(p)
        if d:
            out.append(d)
    return out[:2]  # até 2 responsáveis

def _yes_no(body: str) -> Optional[bool]:
    b = (body or "").strip().lower()
    if b in ("1", "s", "sim", "yes", "y"): return True
    if b in ("2", "n", "nao", "não", "no"): return False
    return None

# ===========================
# DB layout e acesso a usuário
# ===========================
def _db() -> Dict[str, Any]:
    d = load_db()
    d.setdefault("users", {})
    return d

def _save(d: Dict[str, Any]) -> None:
    save_db(d)

GRADES = [
    "Infantil 4 (Pré-I)",
    "Infantil 5 (Pré-II)",
    "1º ano",
    "2º ano",
    "3º ano",
    "4º ano",
    "5º ano",
]

SCHEDULE_ORDER = [("mon","seg"),("tue","ter"),("wed","qua"),("thu","qui"),("fri","sex"),("sat","sáb"),("sun","dom")]

def _default_schedule() -> Dict[str, Optional[str]]:
    return {k: ("19:00" if k != "sun" else None) for k,_ in SCHEDULE_ORDER}

def _get_or_create_user(d: Dict[str, Any], sender: str) -> Tuple[str, Dict[str, Any]):
    key = _digits_only(sender)
    users = d["users"]
    if key in users:
        return key, users[key]
    for k, user in users.items():
        prof = (user.get("profile") or {})
        if _numbers_match(sender, prof.get("child_phone")):
            return k, user
        for g in (prof.get("guardians") or []):
            if _numbers_match(sender, g):
                return k, user
    user = {
        "profile": {
            "timezone": PROJECT_TZ,
            "child_phone": None,
            "guardians": [sender],  # remetente como responsável
            "child_name": None,
            "child_age": None,
            "grade": None,
        },
        "schedule": _default_schedule(),
        "daily_state": {},   # YYYY-MM-DD -> {done, done_ts, done_notified, miss_notified}
        "wizard": None,      # estado do onboarding
        "lesson": None,      # sessão de aula
    }
    users[key] = user
    return key, user

def _is_from_guardian(sender: str, user: Dict[str, Any]) -> bool:
    prof = (user.get("profile") or {})
    for g in (prof.get("guardians") or []):
        if _numbers_match(sender, g):
            return True
    return False

# ================
# Notificações
# ================
def _send_whatsapp(to_number: str, body: str) -> None:
    if not TWILIO_FROM or not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return
    client = _get_twilio()
    to_fmt = to_number if to_number.startswith("whatsapp:") else f"whatsapp:+{_digits_only(to_number)}"
    client.messages.create(from_=TWILIO_FROM, to=to_fmt, body=body)

def _notify_done(user: Dict[str, Any], day_key: str, late: bool = False) -> None:
    name = ((user.get("profile") or {}).get("child_name") or "A criança")
    if late:
        msg = f"✅ {name} concluiu agora as atividades de hoje. Obrigado pelo acompanhamento!"
    else:
        msg = f"✅ {name} concluiu as atividades de hoje (Mat/Port{'/Leitura' if FEATURE_LEITURA else ''}). Bom trabalho!"
    for g in (user.get("profile") or {}).get("guardians", []) or []:
        _send_whatsapp(g, msg)

def _notify_miss(user: Dict[str, Any], day_key: str) -> None:
    name = ((user.get("profile") or {}).get("child_name") or "A criança")
    msg = f"⚠️ {name} ainda não concluiu as atividades de hoje. Precisa de ajuda para finalizar?"
    for g in (user.get("profile") or {}).get("guardians", []) or []:
        _send_whatsapp(g, msg)

# ======================
# Check-in Diário (core)
# ======================
def _get_day_state(user: Dict[str, Any], day_key: str) -> Dict[str, Any]:
    ds = user.setdefault("daily_state", {})
    st = ds.setdefault(day_key, {})
    st.setdefault("done", False)
    st.setdefault("done_ts", None)
    st.setdefault("done_notified", False)
    st.setdefault("miss_notified", False)
    return st

def mark_day_done(user: Dict[str, Any], when: Optional[datetime] = None) -> Tuple[str, Dict[str, Any]):
    when = when or _now()
    day_key = _today_str(when)
    st = _get_day_state(user, day_key)
    st["done"] = True
    if not st["done_ts"]:
        st["done_ts"] = when.isoformat()
    if not st.get("done_notified", False):
        _notify_done(user, day_key, late=bool(st.get("miss_notified", False)))
        st["done_notified"] = True
    return day_key, st

def _get_today_reminder_dt(user: Dict[str, Any], base_dt: Optional[datetime] = None) -> Optional[datetime]:
    base_dt = base_dt or _now()
    sched = user.get("schedule") or {}
    key = _weekday_key(base_dt)
    hhmm = sched.get(key)
    if not hhmm:
        return None
    t = _parse_hhmm_strict(hhmm) or _parse_time_loose(hhmm)
    if not t:
        return None
    return _combine_date_time(base_dt, t)

def process_checkin_cron(user: Dict[str, Any], now_dt: Optional[datetime] = None) -> Optional[str]:
    now_dt = now_dt or _now()
    day_key = _today_str(now_dt)
    st = _get_day_state(user, day_key)
    rem_dt = _get_today_reminder_dt(user, base_dt=now_dt)
    if rem_dt is None:
        return "skip:no-schedule"
    deadline = rem_dt + timedelta(hours=3)
    if st["done"]:
        if not st.get("done_notified", False):
            _notify_done(user, day_key, late=bool(st.get("miss_notified", False)))
            st["done_notified"] = True
            return "sent:done"
        return "skip:already-done-notified"
    if now_dt >= deadline and not st.get("miss_notified", False):
        _notify_miss(user, day_key)
        st["miss_notified"] = True
        return "sent:miss"
    return "skip:not-due"

# ======================
# Aula — sessão MCQ MVP
# ======================
def _build_math_question() -> Dict[str, Any]:
    a = random.randint(2, 9)
    b = random.randint(2, 9)
    op = random.choice(["+", "-"])
    if op == "-" and b > a:
        a, b = b, a
    correct = a + b if op == "+" else a - b
    # gera 3 distratores próximos
    opts = {correct}
    while len(opts) < 4:
        delta = random.choice([-3,-2,-1,1,2,3])
        cand = max(0, correct + delta)
        opts.add(cand)
    options = list(opts)
    random.shuffle(options)
    answer_idx = options.index(correct)
    return {
        "type": "math",
        "prompt": f"Quanto é {a} {op} {b}?",
        "options": [str(x) for x in options],
        "answer": answer_idx  # 0..3
    }

def _build_pt_question() -> Dict[str, Any]:
    # Simples: ortografia/acento
    qs = [
        ("Qual está escrito corretamente?", ["Exceção", "Excessão", "Eceção", "Ecessão"], 0),
        ("Qual plural está correto para *pão*?", ["pãos", "pães", "pãoses", "pãeses"], 1),
        ("Qual forma está correta?", ["A gente vamos", "A gente vai", "Nós vai", "Nós vamos ir"], 1),
        ("Complete: Ela ___ ao mercado ontem.", ["vai", "foi", "iria", "vou"], 1),
    ]
    prompt, options, ans = random.choice(qs)
    return {
        "type": "pt",
        "prompt": prompt,
        "options": options,
        "answer": ans
    }

def _start_lesson(user: Dict[str, Any]) -> str:
    """Cria sessão do dia: 5 Matemática + 1 Português."""
    qts: List[Dict[str, Any]) = []
    for _ in range(max(1, ROUNDS_PER_DAY)):
        qts.append(_build_math_question())
    if FEATURE_PORTUGUES:
        qts.append(_build_pt_question())

    user["lesson"] = {
        "idx": 0,
        "q": qts,
        "hits": 0
    }
    return _present_current_question(user)

def _present_current_question(user: Dict[str, Any]) -> str:
    les = user.get("lesson") or {}
    idx = int(les.get("idx", 0))
    qts: List[Dict[str, Any]) = les.get("q") or []
    if idx >= len(qts):
        return _finish_lesson(user)

    q = qts[idx]
    opts = "\n".join([f"{i+1}) {opt}" for i, opt in enumerate(q["options"])])
    header = "🧮 Matemática" if q["type"] == "math" else "📚 Português"
    return f"{header}\n{q['prompt']}\nResponda com 1, 2, 3 ou 4:\n{opts}"

def _apply_answer(user: Dict[str, Any], body: str) -> str:
    les = user.get("lesson") or {}
    idx = int(les.get("idx", 0))
    qts: List[Dict[str, Any]) = les.get("q") or []
    if not qts or idx >= len(qts):
        return "Não há aula em andamento. Digite *começar aula*."

    m = re.match(r"^\s*([1-4])\s*$", (body or "").strip())
    if not m:
        return "Responda apenas com *1*, *2*, *3* ou *4*."

    choice = int(m.group(1)) - 1
    q = qts[idx]
    correct_idx = int(q["answer"])
    if choice == correct_idx:
        les["hits"] = int(les.get("hits", 0)) + 1

    les["idx"] = idx + 1
    user["lesson"] = les  # save back

    # Próxima ou fim
    if les["idx"] >= len(qts):
        return _finish_lesson(user)
    return _present_current_question(user)

def _finish_lesson(user: Dict[str, Any]) -> str:
    les = user.get("lesson") or {}
    total = len(les.get("q") or [])
    hits = int(les.get("hits", 0))
    user["lesson"] = None
    # Marca dia concluído e notifica responsáveis (idempotente)
    mark_day_done(user, when=_now())
    return f"✅ Aula concluída! Acertos: {hits}/{total}.\nQuer ver o *status* do dia?"

# ======================
# Onboarding (wizard)
# ======================
GRADES_LIST = GRADES  # alias legível

def _start_wizard(user: Dict[str, Any])) -> str:
    user["wizard"] = {
        "step": "ask_name",
        "tmp": {}
    }
    return ("Oi! Eu sou a MARIA ANGELA 🌟 sua assistente de aula.\n"
            "Vou te acompanhar em atividades de Matemática, Português"
            f"{' e Leitura' if FEATURE_LEITURA else ''}.\n\n"
            "Pra começar, me diga: *qual é o nome da criança?*")

def _wizard_prompt_grade() -> str:
    opts = "\n".join([f"{i+1}) {GRADES_LIST[i]}" for i in range(len(GRADES_LIST))])
    return ("E em qual série/ano ela está?\n"
            "Responda o número ou escreva:\n" + opts)

def _wizard_prompt_yesno_domingo() -> str:
    return ("Perfeito! 📅 A rotina é segunda a sábado por padrão.\n"
            "Deseja incluir domingo também?\n"
            "1) sim   2) não")

def _wizard_prompt_time_for(day_pt: str) -> str:
    return (f"Qual horário para *{day_pt}*? (faixa 05:00–21:30)\n"
            "Responda o número ou o horário:\n"
            "1) 08:00   2) 18:30   3) 19:00   4) 20:00   5) outro")

def _wizard_confirm(user: Dict[str, Any], tmp: Dict[str, Any]) -> str:
    child = tmp.get("child_name") or (user.get("profile") or {}).get("child_name") or "—"
    age = tmp.get("child_age") or (user.get("profile") or {}).get("child_age") or "—"
    grade = tmp.get("grade") or (user.get("profile") or {}).get("grade") or "—"
    cphone = tmp.get("child_phone") or (user.get("profile") or {}).get("child_phone")
    guards = tmp.get("guardians") or (user.get("profile") or {}).get("guardians") or []
    sched = tmp.get("schedule") or user.get("schedule") or {}

    parts = []
    for k,pt in SCHEDULE_ORDER:
        v = sched.get(k)
        if v:
            parts.append(f"{pt} {v}")
    rotina = " | ".join(parts) if parts else "—"

    return (
        "Confere? ✅\n"
        f"* Nome: {child}\n"
        f"* Idade: {age}\n"
        f"* Série: {grade}\n"
        f"* WhatsApp da criança: {_mask_phone(cphone)}\n"
        f"* Responsável(is): {', '.join(_mask_phone(g) for g in guards) or '—'}\n"
        f"* Rotina: {rotina}\n"
        "Responda *sim* para salvar, ou *não* para ajustar."
    )

def _handle_wizard(user: Dict[str, Any], body: str) -> Optional[str]:
    wz = user.get("wizard")
    if not wz:
        return None
    step = wz.get("step")
    tmp = wz.setdefault("tmp", {})

    if step == "ask_name":
        name = body.strip()
        if len(name) < 2:
            return "Digite um nome válido (mín. 2 letras). Qual é o nome da criança?"
        tmp["child_name"] = name
        wz["step"] = "ask_age"
        return f"Perfeito, {name}! 😊\nQuantos anos ela tem?"

    if step == "ask_age":
        m = re.match(r"^\s*(\d{1,2})\s*$", body)
        if not m:
            return "Me diga um número (ex.: 9). Quantos anos ela tem?"
        age = int(m.group(1))
        if not (3 <= age <= 17):
            return "Idade fora do padrão (3–17). Tente novamente."
        tmp["child_age"] = age
        wz["step"] = "ask_grade"
        return _wizard_prompt_grade()

    if step == "ask_grade":
        n = re.match(r"^\s*(\d{1,2})\s*$", body)
        if n:
            idx = int(n.group(1)) - 1
            if 0 <= idx < len(GRADES_LIST):
                tmp["grade"] = GRADES_LIST[idx]
            else:
                return _wizard_prompt_grade()
        else:
            txt = body.strip().lower()
            chosen = None
            for g in GRADES_LIST:
                if txt in g.lower():
                    chosen = g
                    break
            if not chosen:
                return _wizard_prompt_grade()
            tmp["grade"] = chosen
        wz["step"] = "ask_child_whatsapp"
        return ("Matheus tem um número próprio de WhatsApp?\n"
                "Envie no formato +55 DDD XXXXX-XXXX ou responda *não tem*.")

    if step == "ask_child_whatsapp":
        b = body.strip().lower()
        if "não tem" in b or "nao tem" in b or b in ("nao", "não", "n"):
            tmp["child_phone"] = None
        else:
            d = _digits_only(body)
            if not d:
                return ("Envie o WhatsApp da criança no formato +55 DDD XXXXX-XXXX "
                        "ou responda *não tem*.")
            tmp["child_phone"] = d
        wz["step"] = "ask_guardians"
        return ("Agora, o(s) número(s) do(s) responsável(is) (1 ou 2), separados por vírgula.\n"
                "Ex.: +55 71 98888-7777, +55 71 97777-8888")

    if step == "ask_guardians":
        gs = _parse_phones_list(body)
        sender = (user.get("profile") or {}).get("guardians", [])[0]
        if sender and _digits_only(sender) not in [_digits_only(x) for x in gs]:
            gs = [sender] + gs
        tmp["guardians"] = list(dict.fromkeys(gs))[:2]
        wz["step"] = "ask_sunday"
        return _wizard_prompt_yesno_domingo()

    if step == "ask_sunday":
        yn = _yes_no(body)
        if yn is None:
            return _wizard_prompt_yesno_domingo()
        tmp.setdefault("schedule", _default_schedule())
        tmp["schedule"]["sun"] = tmp["schedule"]["sun"] if yn else None
        wz["step"] = "ask_time_mon"
        return _wizard_prompt_time_for("seg")

    def _handle_time_for(day_key: str, day_pt: str, next_step: str) -> str:
        s = body.strip()
        choice = re.match(r"^\s*([1-5])\s*$", s)
        if choice:
            c = int(choice.group(1))
            mapping = {1:"08:00", 2:"18:30", 3:"19:00", 4:"20:00"}
            if c in (1,2,3,4):
                val = mapping[c]
                t = _parse_hhmm_strict(val)
            else:
                return "Digite o horário desejado (ex.: 18:30, 19h, 7 pm)."
        else:
            t = _parse_hhmm_strict(s) or _parse_time_loose(s)
            if not t:
                return _wizard_prompt_time_for(day_pt)
        tmp.setdefault("schedule", _default_schedule())
        tmp["schedule"][day_key] = f"{t.hour:02d}:{t.minute:02d}"
        wz["step"] = next_step
        if next_step == "ask_time_tue":  return _wizard_prompt_time_for("ter")
        if next_step == "ask_time_wed":  return _wizard_prompt_time_for("qua")
        if next_step == "ask_time_thu":  return _wizard_prompt_time_for("qui")
        if next_step == "ask_time_fri":  return _wizard_prompt_time_for("sex")
        if next_step == "ask_time_sat":  return _wizard_prompt_time_for("sáb")
        return _wizard_confirm(user, tmp)

    if step == "ask_time_mon": return _handle_time_for("mon","seg","ask_time_tue")
    if step == "ask_time_tue": return _handle_time_for("tue","ter","ask_time_wed")
    if step == "ask_time_wed": return _handle_time_for("wed","qua","ask_time_thu")
    if step == "ask_time_thu": return _handle_time_for("thu","qui","ask_time_fri")
    if step == "ask_time_fri": return _handle_time_for("fri","sex","ask_time_sat")
    if step == "ask_time_sat": return _handle_time_for("sat","sáb","confirm")

    if step == "confirm":
        yn = _yes_no(body)
        if yn is None:
            return _wizard_confirm(user, tmp)
        if not yn:
            user["wizard"] = None
            return _start_wizard(user)
        prof = user.setdefault("profile", {})
        prof["child_name"] = tmp.get("child_name")
        prof["child_age"] = tmp.get("child_age")
        prof["grade"] = tmp.get("grade")
        prof["child_phone"] = tmp.get("child_phone")
        if tmp.get("guardians"):
            prof["guardians"] = tmp["guardians"]
        if tmp.get("schedule"):
            user["schedule"] = tmp["schedule"]
        user["wizard"] = None
        return "Cadastro salvo! ✅ Use *status* para ver a rotina do dia, ou escreva *começar aula* quando quiser iniciar."

    return None

# ======================
# Mensagens e Comandos
# ======================
WELCOME = (
    "Olá! Eu sou a MARIA ANGELA 👋\n"
    "Posso acompanhar as atividades diárias de Matemática e Português"
    f"{' e Leitura' if FEATURE_LEITURA else ''}.\n\n"
    "Digite *iniciar* para configurar, *começar aula* para iniciar atividades, "
    "ou *status* para ver o dia. Para teste, *fim* marca o dia como concluído."
)

def _status_text(user: Dict[str, Any]) -> str:
    now_dt = _now()
    day_key = _today_str(now_dt)
    st = _get_day_state(user, day_key)
    rem_dt = _get_today_reminder_dt(user, base_dt=now_dt)
    rem = rem_dt.strftime("%H:%M") if rem_dt else "—"
    dia = dict(SCHEDULE_ORDER)[_weekday_key(now_dt)]
    in_lesson = "sim" if user.get("lesson") else "não"
    return (
        f"📊 Status {day_key}\n"
        f"- Feito: {'sim' if st['done'] else 'não'}\n"
        f"- Lembrete de hoje ({dia}): {rem}\n"
        f"- Aula em andamento: {in_lesson}\n"
        f"- Notif. feito: {'sim' if st.get('done_notified') else 'não'}\n"
        f"- Notif. falta: {'sim' if st.get('miss_notified') else 'não'}"
    )

# ==================
# Webhook / Endpoints
# ==================
@app.post("/bot")
def bot() -> Response:
    d = _db()
    from_raw = request.values.get("From", "")  # ex.: "whatsapp:+55..."
    body = (request.values.get("Body", "") or "").strip()

    user_key, user = _get_or_create_user(d, from_raw)
    init_user_if_needed(d, user_key)

    resp = MessagingResponse()
    msg = resp.message()
    lower = body.lower()

    # Comandos de atalho (admin/fluxo)
    if lower in ("reiniciar cadastro", "reset cadastro", "recomeçar cadastro", "recomecar cadastro"):
        user["wizard"] = None
        _save(d)
        msg.body(_start_wizard(user))
        return Response(str(resp), mimetype="application/xml")

    if lower in ("iniciar", "start"):
        msg.body(_start_wizard(user))
        _save(d)
        return Response(str(resp), mimetype="application/xml")

    if lower in ("status", "debug status", "s"):
        msg.body(_status_text(user))
        _save(d)
        return Response(str(resp), mimetype="application/xml")

    if lower in ("fim", "finalizar", "concluir", "fechar dia"):
        mark_day_done(user, when=_now())
        _save(d)
        msg.body("✅ Dia marcado como concluído. Aviso enviado aos responsáveis.")
        return Response(str(resp), mimetype="application/xml")

    if lower in ("cancelar aula", "cancelar", "parar aula"):
        user["lesson"] = None
        _save(d)
        msg.body("Aula cancelada. Quando quiser retomar, envie *começar aula*.")
        return Response(str(resp), mimetype="application/xml")

    # Wizard de cadastro tem prioridade
    if user.get("wizard"):
        out = _handle_wizard(user, body)
        if out:
            msg.body(out)
            _save(d)
            return Response(str(resp), mimetype="application/xml")

    # Iniciar aula
    if lower in ("começar aula", "comecar aula", "iniciar aula", "aula"):
        # Se já tiver uma em andamento, continua
        if user.get("lesson"):
            msg.body(_present_current_question(user))
        else:
            msg.body(_start_lesson(user))
        _save(d)
        return Response(str(resp), mimetype="application/xml")

    # Resposta de aula em andamento (1..4)
    if user.get("lesson"):
        msg.body(_apply_answer(user, body))
        _save(d)
        return Response(str(resp), mimetype="application/xml")

    # Default
    msg.body(WELCOME)
    _save(d)
    return Response(str(resp), mimetype="application/xml")

@app.get("/admin/cron")
def cron() -> Response:
    """Executa a verificação de check-in para TODOS os usuários.
       Use /admin/cron?dry=1 para simular sem enviar.
    """
    d = _db()
    dry = request.args.get("dry", "0") in ("1", "true", "True")
    now_dt = _now()

    results: List[Tuple[str, str]] = []
    for k, user in list((d.get("users") or {}).items()):
        if dry:
            tag = _cron_simulate(user, now_dt)
            results.append((k, tag))
        else:
            tag = process_checkin_cron(user, now_dt)
            results.append((k, tag or "skip"))

    if not dry:
        _save(d)
    return jsonify({
        "now": now_dt.isoformat(),
        "dry_run": dry,
        "results": [{"user": k, "result": r} for k, r in results]
    })

def _cron_simulate(user: Dict[str, Any], now_dt: datetime) -> str:
    day_key = _today_str(now_dt)
    st = _get_day_state(user, day_key)
    rem_dt = _get_today_reminder_dt(user, base_dt=now_dt)
    if rem_dt is None:
        return "SIM:skip:no-schedule"
    deadline = rem_dt + timedelta(hours=3)
    if st["done"]:
        return "SIM:sent:done" if not st.get("done_notified", False) else "SIM:skip:already-done-notified"
    if now_dt >= deadline and not st.get("miss_notified", False):
        return "SIM:sent:miss"
    return "SIM:skip:not-due"

# Saúde do serviço
@app.get("/healthz")
def healthz() -> Response:
    return jsonify({"ok": True, "tz": PROJECT_TZ, "time": _now().isoformat()})
