# twitter-bot

Dois bots em um painel Flask:

1. **Auto Comment** (já existia) — comenta nos posts de perfis-alvo usando Playwright.
2. **Auto Posts** (novo) — cria e publica **posts próprios de finanças pessoais 3x por dia**,
   pela API oficial do X. É disso que trata este README.

---

## Como o Auto Posts funciona

```
APScheduler (3 horários/dia)
        │
        ├─ já publicou esse horário hoje? ──► sim: para aqui
        │
        ├─ sorteia um tema que não saiu nos últimos posts
        ├─ Claude escreve o texto
        ├─ valida: tamanho, hashtag, link, promessa de retorno, repetição
        │     └─ reprovado? devolve o motivo ao modelo e pede de novo (até 3x)
        └─ publica no X via tweepy  ──►  grava no histórico
```

Cada horário é um **slot** (`2026-11-22#08:10`). Um slot só publica uma vez, garantido
por um arquivo de trava atômico. Isso protege contra post duplicado quando o scheduler
interno e um cron externo disparam juntos.

### O que o texto nunca vai ter

Essas regras estão no prompt **e** são checadas no código depois, porque prompt sozinho
não é garantia:

- mais que o limite de caracteres configurado
- recomendação de ativo, ação, cripto, fundo ou corretora específica
- promessa de retorno (“x% ao mês”, “renda garantida”, “fique rico”)
- link ou hashtag (hashtag é opcional, desligada por padrão)
- qualquer menção a ser uma IA

Isso **não é** consultoria de investimento regulada — são posts de educação financeira.
Se você for falar de ativo específico ou dar recomendação, aí entra a alçada da CVM, e o
bot foi escrito justamente para não ir nesse caminho.

---

## Configuração

### 1. Chaves do X (Twitter)

Em [developer.x.com](https://developer.x.com) → Projects & Apps → seu app:

1. **User authentication settings** → App permissions: **Read and write**
   (sem isso o post volta 403, e trocar a permissão exige gerar o token de novo)
2. **Keys and tokens** → copie:
   - API Key e API Key Secret
   - Access Token e Access Token Secret

O plano **Free** dá **500 posts/mês** (17/dia). 3 por dia usa ~90/mês — sobra espaço.

### 2. Chave do Claude

[platform.claude.com](https://platform.claude.com) → API keys.

Custo: 3 posts/dia com Opus 5.5 ficam na casa de centavos de dólar por mês — o texto é
curto. Dá pra trocar por Sonnet 5.5 ou Haiku 5.5 no painel se quiser gastar menos.

### 3. Variáveis de ambiente

No Render: **Environment** → Add Environment Variable. Veja `.env.example`.

```
ANTHROPIC_API_KEY=sk-ant-...
X_API_KEY=
X_API_SECRET=
X_ACCESS_TOKEN=
X_ACCESS_TOKEN_SECRET=
CRON_TOKEN=            # opcional, veja "Se o app hibernar"
DATA_DIR=/tmp          # ou o mount path de um disco persistente
```

Você também pode digitar as chaves na aba **Auto Posts** do painel, mas o disco do Render
é apagado a cada deploy — então use variáveis de ambiente para valer.

### 4. Ligar

Abra o painel → **Auto Posts** → ajuste os horários → **Salvar** → ligue a chave.

Antes de ligar, use **Gerar sem publicar** umas vezes para ver se a voz dos posts te agrada.
Se não agradar, mexa nos temas ou no prompt (`PROMPT_FINANCAS` em `autoposter.py`).

---

## Deploy no Render

- **Build Command:** `./build.sh`
- **Start Command:** `gunicorn -w 1 --threads 4 --timeout 180 -b 0.0.0.0:$PORT app:app`

**`-w 1` importa.** Cada worker sobe o próprio APScheduler; com 2 workers o mesmo horário
dispararia duas vezes. A trava de slot segura isso, mas um worker só é mais simples.

### Se o app hibernar (plano Free)

No plano Free o Render derruba o serviço depois de 15 min sem acesso, e aí o scheduler
interno não roda. Duas saídas:

**a) Plano pago** ($7/mês) — o serviço fica sempre de pé e o scheduler resolve tudo.

**b) Cron externo gratuito** — defina `CRON_TOKEN` e aponte um serviço como
[cron-job.org](https://cron-job.org) ou UptimeRobot para:

```
POST https://SEU-APP.onrender.com/api/posts/cron?token=SEU_TOKEN
```

Agende nos mesmos horários. A chamada acorda o app e publica. A tolerância é de 90 min
em volta do horário, então pequenos atrasos não quebram nada, e o slot evita post duplo
se o scheduler interno também acordar.

### Estado persistente (opcional)

Em `/tmp` o histórico se perde a cada restart, e o bot pode voltar a repetir temas. Para
evitar: anexe um disco no Render e aponte `DATA_DIR` para o mount path dele.

---

## API

| Rota | O que faz |
|---|---|
| `GET /api/posts/config` | configuração atual (segredos só como `true/false`) |
| `POST /api/posts/config` | salva; campo de segredo em branco não apaga o que já existe |
| `GET /api/posts/status` | ligado?, horários, próximo disparo, contadores |
| `GET /api/posts/historico` | últimos 100 posts e erros |
| `POST /api/posts/historico/limpar` | zera o histórico |
| `POST /api/posts/preview` | gera o texto **sem publicar** |
| `POST /api/posts/publicar` | gera e publica agora; com `{"texto": "..."}` publica esse texto |
| `GET\|POST /api/posts/cron?token=` | gatilho externo (precisa de `CRON_TOKEN`) |

---

## Arquivos

| Arquivo | |
|---|---|
| `autoposter.py` | geração, validação, publicação, scheduler, histórico |
| `app.py` | Flask: painel, rotas dos dois bots, bot de comentários |
| `templates/index.html` | painel (aba **Auto Posts**) |
| `Procfile` | start command com `-w 1` |
| `.env.example` | variáveis de ambiente |

## Limites e cuidados

- **Volume:** 3 posts/dia é tranquilo. O gargalo é o plano Free do X (17/dia).
- **Texto duplicado:** o X devolve 403 se você repetir um post recente. O bot já compara
  com os posts anteriores antes de tentar.
- **Qualidade:** publica sem revisão. Olhe o histórico de vez em quando — o prompt tem
  guarda-corpo, mas quem assina os posts é você.
