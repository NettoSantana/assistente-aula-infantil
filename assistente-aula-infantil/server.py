# server.py — Assistente de Aula Infantil
# Onboarding "MARIA ANGELA" + Rotina por dia (seg–sáb obrig., dom opcional)
# Currículo Matemática fixo (90 dias): Adição → Subtração → Multiplicação → Divisão → Revisões
# Fluxo: Matemática (lote) → Português (1 questão) → Leitura
import os, re, itertools
from flask import Flask, request, jsonify, Response
from storage import load_db, save_db
from progress import init_user_if_needed
from leitura import get_today_reading_goal, check_reading_submission
from activities import portugues_activity, check_answer  # português (1 questão)

# Twilio — resposta imediata
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client

app = Flask(__name__)

PROJECT_NAME = os.getenv("PROJECT_NAME", "assistente_aula_infantil")

# (REST opcional para envios proativos)
TWILIO_SID   = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM  = os.getenv("TWILIO_FROM", "")
_twilio_client = Client(TWILIO_SID, TWILIO_TOKEN) if (TWILIO_SID and TWILIO_TOKEN) else None

# ------------------- Util: TwiML -------------------
def reply_twiml(text: str) -> Response:
    r = MessagingResponse()
    r.message(text)
    return Response(str(r), mimetype="application/xml", status=200)

# ------------------- Util: telefones -------------------
BR_DEFAULT_CC = "55"
def normalize_phone(s: str) -> str | None:
    if not s:
        return None
    x = re.sub(r"[^\d+]", "", s).strip()
    if x.lower() in {"nao tem", "não tem", "naotem"}:
        return None
    if x.startswith("+"):
        digits = re.sub(r"\D", "", x)
        return f"+{digits}"
    digits = re.sub(r"\D", "", x)
    if 10 <= len(digits) <= 12:
        return f"+{BR_DEFAULT_CC}{digits}"
    return None

def mask_phone(p: str | None) -> str:
    if not p:
        return "não tem"
    d = re.sub(r"\D", "", p)
    if len(d) < 4: return p
    return f"+{d[:2]} {d[2:4]} *****-{d[-2:]}"

# ------------------- Util: série/ano -------------------
GRADE_MAP = {
    "infantil4": "Infantil 4 (Pré-I)",
    "infantil5": "Infantil 5 (Pré-II)",
    "1": "1º ano","2": "2º ano","3":"3º ano","4":"4º ano","5":"5º ano",
}
def parse_grade(txt: str) -> str | None:
    t = (txt or "").lower().strip()
    if "infantil 4" in t or "pré-i" in t or "pre-i" in t: return GRADE_MAP["infantil4"]
    if "infantil 5" in t or "pré-ii" in t or "pre-ii" in t: return GRADE_MAP["infantil5"]
    m = re.search(r"(\d)\s*(º|o)?\s*ano", t)
    if m: return GRADE_MAP.get(m.group(1))
    if t in {"1","2","3","4","5"}: return GRADE_MAP.get(t)
    return None

def age_from_text(txt: str) -> int | None:
    m = re.search(r"(\d{1,2})", txt or "")
    if not m: return None
    val = int(m.group(1))
    return val if 3 <= val <= 13 else None

# ------------------- Util: rotina (dias/horário por dia) -------------------
DEFAULT_DAYS = ["mon","tue","wed","thu","fri","sat"]  # seg–sáb obrigatórios
DAY_ORDER    = ["mon","tue","wed","thu","fri","sat","sun"]
DAYS_PT      = {"mon":"seg","tue":"ter","wed":"qua","thu":"qui","fri":"sex","sat":"sáb","sun":"dom"}
PT2KEY = {
    "seg":"mon","segunda":"mon",
    "ter":"tue","terça":"tue","terca":"tue",
    "qua":"wed","quarta":"wed",
    "qui":"thu","quinta":"thu",
    "sex":"fri","sexta":"fri",
    "sab":"sat","sáb":"sat","sabado":"sat","sábado":"sat",
    "dom":"sun","domingo":"sun",
}

