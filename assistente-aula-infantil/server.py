# server.py — Assistente de Aula Infantil
# Onboarding da "MARIA ANGELA" + fluxo sequencial: Matemática (lote CSV) → Português → Leitura
import os, re
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
    """Normaliza para E.164. Aceita números com/sem +55, com espaços/traços. Retorna None se inválido."""
    if not s:
        return None
    x = re.sub(r"[^\d+]", "", s).strip()
    if x.lower() in {"nao tem", "não tem", "naotem"}:
        return None
    if x.startswith("+"):
        digits = re.sub(r"\D", "", x)
        return f"+{digits}"
    # sem +: assume Brasil
    digits = re.sub(r"\D", "", x)
    # aceita DDD + 8, 9 ou 10+ dígitos (celular geralmente 11 com nono dígito)
    if 10 <= len(digits) <= 12:
        return f"+{BR_DEFAULT_CC}{digits}"
    return None

def mask_phone(p: str | None) -> str:
    if not p:
        return "não tem"
    d = re.sub(r"\D", "", p)
    if len(d) < 4:
        return p
    return f"+{d[:2]} {d[2:4]} *****-{d[-2:]}"

# ------------------- Util: série/ano -------------------
GRADE_MAP = {
    "infantil4": "Infantil 4 (Pré-I)",
    "infantil5": "Infantil 5 (Pré-II)",
    "1": "1º ano", "1º": "1º ano", "1o": "1º ano", "1 ano": "1º ano",
    "2": "2º ano", "2º": "2º ano", "2o": "2º ano", "2 ano": "2º ano",
    "3": "3º ano", "3º": "3º ano", "3o": "3º ano", "3 ano": "3º ano",
    "4": "4º ano", "4º": "4º ano", "4o": "4º ano", "4 ano": "4º ano",
    "5": "5º ano", "5º": "5º ano", "5o": "5º ano", "5 ano": "5º ano",
}

def parse_grade(txt: str) -> str | None:
    t = (txt or "").lower().strip()
    t = t.replace("º", "º").replace("°", "º")
    t = re.sub(r"\s+", " ", t)
    # atalhos
    if "infantil 4" in t or "pre-i" in t or "pré-i" in t or "pre i" in t or "pré i" in t:
        return GRADE_MAP["infantil4"]
    if "infantil 5" in t or "pre-ii" in t or "pré-ii" in t or "pre ii" in t or "pré ii" in t:
        return GRADE_MAP["infantil5"]
    m = re.search(r"(\d)\s*(º|o)?\s*ano", t)
    if m:
        key = m.group(1)
        return GRADE_MAP.get(key)
    # só dígito
    if t in {"1","2","3","4","5","1º","2º","3º","4º","5º","1o","2o","3o","4o","5o"}:
        key = t[0]
        return GRADE_MAP.get(key)
    return None

def age_from_text(txt: str) -> int | None:
    m = re.search(r"(\d{1,2})", txt or "")
    if not m:
        return None
    val = int(m.group(1))
    if 3 <= val <= 13:  # ajuste conforme seu público
        return val
    return None

# ------------------- Matemática (lote) -------------------
def _build_math_batch():
    problems = [f"{i}+{i}" for i in range(1, 11)]
    answers  = [i + i for i in range(1, 11)]
    return {"problems": problems, "answers": answers}

