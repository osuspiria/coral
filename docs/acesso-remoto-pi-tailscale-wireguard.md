# Acesso remoto ao Raspberry Pi 5 — Tailscale + WireGuard

Documentação da configuração de rede e acesso remoto ao Raspberry Pi 5 que fica
instalado remotamente (potencialmente 1+ anos sem acesso físico), ligado à
internet através de um hotspot Vodafone por USB.

> **Estado:** registo da configuração **manual do piloto** (Pombal), feita antes
> da implementação da fase 4. O desenho de referência do acesso de emergência é
> `docs/acesso-emergencia.md` (WireGuard `10.77.0.0/16`, spec §6.4). A rede
> WireGuard manual descrita na secção 4 (`10.8.0.0/24`) está a ser substituída —
> migração em `docs/acesso-emergencia.md` §5. O Tailscale fica só no piloto.

> **Nota:** este ficheiro não contém chaves privadas. Valores entre `<...>` são
> placeholders — os valores reais estão nas máquinas, em `/etc/wireguard/`.

---

## 1. Visão geral

### Problema
O hotspot móvel Vodafone usa **CGNAT** — não há IP público próprio nem
possibilidade de abrir portas. Não é possível fazer SSH diretamente para o Pi
a partir de fora. A solução é o Pi iniciar ligações de saída para uma VPN,
e o acesso faz-se por dentro dessa VPN.

### Arquitetura (dois caminhos independentes)

| Caminho | Função | Depende de |
|---|---|---|
| **Tailscale** | Acesso principal | Serviço Tailscale (coordenação/relays) + conta de login |
| **WireGuard → Hetzner** | Acesso de reserva | Servidor VPS Hetzner próprio |

Só se perde o acesso se **ambos** falharem ao mesmo tempo.

```
 [Portátil Fedora] ──Tailscale──────────────────────┐
        │                                            │
        └──WireGuard──> [Hetzner VPS 10.8.0.1] <──WireGuard── [Raspberry Pi 5]
                                                     │            │
                                                     └─Tailscale──┘
                                                          │
                                                  USB (cdc_ether, eth1)
                                                          │
                                                 [Hotspot Vodafone] ── rede móvel (CGNAT)
```

### Endereçamento WireGuard

| Máquina | IP WireGuard | Notas |
|---|---|---|
| Servidor Hetzner | `10.8.0.1/24` | Hub, escuta em UDP `51820` |
| Portátil (Fedora) | `10.8.0.2/32` | Liga só quando necessário |
| Raspberry Pi 5 | `10.8.0.10/32` | Sempre ligado |

Futuros aparelhos remotos: `10.8.0.11`, `10.8.0.12`, …

---

## 2. Ligação à internet do Pi — hotspot por USB

- O hotspot ligado por USB aparece como placa de rede; **não é preciso Wi-Fi**.
- Driver: `cdc_ether` (já incluído no kernel do Raspberry Pi OS).
- Interface no Pi: **`eth1`** (a `eth0` é a Ethernet da placa).
- Gateway do hotspot: `192.168.0.1`; IP do Pi na rede do hotspot: `192.168.0.x` (DHCP).
- Gerido pelo NetworkManager (DHCP automático).

### Verificação
```bash
ip route get 1.1.1.1                 # deve mostrar: dev eth1
ethtool -i eth1 | grep driver        # driver: cdc_ether
curl -s ifconfig.me; echo            # IP público da Vodafone móvel (não o de casa)
```

### Garantir que usa o hotspot e não o Wi-Fi de casa
```bash
nmcli -f NAME,DEVICE connection show

# prioridade à ligação USB (métrica mais baixa = preferida)
sudo nmcli connection modify "<ligação da eth1>" ipv4.route-metric 50 \
  connection.autoconnect yes connection.autoconnect-priority 100

# Wi-Fi de casa sem ligação automática (costuma chamar-se "preconfigured")
sudo nmcli connection modify "<Wi-Fi de casa>" connection.autoconnect no
```

Opcional — desligar o Wi-Fi no hardware (`/boot/firmware/config.txt`):
```ini
dtoverlay=disable-wifi
```

