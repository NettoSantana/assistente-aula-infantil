# server.py — Assistente de Aula Infantil (com envio via Twilio)
import os
from flask import Flask, request, jsonify
from activities import build_daily_activity, check_answer
from leitura import get_today_reading_goal, check_reading_submission
from progress import next_levels_for_user, init_user_if_needed
from storage import load_db, save_db

# Twilio (para SMS agora; depois vale para WhatsApp também)
from twilio.rest import Client

app = Flask(__name__)

# Identidade do projeto
PROJECT_NAME = os.getenv("PROJECT_NAME", "assistente_aula_infantil")

# Credenciais/Remetente Twilio
TWILIO_SID   = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
# Para SMS agora: coloque seu número no formato +19133957848
# Quando migrar para WhatsApp, troque para "whatsapp:+19133957848"
TWILIO_FROM  = os.getenv("TWILIO_FROM", "")

_twilio_client = Client(TWILIO_SID, TWILIO_TOKEN) if (TWILIO_SID and TWILIO_TOKEN) else None

def _normalize_from_for_channel(to: str, from_cfg: str) -> str:
    """
    Garante que o FROM corresponde ao canal do TO.
    - Se o destino começa com 'whatsapp:', o FROM também precisa.
    - Se o destino for SMS (sem 'whatsapp:'), o FROM não pode ter 'whatsapp:'.
    """
    if not from_cfg:
        return from_cfg
    if to.startswith("whatsapp:") and not from_cfg.startswith("whatsapp:"):
        return "whatsapp:" + from_cfg
    if (not to.startswith("whatsapp:")) and from_cfg.startswith("whatsapp:"):
        return from_cfg.replace("whatsapp:", "", 1)
    return from_cfg

def send_message(user_id: str, text: str):
    """Envia resposta ao usuário pelo mesmo canal (SMS agora; depois WhatsApp)."""
    # Fallback de log se não houver Twilio configurado
    if not (_twilio_client and TWILIO_FROM):
        print(f"[SEND LOG → {user_id}] {text}")
        return

    try:
        from_number = _normalize_from_for_channel(user_id, TWILIO_FROM)
        _twilio_client.messages.create(
            from_=from_number,
            to=user_id,
            body=text
        )
    except Exception as e:
        print(f"⚠️ Falha ao enviar mensagem Twilio para {user_id}: {e}")
        print(f"[SEND LOG (fallback) → {user_id}] {text}")

@app.route("/admin/ping")
def ping():
    return jsonify({"project": PROJECT_NAME, "ok": True}), 200

@app.route("/bot", methods=["POST"])
def bot_webhook():
    # Twilio (SMS/WhatsApp) manda em form: Body, From
    payload = request.form or request.json or {}
    user_id = str(payload.get("From") or payload.get("user_id") or "debug-user")
    text = (payload.get("Body") or payload.get("text") or "").strip()

    db = load_db()
    user = init_user_if_needed(db, user_id)

    if text.lower() in {"menu", "ajuda", "help"}:
        reply = (
            "📚 *Assistente de Aula*\n"
            "Comandos:\n"
            "- *iniciar*: recebe a atividade do dia (mat/port/leitura)\n"
            "- *resposta X*: envia sua resposta (ex.: resposta 42)\n"
            "- *leitura ok*: confirma envio do resumo/áudio\n"
            "- *status*: mostra progresso e níveis atuais\n"
        )
        send_message(user_id, reply)
        return ("", 204)

    if text.lower() == "status":
        reply = (
            f"👤 Níveis — MAT:{user['levels']['matematica']} | PORT:{user['levels']['portugues']}\n"
            f"📈 Feitas — MAT:{len(user['history']['matematica'])} | "
            f"PORT:{len(user['history']['portugues'])} | LEIT:{len(user['history']['leitura'])}"
        )
        send_message(user_id, reply)
        return ("", 204)

    if text.lower() == "iniciar":
        plano = build_daily_activity(user)
        send_message(user_id, "🧩 *Matemática*\n" + plano["matematica"]["enunciado"])
        send_message(user_id, "✍️ *Português*\n" + plano["portugues"]["enunciado"])
        meta = get_today_reading_goal(user)
        send_message(user_id, f"📖 *Leitura* — {meta}")
        db["users"][user_id] = user
        save_db(db)
        return ("", 204)

    if text.lower().startswith("resposta"):
        answer = text.split(" ", 1)[1] if " " in text else ""
        result_txt = check_answer(user, answer)
        send_message(user_id, result_txt)
        db = load_db(); db["users"][user_id] = user; save_db(db)
        return ("", 204)

    if text.lower().startswith("leitura ok"):
        ok, msg = check_reading_submission(user)
        send_message(user_id, msg)
        db = load_db(); db["users"][user_id] = user; save_db(db)
        return ("", 204)

    send_message(user_id, "Não entendi. Digite *menu* para ajuda.")
    return ("", 204)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
