# server.py — Assistente de Aula Infantil (fluxo sequencial: Matemática → Português → Leitura)
import os
from flask import Flask, request, jsonify, Response
from activities import portugues_activity, check_answer  # usamos português do activities
from leitura import get_today_reading_goal, check_reading_submission
from progress import init_user_if_needed
from storage import load_db, save_db

# Twilio — reply imediato via TwiML
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client

app = Flask(__name__)

PROJECT_NAME = os.getenv("PROJECT_NAME", "assistente_aula_infantil")

# (REST opcional para envios proativos no futuro)
TWILIO_SID   = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM  = os.getenv("TWILIO_FROM", "")
_twilio_client = Client(TWILIO_SID, TWILIO_TOKEN) if (TWILIO_SID and TWILIO_TOKEN) else None

# --------- helpers TwiML ---------
def reply_twiml(text: str) -> Response:
    r = MessagingResponse()
    r.message(text)
    return Response(str(r), mimetype="application/xml", status=200)

# --------- Módulo 1 — Matemática (lote) ---------
def _build_math_batch():
    """
    Monta o lote fixo de 10 contas: 1+1 ... 10+10
    Retorna dict com 'problems' (strings) e 'answers' (ints)
    """
    problems = [f"{i}+{i}" for i in range(1, 11)]
    answers  = [i + i for i in range(1, 11)]
    return {"problems": problems, "answers": answers}

def _format_math_prompt(batch):
    lines = ["🧩 *Matemática (lote)*",
             "Responda TUDO em uma única mensagem, *separando por vírgulas*.",
             "Exemplo: 2,4,6,8,10,12,14,16,18,20",
             ""]
    for idx, p in enumerate(batch["problems"], start=1):
        lines.append(f"{idx}) {p} = ?")
    return "\n".join(lines)

def _parse_csv_numbers(s: str):
    # converte "2, 4, 6" -> [2,4,6]
    parts = [x.strip() for x in s.split(",") if x.strip() != ""]
    nums = []
    for x in parts:
        # permite itens como " 12 " ou "12 "
        try:
            nums.append(int(x))
        except Exception:
            # se algum item não for número, falha
            return None
    return nums

def _check_math_batch(user, text: str):
    """Valida o lote de matemática. Retorna (ok: bool, msg: str)."""
    pend = user.get("pending", {}).get("mat_lote")
    if not pend:
        return False, "Nenhum lote de Matemática pendente."

    expected = pend["answers"]  # lista de 10 ints
    got = _parse_csv_numbers(text)
    if got is None:
        return False, "Envie somente números separados por vírgula (ex.: 2,4,6,...)."
    if len(got) != len(expected):
        return False, f"Você enviou {len(got)} respostas, mas são {len(expected)} itens. Reenvie os {len(expected)} valores, separados por vírgulas."

    wrong_idx = [i+1 for i, (g,e) in enumerate(zip(got, expected)) if g != e]
    if wrong_idx:
        # mostra quais posições erraram
        pos = ", ".join(map(str, wrong_idx))
        return False, f"❌ Algumas respostas estão incorretas nas posições: {pos}. Reenvie a lista completa (ex.: 2,4,6,...)"

    # sucesso: registra histórico e avança nível
    user["history"]["matematica"].append({
        "tipo": "lote",
        "problems": pend["problems"],
        "answers": got,
    })
    user["levels"]["matematica"] += 1
    # limpa pendência de matemática
    user["pending"].pop("mat_lote", None)
    return True, f"✅ Matemática concluída! Nível de Matemática agora: {user['levels']['matematica']}"

# --------- Módulo 2 — Português (1 questão) ---------
def _start_portugues(user):
    # usa o gerador existente por nível
    lvl = user["levels"]["portugues"]
    act = portugues_activity(lvl)
    user["pending"]["portugues"] = act.__dict__
    return "✍️ *Português*\n" + act.enunciado

# --------- Módulo 3 — Leitura ---------
def _start_leitura(user):
    meta = get_today_reading_goal(user)
    return f"📖 *Leitura* — {meta}"

# --------- Web ---------
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
    user.setdefault("pending", {})  # garante estrutura

    # ---------- Comandos ----------
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
        # sempre começa na Matemática (lote)
        batch = _build_math_batch()
        user["pending"]["mat_lote"] = batch
        db["users"][user_id] = user
        save_db(db)
        return reply_twiml(_format_math_prompt(batch))

    if low.startswith("leitura ok"):
        ok, msg = check_reading_submission(user)
        db["users"][user_id] = user
        save_db(db)
        return reply_twiml(msg)

    # ---------- Respostas / Fluxo ----------
    # 1) Se há Matemática em aberto, qualquer mensagem (ou "resposta ...") é tratada como lista CSV
    if "mat_lote" in user.get("pending", {}):
        # se vier "resposta ..." ou números soltos, pega tudo após "resposta" se existir
        raw = text
        if low.startswith("resposta"):
            raw = text[len("resposta"):].strip()
            if not raw and " " in text:
                raw = text.split(" ", 1)[1].strip()
            raw = raw.lstrip(":.-").strip() or raw
        ok, msg = _check_math_batch(user, raw)
        if not ok:
            return reply_twiml(msg)
        # ok em Matemática → dispara Português
        port_msg = _start_portugues(user)
        db["users"][user_id] = user
        save_db(db)
        return reply_twiml(msg + "\n\n" + port_msg)

    # 2) Se há Português em aberto, usa o validador existente (1 questão)
    if "portugues" in user.get("pending", {}):
        # aceita "resposta X" OU só "X"
        ans = text
        if low.startswith("resposta"):
            after = text[len("resposta"):].strip()
            if not after and " " in text:
                after = text.split(" ", 1)[1].strip()
            ans = after.lstrip(":.-").strip() or after or ans
        result_txt = check_answer(user, ans)  # incrementa nível e limpa pendência internamente
        # se ficou sem pendências em português → leitura
        if "portugues" not in user.get("pending", {}):
            leitura_msg = _start_leitura(user)
            db["users"][user_id] = user
            save_db(db)
            return reply_twiml(result_txt + "\n\n" + leitura_msg)
        # se ainda pendente (errou), só responde o resultado
        db["users"][user_id] = user
        save_db(db)
        return reply_twiml(result_txt)

    # 3) Se nada pendente, oriente a usar "iniciar"
    return reply_twiml("Digite *iniciar* para começar: Matemática → Português → Leitura.")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