def parse_yes_no(txt: str) -> bool | None:
    t = (txt or "").strip().lower()
    if t in {"sim","s","yes","y"}: return True
    if t in {"não","nao","n","no"}: return False
    return None

def parse_time_hhmm(txt: str) -> str | None:
    """
    Aceita "19", "19:00", "19h", "19h30", "19 30", "7 pm".
    Retorna HH:MM (24h) dentro de 05:00–21:30.
    """
    t = (txt or "").strip().lower()
    t = t.replace("h", ":").replace(" ", "")
    t = t.replace("pm","p").replace("am","a")
    m = re.match(r"^(\d{1,2})(?::?(\d{2}))?([ap])?$", t)
    if not m: return None
    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    ap = m.group(3)
    if ap == "p" and hh < 12: hh += 12
    if ap == "a" and hh == 12: hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59): return None
    if (hh < 5) or (hh > 21) or (hh == 21 and mm > 30): return None
    return f"{hh:02d}:{mm:02d}"

def describe_schedule(sched: dict) -> str:
    """Ex.: seg 16:00 | ter 17:00 | ... (somente dias presentes em sched['days'])"""
    if not sched: return "seg–sáb 19:00"
    days = [d for d in DAY_ORDER if d in (sched.get("days") or [])]
    times = sched.get("times") or {}
    parts = []
    for d in days:
        hhmm = times.get(d, "—")
        parts.append(f"{DAYS_PT.get(d,d)} {hhmm}")
    return " | ".join(parts)

# ------------------- Currículo Matemática (90 dias) -------------------
def _curriculum_spec(day_idx: int):
    """
    Retorna um dict com {phase, op, mode, anchor} para o dia (1..90).
    Fases:
      A Adição: dias 1–24
      B Subtração: 25–38
      C Multiplicação: 39–58
      D Divisão: 59–74
      E Revisão/Misto: 75–90
    """
    if day_idx < 1: day_idx = 1
    if day_idx > 90: day_idx = 90

    if 1 <= day_idx <= 10:
        # Adição direta (âncora 1..10): a+1..a+10
        return {"phase": "A-Adição", "op": "soma", "mode": "direct", "anchor": day_idx}
    if 11 <= day_idx <= 20:
        # Adição invertida (âncora 1..10): 1+a..10+a
        return {"phase": "A-Adição", "op": "soma", "mode": "inv", "anchor": day_idx - 10}
    if 21 <= day_idx <= 24:
        # Completar 10 / misto até 20
        return {"phase": "A-Adição", "op": "soma", "mode": "mix10", "anchor": None}

    if 25 <= day_idx <= 34:
        # Subtração: minuendo 11..20 (m-1..m-10)
        return {"phase": "B-Subtração", "op": "sub", "mode": "minuend", "anchor": day_idx - 14}  # 25->11 ... 34->20
    if 35 <= day_idx <= 38:
        # Subtração mista/fato ausente
        return {"phase": "B-Subtração", "op": "sub", "mode": "mix", "anchor": None}

    if 39 <= day_idx <= 48:
        # Multiplicação direta (âncora 1..10): a×1..a×10
        return {"phase": "C-Multiplicação", "op": "mult", "mode": "direct", "anchor": day_idx - 38}
    if 49 <= day_idx <= 58:
        # Multiplicação comutativa/variações (âncora 1..10)
        return {"phase": "C-Multiplicação", "op": "mult", "mode": "commute", "anchor": day_idx - 48}

    if 59 <= day_idx <= 68:
        # Divisão (divisor 1..10): (d×1)/d .. (d×10)/d
        return {"phase": "D-Divisão", "op": "div", "mode": "divisor", "anchor": day_idx - 58}
    if 69 <= day_idx <= 74:
        # Divisão mista/fato ausente
        return {"phase": "D-Divisão", "op": "div", "mode": "mix", "anchor": None}

    # 75–90 Revisões / Misto equilibrado
    return {"phase": "E-Revisão", "op": "mix", "mode": "review", "anchor": None}

