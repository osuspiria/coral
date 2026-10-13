# Instalação do dashboard (VPS) — Fase 1

Guia para pôr o dashboard a correr num VPS Debian/Ubuntu com Caddy à frente (HTTPS automático). Tudo o que está entre `<...>` tem de ser substituído.

Os comandos usam `sudo` e assumem um utilizador de administração (não-root), criado no passo 0. O código em `/opt/coral` pertence ao root de propósito: o serviço corre como `coral` e só tem acesso de leitura ao código e de escrita a `/var/lib/coral`.

## 0. Utilizador de administração e acesso SSH

Num VPS acabado de criar só existe o `root`. Como `root`, cria o teu utilizador (`<admin>`, ex.: `oleg`):

```bash
adduser <admin>                       # pede uma password (fica para o sudo)
usermod -aG sudo <admin>
# Copiar a chave SSH com que entras como root para o novo utilizador
install -d -m 700 -o <admin> -g <admin> /home/<admin>/.ssh
install -m 600 -o <admin> -g <admin> /root/.ssh/authorized_keys /home/<admin>/.ssh/authorized_keys
```

**Noutro terminal, sem fechar a sessão de root**, confirmar que funciona:

```bash
ssh <admin>@<ip-do-vps>
sudo -v                               # tem de aceitar a password
```

Só depois de isso funcionar, desligar o login de root e por password:

```bash
sudo tee /etc/ssh/sshd_config.d/10-coral.conf > /dev/null <<'CONF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
CONF
sudo sshd -t && sudo systemctl reload ssh
```

Firewall (só SSH, HTTP e HTTPS):

```bash
sudo apt install -y ufw
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```

A partir daqui, tudo é feito como `<admin>` com `sudo`.

### Se já fizeste os passos seguintes como root

Não há nada a desfazer: os passos 1 e 2 correm de qualquer forma com privilégios de root. Faz só estas verificações:

```bash
which uv                              # tem de ser /usr/local/bin/uv
# Se estiver em /root/.local/bin (instalador corrido sem UV_INSTALL_DIR):
#   sudo install -m 755 /root/.local/bin/uv /root/.local/bin/uvx /usr/local/bin/

ls -l /opt/coral/.venv/bin/python     # o destino NÃO pode estar dentro de /root
# Se apontar para /root/... o utilizador coral não o consegue executar. Refazer:
#   cd /opt/coral && sudo rm -rf .venv
#   sudo env UV_PYTHON_INSTALL_DIR=/opt/uv-python uv sync --no-dev --frozen

sudo -u coral /opt/coral/.venv/bin/python -c "import fastapi; print('ok')"
ls -ld /var/lib/coral                 # dono tem de ser coral:coral
```

## 1. Pré-requisitos

- Um VPS com Debian 12 / Ubuntu 22.04+ e um nome DNS (ex.: `signage.<dominio>`) a apontar para o IP público do VPS.
- Portas 80 e 443 abertas (o Caddy precisa da 80 para obter o certificado).

```bash
sudo apt update
sudo apt install -y ffmpeg git caddy curl
ffprobe -version | head -1          # o upload valida os ficheiros com ffprobe
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
```

## 2. Código e ambiente Python

```bash
sudo useradd --system --home /var/lib/coral --shell /usr/sbin/nologin coral
sudo mkdir -p /opt/coral /var/lib/coral /etc/coral
sudo git clone <url-do-repositorio> /opt/coral
cd /opt/coral
# Se o SO não tiver Python ≥ 3.11 (ex.: Ubuntu 22.04), o uv descarrega um; fica em
# /opt/uv-python (e não em /root) para o utilizador coral o poder executar.
sudo env UV_PYTHON_INSTALL_DIR=/opt/uv-python uv sync --no-dev --frozen
sudo -u coral /opt/coral/.venv/bin/python -c "import fastapi; print('ok')"
sudo chown -R coral:coral /var/lib/coral
```

## 3. Configuração (`/etc/coral/dashboard.env`)

```bash
sudo cp /opt/coral/.env.example /etc/coral/dashboard.env
sudo chown root:coral /etc/coral/dashboard.env
sudo chmod 640 /etc/coral/dashboard.env
```

Editar `/etc/coral/dashboard.env`:

- `CORAL_DATA_DIR=/var/lib/coral`
- `CORAL_SECRET_KEY=` → gerar com `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
- `CORAL_ADMIN_PASSWORD_HASH=` → gerar com:
  ```bash
  cd /opt/coral && PYTHONPATH=dashboard:tools .venv/bin/python -m signagectl admin hash-password
  ```
  (copiar a linha impressa, já com plicas).
- `CORAL_COOKIE_SECURE=true` (obrigatório em produção).
- Perfil de vídeo (`CORAL_VIDEO_*`): valores iniciais da spec §4.5, a afinar depois dos testes no ecrã.

## 4. Serviço e Caddy

```bash
sudo cp /opt/coral/deploy/vps/coral-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now coral-dashboard
sudo systemctl status coral-dashboard

sudo cp /opt/coral/deploy/vps/Caddyfile /etc/caddy/Caddyfile
sudo sed -i 's/signage.example.com/<nome-dns>/' /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Abrir `https://<nome-dns>/login` e entrar com a password definida.

## 5. Criar o player

```bash
cd /opt/coral
sudo -u coral PYTHONPATH=dashboard:tools .venv/bin/python -m signagectl \
    --env-file /etc/coral/dashboard.env player add --label pombal --location "<local>"
```

O token aparece **uma única vez**: guardar já, vai para `/etc/signage/token` no Pi. Para revogar: o mesmo comando com `player revoke <id>` em vez de `player add ...`.

## 6. Atualizar o dashboard

```bash
cd /opt/coral && sudo git pull && sudo env UV_PYTHON_INSTALL_DIR=/opt/uv-python uv sync --no-dev --frozen
sudo systemctl restart coral-dashboard   # as migrações correm no arranque
```

## Notas

- Um único worker uvicorn (a recolha diária de media apagada corre no próprio processo).
- Backups, alertas e rate limit do login ficam para a fase 4.
