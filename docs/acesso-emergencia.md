# Acesso de emergência (WireGuard)

Para o dia em que nem os comandos remotos, nem o update, nem o rollback resolvem (spec §6.4). Cada Pi mantém um túnel WireGuard **de saída** para o VPS (funciona atrás de CGNAT); o admin entra no Pi por SSH **através do VPS**. Fora deste caminho o Pi não tem portas abertas.

- Rede do túnel: VPS `10.77.0.1`, players `10.77.x.y` (atribuídos pelo `signagectl`, fixos).
- O Pi só fala com o VPS pelo túnel (`AllowedIPs = 10.77.0.1/32`). Keepalive de 25 s (≈6 MB/mês de dados móveis).
- A chave privada de cada Pi é gerada no próprio Pi e nunca sai de lá.

## 1. VPS (uma vez)

```bash
sudo apt install -y wireguard
sudo sh -c 'umask 077; wg genkey | tee /etc/wireguard/server.key | wg pubkey > /etc/wireguard/server.pub'
sudo cp /opt/coral/deploy/vps/wg0.conf.example /etc/wireguard/wg0.conf
sudo sed -i "s#<contents of /etc/wireguard/server.key>#$(sudo cat /etc/wireguard/server.key)#" /etc/wireguard/wg0.conf
sudo chmod 600 /etc/wireguard/wg0.conf
sudo systemctl enable --now wg-quick@wg0
sudo ufw allow 51820/udp
sudo cat /etc/wireguard/server.pub
```

No `/etc/coral/dashboard.env`, acrescentar:

```
CORAL_WG_ENDPOINT=<nome-dns-do-vps>:51820
CORAL_WG_SERVER_PUBKEY=<conteúdo de server.pub>
```

Se o fornecedor tiver firewall própria (além do ufw), abrir lá também UDP 51820. No Hetzner é a **Cloud Firewall** da consola — é a causa mais comum de "não há handshake".

## 2. Cada Pi

1. No Pi, o `install.sh` já gerou a chave e mostrou a pública (`WireGuard public key of this Pi: …`). Para a voltar a ver: `sudo wg pubkey < /etc/wireguard/signage.key`.
2. No VPS:
   ```bash
   cd /opt/coral
   sudo -u coral PYTHONPATH=dashboard:tools .venv/bin/python -m signagectl \
       --env-file /etc/coral/dashboard.env player wg-add <id> --pubkey <chave-do-pi>
   ```
   O comando imprime **dois passos**: os comandos `sudo wg set …` / `sudo wg-quick save wg0` para correr no VPS, e o comando `install.sh --wireguard-only …` para correr no Pi. Correr ambos.
3. Confirmar: no VPS, `sudo wg show` mostra o peer com `latest handshake` recente; `ping 10.77.x.y` responde.

O `wg0.conf` gerado no Pi usa `MTU = 1280` (rede móvel) e o `install.sh` instala um override do `wg-quick@wg0` (`/etc/systemd/system/wg-quick@wg0.service.d/10-signage.conf`): arranca depois de `network-online.target` e volta a tentar a cada 30 s se falhar (por exemplo, se o hotspot ainda não tiver rede no arranque).

## 3. Entrar num Pi

```bash
ssh -J <admin>@<vps> <utilizador-do-pi>@10.77.x.y
```

A tua chave SSH tem de estar no `~/.ssh/authorized_keys` do utilizador do Pi.

**Opcional — SSH só pelo túnel:** depois de confirmar que o acesso pelo túnel funciona, no Pi:

```bash
sudo /opt/signage/current/deploy/pi/install.sh --wireguard-only --ssh-only-wireguard \
    --wg-address 10.77.x.y --wg-endpoint <vps>:51820 --wg-server-pubkey <chave-do-vps>
```

Passa a aceitar SSH só por chave e só na interface `wg0` (o SSH pela rede local deixa de funcionar). O script recusa se não houver nenhuma `authorized_keys`, para não te deixar trancado.

## 4. Revogar

`signagectl player revoke <id>` (ou `player wg-remove <id>`) imprime os comandos `wg set … remove` para correr no VPS.

## 5. Migrar o piloto (rede manual 10.8 → 10.77)

