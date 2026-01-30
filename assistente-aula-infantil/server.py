# PATH: assistente-aula-infantil/server.py
# LAST_RECODE: 2026-01-29 22:45 America/Bahia
# MOTIVO: Priorizar wizard antes de comandos/menu; evitar conflito de respostas numericas com menu; corrigir fluxo de cadastro.

import os
import re
import random
from typing import Any, Dict, Optional, Tuple, List
from datetime import datetime, timedelta, time as dtime

from flask import Flask, request, Response, jsonify

from storage import load_db, save_db

try:
    from progress import init_user_if_needed  # type: ignore
except Exception:
    def init_user_if_needed(db: Dict[str, Any], user_key: str) -> None:
        pass

from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None

app = Flask(__name__)

FEATURE_PORTUGUES = os.getenv("FEATURE_PORTUGUES", "True") == "True"
FEATURE_LEITURA = os.getenv("FEATURE_LEITURA", "False") == "True"
AUTO_SEQUENCE_PT_AFTER_MATH = os.getenv("AUTO_SEQUENCE_PT_AFTER_MATH", "True") == "True"
ROUNDS_PER_DAY = int(os.getenv("ROUNDS_PER_DAY", "5"))
MAX_MATH_DAY = int(os.getenv("MAX_MATH_DAY", "60"))
MAX_PT_DAY = int(os.getenv("MAX_PT_DAY", "60"))

PROJECT_TZ = os.getenv("PROJECT_TZ", "America/Bahia")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.getenv("TWILIO_FROM", "")

_twilio_client: Optional[Client] = None
def _get_twilio() -> Client:
    global _twilio_client
    if _twilio_client is None:
        _twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _twilio_client

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
    if len(d) < 2: return "-"
    return f"+{d[:-2]}**{d[-2:]}"

def _parse_phones_list(s: str) -> List[str]:
    parts = [p.strip() for p in (s or "").replace(" e ", ",").split(",") if p.strip()]
    out: List[str] = []
    for p in parts:
        d = _digits_only(p)
        if d:
            out.append(d)
    return out[:2]

def _yes_no(body: str) -> Optional[bool]:
    b = (body or "").strip().lower()
    if b in ("1", "s", "sim", "yes", "y"): return True
    if b in ("2", "n", "nao", "não", "no"): return False
    return None

def _is_ok(body: str) -> bool:
    b = (body or "").strip().lower()
    return b in ("ok", "ok!", "ok.", "okay", "okey")

def _db() -> Dict[str, Any]:
    d = load_db()
    d.setdefault("users", {})
    return d

def _save(d: Dict[str, Any]) -> None:
    save_db(d)

GRADES = [
    "Infantil 4 (Pre-I)",
    "Infantil 5 (Pre-II)",
    "1o ano",
    "2o ano",
    "3o ano",
    "4o ano",
    "5o ano",
]

SCHEDULE_ORDER: List[Tuple[str, str]] = [
    ("mon","seg"),("tue","ter"),("wed","qua"),("thu","qui"),("fri","sex"),("sat","sab"),("sun","dom")
]

def _default_schedule() -> Dict[str, Optional[str]]:
    return {k: ("19:00" if k != "sun" else None) for k,_ in SCHEDULE_ORDER}

def _get_or_create_user(d: Dict[str, Any], sender: str) -> Tuple[str, Dict[str, Any]]:
    key = _digits_only(sender)
    users: Dict[str, Dict[str, Any]] = d["users"]
    if key in users:
        return key, users[key]
    for k, user in users.items():
        prof = (user.get("profile") or {})
        if _numbers_match(sender, prof.get("child_phone")):
            return k, user
        for g in (prof.get("guardians") or []):
            if _numbers_match(sender, g):
                return k, user
    user: Dict[str, Any] = {
        "profile": {
            "timezone": PROJECT_TZ,
            "child_phone": None,
            "guardians": [sender],
            "child_name": None,
            "child_age": None,
            "grade": None,
        },
        "schedule": _default_schedule(),
        "daily_state": {},
        "wizard": None,
        "lesson": None,
    }
    users[key] = user
    return key, user

