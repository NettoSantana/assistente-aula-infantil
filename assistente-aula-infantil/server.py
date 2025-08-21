# server.py — Assistente de Aula Infantil (responde via TwiML; mantém envio REST opcional)
import os
from flask import Flask, request, jsonify, Response
from activities import build_daily_activity, check_answer
from leitura import get_today_reading_goal, check_reading_submission
from progress import next_levels_for_user, init_user_if_needed
from storage import load_db, save_db

# Twilio REST (opcional para envios proativos) + TwiML (resposta imediata)
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

# Identidade do projeto
PROJECT_NAME = os.getenv("PROJECT_NAME", "assistente_aula_infantil")

# Credenciais/Remetente Twilio (somente se você for usar REST proativo)
TWILIO_SID   = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
# Para SMS: "+19133957848" | Para WhatsApp: "whatsapp:+19133957848"
TWILIO_FROM  = os.getenv("TWILIO_FROM", "")

_twilio_client = Client(TWILIO_SID, TWILIO_TOKEN) if (TWILIO_SID and TWILIO_TOKEN) else None

def _normalize_from_for_channel(to: str, from_cfg: str) -> str:
    if not from_cfg:
        return from_cfg
    if to.startswith("whatsapp:") and not from_cfg.startswith("whatsapp:"):
        return "whatsapp:" + from_cfg
    if (not to.startswith("whatsapp:")) and from_cfg.startswith("whatsapp:"):
        return from_cfg.replace("whatsapp:", "", 1)
    return from_cfg

def send_message(user_id: str, text: str):
    """Envio REST opcional (não usado para responder ao webhook)."""
    if not (_twilio_client and TWILIO_FROM):
        print(f"[SEND LOG → {user_id}] {text}")
        return
    try:
        from_number = _normalize_from_for_channel(user_id, TWILIO_FROM)
        _twilio_client.messages.create(from_=from_number, to=user_id, body=text)
    except Exception as e:
        print(f"⚠️ Falha ao enviar mensagem Twilio para {user_id}: {e}")
        print(f"[SEND LOG (fallback) → {user_id}] {text}")

def reply_twiml(text: str) -> Response:
    """Responde imediatamente ao WhatsApp/SMS com TwiML (evita 12200)."""
    r = MessagingResponse()
    r.message(text)
    return Response(str(r), mimetype="application/xml", status=200)

@app.route("/admin/ping")
def ping():
    return jsonify({"project": PROJECT_NAME, "ok": True}), 200

@app.route("/bot", methods=["POST"])
def bot_webhook():
    # Twilio (SMS/WhatsApp) manda em form: Body, From
    payload = request.form or request.json or {}
    user_id = str(payload.get("From") or payload.get("user_id") or "debug-user")
    text = (payload.get("Body") or payload.get("text") or "").strip()
    low = text.lower()

    db = load_db()
    user = init_user_if_needed(db, user_id)

    if low in {"menu", "ajuda", "help"}:
        reply = (
            "📚 *Assistente de Aula*\n"
            "Comandos:\n"
            "- *iniciar*: recebe a atividade do dia (mat/port/leitura)\n"
            "- *resposta X*: envia sua resposta (ex.: resposta 42)\n"
            "- *leitura ok*: confirma envio do resumo/áudio\n"
            "- *status*: mostra progresso e níveis atuais\n"
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
        plano = build_daily_activity(user)
        meta = get_today_reading_goal(user)
        db["users"][user_id] = user
        save_db(db)
        msg = (
            "🧩 *Matemática*\n" + plano["matematica"]["enunciado"] + "\n\n"
            "✍️ *Português*\n" + plano["portugues"]["enunciado"] + "\n\n"
            f"📖 *Leitura* — {meta}"
        )
        return reply_twiml(msg)

    if low.startswith("resposta"):
        answer = text.split(" ", 1)[1] if " " in text else ""
        result_txt = check_answer(user, answer)
        db = load_db(); db["users"][user_id] = user; save_db(db)
        return reply_twiml(result_txt)

    if low.startswith("leitura ok"):
        ok, msg = check_reading_submission(user)
        db = load_db(); db["users"][user_id] = user; save_db(db)
        return reply_twiml(msg)

    return reply_twiml("Não entendi. Digite *menu* para ajuda.")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
