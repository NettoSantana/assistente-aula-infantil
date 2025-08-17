
from flask import Flask, request, jsonify
import os
from activities import build_daily_activity, check_answer
from leitura import get_today_reading_goal, check_reading_submission
from progress import next_levels_for_user, init_user_if_needed
from storage import load_db, save_db

app = Flask(__name__)

PROJECT_NAME = os.getenv("PROJECT_NAME", "assistente_aula_infantil")
BOT_NUMBER = os.getenv("BOT_NUMBER", "+5551999999999")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin")

def send_message(user_id: str, text: str):
    # Placeholder: por enquanto só imprime no log.
    # No passo 2 integraremos o Twilio para enviar WhatsApp.
    print(f"[SEND → {user_id}] {text}")

@app.route("/admin/ping")
def ping():
    return jsonify({"project": PROJECT_NAME, "number": BOT_NUMBER, "ok": True})

@app.route("/bot", methods=["POST"])
def bot_webhook():
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
            f"👤 Níveis atuais — MAT:{user['levels']['matematica']} | PORT:{user['levels']['portugues']}\n"
            f"📈 Feitas: MAT:{len(user['history']['matematica'])} | PORT:{len(user['history']['portugues'])} | LEIT:{len(user['history']['leitura'])}"
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
    # Produção no Railway: use waitress-serve (ver start command).
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
