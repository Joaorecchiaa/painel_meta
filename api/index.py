"""
Board Academy — Painel TV: Atingimento de Meta (BRUTO) por Closer
Deploy: Vercel (serverless function, runtime @vercel/python)

Mostra, para cada closer, o valor BRUTO já vendido no mês (campo "value" do
deal, sem multiplicador) contra a meta financeira, numa barra quebrada de
10 em 10 até 100% (e além, se estourar a meta).

Regra de avanço de nível: só passa pro próximo bloco de 10% quando bate
EXATAMENTE aquele percentual (ex: 29,99% fica no bloco 20%, só vira 30%
quando atingir >= 30%).

Cores dos blocos (fixas por faixa, independente de quem está vendo):
  - 10% a 60%  -> vermelho
  - 70% a 90%  -> amarelo
  - 100%       -> verde
  - acima de 100% (110%, 120%...) -> azul

Só entram closers do time presencial, ou seja, dos squads/funis:
  ELITE, SNIPER, OLYMPUS (também aparece como "MGM" na planilha), NAVIGATOR

ENV VARS necessárias (configurar em Vercel > Project > Settings > Environment Variables):
  PIPE_API_KEY   -> token da API do Pipedrive
  FILTER_DEALS   -> id do filtro de negócios ganhos (default 74674)
  URL_METAS      -> CSV publicado da planilha de metas (aba com nome/mes/ano/meta_fin/meta_reu)
  URL_COLAB      -> CSV publicado da planilha de colaboradores (aba com nome/subarea/status)
  SECRET_KEY     -> opcional

Rotas:
  GET /painel-bruto            -> HTML do painel (auto-refresh)
  GET /api/painel-bruto        -> JSON com os dados (usado pelo próprio painel)
      querystring opcional: ?mes=9&ano=2026  (default: mês/ano atual)
"""

from flask import Flask, jsonify, request, render_template_string, make_response
import requests as req
import os
import csv
import math
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from io import StringIO

try:
    from zoneinfo import ZoneInfo
    TZ_BR = ZoneInfo("America/Sao_Paulo")
except Exception:
    # fallback (não deveria acontecer com "tzdata" no requirements.txt) —
    # usa deslocamento fixo de -3h a partir do UTC "de verdade"
    TZ_BR = timezone(timedelta(hours=-3))


def agora_br():
    """Horário atual em Brasília, ancorado em UTC de verdade — não depende do
    fuso local configurado no servidor (a Vercel pode rodar com o relógio já
    em horário de Brasília dependendo da região, e subtrair mais 3h por cima
    disso é o que causava o painel mostrar um horário 3h atrasado)."""
    return datetime.now(timezone.utc).astimezone(TZ_BR)


try:
    # Carrega variáveis do arquivo .env automaticamente quando rodando local.
    # Em produção (Vercel), as env vars já vêm do painel do projeto e esta
    # chamada simplesmente não encontra nenhum .env — sem efeito colateral.
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "boardacademy2026secret")

API_KEY      = os.environ.get("PIPE_API_KEY", "")
BASE_V1      = "https://boardacademy.pipedrive.com/api/v1"
FILTER_DEALS = int(os.environ.get("FILTER_DEALS", "74674"))

URL_METAS = os.environ.get(
    "URL_METAS",
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vSvwO3Ag2f2cbkVgR1pJZp6fANQcbualGKlAG50fmOljuEGKZ1gJBbSAjRdO3SomXUEVQOWnTvlfHRd/pub?gid=0&single=true&output=csv",
)

# Planilha de colaboradores (nome + subarea/squad), mesma usada no server_17.py
URL_COLAB = os.environ.get(
    "URL_COLAB",
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vSvwO3Ag2f2cbkVgR1pJZp6fANQcbualGKlAG50fmOljuEGKZ1gJBbSAjRdO3SomXUEVQOWnTvlfHRd/pub?gid=1782440078&single=true&output=csv",
)

# Pessoas ignoradas no cálculo (mesma lista do server_17.py)
EXCLUIR_PESSOAS_CALC = {"priscila ribeiro"}

