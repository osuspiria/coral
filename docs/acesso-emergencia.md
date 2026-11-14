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

Se o fornecedor tiver firewall própria (além do ufw), abrir lá também UDP 51820.

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
