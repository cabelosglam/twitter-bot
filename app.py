"""
Twitter Auto-Comment Bot — Deploy Render
=========================================
Roda em servidor na nuvem (Render.com), acesse pelo celular.
"""

from flask import Flask, render_template, request, jsonify, Response, session
import asyncio
import random
import threading
import queue
import os
import json
import anthropic
from playwright.async_api import async_playwright

app = Flask(__name__)
app.secret_key = os.urandom(24)

# ─────────────────────────────────────────
#  CONFIGURACOES (via variáveis de ambiente no Render)
# ─────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
COMMENT_LANGUAGE  = "português brasileiro"
COOKIES_FILE      = "twitter_cookies.json"

log_queue  = queue.Queue()
bot_running = False


# ─────────────────────────────────────────
#  IA
# ─────────────────────────────────────────
def gerar_comentario(post_text: str, autor: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""Você é um usuário real do Twitter interagindo com um post.
Gere UM comentário em {COMMENT_LANGUAGE} para o seguinte post de @{autor}:

---
{post_text}
---

Regras:
- Seja natural, como uma pessoa real responderia
- Entre 1 e 3 frases curtas
- Pode concordar, perguntar algo relevante ou adicionar um ponto interessante
- NÃO use hashtags, emojis excessivos nem linguagem robótica
- NÃO mencione que é IA
- Responda APENAS com o texto do comentário, sem aspas ou explicações"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text.strip()


# ─────────────────────────────────────────
#  BOT
# ─────────────────────────────────────────
def log(msg):
    log_queue.put(msg)
    print(msg)


async def fazer_login(page, username: str, password: str) -> bool:
    try:
        log("🔐 Abrindo página de login...")
        await page.goto("https://x.com/login", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        log("✍️ Digitando usuário...")
        user_input = page.locator('input[autocomplete="username"]')
        await user_input.wait_for(timeout=10000)
        await user_input.fill(username)
        await page.keyboard.press("Enter")
        await asyncio.sleep(2)

        # Verificação de identidade (às vezes pede email/telefone)
        try:
            verify = page.locator('input[data-testid="ocfEnterTextTextInput"]')
            if await verify.is_visible(timeout=3000):
                log("🔍 Verificação extra solicitada — digitando usuário...")
                await verify.fill(username)
                await page.keyboard.press("Enter")
                await asyncio.sleep(2)
        except:
            pass

        log("🔑 Digitando senha...")
        pass_input = page.locator('input[name="password"]')
        await pass_input.wait_for(timeout=10000)
        await pass_input.fill(password)
        await page.keyboard.press("Enter")
        await asyncio.sleep(4)

        # Verifica se logou
        if "home" in page.url or "x.com" in page.url:
            log("✅ Login realizado com sucesso!")
            # Salva cookies para reutilizar
            cookies = await page.context.cookies()
            with open(COOKIES_FILE, "w") as f:
                json.dump(cookies, f)
            return True
        else:
            log("❌ Falha no login. Verifique usuário e senha.")
            return False

    except Exception as e:
        log(f"❌ Erro no login: {e}")
        return False


async def carregar_cookies(context) -> bool:
    """Tenta carregar cookies de sessão anteriores."""
    try:
        if os.path.exists(COOKIES_FILE):
            with open(COOKIES_FILE, "r") as f:
                cookies = json.load(f)
            await context.add_cookies(cookies)
            log("🍪 Cookies de sessão carregados.")
            return True
    except:
        pass
    return False


async def comentar_post(page, post_locator, autor: str):
    try:
        texto_post = await post_locator.inner_text()
        texto_post = texto_post[:800]
        log(f"📄 Post: {texto_post[:100]}...")

        log("🤖 Gerando comentário com IA...")
        comentario = gerar_comentario(texto_post, autor)
        log(f"💬 Comentário: {comentario}")

        reply_btn = post_locator.locator('[data-testid="reply"]').first
        await reply_btn.click()
        await asyncio.sleep(2)

        reply_box = page.locator('[data-testid="tweetTextarea_0"]').first
        await reply_box.click()
        await reply_box.type(comentario, delay=random.randint(40, 90))
        await asyncio.sleep(1.5)

        send_btn = page.locator('[data-testid="tweetButton"]').first
        await send_btn.click()
        log("✅ Comentário postado!")

        t = random.uniform(5, 12)
        log(f"⏳ Aguardando {t:.1f}s...")
        await asyncio.sleep(t)
        return True

    except Exception as e:
        log(f"❌ Erro ao comentar: {e}")
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(1)
        except:
            pass
        return False


async def processar_usuario(page, username: str, posts_per_user: int):
    url = f"https://x.com/{username}"
    log(f"\n{'='*40}")
    log(f"👤 Visitando @{username}")
    log(f"{'='*40}")

    await page.goto(url, wait_until="domcontentloaded")
    await asyncio.sleep(3)

    try:
        await page.wait_for_selector('[data-testid="tweet"]', timeout=10000)
    except:
        log(f"⚠️ Nenhum post encontrado para @{username}")
        return 0

    posts_comentados = 0
    tentativas = 0
    max_tentativas = 15

    while posts_comentados < posts_per_user and tentativas < max_tentativas:
        posts = await page.locator('[data-testid="tweet"]').all()

        if tentativas >= len(posts):
            await page.evaluate("window.scrollBy(0, 600)")
            await asyncio.sleep(2)
            tentativas += 1
            continue

        post = posts[tentativas]
        tentativas += 1

        try:
            autor_element = post.locator('[data-testid="User-Name"]').first
            autor_texto = await autor_element.inner_text()
            if username.lower() not in autor_texto.lower():
                log("⏭️ Pulando retweet...")
                continue
        except:
            pass

        ok = await comentar_post(page, post, username)
        if ok:
            posts_comentados += 1

    log(f"📊 {posts_comentados} comentário(s) para @{username}")
    return posts_comentados


async def rodar_bot(tw_user, tw_pass, usuarios, posts_per_user):
    global bot_running
    bot_running = True
    total = 0

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage", "--disable-gpu"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
            page = await context.new_page()

            # Tenta cookies primeiro, depois faz login
            cookies_ok = await carregar_cookies(context)
            if cookies_ok:
                await page.goto("https://x.com/home", wait_until="domcontentloaded")
                await asyncio.sleep(3)
                if "login" in page.url:
                    log("⚠️ Cookies expirados, fazendo login...")
                    cookies_ok = False

            if not cookies_ok:
                ok = await fazer_login(page, tw_user, tw_pass)
                if not ok:
                    return

            for username in usuarios:
                username = username.strip().lstrip("@")
                if username:
                    n = await processar_usuario(page, username, posts_per_user)
                    total += n
                    if len(usuarios) > 1:
                        await asyncio.sleep(random.uniform(8, 15))

            log(f"\n🎉 Finalizado! Total: {total} comentário(s) postado(s).")
            await browser.close()

    except Exception as e:
        log(f"❌ Erro: {e}")
    finally:
        bot_running = False
        log("DONE")


def rodar_em_thread(tw_user, tw_pass, usuarios, posts_per_user):
    asyncio.run(rodar_bot(tw_user, tw_pass, usuarios, posts_per_user))


# ─────────────────────────────────────────
#  ROTAS FLASK
# ─────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/rodar", methods=["POST"])
def rodar():
    global bot_running
    if bot_running:
        return jsonify({"error": "Bot já está rodando!"}), 400

    data = request.json
    tw_user  = data.get("tw_user", "").strip()
    tw_pass  = data.get("tw_pass", "").strip()
    usuarios = [u.strip() for u in data.get("usuarios", "").split("\n") if u.strip()]
    posts_per_user = int(data.get("posts_per_user", 3))

    if not tw_user or not tw_pass:
        return jsonify({"error": "Informe usuário e senha do Twitter."}), 400
    if not usuarios:
        return jsonify({"error": "Informe pelo menos um perfil."}), 400

    while not log_queue.empty():
        log_queue.get()

    thread = threading.Thread(
        target=rodar_em_thread,
        args=(tw_user, tw_pass, usuarios, posts_per_user)
    )
    thread.daemon = True
    thread.start()

    return jsonify({"ok": True})


@app.route("/logs")
def logs():
    def stream():
        while True:
            try:
                msg = log_queue.get(timeout=60)
                yield f"data: {msg}\n\n"
                if msg == "DONE":
                    break
            except queue.Empty:
                yield "data: ping\n\n"
    return Response(stream(), mimetype="text/event-stream")


@app.route("/status")
def status():
    return jsonify({"running": bot_running})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
