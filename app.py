"""
Twitter Auto-Comment Bot — Deploy Render.com
=============================================
Versao para servidor na nuvem.
Login automatico no Twitter com usuario e senha.
Acesse pelo celular de qualquer lugar.
"""

from flask import Flask, render_template, request, jsonify, Response
import asyncio, random, threading, queue, json, os, re
import anthropic
from datetime import datetime, timezone
from playwright.async_api import async_playwright

app = Flask(__name__)

# ── Arquivos de dados (usando /tmp no Render pois o filesystem e efemero)
import pathlib
BASE_DIR       = pathlib.Path(__file__).parent
CONFIG_FILE    = "/tmp/config.json"
HISTORICO_FILE = "/tmp/historico_comentados.json"
STATS_FILE     = "/tmp/stats_comentarios.json"
COOKIES_FILE   = "/tmp/twitter_cookies.json"

log_queue   = queue.Queue()
bot_running = False

DEFAULT_CONFIG = {
    "twitter_user": "",
    "twitter_pass": "",
    "target_users": ["ZattarRafael", "fernandoulrich"],
    "posts_per_user": 3,
    "delay_min": 5,
    "delay_max": 12,
    "prompt_ativo": "direto",
    "prompt_biblico": "",
    "prompt_direto": "",
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
    "ai_prompt": ""
}

PROMPT_BIBLICO = """Voce e um homem de 36 anos, brasileiro, cristao de verdade. Fala como alguem que viveu a Biblia, nao como quem a decorou. Tem o peso de quem ja errou, ja sofreu e encontrou algo real em Deus.

Gere UM comentario para o post de @{autor}:
---
{post_text}
---

REGRAS:
- MAXIMO 240 caracteres
- Inclua UMA passagem biblica usada de um jeito que surpreenda
- ZERO frases de culto: nada de "Deus abencoe", "gloria", "amem irmao"
- Fale como brasileiro de 36 anos: natural, direto, sem firula
- Nunca mencione que e IA
- Responda APENAS com o texto do comentario, sem aspas"""

PROMPT_DIRETO = """Voce e um homem de 36 anos, brasileiro, cristao — nao o tipo que fica gritando aleluia, mas o tipo que quando fala sobre fe, todo mundo para pra ouvir. Pensa no Don Corleone — mas cheio do Espirito Santo. Calmo. Denso. Cada palavra tem peso.

Gere UM comentario para o post de @{autor}:
---
{post_text}
---

SEU JEITO:
- Frases curtas. Nunca apressado. Cada palavra pesa.
- Nunca bajula. Fala de igual pra igual.
- Fala como brasileiro de 36 anos: natural, sem sermao

NUNCA FAZER:
- NUNCA mais de 240 caracteres
- NUNCA "Deus abencoe", "gloria", "amem"
- NUNCA soar como post motivacional do Instagram
- NUNCA mencionar que e IA

FORMATO: 1 frase, no maximo 2. Emoji so se inevitavel — prefira zero.
Responda APENAS com o texto do comentario. Sem aspas."""


# ─────────────────────────────────────────
#  CONFIG / STATS / HISTORICO
# ─────────────────────────────────────────

def carregar_config():
    if os.path.exists(CONFIG_FILE):
        try:
            cfg = json.load(open(CONFIG_FILE, encoding="utf-8"))
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except: pass
    return DEFAULT_CONFIG.copy()

