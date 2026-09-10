# README_MUDANCAS.md — SAVL → CVR South America (CVR SA)

Resumo completo de tudo que foi feito nesta migração, decisões tomadas e
o que você (Meds) ainda precisa configurar manualmente antes de colocar
o `cvr-sa-bot` e o `cvr-sa-site` no ar.

---

## 1. O que foi pedido x o que foi entregue

Você pediu três mudanças específicas em cima do fork da SAVL:

1. **Tudo em inglês** — feito. Toda string visível ao usuário (embeds do
   bot, páginas do site, mensagens de erro) e comentários de código
   foram traduzidos. Fiz duas varreduras finais (com Python, não só
   regex de shell, que estava dando falso positivo/negativo por causa
   de encoding) confirmando zero texto em português restante nos dois
   repositórios.
2. **Rebranding SAVL → CVR South America / CVR SA** — feito em todo
   texto visível: título do site, embeds do Discord, nomes de função,
   e-mails de auditoria, footers, título de páginas, etc.
3. **Nova role "Court Captain"** — implementada com exatamente as
   mesmas permissões do Vice Captain, em **todos** os pontos do bot e
   do site (veja seção 4).

Além disso, você pediu para eu decidir a arquitetura de banco de dados
do bot (Supabase em vez de SQLite local) e a coexistência de Vice
Captain + Court Captain — ambas as decisões estão detalhadas abaixo.

---

## 2. Decisão de arquitetura: banco único, sem "ponte" de sincronização

**Antes (SAVL):** o bot mantinha um SQLite local (`savl.db`) como cópia
de trabalho dos dados de time/roster/matchmaking, e um conjunto de
funções (`services/supabase_bridge.py`, `services/mm_bridge.py`)
sincronizava essa cópia com o Supabase do site em ambas as direções.
Isso existia porque o SQLite era local ao processo do bot — o site não
conseguia enxergá-lo.

**Agora (CVR SA):** o bot conecta diretamente no mesmo Postgres do
Supabase que o site usa (via `asyncpg`, não a API REST). Como só existe
uma cópia dos dados agora, toda a lógica de sincronização foi
**removida** — não porque você pediu explicitamente essa remoção, mas
porque ela deixou de fazer sentido tecnicamente: não há mais duas
representações para manter alinhadas. Isso simplifica bastante o
código e elimina uma classe inteira de bugs (dados desalinhados entre
bot e site).

Consequência prática: `/team add`, `/team remove`, partidas de
matchmaking, etc., feitos pelo Discord aparecem **instantaneamente** no
site, e vice-versa — sem esperar nenhum job de sync.

### Por que `asyncpg` em vez de REST (PostgREST)?

O `cogs/matchmaking.py` original tem SQL bastante complexo (joins,
updates condicionais, contagem de picks do draft). Migrar isso para
chamadas REST do PostgREST significaria reescrever toda a lógica de
fila/draft/ELO do zero, com alto risco de introduzir bugs sutis. Com
`asyncpg`, a lógica SQL foi preservada quase 1:1 (só troquei o dialeto
SQLite→Postgres e os placeholders `?`→`$1,$2...`), o que é muito mais
seguro para um sistema com estado complexo como esse.

### Convenção de schema: `discord_id` como `TEXT`

Todo campo que guarda um snowflake do Discord (usuário, cargo, canal,
mensagem) é `TEXT`, nunca `INTEGER`/`BIGINT`. IDs do Discord têm 64
bits e o `Number` do JavaScript perde precisão acima de 2^53 — por
isso o cliente Supabase do site (e agora o bot também) sempre trata
esses valores como texto opaco. Essa já era a convenção do site; eu
apliquei a mesma convenção nas tabelas que antes só existiam no SQLite
do bot (as `mm_*`, de matchmaking).

### Unificação do agendamento de partidas oficiais

`cogs/schedule.py` e `cogs/match.py` tinham suas próprias tabelas
locais (`schedules`, `match_results`), completamente isoladas da tabela
`matches` que o site usa — ou seja, uma partida agendada pelo Discord
nunca aparecia no site, e vice-versa. Isso já era assim na SAVL. Agora,
como o bot está no mesmo Postgres, `/schedule match` e `/match result`
escrevem diretamente na tabela `matches` compartilhada. É um efeito
colateral positivo da migração, não pedido explicitamente — fico à
disposição se preferir reverter esse ponto especificamente.

