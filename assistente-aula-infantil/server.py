# assistente-aula-infantil/server.py
# Assistente Educacional — Check-in diário (imediato e +3h)
# Flask + Twilio. Webhook: POST /bot | Cron: GET /admin/cron
import os
import re
import itertools
from typing import Any, Dict, Optional, Tuple, List
from datetime import datetime, timedelta, time as dtime

from flask import Flask, request, Response, jsonify

# Persistência simples (JSON). Mantém compatibilidade com projeto.
from storage import load_db, save_db

# Opcional: inicialização de usuário se você já usa isso no projeto.
try:
    from progress import init_user_if_needed  # type: ignore
except Exception:
    def init_user_if_needed(db: Dict[str, Any], user_key: str) -> None:
        pass

# Twilio: resposta imediata (inbound) e envio proativo (REST)
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
# Ex.: "whatsapp:+14155238886" (número sandbox ou número validado)
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

def _parse_hhmm(s: str) -> Optional[dtime]:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", s or "")
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return dtime(hour=hh, minute=mm, second=0)
    return None

def _combine_date_time(date_dt: datetime, hhmm: dtime) -> datetime:
    tz = date_dt.tzinfo
    return datetime(
        year=date_dt.year, month=date_dt.month, day=date_dt.day,
        hour=hhmm.hour, minute=hhmm.minute, second=0, tzinfo=tz
    )

# ===========================
# DB layout e acesso a usuário
# ===========================
def _db() -> Dict[str, Any]:
    d = load_db()
    d.setdefault("users", {})
    return d

def _save(d: Dict[str, Any]) -> None:
    save_db(d)

def _default_schedule() -> Dict[str, Optional[str]]:
    # Seg–Sáb às 19:00; Dom sem lembrete por padrão (pode ligar depois)
    return {
        "mon": "19:00", "tue": "19:00", "wed": "19:00",
        "thu": "19:00", "fri": "19:00", "sat": "19:00",
        "sun": None
    }

def _get_or_create_user(d: Dict[str, Any], sender: str) -> Tuple[str, Dict[str, Any]]:
    key = _digits_only(sender)
    users = d["users"]
    # 1) se já existe diretamente pela chave
    if key in users:
        return key, users[key]
    # 2) tentar achar pelo número do filho ou guardiões
    for k, user in users.items():
        prof = (user.get("profile") or {})
        if _numbers_match(sender, prof.get("child_phone")):
            return k, user
        for g in (prof.get("guardians") or []):
            if _numbers_match(sender, g):
                return k, user
    # 3) criar novo usuário
    user = {
        "profile": {
            "timezone": PROJECT_TZ,
            "child_phone": None,
            "guardians": [sender],
            "child_name": None,
        },
        "schedule": _default_schedule(),
        "daily_state": {},  # YYYY-MM-DD -> {done, done_ts, done_notified, miss_notified}
    }
    users[key] = user
    return key, user

def _is_from_guardian(sender: str, user: Dict[str, Any]) -> bool:
    prof = (user.get("profile") or {})
    for g in (prof.get("guardians") or []):
        if _numbers_match(sender, g):
            return True
    return False

def _is_from_child(sender: str, user: Dict[str, Any]) -> bool:
    prof = (user.get("profile") or {})
    child = prof.get("child_phone")
    if child:
        return _numbers_match(sender, child)
    # Fallback: se não tem child e também não é guardião, tratamos como "child"
    return not _is_from_guardian(sender, user)

def _guardians(user: Dict[str, Any]) -> List[str]:
    return list((user.get("profile") or {}).get("guardians") or [])

# ================
# Notificações SMS
# ================
def _send_whatsapp(to_number: str, body: str) -> None:
    # Se TWILIO_FROM estiver vazio, não envia (modo dev)
    if not TWILIO_FROM or not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return
    client = _get_twilio()
    # Normaliza para formato "whatsapp:+<countrycode...>" se já vier com "whatsapp:" mantém
    to_fmt = to_number if to_number.startswith("whatsapp:") else f"whatsapp:+{_digits_only(to_number)}"
    client.messages.create(from_=TWILIO_FROM, to=to_fmt, body=body)

def _notify_done(user: Dict[str, Any], day_key: str, late: bool = False) -> None:
    name = ((user.get("profile") or {}).get("child_name") or "A criança")
    if late:
        msg = f"✅ {name} concluiu agora as atividades de hoje. Obrigado pelo acompanhamento!"
    else:
        msg = f"✅ {name} concluiu as atividades de hoje (Mat/Port{'/Leitura' if FEATURE_LEITURA else ''}). Bom trabalho!"
    for g in _guardians(user):
        _send_whatsapp(g, msg)

def _notify_miss(user: Dict[str, Any], day_key: str) -> None:
    name = ((user.get("profile") or {}).get("child_name") or "A criança")
    msg = f"⚠️ {name} ainda não concluiu as atividades de hoje. Precisa de ajuda para finalizar?"
    for g in _guardians(user):
        _send_whatsapp(g, msg)