def salvar_config(cfg):
    json.dump(cfg, open(CONFIG_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

def carregar_historico():
    if os.path.exists(HISTORICO_FILE):
        try: return set(json.load(open(HISTORICO_FILE, encoding="utf-8")))
        except: pass
    return set()

def salvar_historico(ids):
    json.dump(list(ids), open(HISTORICO_FILE, "w", encoding="utf-8"), indent=2)

def carregar_stats():
    if os.path.exists(STATS_FILE):
        try: return json.load(open(STATS_FILE, encoding="utf-8"))
        except: pass
    return {"comentarios": []}

def salvar_stats(stats):
    json.dump(stats, open(STATS_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

def registrar_comentario(perfil, post_id):
    stats = carregar_stats()
    stats["comentarios"].append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "perfil": perfil,
        "post_id": post_id
    })
    salvar_stats(stats)

def calcular_dashboard():
    from collections import Counter
    import datetime as dt
    stats = carregar_stats()
    comentarios = stats.get("comentarios", [])
    now = datetime.now(timezone.utc)
    hoje = now.date().isoformat()
    semana_inicio = (now.date() - dt.timedelta(days=now.weekday())).isoformat()
    mes_inicio = now.date().replace(day=1).isoformat()

    total = len(comentarios)
    hoje_count = semana_count = mes_count = 0
    por_perfil = Counter()
    por_dia = {}

    for c in comentarios:
        try:
            ts = datetime.fromisoformat(c["ts"].replace("Z", "+00:00"))
            dia = ts.date().isoformat()
            perfil = c.get("perfil", "?")
            por_perfil[perfil] += 1
            por_dia[dia] = por_dia.get(dia, 0) + 1
            if dia == hoje: hoje_count += 1
            if dia >= semana_inicio: semana_count += 1
            if dia >= mes_inicio: mes_count += 1
        except: pass

    ultimos_30 = []
    for i in range(29, -1, -1):
        d = (now.date() - dt.timedelta(days=i)).isoformat()
        ultimos_30.append({"dia": d, "count": por_dia.get(d, 0)})

    return {
        "total": total,
        "hoje": hoje_count,
        "semana": semana_count,
        "mes": mes_count,
        "por_perfil": [{"perfil": p, "count": c} for p, c in por_perfil.most_common(10)],
        "ultimos_30": ultimos_30,
        "media_diaria_mes": round(mes_count / max(now.day, 1), 1),
        "alerta": hoje_count >= 20
    }


# ─────────────────────────────────────────
#  BOT
# ─────────────────────────────────────────

def log(msg):
    log_queue.put(msg)
    print(msg)

def gerar_comentario(post_text, autor, cfg):
    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    tipo = cfg.get("prompt_ativo", "direto")
    if tipo == "biblico":
        base = cfg.get("prompt_biblico") or PROMPT_BIBLICO
    elif tipo == "custom":
        base = cfg.get("ai_prompt") or PROMPT_DIRETO
    else:
        base = cfg.get("prompt_direto") or PROMPT_DIRETO
    prompt = base.format(post_text=post_text[:800], autor=autor)
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text.strip()


async def fazer_login(page, username, password):
    """Faz login automatico no Twitter."""
    log("🔐 Iniciando login no Twitter...")
    await page.goto("https://x.com/login", wait_until="domcontentloaded")
    await asyncio.sleep(3)

    try:
        user_input = page.locator('input[autocomplete="username"]')
        await user_input.wait_for(timeout=10000)
        await user_input.fill(username)
        await page.keyboard.press("Enter")
        await asyncio.sleep(2)

        # Verificacao extra (email/telefone)
        try:
            verify = page.locator('input[data-testid="ocfEnterTextTextInput"]')
            if await verify.is_visible(timeout=3000):
                log("🔍 Verificacao extra solicitada...")
                await verify.fill(username)
                await page.keyboard.press("Enter")
                await asyncio.sleep(2)
        except: pass

        pass_input = page.locator('input[name="password"]')
        await pass_input.wait_for(timeout=10000)
        await pass_input.fill(password)
        await page.keyboard.press("Enter")
        await asyncio.sleep(4)

        if "home" in page.url or "x.com" in page.url:
            log("✅ Login realizado!")
            cookies = await page.context.cookies()
            with open(COOKIES_FILE, "w") as f:
                json.dump(cookies, f)
            return True
        else:
            log("❌ Falha no login. Verifique usuario e senha.")
            return False
    except Exception as e:
        log(f"❌ Erro no login: {e}")
        return False


async def carregar_cookies(context):
    try:
        if os.path.exists(COOKIES_FILE):
            cookies = json.load(open(COOKIES_FILE))
            await context.add_cookies(cookies)
            log("🍪 Cookies carregados.")
            return True
    except: pass
    return False


async def eh_pinado(post):
    try:
        texto = await post.inner_text()
        for x in ["pinned", "post fixado", "fixado"]:
            if x in texto.lower(): return True
        if "pinned" in (await post.inner_html()).lower(): return True
    except: pass
    return False


async def obter_horas(post):
    try:
        dv = await post.locator('a[href*="/status/"] time').first.get_attribute("datetime", timeout=3000)
        if dv:
            return (datetime.now(timezone.utc) - datetime.fromisoformat(dv.replace("Z", "+00:00"))).total_seconds() / 3600
    except: pass
    try:
        for te in await post.locator("time[datetime]").all():
            dv = await te.get_attribute("datetime", timeout=2000)
            if dv and "T" in dv:
                diff = (datetime.now(timezone.utc) - datetime.fromisoformat(dv.replace("Z", "+00:00"))).total_seconds() / 3600
                if 0 <= diff < 8760: return diff
    except: pass
    return 9999.0


async def obter_id(post):
    try:
        href = await post.locator('a[href*="/status/"]').first.get_attribute("href", timeout=3000)
        if href:
            m = re.search(r'/status/(\d+)', href)
            if m: return m.group(1)
    except: pass
    return ""


async def comentar(page, post, autor, cfg):
    try:
        texto = (await post.inner_text())[:800]
        log(f"📄 {texto[:90]}...")
        log("🤖 Gerando comentario...")
        c = gerar_comentario(texto, autor, cfg)
        log(f"💬 {c}")
        await post.locator('[data-testid="reply"]').first.click()
        await asyncio.sleep(2)
        rb = page.locator('[data-testid="tweetTextarea_0"]').first
        await rb.click()
        await rb.type(c, delay=random.randint(40, 90))
        await asyncio.sleep(1.5)
        await page.locator('[data-testid="tweetButton"]').first.click()
        log("✅ Postado!")
        t = random.uniform(cfg["delay_min"], cfg["delay_max"])
        log(f"⏳ Aguardando {t:.1f}s...")
        await asyncio.sleep(t)
        return True
    except Exception as e:
        log(f"❌ {e}")
        try: await page.keyboard.press("Escape"); await asyncio.sleep(1)
        except: pass
        return False


async def processar(page, username, historico, cfg):
    log(f"\n{'='*40}\n👤 @{username}\n{'='*40}")
    await page.goto(f"https://x.com/{username}", wait_until="domcontentloaded")
    await asyncio.sleep(3)
    try: await page.wait_for_selector('[data-testid="tweet"]', timeout=10000)
    except: log(f"⚠️ Sem posts para @{username}"); return 0

    comentados = 0; i = 0; recente = False
    while comentados < cfg["posts_per_user"] and i < 25:
        posts = await page.locator('[data-testid="tweet"]').all()
        if i >= len(posts):
            await page.evaluate("window.scrollBy(0,600)"); await asyncio.sleep(2); i += 1; continue
        post = posts[i]; i += 1
        if await eh_pinado(post): log("📌 Pinado — pulando..."); continue
        try:
            at = await post.locator('[data-testid="User-Name"]').first.inner_text()
            if username.lower() not in at.lower(): log("⏭️ Retweet — pulando..."); continue
        except: pass
        h = await obter_horas(post)
        log(f"🕐 {h:.1f}h atras")
        if h > 24:
            if recente: log("🛑 Post +24h — parando."); break
            else: log("⏭️ Antigo — pulando..."); continue
        recente = True
        pid = await obter_id(post)
        if pid and pid in historico: log(f"🔁 Ja comentado — pulando..."); continue
        ok = await comentar(page, post, username, cfg)
        if ok and pid:
            historico.add(pid)
            salvar_historico(historico)
            registrar_comentario(username, pid)
            comentados += 1
    log(f"📊 {comentados} comentario(s) para @{username}")
    return comentados


async def rodar_bot(cfg):
    global bot_running
    bot_running = True
    historico = carregar_historico()
    log(f"📚 Historico: {len(historico)} ja comentado(s)")

    try:
        async with async_playwright() as p:
            import os
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/render/project/src/.playwright"
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage", "--disable-gpu",
                      "--single-process"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
            page = await context.new_page()

            # Tenta cookies salvos primeiro
            cookies_ok = await carregar_cookies(context)
            if cookies_ok:
                await page.goto("https://x.com/home", wait_until="domcontentloaded")
                await asyncio.sleep(3)
                if "login" in page.url:
                    log("⚠️ Cookies expirados, fazendo login...")
                    cookies_ok = False

            if not cookies_ok:
                ok = await fazer_login(page, cfg["twitter_user"], cfg["twitter_pass"])
                if not ok:
                    log("❌ Nao foi possivel fazer login. Verifique usuario e senha.")
                    return

            total = 0
            for user in cfg["target_users"]:
                user = user.strip().lstrip("@")
                if user:
                    total += await processar(page, user, historico, cfg)
                    await asyncio.sleep(random.uniform(8, 15))

            log(f"\n🎉 Finalizado! {total} comentario(s) no total.")
            await browser.close()

    except Exception as e:
        log(f"❌ Erro geral: {e}")
    finally:
        bot_running = False
        log("DONE")


def thread_bot(cfg): asyncio.run(rodar_bot(cfg))


# ─────────────────────────────────────────
#  ROTAS
# ─────────────────────────────────────────

@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/config", methods=["GET"])
def get_config():
    cfg = carregar_config()
    # Nao expoe a senha na API
    safe = cfg.copy()
    if safe.get("twitter_pass"): safe["twitter_pass"] = "••••••••"
    return jsonify(safe)

@app.route("/api/config", methods=["POST"])
def set_config():
    data = request.json
    cfg = carregar_config()
    # So atualiza a senha se vier diferente de pontos
    if data.get("twitter_pass") == "••••••••":
        data["twitter_pass"] = cfg.get("twitter_pass", "")
    cfg.update(data)
    salvar_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/historico/count")
def count_hist(): return jsonify({"count": len(carregar_historico())})

@app.route("/api/historico/limpar", methods=["POST"])
def limpar_hist():
    salvar_historico(set())
    salvar_stats({"comentarios": []})
    return jsonify({"ok": True})

@app.route("/api/dashboard")
def dashboard(): return jsonify(calcular_dashboard())

@app.route("/api/gerar-post", methods=["POST"])
def gerar_post_route():
    data = request.json
    cfg = carregar_config()
    try:
        client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": data.get("prompt", "")}]
        )
        return jsonify({"ok": True, "texto": msg.content[0].text.strip()})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500

@app.route("/api/rodar", methods=["POST"])
def rodar():
    global bot_running
    if bot_running: return jsonify({"error": "Bot ja esta rodando!"}), 400
    while not log_queue.empty(): log_queue.get()
    cfg = carregar_config()
    if not cfg.get("twitter_user") or not cfg.get("twitter_pass"):
        return jsonify({"error": "Configure usuario e senha do Twitter em Configuracoes!"}), 400
    threading.Thread(target=thread_bot, args=(cfg,), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/status")
def status(): return jsonify({"running": bot_running})

@app.route("/api/logs")
def logs():
    def stream():
        while True:
            try:
                msg = log_queue.get(timeout=60)
                yield f"data: {msg}\n\n"
                if msg == "DONE": break
            except queue.Empty: yield "data: ping\n\n"
    return Response(stream(), mimetype="text/event-stream")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n{'='*50}\n🚀 App rodando em http://localhost:{port}\n{'='*50}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