def _send_whatsapp(to_number: str, body: str) -> None:
    if not TWILIO_FROM or not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return
    client = _get_twilio()
    to_fmt = to_number if to_number.startswith("whatsapp:") else f"whatsapp:+{_digits_only(to_number)}"
    client.messages.create(from_=TWILIO_FROM, to=to_fmt, body=body)

def _notify_done(user: Dict[str, Any], day_key: str, late: bool = False) -> None:
    name = ((user.get("profile") or {}).get("child_name") or "A crianca")
    if late:
        msg = f"{name} concluiu agora as atividades de hoje."
    else:
        msg = f"{name} concluiu as atividades de hoje (Mat/Port{'/Leitura' if FEATURE_LEITURA else ''})."
    for g in (user.get("profile") or {}).get("guardians", []) or []:
        _send_whatsapp(g, msg)

def _notify_miss(user: Dict[str, Any], day_key: str) -> None:
    name = ((user.get("profile") or {}).get("child_name") or "A crianca")
    msg = f"{name} ainda nao concluiu as atividades de hoje. Precisa de ajuda?"
    for g in (user.get("profile") or {}).get("guardians", []) or []:
        _send_whatsapp(g, msg)

def _get_day_state(user: Dict[str, Any], day_key: str) -> Dict[str, Any]:
    ds = user.setdefault("daily_state", {})
    st = ds.setdefault(day_key, {})
    st.setdefault("done", False)
    st.setdefault("done_ts", None)
    st.setdefault("done_notified", False)
    st.setdefault("miss_notified", False)
    return st

def mark_day_done(user: Dict[str, Any], when: Optional[datetime] = None) -> Tuple[str, Dict[str, Any]]:
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

def _build_math_question(op: Optional[str] = None) -> Dict[str, Any]:
    if op == "mix" or op is None:
        op = random.choice(["+", "-", "*", "/"])

    if op == "+":
        a, b = random.randint(2, 9), random.randint(2, 9)
        correct = a + b
        prompt = f"Quanto e {a} + {b}?"
        corpus = list(range(max(0, correct - 4), correct + 5))
        corpus = [x for x in corpus if x >= 0]
    elif op == "-":
        a, b = random.randint(2, 9), random.randint(2, 9)
        if b > a: a, b = b, a
        correct = a - b
        prompt = f"Quanto e {a} - {b}?"
        corpus = list(range(max(0, correct - 4), correct + 5))
    elif op == "*":
        a, b = random.randint(2, 9), random.randint(2, 9)
        correct = a * b
        prompt = f"Quanto e {a} x {b}?"
        corpus = [correct + d for d in (-6,-4,-3,-2,-1,1,2,3,4,6) if correct + d > 0]
    elif op == "/":
        b = random.randint(2, 9)
        q = random.randint(2, 9)
        a = b * q
        correct = q
        prompt = f"Quanto e {a} / {b}?"
        corpus = [max(1, correct + d) for d in (-3,-2,-1,1,2,3)]
    else:
        a, b = 2, 2
        correct = 4
        prompt = "Quanto e 2 + 2?"
        corpus = [1,2,3,4,5,6]

    opts = {correct}
    random.shuffle(corpus)
    for v in corpus:
        if len(opts) >= 4: break
        if v != correct: opts.add(v)
    options = list(opts)
    random.shuffle(options)
    answer_idx = options.index(correct)
    return {
        "type": "math",
        "op": op,
        "prompt": prompt,
        "options": [str(x) for x in options],
        "answer": answer_idx
    }