# ======================
# Lógica de Check-in Dia
# ======================
def _get_day_state(user: Dict[str, Any], day_key: str) -> Dict[str, Any]:
    ds = user.setdefault("daily_state", {})
    st = ds.setdefault(day_key, {})
    st.setdefault("done", False)
    st.setdefault("done_ts", None)
    st.setdefault("done_notified", False)
    st.setdefault("miss_notified", False)
    return st

def mark_day_done(user: Dict[str, Any], when: Optional[datetime] = None) -> Tuple[str, Dict[str, Any]]:
    """Marca o dia corrente como concluído (idempotente) e notifica guardiões se preciso."""
    when = when or _now()
    day_key = _today_str(when)
    st = _get_day_state(user, day_key)
    already = st["done"]
    st["done"] = True
    if not st["done_ts"]:
        st["done_ts"] = when.isoformat()

    # Notificação imediata de "Fez", caso ainda não tenha ido
    if not st.get("done_notified", False):
        # Se já havia "falta" enviada, usa mensagem de recuperação
        late = bool(st.get("miss_notified", False))
        _notify_done(user, day_key, late=late)
        st["done_notified"] = True
    return day_key, st

def _get_today_reminder_dt(user: Dict[str, Any], base_dt: Optional[datetime] = None) -> Optional[datetime]:
    base_dt = base_dt or _now()
    sched = user.get("schedule") or {}
    key = _weekday_key(base_dt)
    hhmm = sched.get(key)
    if not hhmm:
        return None
    t = _parse_hhmm(hhmm)
    if not t:
        return None
    return _combine_date_time(base_dt, t)

def process_checkin_cron(user: Dict[str, Any], now_dt: Optional[datetime] = None) -> Optional[str]:
    """Regra: +3h do horário de lembrete do dia → se não concluiu, notifica falta (1x).
       Se concluir depois, a marcação de done dispara um 'concluiu agora'.
    """
    now_dt = now_dt or _now()
    day_key = _today_str(now_dt)
    st = _get_day_state(user, day_key)
    rem_dt = _get_today_reminder_dt(user, base_dt=now_dt)

    # Sem agendamento hoje → nada a fazer
    if rem_dt is None:
        return "skip:no-schedule"

    deadline = rem_dt + timedelta(hours=3)
    # Se já concluiu, garantir "done_notified" (idempotente)
    if st["done"]:
        if not st.get("done_notified", False):
            _notify_done(user, day_key, late=bool(st.get("miss_notified", False)))
            st["done_notified"] = True
            return "sent:done"
        return "skip:already-done-notified"

    # Não concluiu: se passou o deadline e ainda não notificou falta
    if now_dt >= deadline and not st.get("miss_notified", False):
        _notify_miss(user, day_key)
        st["miss_notified"] = True
        return "sent:miss"

    return "skip:not-due"

# ======================
# Mensagens e Comandos
# ======================
WELCOME = (
    "Olá! Eu sou a MARIA ANGELA 👋\n"
    "Vamos iniciar as atividades diárias de Matemática e Português "
    f"{'(e Leitura)' if FEATURE_LEITURA else ''}.\n"
    "Comandos: \n"
    "- *iniciar*: cria/ajusta seu cadastro e agenda padrão seg–sáb 19:00\n"
    "- *status*: mostra a situação de hoje e o horário de lembrete\n"
    "- *fim*: marca o dia como concluído (teste)\n"
)

def _status_text(user: Dict[str, Any]) -> str:
    now_dt = _now()
    day_key = _today_str(now_dt)
    st = _get_day_state(user, day_key)
    rem_dt = _get_today_reminder_dt(user, base_dt=now_dt)
    rem = rem_dt.strftime("%H:%M") if rem_dt else "—"
    return (
        f"📊 Status {day_key}\n"
        f"- Feito: {'sim' if st['done'] else 'não'}\n"
        f"- Lembrete do dia: {rem}\n"
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
    body = (request.values.get("Body", "") or "").strip().lower()

    user_key, user = _get_or_create_user(d, from_raw)
    init_user_if_needed(d, user_key)  # no-op se não existir

    resp = MessagingResponse()
    msg = resp.message()

    # Comandos mínimos p/ teste e operação
    if body in ("iniciar", "start"):
        # Garante agenda padrão e dá boas-vindas
        user.setdefault("schedule", _default_schedule())
        user.setdefault("profile", {}).setdefault("timezone", PROJECT_TZ)
        _save(d)
        msg.body(WELCOME)
        return Response(str(resp), mimetype="application/xml")

    if body in ("status", "debug status", "s"):
        msg.body(_status_text(user))
        return Response(str(resp), mimetype="application/xml")

    if body in ("fim", "finalizar", "concluir", "fechar dia"):
        mark_day_done(user, when=_now())
        _save(d)
        msg.body("✅ Dia marcado como concluído. Aviso enviado aos responsáveis.")
        return Response(str(resp), mimetype="application/xml")

    # Aqui entraria seu fluxo real (MAT → PT → [LEITURA]).
    # Ao finalizar o pipeline do dia, *obrigatoriamente* chamar:
    #   mark_day_done(user)
    # e depois _save(d).
    # Para este MVP, só respondemos ajuda:
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
        # Se dry-run, apenas calcula o que *faria*.
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
    # Versão sem side-effects (para /admin/cron?dry=1)
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
