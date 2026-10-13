# Especificação Técnica

## Sistema de Digital Signage — Dashboard e Players Remotos

**Versão 1.2 — Outubro 2026**

---

## Alterações face à versão 1.1

| Área | Alteração |
|---|---|
| Limpeza do ecrã | Novo estado explícito por player (`cleared`), definido pela ação "Clear screen" no dashboard. O manifest passa a ter `assigned` e `cleared`. Um manifest com `items` vazio só é aceite quando traz `cleared: true`; uma lista vazia ou um player sem atribuição nunca limpam o ecrã por si (secções 3.3, 4.4, 5.3 e 9). |

---

## Alterações face à versão 1.0

| Área | Alteração |
|---|---|
| Estado dos players | MQTT removido. Passa a **heartbeat HTTPS** (`POST /api/v1/heartbeat`) ao próprio dashboard. |
| Transferência de media | rsync/SSH substituído por **manifest + download HTTPS com verificação sha256**. Um só protocolo e uma só autenticação. |
| Atualização do código | Nova secção: **atualização remota assinada, com rollback automático** e canais canary/stable. |
| Modelo de dados | Introduzidas **playlists independentes** atribuídas a cada player (antes existia uma playlist global). |
| Robustez | Tratamento de relógio errado, expiração de itens por data, playlist vazia, vídeos incompatíveis, mpv bloqueado, cortes de energia. |
| Operação | Alertas, backups, provisionamento/revogação de players e acesso de emergência. |

---

## Índice

