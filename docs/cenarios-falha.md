# Cenários de falha (spec §9): como cada um é testado

Critério de aceitação da fase 4: todos os cenários passam. **A** = teste automático (corre em `uv run pytest`; os marcados *Linux* precisam do WSL/Linux), **M** = verificação manual no Pi (com o resultado registado na spec §10.3).

| Cenário | Comportamento esperado | Testes |
|---|---|---|
| Corte de energia | Reinicia sozinho; toca a última lista válida; sem corrupção | A `player/tests/test_agent.py::test_power_cut_restores_last_list_before_network`; M fase 1 (tirar a ficha) |
| Rede em baixo | Continua a tocar; backoff; ao voltar sincroniza | A `test_agent.py::test_heartbeat_network_failure_uses_backoff`; M fase 2 (rede cortada 4 min) |
| VPS em baixo | Igual; o healthchecks.io avisa | A `test_agent.py::test_power_cut_restores_last_list_before_network` (servidor 500); M fase 4 (parar o dashboard) |
| Ficheiro corrompido/incompleto | Não promovido; lista anterior mantém-se; erro reportado | A `test_sync.py::test_corrupted_file_is_not_promoted`, `test_agent.py::test_corrupted_file_keeps_previous_playlist`, `test_sync.py::test_interrupted_download_resumes_with_range` |
| Manifest vazio (sem `cleared`) ou inválido | Recusado; nada apagado; alerta | A `test_agent.py::test_empty_manifest_without_flag_is_refused`, `::test_invalid_manifest_is_refused`, `dashboard/tests/test_alerts.py` (sync_failing); M fase 1 passo 5 |
| Limpeza explícita | Fallback imediato; cache mantida | A `test_agent.py::test_cleared_shows_fallback_and_keeps_cache`; M fase 1 passo 4 |
| Sem playlist atribuída | Mantém o que tem; fallback no 1.º arranque | A `test_agent.py::test_first_boot_unassigned_shows_fallback`, `::test_unassign_without_clear_keeps_list` |
| Manifest 404 / 5xx | Nunca limpa | A `test_agent.py::test_404_and_5xx_never_clear` |
| Playlist sem itens válidos hoje | Fallback | A `test_agent.py::test_no_valid_items_today_shows_fallback` |
| Item expira à meia-noite | Sai sem novo sync | A `test_agent.py::test_item_expires_at_midnight_without_sync`; M fase 1 (journal 00:00:01) |
| Relógio errado no arranque | Mantém a lista; `clock_drift`; corrige com o chrony | A `test_agent.py::test_invalid_clock_keeps_last_list_and_reports`, `::test_server_time_skew_sets_clock_drift`, `test_alerts.py` (clock_drift) |
| Disco quase cheio | Sync recusa; lista anterior; alerta | A `test_sync.py::test_disk_full_refuses_download`, `test_agent.py::test_disk_full_keeps_previous_list`, `test_alerts.py` (disk_low) |
| Vídeo incompatível | Rejeitado no upload | A `dashboard/tests/test_media.py::test_rejects_incompatible_videos` (+ ffprobe real, *Linux*) |
| mpv bloqueado | Detetado; mpv reiniciado; reportado | A `test_agent.py::test_stuck_mpv_is_restarted_and_reported`, `::test_persistently_idle_mpv_is_restarted_not_reloaded_forever`, `test_alerts.py` (mpv_not_playing) |
| Comando para player offline | Expira ao fim de 15 min | A `dashboard/tests/test_commands_status.py::test_undelivered_command_expires_after_15_min` |
| Release com assinatura inválida | Rejeitada; nada instalado; reportado | A *Linux* `player/updater/tests/test_updater.py::test_bad_signatures_are_rejected`, `test_e2e_update.py::test_tampered_release_is_rejected` |
| Release que falha o selftest | Não instalada; reportado | A *Linux* `test_updater.py::test_selftest_failure_is_rejected` |
| Release que não arranca | Rollback automático ≈3 min; marcada como falhada | A *Linux* `test_updater.py::test_no_commit_rolls_back_after_deadline`, `test_e2e_update.py`; M fase 3 (0.3.3) |
| Token revogado | Heartbeat 401; continua a tocar; deixa de sincronizar | A `test_agent.py::test_revoked_token_keeps_playing_cache`, `::test_heartbeat_401_keeps_playing_and_backs_off` |
| Pi bloqueado por completo | Watchdog de hardware reinicia | M fase 4: `sudo sysctl -w kernel.panic=0` (para o kernel não reiniciar sozinho) e `echo c \| sudo tee /proc/sysrq-trigger` (kernel panic de teste) → o Pi reinicia sozinho e volta a tocar |

## Checklist manual da fase 4 (no Pi e no VPS) — feita a 14–15/11/2026, registo na spec §10.3

- [x] Alerta `offline` chega ao telemóvel ~10 min depois de cortar a rede do Pi; `RESOLVED` ao voltar.
- [x] Alerta `disk_low` com o limiar temporariamente alto (`CORAL_ALERT_DISK_MIN_MB` acima do disco livre), resolvido ao repor.
- [x] healthchecks.io avisa com o dashboard parado (`systemctl stop coral-dashboard` durante > período + tolerância).
- [x] Backup manual (`systemctl start coral-backup`) e **restauro testado** com `verify-data` (`docs/backups-restauro.md` §3).
- [x] SSH ao p0 pelo túnel WireGuard (`ssh -J`), com o Tailscale desligado no portátil (`tailscale down`).
- [x] Depois de um corte de corrente, o `wg-quick@wg0` volta sozinho (handshake recente em `sudo wg show` no VPS).
- [x] Watchdog: kernel panic de teste → reinício automático.