def _build_pt_question() -> Dict[str, Any]:
    qs = [
        ("Qual esta escrito corretamente?", ["Excecao", "Excessao", "Ececao", "Ecessao"], 0),
        ("Qual plural esta correto para pao?", ["paos", "paes", "paoses", "paeses"], 1),
        ("Qual forma esta correta?", ["A gente vamos", "A gente vai", "Nos vai", "Nos vamos ir"], 1),
        ("Complete: Ela ___ ao mercado ontem.", ["vai", "foi", "iria", "vou"], 1),
    ]
    prompt, options, ans = random.choice(qs)
    return {"type": "pt", "prompt": prompt, "options": options, "answer": ans}

def _options_with_letters(options: List[str]) -> str:
    letters = ["a","b","c","d"]
    return "\n".join([f"{letters[i]}) {options[i]}" for i in range(min(4, len(options)))])

def _choice_to_index(body: str) -> Optional[int]:
    m = re.match(r"^\s*([a-dA-D])\s*$", (body or "").strip())
    if not m:
        return None
    letters = ["a","b","c","d"]
    return letters.index(m.group(1).lower())

def _start_lesson(user: Dict[str, Any]) -> str:
    ops_order = ["+", "-", "*", "/", "mix"]
    qts: List[Dict[str, Any]] = [_build_math_question(op) for op in ops_order]
    if FEATURE_PORTUGUES and AUTO_SEQUENCE_PT_AFTER_MATH:
        qts.append(_build_pt_question())
    user["lesson"] = {"idx": 0, "q": qts, "hits": 0, "tries": {}}
    return _present_current_question(user)

def _present_current_question(user: Dict[str, Any]) -> str:
    les = user.get("lesson") or {}
    idx = int(les.get("idx", 0))
    qts: List[Dict[str, Any]] = les.get("q") or []
    if idx >= len(qts):
        return _finish_lesson(user)

    q = qts[idx]
    tries = int((les.get("tries") or {}).get(idx, 0))

    header = "PORTUGUES" if q.get("type") == "pt" else "MATEMATICA"

    hint = ""
    if tries >= 2 and q.get("type") != "pt":
        hint = "\nDica: revise a conta com calma."

    opts = _options_with_letters(q["options"])
    return f"{header}\n{q['prompt']}{hint}\nResponda com a, b, c ou d:\n{opts}"

def _apply_answer(user: Dict[str, Any], body: str) -> str:
    les = user.get("lesson") or {}
    idx = int(les.get("idx", 0))
    qts: List[Dict[str, Any]] = les.get("q") or []
    if not qts or idx >= len(qts):
        return "Nao ha aula em andamento. Digite comecar aula."

    choice = _choice_to_index(body)
    if choice is None:
        return "Responda apenas com a, b, c ou d."

    q = qts[idx]
    correct_idx = int(q["answer"])

    if choice == correct_idx:
        les["hits"] = int(les.get("hits", 0)) + 1
        tries_map: Dict[int, int] = les.setdefault("tries", {})
        tries_map.pop(idx, None)
        les["idx"] = idx + 1
        user["lesson"] = les
        return _present_current_question(user)

    tries_map2: Dict[int, int] = les.setdefault("tries", {})
    t = int(tries_map2.get(idx, 0)) + 1
    tries_map2[idx] = t
    user["lesson"] = les

    if t >= 3:
        letters = ["a", "b", "c", "d"]
        correct_val = q["options"][correct_idx]
        les["idx"] = idx + 1
        tries_map2.pop(idx, None)
        user["lesson"] = les
        return f"Nao foi dessa vez. A correta era {letters[correct_idx]}) {correct_val}.\n" + _present_current_question(user)

    return "Tente novamente.\n" + _present_current_question(user)

def _finish_lesson(user: Dict[str, Any]) -> str:
    les = user.get("lesson") or {}
    total = len(les.get("q") or [])
    hits = int(les.get("hits", 0))
    user["lesson"] = None
    mark_day_done(user, when=_now())
    return f"Aula concluida. Acertos: {hits}/{total}.\nQuer ver o status do dia?"