# Só entram closers desses funis/squads (time presencial) — "olympus" e "mgm"
# são o mesmo squad com dois nomes diferentes na planilha, então os dois entram
SQUADS_PERMITIDOS = {"elite", "sniper", "olympus", "mgm", "navigator"}

# Denise Mussolin gerencia Elite/Sniper/Olympus mas aparece com subarea
# "Ascensão" na planilha de colaboradores — entra sempre, independente do squad
PESSOAS_SEMPRE_INCLUIR = {"denise mussolin"}

# Metas compartilhadas: as vendas de todo mundo listado aqui são somadas e
# aparecem numa única linha no painel, sob o nome/foto da pessoa "principal"
# (a chave do dicionário). A meta usada é a da pessoa principal — não soma as
# metas dos dois, só o valor bruto vendido. Ex.: Denise Mussolin e Mylena
# Oliveira têm a mesma meta de 500k e todas as vendas das duas contam pra
# essa meta única.
GRUPOS_META_COMPARTILHADA = {
    "denise mussolin": ["denise mussolin", "mylena oliveira"],
}
NOME_EXIBICAO_GRUPO = {
    "denise mussolin": "TIME ASCENSÃO",
}
# nomes que só existem "dentro" de um grupo — não devem aparecer como linha própria
MEMBROS_SECUNDARIOS_GRUPO = {
    mem for principal, membros in GRUPOS_META_COMPARTILHADA.items()
    for mem in membros if mem != principal
}

# Repositório público no GitHub com as fotos do time (arquivos "Nome Sobrenome.ext")
GITHUB_REPO_FOTOS = os.environ.get("GITHUB_REPO_FOTOS", "negocios87-sketch/fotos_time_comercial")
_FOTOS_CACHE = None
_FOTOS_CACHE_TS = 0
_FOTOS_CACHE_TTL = 6 * 3600  # 6 horas — as fotos quase nunca mudam, evita bater na API do GitHub toda hora


# ── HELPERS ──────────────────────────────────────────────────────
def norm(s):
    if not s:
        return ""
    s = str(s).strip().lower()
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode()


def arred(v):
    try:
        f = float(v)
        return 0.0 if math.isnan(f) or math.isinf(f) else round(f, 2)
    except Exception:
        return 0.0


def safe_div(a, b):
    try:
        return float(a) / float(b) if b else 0.0
    except Exception:
        return 0.0


def get_owner_name(deal):
    uid = deal.get("user_id")
    if isinstance(uid, dict):
        return uid.get("name", "")
    return ""


def get_owner_id(deal):
    uid = deal.get("user_id")
    if isinstance(uid, dict):
        return uid.get("id")
    return uid


def won_time_br(deal):
    wt = deal.get("won_time", "")
    if not wt:
        return ""
    try:
        dt = datetime.fromisoformat(str(wt).replace("Z", "+00:00"))
        dt_br = dt - timedelta(hours=3)
        return dt_br.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(wt)


def ler_sheet_rows(url, tentativas=3):
    """Lê um CSV publicado do Google Sheets sem depender de pandas
    (mantém o pacote leve para o runtime serverless da Vercel).
    O Google às vezes demora/soluça na primeira tentativa (redireciona
    pra doc-xx-xx-sheets.googleusercontent.com), então tenta de novo
    algumas vezes com timeout maior antes de desistir."""
    erro_final = None
    for i in range(tentativas):
        try:
            resp = req.get(url, timeout=30)
            resp.encoding = "utf-8"
            resp.raise_for_status()
            reader = csv.DictReader(StringIO(resp.text))
            rows = list(reader)
            fieldnames = [c.strip() for c in (reader.fieldnames or [])]
            return rows, fieldnames
        except Exception as e:
            erro_final = e
            if i < tentativas - 1:
                time.sleep(1.5 * (i + 1))
    raise erro_final


def buscar_users_pipe():
    resp = req.get(f"{BASE_V1}/users", params={"api_token": API_KEY}, timeout=15)
    resp.raise_for_status()
    return {u["id"]: u["name"] for u in (resp.json().get("data") or [])}


