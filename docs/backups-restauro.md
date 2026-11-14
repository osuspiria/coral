# Backups e restauro (VPS)

Backup diário (03:30) com **restic** para um **Shared Drive do Google Workspace** (através do `rclone`, com uma service account que só é membro desse drive): snapshot consistente da base de dados, media, releases e `/etc/coral` (inclui o `.env`; o restic cifra tudo). Retenção: 7 diários, 4 semanais, 6 mensais; `restic check` ao domingo. O healthchecks.io avisa se um backup não acontecer.

> **A password do restic é a chave de tudo.** Sem ela os backups são ilegíveis. Guardar uma cópia **fora do VPS** (gestor de passwords) no dia da configuração.

## 1. Configurar (uma vez)

### 1.1 Google (consola)

1. **Projeto e API:** em console.cloud.google.com, criar o projeto `coral-backup` e ativar a **Google Drive API** (*APIs & Services → Library*).
2. **Service account:** *IAM & Admin → Service Accounts → Create* (`coral-backup`, sem papéis no projeto); depois *Keys → Add key → JSON*.
   - Nas organizações Workspace a política `iam.disableServiceAccountKeyCreation` vem ligada por omissão e bloqueia este passo. Com o papel *Organization Administrator*, atribuir a si próprio *Organization Policy Administrator* (IAM da **organização**, não do projeto), desligar a política só no projeto `coral-backup` (*Organization Policies → "Disable service account key creation" → Override → Off*), criar a chave e **voltar a pôr a política como herdada** logo a seguir. A chave criada continua válida.
3. **Shared Drive:** em drive.google.com, criar o Shared Drive `coral-backup` e juntar o email da service account (`coral-backup@<projeto>.iam.gserviceaccount.com`) com o papel **Content manager** (o restic apaga ficheiros na retenção). A service account não vê mais nada do Drive.
4. Copiar o **ID do Shared Drive** (o fim do URL `https://drive.google.com/drive/folders/<ID>`).
5. No healthchecks.io, criar o check `coral-backup` (período 1 dia, tolerância 2 h).

### 1.2 VPS

```bash
sudo apt install -y restic rclone
# Chave da service account (no portátil: scp <chave>.json <admin>@<vps>:/tmp/sa.json; depois apagá-la do portátil)
sudo install -m 600 -o root -g root /tmp/sa.json /etc/coral/gdrive-sa.json && rm /tmp/sa.json

sudo rclone --config /etc/coral/rclone.conf config create gdrive drive \
    scope=drive service_account_file=/etc/coral/gdrive-sa.json team_drive=<ID-do-shared-drive>
sudo chmod 600 /etc/coral/rclone.conf
sudo rclone --config /etc/coral/rclone.conf lsd gdrive:      # tem de terminar sem erros

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

1. Instalar um VPS novo (`docs/instalacao-vps.md`, passos 0–2), com o acesso ao Google Drive configurado (secção 1.2 acima, mas **sem** `restic init`) e a **mesma** password do restic. A chave da service account e o `rclone.conf` também estão no backup (`/etc/coral`), mas só se leem depois de ter acesso: criar uma chave nova para a mesma service account (secção 1.1, passo 2) e revogar a antiga.
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