### Possíveis problemas
- **Hotspot aparece como CD-ROM e não como rede** → `sudo apt install usb-modeswitch`.
- **Corrente USB**: o Pi 5 limita as portas USB a ~600 mA sem a fonte oficial de 5 A.
  O hotspot tem bateria própria, por isso normalmente não é problema.
- **Cabo**: tem de ser cabo de dados, não só de carga.

---

## 3. Tailscale (acesso principal)

### Instalação no Pi
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
```
`--ssh` usa o Tailscale SSH (autenticação pela conta Tailscale). Sem esta flag,
usa-se o `sshd` normal através do IP `100.x.x.x`.

Acesso: `ssh oleg@<nome-do-pi>` (MagicDNS) ou `ssh oleg@100.x.x.x`.

### Arranque garantido e reinício automático
```bash
sudo systemctl enable --now tailscaled ssh
sudo systemctl edit tailscaled
```
Override:
```ini
[Service]
Restart=always
RestartSec=10
```
```bash
sudo systemctl daemon-reload
sudo systemctl restart tailscaled
tailscale status
```
O estado/autenticação fica em `/var/lib/tailscale` — sobrevive a reboots.

### Expiração da chave (crítico para aparelhos remotos)
Por defeito a chave de um nó expira (~180 dias) e o aparelho perde acesso.

- Consola: https://login.tailscale.com/admin/machines
- Na linha do Pi → menu **⋯** → **Disable key expiry**.
- Manter a expiração **ativa** no portátil e no telemóvel.

### Para futuros aparelhos
- Registar com **tag** (ex.: `tag:remote`) através de uma auth key — nós com tag
  ficam sem expiração da chave por defeito.
- Usar ACLs para que só o portátil chegue aos aparelhos remotos (e estes não se vejam entre si).
- `tailscale set --auto-update` para atualizações automáticas (avaliar o risco).
- Alternativa futura sem depender da empresa: **Headscale** (servidor de controlo
  self-hosted, compatível com os clientes oficiais) no Hetzner.

---

## 4. WireGuard (acesso de reserva via Hetzner)

> **Substituída a 14/11/2026.** Na verificação antes da migração viu-se que o
> túnel do Pi nunca tinha funcionado (`wg-quick@wg0` desativado, sem handshake no
> servidor). O piloto está agora em `10.77.0.2` (`docs/acesso-emergencia.md` §5);
> os ficheiros antigos ficaram em `/etc/wireguard/legacy-10.8/` até à limpeza.
>
> Esta rede (`10.8.0.0/24`, portátil como peer, encaminhamento
> entre peers no servidor) não é o desenho de referência: lá cada Pi só fala com
> o VPS e o acesso é por `ssh -J`. Não usar para novos aparelhos; migração do
> piloto em `docs/acesso-emergencia.md` §5. Fica aqui como registo.

### Regras das chaves
| Ficheiro | Campo | Chave |
|---|---|---|
| `wg0.conf` de uma máquina, `[Interface]` | `PrivateKey` | **privada dessa máquina** |
| `wg0.conf` de uma máquina, `[Peer]` | `PublicKey` | **pública do outro lado** |

A chave privada nunca sai da máquina a que pertence. Formato: 44 caracteres base64 terminados em `=`.

Obter chaves públicas:
```bash
sudo wg show wg0 public-key                                            # com a interface ativa
sudo grep PrivateKey /etc/wireguard/wg0.conf | awk '{print $3}' | wg pubkey  # a partir do .conf
sudo cat /etc/wireguard/<nome>.pub                                     # se foi guardada em ficheiro
```

Em vez de colar a chave privada no `.conf`, pode ler-se do ficheiro:
```ini
PostUp = wg set %i private-key /etc/wireguard/<nome>.key
```
(**Não** funciona se a configuração for importada para o NetworkManager — aí é preciso `PrivateKey = ...`.)

### 4.1 Servidor Hetzner

Verificar se já existe WireGuard:
```bash
which wg wg-quick
sudo wg show
ls /etc/wireguard/
systemctl list-units 'wg-quick@*' --all
docker ps 2>/dev/null | grep -iE 'wg|wireguard'   # ex.: wg-easy
```

Instalação (Debian/Ubuntu):
```bash
sudo apt update && sudo apt install wireguard
wg genkey | sudo tee /etc/wireguard/server.key | wg pubkey | sudo tee /etc/wireguard/server.pub
sudo chmod 600 /etc/wireguard/server.key
```

`/etc/wireguard/wg0.conf`:
```ini
[Interface]
Address = 10.8.0.1/24
ListenPort = 51820
PostUp = wg set %i private-key /etc/wireguard/server.key
PostUp = iptables -A FORWARD -i wg0 -o wg0 -j ACCEPT
PostDown = iptables -D FORWARD -i wg0 -o wg0 -j ACCEPT