1. [Visão Geral](#1-visão-geral)
2. [Arquitetura](#2-arquitetura)
3. [Dashboard (VPS)](#3-dashboard-vps)
4. [Player (Raspberry Pi 5)](#4-player-raspberry-pi-5)
5. [API e Protocolo](#5-api-e-protocolo)
6. [Atualização Remota do Player](#6-atualização-remota-do-player)
7. [Segurança](#7-segurança)
8. [Operação](#8-operação)
9. [Cenários de Falha e Comportamento Esperado](#9-cenários-de-falha-e-comportamento-esperado)
10. [Entregáveis e Faseamento](#10-entregáveis-e-faseamento)
11. [Decisões e Pressupostos](#11-decisões-e-pressupostos)

---

## 1. Visão Geral

Sistema de digital signage para reprodução remota de imagens e vídeos. É composto por um **dashboard** num VPS (gestão de conteúdo e monitorização) e por um ou mais **players** Raspberry Pi 5 headless, em locais diferentes (ex.: "pombal", "coimbra").

### 1.1 Princípios de Design

- **Simplicidade:** sem CMS, sem frameworks de frontend com build step, sem serviços que não sejam estritamente necessários.
- **Autonomia do player:** o player toca sempre a última versão válida que tem em disco. Nunca depende da rede para reproduzir.
- **Pull, nunca push:** os players estão atrás de NAT e iniciam sempre as ligações. O VPS nunca se liga a eles.
- **Um só canal:** todo o tráfego player↔servidor (estado, comandos, playlists, media, releases) é HTTPS com o mesmo token.
- **Falhar de forma benigna:** qualquer falha (rede, VPS, ficheiro corrompido, update mau) deixa o ecrã a mostrar conteúdo válido e é reportada, nunca silenciada.
- **Multi-player desde o início**, cada player com a sua playlist.

### 1.2 Fora de Âmbito (v1.1)

- Multi-utilizador e permissões granulares (um administrador).
- Layouts multi-zona.
- Transcodificação automática (os ficheiros incompatíveis são rejeitados; transcodificação no VPS é uma evolução possível).
- Comandos em tempo real (latência aceite: 30–60 s).
- Agendamento de ecrã ligado/desligado (HDMI-CEC) — evolução futura.

---

## 2. Arquitetura

### 2.1 Diagrama Lógico

```
            DASHBOARD (VPS)                                   PLAYER (Raspberry Pi 5)
 +---------------------------------+                +------------------------------------+
 | Caddy (HTTPS, TLS automático)   |                |  signage-agent.service             |
 |   -> FastAPI (1 worker)         |   HTTPS 443    |    - heartbeat  (cada 30-60 s)     |
 |        SQLite (WAL)             | <------------- |    - sync de media (manifest)      |
 |        media/ (sha256.ext)      |   Bearer token |    - motor de playlist (datas)     |
 |        releases/ (assinadas)    |                |  mpv.service (DRM/KMS, IPC)        |
 |                                 |                |  signage-updater (timer + rollback)|
 +---------------------------------+                +------------------------------------+
          ^                                                           
          | browser (admin, login + CSRF)                            
```

### 2.2 Tecnologias

| Componente | Tecnologia | Função |
|---|---|---|
| Reverse proxy | Caddy | HTTPS automático, limites de upload, rate limit |
| Dashboard | FastAPI + SQLite (WAL) + Jinja2 | Gestão, API, monitorização |
| Player: agente | Python 3.11+ | Heartbeat, sync, motor de playlist |
| Player: reprodução | mpv (DRM/KMS) | Imagens e vídeos sem ambiente gráfico |
| Player: updater | Python/shell mínimo | Atualização, selftest, rollback |
| Orquestração | systemd (services + timers + watchdog) | Arranque e recuperação automáticos |
| Hora | chrony ou systemd-timesyncd | Relógio correto (o Pi 5 não tem RTC por omissão) |

### 2.3 Fluxo Normal

1. O admin faz upload de media, organiza uma playlist e atribui-a a um player.
2. O dashboard recalcula o **hash da playlist** desse player.
3. No heartbeat seguinte (≤ 60 s), o player vê que `playlist_hash` mudou, obtém o manifest, descarrega só os ficheiros em falta (verificados por sha256) e aplica a nova playlist sem cortar o item em reprodução.
4. O agente reporta o estado no heartbeat seguinte, e o dashboard mostra-o.

---

## 3. Dashboard (VPS)

### 3.1 Stack e Execução

- FastAPI com **um único worker uvicorn** (evita tarefas duplicadas), SQLite em **modo WAL**, templates Jinja2, HTML/CSS/JS vanilla.
- Serviço systemd com restart automático. Caddy à frente para TLS e limites.

### 3.2 Modelo de Dados (SQLite)

| Tabela | Campos principais |
|---|---|
| `media` | `id`, `sha256` (único), `stored_name`, `original_name`, `kind` (image/video), `size`, `duration_s`, `codec`, `width`, `height`, `created_at` |
| `playlists` | `id`, `name`, `created_at` |
| `playlist_items` | `id`, `playlist_id`, `media_id`, `position`, `start_date` (nulo = sem início), `end_date` (nulo = sem fim), `image_duration_s` (nulo = por omissão) |
| `players` | `id`, `label`, `location`, `playlist_id`, `cleared` (0/1), `channel` (canary/stable), `desired_version`, `token_hash`, `revoked`, `last_seen`, `last_state` (JSON), `created_at` |
| `player_commands` | `id`, `player_id`, `name`, `created_at`, `acked_at` |
| `alerts` | `id`, `player_id`, `kind`, `opened_at`, `resolved_at`, `notified_at` |

Notas: a ordem usa `position` (não `order`, que é palavra reservada em SQL). A reordenação é transacional. O mesmo `media` pode estar em várias playlists.

### 3.3 Funcionalidades

**Media**
- Upload com escrita **em streaming** para disco. O nome guardado é `sha256.ext` (elimina path traversal e duplicados).
- Validação por `ffprobe` (não pela extensão): tipo, codec, resolução, duração, bitrate. Ficheiros fora do perfil de compatibilidade (secção 4.5) são rejeitados com mensagem clara.
- Imagens acima de 4096 px são redimensionadas.
- Quota de disco: upload recusado abaixo de um limite mínimo de espaço livre.
- Apagar media só remove a referência; o ficheiro físico é removido quando já nenhuma playlist o usa, após período de graça (ex.: 7 dias).

**Playlists**
- CRUD de playlists. Itens com ordem (drag-and-drop ou campo numérico), `start_date`, `end_date` e duração de imagem.
- Semântica das datas: **inclusivas**, em **hora local do fuso configurado** (por omissão `Europe/Lisbon`); nulo significa sem limite.
- Cada playlist tem um `playlist_hash` (sha256 do manifest ordenado), recalculado em cada alteração.
- Gravar uma playlist **vazia** que esteja atribuída a players **não limpa os ecrãs** (o player recusa manifests vazios sem `cleared`). O dashboard avisa: *"This won't clear the screens. Use Clear screen if that's what you want."*

**Players**
- Lista de players: `id – label – estado – playlist atual – versão – último contacto`, p. ex.:
  ```
  player1 - pombal  - online  - lista "Loja"  - v1.4.2 - há 20 s
  player2 - coimbra - offline - lista "Hall"  - v1.4.2 - há 14 min
  ```
- Estado **online/offline calculado pelo servidor**: offline se `now - last_seen > 3 × intervalo` (por omissão 3 min). Mostra também: hash aplicado vs. desejado, último sync (OK/erro), espaço livre, temperatura, uptime, estado de update.
- Atribuir playlist, canal (canary/stable), versão desejada, e enviar comandos da lista fechada (secção 5.2).
- **Limpar o ecrã (ação explícita):** "Clear screen" por player, com confirmação, grava `players.cleared = 1`; o manifest desse player passa a ser `"cleared": true` com `"items": []` e o player mostra o fallback **de imediato, cortando o item em reprodução** (única exceção à regra de não interromper; ver secção 4.5). "Resume" ou atribuir uma playlist repõe `cleared = 0`. A limpeza nunca é deduzida de uma lista vazia.
- **Retirar a playlist** a um player pergunta *"Clear the screen too?"*: se sim, é uma limpeza explícita (como acima); se não, o player mantém a última lista que tem.
- A partir da fase 2, o estado `cleared` é reportado no heartbeat e mostrado no dashboard em vez de uma lista desatualizada.

**Releases**
- Página/CLI para carregar releases assinadas (secção 6) e definir a versão desejada por canal ou por player.

**Autenticação**
- Login de administrador único (secção 7).

### 3.4 Alertas

Notificação (webhook genérico: ntfy, Telegram ou email) quando:
- player offline há mais de X min (por omissão 10);
- sync a falhar repetidamente, update falhado/revertido;
- disco livre baixo, temperatura alta;
- desvio de relógio > 2 min entre player e servidor;
- mpv sem reproduzir (reportado no heartbeat).

Cada alerta tem abertura e resolução (notifica também a recuperação), com deduplicação.
**Vigilância do próprio dashboard:** ping periódico para um serviço externo (tipo healthchecks.io); se o VPS morrer, é esse serviço que avisa.

### 3.5 Backups

- Diário: `sqlite3 .backup` + `media/` + `releases/` para fora do VPS (restic para um Storage Box ou equivalente).
- O **restauro é testado** antes de ir para produção.

---

## 4. Player (Raspberry Pi 5)

### 4.1 Sistema e Hardware

- Raspberry Pi OS Lite 64-bit (Bookworm), headless.
- Fonte oficial 5 V / 5 A (fonte fraca causa instabilidade do SSD).
- Partição de dados separada para media e estado (`/var/lib/signage`); root em só-leitura (overlayfs) como objetivo recomendado, para sobreviver a cortes de energia.
- `journald` com tamanho limitado (`SystemMaxUse=100M`).
- Watchdog de hardware ativo (`RuntimeWatchdogSec` no systemd).

### 4.2 Estrutura de Diretórios

```
/opt/signage/
  releases/<versão>/        código + dependências empacotadas (wheels) + assets/fallback.png
  current -> releases/<v>   symlink atómico
  previous -> releases/<v>  versão anterior (rollback)
  updater/                  updater mínimo (muda raramente)
/etc/signage/
  config.toml               URL do servidor, id do player, intervalo
  token                     token do player (modo 600)
  release-pubkey.pub        chave pública de verificação de releases
/var/lib/signage/
  media/                    <sha256>.<ext>
  media/.staging/           downloads parciais
  manifest.json             último manifest válido
  state.json                estado local (último sync, update pendente, etc.)
```

### 4.3 Serviços systemd

| Serviço | Função |
|---|---|
| `mpv.service` | Reprodução contínua, `Restart=always` |
| `signage-agent.service` | Heartbeat, sync, motor de playlist; `WatchdogSec` com `sd_notify` |
| `signage-updater.service` + `.timer` | Verificação/aplicação de updates e rollback (secção 6) |

Três serviços em vez de quatro: heartbeat, sync e playlist vivem no mesmo agente. Os hooks são chamadas internas, não processos separados à espera de eventos (sem `inotifywait`).

### 4.4 Sincronização de Media

1. O heartbeat devolve `playlist_hash`. Se difere do aplicado, o agente faz `GET /api/v1/manifest`. Há também verificação completa periódica (ex.: cada 15 min).
2. O manifest indica `assigned` (o player tem playlist atribuída) e `cleared` (limpeza explícita), o fuso e a duração de imagem por omissão, e lista os itens: `sha256`, `size`, `url`, `kind`, `position`, `start_date`, `end_date`, `duration`. Regras de aceitação:
   - `cleared: true` (com `items: []`) é aceite: o player descarta a lista aplicada e mostra o fallback; a cache local mantém-se pelo período de graça normal, para um restauro rápido.
   - `items` vazio **sem** `cleared: true` é recusado (ver ponto 5).
   - `assigned: false` (ex.: player acabado de provisionar) não é uma limpeza: o player mantém o que tem (no primeiro arranque, o fallback).
   - Um 404, 5xx ou erro de rede **nunca** significa limpeza: o player mantém o que tem.
   - Uma playlist com itens mas nenhum válido hoje (filtro de datas) é um manifest válido: o player mostra o fallback (secção 4.5).
3. Para cada ficheiro em falta: download para `media/.staging/<sha256>.part` com `Range` (retoma), timeout e backoff; verificação de **sha256 e tamanho**; só então `rename` para `media/<sha256>.<ext>`.
4. O manifest só é promovido a `manifest.json` (e aplicado) **quando todos os ficheiros estão presentes e verificados**. Nunca há playlist a apontar para ficheiros em falta.
5. **Limpeza:** ficheiros que já não constam do manifest são removidos com período de graça. Um **manifest vazio sem `cleared: true` é recusado** (tratado como erro e alertado; nunca apaga nada) e há limite de remoção por operação.
6. Verificação de espaço livre antes de descarregar. Se faltar espaço, reporta erro e mantém a playlist anterior.

### 4.5 Motor de Playlist e Reprodução

**Avaliação de datas**
- O agente avalia o `manifest.json` local no fuso do manifest e filtra por `[start_date, end_date]` (inclusivas). Reavalia **a cada 5 min e à meia-noite local**, e só aplica se a lista resultante mudou. Itens expiram e entram sem depender de novos ficheiros.
- **Relógio inválido:** se a hora do sistema for anterior à data de build do software, ou o `server_time` do heartbeat divergir mais de 2 min, o agente mantém a última lista aplicada e reporta `clock_drift`. Corrige com chrony assim que houver rede.

**Reprodução (mpv)**
- DRM/KMS sem X11: `--vo=gpu --gpu-context=drm --drm-device=/dev/dri/by-path/platform-axi:gpu-card --swapchain-depth=1` (validado no Pi 5 com mpv 0.40.0: o ecrã está no dispositivo `vc4`, e sem `--swapchain-depth=1` as imagens aparecem com um item de atraso; o `--vo=drm` tem o mesmo problema), `--idle=yes`, `--force-window=yes`, `--loop-playlist=inf`, socket IPC `--input-ipc-server=/run/signage/mpv.sock`.
- **Aplicar uma nova lista não pode interromper o item em reprodução.** O `loadlist` em modo substituição reinicia o item; a implementação deve usar `playlist-clear` + `loadfile … append` (ou o agente controlar a sequência com `loadfile` no fim de cada item). O comportamento exato **tem de ser testado**. **Exceção:** uma limpeza explícita (`cleared: true`, ação "Clear screen" ou retirar a playlist com limpeza) corta o item atual e mostra logo o fallback, porque é uma ação deliberada do admin e um vídeo longo podia atrasá-la muitos minutos. Itens que deixam de ser válidos pelas datas continuam a esperar pelo fim do item atual.
- Duração de imagens: por item (`image_duration_s`) ou valor por omissão (ex.: 10 s).
- **Playlist vazia ou sem itens válidos hoje:** o mpv mostra a imagem de fallback incluída na release (`assets/fallback.png`). Nunca fica ecrã preto.

**Perfil de compatibilidade de vídeo** (validado no upload; a confirmar com testes no Pi 5 e no ecrã real)
- O Pi 5 não tem decodificação H.264 por hardware (tem HEVC). H.264 até 1080p30 em software costuma ser viável, 4K não.
- Perfil inicial: H.264 ou HEVC, até 1920×1080, até 30 fps, bitrate máximo configurável. Ajustar após testes.

### 4.6 Estado e Saúde

O agente consulta o mpv via IPC: ficheiro atual e se `time-pos` avança (para imagens, se a lista roda). Se o mpv estiver bloqueado (sem progresso > 2 min), o agente reinicia o `mpv.service` e reporta. O heartbeat inclui estes dados (secção 5.1), por isso "online" significa realmente "a funcionar". `WatchdogSec` e o watchdog de hardware reiniciam o sistema em caso de bloqueio total.

---

## 5. API e Protocolo

Todos os endpoints do player usam `Authorization: Bearer <token>` sobre HTTPS. O token é de 256 bits, guardado com hash no servidor e revogável.

### 5.1 Heartbeat

`POST /api/v1/heartbeat` — a cada 30–60 s (valor controlado pelo servidor).

Pedido:

```json
{
  "agent_version": "1.4.2",
  "player_time": "2026-10-13T13:40:00+01:00",
  "playlist_hash_applied": "ab12...",
  "playlist_hash_seen": "ab12...",
  "cleared": false,
  "clock_drift": false,
  "mpv": { "state": "playing", "current_file": "9f3c....mp4", "progressing": true, "restarts": 0 },
  "sync": { "last_ok": "2026-10-13T13:35:10+01:00", "last_error": null },
  "update": { "state": "idle", "last_error": null },
  "system": { "disk_free_mb": 182000, "temp_c": 51.2, "uptime_s": 86400 },
  "acked_commands": [41]
}
```

Resposta:

```json
{
  "server_time": "2026-10-13T13:40:01+01:00",
  "playlist_hash": "ab12...",
  "desired_version": "1.4.2",
  "commands": [ { "id": 42, "name": "sync-now" } ],
  "heartbeat_interval_s": 60
}
```

O pedido tem timeout curto. Em falha, o player faz **backoff exponencial com jitter** e continua a reproduzir a cache local.

Campos acrescentados na v1.2 (fase 2):
- `playlist_hash_applied`: hash do último manifest promovido (o que está em `manifest.json`).
- `playlist_hash_seen`: hash do último manifest tratado com sucesso (aplicado, limpeza ou sem atribuição). Difere do desejado quando o player recusa um manifest (ex.: vazio sem `cleared`), o que o dashboard mostra como "não aplicado".
- `cleared`: o player está em limpeza explícita (mostra o fallback por decisão do admin, secção 3.3).
- `clock_drift`: o relógio do player é inválido (secção 4.5); a lista aplicada está congelada.
- `mpv.state`: `playing`, `idle`, `stuck` (sem progresso, reiniciado pelo agente) ou `disconnected` (sem ligação IPC); `mpv.restarts`: reinícios do mpv feitos pelo agente desde o arranque.
- `desired_version` é `null` e `update.state` é `idle` até à fase 3.

O player só pede o manifest quando o `playlist_hash` da resposta difere do último tratado, ou na verificação completa periódica (secção 4.4).

### 5.2 Comandos (lista fechada)

`sync-now`, `restart-player`, `reboot`. Nunca texto arbitrário nem shell remoto. Cada comando tem `id` e só é considerado concluído quando o player o confirma em `acked_commands`.

- `sync-now`: verificação completa imediata do manifest e dos ficheiros.
- `restart-player`: reinicia o `mpv.service`; o agente reaplica a lista ao reconectar.
- `reboot`: reinicia o sistema.
- **Expiração:** um comando que não foi entregue a um heartbeat nos 15 min seguintes à criação passa a `expired` e nunca chega ao player (evita, p. ex., um reboot dias depois de o player voltar a estar online).
- **Sem repetição:** o player grava o `id` antes de executar e confirma-o no heartbeat seguinte; um `id` já executado nunca volta a ser executado (um `reboot` não entra em ciclo).
- O agente corre sem root: `restart-player` e `reboot` passam por uma regra polkit limitada ao utilizador `signage`.

### 5.3 Restantes Endpoints (player)

| Método e caminho | Função |
|---|---|
| `GET /api/v1/manifest` | Manifest da playlist atribuída a este player. Sempre 200 para um token válido, com `assigned` e `cleared` (secção 4.4). `ETag` = `playlist_hash`; com `If-None-Match` igual responde 304 |
| `GET /api/v1/media/<sha256>` | Download de ficheiro (suporta `Range`) |
| `GET /api/v1/releases/<versão>` | Tarball da release e respetiva assinatura |

---

## 6. Atualização Remota do Player

### 6.1 Princípios

- **Pull:** o player decide atualizar quando o heartbeat indica `desired_version` diferente da atual.
- **Releases imutáveis e versionadas** em `/opt/signage/releases/<versão>/`, com dependências empacotadas (sem `pip` online). Configuração e dados ficam fora da release.
- **Assinatura obrigatória** (minisign, decidido na fase 3): a chave privada fica **fora do VPS** (na máquina de desenvolvimento). O player só instala releases verificadas com a chave pública que tem. Assim, um VPS comprometido não consegue distribuir código para os players.
- **Updater mínimo e estável**, separado do agente: não é atualizado pelo mesmo mecanismo (atualização rara e deliberada).

### 6.2 Fluxo

1. Descarrega tarball e assinatura; **verifica assinatura** (e sha256).
2. Extrai para `releases/<v>.tmp`, corre `selftest` (importa módulos, valida config, testa acesso ao mpv/IPC), renomeia para `releases/<v>`.
3. Marca `previous` = versão atual, troca o symlink `current` de forma atómica e reinicia agente e mpv.
4. Escreve um marcador de **update pendente com prazo (≈3 min)**.
5. **Commit:** o agente, após arrancar, só confirma quando (a) o heartbeat foi aceite pelo servidor com a nova versão e (b) o mpv está a reproduzir (ou a mostrar o fallback numa playlist vazia). Remove o marcador.
6. **Rollback automático:** se o prazo expirar sem commit, o updater (que corre por timer, independente do agente) repõe `previous`, reinicia e reporta `update_failed`.
7. Após falha, o servidor marca essa versão como má **para esse player** e não repete a tentativa até a versão desejada mudar (evita ciclos de update-rollback).

### 6.3 Canais e Retenção

- **canary / stable** por player: o player de teste atualiza primeiro; a promoção é feita no dashboard ao definir a versão desejada para o canal stable.
- São mantidas as últimas 3 releases no player.

### 6.5 Detalhes de implementação (fase 3)

- **Assinatura:** `minisign`. O *trusted comment* (também assinado) é `coral-player <versão>`; o updater recusa a release se a versão não coincidir com a pedida, o que impede servir uma release antiga com outro nome. A chave pública está em `/etc/signage/release-pubkey.pub`.
- **Separação de privilégios:** o agente corre sem root e só **pede** o update (`/var/lib/signage/update/request.json`) e o **confirma** (`commit.json`). O updater corre como root (timer de 1 min), só lê esses ficheiros (sem seguir symlinks, tamanho limitado, versão validada) e guarda o seu estado em `/var/lib/signage-updater/` (`pending.json`, `status.json`, `failed.json`), que o agente lê para o reportar.
- **Endpoints:** `GET /api/v1/releases/<versão>` (tarball, suporta `Range`) e `GET /api/v1/releases/<versão>/signature`.
- **Versão desejada efetiva** de um player: a sua própria (`players.desired_version`), se definida; senão a do canal. Se for igual a `players.failed_version`, o servidor devolve `null` (não volta a tentar até a versão desejada mudar).
- **Relatório no heartbeat:** `update: {"state": ..., "version": ..., "last_error": ...}` com `state` em `idle`, `downloading`, `installing`, `pending_commit`, `committed`, `rolled_back`, `rejected` (assinatura ou selftest) ou `failed`. Com `rolled_back` ou `rejected`, o servidor grava `failed_version`.
- **O updater não altera units systemd nem a regra polkit**; essas mudanças continuam manuais.

### 6.4 Acesso de Emergência

Para o dia em que nem o update nem o rollback funcionem: **WireGuard** (ou túnel SSH reverso) do Pi para o VPS, SSH só por chave e apenas acessível pela interface do túnel. Fica fora do caminho normal de operação.

---

## 7. Segurança

**Dashboard**
- HTTPS obrigatório (Caddy, TLS automático) e redirecionamento de HTTP.
- Password com hash **argon2id**; rate limit e atraso progressivo no login.
- Cookies `Secure`, `HttpOnly`, `SameSite=Strict`; proteção CSRF em todos os formulários.
- Cabeçalhos de segurança (CSP, `X-Content-Type-Options`, etc.).
- Limite de corpo de pedido ajustado ao upload (Caddy), escrita em streaming; validação de ficheiros por `ffprobe`; nomes gerados pelo servidor.

**Players**
- Token por player, 256 bits, só o hash no servidor; revogação imediata (`revoked=true`).
- O player só aceita o servidor configurado; validação de TLS normal.
- Releases verificadas por assinatura (secção 6); chave privada nunca no VPS.
- Comandos remotos apenas da lista fechada.

**Segredos**
- Nada hardcoded: configuração em `.env` / `config.toml` com permissões restritas, fora do controlo de versões.

---

## 8. Operação

### 8.1 Provisionamento e Revogação

- `signagectl player add --label pombal --playlist <id>` cria o player e gera o token (mostrado uma só vez).
- `install.sh` no Pi: instala dependências, updater, serviços systemd, chrony, chave pública de releases, `config.toml` e token. No fim, o Pi faz o primeiro heartbeat.
- `signagectl player revoke <id>` invalida o token. A substituição de um Pi é: revogar, criar novo, instalar.
- O script cria também a partição de dados e configura o watchdog.

### 8.2 Monitorização

Dashboard de estado (secção 3.3), alertas (secção 3.4) e vigilância externa do VPS. Logs locais no Pi via journald (limitado); o essencial viaja no heartbeat.

### 8.3 Manutenção

Atualização do SO dos Pis com `unattended-upgrades` apenas para segurança, ou janelas manuais; a atualização do código da aplicação segue a secção 6.

---

## 9. Cenários de Falha e Comportamento Esperado

Estes cenários devem ser testados antes de produção.

| Cenário | Comportamento esperado |
|---|---|
| Corte de energia | Reinicia sozinho; mpv e agente arrancam; toca a última playlist válida; sem corrupção de dados |
| Rede em baixo | Continua a tocar a cache; backoff no heartbeat; ao voltar, sincroniza |
| VPS em baixo | Igual ao anterior; alerta externo avisa que o dashboard caiu |
| Ficheiro corrompido/incompleto | Falha a verificação sha256; não é promovido; playlist anterior mantém-se; erro reportado |
| Manifest vazio (sem `cleared`) ou inválido | Recusado; nada é apagado; alerta |
| Limpeza explícita (`cleared: true`) | Aceite; corta o item atual e mostra logo o fallback; cache mantida pelo período de graça |
| Player sem playlist atribuída (`assigned: false`) | Mantém o que tem; no primeiro arranque mostra o fallback |
| Manifest responde 404 ou 5xx | Nunca limpa; mantém a lista atual; erro reportado |
| Playlist sem itens válidos hoje | Mostra o fallback; não fica ecrã preto |
| Item expira à meia-noite | Reavaliação periódica remove-o sem novo sync |
| Relógio errado no arranque | Mantém última lista; reporta `clock_drift`; corrige quando chrony sincronizar |
| Disco quase cheio | Sync recusa descarregar; mantém lista anterior; alerta |
| Vídeo incompatível carregado | Rejeitado no upload com mensagem clara |
| mpv bloqueado | Detetado por `time-pos` parado; agente reinicia o mpv; reporta |
| Comando pendente para player offline | Expira ao fim de 15 min sem entrega; nunca é executado mais tarde |
| Release com assinatura inválida | Rejeitada; nada instalado; reportado (`rejected`) |
| Release que falha o selftest | Não instalada; `current` intacto; reportado (`rejected`) |
| Release que não arranca | Rollback automático em ≈3 min; `update_failed`; versão marcada má |
| Token revogado | Heartbeat 401; player continua a tocar cache; deixa de sincronizar |
| Pi bloqueado por completo | Watchdog de hardware reinicia |

---

## 10. Entregáveis e Faseamento

### 10.1 Estrutura do Repositório (monorepo)

```
/dashboard     FastAPI, templates, migrações, testes
/player        agent, updater, assets/fallback.png, selftest
/tools         signagectl, script de release (assina e empacota)
/deploy        Caddyfile, units systemd (VPS e Pi), install.sh
/docs          instalação VPS, instalação Pi, runbook, README
```

### 10.2 Entregáveis

- Código funcional de dashboard, agente e updater, com testes dos cenários da secção 9 que sejam automatizáveis.
- Units systemd, Caddyfile e scripts de instalação/provisionamento.
- Documentação: instalação do VPS, instalação do Pi, geração/gestão das chaves de release, runbook de operação (o que fazer quando há um alerta).

### 10.3 Faseamento Sugerido

| Fase | Âmbito | Critério de aceitação |
|---|---|---|
| 1 | Dashboard (upload, playlists, manifest) + agente (sync, motor de playlist, mpv, fallback) | Um Pi reproduz a playlist atribuída e atualiza ao alterá-la |
| 2 | Heartbeat, estado real, offline por `last_seen`, comandos | Dashboard mostra estado fiável; `sync-now` funciona |
| 3 | Updater assinado, selftest, rollback, canais | Update bom aplica; update mau reverte sozinho |
| 4 | Alertas, backups, hardening, provisionamento, acesso de emergência | Cenários da secção 9 passam; restauro de backup testado |

#### Registo de aceitação

**Fase 1 — aceite em 13/10/2026**, no primeiro Pi 5 (player `p0`, Pombal), com mpv 0.40.0, agente 0.1.1 e o dashboard em produção (`coral.lesto.cc`). O teste foi feito no ecrã real com media de teste (imagens com legenda e um vídeo de 40 s com relógio):

| Verificação | Resultado |
|---|---|
| A playlist atribuída é descarregada, verificada e toca em loop | OK |
| Alterar a playlist durante um vídeo: o vídeo vai até ao fim e só depois entra a lista nova (critério da fase) | OK |
| Item com `end_date` passado sai da rotação | OK |
| "Clear screen" corta logo para o fallback; "Resume" repõe o conteúdo a partir da cache, sem downloads | OK |
| Playlist esvaziada: o dashboard avisa, o player recusa o manifest vazio, continua a tocar e reporta o erro com backoff | OK |
| Corte de energia com o manifest vazio no servidor: o player retoma sozinho a última lista | OK |
| Validação do mpv real (`player/tests/test_mpv_real.py`) no Pi | 5/5 OK |
| Item com `end_date` = dia do teste sai à meia-noite local sem pedido ao servidor | OK (journal: `Play list changed: 2 entries` às 00:00:01 de 14/10/2026, agente 0.2.0) |

Problemas encontrados no ecrã real e corrigidos durante a aceitação: o fallback aberto através do symlink `current` não era reconhecido (agente 0.1.1); um vídeo longo atrasava o "Clear screen", que passou a cortar o item atual (exceção à invariante 3, secções 3.3 e 4.5); e as imagens apareciam com um item de atraso no Pi 5, corrigido no `mpv.service` com o dispositivo `vc4` por caminho estável e `--swapchain-depth=1` (secção 4.5).

**Fase 2 — aceite em 13/10/2026**, no mesmo Pi 5 (`p0`), com agente 0.2.0, polkit 126 e o dashboard em produção:

| Verificação | Resultado |
|---|---|
| O dashboard mostra o estado real: online, versão, hash desejado/visto/aplicado, último sync e erro, disco, temperatura, uptime | OK |
| Um manifest recusado (playlist vazia) aparece como "not applied" com a mensagem de erro | OK |
| Regra polkit: o utilizador `signage` pode reiniciar o `mpv.service` e não pode pará-lo | OK |
| `restart-player` executado e confirmado (acked) | OK |
| Rede cortada 4 min: offline ~3 min depois do último heartbeat, online de novo ao voltar; o ecrã nunca parou | OK |
| `sync-now` repara um ficheiro apagado da cache sem mudança de hash (um heartbeat normal não o faria); acked | OK (critério da fase) |
| `reboot`: reinicia uma só vez e fica acked no primeiro heartbeat depois do arranque | OK |

Nota: com a playlist vazia no servidor, o `sync-now` não repara a cache, porque o manifest vazio é recusado e nada é alterado (invariante 2).

---

## 11. Decisões e Pressupostos

- **MQTT removido deste sistema.** O Mosquitto, se montado no VPS para outro projeto, fica independente do signage (sem ponto de falha partilhado).
- **Transferência de media por HTTPS** (manifest + sha256) em vez de rsync/SSH, para ter um só protocolo e autenticação e promoção atómica da playlist. Se se preferir manter rsync, a alternativa é aceitável mas obriga a staging e validação do manifest pelo agente.
- **Decodificação de vídeo:** perfil de compatibilidade a confirmar com testes no Pi 5 e no ecrã real.
- **Comportamento do mpv ao aplicar listas** (sem cortar o item atual): **validado na fase 1** (13/10/2026) com `playlist-clear` + `loadfile … append` (argumentos nomeados) e remoção da entrada antiga no `start-file` seguinte, no mpv 0.34.1 (WSL, `--vo=null`) e no mpv 0.40.0 do Pi 5 (`--vo=drm`). O teste é `player/tests/test_mpv_real.py`.
- **Fuso horário:** `Europe/Lisbon` por omissão, configurável.
- Existe experiência prévia com Raspberry Pi, Python e systemd.
- Prioridade explícita: simplicidade e robustez acima de funcionalidades adicionais.