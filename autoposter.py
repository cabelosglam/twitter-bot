"""
autoposter.py — Publicacao automatica de posts de financas pessoais no X/Twitter
===============================================================================

Gera o texto com a API da Anthropic (Claude) e publica pela API oficial do X
(tweepy / OAuth 1.0a User Context), 3x por dia nos horarios configurados.

Pontos de entrada:
    iniciar_scheduler()      -> liga o APScheduler com os horarios da config
    executar_ciclo(origem)   -> gera + publica UM post (idempotente por slot)
    gerar_post(cfg)          -> so gera o texto, sem publicar (preview)

O estado vive em DATA_DIR (default /tmp). No Render o disco e efemero: as
credenciais devem vir de variaveis de ambiente, nunca so do arquivo.
"""

import os
import json
import random
import difflib
import pathlib
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic
import tweepy
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ─────────────────────────────────────────
#  ARQUIVOS / ESTADO
# ─────────────────────────────────────────

DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", "/tmp"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

POSTS_CONFIG_FILE = DATA_DIR / "posts_config.json"
POSTS_HIST_FILE   = DATA_DIR / "posts_historico.json"

_lock = threading.Lock()
_scheduler = None
_log_fn = None


def set_logger(fn):
    """app.py injeta aqui a funcao de log que alimenta o painel (SSE)."""
    global _log_fn
    _log_fn = fn


def log(msg):
    print(f"[autoposter] {msg}", flush=True)
    if _log_fn:
        try:
            _log_fn(msg)
        except Exception:
            pass


# ─────────────────────────────────────────
#  TEMAS E PROMPT
# ─────────────────────────────────────────

TEMAS_PADRAO = [
    "reserva de emergencia: por que ela vem antes de qualquer investimento",
    "sair do rotativo do cartao de credito",
    "como montar um orcamento mensal que a pessoa consegue manter",
    "juros compostos trabalhando contra voce nas dividas",
    "a diferenca entre investir e apostar",
    "compra por impulso e os gatilhos que o varejo usa",
    "gastos pequenos e recorrentes que somam muito no ano (assinaturas)",
    "inflacao no dia a dia: o dinheiro parado perdendo valor",
    "renegociacao de divida: como chegar na mesa em posicao melhor",
    "custo de oportunidade de cada compra grande",
    "financiamento x consorcio x juntar e comprar a vista",
    "o erro de investir sem ter o basico resolvido",
    "metas financeiras com prazo e numero, nao com desejo vago",
    "aumentar renda x cortar gasto: quando cada um importa mais",
    "psicologia do dinheiro: comparacao social e padrao de vida",
    "o que o score de credito e e o que ele nao e",
    "primeiro salario: o habito que define os proximos dez anos",
    "aposentadoria: comecar cedo com pouco x comecar tarde com muito",
    "educacao financeira para os filhos pelo exemplo",
    "planejamento do 13o e das ferias sem furar o orcamento",
    "por que ter o dinheiro separado por objetivo muda o comportamento",
    "renda extra: avaliar hora trabalhada antes de aceitar qualquer bico",
    "o custo invisivel de parcelar tudo em 12x sem juros",
    "revisar contratos recorrentes: plano, seguro, internet, banco",
    "o perigo das promessas de rendimento rapido nas redes",
]

PROMPT_FINANCAS = """Voce escreve posts curtos sobre financas pessoais para o X (Twitter), em portugues do Brasil.

Voce fala como um brasileiro que entende do assunto na pratica — ja se enrolou com dinheiro, saiu do buraco e aprendeu. Nao e guru, nao e coach, nao vende curso. Fala de igual pra igual, com quem esta apertado no fim do mes.

TEMA DE HOJE:
{tema}

COMO VOCE ESCREVE:
- Frases curtas. Cada palavra pesa.
- Comeca pelo concreto, nao pela licao de moral.
- Uma ideia por post. Nao tenta ensinar tudo de uma vez.
- Se der um numero, use conta simples e redonda que qualquer um confere de cabeca.
- Portugues do Brasil natural, sem jargao de banco e sem ingles desnecessario.

NUNCA FACA:
- NUNCA passe de {max_chars} caracteres — conte antes de responder.
- NUNCA recomende ativo, acao, cripto, fundo ou corretora especifica.
- NUNCA prometa, garanta ou sugira retorno financeiro ("x% ao mes", "fique rico", "renda garantida").
- NUNCA invente estatistica, pesquisa, dado de mercado ou taxa atual.
- NUNCA use tom de post motivacional de Instagram.
- NUNCA mencione que e IA, nem fale de si mesmo como assistente.
- NUNCA use link.
{regra_hashtag}

POSTS RECENTES (NAO repita o tema, a estrutura nem a primeira frase deles):
{recentes}

FORMATO: 1 a 3 frases curtas, ou uma frase + uma quebra de linha + um fechamento.
Responda APENAS com o texto do post. Sem aspas, sem explicacao, sem contagem de caracteres."""