def buscar_fotos_time():
    """Lê a listagem do repositório público do GitHub com as fotos do time e
    devolve {nome_norm: url_da_foto}. Fica em cache por algumas horas (o
    repositório de fotos não muda quase nunca, e a API pública do GitHub tem
    limite de requisições por hora)."""
    global _FOTOS_CACHE, _FOTOS_CACHE_TS
    agora = time.time()
    if _FOTOS_CACHE is not None and (agora - _FOTOS_CACHE_TS) < _FOTOS_CACHE_TTL:
        return _FOTOS_CACHE

    try:
        resp = req.get(
            f"https://api.github.com/repos/{GITHUB_REPO_FOTOS}/contents/",
            headers={"Accept": "application/vnd.github+json"},
            timeout=15,
        )
        resp.raise_for_status()
        itens = resp.json()
        mapa = {}
        for item in itens if isinstance(itens, list) else []:
            if item.get("type") != "file":
                continue
            nome_arquivo = item.get("name", "")
            base = os.path.splitext(nome_arquivo)[0]
            nn = norm(base)
            url_foto = item.get("download_url") or ""
            if nn and url_foto:
                mapa[nn] = url_foto
        _FOTOS_CACHE = mapa
        _FOTOS_CACHE_TS = agora
        return mapa
    except Exception:
        # se der erro (rate limit, rede etc), mantém o cache antigo (mesmo vazio)
        # em vez de derrubar o painel inteiro
        return _FOTOS_CACHE or {}


def buscar_colaboradores_squad(mes, ano):
    """Lê a planilha de colaboradores e devolve {nome_norm: subarea_norm}.
    Mesma lógica do server_17.py (filtra pelo mês/ano de referência quando
    existir essa coluna, dedup por nome, filtra status=ativo se existir)."""
    rows, cols = ler_sheet_rows(URL_COLAB)

    mes_col    = next((c for c in cols if "mes" in norm(c) and "ref" in norm(c)), None)
    ano_col    = next((c for c in cols if "ano" in norm(c) and "ref" in norm(c)), None)
    nome_col   = next((c for c in cols if norm(c) == "nome"), None)
    sub_col    = next((c for c in cols if norm(c) == "subarea"), None)
    status_col = next((c for c in cols if "status" in norm(c)), None)

    def to_int(v):
        try:
            return int(float(str(v)))
        except Exception:
            return 0

    filtradas = rows
    if mes_col and ano_col and mes and ano:
        tmp = [r for r in rows if to_int(r.get(mes_col)) == mes and to_int(r.get(ano_col)) == ano]
        if tmp:
            vistos, dedup = set(), []
            for r in tmp:
                nn = norm(r.get(nome_col, "")) if nome_col else ""
                if nn in vistos:
                    continue
                vistos.add(nn)
                dedup.append(r)
            filtradas = dedup

    if status_col:
        filtradas = [r for r in filtradas if norm(r.get(status_col, "")) == "ativo"]

    mapa = {}
    for r in filtradas:
        nn  = norm(r.get(nome_col, "")) if nome_col else ""
        sub = norm(r.get(sub_col, "")) if sub_col else ""
        if nn:
            mapa[nn] = sub
    return mapa


def buscar_metas_todas(ano, mes):
    rows, cols = ler_sheet_rows(URL_METAS)

    def to_num(v):
        try:
            if v is None:
                return 0.0
            return float(str(v).replace("R$", "").replace(".", "").replace(",", ".").strip() or "0")
        except Exception:
            return 0.0

    col_ano  = next((c for c in cols if norm(c) == "ano"), None)
    col_mes  = next((c for c in cols if norm(c) == "mes"), None)
    col_nome = next((c for c in cols if norm(c) == "nome"), None)
    col_reu  = next((c for c in cols if "reuni" in norm(c) and "meta" in norm(c)), None)
    col_fin  = next((c for c in cols if "financ" in norm(c)), None)

    out = []
    for row in rows:
        try:
            a = int(float(str(row.get(col_ano, "")))) if col_ano else 0
            m = int(float(str(row.get(col_mes, "")))) if col_mes else 0
        except Exception:
            continue
        if a != ano or m != mes:
            continue
        nome_raw = str(row.get(col_nome, "")).strip() if col_nome else ""
        meta_reu = to_num(row.get(col_reu)) if col_reu else 0.0
        meta_fin = to_num(row.get(col_fin)) if col_fin else 0.0
        out.append({
            "nome": nome_raw, "nome_norm": norm(nome_raw),
            "meta_reu": meta_reu, "meta_fin": meta_fin,
        })
    return out


