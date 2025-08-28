# server.py — Assistente de Aula Infantil (ONLINE-ONLY)
# Regra: não envia NENHUMA mensagem para a criança.
#        Somente envia SMS aos responsáveis quando o dia termina com sucesso.
# Fluxo diário: Matemática (5) -> Português (5) -> Leitura (3 páginas) -> fecha o dia e notifica responsáveis.

import os, re, random, tempfile, shutil
import subprocess  # NEW: fallback com ffprobe
from typing import Optional, Dict, Any, List, Tuple
from flask import Flask, request, jsonify, Response
from storage import load_db, save_db
from progress import init_user_if_needed

from datetime import datetime
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None

# Twilio — TwiML (resposta imediata) + envios proativos
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client

# NOVO: libs para PDF e áudio
import requests
from mutagen import File as MutagenFile
try:
    from pypdf import PdfReader
except Exception:
    # fallback se pacote instalado com nome antigo
    from PyPDF2 import PdfReader  # type: ignore

app = Flask(__name__)

PROJECT_NAME = os.getenv("PROJECT_NAME", "assistente_aula_infantil")

# Twilio (Railway)
TWILIO_SID   = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM  = os.getenv("TWILIO_FROM", "")
_twilio_client = Client(TWILIO_SID, TWILIO_TOKEN) if (TWILIO_SID and TWILIO_TOKEN) else None

# ------------------- Flags / Config -------------------
FEATURE_PORTUGUES = True
FEATURE_LEITURA   = True  # ATIVADO
AUTO_SEQUENCE_PT_AFTER_MATH   = True                   # após Matemática inicia Português
AUTO_SEQUENCE_READ_AFTER_PT   = True                   # após Português inicia Leitura
MAX_MATH_DAY      = 60
MAX_PT_DAY        = 60
ROUNDS_PER_DAY    = int(os.getenv("ROUNDS", "5"))      # 5 por disciplina

# Livros (PDFs) — ajustado para funcionar local (Windows/macOS) e no Railway
def _default_books_dir():
    # Se existir /data, usamos /data/books (persistente no Railway)
    if os.path.isdir("/data"):
        p = "/data/books"
        os.makedirs(p, exist_ok=True)
        return p
    # Fallback local: pasta "books" ao lado do server.py
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, "books")
    os.makedirs(p, exist_ok=True)
    return p

BOOKS_DIR = os.getenv("BOOKS_DIR", _default_books_dir())

