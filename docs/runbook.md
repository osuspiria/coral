# Runbook — o que fazer quando há um alerta

Os alertas chegam ao telemóvel pelo **ntfy** (título `player (local): tipo`) e ficam na página **Alerts** do dashboard. Cada alerta notifica ao abrir e outra vez ao resolver (`RESOLVED …`). Regra geral: **o ecrã continua a tocar conteúdo válido em quase todos os casos** — há tempo para investigar.

Ferramentas, da mais leve para a mais pesada:

1. Página do player no dashboard (estado completo, último erro, comandos).
2. Comandos remotos: `sync-now` → `restart-player` → `reboot`.
3. Acesso de emergência por WireGuard (`docs/acesso-emergencia.md`): `ssh -J <admin>@<vps> <utilizador>@10.77.x.y`, depois `journalctl -u signage-agent -u mpv -u signage-updater -n 200`.
4. Ir ao local.

## offline — sem heartbeat há mais de 10 min

- **Primeiro:** é só um player ou são todos? Todos → o problema é do VPS/rede central (ver "dashboard em baixo").
- Um só: corte de energia, hotspot sem rede/dados esgotados, router desligado. O ecrã, se tiver energia, continua a tocar a cache.
- Esperar 15–30 min (hotspots móveis caem e voltam). Se não voltar: contactar o local (está ligado? o hotspot tem dados?).
- Ao voltar, o player sincroniza sozinho e o alerta resolve-se.

## mpv_not_playing — o ecrã não está a tocar (2 heartbeats seguidos)

- O agente já tenta sozinho: recarrega a lista e, se não chegar, reinicia o mpv (ver `mpv restarts` na página do player).
- Se persistir: `restart-player`; depois `reboot`.
- Se continuar: WireGuard → `journalctl -u mpv -b`; causas prováveis: TV desligada/HDMI solto (o mpv não arranca sem saída), ficheiro de vídeo que o Pi não consegue descodificar (ver perfil §4.5), SSD com erros (`dmesg`).

## sync_failing — a sincronização falha há mais de 30 min

- Ler o erro na página do player:
  - `refused manifest: empty manifest…` → a playlist atribuída está vazia. Juntar itens, ou usar **Clear screen** se a intenção era limpar o ecrã.
  - `sha256 mismatch` → ficheiro corrompido no VPS ou na rede; voltar a fazer upload do ficheiro; `sync-now`.
  - `not enough disk space` → ver "disk_low".
  - `network error` / `HTTP 5xx` → problema de rede ou do VPS; ver se outros players também falham.
  - `401` → token revogado ou errado: o player continua a tocar a cache; se foi engano, provisionar de novo (`signagectl player add` + token no Pi).

## update_failed — update revertido ou rejeitado

- `rolled_back <versão>`: a versão nova não confirmou um arranque saudável e o Pi voltou à anterior sozinho. **Não instalar essa versão noutros players.** Ver o log do agente da versão falhada (WireGuard: `journalctl -u signage-agent --since -1h`), corrigir, publicar uma versão **nova**.
- `rejected <versão>`: assinatura inválida ou selftest falhado. Assinatura: a release no VPS não é a assinada (VPS comprometido? ficheiro errado?) — **investigar antes de mais nada**. Selftest: ver `journalctl -u signage-updater`.
- A versão fica como "Failed version" no player e não é pedida outra vez; "Try again" só depois de corrigida a causa.

## disk_low — menos de 2 GB livres

- A sync recusa descarregar e mantém a lista anterior; nada se perde.
- Causas: biblioteca de media demasiado grande para o SSD, ou ficheiros antigos ainda em período de graça (7 dias). Reduzir a playlist, esperar a limpeza, ou (WireGuard) ver `du -sh /var/lib/signage/media`.

## temp_high — temperatura acima de 80 °C

- O Pi 5 reduz a velocidade sozinho a partir de ~85 °C. Ver a ventoinha da caixa, ventilação do local, sol direto.
- Se for persistente: ir ao local (ventoinha avariada, caixa fechada sem ventilação).

## clock_drift — relógio errado

- O player mantém a última lista (não aplica datas com um relógio errado).
- Normalmente resolve-se sozinho quando o chrony sincroniza (precisa de rede). Se persistir com o player online: WireGuard → `chronyc tracking`, `chronyc sources`; firewall do hotspot a bloquear NTP (UDP 123)?

## Dashboard em baixo (aviso do healthchecks.io, não do ntfy)

- O VPS ou o serviço caiu: `ssh <admin>@<vps>`, `systemctl status coral-dashboard caddy`, `journalctl -u coral-dashboard -n 100`.
- Os players continuam a tocar a cache; voltam a sincronizar sozinhos.
- VPS perdido: reinstalar (`docs/instalacao-vps.md`) e restaurar o backup (`docs/backups-restauro.md`).

## Backup falhado (aviso do healthchecks.io do check "backup")

- `journalctl -u coral-backup -n 100` no VPS. Causas: Google Drive inacessível (`sudo rclone --config /etc/coral/rclone.conf lsd gdrive:`), service account removida do Shared Drive ou chave revogada, password do restic.
- Correr à mão depois de corrigir: `sudo systemctl start coral-backup` e confirmar o ping.