def _module_label(op: str, etapa: int) -> str:
    # (mantido para compatibilidade de formatação)
    labels = {"soma":"Soma","sub":"Subtração","mult":"Multiplicação","div":"Divisão","mix":"Revisão"}
    extra = { "soma": f"+{etapa}", "sub": f"-{etapa}", "mult": f"×{etapa}", "div": f"÷{etapa}", "mix": "" }
    return f"{labels.get(op, op.title())} {etapa} ({extra.get(op,'')})"

def _format_math_prompt(batch):
    title = batch.get("title", "Matemática")
    lines = [
        f"🧩 *{title}*",
        "Responda TUDO em uma única mensagem, *separando por vírgulas*.",
        "Ex.: 2,4,6,8,10,12,14,16,18,20",
        ""
    ]
    for idx, p in enumerate(batch["problems"], start=1):
        lines.append(f"{idx}) {p} = ?")
    return "\n".join(lines)

def _parse_csv_numbers(s: str):
    parts = [x.strip() for x in (s or "").split(",") if x.strip() != ""]
    nums = []
    for x in parts:
        try:
            nums.append(int(x))
        except Exception:
            return None
    return nums

# ---------- Geradores de listas conforme spec ----------
def _gen_add_direct(a: int):
    problems = [f"{a}+{i}" for i in range(1, 11)]
    answers  = [a + i for i in range(1, 11)]
    return problems, answers

def _gen_add_inv(a: int):
    problems = [f"{i}+{a}" for i in range(1, 11)]
    answers  = [i + a for i in range(1, 11)]
    return problems, answers

def _gen_add_mix10():
    pairs = [(1,9),(2,8),(3,7),(4,6),(5,5),(6,4),(7,3),(8,2),(9,1),(10,0)]
    problems = [f"{x}+{y}" for x,y in pairs]
    answers  = [x+y for x,y in pairs]
    return problems, answers

def _gen_sub_minuend(m: int):
    problems = [f"{m}-{i}" for i in range(1, 11)]
    answers  = [m - i for i in range(1, 11)]
    return problems, answers

def _gen_sub_mix():
    # mistura fatos 11..20 com faltantes
    base = list(range(11, 16))  # 11..15
    problems = []
    answers  = []
    for m in base:
        problems.append(f"{m}-1")
        answers.append(m-1)
    # fatos ausentes do tipo "__ + a = b" (resposta é b-a)
    missing = [(10,7),(12,5),(14,9),(15,8),(18,6)]
    for total,a in missing:
        problems.append(f"__+{a}={total}")
        answers.append(total - a)
    # completa 10 itens
    problems = problems[:10]
    answers  = answers[:10]
    return problems, answers

def _gen_mult_direct(a: int):
    problems = [f"{a}x{i}" for i in range(1, 11)]
    answers  = [a * i for i in range(1, 11)]
    return problems, answers

def _gen_mult_commute(a: int):
    # intercala a×i com i×a (1..10)
    left  = [f"{a}x{i}" for i in range(1, 6)]
    right = [f"{i}x{a}" for i in range(6, 11)]
    problems = left + right
    answers  = [a*i for i in range(1,6)] + [i*a for i in range(6,11)]
    return problems, answers

def _gen_div_divisor(d: int):
    # (d×1)/d .. (d×10)/d
    problems = [f"{d*i}/{d}" for i in range(1, 11)]
    answers  = [i for i in range(1, 11)]
    return problems, answers

