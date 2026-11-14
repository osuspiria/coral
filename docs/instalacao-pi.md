# Instalação do player (Raspberry Pi 5)

Pressupostos: Raspberry Pi OS **Lite** 64-bit instalado no SSD (Debian 12 ou 13), ligação ao hotspot (no piloto, por USB na `eth1`, ver `docs/acesso-remoto-pi-tailscale-wireguard.md` §2), acesso por teclado/ecrã ou SSH **durante a instalação**. A instalação faz-se com `deploy/pi/install.sh` (idempotente: pode correr outra vez sem estragar nada). A partir daí o código atualiza-se sozinho com releases assinadas (`docs/releases.md`).

## 0. No dashboard

```bash
# no VPS (ver docs/instalacao-vps.md §5)
signagectl player add --label <nome> --location "<local>"     # guardar o token (mostrado uma vez)
```

## 1. Copiar os ficheiros para o Pi

Do computador de desenvolvimento (release assinada, ver `docs/releases.md`):

```bash
V=0.3.5
scp dist/coral-player-$V.tar.gz dist/coral-player-$V.tar.gz.minisig deploy/pi/release-pubkey.pub \
    <utilizador>@<ip-do-pi>:                    # os ":" no fim são obrigatórios
```

## 2. Correr o instalador

No Pi:

```bash
V=0.3.5
mkdir -p ~/coral-install && tar xzf coral-player-$V.tar.gz -C ~/coral-install deploy/pi/install.sh
echo '<token>' > ~/token && chmod 600 ~/token
sudo ~/coral-install/deploy/pi/install.sh --dry-run \
    --release ~/coral-player-$V.tar.gz --pubkey ~/release-pubkey.pub \
    --server https://<nome-dns-do-dashboard> --token-file ~/token       # ver o que vai fazer
sudo ~/coral-install/deploy/pi/install.sh \
    --release ~/coral-player-$V.tar.gz --pubkey ~/release-pubkey.pub \
    --server https://<nome-dns-do-dashboard> --token-file ~/token
rm ~/token
```

O instalador:

- confirma o SO e o Python ≥ 3.11 (pára se não for o caso);
- instala mpv, polkitd, minisign, chrony, wireguard-tools, unattended-upgrades, nftables;
- cria o utilizador `signage`, **verifica a assinatura da release antes de extrair**, escreve `config.toml` e token (nunca os reescreve se já existirem);
- instala os serviços (mpv, agente, updater + timer), a regra polkit, o journald limitado;
- configura o **chrony** (o Pi não tem RTC: o relógio é acertado de uma vez sempre que estiver errado), o **watchdog de hardware** (o limite é lido do próprio dispositivo) e as **atualizações automáticas só de segurança**;
- gera a chave WireGuard do Pi e mostra a pública (para o acesso de emergência, `docs/acesso-emergencia.md`).

O ecrã mostra logo o fallback (azul-escuro) e, depois do primeiro sync, a playlist atribuída.

## 3. Verificar

```bash
systemctl status mpv signage-agent
systemctl list-timers signage-updater.timer
journalctl -u signage-agent -f            # "Connected to mpv", "Applied manifest ...", sem "Heartbeat failed"
cat /etc/systemd/system.conf.d/signage-watchdog.conf
chronyc tracking | head -3
```

No dashboard, o player aparece **online** com a versão, disco, temperatura e uptime.

## 4. Saída de vídeo (validado no primeiro Pi 5, mpv 0.40.0)

O `mpv.service` usa `--vo=gpu --gpu-context=drm --drm-device=/dev/dri/by-path/platform-axi:gpu-card --swapchain-depth=1`:

- O ecrã está no dispositivo `vc4` (`platform-axi:gpu-card`); o `card0` costuma ser o `v3d`, sem saídas, e a numeração `cardN` pode mudar entre arranques. Confirmar com `ls -l /dev/dri/by-path/` e `cat /sys/class/drm/card*-HDMI-A-*/status`.
- Sem `--swapchain-depth=1` cada imagem aparece com um item de atraso (o `--vo=drm` tem o mesmo problema).
- Os avisos `VT_GETMODE failed` ao correr o mpv por SSH são inofensivos.

Teste do comportamento do mpv no ecrã real (lista nova sem interromper o item em reprodução):

```bash
sudo apt install -y ffmpeg      # só para o teste
sudo systemctl stop signage-agent mpv
sudo -u signage env PYTHONPATH=/opt/signage/current/player \
    python3 /opt/signage/current/player/tests/test_mpv_real.py --vo=drm
sudo systemctl start mpv signage-agent
```