O piloto de Pombal e o VPS foram configurados à mão antes desta implementação (`docs/acesso-remoto-pi-tailscale-wireguard.md`): `wg0` em `10.8.0.0/24`, com o portátil como peer e encaminhamento entre peers. Esse `wg0` ocupa o mesmo nome e a mesma porta que o desenho de referência, por isso a secção 1 não se pode correr por cima dele.

Ideia: a rede manual passa para **`wg1` (UDP 51821)**, a nova `wg0` (10.77) monta-se ao lado e a antiga só se retira depois de a nova estar testada. Durante a troca, o **Tailscale** é o acesso de segurança ao Pi: confirmar `ssh <utilizador>@<nome-do-pi>` pelo Tailscale **antes** de começar.

1. **VPS — mudar a rede manual para `wg1`:**
   ```bash
   sudo wg-quick down wg0 && sudo systemctl disable wg-quick@wg0
   sudo mv /etc/wireguard/wg0.conf /etc/wireguard/wg1.conf
   sudo mv /etc/wireguard/server.key /etc/wireguard/legacy.key
   sudo mv /etc/wireguard/server.pub /etc/wireguard/legacy.pub
   sudoedit /etc/wireguard/wg1.conf
   #   ListenPort = 51821
   #   PostUp = wg set %i private-key /etc/wireguard/legacy.key
   #   iptables ... -i wg1 -o wg1 ...   (nas linhas PostUp e PostDown)
   sudo ufw allow 51821/udp          # e na Cloud Firewall do Hetzner
   sudo systemctl enable --now wg-quick@wg1
   ```
2. **Pi (pelo Tailscale):**
   ```bash
   sudo wg-quick down wg0 && sudo systemctl disable wg-quick@wg0
   sudo mv /etc/wireguard/wg0.conf /etc/wireguard/wg1.conf
   sudoedit /etc/wireguard/wg1.conf       # Endpoint = <IP do Hetzner>:51821
   # se existir override em /etc/systemd/system/wg-quick@wg0.service.d/, copiar para wg-quick@wg1.service.d/
   sudo systemctl enable --now wg-quick@wg1
   sudo wg show wg1                        # handshake recente; ping 10.8.0.1
   ```
   No portátil, mudar o `Endpoint` do `wg0` manual para a porta 51821 (só se ainda se quiser esse caminho durante a migração).
3. **VPS — nova `wg0` (10.77):** secção 1 deste documento (a `server.key` já está livre) e as variáveis `CORAL_WG_*` no `dashboard.env`; reiniciar o `coral-dashboard`.
4. **Pi — registar e configurar a nova `wg0`:** a chave do Pi para este desenho é `/etc/wireguard/signage.key` (diferente da `pi.key` manual). Se o `install.sh` já correu, a pública vê-se com `sudo wg pubkey < /etc/wireguard/signage.key`; se não, `sudo /opt/signage/current/deploy/pi/install.sh --wireguard-only` gera-a e mostra-a. Depois, secção 2 (`signagectl player wg-add` no VPS + `install.sh --wireguard-only --wg-address …` no Pi).
5. **Testar**, com o Tailscale desligado no portátil (`tailscale down`): `ssh -J <admin>@<vps> <utilizador>@10.77.x.y`. Fazer também o teste de corte de corrente (`docs/cenarios-falha.md`) e confirmar que a `wg0` volta sozinha.
6. **Retirar a rede manual** (só depois do passo 5):
   ```bash
   # Pi e portátil
   sudo systemctl disable --now wg-quick@wg1 && sudo rm /etc/wireguard/wg1.conf /etc/wireguard/pi.key /etc/wireguard/pi.pub
   # VPS
   sudo systemctl disable --now wg-quick@wg1 && sudo rm /etc/wireguard/wg1.conf /etc/wireguard/legacy.*
   sudo ufw delete allow 51821/udp   # e fechar na Cloud Firewall
   ```
   (No portátil os ficheiros são o `wg0.conf` manual e `portatil.key`/`.pub`.) O desenho de referência não precisa de `net.ipv4.ip_forward` no VPS: só remover `/etc/sysctl.d/99-wg.conf` se nada mais o usar (o Docker, por exemplo, precisa).

A partir daí o portátil **deixa de ser peer WireGuard**: o acesso é sempre `ssh -J` pelo VPS.