def _format_math_prompt(batch):
    lines = [
        "🧩 *Matemática (lote)*",
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
    # sucesso
    user["history"]["matematica"].append({
        "tipo": "lote",
        "problems": pend["problems"],
        "answers": got,
    })
    user["levels"]["matematica"] += 1
    user["pending"].pop("mat_lote", None)
    return True, f"✅ Matemática concluída! Nível de Matemática agora: {user['levels']['matematica']}"

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
    if not prof.get("child_name"):
        return True
    if not prof.get("age"):
        return True
    if not prof.get("grade"):
        return True
    # pelo menos 1 responsável
    guardians = prof.get("guardians") or []
    if len(guardians) < 1:
        return True
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

def ob_step(user, text: str) -> str:
    st = ob_state(user)
    step = st.get("step")
    data = st.get("data", {})

    # Correções diretas (ex.: "nome: Ana", "idade: 7", "serie: 2º ano", "crianca: +5571...", "pais: +55..., +55...")
    m = re.match(r"^\s*(nome|idade|serie|série|crianca|criança|pais|pais/responsaveis)\s*:\s*(.+)$", text, re.I)
    if m:
        field = m.group(1).lower()
        val = m.group(2).strip()
        if field in {"serie", "série"}:
            g = parse_grade(val)
            if not g:
                return "Não reconheci a *série/ano*. Exemplos: *Infantil 4*, *1º ano*, *3º ano*."
            data["grade"] = g
        elif field in {"crianca", "criança"}:
            data["child_phone"] = normalize_phone(val)
        elif field in {"pais", "pais/responsaveis"}:
            nums = [normalize_phone(x) for x in val.split(",")]
            nums = [n for n in nums if n]
            if not nums:
                return "Envie pelo menos *1* número de responsável no formato +55 DDD XXXXX-XXXX."
            data["guardians"] = nums[:2]
        elif field == "nome":
            data["child_name"] = val
        elif field == "idade":
            a = age_from_text(val)
            if not a:
                return "Idade inválida. Envie um número entre 3 e 13."
            data["age"] = a
        st["data"] = data
        st["step"] = "confirm"
        return ob_summary(data)

    if step in (None, "name"):
        st["step"] = "age"
        data["child_name"] = text.strip()
        st["data"] = data
        return f"Perfeito, *{data['child_name']}*! 😊\nQuantos *anos* ela tem?"

    if step == "age":
        a = age_from_text(text)
        if not a:
            return "Idade inválida. Envie um número entre 3 e 13."
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
        if not g:
            return "Não reconheci a *série/ano*. Exemplos: *Infantil 4*, *1º ano*, *3º ano*."
        data["grade"] = g
        st["data"] = data
        st["step"] = "child_phone"
        return (
            f"{data['child_name']} tem um número próprio de WhatsApp?\n"
            "Envie no formato *+55 DDD XXXXX-XXXX* ou responda *não tem*."
        )

    if step == "child_phone":
        ph = normalize_phone(text)
        data["child_phone"] = ph  # pode ser None (não tem)
        st["data"] = data
        st["step"] = "guardians"
        return (
            "Agora, o(s) número(s) do(s) *responsável(is)* (1 ou 2), separados por vírgula.\n"
            "Ex.: +55 71 98888-7777, +55 71 97777-8888"
        )

    if step == "guardians":
        nums = [normalize_phone(x) for x in text.split(",")]
        nums = [n for n in nums if n]
        if not nums:
            return "Envie pelo menos *1* número de responsável no formato +55 DDD XXXXX-XXXX."
        st["data"]["guardians"] = nums[:2]
        st["step"] = "confirm"
        return ob_summary(st["data"])

    if step == "confirm":
        if text.strip().lower() == "sim":
            # grava no profile
            prof = user.setdefault("profile", {})
            prof["child_name"]  = data.get("child_name")
            prof["age"]         = data.get("age")
            prof["grade"]       = data.get("grade")
            prof["child_phone"] = data.get("child_phone")
            prof["guardians"]   = data.get("guardians", [])
            prof.setdefault("tz", "America/Bahia")
            # limpa estado de onboarding
            user["onboarding"] = {"step": None, "data": {}}
            return (
                "Maravilha! ✅ Cadastro feito.\n"
                "Quando quiser, envie *iniciar* para receber as atividades do dia."
            )
        elif text.strip().lower() == "não" or text.strip().lower() == "nao":
            return (
                "Sem problema! Diga o que deseja corrigir usando um dos formatos:\n"
                "• *nome:* Ana Souza\n"
                "• *idade:* 7\n"
                "• *serie:* 2º ano\n"
                "• *crianca:* +55 71 91234-5678 (ou *não tem*)\n"
                "• *pais:* +55 71 98888-7777, +55 71 97777-8888"
            )
        else:
            return "Responda *sim* para salvar, ou *não* para ajustar."

    # segurança (não deveria cair aqui)
    st["step"] = None
    return ob_start()

def ob_summary(data: dict) -> str:
    return (
        "Confere? ✅\n"
        f"• *Nome:* {data.get('child_name')}\n"
        f"• *Idade:* {data.get('age')} anos\n"
        f"• *Série:* {data.get('grade')}\n"
        f"• *WhatsApp da criança:* {mask_phone(data.get('child_phone'))}\n"
        f"• *Responsável(is):* {', '.join(mask_phone(p) for p in (data.get('guardians') or []))}\n"
        "Responda *sim* para salvar, ou *não* para ajustar."
    )

# ------------------- Web -------------------
@app.route("/admin/ping")
def ping():
    return jsonify({"project": PROJECT_NAME, "ok": True}), 200

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

    # -------- Onboarding primeiro --------
    if needs_onboarding(user):
        st = user["onboarding"]
        if st["step"] is None:
            st["step"] = "name"
            db["users"][user_id] = user; save_db(db)
            return reply_twiml(ob_start())
        else:
            reply = ob_step(user, text)
            db["users"][user_id] = user; save_db(db)
            return reply_twiml(reply)

    # -------- Comandos gerais --------
    if low in {"menu", "ajuda", "help"}:
        reply = (
            "📚 *Assistente de Aula*\n"
            "Fluxo do dia:\n"
            "1) Matemática (lote com 10 itens — responda tudo separado por vírgula)\n"
            "2) Português (1 questão)\n"
            "3) Leitura (meta do dia)\n\n"
            "Comandos:\n"
            "- *iniciar*: começa em Matemática\n"
            "- *resposta X* ou apenas *X*: responde à etapa atual\n"
            "- *leitura ok*: confirma leitura do dia\n"
            "- *status*: mostra progresso\n"
        )
        return reply_twiml(reply)

    if low == "status":
        reply = (
            f"👤 Níveis — MAT:{user['levels']['matematica']} | PORT:{user['levels']['portugues']}\n"
            f"📈 Feitas — MAT:{len(user['history']['matematica'])} | "
            f"PORT:{len(user['history']['portugues'])} | LEIT:{len(user['history']['leitura'])}"
        )
        return reply_twiml(reply)

    if low == "iniciar":
        batch = _build_math_batch()
        user["pending"]["mat_lote"] = batch
        db["users"][user_id] = user; save_db(db)
        return reply_twiml(_format_math_prompt(batch))

    if low.startswith("leitura ok"):
        ok, msg = check_reading_submission(user)
        db["users"][user_id] = user; save_db(db)
        return reply_twiml(msg)

    # -------- Respostas do fluxo --------
    # 1) Matemática (lote)
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
    return reply_twiml("Digite *iniciar* para começar: Matemática → Português → Leitura.")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
