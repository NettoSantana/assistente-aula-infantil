# tente-aula-infantil/server.py — Assistente de Aula Infantil
# Onboarding "MARIA ANGELA" + Rotina por dia (seg–sáb obrig., dom opcional)
# Fluxo: Matemática + Português (Leitura TEMPORARIAMENTE desativada)
import os, re, itertools
from typing import Optional, Dict, Any
from flask import Flask, request, jsonify, Response
from storage import load_db, save_db
from progress import init_user_if_needed

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

# ------------------- Flags / Config -------------------
FEATURE_PORTUGUES = True
FEATURE_LEITURA   = False

MAX_MATH_DAY      = 60                                 # limite do plano de Matemática
MAX_PT_DAY        = 60                                 # limite do plano de Português
ROUNDS_PER_DAY    = int(os.getenv("ROUNDS", "5"))      # 5 rodadas por dia, 10 exercícios cada

# ------------------- Util: TwiML -------------------
def reply_twiml(text: str) -> Response:
    r = MessagingResponse()
    r.message(text)
    return Response(str(r), mimetype="application/xml", status=200)

# ------------------- Util: telefones -------------------
BR_DEFAULT_CC = "55"
def normalize_phone(s: str) -> Optional[str]:
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

def mask_phone(p: Optional[str]) -> str:
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
def parse_grade(txt: str) -> Optional[str]:
    t = (txt or "").lower().strip()
    if "infantil 4" in t or "pré-i" in t or "pre-i" in t: return GRADE_MAP["infantil4"]
    if "infantil 5" in t or "pré-ii" in t or "pre-ii" in t: return GRADE_MAP["infantil5"]
    m = re.search(r"(\d)\s*(º|o)?\s*ano", t)
    if m: return GRADE_MAP.get(m.group(1))
    if t in {"1","2","3","4","5"}: return GRADE_MAP.get(t)
    return None

def age_from_text(txt: str) -> Optional[int]:
    m = re.search(r"(\d{1,2})", txt or "")
    if not m: return None
    val = int(m.group(1))
    return val if 3 <= val <= 13 else None

# ------------------- Saudação (nome da criança) -------------------
def first_name_from_profile(user) -> str:
    name = (user.get("profile", {}).get("child_name") or "").strip()
    return name.split()[0] if name else "aluno"

# ------------------- Util: rotina (dias/horário por dia) -------------------
DEFAULT_DAYS = ["mon","tue","wed","thu","fri","sat"]
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

def parse_yes_no(txt: str) -> Optional[bool]:
    t = (txt or "").strip().lower()
    if t in {"sim","s","yes","y"}: return True
    if t in {"não","nao","n","no"}: return False
    return None

def parse_time_hhmm(txt: str) -> Optional[str]:
    t = (txt or "").strip().lower()
    t = t.replace("h", ":").replace(" ", "")
    t = t.replace("pm","p").replace("am","a")
    m = re.match(r"^(\d{1,2})(?::?(\d{2}))?([ap])?$", t)
    if not m: return None
    hh = int(m.group(1)); mm = int(m.group(2) or 0); ap = m.group(3)
    if ap == "p" and hh < 12: hh += 12
    if ap == "a" and hh == 12: hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59): return None
    if (hh < 5) or (hh > 21) or (hh == 21 and mm > 30): return None
    return f"{hh:02d}:{mm:02d}"

def describe_schedule(sched: dict) -> str:
    if not sched: return "seg–sáb 19:00"
    days = [d for d in DAY_ORDER if d in (sched.get("days") or [])]
    times = sched.get("times") or {}
    parts = []
    for d in days:
        hhmm = times.get(d, "—")
        parts.append(f"{DAYS_PT.get(d,d)} {hhmm}")
    return " | ".join(parts)

# ============================================================
# =================== MATEMÁTICA (progressivo) ===============
# ============================================================
def _curriculum_spec(day_idx: int):
    """
    Retorna {phase, op, mode, anchor} para o dia [1..60].
    (Roteiro final por rodada decide: soma → sub → mult → div → mista.)
    """
    if day_idx < 1: day_idx = 1
    if day_idx > MAX_MATH_DAY: day_idx = MAX_MATH_DAY
    return {"phase": "A-Adição", "op": "soma", "mode": "direct", "anchor": day_idx}

