# Instalação do player (Raspberry Pi 5)

Pressupostos: Raspberry Pi OS **Lite** 64-bit já instalado e a arrancar pelo SSD, com Wi-Fi ligado ao hotspot. Os comandos correm no Pi, com teclado/ecrã ou SSH **apenas durante a instalação**. A instalação inicial é manual e está aqui descrita passo a passo (o `install.sh` é da fase 4). A partir da **0.3.0** o player atualiza-se sozinho com releases assinadas (updater, ver `docs/releases.md`); só a primeira instalação e as mudanças de units systemd são manuais.

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
sudo apt install -y mpv polkitd minisign
mpv --version | head -1              # anotar a versão (IPC validado no 0.34.1 e no 0.40.0 do Pi 5)
sudo apt install -y ffmpeg           # só para o teste de validação do passo 7 (pode remover-se depois)
pkaction --version                   # polkit ≥ 0.106 (regras em JavaScript); o Bookworm traz o 122
minisign -v                          # verificação das releases (spec §6)
```

O agente não tem dependências Python externas (só a stdlib), não há `pip install`.

## 2. Utilizador e diretórios

```bash
sudo useradd --system --home-dir /var/lib/signage --no-create-home \
    --shell /usr/sbin/nologin --groups video,render,audio signage
sudo mkdir -p /opt/signage/releases /etc/signage
```

## 3. Código (release)

Usar uma release assinada, construída no computador de desenvolvimento como em `docs/releases.md` (`dist/coral-player-<v>.tar.gz` + `.minisig`). Copiar para o Pi os dois ficheiros e a chave pública:

```bash
V=0.3.0
scp dist/coral-player-$V.tar.gz dist/coral-player-$V.tar.gz.minisig deploy/pi/release-pubkey.pub \
    <utilizador>@<ip-do-pi>:                              # os ":" no fim são obrigatórios
```

No Pi, verificar a assinatura **antes** de instalar e só depois extrair:

```bash
V=0.3.0
sudo install -m 644 release-pubkey.pub /etc/signage/release-pubkey.pub
minisign -V -p /etc/signage/release-pubkey.pub -m coral-player-$V.tar.gz   # "Signature and comment signature verified"
sudo mkdir -p /opt/signage/releases/$V
sudo tar xzf coral-player-$V.tar.gz -C /opt/signage/releases/$V
sudo ln -sfn /opt/signage/releases/$V /opt/signage/current
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
sudo install -m 644 $D/polkit-signage.rules /etc/polkit-1/rules.d/50-signage.rules
sudo systemd-tmpfiles --create /etc/tmpfiles.d/signage.conf
sudo systemctl restart systemd-journald
# Updater (fica fora das releases e não se atualiza a si próprio, spec §6.1):
sudo rm -rf /opt/signage/updater && sudo mkdir -p /opt/signage/updater
sudo cp -r /opt/signage/current/player/updater/coral_updater /opt/signage/updater/
sudo cp $D/signage-updater.service $D/signage-updater.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mpv.service signage-agent.service signage-updater.timer
```

O ecrã deve mostrar logo a imagem de fallback (azul-escuro liso). Depois do primeiro sync passa para a playlist atribuída.

A regra polkit permite ao utilizador `signage` (sem root) fazer só duas coisas: `systemctl restart mpv.service` (comando `restart-player` e reinício automático do mpv bloqueado) e `systemctl reboot` (comando `reboot`). Confirmar:

```bash
sudo -u signage systemctl --no-ask-password restart --no-block mpv.service && echo "restart OK"
sudo -u signage systemctl --no-ask-password stop mpv.service   # tem de ser RECUSADO (fora da regra)
sudo systemctl start mpv.service
```

## 6. Verificar

```bash
systemctl status mpv signage-agent
systemctl list-timers signage-updater.timer      # próxima execução do updater
journalctl -u signage-updater -n 20 --no-pager   # o que o updater fez
cat /var/lib/signage-updater/status.json 2>/dev/null   # estado do último update
journalctl -u signage-agent -f            # "Connected to mpv", "Applied manifest ...", sem "Heartbeat failed"
journalctl -u mpv -b --no-pager | tail -30
sudo cat /var/lib/signage/state.json      # playing, last_sync_*, last_heartbeat_*, clock_drift
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
# --vo=drm basta aqui: o teste verifica o estado do mpv, não a imagem no ecrã
sudo systemctl start mpv signage-agent
```

O resultado esperado é cinco linhas `OK` e `All checks passed.` Guardar a saída (inclui a versão do mpv). No primeiro Pi (mpv 0.40.0) passaram as cinco.

Este teste verifica o estado do mpv, não o que aparece no ecrã. Para confirmar que as imagens aparecem a tempo, gerar duas imagens e mostrá-las com durações diferentes (vermelho 2 s, verde 6 s); se o vermelho ficar 6 s e o verde 2 s, há um item de atraso (falta `--swapchain-depth=1`):

```bash
sudo systemctl stop signage-agent mpv
cd /tmp
ffmpeg -v error -y -f lavfi -i color=c=red:s=1920x1080 -frames:v 1 r.png
ffmpeg -v error -y -f lavfi -i color=c=green:s=1920x1080 -frames:v 1 g.png
sudo -u signage timeout 20 mpv --vo=gpu --gpu-context=drm \
    --drm-device=/dev/dri/by-path/platform-axi:gpu-card --swapchain-depth=1 \
    --loop-playlist=inf --really-quiet \
    --{ --image-display-duration=2 /tmp/r.png --} --{ --image-display-duration=6 /tmp/g.png --}
sudo systemctl start mpv signage-agent
```

## 8. Atualizações

**A partir da 0.3.0 são automáticas**: publicar a release no dashboard e escolher a versão no canal ou no player (`docs/releases.md`). O Pi faz o resto no minuto seguinte ao heartbeat: descarrega, verifica a assinatura, corre o selftest, troca de versão e confirma; se a versão nova não confirmar em ~3 min, volta à anterior sozinho e o dashboard mostra a versão como falhada.

**Passar um Pi de 0.2.x para 0.3.0** (uma vez, porque a 0.2.x não tem updater): instalar o minisign (passo 1), seguir o passo 3 com a release 0.3.0 e o passo 5 completo (units novos, tmpfiles, updater e timer). Os units de `mpv` e `signage-agent` não mudaram, mas convém copiá-los de novo.

**Mudanças nos units systemd ou na regra polkit** nunca chegam pelo updater: repetir os comandos do passo 5 que lhes dizem respeito.

**Voltar atrás à mão** (se for preciso): `sudo ln -sfn /opt/signage/releases/<versão> /opt/signage/current && sudo systemctl restart mpv signage-agent`.

## 9. Notas e limitações

- Root só-leitura (overlay), partição de dados separada, watchdog de hardware, chrony e `install.sh` são da fase 4. O agente já só escreve em `/var/lib/signage` e usa escritas atómicas, por isso aguenta cortes de energia.
- Heartbeat a cada 30–60 s (valor do servidor). O dashboard mostra o player offline ao fim de 3 heartbeats falhados. Os erros ficam também no journal e em `state.json`.
- Sem progresso no ecrã durante mais de 2 min (ou a duração da imagem + 30 s), o agente reinicia o `mpv.service` e reporta-o.
- O updater corre como root (precisa de escrever em `/opt/signage` e reiniciar serviços); o agente continua sem privilégios e só pede e confirma updates através de ficheiros em `/var/lib/signage/update/`. São mantidas as últimas 3 releases, mais a atual e a anterior.
