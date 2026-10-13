# CORAL — Digital signage (Dashboard + Player Raspberry Pi)

## O que estamos a construir

Sistema de digital signage: um **dashboard** num VPS (gestão de media, playlists, agendamento por datas, monitorização) e **players** Raspberry Pi 5 ligados a uma TV por HDMI, que reproduzem em loop imagens e vídeos. Hoje há 1 player (piloto em Pombal); o desenho tem de escalar para **centenas**.

Hardware e SO do Pi já estão prontos (Pi 5 4GB, arranque pelo SSD NVMe, Raspberry Pi OS 64-bit instalado). **O trabalho agora é só software.** Orçamento, compras e contexto comercial estão no Notion e não interessam ao código.

## Documento de referência

A especificação completa está em `docs/especificacao-tecnica.md` (v1.1). É a fonte de verdade para modelo de dados, API, fluxos de sync/update, cenários de falha e faseamento. **Lê a secção relevante antes de implementar** (não a carregues toda de cada vez). Se o código e a spec divergirem, pára e pergunta; não decidas sozinho. Este ficheiro resume só o que tens de ter sempre presente.

## Arquitetura (decisões fixas)

- **Dashboard:** FastAPI (1 worker uvicorn) + SQLite em modo WAL + Jinja2 + HTML/CSS/JS vanilla (sem build step, sem frameworks de frontend). Caddy à frente para HTTPS.
- **Player:** um agente Python (`signage-agent`) + `mpv` nativo (DRM/KMS, sem X11, sem browser/kiosk) + um updater mínimo separado. Serviços systemd: `mpv.service`, `signage-agent.service`, `signage-updater.service/.timer`.
- **Comunicação:** só HTTPS, sempre **iniciada pelo player** (pull), com `Authorization: Bearer <token>` por player. **Sem MQTT, sem SSH/rsync, sem portas abertas no Pi.**
- **Estado dos players:** heartbeat `POST /api/v1/heartbeat` a cada 30–60 s. Online/offline é calculado no servidor a partir de `last_seen`.
- **Media:** o player descarrega para disco local (manifest + sha256, com retoma por `Range`) e só depois toca. O ecrã nunca depende da rede.
- **Playlists:** cada player tem a sua playlist; itens com ordem (`position`), `start_date`/`end_date` inclusivas, em hora local (`Europe/Lisbon` por omissão), nulo = sem limite.
- **Updates do código do player:** pull, releases versionadas e imutáveis, **assinatura obrigatória** (chave privada fora do VPS), selftest, troca atómica de symlink, **rollback automático** se não houver commit em ~3 min. Canais canary/stable.

## Restrições do ambiente (afetam o código)

- **Cortes de energia bruscos** (sem UPS). O código tem de ser seguro contra desligar a meio: escritas atómicas (tmp + `rename`), downloads em `.staging` verificados antes de promover, nada de estado crítico só em memória. O root do Pi pode estar em overlay/só-leitura: **o agente escreve apenas em `/var/lib/signage`** (configurável) e confirma-se na máquina antes de assumir.
- **Ligação por hotspot móvel Vodafone, atrás de CGNAT, com dados limitados e instável.** Mantém o tráfego mínimo (manifest só quando o `playlist_hash` muda, nunca re-descarregar o que já tem o sha256 certo), timeouts curtos, backoff exponencial com jitter, tolerância a quedas e a mudança de IP.
- **O Pi não tem RTC.** O relógio pode estar errado após um arranque sem rede: se a hora for inválida, mantém a última lista e reporta `clock_drift`.
- **Pi 5 sem decodificação H.264 por hardware** (só HEVC). O perfil de vídeo aceite (spec §4.5) é um valor configurável a afinar com testes no ecrã real.
- Antes de escrever código que depende do SO (paths, versão do Python, mpv), verifica na máquina (`cat /etc/os-release`, `python3 --version`, `mpv --version`) em vez de assumir.

## Invariantes (nunca quebrar)

1. O player **nunca** fica com ecrã preto: playlist vazia ou sem itens válidos → imagem de fallback incluída na release.
2. Uma playlist nova **só é aplicada com todos os ficheiros presentes e verificados** (sha256 + tamanho). Manifest vazio ou inválido é recusado e nunca apaga nada.
3. Aplicar uma playlist nova **não pode interromper o item em reprodução** (testar o comportamento do mpv; `loadlist` em modo substituição reinicia o item).
4. Falhas (rede, VPS, ficheiro corrompido, update mau) deixam o ecrã a tocar conteúdo válido e são **reportadas**, nunca silenciadas.
5. **Comandos remotos só de uma lista fechada** (`sync-now`, `restart-player`, `reboot`). Nunca shell nem texto arbitrário.
6. O player só instala releases com assinatura válida.
7. Nenhum segredo no repositório (tokens, passwords, chaves). Configuração por `.env` / `config.toml` fora do git, permissões restritas.
8. Itens com datas são reavaliados periodicamente (cada 5 min e à meia-noite local), sem depender de novos ficheiros.

## Estrutura do repositório

```
/dashboard   FastAPI, templates, migrações SQL, testes
/player      agent, updater, selftest, assets/fallback.png
/tools       signagectl (provisionar/revogar players), script de release (assina e empacota)
/deploy      Caddyfile, units systemd (VPS e Pi), install.sh
/docs        especificacao-tecnica.md, instalação VPS, instalação Pi, runbook
```

## Convenções

- Python 3.11+. Dependências do player **empacotadas na release** (wheels); o player não faz `pip install` em produção.
- Dashboard: SQL com parâmetros (nunca concatenar), migrações versionadas, camada de acesso a dados isolada (para poder migrar de SQLite para Postgres se a escala o pedir).
- **Idioma: tudo em inglês** — interface do dashboard, identificadores, comentários, docstrings, mensagens de log e de erro, mensagens dos scripts (`install.sh`, `signagectl`) e mensagens de commit. A única exceção é a documentação em `docs/` (especificação, guias de instalação, runbook) e este ficheiro, que ficam em português.
- Testes com `pytest`; lint/format com `ruff`. Os cenários de falha da spec §9 que sejam automatizáveis têm teste.
- Ao adicionar uma dependência, justifica-a. A prioridade é simplicidade e robustez; preferir a stdlib e evitar bibliotecas pesadas.
- Nada de funcionalidades fora da spec sem pedido explícito. Ideias novas vão para a lista de evolução, não para o código.

## Fases

| Fase | Âmbito |
|---|---|
| 1 | Dashboard (upload, playlists, manifest) + agente (sync, motor de playlist, mpv, fallback) |
| 2 | Heartbeat, estado real, offline por `last_seen`, comandos |
| 3 | Updater assinado, selftest, rollback, canais |
| 4 | Alertas, backups, hardening, provisionamento, acesso de emergência (WireGuard) |

**Fase atual: 1.** Não avances para a fase seguinte sem o critério de aceitação da atual cumprido (spec §10.3).

## Comandos úteis

_(Preencher à medida que o projeto é criado: arrancar o dashboard em dev, correr testes, construir e assinar uma release, instalar no Pi.)_