DEFAULT_POSTS_CONFIG = {
    "ativo": False,
    "timezone": "America/Sao_Paulo",
    "horarios": ["08:10", "12:40", "19:20"],
    "temas": TEMAS_PADRAO,
    "prompt": PROMPT_FINANCAS,
    "modelo": "claude-opus-5-5",
    "effort": "medium",
    "max_chars": 275,
    "hashtags": False,
    "n_recentes": 25,
    # Credenciais — preferir variaveis de ambiente (disco do Render e efemero)
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
    "x_api_key": os.getenv("X_API_KEY", ""),
    "x_api_secret": os.getenv("X_API_SECRET", ""),
    "x_access_token": os.getenv("X_ACCESS_TOKEN", ""),
    "x_access_token_secret": os.getenv("X_ACCESS_TOKEN_SECRET", ""),
    "cron_token": os.getenv("CRON_TOKEN", ""),
}

SEGREDOS = ("anthropic_api_key", "x_api_key", "x_api_secret",
            "x_access_token", "x_access_token_secret", "cron_token")


# ─────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────

def carregar_config():
    cfg = DEFAULT_POSTS_CONFIG.copy()
    if POSTS_CONFIG_FILE.exists():
        try:
            salvo = json.loads(POSTS_CONFIG_FILE.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in salvo.items() if v not in (None, "")})
        except Exception as e:
            log(f"config invalida, usando padrao ({e})")
    # Variavel de ambiente tem a ultima palavra se o arquivo nao trouxe segredo
    for k in SEGREDOS:
        if not cfg.get(k):
            cfg[k] = DEFAULT_POSTS_CONFIG.get(k, "")
    return cfg


def salvar_config(novo):
    with _lock:
        cfg = carregar_config()
        for k, v in (novo or {}).items():
            if k in SEGREDOS and v in ("", None, "••••••••"):
                continue          # nao apaga segredo ao salvar o formulario
            cfg[k] = v
        POSTS_CONFIG_FILE.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg


def config_publica():
    """Config sem expor segredos — apenas se estao preenchidos."""
    cfg = carregar_config()
    pub = {k: v for k, v in cfg.items() if k not in SEGREDOS}
    pub["credenciais"] = {k: bool(cfg.get(k)) for k in SEGREDOS}
    pub["pronto"] = bool(
        cfg.get("anthropic_api_key") and cfg.get("x_api_key")
        and cfg.get("x_api_secret") and cfg.get("x_access_token")
        and cfg.get("x_access_token_secret"))
    return pub


# ─────────────────────────────────────────
#  HISTORICO
# ─────────────────────────────────────────