[Peer]
# raspberry-pi-remoto
PublicKey = <conteúdo de /etc/wireguard/pi.pub no Pi>
AllowedIPs = 10.8.0.10/32

[Peer]
# portatil
PublicKey = <conteúdo de /etc/wireguard/portatil.pub no portátil>
AllowedIPs = 10.8.0.2/32
```

Encaminhamento entre peers, firewall e arranque:
```bash
echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/99-wg.conf
sudo sysctl --system
sudo ufw allow 51820/udp            # se usar ufw
sudo systemctl enable --now wg-quick@wg0
```

> **Hetzner Cloud Firewall:** se estiver ativa na consola Hetzner, abrir também
> **UDP 51820** lá. É a causa mais comum de "não há handshake".

Aplicar alterações de peers sem cortar ligações:
```bash
sudo wg syncconf wg0 <(wg-quick strip wg0)
```

### 4.2 Raspberry Pi

```bash
sudo apt install wireguard
wg genkey | sudo tee /etc/wireguard/pi.key | wg pubkey | sudo tee /etc/wireguard/pi.pub
sudo chmod 600 /etc/wireguard/pi.key
```

`/etc/wireguard/wg0.conf`:
```ini
[Interface]
Address = 10.8.0.10/24
PostUp = wg set %i private-key /etc/wireguard/pi.key
MTU = 1280

[Peer]
PublicKey = <chave pública do servidor Hetzner>
Endpoint = <IP público do Hetzner>:51820
AllowedIPs = 10.8.0.0/24
PersistentKeepalive = 25
```

Decisões importantes:
- **`AllowedIPs = 10.8.0.0/24`** (não `0.0.0.0/0`) — só o tráfego da VPN vai pelo
  túnel; a internet continua a sair pelo hotspot e não há conflito com o Tailscale.
- **`Endpoint` com IP, não domínio** — evita falha no arranque se o DNS ainda não estiver pronto.
- **`PersistentKeepalive = 25`** — mantém o túnel aberto através do CGNAT.
- **`MTU = 1280`** — evita problemas de fragmentação na rede móvel.

Serviço com arranque e reinício:
```bash
sudo systemctl enable --now wg-quick@wg0
sudo systemctl edit wg-quick@wg0
```
Override:
```ini
[Unit]
After=network-online.target
Wants=network-online.target

[Service]
Restart=on-failure
RestartSec=30
```

### 4.3 Portátil (Fedora)

```bash
sudo dnf install wireguard-tools
sudo mkdir -p /etc/wireguard
wg genkey | sudo tee /etc/wireguard/portatil.key | wg pubkey | sudo tee /etc/wireguard/portatil.pub
sudo chmod 600 /etc/wireguard/portatil.key
```

`/etc/wireguard/wg0.conf`:
```ini
[Interface]
Address = 10.8.0.2/24
PostUp = wg set %i private-key /etc/wireguard/portatil.key