def _start_wizard(user: Dict[str, Any]) -> str:
    user["wizard"] = {"step": "ask_name", "tmp": {}}
    return (
        "Oi! Eu sou a MARIA ANGELA, sua assistente de aula.\n"
        "Vou te acompanhar em atividades de Matematica, Portugues.\n\n"
        "Pra comecar, me diga: qual e o nome da crianca?"
    )

def _wizard_prompt_grade() -> str:
    opts = "\n".join([f"{i+1}) {GRADES[i]}" for i in range(len(GRADES))])
    return ("E em qual serie/ano ela esta?\n"
            "Responda o numero ou escreva:\n" + opts)

def _wizard_prompt_yesno_domingo() -> str:
    return ("Perfeito! A rotina e segunda a sabado por padrao.\n"
            "Deseja incluir domingo tambem?\n"
            "1) sim   2) nao   (ou responda ok para nao)")

def _wizard_prompt_time_for(day_pt: str) -> str:
    return (f"Qual horario para {day_pt}? (faixa 05:00-21:30)\n"
            "Responda o numero ou o horario:\n"
            "1) 08:00   2) 18:30   3) 19:00   4) 20:00   5) outro   (ou ok para 19:00)")

def _wizard_confirm(user: Dict[str, Any], tmp: Dict[str, Any]) -> str:
    child = tmp.get("child_name") or (user.get("profile") or {}).get("child_name") or "-"
    age = tmp.get("child_age") or (user.get("profile") or {}).get("child_age") or "-"
    grade = tmp.get("grade") or (user.get("profile") or {}).get("grade") or "-"
    cphone = tmp.get("child_phone") or (user.get("profile") or {}).get("child_phone")
    guards = tmp.get("guardians") or (user.get("profile") or {}).get("guardians") or []
    sched = tmp.get("schedule") or user.get("schedule") or {}
    parts = []
    for k, pt in SCHEDULE_ORDER:
        v = sched.get(k)
        if v:
            parts.append(f"{pt} {v}")
    rotina = " | ".join(parts) if parts else "-"
    return (
        "Confere?\n"
        f"Nome: {child}\n"
        f"Idade: {age}\n"
        f"Serie: {grade}\n"
        f"WhatsApp da crianca: {_mask_phone(cphone)}\n"
        f"Responsavel(is): {', '.join(_mask_phone(g) for g in guards) or '-'}\n"
        f"Rotina: {rotina}\n"
        "Responda sim para salvar, ou nao para ajustar. (ou ok para salvar)"
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
            return "Digite um nome valido (min. 2 letras). Qual e o nome da crianca?"
        tmp["child_name"] = name
        wz["step"] = "ask_age"
        return f"Perfeito, {name}!\nQuantos anos ela tem?"

    if step == "ask_age":
        m = re.match(r"^\s*(\d{1,2})\s*$", body)
        if not m:
            return "Me diga um numero (ex.: 9). Quantos anos ela tem?"
        age = int(m.group(1))
        if not (3 <= age <= 17):
            return "Idade fora do padrao (3-17). Tente novamente."
        tmp["child_age"] = age
        wz["step"] = "ask_grade"
        return _wizard_prompt_grade()

    if step == "ask_grade":
        n = re.match(r"^\s*(\d{1,2})\s*$", body)
        if n:
            idx = int(n.group(1)) - 1
            if 0 <= idx < len(GRADES):
                tmp["grade"] = GRADES[idx]
            else:
                return _wizard_prompt_grade()
        else:
            txt = body.strip().lower()
            chosen = None
            for g in GRADES:
                if txt in g.lower():
                    chosen = g
                    break
            if not chosen:
                return _wizard_prompt_grade()
            tmp["grade"] = chosen
        wz["step"] = "ask_child_whatsapp"
        return ("A crianca tem um numero proprio de WhatsApp?\n"
                "Envie no formato +55 DDD XXXXX-XXXX ou responda nao tem (ou ok para nao tem).")

    if step == "ask_child_whatsapp":
        b = body.strip().lower()
        if _is_ok(b) or "nao tem" in b or b in ("nao", "não", "n"):
            tmp["child_phone"] = None
        else:
            d = _digits_only(body)
            if not d:
                return "Envie o WhatsApp da crianca no formato +55 DDD XXXXX-XXXX ou responda nao tem."
            tmp["child_phone"] = d
        wz["step"] = "ask_guardians"
        return ("Agora, o(s) numero(s) do(s) responsavel(is) (1 ou 2), separados por virgula.\n"
                "Ex.: +55 71 98888-7777, +55 71 97777-8888\n"
                "(ou responda ok para manter so o seu numero)")

    if step == "ask_guardians":
        if _is_ok(body):
            sender = (user.get("profile") or {}).get("guardians", [None])[0]
            tmp["guardians"] = [g for g in [sender] if g]
        else:
            gs = _parse_phones_list(body)
            sender = (user.get("profile") or {}).get("guardians", [])[0]
            if sender and _digits_only(sender) not in [_digits_only(x) for x in gs]:
                gs = [sender] + gs
            tmp["guardians"] = list(dict.fromkeys(gs))[:2]
        wz["step"] = "ask_sunday"
        return _wizard_prompt_yesno_domingo()

    if step == "ask_sunday":
        if _is_ok(body):
            yn = False
        else:
            yn = _yes_no(body)
            if yn is None:
                return _wizard_prompt_yesno_domingo()
        tmp.setdefault("schedule", _default_schedule())
        tmp["schedule"]["sun"] = tmp["schedule"]["sun"] if yn else None
        wz["step"] = "ask_time_mon"
        return _wizard_prompt_time_for("seg")

    def _handle_time_for(day_key: str, day_pt: str, next_step: str) -> str:
        s = body.strip()
        if _is_ok(s):
            t = _parse_hhmm_strict("19:00")
        else:
            choice = re.match(r"^\s*([1-5])\s*$", s)
            if choice:
                c = int(choice.group(1))
                mapping = {1:"08:00", 2:"18:30", 3:"19:00", 4:"20:00"}
                if c in (1,2,3,4):
                    val = mapping[c]
                    t = _parse_hhmm_strict(val)
                else:
                    return "Digite o horario desejado (ex.: 18:30, 19h, 7 pm)."
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
        if next_step == "ask_time_sat":  return _wizard_prompt_time_for("sab")
        return _wizard_confirm(user, tmp)

    if step == "ask_time_mon": return _handle_time_for("mon","seg","ask_time_tue")
    if step == "ask_time_tue": return _handle_time_for("tue","ter","ask_time_wed")
    if step == "ask_time_wed": return _handle_time_for("wed","qua","ask_time_thu")
    if step == "ask_time_thu": return _handle_time_for("thu","qui","ask_time_fri")
    if step == "ask_time_fri": return _handle_time_for("fri","sex","ask_time_sat")
    if step == "ask_time_sat": return _handle_time_for("sat","sab","confirm")

    if step == "confirm":
        yn: Optional[bool]
        if _is_ok(body):
            yn = True
        else:
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
        return "Cadastro salvo. Use status para ver a rotina do dia, ou escreva comecar aula quando quiser iniciar."

    return None

WELCOME = (
    "Ola! Eu sou a MARIA ANGELA.\n"
    "Posso acompanhar as atividades diarias de Matematica e Portugues"
    f"{' e Leitura' if FEATURE_LEITURA else ''}.\n\n"
    "Escolha uma opcao:\n"
    "a) iniciar   b) status   c) comecar aula   d) #resetar\n"
    "(ou digite os comandos normalmente)"
)

def _status_text(user: Dict[str, Any]) -> str:
    now_dt = _now()
    day_key = _today_str(now_dt)
    st = _get_day_state(user, day_key)
    rem_dt = _get_today_reminder_dt(user, base_dt=now_dt)
    rem = rem_dt.strftime("%H:%M") if rem_dt else "-"
    dia_map = dict(SCHEDULE_ORDER)
    dia = dia_map.get(_weekday_key(now_dt), "-")
    in_lesson = "sim" if user.get("lesson") else "nao"
    return (
        f"Status {day_key}\n"
        f"- Feito: {'sim' if st['done'] else 'nao'}\n"
        f"- Lembrete de hoje ({dia}): {rem}\n"
        f"- Aula em andamento: {in_lesson}\n"
        f"- Notif. feito: {'sim' if st.get('done_notified') else 'nao'}\n"
        f"- Notif. falta: {'sim' if st.get('miss_notified') else 'nao'}"
    )

@app.post("/bot")
def bot() -> Response:
    d = _db()
    from_raw = request.values.get("From", "")
    body = (request.values.get("Body", "") or "").strip()
    lower = body.lower()

    user_key, user = _get_or_create_user(d, from_raw)
    init_user_if_needed(d, user_key)

    resp = MessagingResponse()
    msg = resp.message()

    # Multi-escolha global (só quando NAO estiver em wizard nem em aula)
    if not user.get("wizard") and not user.get("lesson"):
        if lower in ("a",): lower = "iniciar"
        if lower in ("b",): lower = "status"
        if lower in ("c",): lower = "começar aula"
        if lower in ("d",): lower = "#resetar"

    if lower in ("#resetar", "resetar", "#reset", "reset"):
        d["users"].pop(user_key, None)
        _save(d)
        msg.body("Tudo zerado. Digite iniciar para comecar do zero.")
        return Response(str(resp), mimetype="application/xml")

    if lower in ("reiniciar cadastro", "reset cadastro", "recomeçar cadastro", "recomecar cadastro"):
        user["wizard"] = None
        _save(d)
        msg.body(_start_wizard(user))
        return Response(str(resp), mimetype="application/xml")

    # Wizard tem prioridade total
    if user.get("wizard"):
        out = _handle_wizard(user, body)
        if out:
            msg.body(out)
            _save(d)
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
        msg.body("Dia marcado como concluido. Aviso enviado aos responsaveis.")
        return Response(str(resp), mimetype="application/xml")

    if lower in ("cancelar aula", "cancelar", "parar aula"):
        user["lesson"] = None
        _save(d)
        msg.body("Aula cancelada. Quando quiser retomar, envie comecar aula.")
        return Response(str(resp), mimetype="application/xml")

    if lower in ("começar aula", "comecar aula", "iniciar aula", "aula", "começar", "comecar"):
        if user.get("lesson"):
            msg.body(_present_current_question(user))
        else:
            msg.body(_start_lesson(user))
        _save(d)
        return Response(str(resp), mimetype="application/xml")

    if user.get("lesson"):
        msg.body(_apply_answer(user, body))
        _save(d)
        return Response(str(resp), mimetype="application/xml")

    msg.body(WELCOME)
    _save(d)
    return Response(str(resp), mimetype="application/xml")

@app.get("/admin/cron")
def cron() -> Response:
    d = _db()
    dry = request.args.get("dry", "0") in ("1", "true", "True")
    now_dt = _now()

    results: List[Tuple[str, str]] = []
    for k, user in list((d.get("users") or {}).items()):
        tag = process_checkin_cron(user, now_dt)
        results.append((k, tag or "skip"))

    if not dry:
        _save(d)
    return jsonify({
        "now": now_dt.isoformat(),
        "dry_run": dry,
        "results": [{"user": k, "result": r} for k, r in results]
    })

@app.get("/healthz")
def healthz() -> Response:
    return jsonify({"ok": True, "tz": PROJECT_TZ, "time": _now().isoformat()})