def _gen_div_mix():
    # divisões variadas com divisor 2..10
    divs = [(12,3),(14,7),(16,4),(18,9),(20,5),(21,7),(24,6),(30,5),(32,8),(40,10)]
    problems = [f"{a}/{b}" for a,b in divs]
    answers  = [a//b for a,b in divs]
    return problems, answers

def _gen_review_mix():
    # 10 itens: 3 adições, 3 subtrações, 2 multiplicações, 2 divisões (determinístico)
    adds = [(7,3),(8,5),(9,6)]
    subs = [(15,7),(18,9),(20,11)]
    mult = [(3,7),(4,6)]
    divs = [(24,6),(40,10)]
    problems = [f"{a}+{b}" for a,b in adds] + \
               [f"{a}-{b}" for a,b in subs] + \
               [f"{a}x{b}" for a,b in mult] + \
               [f"{a}/{b}" for a,b in divs]
    answers  = [a+b for a,b in adds] + \
               [a-b for a,b in subs] + \
               [a*b for a,b in mult] + \
               [a//b for a,b in divs]
    return problems, answers

def _build_batch_from_spec(spec: dict):
    phase = spec["phase"]; op = spec["op"]; mode = spec["mode"]; anchor = spec["anchor"]
    title = f"Matemática — {phase}"
    if op == "soma":
        if mode == "direct":
            p,a = _gen_add_direct(anchor)
            title += f" · {anchor}+1 … {anchor}+10"
        elif mode == "inv":
            p,a = _gen_add_inv(anchor)
            title += f" · 1+{anchor} … 10+{anchor}"
        else:
            p,a = _gen_add_mix10()
            title += " · completar 10"
    elif op == "sub":
        if mode == "minuend":
            p,a = _gen_sub_minuend(anchor)
            title += f" · {anchor}-1 … {anchor}-10"
        else:
            p,a = _gen_sub_mix()
            title += " · misto"
    elif op == "mult":
        if mode == "direct":
            p,a = _gen_mult_direct(anchor)
            title += f" · {anchor}×1 … {anchor}×10"
        else:
            p,a = _gen_mult_commute(anchor)
            title += f" · comutativas de {anchor}"
    elif op == "div":
        if mode == "divisor":
            p,a = _gen_div_divisor(anchor)
            title += f" · ÷{anchor}"
        else:
            p,a = _gen_div_mix()
            title += " · misto"
    else:
        p,a = _gen_review_mix()
        title += " · revisão"

    return {"problems": p, "answers": a, "title": title, "spec": spec}

def _check_math_batch(user, text: str):
    pend = user.get("pending", {}).get("mat_lote")
    if not pend:
        return False, "Nenhum lote de Matemática pendente."
    expected = pend["answers"]
    got = _parse_csv_numbers(text)
    if got is None:
        return False, "Envie somente números separados por vírgula (ex.: 2,4,6,...)"
    if len(got) != len(expected):
        return False, f"Você enviou {len(got)} respostas, mas são {len(expected)} itens. Reenvie os {len(expected)} valores, separados por vírgula."
    wrong_idx = [i+1 for i, (g,e) in enumerate(zip(got, expected)) if g != e]
    if wrong_idx:
        pos = ", ".join(map(str, wrong_idx))
        return False, f"❌ Algumas respostas estão incorretas nas posições: {pos}. Reenvie a lista completa (ex.: 2,4,6,...)"
    # sucesso: registra com info do currículo
    spec = pend.get("spec", {})
    user["history"]["matematica"].append({
        "tipo": "lote",
        "curriculum": spec,
        "problems": pend["problems"],
        "answers": got,
    })
    user["levels"]["matematica"] += 1
    # avança dia do currículo
    cur = user.setdefault("curriculum", {"math_day": 1, "total_days": 90})
    cur["math_day"] = min(90, int(cur.get("math_day",1)) + 1)
    # limpa pendência
    user["pending"].pop("mat_lote", None)
    return True, f"✅ Matemática concluída! Avançando para o *dia {cur['math_day']}* do plano."

# ------------------- Português / Leitura -------------------
def _start_portugues(user):
    lvl = user["levels"]["portugues"]
    act = portugues_activity(lvl)
    user["pending"]["portugues"] = act.__dict__
    return "✍️ *Português*\n" + act.enunciado

def _start_leitura(user):
    meta = get_today_reading_goal(user)
    return f"📖 *Leitura* — {meta}"

# ------------------- Onboarding (MARIA ANGELA) -------------------
def needs_onboarding(user) -> bool:
    prof = user.get("profile", {})
    if not prof.get("child_name"): return True
    if not prof.get("age"): return True
    if not prof.get("grade"): return True
    guardians = prof.get("guardians") or []
    if len(guardians) < 1: return True
    sched = prof.get("schedule") or {}
    days  = sched.get("days")
    times = sched.get("times")
    if not days or not isinstance(days, list): return True
    if not times or not all(d in times and times[d] for d in days): return True
    return False

def ob_state(user):
    user.setdefault("onboarding", {"step": None, "data": {}})
    return user["onboarding"]

def ob_start() -> str:
    return (
        "Oi! Eu sou a *MARIA ANGELA* 🌟 sua assistente de aula.\n"
        "Vou te acompanhar em atividades de *Matemática, Português e Leitura*.\n\n"
        "Pra começar, me diga: *qual é o nome da criança?*"
    )

def _schedule_init_days(data, include_sun: bool):
    days = DEFAULT_DAYS.copy()
    if include_sun: days.append("sun")
    data.setdefault("schedule", {})
    data["schedule"]["days"] = days
    data["schedule"]["times"] = {}
    data["schedule"]["pending_days"] = days.copy()
    data["schedule"]["current_day"]  = None

def _prompt_for_next_day_time(data) -> str:
    pend = data["schedule"]["pending_days"]
    if not pend:
        return ob_summary(data)
    day = pend[0]
    data["schedule"]["current_day"] = day
    label = DAYS_PT.get(day, day)
    return f"Qual *horário* para *{label}*? (ex.: 18:30, 19h, 7 pm) — faixa 05:00–21:30."

def _set_time_for_current_day(data, text: str) -> str | None:
    hhmm = parse_time_hhmm(text)
    if not hhmm:
        return "Horário inválido. Exemplos: *19:00*, *18h30*, *7 pm*. Faixa aceita: 05:00–21:30."
    day = data["schedule"]["current_day"]
    data["schedule"]["times"][day] = hhmm
    # remove da fila
    data["schedule"]["pending_days"].pop(0)
    data["schedule"]["current_day"] = None
    return None

def ob_summary(data: dict) -> str:
    sched = data.get("schedule") or {}
    return (
        "Confere? ✅\n"
        f"• *Nome:* {data.get('child_name')}\n"
        f"• *Idade:* {data.get('age')} anos\n"
        f"• *Série:* {data.get('grade')}\n"
        f"• *WhatsApp da criança:* {mask_phone(data.get('child_phone'))}\n"
        f"• *Responsável(is):* {', '.join(mask_phone(p) for p in (data.get('guardians') or []))}\n"
        f"• *Rotina:* {describe_schedule(sched)}\n"
        "Responda *sim* para salvar, ou *não* para ajustar."
    )

def ob_step(user, text: str) -> str:
    st = ob_state(user)
    step = st.get("step")
    data = st.get("data", {})

    # Correções diretas (básicos/domingo e horário por dia tipo "seg: 16:00")
    m = re.match(r"^\s*(nome|idade|serie|série|crianca|criança|pais|pais/responsaveis|domingo)\s*:\s*(.+)$", text, re.I)
    if m:
        field = m.group(1).lower()
        val = m.group(2).strip()
        if field in {"serie", "série"}:
            g = parse_grade(val)
            if not g: return "Não reconheci a *série/ano*. Exemplos: *Infantil 4*, *1º ano*, *3º ano*."
            data["grade"] = g
        elif field in {"crianca", "criança"}:
            data["child_phone"] = normalize_phone(val)
        elif field in {"pais", "pais/responsaveis"}:
            nums = [normalize_phone(x) for x in val.split(",")]
            nums = [n for n in nums if n]
            if not nums: return "Envie pelo menos *1* número de responsável no formato +55 DDD XXXXX-XXXX."
            data["guardians"] = nums[:2]
        elif field == "nome":
            data["child_name"] = val
        elif field == "idade":
            a = age_from_text(val)
            if not a: return "Idade inválida. Envie um número entre 3 e 13."
            data["age"] = a
        elif field == "domingo":
            yn = parse_yes_no(val)
            if yn is None: return "Responda *sim* ou *não* para *domingo:*"
            _schedule_init_days(data, include_sun=yn)
            st["step"] = "schedule_time"
            st["data"] = data
            return _prompt_for_next_day_time(data)
        st["data"] = data
        st["step"] = "confirm"
        return ob_summary(data)

    md = re.match(r"^\s*(seg|segunda|ter|terça|terca|qua|quarta|qui|quinta|sex|sexta|sab|sáb|sabado|sábado|dom|domingo)\s*:\s*(.+)$", text, re.I)
    if md:
        day_key = PT2KEY.get(md.group(1).lower())
        val = md.group(2).strip()
        hhmm = parse_time_hhmm(val)
        if not hhmm: return "Horário inválido. Exemplos: *19:00*, *18h30*, *7 pm*. Faixa 05:00–21:30."
        data.setdefault("schedule", {})
        data["schedule"].setdefault("days", DEFAULT_DAYS.copy())
        data["schedule"].setdefault("times", {})
        if day_key not in data["schedule"]["days"]:
            data["schedule"]["days"].append(day_key)
        data["schedule"]["times"][day_key] = hhmm
        st["data"] = data
        st["step"] = "confirm"
        return ob_summary(data)

    # Fluxo
    if step in (None, "name"):
        st["step"] = "age"
        data["child_name"] = text.strip()
        st["data"] = data
        return f"Perfeito, *{data['child_name']}*! 😊\nQuantos *anos* ela tem?"

    if step == "age":
        a = age_from_text(text)
        if not a: return "Idade inválida. Envie um número entre 3 e 13."
        data["age"] = a
        st["data"] = data
        st["step"] = "grade"
        return (
            "E em qual *série/ano* ela está?\n"
            "Escolha ou escreva:\n"
            "• Infantil 4 (Pré-I)\n• Infantil 5 (Pré-II)\n• 1º ano • 2º ano • 3º ano • 4º ano • 5º ano"
        )

    if step == "grade":
        g = parse_grade(text)
        if not g: return "Não reconheci a *série/ano*. Exemplos: *Infantil 4*, *1º ano*, *3º ano*."
        data["grade"] = g
        st["data"] = data
        st["step"] = "child_phone"
        return (
            f"{data['child_name']} tem um número próprio de WhatsApp?\n"
            "Envie no formato *+55 DDD XXXXX-XXXX* ou responda *não tem*."
        )

    if step == "child_phone":
        ph = normalize_phone(text)
        data["child_phone"] = ph
        st["data"] = data
        st["step"] = "guardians"
        return (
            "Agora, o(s) número(s) do(s) *responsável(is)* (1 ou 2), separados por vírgula.\n"
            "Ex.: +55 71 98888-7777, +55 71 97777-8888"
        )

    if step == "guardians":
        nums = [normalize_phone(x) for x in text.split(",")]
        nums = [n for n in nums if n]
        if not nums: return "Envie pelo menos *1* número de responsável no formato +55 DDD XXXXX-XXXX."
        st["data"]["guardians"] = nums[:2]
        st["step"] = "schedule_sunday"
        return (
            "Perfeito! 📅 A rotina é *segunda a sábado* por padrão.\n"
            "Deseja *incluir domingo* também? (responda *sim* ou *não*)"
        )

    if step == "schedule_sunday":
        yn = parse_yes_no(text)
        if yn is None:
            return "Responda *sim* para incluir domingo, ou *não* para manter seg–sáb."
        _schedule_init_days(data, include_sun=yn)
        st["data"] = data
        st["step"] = "schedule_time"
        return _prompt_for_next_day_time(data)

    if step == "schedule_time":
        if "schedule" not in data or not data["schedule"].get("pending_days"):
            _schedule_init_days(data, include_sun=("sun" in (data.get("schedule",{}).get("days") or [])))
        err = _set_time_for_current_day(data, text)
        if err: return err
        if data["schedule"]["pending_days"]:
            return _prompt_for_next_day_time(data)
        st["data"] = data
        st["step"] = "confirm"
        return ob_summary(data)

    if step == "confirm":
        t = text.strip().lower()
        if t == "sim":
            prof = user.setdefault("profile", {})
            prof["child_name"]  = data.get("child_name")
            prof["age"]         = data.get("age")
            prof["grade"]       = data.get("grade")
            prof["child_phone"] = data.get("child_phone")
            prof["guardians"]   = data.get("guardians", [])
            prof.setdefault("tz", "America/Bahia")
            sched = data.get("schedule", {})
            prof["schedule"] = {
                "days":  [d for d in DAY_ORDER if d in (sched.get("days") or [])],
                "times": sched.get("times", {})
            }
            # inicia currículo no dia 1
            user.setdefault("curriculum", {"math_day": 1, "total_days": 90})
            user["onboarding"] = {"step": None, "data": {}}
            return ("Maravilha! ✅ Cadastro e rotina definidos.\n"
                    "Envie *iniciar* para começar o *Dia 1* do plano.")
        elif t in {"não","nao"}:
            return ("Sem problema! Você pode corrigir assim:\n"
                    "• *nome:* Ana Souza\n• *idade:* 7\n• *serie:* 2º ano\n"
                    "• *crianca:* +55 71 91234-5678 (ou *não tem*)\n"
                    "• *pais:* +55 71 98888-7777, +55 71 97777-8888\n"
                    "• *domingo:* sim/não\n"
                    "• *seg:* 16:00  • *ter:* 17:00  • *qua:* 18:30  • *qui:* 19:00  • *sex:* 19:00  • *sáb:* 10:00  • *dom:* 16:00")
        else:
            return "Responda *sim* para salvar, ou *não* para ajustar."

    st["step"] = None
    return ob_start()

# ------------------- Web -------------------
@app.route("/admin/ping")
def ping():
    return jsonify({"project": PROJECT_NAME, "ok": True}), 200

def _curriculum_phase_title(day_idx: int) -> str:
    spec = _curriculum_spec(day_idx)
    return spec["phase"]

@app.route("/bot", methods=["POST"])
def bot_webhook():
    payload = request.form or request.json or {}
    user_id = str(payload.get("From") or payload.get("user_id") or "debug-user")
    text = (payload.get("Body") or payload.get("text") or "").strip()
    low = text.lower()

    db = load_db()
    user = init_user_if_needed(db, user_id)
    user.setdefault("pending", {})
    user.setdefault("profile", {})
    user.setdefault("onboarding", {"step": None, "data": {}})
    user.setdefault("curriculum", {"math_day": 1, "total_days": 90})

    # -------- Onboarding primeiro --------
    if needs_onboarding(user):
        st = user["onboarding"]
        if st["step"] is None:
            st["step"] = "name"
            db["users"][user_id] = user; save_db(db)
            return reply_twiml(ob_start())
        reply = ob_step(user, text)
        db["users"][user_id] = user; save_db(db)
        return reply_twiml(reply)

    # -------- Comandos gerais --------
    if low in {"menu", "ajuda", "help"}:
        day = int(user.get("curriculum",{}).get("math_day",1))
        phase = _curriculum_phase_title(day)
        sched = user.get("profile", {}).get("schedule", {})
        reply = (
            "📚 *Assistente de Aula*\n"
            f"📆 *Plano Matemática:* Dia {day}/90 — {phase}\n"
            f"🗓️ Rotina: {describe_schedule(sched)}\n\n"
            "Fluxo do dia:\n"
            "1) Matemática (10 itens — responda por vírgula)\n"
            "2) Português (1 questão)\n"
            "3) Leitura (meta do dia)\n\n"
            "Comandos:\n"
            "- *iniciar*: começa a sessão do dia\n"
            "- *resposta X* ou apenas *X*: responde à etapa atual\n"
            "- *leitura ok*: confirma leitura do dia\n"
            "- *status*: mostra progresso\n"
        )
        return reply_twiml(reply)

    if low == "status":
        day = int(user.get("curriculum",{}).get("math_day",1))
        phase = _curriculum_phase_title(day)
        sched = user.get("profile", {}).get("schedule", {})
        reply = (
            f"👤 Níveis — MAT:{user['levels']['matematica']} | PORT:{user['levels']['portugues']}\n"
            f"📈 Feitas — MAT:{len(user['history']['matematica'])} | "
            f"PORT:{len(user['history']['portugues'])} | LEIT:{len(user['history']['leitura'])}\n"
            f"📆 *Plano Matemática:* Dia {day}/90 — {phase}\n"
            f"🗓️ Rotina: {describe_schedule(sched)}"
        )
        return reply_twiml(reply)

    if low == "iniciar":
        # Se já há pendência de matemática, reapresenta a lista
        if "mat_lote" in user.get("pending", {}):
            batch = user["pending"]["mat_lote"]
            db["users"][user_id] = user; save_db(db)
            return reply_twiml(_format_math_prompt(batch))
        # Gera sessão do dia atual
        day = int(user.get("curriculum",{}).get("math_day",1))
        spec = _curriculum_spec(day)
        batch = _build_batch_from_spec(spec)
        user["pending"]["mat_lote"] = batch
        db["users"][user_id] = user; save_db(db)
        return reply_twiml(_format_math_prompt(batch))

    if low.startswith("leitura ok"):
        ok, msg = check_reading_submission(user)
        db["users"][user_id] = user; save_db(db)
        return reply_twiml(msg)

    # -------- Respostas do fluxo --------
    # 1) Matemática (lote CSV)
    if "mat_lote" in user.get("pending", {}):
        raw = text
        if low.startswith("resposta"):
            raw = text[len("resposta"):].strip()
            if not raw and " " in text:
                raw = text.split(" ", 1)[1].strip()
            raw = raw.lstrip(":.-").strip() or raw
        ok, msg = _check_math_batch(user, raw)
        if not ok:
            db["users"][user_id] = user; save_db(db)
            return reply_twiml(msg)
        # sucesso → Português
        port_msg = _start_portugues(user)
        db["users"][user_id] = user; save_db(db)
        return reply_twiml(msg + "\n\n" + port_msg)

    # 2) Português (1 questão) — aceita "resposta X" OU só "X"
    if "portugues" in user.get("pending", {}):
        ans = text
        if low.startswith("resposta"):
            after = text[len("resposta"):].strip()
            if not after and " " in text:
                after = text.split(" ", 1)[1].strip()
            ans = (after.lstrip(":.-").strip() or after or ans)
        result_txt = check_answer(user, ans)
        if "portugues" not in user.get("pending", {}):
            leitura_msg = _start_leitura(user)
            db["users"][user_id] = user; save_db(db)
            return reply_twiml(result_txt + "\n\n" + leitura_msg)
        db["users"][user_id] = user; save_db(db)
        return reply_twiml(result_txt)

    # 3) Nada pendente
    return reply_twiml("Envie *iniciar* para começar a sessão do dia (Matemática → Português → Leitura).")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