def _module_label(op: str, etapa: int) -> str:
    labels = {"soma":"Soma","sub":"Subtração","mult":"Multiplicação","div":"Divisão","mix":"Revisão"}
    extra = { "soma": f"+{etapa}", "sub": f"-{etapa}", "mult": f"×{etapa}", "div": f"÷{etapa}", "mix": "" }
    return f"{labels.get(op, op.title())} {etapa} ({extra.get(op,'')})"

# ---------- Enunciado (comum) ----------
def _format_math_prompt(batch):
    title = batch.get("title", "Matemática")
    round_i = batch.get("round", 1)
    round_n = batch.get("rounds_total", 1)

    hint = batch.get("prompt_hint") or "Responda TUDO em uma única mensagem, *separando por vírgulas*."
    example = batch.get("prompt_example") or "Ex.: 2,4,6,8,10,12,14,16,18,20"

    lines = [
        f"🧩 *{title}* — Rodada {round_i}/{round_n}",
        hint,
        example,
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

# ---------- Geradores base (Matemática) ----------
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
    base = list(range(11, 16))
    problems = []
    answers  = []
    for m in base:
        problems.append(f"{m}-1"); answers.append(m-1)
    missing = [(10,7),(12,5),(14,9),(15,8),(18,6)]
    for total,a in missing:
        problems.append(f"__+{a}={total}"); answers.append(total - a)
    problems = problems[:10]; answers  = answers[:10]
    return problems, answers

def _gen_mult_direct(a: int):
    problems = [f"{a}x{i}" for i in range(1, 11)]
    answers  = [a * i for i in range(1, 11)]
    return problems, answers

def _gen_mult_commute(a: int):
    left  = [f"{a}x{i}" for i in range(1, 6)]
    right = [f"{i}x{a}" for i in range(6, 11)]
    problems = left + right
    answers  = [a*i for i in range(1,6)] + [i*a for i in range(6,11)]
    return problems, answers

def _gen_div_divisor(d: int):
    problems = [f"{d*i}/{d}" for i in range(1, 11)]
    answers  = [i for i in range(1, 11)]
    return problems, answers

def _gen_div_mix():
    divs = [(12,3),(14,7),(16,4),(18,9),(20,5),(21,7),(24,6),(30,5),(32,8),(40,10)]
    problems = [f"{a}/{b}" for a,b in divs]
    answers  = [a//b for a,b in divs]
    return problems, answers

def _gen_review_for_anchor(k: int):
    k = max(1, int(k))
    adds = [(k,3),(k+1,2),(k+2,1)]
    subs = [(min(20, k+10), 1),(min(20, k+10), 2),(min(20, k+10), 3)]
    mult = [(k,2),(k,3)]
    divs = [(k*2,k),(k*3,k)]
    problems = [f"{a}+{b}" for a,b in adds] + \
               [f"{a}-{b}" for a,b in subs] + \
               [f"{a}x{b}" for a,b in mult] + \
               [f"{a}/{b}" for a,b in divs]
    answers  = [a+b for a,b in adds] + \
               [a-b for a,b in subs] + \
               [a*b for a,b in mult] + \
               [a//b for a,b in divs]
    return problems, answers

# ---------- Build do lote (Matemática) ----------
def _build_batch_from_spec(spec: dict, *, model: Optional[str] = None):
    phase = spec["phase"]; op = spec["op"]; mode = spec["mode"]; anchor = spec["anchor"]
    title = f"Matemática — {phase}"
    if op == "soma":
        if mode == "direct":
            p,a = _gen_add_direct(anchor); title += f" · {anchor}+1 … {anchor}+10"
        elif mode == "inv":
            p,a = _gen_add_inv(anchor);    title += f" · 1+{anchor} … 10+{anchor}"
        else:
            p,a = _gen_add_mix10();        title += " · completar 10"
    elif op == "sub":
        if mode == "minuend":
            p,a = _gen_sub_minuend(anchor); title += f" · {anchor}-1 … {anchor}-10"
        else:
            p,a = _gen_sub_mix();           title += " · misto"
    elif op == "mult":
        if mode == "direct":
            p,a = _gen_mult_direct(anchor);  title += f" · {anchor}×1 … {anchor}×10"
        else:
            p,a = _gen_mult_commute(anchor); title += f" · comutativas de {anchor}"
    elif op == "div":
        if mode == "divisor":
            p,a = _gen_div_divisor(anchor);  title += f" · ÷{anchor}"
        else:
            p,a = _gen_div_mix();            title += " · misto"
    else:
        p,a = _gen_review_for_anchor(anchor or 1); title += " · revisão"
    return {"problems": p, "answers": a, "title": title, "spec": spec}

# ---------- Avanço de âncora / Roteiro progressivo ----------
def _spec_for_round(base_spec: dict, round_idx: int) -> dict:
    """
    ROTEIRO PROGRESSIVO (5 rodadas fixas por dia):
      1) Adição (direta)
      2) Subtração (minuendo seguro)
      3) Multiplicação (direta)
      4) Divisão (por divisor)
      5) Mista (+, −, ×, ÷) — dinâmica pela âncora
    Âncora do dia = min(dia, 20) e é usada em todas as rodadas do dia.
    """
    spec = dict(base_spec)
    day_anchor = min(20, max(1, int(spec.get("anchor") or 1)))
    plan = [
        ("soma", "direct",  day_anchor),
        ("sub",  "minuend", max(11, min(20, day_anchor + 10))),
        ("mult", "direct",  day_anchor),
        ("div",  "divisor", day_anchor),
        ("mix",  "review",  day_anchor),
    ]
    i = max(1, min(5, int(round_idx))) - 1
    op2, mode2, a2 = plan[i]
    phase_by_op = {"soma":"A-Adição","sub":"B-Subtração","mult":"C-Multiplicação","div":"D-Divisão","mix":"Revisão"}
    spec["op"]    = op2
    spec["mode"]  = mode2
    spec["anchor"]= a2
    spec["phase"] = phase_by_op.get(op2, "Revisão")
    return spec

def _apply_round_variation(batch: dict, round_idx: int):
    """Varia ordem determinística por rodada (rotaciona)."""
    p = batch["problems"][:]; a = batch["answers"][:]
    if len(p) <= 1: return batch
    k = round_idx % len(p)
    if k:
        p = p[k:] + p[:k]; a = a[k:] + a[:k]
    batch["problems"] = p; batch["answers"] = a
    return batch

def _start_math_batch_for_day(user, day: int, round_idx: int = 1):
    day = max(1, min(MAX_MATH_DAY, int(day)))
    base_spec = _curriculum_spec(day)
    spec = _spec_for_round(base_spec, round_idx)

    batch = _build_batch_from_spec(spec)
    batch["day"] = day
    batch["round"] = round_idx
    batch["rounds_total"] = ROUNDS_PER_DAY

    _apply_round_variation(batch, round_idx)
    user["pending"]["mat_lote"] = batch
    return batch

# ------------------- Correção / avanço (Matemática) -------------------
def _check_math_batch(user, text: str):
    pend = user.get("pending", {}).get("mat_lote")
    if not pend:
        return False, "Nenhum lote de Matemática pendente."

    raw = (text or "").strip().lower()
    if raw in {"ok", "ok!", "ok."}:
        spec = pend.get("spec", {})
        user["history"]["matematica"].append({
            "tipo": "lote", "curriculum": spec,
            "problems": pend["problems"], "answers": pend["answers"],
            "bypass": "ok", "round": pend.get("round"), "day": pend.get("day"),
        })
    else:
        expected = pend["answers"]
        got = _parse_csv_numbers(text)
        if got is None:
            return False, "Envie somente números separados por vírgula (ex.: 2,4,6,...)"
        if len(got) != len(expected):
            return False, f"Você enviou {len(got)} respostas, mas são {len(expected)} itens. Reenvie os {len(expected)} valores."
        wrong_idx = [i+1 for i, (g,e) in enumerate(zip(got, expected)) if g != e]
        if wrong_idx:
            pos = ", ".join(map(str, wrong_idx))
            return False, f"❌ Algumas respostas estão incorretas nas posições: {pos}. Reenvie a lista completa."
        spec = pend.get("spec", {})
        user["history"]["matematica"].append({
            "tipo": "lote", "curriculum": spec,
            "problems": pend["problems"], "answers": got,
            "round": pend.get("round"), "day": pend.get("day"),
        })

    # Avança rodada/dia
    round_idx = int(pend.get("round", 1))
    rounds_total = int(pend.get("rounds_total", ROUNDS_PER_DAY))
    day = int(user.get("curriculum",{}).get("math_day",1))

    user["pending"].pop("mat_lote", None)

    if round_idx < rounds_total:
        next_round = round_idx + 1
        batch2 = _start_math_batch_for_day(user, day, next_round)
        return True, f"✅ Rodada {round_idx}/{rounds_total} concluída! Vamos para a *Rodada {next_round}/{rounds_total}*.\n\n" + _format_math_prompt(batch2)

    # Fechou as rodadas → avança dia (até 60)
    user["levels"]["matematica"] = user["levels"].get("matematica", 0) + 1
    cur = user.setdefault("curriculum", {"math_day": 1, "total_days": MAX_MATH_DAY})
    next_day = min(MAX_MATH_DAY, int(cur.get("math_day",1)) + 1)
    cur["math_day"] = next_day

    if day == MAX_MATH_DAY and round_idx == rounds_total:
        return True, "🎉 *Parabéns!* Você concluiu o plano até o *dia 60*. Para recomeçar, envie *reiniciar*."

    batch2 = _start_math_batch_for_day(user, next_day, 1)
    return True, f"🎉 *Parabéns!* Dia {day} concluído.\nAgora avançando para o *dia {next_day}*.\n\n" + _format_math_prompt(batch2)

# ============================================================
# ===================== PORTUGUÊS (novo) =====================
# ============================================================
# Temas rotativos por dia
PT_THEMES = ["vogais", "m_n", "p_b", "t_d", "c_g"]
PT_THEME_LABEL = {
    "vogais": "Vogais",
    "m_n": "M/N",
    "p_b": "P/B",
    "t_d": "T/D",
    "c_g": "C/G",
}

def _pt_theme_for_day(day: int) -> str:
    return PT_THEMES[(max(1, int(day)) - 1) % len(PT_THEMES)]

# Bancos de palavras (sem acentos para facilitar digitação/correção)
PT_WORDS = {
    "vogais": ["abelha","elefante","igreja","ovelha","uva","abacate","escola","ilha","ovo","urso"],
    "m_n":    ["mala","mapa","mesa","milho","manga","ninho","nariz","neto","neve","nota"],
    "p_b":    ["pato","pote","pena","pano","pipa","bola","barco","beijo","boca","bala"],
    "t_d":    ["tatu","teto","tapa","tubo","taco","dado","dedo","dente","dama","duna"],
    "c_g":    ["casa","copo","cabo","cama","cubo","gato","gola","galo","gomo","gude"],
}

def _format_pt_prompt(batch):
    title = batch.get("title", "Português")
    round_i = batch.get("round", 1)
    round_n = batch.get("rounds_total", 1)

    hint = batch.get("prompt_hint") or "Responda TUDO em uma única mensagem, *separando por vírgulas*."
    example = batch.get("prompt_example") or "Ex.: a,b,c,d,e,f,g,h,i,j"

    lines = [
        f"✍️ *{title}* — Rodada {round_i}/{round_n}",
        hint,
        example,
        ""
    ]
    for idx, p in enumerate(batch["problems"], start=1):
        lines.append(f"{idx}) {p}")
    return "\n".join(lines)

def _parse_csv_tokens(s: str):
    parts = [x.strip().lower() for x in (s or "").split(",") if x.strip() != ""]
    return parts if parts else None

# Helpers para sílaba inicial dinâmica:
def _first_chunk(word: str) -> str:
    # se começa com vogal → 1 letra, senão → consoante+vogal (2 letras)
    if not word: return ""
    return word[0] if word[0] in "aeiou" else word[:2]

def _rest_chunk(word: str) -> str:
    k = 1 if word and word[0] in "aeiou" else 2
    return word[k:]

# Geradores por rodada (Português)
def _pt_round1_som_inicial(theme: str):
    words = PT_WORDS[theme]
    problems = [f"Letra inicial de *{w.upper()}* = ?" for w in words]
    answers  = [w[0] for w in words]
    return problems, answers, "Som inicial (diga só a letra).", "Ex.: p,b,a,n,..."

def _pt_round2_silabas(theme: str):
    words = PT_WORDS[theme]
    problems = [f"Complete: (___) + { _rest_chunk(w).upper() }" for w in words]
    answers  = [_first_chunk(w) for w in words]
    return problems, answers, "Sílabas: responda a sílaba/letra inicial (ex.: pa, ba, ta, ga, a...).", "Ex.: pa,ba,ta,da,ca,ga,a,e,i,o"

def _pt_round3_decodificacao(theme: str):
    words = PT_WORDS[theme]
    problems = [f"Junte e escreva: { _first_chunk(w).upper() }-{ _rest_chunk(w).upper() }" for w in words]
    answers  = [w for w in words]
    return problems, answers, "Decodifique e escreva a palavra (sem acentos).", "Ex.: pato,bola,casa,..."

def _pt_round4_ortografia(theme: str):
    words = PT_WORDS[theme]
    problems = [f"Complete a palavra: {'_'*len(_first_chunk(w))}{ _rest_chunk(w).upper() }" for w in words]
    answers  = [_first_chunk(w) for w in words]
    return problems, answers, "Ortografia: escreva a(s) letra(s) que faltam no começo.", "Ex.: pa,ba,a,ta,ga..."

def _pt_round5_leitura(theme: str):
    words = PT_WORDS[theme]
    problems = [f"Leia e escreva a palavra: {w.upper()}" for w in words]
    answers  = [w for w in words]
    return problems, answers, "Leitura: copie a palavra (sem acentos).", "Ex.: gato,casa,pato,..."

def _build_pt_batch(day: int, round_idx: int):
    theme = _pt_theme_for_day(day)
    theme_label = PT_THEME_LABEL[theme]
    title = f"Português — {theme_label}"

    if round_idx == 1:
        p,a,h,e = _pt_round1_som_inicial(theme)
    elif round_idx == 2:
        p,a,h,e = _pt_round2_silabas(theme)
    elif round_idx == 3:
        p,a,h,e = _pt_round3_decodificacao(theme)
    elif round_idx == 4:
        p,a,h,e = _pt_round4_ortografia(theme)
    else:
        p,a,h,e = _pt_round5_leitura(theme)

    return {
        "day": day,
        "round": round_idx,
        "rounds_total": ROUNDS_PER_DAY,
        "title": title,
        "problems": p,
        "answers": a,
        "spec": {"module":"pt", "theme": theme, "round": round_idx},
        "prompt_hint": h,
        "prompt_example": e,
    }

def _start_pt_batch_for_day(user, day: int, round_idx: int = 1):
    day = max(1, min(MAX_PT_DAY, int(day)))
    batch = _build_pt_batch(day, round_idx)
    # leve variação de ordem como na matemática
    _apply_round_variation(batch, round_idx)
    user["pending"]["pt_lote"] = batch
    return batch

def _check_pt_batch(user, text: str):
    pend = user.get("pending", {}).get("pt_lote")
    if not pend:
        return False, "Nenhum lote de Português pendente."

    raw = (text or "").strip().lower()
    if raw in {"ok", "ok!", "ok."}:
        spec = pend.get("spec", {})
        user["history"]["portugues"].append({
            "tipo": "lote", "spec": spec,
            "problems": pend["problems"], "answers": pend["answers"],
            "bypass": "ok", "round": pend.get("round"), "day": pend.get("day"),
        })
    else:
        expected = pend["answers"]
        got = _parse_csv_tokens(text)
        if got is None:
            return False, "Envie respostas *textuais* separadas por vírgula (ex.: p,b,a,pa,ga...)."
        if len(got) != len(expected):
            return False, f"Você enviou {len(got)} respostas, mas são {len(expected)} itens. Reenvie os {len(expected)} valores."
        wrong_idx = [i+1 for i, (g,e) in enumerate(zip(got, expected)) if g != (e or "").lower()]
        if wrong_idx:
            pos = ", ".join(map(str, wrong_idx))
            return False, f"❌ Algumas respostas estão incorretas nas posições: {pos}. Reenvie a lista completa."
        spec = pend.get("spec", {})
        user["history"]["portugues"].append({
            "tipo": "lote", "spec": spec,
            "problems": pend["problems"], "answers": got,
            "round": pend.get("round"), "day": pend.get("day"),
        })

    # Avança rodada/dia em PT
    round_idx = int(pend.get("round", 1))
    rounds_total = int(pend.get("rounds_total", ROUNDS_PER_DAY))
    day = int(user.get("curriculum_pt",{}).get("pt_day",1))

    user["pending"].pop("pt_lote", None)

    if round_idx < rounds_total:
        next_round = round_idx + 1
        batch2 = _start_pt_batch_for_day(user, day, next_round)
        return True, f"✅ Rodada {round_idx}/{rounds_total} (PT) concluída! Vamos para a *Rodada {next_round}/{rounds_total}*.\n\n" + _format_pt_prompt(batch2)

    # última rodada do dia → avança dia PT
    user["levels"]["portugues"] = user["levels"].get("portugues", 0) + 1
    cur = user.setdefault("curriculum_pt", {"pt_day": 1, "total_days": MAX_PT_DAY})
    next_day = min(MAX_PT_DAY, int(cur.get("pt_day",1)) + 1)
    cur["pt_day"] = next_day

    if day == MAX_PT_DAY and round_idx == rounds_total:
        return True, "🎉 *Parabéns!* Você concluiu o plano de Português até o final. Para recomeçar, envie *reiniciar pt*."

    batch2 = _start_pt_batch_for_day(user, next_day, 1)
    return True, f"🎉 *Parabéns!* Português do dia {day} concluído.\nAgora avançando para o *dia {next_day}*.\n\n" + _format_pt_prompt(batch2)

# ============================================================
# ==================== Onboarding (MA) =======================
# ============================================================
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
        "Vou te acompanhar em atividades de *Matemática* e *Português*.\n\n"
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

def _set_time_for_current_day(data, text: str) -> Optional[str]:
    hhmm = parse_time_hhmm(text)
    if not hhmm:
        return "Horário inválido. Exemplos: *19:00*, *18h30*, *7 pm*. Faixa aceita: 05:00–21:30."
    day = data["schedule"]["current_day"]
    data["schedule"]["times"][day] = hhmm
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
            st["step"] = "schedule_time"; st["data"] = data
            return _prompt_for_next_day_time(data)
        st["data"] = data; st["step"] = "confirm"
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
        st["data"] = data; st["step"] = "confirm"
        return ob_summary(data)

    if step in (None, "name"):
        st["step"] = "age"; data["child_name"] = text.strip(); st["data"] = data
        return f"Perfeito, *{data['child_name']}*! 😊\nQuantos *anos* ela tem?"

    if step == "age":
        a = age_from_text(text)
        if not a: return "Idade inválida. Envie um número entre 3 e 13."
        data["age"] = a; st["data"] = data; st["step"] = "grade"
        return ("E em qual *série/ano* ela está?\n"
                "Escolha ou escreva:\n"
                "• Infantil 4 (Pré-I)\n• Infantil 5 (Pré-II)\n"
                "• 1º ano • 2º ano • 3º ano • 4º ano • 5º ano")

    if step == "grade":
        g = parse_grade(text)
        if not g: return "Não reconheci a *série/ano*. Exemplos: *Infantil 4*, *1º ano*, *3º ano*."
        data["grade"] = g; st["data"] = data; st["step"] = "child_phone"
        return (f"{data['child_name']} tem um número próprio de WhatsApp?\n"
                "Envie no formato *+55 DDD XXXXX-XXXX* ou responda *não tem*.")

    if step == "child_phone":
        ph = normalize_phone(text); data["child_phone"] = ph; st["data"] = data; st["step"] = "guardians"
        return ("Agora, o(s) número(s) do(s) *responsável(is)* (1 ou 2), separados por vírgula.\n"
                "Ex.: +55 71 98888-7777, +55 71 97777-8888")

    if step == "guardians":
        nums = [normalize_phone(x) for x in text.split(",")]; nums = [n for n in nums if n]
        if not nums: return "Envie pelo menos *1* número de responsável no formato +55 DDD XXXXX-XXXX."
        st["data"]["guardians"] = nums[:2]; st["step"] = "schedule_sunday"
        return ("Perfeito! 📅 A rotina é *segunda a sábado* por padrão.\n"
                "Deseja *incluir domingo* também? (responda *sim* ou *não*)")

    if step == "schedule_sunday":
        yn = parse_yes_no(text)
        if yn is None: return "Responda *sim* para incluir domingo, ou *não* para manter seg–sáb."
        _schedule_init_days(data, include_sun=yn); st["data"] = data; st["step"] = "schedule_time"
        return _prompt_for_next_day_time(data)

    if step == "schedule_time":
        if "schedule" not in data or not data["schedule"].get("pending_days"):
            _schedule_init_days(data, include_sun=("sun" in (data.get("schedule",{}).get("days") or [])))
        err = _set_time_for_current_day(data, text)
        if err: return err
        if data["schedule"]["pending_days"]: return _prompt_for_next_day_time(data)
        st["data"] = data; st["step"] = "confirm"
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
            prof["schedule"] = {"days": [d for d in DAY_ORDER if d in (sched.get("days") or [])],
                                "times": sched.get("times", {})}
            user.setdefault("curriculum",   {"math_day": 1, "total_days": MAX_MATH_DAY})
            user.setdefault("curriculum_pt",{"pt_day":   1, "total_days": MAX_PT_DAY})
            user["onboarding"] = {"step": None, "data": {}}
            return ("Maravilha! ✅ Cadastro e rotina definidos.\n"
                    "Envie *iniciar* (Matemática) ou *iniciar pt* (Português) para começar.")
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

    user.setdefault("curriculum", {"math_day": 1, "total_days": MAX_MATH_DAY})
    user.setdefault("curriculum_pt", {"pt_day": 1, "total_days": MAX_PT_DAY})

    levels = user.setdefault("levels", {})
    levels.setdefault("matematica", 0)
    levels.setdefault("portugues", 0)

    history = user.setdefault("history", {})
    history.setdefault("matematica", [])
    history.setdefault("portugues", [])

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

    # -------- Comandos --------
    if low in {"menu", "ajuda", "help"}:
        reply = (
            "Para começar, envie *iniciar* (Matemática) ou *iniciar pt* (Português).\n"
            f"Cada dia tem *{ROUNDS_PER_DAY} rodadas* de *10 itens*.\n"
            "MAT Rodadas: 1) Adição  2) Subtração  3) Multiplicação  4) Divisão  5) Mista.\n"
            "PT  Rodadas: 1) Som inicial  2) Sílabas  3) Decodificação  4) Ortografia  5) Leitura.\n"
            "Responda em *CSV* (separe por vírgulas) ou envie *ok* para pular e avançar.\n"
            "Comandos: *iniciar*, *iniciar pt*, *resposta ...*, *ok*, *status*, *debug*, *reiniciar*, *reiniciar pt*."
        )
        return reply_twiml(reply)

    if low == "status":
        cur_day = int(user.get("curriculum",{}).get("math_day",1))
        cur_pt  = int(user.get("curriculum_pt",{}).get("pt_day",1))
        pend    = user.get("pending", {}).get("mat_lote")
        pend_pt = user.get("pending", {}).get("pt_lote")
        round_mat = f"{pend.get('round',1)}/{pend.get('rounds_total',ROUNDS_PER_DAY)}" if pend else "—"
        round_pt  = f"{pend_pt.get('round',1)}/{pend_pt.get('rounds_total',ROUNDS_PER_DAY)}" if pend_pt else "—"
        reply = (f"📊 *Status*\n"
                 f"• Matemática: dia {cur_day}/{MAX_MATH_DAY} | rodada {round_mat} | nível {user['levels']['matematica']} | feitos {len(user['history']['matematica'])}\n"
                 f"• Português:  dia {cur_pt}/{MAX_PT_DAY} | rodada {round_pt} | nível {user['levels']['portugues']} | feitos {len(user['history']['portugues'])}")
        return reply_twiml(reply)

    if low == "debug":
        cur_day = int(user.get("curriculum", {}).get("math_day", 1))
        cur_pt  = int(user.get("curriculum_pt", {}).get("pt_day", 1))
        pend    = user.get("pending", {}).get("mat_lote")
        pend_pt = user.get("pending", {}).get("pt_lote")
        pend_flag   = "sim" if pend else "não"
        pend_ptflag = "sim" if pend_pt else "não"
        spec = (pend or {}).get("spec", {}) or {}
        spec_pt = (pend_pt or {}).get("spec", {}) or {}
        title = (pend or {}).get("title", "-")
        title_pt = (pend_pt or {}).get("title", "-")
        round_str = f"{(pend or {}).get('round','-')}/{(pend or {}).get('rounds_total','-')}" if pend else "-"
        round_str_pt = f"{(pend_pt or {}).get('round','-')}/{(pend_pt or {}).get('rounds_total','-')}" if pend_pt else "-"
        phase = spec.get("phase","-"); op = spec.get("op","-"); mode = spec.get("mode","-"); anchor = spec.get("anchor","-")
        reply = (
            "🛠 *DEBUG*\n"
            f"• MAT day: {cur_day}/{MAX_MATH_DAY} | pendência: {pend_flag} | round: {round_str}\n"
            f"  title: {title}\n"
            f"  spec: phase={phase} | op={op} | mode={mode} | anchor={anchor}\n"
            f"• PT  day: {cur_pt}/{MAX_PT_DAY} | pendência: {pend_ptflag} | round: {round_str_pt}\n"
            f"  title: {title_pt}\n"
            f"  spec: {spec_pt}"
        )
        return reply_twiml(reply)

    if low in {"reiniciar", "zerar", "resetar"}:
        user["curriculum"] = {"math_day": 1, "total_days": MAX_MATH_DAY}
        user["pending"].pop("mat_lote", None)
        db["users"][user_id] = user; save_db(db)
        return reply_twiml("🔁 Matemática reiniciada. Envie *iniciar* para começar do *Dia 1* (Rodada 1).")

    if low in {"reiniciar pt", "resetar pt", "zerar pt"}:
        user["curriculum_pt"] = {"pt_day": 1, "total_days": MAX_PT_DAY}
        user["pending"].pop("pt_lote", None)
        db["users"][user_id] = user; save_db(db)
        return reply_twiml("🔁 Português reiniciado. Envie *iniciar pt* para começar do *Dia 1* (Rodada 1).")

    # -------- Iniciar sessões --------
    if low == "iniciar":
        if "mat_lote" in user.get("pending", {}):
            batch = user["pending"]["mat_lote"]
            db["users"][user_id] = user; save_db(db)
            return reply_twiml(_format_math_prompt(batch))
        day = int(user.get("curriculum",{}).get("math_day",1))
        if day > MAX_MATH_DAY:
            return reply_twiml("✅ Você já concluiu o plano até o *dia 60*. Envie *reiniciar* para começar de novo.")
        batch = _start_math_batch_for_day(user, day, 1)
        db["users"][user_id] = user; save_db(db)
        nome = first_name_from_profile(user)
        saudacao = f"Olá, {nome}! Vamos iniciar *Matemática* de hoje. 👋"
        return reply_twiml(saudacao + "\n\n" + _format_math_prompt(batch))

    if low in {"iniciar pt","pt iniciar","iniciar português","iniciar portugues"}:
        if not FEATURE_PORTUGUES:
            return reply_twiml("✍️ *Português* está desativado no momento.")
        if "pt_lote" in user.get("pending", {}):
            batch = user["pending"]["pt_lote"]
            db["users"][user_id] = user; save_db(db)
            return reply_twiml(_format_pt_prompt(batch))
        day = int(user.get("curriculum_pt",{}).get("pt_day",1))
        if day > MAX_PT_DAY:
            return reply_twiml("✅ Você já concluiu o plano de *Português*. Envie *reiniciar pt* para começar de novo.")
        batch = _start_pt_batch_for_day(user, day, 1)
        db["users"][user_id] = user; save_db(db)
        nome = first_name_from_profile(user)
        saudacao = f"Olá, {nome}! Vamos iniciar *Português* de hoje. 👋"
        return reply_twiml(saudacao + "\n\n" + _format_pt_prompt(batch))

    if low.startswith("leitura ok"):
        db["users"][user_id] = user; save_db(db)
        return reply_twiml("📖 *Leitura* está desativada no momento. Siga com *Matemática* ou *Português*.")

    # -------- Respostas --------
    # Preferência: se PT está pendente, tratamos PT primeiro; senão Matemática
    if low in {"ok", "ok!", "ok."} and ("pt_lote" not in user.get("pending", {}) and "mat_lote" not in user.get("pending", {})):
        # nenhuma pendência → abrir a do dia de Matemática por padrão
        day = int(user.get("curriculum",{}).get("math_day",1))
        if day > MAX_MATH_DAY:
            return reply_twiml("✅ Plano de Matemática encerrado. Envie *reiniciar* para recomeçar.")
        _start_math_batch_for_day(user, day, 1)

    if "pt_lote" in user.get("pending", {}):
        raw = text
        if low.startswith("resposta"):
            raw = text.split(" ", 1)[1].strip() if " " in text else ""
            raw = raw.lstrip(":.-").strip() or raw
        ok_flag, msg = _check_pt_batch(user, raw)
        db["users"][user_id] = user; save_db(db)
        return reply_twiml(msg)

    if "mat_lote" in user.get("pending", {}):
        raw = text
        if low.startswith("resposta"):
            raw = text.split(" ", 1)[1].strip() if " " in text else ""
            raw = raw.lstrip(":.-").strip() or raw
        ok_flag, msg = _check_math_batch(user, raw)
        db["users"][user_id] = user; save_db(db)
        return reply_twiml(msg)

    return reply_twiml("Envie *iniciar* (Matemática) ou *iniciar pt* (Português) para começar.")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