**Importante:** `cogs/schedule.py` e `cogs/match.py` **não são
carregados por padrão** (não estão em `EXTENSIONS` no `bot.py`) — isso
já era assim no projeto original que você me mandou (os comandos
`/schedule` e `/match result` já estavam inativos). Mantive esse
comportamento. Se quiser ativá-los, adicione `"cogs.schedule"` e
`"cogs.match"` à lista `EXTENSIONS` em `bot.py`.

---

## 3. Segurança: credenciais nos ZIPs originais

Os dois ZIPs que você me mandou (`savl-bot-atualizado.zip` e
`savl-site-atualizado.zip`) continham arquivos `.env`/`.env.local` com
credenciais reais (token do bot Discord, chaves do Supabase, etc.). Eu
**não reproduzi nenhuma dessas credenciais** em nenhum momento desta
conversa, e os `.env`/`.env.local` originais foram removidos e
substituídos por `.env.example` (sem valores). Como você vai criar um
Supabase novo e (presumivelmente) uma aplicação de bot nova no Discord
para o CVR SA, essas credenciais antigas ficam obsoletas de qualquer
forma — mas se pretende reciclar o mesmo bot/app do Discord, recomendo
revogar e gerar um token novo antes de qualquer coisa.

---

## 4. Court Captain — todos os pontos implementados

Vice Captain e Court Captain têm **exatamente as mesmas permissões** e
**podem coexistir no mesmo time** (confirmado por você). Pontos
implementados:

### Bot (`cvr-sa-bot`)
- `config.py`: nova env var `COURT_CAPTAIN_ROLE_ID`
- `cogs/team.py`: `can_manage_team()`, `get_management_team()`,
  `extra_role_id_for()`, embeds (`/team info` agora mostra Vice
  Captains e Court Captains em campos separados), dropdowns de
  `/team add` e `/team staffadd`/`staffremove`

### Site (`cvr-sa-site`)
- `app/api/team-sync/route.ts`: tipo `TeamPlayerRole`, env vars
  `DISCORD_COURT_CAPTAIN_ROLE_ID`/`COURT_CAPTAIN_ROLE_ID`, helper
  `extraRoleIdFor()`, `canManageTeam()`, validação de `role` em
  `addPlayer`/`changeCaptain`
