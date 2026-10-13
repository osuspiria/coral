# Instalação do dashboard (VPS) — Fase 1

Guia para pôr o dashboard a correr num VPS Debian/Ubuntu com Caddy à frente (HTTPS automático). Tudo o que está entre `<...>` tem de ser substituído.

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
sudo uv sync --no-dev --frozen          # cria /opt/coral/.venv com Python ≥ 3.11
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
cd /opt/coral && sudo git pull && sudo uv sync --no-dev --frozen
sudo systemctl restart coral-dashboard   # as migrações correm no arranque
```

## Notas

- Um único worker uvicorn (a recolha diária de media apagada corre no próprio processo).
- Backups, alertas e rate limit do login ficam para a fase 4.