def buscar_deals_mes(mes, ano):
    todos, start = [], 0
    mes_str = f"{ano}-{mes:02d}"
    while True:
        resp = req.get(f"{BASE_V1}/deals", params={
            "filter_id": FILTER_DEALS, "status": "won",
            "sort": "won_time DESC", "limit": 500,
            "start": start, "api_token": API_KEY,
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        lote = data.get("data") or []
        found_older = False
        for deal in lote:
            wt_br = won_time_br(deal)[:7]
            if wt_br == mes_str:
                todos.append(deal)
            elif wt_br < mes_str:
                found_older = True
        mais = data.get("additional_data", {}).get("pagination", {}).get("more_items_in_collection", False)
        if not mais or not lote or found_older:
            break
        start += 500
    return todos


# ── CÁLCULO PRINCIPAL ────────────────────────────────────────────
def calcular_painel(mes=None, ano=None):
    hoje = agora_br().date()  # data de hoje em Brasília, não a do relógio local do servidor
    mes = mes or hoje.month
    ano = ano or hoje.year

    metas       = buscar_metas_todas(ano, mes)
    users_pipe  = buscar_users_pipe()
    deals       = buscar_deals_mes(mes, ano)
    nome_squad  = buscar_colaboradores_squad(mes, ano)  # nome_norm -> subarea normalizada
    fotos       = buscar_fotos_time()  # nome_norm -> url da foto

    uid_to_nome_norm = {uid: norm(name) for uid, name in users_pipe.items()}

    # Soma o valor BRUTO (campo "value", sem multiplicador) ganho por closer
    closer_bruto = {}
    for deal in deals:
        owner_nome = norm(get_owner_name(deal))
        if not owner_nome:
            oid = get_owner_id(deal)
            owner_nome = uid_to_nome_norm.get(oid, "")
        if not owner_nome:
            continue
        valor = float(deal.get("value") or 0)
        closer_bruto[owner_nome] = closer_bruto.get(owner_nome, 0.0) + valor

    # Só entram quem tem meta financeira, não tem meta de reunião (= closer),
    # e pertence a um dos squads/funis do time presencial
    closers_metas = [
        m for m in metas
        if m["meta_reu"] == 0 and m["meta_fin"] > 0
        and m["nome_norm"] not in EXCLUIR_PESSOAS_CALC
        and m["nome_norm"] not in MEMBROS_SECUNDARIOS_GRUPO
        and (nome_squad.get(m["nome_norm"], "") in SQUADS_PERMITIDOS or m["nome_norm"] in PESSOAS_SEMPRE_INCLUIR)
    ]

    resultado = []
    for m in closers_metas:
        nn      = m["nome_norm"]
        membros = GRUPOS_META_COMPARTILHADA.get(nn, [nn])
        bruto   = sum(closer_bruto.get(mem, 0.0) for mem in membros)
        meta    = m["meta_fin"]
        pct     = safe_div(bruto, meta) * 100
        resultado.append({
            "nome": NOME_EXIBICAO_GRUPO.get(nn, m["nome"]),
            "bruto": arred(bruto),
            "meta": arred(meta),
            "pct": arred(pct),
            "foto": fotos.get(nn, ""),  # foto da pessoa "principal" do grupo (nn)
        })

    resultado.sort(key=lambda x: x["pct"], reverse=True)

    return {
        "periodo": {
            "mes": mes, "ano": ano,
            "atualizado_em": agora_br().strftime("%d/%m/%Y %H:%M:%S"),
        },
        "closers": resultado,
    }


def _sem_cache(resp):
    """Garante que nem o navegador, nem a CDN da Vercel, guardem essa resposta em cache —
    sem isso o painel pode ficar preso mostrando dados antigos (horário parado, valores errados)."""
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ── ROTAS ─────────────────────────────────────────────────────
@app.route("/api/painel-bruto")
def api_painel_bruto():
    try:
        mes = request.args.get("mes", type=int)
        ano = request.args.get("ano", type=int)
        data = calcular_painel(mes, ano)
        return _sem_cache(jsonify(data))
    except Exception as e:
        return _sem_cache(jsonify({"erro": str(e)})), 500


@app.route("/painel-bruto")
@app.route("/")
def painel_bruto():
    return _sem_cache(make_response(render_template_string(TEMPLATE)))


TEMPLATE = r"""
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Atingimento de Meta — Bruto</title>
<style>
  :root{
    --gold: #FFD700;
    --gold-soft: #cbbf94;
    --black: #0d0d0d;
    --gray: #1a1a1a;
    --text: #f2f2f2;
    --muted: #a8a8a8;
    --border: #2b2b2b;
    --dark-text: #111111;
    --gold-glow: rgba(255,215,0,.25);

    --bg: var(--black);
    --bg-card: var(--gray);
    --text-dim: var(--muted);
    --red: #e5473a;
    --yellow: #f5a623;
    --green: #2fb35c;
    --blue: #2f7de1;
    --empty: #232323;
    --rowH: 72px;   /* recalculado via JS conforme a quantidade de closers, pra sempre caber na tela sem cortar */
    --gap: 8px;
  }
  *{box-sizing:border-box; margin:0; padding:0;}
  html,body{
    background:var(--bg);
    color:var(--text);
    font-family:"Segoe UI", Arial, sans-serif;
    height:100%;
    overflow:hidden;
  }
  body{ background: var(--black); }
  .bg-glow{
    position:fixed;
    inset:0;
    z-index:0;
    pointer-events:none;
    background:
      radial-gradient(circle at 20% 30%, rgba(255,215,0,.04), transparent 32%),
      radial-gradient(circle at 80% 70%, rgba(255,215,0,.03), transparent 32%),
      radial-gradient(circle at 50% 50%, rgba(255,215,0,.02), transparent 38%);
    background-size:150% 150%;
    animation:drift 22s ease-in-out infinite;
  }
  @keyframes drift{
    0%{ background-position:0% 0%, 100% 100%, 50% 50%; }
    50%{ background-position:60% 40%, 40% 60%, 55% 45%; }
    100%{ background-position:0% 0%, 100% 100%, 50% 50%; }
  }
  .particles{
    position:fixed;
    inset:0;
    z-index:0;
    pointer-events:none;
    overflow:hidden;
  }
  .particle{
    position:absolute;
    bottom:-20px;
    width:4px;
    height:4px;
    background:#FFD700;
    border-radius:50%;
    opacity:.25;
    animation:rise linear infinite;
  }
  @keyframes rise{
    0%{ transform:translateY(0) translateX(0); opacity:0; }
    10%{ opacity:.35; }
    90%{ opacity:.25; }
    100%{ transform:translateY(-110vh) translateX(20px); opacity:0; }
  }
  .wrap{
    padding:2.2vh 2.4vw;
    height:100vh;
    display:flex;
    flex-direction:column;
    position:relative;
    z-index:1;
  }
  header{
    display:flex;
    justify-content:space-between;
    align-items:flex-end;
    margin-bottom:1.6vh;
    flex-shrink:0;
  }
  h1{
    font-size:1.15vw;
    letter-spacing:0.08em;
    font-weight:800;
    text-transform:uppercase;
    color:var(--text);
  }
  h1 span{ color:var(--gold-soft); font-weight:700; }
  .meta-info{
    text-align:right;
    color:var(--muted);
    font-size:0.75vw;
  }
  #lista{
    flex:1;
    display:flex;
    flex-direction:column;
    gap:var(--gap);
    overflow:hidden;
    justify-content:flex-start;
    min-height:0;
  }
  .row{
    background:var(--bg-card);
    border:1px solid var(--border);
    border-radius:10px;
    padding:0 1.4vw;
    display:flex;
    align-items:center;
    gap:1.4vw;
    height:var(--rowH);
    flex-shrink:0;
  }
  .avatar-wrap{
    width:calc(var(--rowH) * 0.78);
    height:calc(var(--rowH) * 0.78);
    flex-shrink:0;
    border-radius:50%;
    overflow:hidden;
    background:var(--empty);
    border:1px solid var(--border);
    position:relative;
    animation: avatarFloat 4.5s ease-in-out infinite;
  }
  @keyframes avatarFloat{
    0%, 100%{ transform: translateY(0) scale(1); }
    50%{ transform: translateY(-3px) scale(1.03); }
  }
  .avatar-wrap img{
    width:100%;
    height:100%;
    object-fit:cover;
    display:block;
  }
  .avatar-fallback{
    position:absolute;
    inset:0;
    display:none;
    align-items:center;
    justify-content:center;
    font-weight:700;
    font-size:calc(var(--rowH) * 0.26);
    color:var(--text-dim);
    background:var(--empty);
  }
  .avatar-dupla{
    width:calc(var(--rowH) * 0.78);
    flex-shrink:0;
    display:flex;
    flex-direction:column;
    align-items:center;
    gap:calc(var(--rowH) * 0.08);
    animation: avatarFloat 4.5s ease-in-out infinite;
  }
  .avatar-dupla .avatar-mini{
    width:calc(var(--rowH) * 0.42);
    height:calc(var(--rowH) * 0.42);
    border-radius:50%;
    overflow:hidden;
    background:var(--empty);
    border:1px solid var(--border);
    position:relative;
    flex-shrink:0;
  }
  .avatar-dupla .avatar-mini img{
    width:100%;
    height:100%;
    object-fit:cover;
    display:block;
  }
  .avatar-dupla .avatar-fallback{
    font-size:calc(var(--rowH) * 0.14);
  }
  .nome-col{
    width:17vw;
    flex-shrink:0;
    overflow:hidden;
  }
  .nome{
    font-size:calc(var(--rowH) * 0.30);
    font-weight:700;
    white-space:nowrap;
    overflow:hidden;
    text-overflow:ellipsis;
    line-height:1.15;
  }
  .nome-dupla{
    display:flex;
    flex-direction:column;
    justify-content:center;
    gap:calc(var(--rowH) * 0.02);
  }
  .nome-dupla .nome{
    font-size:calc(var(--rowH) * 0.22);
    line-height:1.2;
  }
  .barra{
    flex:1;
    display:flex;
    gap:0.35vw;
    height:calc(var(--rowH) * 0.42);
    align-items:stretch;
  }
  .legenda-row{
    display:flex;
    align-items:center;
    gap:1.4vw;
    padding:0 1.4vw;
    margin-bottom:0.8vh;
    flex-shrink:0;
  }
  .legenda-avatar-spacer{
    width:calc(var(--rowH) * 0.78);
    flex-shrink:0;
  }
  .legenda-nome-spacer{
    width:17vw;
    flex-shrink:0;
  }
  .legenda-barra{
    flex:1;
    display:flex;
    gap:0.35vw;
  }
  .legenda-barra .lbl{
    flex:1;
    text-align:center;
    font-size:calc(var(--rowH) * 0.24);
    font-weight:800;
    color:#ffffff;
    opacity:0.95;
  }
  .legenda-pct-spacer{
    width:5.2vw;
    flex-shrink:0;
  }
  .bloco{
    flex:1;
    border-radius:5px;
    background:var(--empty);
    position:relative;
    overflow:hidden;
  }
  .bloco.on{ box-shadow: inset 0 0 0 1px rgba(255,255,255,0.15); }
  .bloco.on::after{
    content:"";
    position:absolute;
    top:0; left:-150%;
    width:60%; height:100%;
    background:linear-gradient(90deg, transparent, rgba(255,255,255,0.45), transparent);
    animation: shimmer 5s ease-in-out infinite;
  }
  @keyframes shimmer{
    0%{ left:-60%; }
    100%{ left:110%; }
  }
  .bloco.red{ background:var(--red); }
  .bloco.yellow{ background:var(--yellow); }
  .bloco.green{ background:var(--green); }
  .bloco.blue{ background:var(--blue); }
  .pct{
    width:5.2vw;
    text-align:right;
    font-size:calc(var(--rowH) * 0.20);
    font-weight:800;
    flex-shrink:0;
  }
  .pct.red{ color:var(--red); }
  .pct.yellow{ color:var(--yellow); }
  .pct.green{ color:var(--green); }
  .pct.blue{ color:var(--blue); }
  .erro{
    color:var(--red);
    font-size:1.2vw;
    padding:2vh;
  }
</style>
</head>
<body>
<div class="bg-glow"></div>
<div class="particles" id="particles"></div>
<div class="wrap">
  <header>
    <div>
      <h1>Atingimento de Meta <span>— Bruto</span></h1>
    </div>
    <div class="meta-info">
      <div id="periodo">—</div>
      <div id="atualizado">—</div>
    </div>
  </header>
  <div id="legenda" class="legenda-row"></div>
  <div id="lista"></div>
</div>

<script>
const MESES = ["","Janeiro","Fevereiro","Março","Abril","Maio","Junho","Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"];

function corDoBloco(v){
  if(v <= 60) return "red";
  if(v <= 90) return "yellow";
  if(v === 100) return "green";
  return "blue";
}

function iniciais(nome){
  const partes = String(nome).trim().split(/\s+/).filter(Boolean);
  const a = partes[0] ? partes[0][0] : "";
  const b = partes.length > 1 ? partes[partes.length - 1][0] : "";
  return (a + b).toUpperCase();
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, ch => ({
    "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"
  })[ch]);
}

async function carregar(){
  try{
    // "cache: no-store" + timestamp na URL evitam que o navegador (ou algum proxy no meio do
    // caminho) sirva uma resposta antiga em vez de buscar os dados de novo
    const resp = await fetch(`/api/painel-bruto?t=${Date.now()}`, { cache: "no-store" });
    const data = await resp.json();
    if(data.erro){
      document.getElementById("lista").innerHTML = `<div class="erro">Erro: ${data.erro}</div>`;
      return;
    }
    render(data);
  }catch(e){
    document.getElementById("lista").innerHTML = `<div class="erro">Falha ao carregar dados</div>`;
  }
}

let ULTIMA_QTD = 0;
let ULTIMO_PESO = 0;
const FATOR_LINHA_DUPLA = 1.65; // quanto uma linha com 2 fotos (meta compartilhada) vale a mais que uma linha normal

function ajustarAlturaLinhas(qtd, pesoTotal){
  const lista = document.getElementById("lista");
  const gapPx = 8; // precisa bater com --gap no CSS
  const alturaDisponivel = lista.clientHeight || (window.innerHeight * 0.7);
  if(qtd <= 0) return;
  const peso = pesoTotal || qtd;
  // "peso" conta linhas com meta compartilhada (2 fotos) como valendo mais de 1,
  // assim a soma de todas as alturas continua cabendo certinho na tela, sem
  // estourar (e sem precisar dar zoom out) mesmo com uma linha maior que as outras
  const alturaBruta = (alturaDisponivel - gapPx * (qtd - 1)) / peso;
  // limita entre um mínimo legível e um máximo (pra não ficar gigante com poucos closers)
  const rowH = Math.max(30, Math.min(112, Math.floor(alturaBruta)));
  document.documentElement.style.setProperty("--rowH", rowH + "px");
  document.documentElement.style.setProperty("--gap", gapPx + "px");
}

function render(data){
  document.getElementById("periodo").textContent = `${MESES[data.periodo.mes]} / ${data.periodo.ano}`;
  document.getElementById("atualizado").textContent = `Atualizado às ${data.periodo.atualizado_em}`;

  ULTIMA_QTD = data.closers.length;
  ULTIMO_PESO = data.closers.reduce((soma, c) => soma + ((c.integrantes && c.integrantes.length > 1) ? FATOR_LINHA_DUPLA : 1), 0);
  ajustarAlturaLinhas(ULTIMA_QTD, ULTIMO_PESO);
  const rowHPx = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--rowH")) || 60;

  // nível máximo (colunas) — pelo menos 100, ou o teto de quem estourou a meta
  let maxNivel = 100;
  data.closers.forEach(c => {
    const nivel = Math.floor(c.pct / 10) * 10;
    if(nivel > maxNivel) maxNivel = nivel;
  });
  const colunas = [];
  for(let v = 10; v <= maxNivel; v += 10) colunas.push(v);

  document.getElementById("legenda").innerHTML = `
    <div class="legenda-avatar-spacer"></div>
    <div class="legenda-nome-spacer"></div>
    <div class="legenda-barra">${colunas.map(v => `<div class="lbl">${v}%</div>`).join("")}</div>
    <div class="legenda-pct-spacer"></div>
  `;

  const lista = document.getElementById("lista");
  lista.innerHTML = "";

  data.closers.forEach((c, i) => {
    const row = document.createElement("div");
    row.className = "row";

    // só avança ao bater exatamente o nível de 10 em 10 — exceto o primeiro bloco (10%),
    // que já acende a partir de 5% (4,99% ou menos não acende nada)
    let nivelAtingido = Math.floor(c.pct / 10) * 10;
    if(nivelAtingido === 0 && c.pct >= 5) nivelAtingido = 10;

    let blocosHtml = "";
    colunas.forEach(v => {
      const ligado = nivelAtingido >= v;
      const cor = ligado ? corDoBloco(v) : "";
      blocosHtml += `<div class="bloco ${ligado ? 'on ' + cor : ''}"></div>`;
    });

    const corPct = corDoBloco(nivelAtingido === 0 ? 10 : nivelAtingido);
    const nomeSeguro = escapeHtml(c.nome);
    const delayFoto = (i % 5) * 0.4; // alterna o delay pra não balançar tudo junto
    const integrantes = (c.integrantes && c.integrantes.length > 1) ? c.integrantes : null;

    // bloco com duas fotos fica um pouco mais alto que os demais, pra enquadrar melhor
    if(integrantes) row.style.setProperty("--rowH", (rowHPx * FATOR_LINHA_DUPLA) + "px");

    let avatarColHtml, nomeColHtml;
    if(integrantes){
      // duas (ou mais) pessoas dividindo a mesma meta: avatar e nome de cada uma, empilhados
      avatarColHtml = `<div class="avatar-dupla" style="animation-delay:${delayFoto}s">` +
        integrantes.map(p => {
          const nomeP = escapeHtml(p.nome);
          return p.foto
            ? `<div class="avatar-mini"><img src="${p.foto}" alt="${nomeP}" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';"><div class="avatar-fallback">${iniciais(p.nome)}</div></div>`
            : `<div class="avatar-mini"><div class="avatar-fallback" style="display:flex">${iniciais(p.nome)}</div></div>`;
        }).join("") +
        `</div>`;
      nomeColHtml = `<div class="nome-dupla">` +
        integrantes.map(p => `<div class="nome">${escapeHtml(p.nome)}</div>`).join("") +
        `</div>`;
    } else {
      const avatarHtml = c.foto
        ? `<img src="${c.foto}" alt="${nomeSeguro}" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
           <div class="avatar-fallback">${iniciais(c.nome)}</div>`
        : `<div class="avatar-fallback" style="display:flex">${iniciais(c.nome)}</div>`;
      avatarColHtml = `<div class="avatar-wrap" style="animation-delay:${delayFoto}s">${avatarHtml}</div>`;
      nomeColHtml = `<div class="nome">${nomeSeguro}</div>`;
    }

    row.innerHTML = `
      ${avatarColHtml}
      <div class="nome-col">
        ${nomeColHtml}
      </div>
      <div class="barra">${blocosHtml}</div>
      <div class="pct ${corPct}">${c.pct.toFixed(1).replace('.', ',')}%</div>
    `;
    lista.appendChild(row);
  });
}

carregar();
setInterval(carregar, 300000); // atualiza a cada 5 minutos
window.addEventListener("resize", () => { if(ULTIMA_QTD > 0) ajustarAlturaLinhas(ULTIMA_QTD, ULTIMO_PESO); });

// partículas douradas subindo no fundo (efeito sutil, puramente decorativo)
(function(){
  const container = document.getElementById('particles');
  const count = 35;
  for(let i=0;i<count;i++){
    const p = document.createElement('div');
    p.className = 'particle';
    p.style.left = Math.random()*100 + 'vw';
    p.style.width = p.style.height = (2 + Math.random()*3) + 'px';
    p.style.animationDuration = (8 + Math.random()*10) + 's';
    p.style.animationDelay = (Math.random()*10) + 's';
    container.appendChild(p);
  }
})();
</script>
</body>
</html>
"""
