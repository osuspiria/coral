# Instalação do player (Raspberry Pi 5) — Fase 1

Pressupostos: Raspberry Pi OS **Lite** 64-bit já instalado e a arrancar pelo SSD, com Wi-Fi ligado ao hotspot. Os comandos correm no Pi, com teclado/ecrã ou SSH **apenas durante a instalação**. Na fase 1 ainda não há `install.sh` nem updater (fases 3 e 4): a instalação é manual e está aqui descrita passo a passo.

## 0. Confirmar a máquina (não assumir)

```bash
cat /etc/os-release | head -3        # esperado: Debian 12 (bookworm) ou 13 (trixie)
python3 --version                    # tem de ser ≥ 3.11
uname -m                             # aarch64
timedatectl show -p NTPSynchronized  # relógio sincronizado?
findmnt /                            # root normal ou overlay? (o agente só escreve em /var/lib/signage)
```

Se o Python for < 3.11, **parar** e reportar: o agente usa `tomllib` (3.11+).

## 1. Pacotes

```bash
sudo apt update
sudo apt install -y mpv
mpv --version | head -1              # anotar a versão (IPC validado no 0.34.1 e no 0.40.0 do Pi 5)
sudo apt install -y ffmpeg           # só para o teste de validação do passo 7 (pode remover-se depois)
```

O agente não tem dependências Python externas (só a stdlib), não há `pip install`.

## 2. Utilizador e diretórios

```bash
sudo useradd --system --home-dir /var/lib/signage --no-create-home \
    --shell /usr/sbin/nologin --groups video,render,audio signage
sudo mkdir -p /opt/signage/releases /etc/signage
```

## 3. Código (release 0.1.0)

No computador de desenvolvimento, na raiz do repositório:

```bash
git archive --format=tar.gz -o coral-player-0.1.0.tar.gz HEAD player deploy
```

Copiar o ficheiro para o Pi (pen USB ou `scp`) e, no Pi:

```bash
sudo mkdir -p /opt/signage/releases/0.1.0
sudo tar xzf coral-player-0.1.0.tar.gz -C /opt/signage/releases/0.1.0
sudo ln -sfn /opt/signage/releases/0.1.0 /opt/signage/current
ls /opt/signage/current/player/assets/fallback.png
```

## 4. Configuração e token

```bash
sudo cp /opt/signage/current/player/config.example.toml /etc/signage/config.toml
sudo nano /etc/signage/config.toml        # server_url = "https://<nome-dns-do-dashboard>"
echo '<token-dado-pelo-signagectl>' | sudo tee /etc/signage/token > /dev/null
sudo chown root:signage /etc/signage/config.toml /etc/signage/token
sudo chmod 640 /etc/signage/config.toml
sudo chmod 640 /etc/signage/token
```

(O token fica legível pelo grupo `signage`, porque o agente corre como esse utilizador; ninguém mais o lê.)

## 5. Serviços systemd

```bash
D=/opt/signage/current/deploy/pi
sudo cp $D/mpv.service $D/signage-agent.service /etc/systemd/system/
sudo cp $D/tmpfiles-signage.conf /etc/tmpfiles.d/signage.conf
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp $D/journald-signage.conf /etc/systemd/journald.conf.d/signage.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/signage.conf
sudo systemctl restart systemd-journald
sudo systemctl daemon-reload
sudo systemctl enable --now mpv.service signage-agent.service
```

O ecrã deve mostrar logo a imagem de fallback (azul-escuro liso). Depois do primeiro sync passa para a playlist atribuída.

## 6. Verificar

```bash
systemctl status mpv signage-agent
journalctl -u signage-agent -f            # "Connected to mpv", "Applied manifest ..."
journalctl -u mpv -b --no-pager | tail -30
sudo cat /var/lib/signage/state.json      # playing, last_sync_ok, last_sync_error, clock_drift
ls -la /var/lib/signage/media
```

**Saída de vídeo no Pi 5** (validado no primeiro Pi, mpv 0.40.0): o `mpv.service` usa `--vo=gpu --gpu-context=drm --drm-device=/dev/dri/by-path/platform-axi:gpu-card --swapchain-depth=1`.

- O ecrã está no dispositivo `vc4` (`platform-axi:gpu-card`). O `card0` costuma ser o `v3d`, sem saídas, e a numeração `cardN` pode mudar entre arranques. Confirmar com `ls -l /dev/dri/by-path/` e `cat /sys/class/drm/card*-HDMI-A-*/status`.
- Sem `--swapchain-depth=1` cada imagem só aparece quando chega o frame seguinte, ou seja, com um item de atraso (a IMAGEM 2 de uma lista "pisca" e a IMAGEM 1 fica no ecrã o tempo das duas). O `--vo=drm` tem o mesmo problema.
- Os avisos `VT_GETMODE failed` ao correr o mpv por SSH são inofensivos.

## 7. Validar o comportamento do mpv no Pi (invariante 3)

Este teste usa o mpv real do Pi, no ecrã, e confirma que uma lista nova não reinicia o item em reprodução. Pára os serviços enquanto corre:

```bash
sudo systemctl stop signage-agent mpv
sudo -u signage env PYTHONPATH=/opt/signage/current/player \
    python3 /opt/signage/current/player/tests/test_mpv_real.py --vo=drm
# se o passo 6 precisou de --vo=drm, usar --vo=drm aqui
sudo systemctl start mpv signage-agent
```

O resultado esperado é cinco linhas `OK` e `All checks passed.` Guardar a saída (inclui a versão do mpv). No primeiro Pi (mpv 0.40.0) passaram as cinco.

Este teste verifica o estado do mpv, não o que aparece no ecrã. Para confirmar que as imagens aparecem a tempo, gerar duas imagens e mostrá-las com durações diferentes (vermelho 2 s, verde 6 s); se o vermelho ficar 6 s e o verde 2 s, há um item de atraso (falta `--swapchain-depth=1`):

```bash
sudo systemctl stop signage-agent mpv
cd /tmp
ffmpeg -v error -y -f lavfi -i color=c=red:s=1920x1080 -frames:v 1 r.png
ffmpeg -v error -y -f lavfi -i color=c=green:s=1920x1080 -frames:v 1 g.png
sudo -u signage timeout 20 mpv --vo=gpu --gpu-context=drm     --drm-device=/dev/dri/by-path/platform-axi:gpu-card --swapchain-depth=1     --loop-playlist=inf --really-quiet     --{ --image-display-duration=2 /tmp/r.png --} --{ --image-display-duration=6 /tmp/g.png --}
sudo systemctl start mpv signage-agent
```

## 8. Notas e limitações da fase 1

- Root só-leitura (overlay), partição de dados separada, watchdog de hardware, chrony e `install.sh` são da fase 4. O agente já só escreve em `/var/lib/signage` e usa escritas atómicas, por isso aguenta cortes de energia.
- Não há heartbeat (fase 2). O agente faz polling de `GET /api/v1/manifest` a cada 60 s (com ETag/304), e os erros ficam no journal e em `state.json`.
- A deteção do mpv bloqueado (§4.6) é da fase 2. Até lá, o `Restart=always` cobre só os crashes do mpv.