Esperado: cinco linhas `OK` e `All checks passed.`

## 5. Pis novos: partição de dados e root só-leitura (spec §4.1)

Com root só-leitura (overlay), um corte de energia não pode corromper o sistema: tudo o que é escrito na root perde-se no arranque seguinte. O que tem de persistir (releases, media, estado do agente e do updater) vai para uma **partição de dados** própria.

### 5.1 Criar a partição de dados (com o microSD de reserva)

O Raspberry Pi OS expande a root para o disco inteiro no primeiro arranque; para criar a partição 3 é preciso encolher a root **com ela desmontada**, ou seja, arrancando pelo **microSD de reserva**:

1. Desligar o Pi, pôr o microSD (Raspberry Pi OS) e arrancar por ele (se o SSD tiver prioridade, ajustar a ordem de arranque com `sudo raspi-config` → Advanced → Boot Order).
2. No Pi arrancado pelo SD, **confirmar os nomes na máquina** antes de qualquer comando:
   ```bash
   lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT      # esperado: nvme0n1p1 (boot), nvme0n1p2 (root), nada do nvme montado
   ```
3. Encolher a root do SSD (exemplo: 32 GiB para o sistema) e criar a partição de dados no resto:
   ```bash
   sudo e2fsck -f /dev/nvme0n1p2
   sudo resize2fs /dev/nvme0n1p2 30G               # o sistema de ficheiros fica abaixo da partição nova
   sudo parted /dev/nvme0n1 ---pretend-input-tty resizepart 2 32GiB
   sudo resize2fs /dev/nvme0n1p2                   # volta a ocupar a partição (32 GiB)
   sudo parted /dev/nvme0n1 mkpart primary ext4 32GiB 100%
   lsblk /dev/nvme0n1                              # aparece nvme0n1p3, sem sistema de ficheiros
   ```
4. Desligar, retirar o microSD, arrancar pelo SSD.

### 5.2 Instalar com partição de dados e overlay

```bash
sudo ~/coral-install/deploy/pi/install.sh ... --data-partition /dev/nvme0n1p3 --readonly-root
sudo reboot
```

O instalador formata a partição (recusa se já tiver outro sistema de ficheiros ou dados que não sejam nossos, a não ser com `--force`), monta-a em `/srv/signage`, liga `/opt/signage`, `/var/lib/signage` e `/var/lib/signage-updater` a ela (copiando o que já lá estiver), desliga as atualizações automáticas do SO (com overlay perder-se-iam) e ativa o overlay. Depois do reboot: `findmnt /` mostra `overlay`.

### 5.3 Modo manutenção (atualizar o SO com overlay)

```bash
sudo raspi-config nonint do_overlayfs 1 && sudo reboot        # desligar o overlay
sudo apt update && sudo apt full-upgrade                      # atualizar
sudo raspi-config nonint do_overlayfs 0 && sudo reboot        # voltar a ligar
```

As atualizações do **código do player** não precisam disto: vivem na partição de dados e o updater trata delas.

## 6. Pis já instalados (como o p0)

Correr o `install.sh` com a release atual (sem `--data-partition`/`--readonly-root`): instala o que faltar (chrony, watchdog, atualizações de segurança, chave WireGuard) e não mexe na configuração, no token nem na versão em uso. Para passar um Pi já instalado para partição de dados + overlay: secção 5 (5.1 e depois 5.2 com a mesma release).

## 7. Atualizações

- **Código do player:** automáticas a partir da 0.3.0 (`docs/releases.md`).
- **Units systemd, regra polkit, código do updater:** nunca chegam pelo updater. Para os aplicar num Pi já instalado, correr outra vez o `install.sh` com a release nova.
- **Voltar atrás à mão:** `sudo ln -sfn /opt/signage/releases/<versão> /opt/signage/current && sudo systemctl restart mpv signage-agent`.

## 8. Notas

- O agente corre sem root e só escreve em `/var/lib/signage`; o updater (root) só lê os pedidos do agente e guarda o seu estado em `/var/lib/signage-updater`.
- São mantidas as últimas 3 releases, mais a atual e a anterior.
- Heartbeat a cada 30–60 s; offline no dashboard ao fim de 3 heartbeats falhados; alertas por ntfy (`docs/runbook.md`).
- Sem progresso no ecrã durante mais de 2 min (ou a duração da imagem + 30 s), o agente reinicia o mpv e reporta-o.