def _ensure_books_dir():
    """
    Copia PDFs da pasta 'books' ao lado do server.py para o destino efetivo (BOOKS_DIR).
    - Em produção: copia do repo -> /data/books
    - Em dev local (sem /data): BOOKS_DIR já é '.../assistente-aula-infantil/books', então não copia.
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        src = os.path.join(here, "books")
        dst = BOOKS_DIR
        os.makedirs(dst, exist_ok=True)
        if os.path.abspath(src) == os.path.abspath(dst):
            return
        if os.path.isdir(src):
            for name in os.listdir(src):
                if name.lower().endswith(".pdf"):
                    s = os.path.join(src, name)
                    d = os.path.join(dst, name)
                    if not os.path.exists(d):
                        shutil.copyfile(s, d)
    except Exception:
        pass

# Garante que /data/books (se existir) receba os PDFs do repo
_ensure_books_dir()

# ------------------- Mensagens motivacionais -------------------
MOTIV_QUOTES = [
    ("A disciplina é a ponte entre metas e conquistas.", "Jim Rohn"),
    ("O sucesso é a soma de pequenos esforços repetidos dia após dia.", "Robert Collier"),
    ("Persistência é o caminho do êxito.", "Charlie Chaplin"),
    ("A prática não leva à perfeição; a prática consistente leva ao progresso.", None),
    ("Você não precisa ser o melhor, só precisa ser melhor do que ontem.", None),
    ("Disciplina é fazer o simples mesmo quando ninguém está vendo.", None),
    ("Esforço hoje é confiança amanhã.", None),
    ("Passinho a passinho, a montanha inteira se movimenta.", None),
    ("Quem treina todo dia constrói músculos de mente.", None),
]
def pick_quote() -> str:
    text, author = random.choice(MOTIV_QUOTES)
    return f"“{text}”" + (f" — {author}" if author else "")

# ------------------- Util: TwiML -------------------
def reply_twiml(text: str) -> Response:
    r = MessagingResponse()
    r.message(text)
    return Response(str(r), mimetype="application/xml", status=200)

def _send_sms(to: Optional[str], body: str) -> bool:
    if not to or not _twilio_client or not TWILIO_FROM:
        return False
    try:
        _twilio_client.messages.create(to=to, from_=TWILIO_FROM, body=body)
        return True
    except Exception:
        return False

# ------------------- Telefones / formatação -------------------
BR_DEFAULT_CC = "55"
def normalize_phone(s: str) -> Optional[str]:
    if not s: return None
    x = re.sub(r"[^\d+]", "", s).strip()
    if x.lower() in {"nao tem", "não tem", "naotem"}: return None
    if x.startswith("+"):
        digits = re.sub(r"\D", "", x)
        return f"+{digits}"
    digits = re.sub(r"\D", "", x)
    if 10 <= len(digits) <= 12:
        return f"+{BR_DEFAULT_CC}{digits}"
    return None

def mask_phone(p: Optional[str]) -> str:
    if not p: return "não tem"
    d = re.sub(r"\D", "", p)
    if len(d) < 4: return p
    return f"+{d[:2]} {d[2:4]} *****-{d[-2:]}"

# ------------------- Série/Ano -------------------
GRADE_MAP = {
    "infantil4": "Infantil 4 (Pré-I)",
    "infantil5": "Infantil 5 (Pré-II)",
    "1": "1º ano","2":"2º ano","3":"3º ano","4":"4º ano","5":"5º ano",
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

# ------------------- Saudação -------------------
def first_name_from_profile(user) -> str:
    name = (user.get("profile", {}).get("child_name") or "").strip()
    return name.split()[0] if name else "aluno"

# ------------------- Tempo / Rotina -------------------
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

def _user_tz(user):
    tzname = user.get("profile", {}).get("tz") or "America/Bahia"
    if ZoneInfo:
        try:
            return ZoneInfo(tzname)
        except Exception:
            pass
    return None

def _now(user=None) -> datetime:
    z = _user_tz(user) if user else None
    return datetime.now(tz=z) if z else datetime.utcnow()

def _today_key(user) -> str:
    n = _now(user)
    return n.strftime("%Y-%m-%d")

# ============================================================
# =================== MATEMÁTICA (progressivo) ===============
# ============================================================
def _curriculum_spec(day_idx: int):
    if day_idx < 1: day_idx = 1
    if day_idx > MAX_MATH_DAY: day_idx = MAX_MATH_DAY
    return {"phase": "A-Adição", "op": "soma", "mode": "direct", "anchor": day_idx}

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
        try: nums.append(int(x))
        except Exception: return None
    return nums

# Geradores de exercícios (MAT)
def _gen_add_direct(a: int):  return ([f"{a}+{i}" for i in range(1, 11)], [a + i for i in range(1, 11)])
def _gen_add_inv(a: int):     return ([f"{i}+{a}" for i in range(1, 11)], [i + a for i in range(1, 11)])
def _gen_add_mix10():
    pairs = [(1,9),(2,8),(3,7),(4,6),(5,5),(6,4),(7,3),(8,2),(9,1),(10,0)]
    return ([f"{x}+{y}" for x,y in pairs], [x+y for x,y in pairs])
def _gen_sub_minuend(m: int): return ([f"{m}-{i}" for i in range(1, 11)], [m - i for i in range(1, 11)])
def _gen_sub_mix():
    base = list(range(11, 16))
    problems, answers = [], []
    for m in base: problems.append(f"{m}-1"); answers.append(m-1)
    for total,a in [(10,7),(12,5),(14,9),(15,8),(18,6)]:
        problems.append(f"__+{a}={total}"); answers.append(total - a)
    return problems[:10], answers[:10]
def _gen_mult_direct(a: int):  return ([f"{a}x{i}" for i in range(1, 11)], [a * i for i in range(1, 11)])
def _gen_mult_commute(a: int):
    left  = [f"{a}x{i}" for i in range(1, 6)]
    right = [f"{i}x{a}" for i in range(6, 11)]
    return (left + right, [a*i for i in range(1,6)] + [i*a for i in range(6,11)])
def _gen_div_divisor(d: int):  return ([f"{d*i}/{d}" for i in range(1, 11)], [i for i in range(1, 11)])
def _gen_div_mix():
    divs = [(12,3),(14,7),(16,4),(18,9),(20,5),(21,7),(24,6),(30,5),(32,8),(40,10)]
    return ([f"{a}/{b}" for a,b in divs], [a//b for a,b in divs])
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

def _build_batch_from_spec(spec: dict):
    phase = spec["phase"]; op = spec["op"]; mode = spec["mode"]; anchor = spec["anchor"]
    title = f"Matemática — {phase}"
    if op == "soma":
        if mode == "direct": p,a = _gen_add_direct(anchor); title += f" · {anchor}+1 … {anchor}+10"
        elif mode == "inv":  p,a = _gen_add_inv(anchor);    title += f" · 1+{anchor} … 10+{anchor}"
        else:                p,a = _gen_add_mix10();        title += " · completar 10"
    elif op == "sub":
        if mode == "minuend": p,a = _gen_sub_minuend(anchor); title += f" · {anchor}-1 … {anchor}-10"
        else:                  p,a = _gen_sub_mix();           title += " · misto"
    elif op == "mult":
        if mode == "direct":  p,a = _gen_mult_direct(anchor);  title += f" · {anchor}×1 … {anchor}×10"
        else:                  p,a = _gen_mult_commute(anchor); title += f" · comutativas de {anchor}"
    elif op == "div":
        if mode == "divisor": p,a = _gen_div_divisor(anchor);  title += f" · ÷{anchor}"
        else:                  p,a = _gen_div_mix();            title += " · misto"
    else:
        p,a = _gen_review_for_anchor(anchor or 1); title += " · revisão"
    return {"problems": p, "answers": a, "title": title, "spec": spec}

def _spec_for_round(base_spec: dict, round_idx: int) -> dict:
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
    spec.update({"op": op2, "mode": mode2, "anchor": a2, "phase": phase_by_op.get(op2,"Revisão")})
    return spec

def _apply_round_variation(batch: dict, round_idx: int):
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
    batch.update({"day": day, "round": round_idx, "rounds_total": ROUNDS_PER_DAY})
    _apply_round_variation(batch, round_idx)
    user["pending"]["mat_lote"] = batch
    return batch

# ============================================================
# ===================== PORTUGUÊS ============================
# ============================================================
PT_THEMES = ["vogais", "m_n", "p_b", "t_d", "c_g"]
PT_THEME_LABEL = {"vogais":"Vogais","m_n":"M/N","p_b":"P/B","t_d":"T/D","c_g":"C/G"}
def _pt_theme_for_day(day: int) -> str:
    return PT_THEMES[(max(1, int(day)) - 1) % len(PT_THEMES)]

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

def _first_chunk(word: str) -> str:
    if not word: return ""
    return word[0] if word[0] in "aeiou" else word[:2]

def _rest_chunk(word: str) -> str:
    k = 1 if word and word[0] in "aeiou" else 2
    return word[k:]

def _pt_round1_som_inicial(theme: str):
    words = PT_WORDS[theme]
    problems = [f"Letra inicial de *{w.upper()}* = ?" for w in words]
    answers  = [w[0] for w in words]
    return problems, answers, "Som inicial (diga só a letra).", "Ex.: p,b,a,n,..."

def _pt_round2_silabas(theme: str):
    words = PT_WORDS[theme]
    problems = [f"Complete: (___) + { _rest_chunk(w).upper() }" for w in words]
    answers  = [_first_chunk(w) for w in words]
    return problems, answers, "Sílabas: responda a sílaba/letra inicial.", "Ex.: pa,ba,ta,da,ca,ga,a,e,i,o"

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
    if   round_idx == 1: p,a,h,e = _pt_round1_som_inicial(theme)
    elif round_idx == 2: p,a,h,e = _pt_round2_silabas(theme)
    elif round_idx == 3: p,a,h,e = _pt_round3_decodificacao(theme)
    elif round_idx == 4: p,a,h,e = _pt_round4_ortografia(theme)
    else:                p,a,h,e = _pt_round5_leitura(theme)
    return {
        "day": day, "round": round_idx, "rounds_total": ROUNDS_PER_DAY,
        "title": title, "problems": p, "answers": a,
        "spec": {"module":"pt", "theme": theme, "round": round_idx},
        "prompt_hint": h, "prompt_example": e,
    }

def _start_pt_batch_for_day(user, day: int, round_idx: int = 1):
    day = max(1, min(MAX_PT_DAY, int(day)))
    batch = _build_pt_batch(day, round_idx)
    _apply_round_variation(batch, round_idx)
    user["pending"]["pt_lote"] = batch
    return batch

# ---------- Aux: relatório e notificação (somente responsáveis) ----------
def _count_rounds_for_day(user, subject: str, day_num: int) -> int:
    hist = user.get("history", {}).get(subject, []) or []
    return sum(1 for h in hist if h.get("tipo") == "lote" and int(h.get("day", -1)) == int(day_num))

def _guardians_list(user):
    return (user.get("profile", {}).get("guardians") or [])[:2]

def _mini_report_text(user, day_num: int) -> str:
    nome = first_name_from_profile(user).title()
    today = _today_key(user)
    mat = _count_rounds_for_day(user, "matematica", day_num)
    pt  = _count_rounds_for_day(user, "portugues",  day_num)
    read_ok = "sim" if any(h.get("tipo")=="leitura" and h.get("day")==day_num for h in user.get("history",{}).get("leitura",[])) else "não"
    quote = pick_quote()
    return (f"✅ Relatório do dia ({today})\n"
            f"{nome} *concluiu as atividades* de hoje.\n"
            f"• Matemática: {mat}/5 rodadas\n"
            f"• Português: {pt}/5 rodadas\n"
            f"• Leitura: {read_ok}\n"
            f"{quote}")

def _close_day_and_notify(user, current_day: int):
    """Marca concluído e envia SMS só aos responsáveis (nunca à criança)."""
    dk = _today_key(user)
    flags = user.setdefault("daily_flags", {}).setdefault(dk, {"report_sent": False, "completed": False})
    flags["completed"] = True
    if not flags.get("report_sent"):
        report = _mini_report_text(user, current_day)
        for g in _guardians_list(user):
            _send_sms(g, report)
        flags["report_sent"] = True

# ============================================================
# ===================== LEITURA (NOVO) =======================
# ============================================================
READ_KEYWORDS = {"SUMÁRIO","INDICE","ÍNDICE","PREFÁCIO","APRESENTAÇÃO","DEDICATÓRIA","AGRADECIMENTOS","CAPA","CONTENTS"}
MIN_TEXT_CHARS = 120  # densidade mínima para considerar "conteúdo"
MIN_AUDIO_SEC  = 60
PASS_MIN_SCORE = 8.0001  # precisa ser > 8

def _reading_state(user) -> Dict[str, Any]:
    return user.setdefault("reading", {
        "selected_book": None,
        "total_pages": 0,
        "start_page": None,
        "cursor": None,
        "last_pages": None,
        "awaiting_audio": False,
        "menu": [],  # lista de arquivos mostrados por último para seleção numérica
    })

def _list_books() -> List[str]:
    try:
        files = [f for f in os.listdir(BOOKS_DIR) if f.lower().endswith(".pdf")]
        files.sort()
        return files
    except Exception:
        return []

def _book_path(name: str) -> Optional[str]:
    if not name: return None
    path = os.path.abspath(os.path.join(BOOKS_DIR, name))
    if not path.startswith(os.path.abspath(BOOKS_DIR)):  # sandbox
        return None
    return path if os.path.isfile(path) else None

def _pdf_total_pages(path: str) -> int:
    with open(path, "rb") as f:
        reader = PdfReader(f)
        return len(reader.pages)

def _extract_text_len(reader: PdfReader, page_index: int) -> int:
    try:
        t = reader.pages[page_index].extract_text() or ""
        t = t.strip()
        if any(k in t.upper() for k in READ_KEYWORDS): return 0
        return len(re.sub(r"\s+", " ", t))
    except Exception:
        return 0

def _suggest_start_page(path: str) -> int:
    with open(path, "rb") as f:
        reader = PdfReader(f)
        n = len(reader.pages)
        max_probe = min(15, n)
        best = 1
        for i in range(0, max_probe):
            L = _extract_text_len(reader, i)
            if L >= MIN_TEXT_CHARS:
                best = i + 1
                break
        return best

def _pick_next_pages(user) -> Optional[Tuple[int,int,int]]:
    st = _reading_state(user)
    cur = int(st.get("cursor") or 1)
    tot = int(st.get("total_pages") or 0)
    if cur > tot: return None
    p1 = cur
    p2 = min(cur+1, tot)
    p3 = min(cur+2, tot)
    return (p1, p2, p3)

def _format_reading_prompt(pages: Tuple[int,int,int], book: str) -> str:
    p1,p2,p3 = pages
    return (f"📖 *Leitura* — Livro: *{book}*\n"
            f"Páginas da vez: *{p1}, {p2}, {p3}*.\n"
            f"Grave *1 áudio* com *≥ {MIN_AUDIO_SEC}s* resumindo o que leu nessas páginas.\n"
            f"Critério: nota > {int(PASS_MIN_SCORE)} para passar. Envie apenas o áudio.")

def _reading_book_in_progress(st) -> bool:
    if not st.get("selected_book"): return False
    cur = int(st.get("cursor") or 0)
    tot = int(st.get("total_pages") or 0)
    return bool(cur and tot and cur <= tot)

def _lock_msg(st) -> str:
    return (f"🔒 Você já está lendo *{st.get('selected_book')}* "
            f"(pág {int(st.get('cursor') or 0)}/{int(st.get('total_pages') or 0)}).\n"
            f"Só pode escolher outro livro quando *concluir este*. "
            f"Para continuar, envie *iniciar leitura*.")

def _reading_menu_text(user) -> str:
    _ensure_books_dir()
    files = _list_books()
    st = _reading_state(user)
    st["menu"] = files
    if not files:
        return f"📚 Nenhum PDF encontrado em *{BOOKS_DIR}*. Suba os livros e tente de novo."
    lines = [f"{i+1}) {nm}" for i, nm in enumerate(files[:30])]
    tail = "" if len(files) <= 30 else f"\n… ({len(files)-30} mais)"
    return "📚 *Escolha um livro (digite o número):*\n" + "\n".join(lines) + tail + "\n\nUse: *escolher <número>*"

def _reading_start_for_user(user) -> str:
    st = _reading_state(user)
    book = st.get("selected_book")
    if not book:
        return _reading_menu_text(user)
    pages = _pick_next_pages(user)
    if not pages:
        return "📘 Este livro foi concluído! Envie *livros* para escolher outro."
    st["awaiting_audio"] = True
    st["last_pages"] = list(pages)
    return _format_reading_prompt(pages, book)

def _reading_select_book(user, name_or_pattern: str) -> str:
    st = _reading_state(user)
    if _reading_book_in_progress(st):
        return _lock_msg(st)

    # aceita nome exato/substring (compat) OU índice numérico
    files = _list_books()
    if not files:
        return f"📚 Nenhum PDF encontrado em *{BOOKS_DIR}*."

    # índice?
    mnum = re.fullmatch(r"\d{1,3}", name_or_pattern.strip())
    if mnum:
        idx = int(mnum.group(0))
        if not (1 <= idx <= len(files)):
            return "Número inválido. Envie *livros* para ver a lista numerada novamente."
        resolved = files[idx-1]
    else:
        query = name_or_pattern.strip().lower()
        exact = next((f for f in files if f.lower() == query), None)
        if exact:
            resolved = exact
        else:
            cand = [f for f in files if query in f.lower()]
            if len(cand) != 1:
                return "Livro não encontrado ou ambíguo. Envie *livros* e escolha por número (ex.: *escolher 1*)."
            resolved = cand[0]

    path = _book_path(resolved)
    if not path:
        return "Livro não encontrado. Envie *livros* e escolha por número."
    tot = _pdf_total_pages(path)
    start = _suggest_start_page(path)
    st.update({
        "selected_book": os.path.basename(resolved),
        "total_pages": tot,
        "start_page": start,
        "cursor": start,
        "last_pages": None,
        "awaiting_audio": False,
    })
    return (f"📚 Livro selecionado: *{os.path.basename(resolved)}* ({tot} páginas).\n"
            f"Sugestão de início: *página {start}*.\n"
            f"Se quiser alterar: *inicio <n>*.\n"
            f"Quando quiser começar: *iniciar leitura*.")

def _reading_set_start(user, n: int) -> str:
    st = _reading_state(user)
    if not st.get("selected_book"):
        return "Escolha um livro antes. Envie *livros* e depois *escolher <número>*."
    n = max(1, int(n))
    n = min(n, int(st.get("total_pages") or n))
    st["start_page"] = n
    st["cursor"] = n
    st["last_pages"] = None
    st["awaiting_audio"] = False
    return f"✅ Início ajustado para a *página {n}*. Envie *iniciar leitura*."

def _reading_register_result(user, pages: Tuple[int,int,int], seconds: float, score: float, day_num: int):
    hist = user.setdefault("history", {})
    hist.setdefault("leitura", [])
    hist["leitura"].append({
        "tipo":"leitura",
        "pages": list(pages),
        "seconds": round(seconds,1),
        "score": round(score,2),
        "book": _reading_state(user).get("selected_book"),
        "day": day_num,
    })
    user.setdefault("levels", {}).setdefault("leitura", 0)
    user["levels"]["leitura"] += 1

def _score_from_seconds(sec: float) -> float:
    # 60s = 6; +1 pto a cada 6s extra; teto 10
    base = 6.0 + max(0.0, (sec - MIN_AUDIO_SEC)) / 6.0
    return min(10.0, base)

# NEW: fallback com ffprobe
def _probe_duration_ffprobe(fpath: str) -> Optional[float]:
    """Tenta medir duração via ffprobe (se ffmpeg estiver instalado)."""
    try:
        if not shutil.which("ffprobe"):
            return None
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                fpath,
            ],
            stderr=subprocess.STDOUT,
            timeout=10,
            universal_newlines=True,
        ).strip()
        val = float(out)
        return val if val > 0 else None
    except Exception:
        return None

def _handle_audio_submission(user, payload) -> Optional[str]:
    """Processa áudio quando estamos aguardando (via Twilio WhatsApp)."""
    st = _reading_state(user)
    if not st.get("awaiting_audio"):
        return None

    num_media = int(payload.get("NumMedia", "0") or "0")
    if num_media < 1:
        return "Envie o *áudio* (nota por duração)."

    # Aceita tipos 'audio/*' e alguns que chegam como video/ogg, webm ou application/ogg
    media_url = None
    ctype = None
    for i in range(num_media):
        ct = (payload.get(f"MediaContentType{i}") or "").lower()
        url = payload.get(f"MediaUrl{i}")
        if not ct or not url:
            continue
        is_audioish = (
            ct.startswith("audio")
            or ct in {"video/ogg", "video/webm", "application/ogg", "application/octet-stream"}
        )
        if is_audioish:
            media_url = url
            ctype = ct
            break

    if not media_url:
        return "Anexo recebido, mas não é áudio. Envie um *áudio* de resumo (≥ 60s)."

    # Baixa o arquivo com auth da Twilio
    try:
        resp = requests.get(media_url, auth=(TWILIO_SID, TWILIO_TOKEN), timeout=20)
        resp.raise_for_status()
    except Exception:
        return "Não consegui baixar o áudio. Tente reenviar."

    # Mapeia extensões de forma mais ampla
    ext_map = {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/ogg": ".ogg",
        "audio/ogg; codecs=opus": ".ogg",
        "application/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/aac": ".m4a",
        "audio/mp4": ".m4a",
        "audio/m4a": ".m4a",
        "audio/3gpp": ".3gp",
        "audio/amr": ".amr",
        "audio/webm": ".webm",
        "video/ogg": ".ogg",
        "video/webm": ".webm",
        "application/octet-stream": ".bin",
    }

    tmpdir = tempfile.mkdtemp()
    try:
        ext = ext_map.get(ctype or "", ".bin")
        if ext == ".bin" and (ctype or "").startswith("audio/") and "/" in (ctype or ""):
            ext = "." + ctype.split("/")[1].split(";")[0]

        fpath = os.path.join(tmpdir, f"audio{ext}")
        with open(fpath, "wb") as f:
            f.write(resp.content)

        # 1ª tentativa: mutagen
        sec = None
        try:
            au = MutagenFile(fpath)
            if au and getattr(au, "info", None) and getattr(au.info, "length", None):
                sec = float(au.info.length)
        except Exception:
            sec = None

        # 2ª tentativa: ffprobe (se disponível)
        if not sec:
            sec = _probe_duration_ffprobe(fpath)

        # 3ª tentativa: algum campo de duração no webhook (se existir)
        if not sec:
            mdur = payload.get("MediaDuration0") or payload.get("MediaDuration")
            try:
                if mdur:
                    val = float(mdur)
                    sec = val / 1000.0 if val > 1000 else val
            except Exception:
                pass

        if not sec or sec <= 0:
            return (f"Não consegui ler a duração do áudio (tipo: {ctype or 'desconhecido'}).\n"
                    "Reenvie como *arquivo de áudio* em *OGG/MP3/M4A* (evite WEBM/3GP) "
                    "ou grave como *mensagem de voz* padrão do WhatsApp.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    pages = tuple(st.get("last_pages") or _pick_next_pages(user) or [])
    if not pages:
        st["awaiting_audio"] = False
        return "Não encontrei páginas pendentes. Envie *iniciar leitura*."

    score = _score_from_seconds(sec)
    p1,p2,p3 = pages
    sec_i = int(round(sec))

    if sec < MIN_AUDIO_SEC or score <= PASS_MIN_SCORE:
        need = f"❌ Áudio com *{sec_i}s* → nota *{score:.1f}/10*.\n"
        need += f"Critério: *≥ {MIN_AUDIO_SEC}s* e *nota > 8*.\n"
        need += f"Regrave o resumo das páginas *{p1}–{p3}*."
        return need

    # Aprovado
    day = int(user.get("curriculum_pt",{}).get("pt_day", user.get("curriculum",{}).get("math_day",1)))
    _reading_register_result(user, pages, sec, score, day)

    # avança cursor (+3 sem 'min' para permitir finalizar o livro)
    st["cursor"] = int(st["cursor"] or 1) + 3
    st["awaiting_audio"] = False
    st["last_pages"] = None

    # Fecha o dia: sincroniza próximos dias de MAT/PT e notifica responsáveis
    cur_pt  = user.setdefault("curriculum_pt", {"pt_day": 1, "total_days": MAX_PT_DAY})
    cur_mat = user.setdefault("curriculum",   {"math_day": 1, "total_days": MAX_MATH_DAY})
    current_day = int(day)
    next_day = current_day + 1
    cur_pt["pt_day"]    = min(MAX_PT_DAY,  next_day)
    cur_mat["math_day"] = min(MAX_MATH_DAY, next_day)
    _close_day_and_notify(user, current_day)

    tail = ""
    if int(st.get("cursor") or 0) > int(st.get("total_pages") or 0):
        tail = "\n📘 *Livro concluído!* Para escolher outro: envie *livros* e depois *escolher <número>*."

    return (f"✅ *Leitura concluída!* Páginas *{p1}–{p3}*.\n"
            f"Áudio: *{sec_i}s* → Nota *{score:.1f}/10*.\n"
            f"📅 *Dia {current_day} fechado.* Amanhã seguimos com a Matemática do dia {next_day}."
            f"{tail}")

# ------------------- Correção / avanço (Matemática) -------------------
def _check_math_batch(user, text: str):
    pend = user.get("pending", {}).get("mat_lote")
    if not pend: return False, "Nenhum lote de Matemática pendente."

    raw = (text or "").strip().lower()
    if raw in {"ok","ok!","ok."}:
        spec = pend.get("spec", {})
        user["history"]["matematica"].append({
            "tipo":"lote","curriculum":spec,
            "problems":pend["problems"],"answers":pend["answers"],
            "bypass":"ok","round":pend.get("round"),"day":pend.get("day"),
        })
    else:
        expected = pend["answers"]
        got = _parse_csv_numbers(text)
        if got is None: return False, "Envie somente números separados por vírgula (ex.: 2,4,6,...)"
        if len(got) != len(expected):
            return False, f"Você enviou {len(got)} respostas, mas são {len(expected)} itens. Reenvie os {len(expected)} valores."
        wrong_idx = [i+1 for i,(g,e) in enumerate(zip(got, expected)) if g != e]
        if wrong_idx:
            pos = ", ".join(map(str, wrong_idx))
            return False, f"❌ Algumas respostas estão incorretas nas posições: {pos}. Reenvie a lista completa."
        spec = pend.get("spec", {})
        user["history"]["matematica"].append({
            "tipo":"lote","curriculum":spec,
            "problems":pend["problems"],"answers":got,
            "round":pend.get("round"),"day":pend.get("day"),
        })

    round_idx = int(pend.get("round", 1))
    rounds_total = int(pend.get("rounds_total", ROUNDS_PER_DAY))
    day = int(user.get("curriculum",{}).get("math_day",1))
    user["pending"].pop("mat_lote", None)

    if round_idx < rounds_total:
        next_round = round_idx + 1
        batch2 = _start_math_batch_for_day(user, day, next_round)
        return True, f"✅ Rodada {round_idx}/{rounds_total} concluída! Vamos para a *Rodada {next_round}/{rounds_total}*.\n\n" + _format_math_prompt(batch2)

    # Finalizou as 5 de MAT
    user["levels"]["matematica"] = user["levels"].get("matematica", 0) + 1

    if FEATURE_PORTUGUES and AUTO_SEQUENCE_PT_AFTER_MATH:
        user["pending"].pop("pt_lote", None)
        cur_pt = user.setdefault("curriculum_pt", {"pt_day": 1, "total_days": MAX_PT_DAY})
        cur_pt["pt_day"] = day
        batch2 = _start_pt_batch_for_day(user, day, 1)
        return True, f"🎉 *Matemática do dia {day} concluída!* Agora vamos para *Português* (5 rodadas).\n\n" + _format_pt_prompt(batch2)

    _close_day_and_notify(user, day)
    cur = user.setdefault("curriculum", {"math_day": 1, "total_days": MAX_MATH_DAY})
    next_day = min(MAX_MATH_DAY, int(cur.get("math_day",1)) + 1)
    cur["math_day"] = next_day
    if day == MAX_MATH_DAY and round_idx == rounds_total:
        return True, "🎉 *Parabéns!* Você concluiu o plano até o *dia 60*. Para recomeçar, envie *reiniciar*."
    batch2 = _start_math_batch_for_day(user, next_day, 1)
    return True, f"🎉 *Dia {day} concluído!* {first_name_from_profile(user).title()} foi muito bem.\n\n" + _format_math_prompt(batch2)

# ------------------- Correção / avanço (Português) -------------------
def _check_pt_batch(user, text: str):
    pend = user.get("pending", {}).get("pt_lote")
    if not pend: return False, "Nenhum lote de Português pendente."

    raw = (text or "").strip().lower()
    if raw in {"ok","ok!","ok."}:
        spec = pend.get("spec", {})
        user["history"]["portugues"].append({
            "tipo":"lote","spec":spec,
            "problems":pend["problems"],"answers":pend["answers"],
            "bypass":"ok","round":pend.get("round"),"day":pend.get("day"),
        })
    else:
        expected = pend["answers"]
        got = _parse_csv_tokens(text)
        if got is None: return False, "Envie respostas *textuais* separadas por vírgula (ex.: p,b,a,pa,ga...)."
        if len(got) != len(expected):
            return False, f"Você enviou {len(got)} respostas, mas são {len(expected)} itens. Reenvie os {len(expected)} valores."
        wrong_idx = [i+1 for i,(g,e) in enumerate(zip(got, expected)) if g != (e or "").lower()]
        if wrong_idx:
            pos = ", ".join(map(str, wrong_idx))
            return False, f"❌ Algumas respostas estão incorretas nas posições: {pos}. Reenvie a lista completa."
        spec = pend.get("spec", {})
        user["history"]["portugues"].append({
            "tipo":"lote","spec":spec,
            "problems":pend["problems"],"answers":got,
            "round":pend.get("round"),"day":pend.get("day"),
        })

    round_idx = int(pend.get("round", 1))
    rounds_total = int(pend.get("rounds_total", ROUNDS_PER_DAY))
    day = int(user.get("curriculum_pt",{}).get("pt_day",1))
    user["pending"].pop("pt_lote", None)

    if round_idx < rounds_total:
        next_round = round_idx + 1
        batch2 = _start_pt_batch_for_day(user, day, next_round)
        return True, f"✅ Rodada {round_idx}/{rounds_total} (PT) concluída! Vamos para a *Rodada {next_round}/{rounds_total}*.\n\n" + _format_pt_prompt(batch2)

    user["levels"]["portugues"] = user["levels"].get("portugues", 0) + 1

    if FEATURE_LEITURA and AUTO_SEQUENCE_READ_AFTER_PT:
        user.setdefault("history", {}).setdefault("leitura", [])
        user.setdefault("levels", {}).setdefault("leitura", 0)
        msg = _reading_start_for_user(user)
        return True, f"🎉 *Português do dia {day} concluído!* Agora vamos para *Leitura*.\n\n{msg}"

    cur_pt  = user.setdefault("curriculum_pt", {"pt_day": 1, "total_days": MAX_PT_DAY})
    cur_mat = user.setdefault("curriculum",   {"math_day": 1, "total_days": MAX_MATH_DAY})
    current_day = int(pend.get("day", day))
    next_day = current_day + 1
    cur_pt["pt_day"]    = min(MAX_PT_DAY,  next_day)
    cur_mat["math_day"] = min(MAX_MATH_DAY, next_day)
    _close_day_and_notify(user, current_day)
    if current_day == MAX_PT_DAY:
        return True, "🎉 *Parabéns!* Você concluiu o plano de Português. Para recomeçar, envie *reiniciar pt*."
    return True, f"🎉 *Dia {current_day} concluído!* Amanhã seguimos com *Matemática do dia {next_day}*. Envie *iniciar* quando quiser começar."

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
        "Vou te acompanhar em atividades de *Matemática*, *Português* e *Leitura*.\n\n"
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
    if not pend: return ob_summary(data)
    day = pend[0]
    data["schedule"]["current_day"] = day
    label = DAYS_PT.get(day, day)
    return f"Qual *horário* para *{label}*? (ex.: 18:30, 19h, 7 pm) — faixa 05:00–21:30."

def _set_time_for_current_day(data, text: str) -> Optional[str]:
    hhmm = parse_time_hhmm(text)
    if not hhmm: return "Horário inválido. Exemplos: *19:00*, *18h30*, *7 pm*. Faixa aceita: 05:00–21:30."
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
        if field in {"serie","série"}:
            g = parse_grade(val)
            if not g: return "Não reconheci a *série/ano*. Exemplos: *Infantil 4*, *1º ano*, *3º ano*."
            data["grade"] = g
        elif field in {"crianca","criança"}:
            data["child_phone"] = normalize_phone(val)
        elif field in {"pais","pais/responsaveis"}:
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
            user.setdefault("daily_flags", {})
            return ("Maravilha! ✅ Cadastro e rotina definidos.\n"
                    "Envie *iniciar* (Matemática). Depois vem *Português* e *Leitura* automaticamente.")
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
    levels.setdefault("leitura", 0)

    history = user.setdefault("history", {})
    history.setdefault("matematica", [])
    history.setdefault("portugues", [])
    history.setdefault("leitura", [])

    user.setdefault("daily_flags", {})

    # -------- RESET TOTAL (#resetar) --------
    if low == "#resetar":
        fresh = {
            "profile": {},
            "onboarding": {"step": "name", "data": {}},
            "pending": {},
            "curriculum": {"math_day": 1, "total_days": MAX_MATH_DAY},
            "curriculum_pt": {"pt_day": 1, "total_days": MAX_PT_DAY},
            "levels": {"matematica": 0, "portugues": 0, "leitura": 0},
            "history": {"matematica": [], "portugues": [], "leitura": []},
            "daily_flags": {},
            "reading": {
                "selected_book": None, "total_pages": 0,
                "start_page": None, "cursor": None,
                "last_pages": None, "awaiting_audio": False,
                "menu": []
            }
        }
        db["users"][user_id] = fresh
        save_db(db)
        return reply_twiml("♻️ Tudo resetado. Vamos começar do zero.\n" + ob_start())

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
    if low in {"menu","ajuda","help"}:
        reply = (
            f"Fluxo do dia: *5 Matemática* → *5 Português* → *Leitura* (3 páginas) → fim do dia.\n\n"
            "MAT: 1) Adição  2) Subtração  3) Multiplicação  4) Divisão  5) Mista.\n"
            "PT : 1) Som inicial  2) Sílabas  3) Decodificação  4) Ortografia  5) Leitura.\n"
            f"LEITURA: escolha 1 PDF ({BOOKS_DIR}), 3 páginas sequenciais por sessão, áudio ≥ {MIN_AUDIO_SEC}s, nota > 8.\n\n"
            "Responda em *CSV* (vírgulas) nos módulos de MAT/PT ou envie *ok* para pular.\n"
            "Comandos: *iniciar*, *iniciar pt*, *iniciar leitura*, *livros*, *escolher <número>*, *inicio <n>*, "
            "*resposta ...*, *ok*, *status*, *debug*, *reiniciar*, *reiniciar pt*, *#resetar*."
        )
        return reply_twiml(reply)

    # ========== LEITURA — utilitários/controle ==========
    st_read = _reading_state(user)

    if low == "livros":
        msg = _reading_menu_text(user)
        db["users"][user_id] = user; save_db(db)
        return reply_twiml(msg)

    # seleção numérica: "escolher 2" ou "livro 2"
    m_sel_num = re.match(r"^(?:escolher|livro)\s+(\d+)$", low)
    if m_sel_num:
        num = int(m_sel_num.group(1))
        msg = _reading_select_book(user, str(num))
        db["users"][user_id] = user; save_db(db)
        return reply_twiml(msg)

    # atalho: se a lista foi mostrada e o usuário manda só "2"
    m_only_num = re.match(r"^(\d{1,3})$", low)
    if m_only_num and (st_read.get("menu") or not st_read.get("selected_book")):
        num = int(m_only_num.group(1))
        msg = _reading_select_book(user, str(num))
        db["users"][user_id] = user; save_db(db)
        return reply_twiml(msg)

    # compat: escolher por nome (ainda aceito, mas preferimos número)
    m_sel_name = re.match(r"^escolher\s+livro\s+(.+)$", low)
    if m_sel_name:
        name = m_sel_name.group(1).strip()
        msg = _reading_select_book(user, name)
        db["users"][user_id] = user; save_db(db)
        return reply_twiml(msg)

    m_ini = re.match(r"^inicio\s+(\d+)$", low)
    if m_ini:
        n = int(m_ini.group(1))
        msg = _reading_set_start(user, n)
        db["users"][user_id] = user; save_db(db)
        return reply_twiml(msg)

    if low in {"iniciar leitura","leitura iniciar"}:
        msg = _reading_start_for_user(user)
        db["users"][user_id] = user; save_db(db)
        return reply_twiml(msg)

    # -------- Iniciar sessões MAT/PT --------
    if low == "iniciar":
        if "pt_lote" in user.get("pending", {}):
            batch = user["pending"]["pt_lote"]
            db["users"][user_id] = user; save_db(db)
            return reply_twiml("Estamos em *Português* agora. Conclua as 5 rodadas de PT.\n\n" + _format_pt_prompt(batch))
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
        saudacao = f"Olá, {nome}! Vamos iniciar *Matemática* de hoje (5 rodadas). 👋"
        return reply_twiml(saudacao + "\n\n" + _format_math_prompt(batch))

    if low in {"iniciar pt","pt iniciar","iniciar português","iniciar portugues"}:
        if "mat_lote" in user.get("pending", {}):
            batch = user["pending"]["mat_lote"]
            db["users"][user_id] = user; save_db(db)
            return reply_twiml("Estamos em *Matemática* agora. Termine as 5 rodadas de MAT antes do Português.\n\n" + _format_math_prompt(batch))
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
        saudacao = f"Olá, {nome}! Vamos iniciar *Português* de hoje (5 rodadas). 👋"
        return reply_twiml(saudacao + "\n\n" + _format_pt_prompt(batch))

    # -------- Respostas MAT/PT --------
    if low in {"ok","ok!","ok."} and ("pt_lote" not in user.get("pending", {}) and "mat_lote" not in user.get("pending", {})):
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

    # -------- Áudio (LEITURA) --------
    try:
        if int(payload.get("NumMedia", "0") or "0") > 0:
            msg = _handle_audio_submission(user, payload)
            if msg:
                db["users"][user_id] = user; save_db(db)
                return reply_twiml(msg)
    except Exception:
        pass

    return reply_twiml("Envie *iniciar* (Matemática). O fluxo é: MAT → PT → LEITURA (3 páginas).")

if __name__ == "__main__":
    # Railway define PORT automaticamente
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