- `app/page.tsx` e `app/admin/page.tsx`: tipo `TeamPlayerRole`,
  ordenação de roster (`getPlayerRoleOrder`), cor do badge
  (`getRosterRoleBadgeClass` — usei violeta para Court Captain, azul
  para Vice Captain), dropdowns de formulário (`roleOptions`, o
  `<select>` inline de edição de roster no admin, e o campo "Old
  Captain New Role" da troca de capitão)
- `app/stats/page.tsx`, `app/archives/page.tsx`: tipo `TeamPlayerRole`
  e `rosterRoleRank()` (arquivo de temporadas passadas)
- `app/profile/page.tsx`: `isManagerProfile` agora reconhece Court
  Captain
- `app/api/profile-transactions/route.ts`: **bug encontrado e
  corrigido** — `isManager` só reconhecia `"Vice Captain"`, não
  apareceria numa busca por texto, só percebi revisando a lógica linha
  por linha

### Schema (`schema.sql`)
- `team_players.role` aceita `'Vice Captain' | 'Court Captain' | 'Player'`

---

## 5. Checklist de deploy (do zero)

### 5.1 Supabase
1. Crie um projeto novo no Supabase.
2. Rode `schema.sql` (raiz do `cvr-sa-bot`) uma vez, inteiro, no SQL
   Editor do projeto.
3. Anote a **Connection string → Session pooler** (Database Settings)
   — é isso que vai no `DATABASE_URL` do bot. Use o Session pooler (não
   a conexão direta) porque ele funciona sobre IPv4, que a maioria dos
   hosts de bot (incluindo o Discloud) exige — a conexão direta do
   Supabase é IPv6-only no plano gratuito.
4. Anote a URL do projeto + a chave pública (`anon`/`publishable`) e a
   **Service Role Key** (Settings → API) — vão no `.env.local` do site.

### 5.2 Discord
1. Crie uma aplicação/bot novo no Discord Developer Portal (ou reuse a
   antiga, mas gere um token novo — veja seção 3).
2. Crie os cargos no servidor: Captain, Vice Captain, **Court
   Captain**, Player, Referee, Streamer/Media, Match Organizer, VIP,
   VIP+, e os cargos de staff aprovadora que quiser.
3. Preencha os IDs desses cargos nos dois `.env` (bot e site — os
   nomes das variáveis batem entre os dois arquivos `.env.example`).
4. Configure OAuth do Discord no Supabase Auth (Authentication →
   Providers → Discord) para o login do site funcionar.

### 5.3 Bot (`cvr-sa-bot`)
1. `cp .env.example .env` e preencha tudo.
2. `pip install -r requirements.txt --break-system-packages` (ou num
   virtualenv).
3. Rode o bot. Ele conecta no Postgres via `DATABASE_URL` — não cria
   mais tabelas sozinho em runtime (isso já foi feito pelo
   `schema.sql`).
4. No Discloud: suba o projeto normalmente, cole as env vars no
   dashboard (o `discloud.config` já está atualizado para
   `cvr-sa-bot`).

### 5.4 Site (`cvr-sa-site`)
1. `cp .env.example .env.local` e preencha tudo.
2. `npm install`.
3. **Antes do primeiro deploy**, conceda a role `administrator` em
   `site_user_roles` para si mesmo (ou configure `DISCORD_OWNER_ID` no
   `.env.local`, que te dá acesso de Owner automaticamente ao logar
   com Discord — mais simples).
4. `npm run dev` para testar local, `npm run build` + deploy no Vercel
   quando estiver pronto. Cole as mesmas env vars no dashboard do
   Vercel.
5. Configure o webhook do Stripe (Developers → Webhooks) apontando
   para `<seu-domínio>/api/vip/webhook`, evento
   `checkout.session.completed`.

### 5.5 Pendências visuais/manuais (não consegui fazer sozinho)
- **Logo**: `public/savl-logo.png` e `public/savl-gfx.png` ainda
  contêm a arte antiga da SAVL — não tenho ferramenta de geração de
  imagem disponível para criar uma logo nova da CVR SA. Substitua o
  **conteúdo** desses dois arquivos por artes novas (mantendo os
  mesmos nomes), assim nenhum código precisa mudar. Se preferir nomes
  novos, me avise que eu atualizo as referências no código.
- **Link do Challonge**: eu troquei
  `https://challonge.com/pt_BR/communities/savl` pela homepage genérica
  `https://challonge.com` (para não deixar um link quebrado). Quando
  você criar a comunidade da CVR SA no Challonge, me avise ou atualize
  o `href` em `app/page.tsx` (busque por `challonge.com`).
- **README do bot**: o repo `cvr-sa-bot` não tinha um `README.md` no
  ZIP original, então não criei um do zero — só ajustei
  `discloud.config`/`.gitignore`. Posso escrever um se quiser.

---

## 6. Validação feita

- **Bot**: `python3 -m py_compile` em todos os arquivos `.py` — sem
  erros.
- **Site**: `npm install` + `npx tsc --noEmit` — **zero erros de
  TypeScript**. Também rodei `npm run build`; o único erro foi a busca
  da fonte Geist no Google Fonts, que falhou por falta de acesso à
  internet **neste ambiente de sandbox** (não é um bug de código — vai
  funcionar normalmente no Vercel, que tem acesso irrestrito à
  internet).
- Duas varreduras finais (em Python, não regex de shell) confirmando
  zero texto em português e zero menção a "SAVL"/"South America
  Volleyball League" visível ao usuário nos dois repositórios (fora os
  dois arquivos de imagem já citados).

---

## 7. Arquivos novos/removidos (bot)

**Novos:**
`database.py` (reescrito do zero, agora asyncpg), `services/profiles.py`,
`services/vip_data.py`, `.env.example`

**Removidos:**
`services/supabase_bridge.py`, `services/mm_bridge.py` (lógica
absorvida por `services/profiles.py`, `services/vip_data.py`, e
consultas diretas nos cogs)

**Reescritos por completo:**
`config.py`, `bot.py`, `cogs/team.py`, `cogs/matchmaking.py`,
`cogs/vip.py`, `cogs/scrim.py`, `cogs/schedule.py`, `cogs/match.py`

**Sem mudanças:** `utils/roblox.py` (já estava 100% em inglês, sem
dependência de banco)

---

## 8. Atualização (Setembro/2026) — Posições reais do CVR + VIPs

Mudanças feitas só em `cogs/matchmaking.py`. Nenhum outro arquivo foi
tocado nesta rodada, e nenhuma migration de banco foi necessária —
`mm_match_players.role_pref` e `mm_matches.queue_channel_id` já
existiam e não têm `CHECK` constraint no `schema.sql`, então aceitam os
novos valores sem alterar o schema.

