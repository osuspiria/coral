# Backups e restauro (VPS)

Backup diário (03:30) com **restic** para uma **Hetzner Storage Box**: snapshot consistente da base de dados, media, releases e `/etc/coral` (inclui o `.env`; o restic cifra tudo). Retenção: 7 diários, 4 semanais, 6 mensais; `restic check` ao domingo. O healthchecks.io avisa se um backup não acontecer.

> **A password do restic é a chave de tudo.** Sem ela os backups são ilegíveis. Guardar uma cópia **fora do VPS** (gestor de passwords) no dia da configuração.

## 1. Configurar (uma vez)

No painel da Hetzner: criar a Storage Box, ativar **SSH support**, criar uma subconta (ex.: `u123456-sub1`) com diretório próprio.

No VPS, como root:

```bash
sudo apt install -y restic
# Chave SSH só para os backups
sudo ssh-keygen -t ed25519 -N "" -f /root/.ssh/storagebox
sudo cat /root/.ssh/storagebox.pub   # colar no painel da Storage Box (subconta, SSH keys)
sudo tee -a /root/.ssh/config > /dev/null <<'EOF'
Host storagebox
    HostName u123456-sub1.your-storagebox.de
    User u123456-sub1
    Port 23
    IdentityFile /root/.ssh/storagebox
EOF
sudo sftp storagebox <<< 'ls'          # aceitar a chave do host e testar

# Password do restic (guardar cópia fora do VPS!)
sudo sh -c 'umask 077; openssl rand -base64 32 > /etc/coral/restic.pass'
sudo cp /opt/coral/deploy/vps/backup.env.example /etc/coral/backup.env
sudo chmod 600 /etc/coral/backup.env
sudo nano /etc/coral/backup.env        # HEALTHCHECK_BACKUP_URL do check "coral-backup"
sudo sh -c 'set -a; . /etc/coral/backup.env; restic init'

sudo cp /opt/coral/deploy/vps/coral-backup.service /opt/coral/deploy/vps/coral-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now coral-backup.timer
sudo systemctl start coral-backup      # primeiro backup já
journalctl -u coral-backup -n 30 --no-pager
```

## 2. Ver os backups

```bash
sudo sh -c 'set -a; . /etc/coral/backup.env; restic snapshots'
```

## 3. Restaurar

### Teste de restauro (fazer depois de configurar e periodicamente)

```bash
sudo sh -c 'set -a; . /etc/coral/backup.env; restic restore latest --target /tmp/restore'
R=/tmp/restore/var/lib/coral
sudo cp $R/backup/coral.sqlite3 $R/coral.sqlite3          # a snapshot é a base de dados a usar
cd /opt/coral
sudo PYTHONPATH=dashboard:tools .venv/bin/python -m signagectl verify-data --data-dir $R
```

O resultado tem de terminar em `OK   data directory is complete and consistent`. Para ver os dados restaurados no browser sem tocar no dashboard real, arrancar uma cópia noutra porta e abrir um túnel SSH:

```bash
sudo env $(sudo grep -v '^#' /etc/coral/dashboard.env | xargs) CORAL_DATA_DIR=$R CORAL_COOKIE_SECURE=false \
    PYTHONPATH=dashboard .venv/bin/uvicorn coral_dashboard.main:create_app --factory --port 8099
# no teu PC: ssh -L 8099:127.0.0.1:8099 <admin>@<vps>  → http://127.0.0.1:8099
```

No fim: `sudo rm -rf /tmp/restore`.

### Restauro real (VPS perdido ou dados destruídos)

1. Instalar um VPS novo (`docs/instalacao-vps.md`, passos 0–2), com a Storage Box configurada (secção 1 acima, mas **sem** `restic init`) e a **mesma** password do restic.
2. `sudo systemctl stop coral-dashboard`
3. Restaurar e pôr no sítio:
   ```bash
   sudo sh -c 'set -a; . /etc/coral/backup.env; restic restore latest --target /tmp/restore'
   R=/tmp/restore/var/lib/coral
   sudo rsync -a $R/media $R/releases /var/lib/coral/
   sudo cp $R/backup/coral.sqlite3 /var/lib/coral/coral.sqlite3
   sudo rm -f /var/lib/coral/coral.sqlite3-wal /var/lib/coral/coral.sqlite3-shm
   sudo cp /tmp/restore/etc/coral/dashboard.env /etc/coral/dashboard.env
   sudo chown -R coral:coral /var/lib/coral
   sudo PYTHONPATH=dashboard:tools /opt/coral/.venv/bin/python -m signagectl verify-data --data-dir /var/lib/coral
   sudo systemctl start coral-dashboard
   ```
4. Apontar o DNS para o VPS novo. Os players continuam com os mesmos tokens e voltam a ligar-se sozinhos.