def carregar_historico():
    if POSTS_HIST_FILE.exists():
        try:
            return json.loads(POSTS_HIST_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def salvar_historico(hist):
    POSTS_HIST_FILE.write_text(
        json.dumps(hist[-500:], indent=2, ensure_ascii=False), encoding="utf-8")


def registrar(entrada):
    with _lock:
        hist = carregar_historico()
        hist.append(entrada)
        salvar_historico(hist)
    return entrada


def publicados(hist=None):
    return [h for h in (hist if hist is not None else carregar_historico())
            if h.get("status") == "publicado"]


def textos_recentes(cfg, hist=None):
    n = int(cfg.get("n_recentes", 25))
    return [h["texto"] for h in publicados(hist)[-n:] if h.get("texto")]


def temas_recentes(cfg, hist=None):
    n = max(1, len(cfg.get("temas") or TEMAS_PADRAO) // 2)
    return [h.get("tema") for h in publicados(hist)[-n:] if h.get("tema")]


# ─────────────────────────────────────────
#  SLOTS (idempotencia: 1 post por horario por dia)
# ─────────────────────────────────────────

def agora(cfg):
    try:
        return datetime.now(ZoneInfo(cfg.get("timezone") or "America/Sao_Paulo"))
    except Exception:
        return datetime.now(ZoneInfo("America/Sao_Paulo"))


def slot_de(cfg, momento=None, tolerancia_min=90):
    """Qual horario configurado este momento representa. None se nenhum."""
    momento = momento or agora(cfg)
    melhor, menor = None, None
    for hhmm in cfg.get("horarios") or []:
        try:
            h, m = [int(x) for x in str(hhmm).split(":")]
        except Exception:
            continue
        alvo = momento.replace(hour=h, minute=m, second=0, microsecond=0)
        dif = abs((momento - alvo).total_seconds()) / 60
        if menor is None or dif < menor:
            melhor, menor = hhmm, dif
    if melhor is not None and menor <= tolerancia_min:
        return f"{momento.date().isoformat()}#{melhor}"
    return None


def slot_ja_publicado(slot, hist=None):
    if not slot:
        return False
    return any(h.get("slot") == slot for h in publicados(hist))


def publicados_hoje(cfg, hist=None):
    hoje = agora(cfg).date().isoformat()
    return [h for h in publicados(hist) if str(h.get("slot", "")).startswith(hoje)
            or str(h.get("ts", "")).startswith(hoje)]


def reservar_slot(slot):
    """Trava atomica no disco: so um processo/worker publica um slot.

    Evita post duplicado quando o app roda com mais de um worker do gunicorn
    (cada worker tem seu proprio scheduler) ou quando o cron externo cai junto
    com o horario agendado.
    """
    if not slot:
        return True
    lock = DATA_DIR / f"slot-{slot.replace('#', '_').replace(':', '')}.lock"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception as e:
        log(f"nao consegui criar a trava do slot ({e}); seguindo sem ela")
        return True


def liberar_slot(slot):
    """Libera a trava quando o post NAO foi publicado, para o retry funcionar."""
    if not slot:
        return
    lock = DATA_DIR / f"slot-{slot.replace('#', '_').replace(':', '')}.lock"
    try:
        lock.unlink(missing_ok=True)
    except Exception:
        pass


# ─────────────────────────────────────────
#  GERACAO DO TEXTO (Claude)
# ─────────────────────────────────────────

def escolher_tema(cfg, hist=None):
    temas = list(cfg.get("temas") or TEMAS_PADRAO)
    usados = set(temas_recentes(cfg, hist))
    disponiveis = [t for t in temas if t not in usados] or temas
    return random.choice(disponiveis)


def _limpar(texto):
    t = (texto or "").strip()
    for par in ('""', "''", "“”", "‘’", "«»"):
        if len(t) > 1 and t[0] == par[0] and t[-1] == par[1]:
            t = t[1:-1].strip()
    # Remove preambulo tipo "Post:" / "Aqui esta:"
    for pref in ("post:", "texto:", "tweet:", "aqui esta:", "aqui vai:"):
        if t.lower().startswith(pref):
            t = t[len(pref):].strip()
    return t


def _problemas(texto, cfg):
    """Validacao local. Devolve lista de problemas (vazia = aprovado)."""
    probs = []
    max_chars = int(cfg.get("max_chars", 275))
    if not texto:
        probs.append("texto vazio")
        return probs
    if len(texto) > max_chars:
        probs.append(f"tem {len(texto)} caracteres, o limite e {max_chars}")
    if not cfg.get("hashtags") and "#" in texto:
        probs.append("usou hashtag, e hashtags estao desligadas")
    if "http://" in texto or "https://" in texto or "www." in texto:
        probs.append("incluiu link, o que nao e permitido")
    baixo = texto.lower()
    for termo in ("como ia", "sou uma ia", "modelo de linguagem", "como assistente",
                  "rendimento garantido", "retorno garantido", "lucro garantido",
                  "fique rico", "dinheiro facil", "ganho garantido"):
        if termo in baixo:
            probs.append(f"usou a expressao proibida '{termo}'")
    for t in textos_recentes(cfg):
        if difflib.SequenceMatcher(None, baixo, t.lower()).ratio() > 0.72:
            probs.append("ficou parecido demais com um post recente")
            break
    return probs


def _chamar_claude(cfg, mensagens):
    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    kwargs = dict(
        model=cfg.get("modelo") or "claude-opus-5-5",
        max_tokens=2000,                       # cabe o thinking adaptativo + o post
        output_config={"effort": cfg.get("effort") or "medium"},
        messages=mensagens,
    )
    try:
        # fallback no servidor: se o modelo recusar por politica, outro assume
        resp = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            **kwargs)
    except (anthropic.BadRequestError, TypeError) as e:
        log(f"fallback do servidor indisponivel ({e}); seguindo sem ele")
        resp = client.messages.create(**kwargs)

    if getattr(resp, "stop_reason", None) == "refusal":
        det = getattr(resp, "stop_details", None)
        raise RuntimeError(f"o modelo recusou gerar o post ({getattr(det, 'category', '?')})")
    return "".join(b.text for b in resp.content if b.type == "text")


def gerar_post(cfg=None, tema=None, tentativas=3):
    """Gera e valida UM texto de post. Levanta excecao se nao conseguir."""
    cfg = cfg or carregar_config()
    if not cfg.get("anthropic_api_key"):
        raise RuntimeError("falta a chave da API da Anthropic (ANTHROPIC_API_KEY)")

    hist = carregar_historico()
    tema = tema or escolher_tema(cfg, hist)
    recentes = textos_recentes(cfg, hist)

    prompt = (cfg.get("prompt") or PROMPT_FINANCAS).format(
        tema=tema,
        max_chars=int(cfg.get("max_chars", 275)),
        regra_hashtag=("- Use no maximo UMA hashtag, e so se somar algo."
                       if cfg.get("hashtags")
                       else "- NUNCA use hashtag."),
        recentes=("\n".join(f"- {t}" for t in recentes[-12:]) or "(nenhum ainda)"),
    )

    mensagens = [{"role": "user", "content": prompt}]
    ultimo, ultimos_probs = "", ["nao gerou"]

    for n in range(1, tentativas + 1):
        texto = _limpar(_chamar_claude(cfg, mensagens))
        probs = _problemas(texto, cfg)
        if not probs:
            log(f"post gerado na tentativa {n} ({len(texto)} caracteres) — tema: {tema}")
            return {"texto": texto, "tema": tema}
        ultimo, ultimos_probs = texto, probs
        log(f"tentativa {n} rejeitada: {'; '.join(probs)}")
        mensagens += [
            {"role": "assistant", "content": texto},
            {"role": "user", "content":
                "Esse post nao serve: " + "; ".join(probs) +
                ". Reescreva do zero, mesmo tema, respeitando todas as regras. "
                "Responda apenas com o novo texto."},
        ]

    raise RuntimeError(f"nao consegui um post valido em {tentativas} tentativas "
                       f"(ultimo problema: {'; '.join(ultimos_probs)})")


# ─────────────────────────────────────────
#  PUBLICACAO (API oficial do X)
# ─────────────────────────────────────────

def cliente_x(cfg):
    faltando = [k for k in ("x_api_key", "x_api_secret",
                            "x_access_token", "x_access_token_secret")
                if not cfg.get(k)]
    if faltando:
        raise RuntimeError("faltam credenciais do X: " + ", ".join(faltando))
    return tweepy.Client(
        consumer_key=cfg["x_api_key"],
        consumer_secret=cfg["x_api_secret"],
        access_token=cfg["x_access_token"],
        access_token_secret=cfg["x_access_token_secret"],
    )


def publicar(texto, cfg=None):
    """Publica no X e devolve (tweet_id, url)."""
    cfg = cfg or carregar_config()
    client = cliente_x(cfg)
    try:
        resp = client.create_tweet(text=texto)
    except tweepy.TooManyRequests:
        raise RuntimeError("limite de requisicoes do X atingido (429). "
                           "No plano Free sao 500 posts/mes, 17/dia.")
    except tweepy.Forbidden as e:
        raise RuntimeError(f"o X recusou o post (403) — texto duplicado ou "
                           f"app sem permissao de escrita: {e}")
    except tweepy.Unauthorized:
        raise RuntimeError("credenciais do X invalidas (401). Gere novamente o "
                           "Access Token com permissao Read and write.")
    except tweepy.BadRequest as e:
        raise RuntimeError(f"o X recusou o conteudo (400): {e}")
    except tweepy.TwitterServerError as e:
        raise RuntimeError(f"a API do X esta fora do ar (5xx): {e}")
    except Exception as e:
        # rede, DNS, proxy, timeout — qualquer coisa fora do protocolo
        raise RuntimeError(f"nao consegui falar com a API do X: "
                           f"{type(e).__name__}: {e}")
    tweet_id = str((resp.data or {}).get("id", ""))
    return tweet_id, (f"https://x.com/i/web/status/{tweet_id}" if tweet_id else "")


# ─────────────────────────────────────────
#  CICLO: gera + publica
# ─────────────────────────────────────────

def executar_ciclo(origem="scheduler", forcar=False, tema=None):
    """Gera e publica um post. Idempotente por slot (horario do dia)."""
    cfg = carregar_config()
    ts = agora(cfg)

    if not cfg.get("ativo") and not forcar:
        log("autopostagem desligada — nada a fazer")
        return {"ok": False, "motivo": "desligado"}

    slot = slot_de(cfg, ts) if origem != "manual" else None
    if slot and slot_ja_publicado(slot) and not forcar:
        log(f"slot {slot} ja publicado — ignorando chamada duplicada")
        return {"ok": False, "motivo": "slot_ja_publicado", "slot": slot}

    limite = max(len(cfg.get("horarios") or []), 1)
    if origem != "manual" and not forcar and len(publicados_hoje(cfg)) >= limite:
        log(f"limite diario de {limite} post(s) ja alcancado")
        return {"ok": False, "motivo": "limite_diario", "limite": limite}

    if not forcar and not reservar_slot(slot):
        log(f"slot {slot} ja reservado por outro processo — ignorando")
        return {"ok": False, "motivo": "slot_reservado", "slot": slot}

    base = {"ts": ts.isoformat(), "slot": slot, "origem": origem}
    try:
        gerado = gerar_post(cfg, tema=tema)
    except Exception as e:
        log(f"erro ao gerar: {e}")
        liberar_slot(slot)
        return registrar({**base, "status": "erro_geracao", "erro": str(e)}) | {
            "ok": False, "motivo": "erro_geracao"}

    try:
        tweet_id, url = publicar(gerado["texto"], cfg)
    except Exception as e:
        log(f"erro ao publicar: {e}")
        liberar_slot(slot)
        return registrar({**base, **gerado, "status": "erro_publicacao",
                          "erro": str(e)}) | {"ok": False, "motivo": "erro_publicacao"}

    log(f"publicado: {url or tweet_id}")
    return registrar({**base, **gerado, "status": "publicado",
                      "tweet_id": tweet_id, "url": url}) | {
        "ok": True, "motivo": "publicado"}


# ─────────────────────────────────────────
#  SCHEDULER
# ─────────────────────────────────────────

def _job():
    try:
        executar_ciclo(origem="scheduler")
    except Exception as e:
        log(f"falha inesperada no job: {e}")


def iniciar_scheduler():
    global _scheduler
    if _scheduler is None:
        cfg = carregar_config()
        tz = cfg.get("timezone") or "America/Sao_Paulo"
        _scheduler = BackgroundScheduler(timezone=tz, daemon=True)
        _scheduler.start()
        log(f"scheduler iniciado ({tz})")
    recarregar_jobs()
    return _scheduler


def recarregar_jobs():
    """(Re)cria um job por horario configurado. Chamar apos salvar a config."""
    if _scheduler is None:
        return []
    cfg = carregar_config()
    for job in _scheduler.get_jobs():
        job.remove()
    if not cfg.get("ativo"):
        log("autopostagem desligada — nenhum horario agendado")
        return []
    criados = []
    for hhmm in cfg.get("horarios") or []:
        try:
            h, m = [int(x) for x in str(hhmm).split(":")]
            _scheduler.add_job(
                _job, CronTrigger(hour=h, minute=m,
                                  timezone=cfg.get("timezone") or "America/Sao_Paulo"),
                id=f"post-{hhmm}", replace_existing=True,
                misfire_grace_time=3600, coalesce=True, max_instances=1)
            criados.append(hhmm)
        except Exception as e:
            log(f"horario invalido '{hhmm}': {e}")
    log(f"horarios agendados: {', '.join(criados) or 'nenhum'}")
    return criados


def status():
    cfg = carregar_config()
    hist = carregar_historico()
    jobs = []
    if _scheduler:
        for j in _scheduler.get_jobs():
            prox = getattr(j, "next_run_time", None)
            jobs.append({"id": j.id, "proximo": prox.isoformat() if prox else None})
    pub = publicados(hist)
    return {
        "ativo": bool(cfg.get("ativo")),
        "scheduler_ligado": _scheduler is not None and _scheduler.running,
        "timezone": cfg.get("timezone"),
        "horarios": cfg.get("horarios"),
        "jobs": sorted(jobs, key=lambda x: x["proximo"] or ""),
        "agora": agora(cfg).isoformat(),
        "publicados_hoje": len(publicados_hoje(cfg, hist)),
        "total_publicados": len(pub),
        "total_erros": len([h for h in hist if str(h.get("status", "")).startswith("erro")]),
        "ultimo": (hist[-1] if hist else None),
        "pronto": config_publica()["pronto"],
    }