### 8.1 Posições: Setter/Spiker → as 4 posições reais

Antes: 2 posições genéricas (Setter, cap. 4 no total / Spiker, cap. 8
no total), herdadas do outro bot. Agora: as 4 posições do CVR, 6 por
time / 12 no total:

| Posição | Por time | Total na fila |
|---|---|---|
| Setter | 1 | 2 |
| Outside Hitter | 2 | 4 |
| Middle Blocker | 2 | 4 |
| Opposite Hitter | 1 | 2 |

Pontos alterados: os 2 botões de entrar na fila viraram 4 (+ o botão
Leave, que continua igual), todos os embeds (fila, cancelada,
capitães, draft, partida em andamento, resultado), a ordenação SQL, e
os menus de seleção de capitão/MVP.

### 8.2 VIP — fila com ELO em dobro

- Canal da fila VIP: `1547095549264666654` (constante
  `VIP_QUEUE_CHANNEL_ID`). Você confirmou que o acesso a esse canal já
  é restrito por permissão de cargo no Discord, então o bot **não**
  bloqueia ninguém na entrada — só detecta que aquela partida foi
  aberta ali (`mm_matches.queue_channel_id`, que já era salvo) e
  aplica o dobro de ELO em quem ganha, no `finalize_match`.
- Você confirmou que pode empilhar com tudo: dobro da fila VIP ×
  multiplicador da Golden Match (até x3) × bônus % de VIP/VIP+ por
  jogador. Isso pode gerar ganhos de ELO bem altos em vitórias raras
  (fila VIP + Golden Match + VIP+ jogando), de propósito.
- O embed da fila, da partida em andamento e do resultado agora
  mostram "VIP Queue (2x ELO on wins)" quando aplicável.
- `/mm leaderboard` agora mostra ⭐ VIP / 👑 VIP+ do lado de quem tem
  assinatura ativa (busca em lote em `vip_subscriptions`, filtrando
  `expires_at > now()` pra não mostrar quem já venceu mas o job de
  expiração do `cogs/vip.py` ainda não rodou).
- `/mm vip` foi reescrito com a lista de benefícios exata que você
  passou.

### 8.3 VIP+ — prioridade pra furar fila lotada

Você confirmou: um VIP+ pode entrar numa posição já no limite, desde
que ainda sobre vaga no total de 12 — nunca passa disso. Implementado
na entrada da fila (`JoinQueueView._join_role`).

**Detalhe que não estava no seu pedido, mas era necessário**: se isso
deixar uma posição com mais gente do que o normal (ex.: 3 Setters em
vez de 2), o limite por time durante o **draft** não pode continuar
fixo em "1 por time" — senão o 3º Setter nunca seria escolhido por
nenhum time e o draft travaria pra sempre (o `remaining` do draft
nunca ficaria vazio). Resolvi calculando esse limite dinamicamente:
metade do total daquela posição na fila, arredondado pra cima. No caso
normal (2 Setters) isso dá exatamente 1 por time, igual antes. No caso
de VIP+ furando fila (3 Setters), dá 2 pra um time e 1 pro outro — todo
mundo sempre acaba sendo escolhido.

### 8.4 O que ficou fora / decisões que fiz sozinho

- **Não removi** o sistema antigo de "prioridade pra ser sorteado
  capitão" (peso de VIP/VIP+ no sorteio de capitães, em
  `services/vip_data.py` / `CaptainPickSelect`) — ele não estava na
  sua lista de benefícios nova, mas também não pareceu que você queria
  removê-lo, e ele já funcionava. Continua ativo, só não está mais
  anunciado no `/mm vip` (que agora só lista os benefícios que você
  descreveu). Me avisa se quiser remover de vez.

### 8.5 Validação
- `python3 -m py_compile cogs/matchmaking.py` — sem erros.
- Importei o módulo de verdade num ambiente com `discord.py`/`asyncpg`
  instalados (não só `py_compile`, que só pega erro de sintaxe) — sem
  `NameError` nem erro de import.
- Instanciei o `JoinQueueView` e confirmei os 5 botões (4 posições +
  Leave) registrando com os labels e caps corretos.
- Busquei em todo o projeto (todos os `.py`, não só o matchmaking) por
  qualquer referência solta a "setter"/"spiker" ou aos limites antigos
  — não sobrou nenhuma.