[Peer]
PublicKey = <chave pública do servidor Hetzner>
Endpoint = <IP público do Hetzner>:51820
AllowedIPs = 10.8.0.0/24
PersistentKeepalive = 25
```

Uso (só quando o Tailscale falhar):
```bash
sudo wg-quick up wg0
ssh oleg@10.8.0.10
sudo wg-quick down wg0
```

Opcional — gerir pelo menu de rede do GNOME (pôr `PrivateKey = ...` no ficheiro antes de importar):
```bash
sudo nmcli connection import type wireguard file /etc/wireguard/wg0.conf
nmcli connection modify wg0 connection.autoconnect no
```

### 4.4 Testes
```bash
# no Pi
sudo wg show            # "latest handshake" recente
ping 10.8.0.1

# no servidor
sudo wg show            # endpoint do Pi = IP da Vodafone móvel

# no portátil, com o Tailscale desligado para provar o caminho de reserva
tailscale down
sudo wg-quick up wg0
ssh oleg@10.8.0.10
sudo wg-quick down wg0
tailscale up
```

---

## 5. Watchdogs (recuperação sem acesso físico)

### Watchdog de hardware — sistema pendurado
> Já tratado pelo `install.sh` (drop-in `/etc/systemd/system.conf.d/signage-watchdog.conf`,
> com o timeout ajustado ao máximo do hardware). O drop-in sobrepõe-se ao
> `system.conf`, por isso a edição manual abaixo é redundante.

`/etc/systemd/system.conf`:
```ini
RuntimeWatchdogSec=15
RebootWatchdogSec=2min
```
```bash
sudo systemctl daemon-reexec
```

### Watchdog de rede — reboot se não houver rede há 30 min
`/usr/local/bin/net-watchdog.sh`:
```bash
#!/bin/bash
# reinicia se não houver rede há mais de 30 min
STAMP=/run/net-ok
if ping -c2 -W5 1.1.1.1 >/dev/null || ping -c2 -W5 10.8.0.1 >/dev/null; then
  touch "$STAMP"
elif [ -f "$STAMP" ] && [ $(( $(date +%s) - $(stat -c %Y "$STAMP") )) -gt 1800 ]; then
  logger "net-watchdog: sem rede há 30 min, a reiniciar"
  systemctl reboot
fi
[ -f "$STAMP" ] || touch "$STAMP"
```
```bash
sudo chmod +x /usr/local/bin/net-watchdog.sh
echo '*/5 * * * * root /usr/local/bin/net-watchdog.sh' | sudo tee /etc/cron.d/net-watchdog
```
O reboot também recupera casos em que o hotspot USB "adormece".

> **Só no piloto, fora da spec** (evolução em §1.2). Atenção: o carimbo fica em
> `/run`, que se perde no reboot; se o hotspot estiver em baixo durante horas, o
> Pi reinicia a cada ~30–35 min e cada reboot interrompe o ecrã. Uma versão para
> a frota precisa de backoff entre reboots.

---

## 6. Checklist antes de deixar o Pi no local remoto

- [ ] `ip route get 1.1.1.1` → `dev eth1` (hotspot)
- [ ] Wi-Fi de casa com `autoconnect no` (ou apagado / `disable-wifi`)
- [ ] `tailscaled` e `ssh` com `enable`; override `Restart=always`
- [ ] Key expiry do Pi desativada na consola Tailscale
- [ ] `wg-quick@wg0` com `enable`; override `Restart=on-failure`
- [ ] `sudo wg show` no Pi mostra handshake recente
- [ ] Watchdog de hardware e `net-watchdog` ativos
- [ ] **Teste de corte de corrente:** desligar da tomada, voltar a ligar e confirmar:
  - [ ] rota por defeito continua em `eth1`
  - [ ] SSH pelo Tailscale funciona
  - [ ] SSH por `10.8.0.10` (WireGuard) funciona com o Tailscale desligado no portátil

---

## 7. Adicionar um novo aparelho remoto

Não seguir o procedimento manual: usar `docs/instalacao-pi.md` (`install.sh`) e
`docs/acesso-emergencia.md` §2 (`signagectl player wg-add`), que atribuem o IP
`10.77.x.y`, geram a configuração (com `MTU = 1280` e override systemd) e o
watchdog de hardware. O Tailscale não faz parte da instalação padrão (spec §6.4).
