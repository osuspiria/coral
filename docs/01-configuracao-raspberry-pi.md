# Fase 1 — Configuração da Raspberry Pi

Objetivo desta fase: montar fisicamente a estação e deixar o SO pronto e
resiliente, para depois (fase seguinte) instalar o software do player + portal
de gestão com o Claude Code.

## 0. Hardware à mão

- Raspberry Pi 5 4GB (SC1111)
- Caixa JOY-IT RB-AlucaseP5-M2 (alumínio, ventilação ativa dupla) — desenhada
  precisamente para esta combinação Pi 5 + M.2 HAT, por isso a montagem deve
  encaixar como previsto no manual
- M.2 HAT para Raspberry Pi 5 (SC1166)
- SSD NVMe M.2 2242 256GB
- Fonte de alimentação oficial USB-C 45W
- Cabo micro-HDMI → HDMI
- microSD 32GB A2, já com Raspberry Pi OS 64-bit pré-instalado (fica como
  cartão de recuperação/fallback — o arranque principal vai ser pelo SSD)
- TV com entrada HDMI
- Hotspot Vodafone (com SSID/password à mão)
- Um teclado/rato USB e monitor (ou a própria TV) para a primeira configuração,
  ou um portátil na mesma rede para fazer tudo via SSH depois do primeiro boot

## 1. Montagem física (linhas gerais)

1. Sem energia ligada, encaixa o SSD NVMe no slot M.2 do HAT, fixando com o
   parafuso/espaçador incluído.
2. Monta o M.2 HAT sobre a Raspberry Pi 5 através do conector PCIe/FPC,
   conforme o manual do HAT — presta atenção ao sentido do cabo flat.
3. Encaixa o conjunto (Pi 5 + HAT + SSD) na base da caixa, alinhando os furos
   com os headers GPIO/USB/HDMI conforme o manual da JOY-IT RB-AlucaseP5-M2.
4. Aplica as almofadas térmicas fornecidas nos chips (SoC, RAM, controlador de
   alimentação) antes de colocar o dissipador — sem isto a ventilação ativa
   perde eficácia.
5. Liga o conector JST da(s) ventoinha(s) PWM ao header de ventoinha da Pi 5
   (não a um pino GPIO genérico).
6. Fecha a caixa e aperta os parafusos sem forçar (alumínio + rosca fina).
7. Insere o cartão microSD (fica como reserva — ver secção 2 sobre arrancar a
   partir do SSD).
8. Liga o cabo micro-HDMI à TV **antes** de dar energia.
9. Só no fim, liga a fonte de alimentação USB-C.

## 2. Escolha de SO e arranque pelo SSD — recomendação

Com o SSD NVMe disponível, faz sentido que a Pi arranque a partir dele em vez
do microSD — é mais rápido e mais resistente a escritas repetidas do que um
cartão SD (importante já que não há UPS e o sistema vai ter cortes de energia
não controlados).

Duas formas de lá chegar:

1. **Instalar de raiz no SSD** (recomendado, mais simples): com o SSD já
   montado no M.2 HAT e a Pi ligada, usa o **Raspberry Pi Imager** a partir de
   outro computador (ou a partir da própria Pi, se arrancares uma vez pelo
   microSD) e escolhe o SSD NVMe como destino. Escreve a mesma imagem
   **Raspberry Pi OS (64-bit)** que já vinha no cartão.
2. **Clonar o cartão para o SSD**: se preferires manter exatamente a
   configuração que já vem pré-instalada, arranca uma vez pelo microSD e usa
   a ferramenta **SD Card Copier** (vem no Raspberry Pi OS Desktop) para
   clonar para o SSD.

Depois de instalado/clonado no SSD, muda a ordem de arranque da Pi para
priorizar o SSD:

```
sudo rpi-eeprom-config --edit
```

Altera a linha `BOOT_ORDER` para `0xf416` (lê-se da direita para a esquerda:
SSD NVMe primeiro, microSD como reserva, USB por último). Se o M.2 HAT for o
modelo oficial (M.2 HAT+, referência SC1166), o PCIe ativa-se automaticamente
— não deverias precisar de tocar em `dtparam=pciex1` no
`/boot/firmware/config.txt`; só adiciona essa linha se o SSD não for
detetado.

Quanto a Desktop vs. Lite: o cartão trouxe **Raspberry Pi OS 64-bit** — para
este caso de uso (slideshow em ecrã fixo, depois com software próprio feito
via Claude Code) a recomendação é **manter esta imagem** (Desktop) em vez de
reinstalar com uma distro de digital signage dedicada (Screenly, Xibo, etc.),
que trazem um portal/CMS próprio que não precisas. Se preferires algo mais
leve e previsível (sem ambiente gráfico completo, boot mais rápido), a
alternativa é **Raspberry Pi OS Lite (64-bit)** com um compositor mínimo
lançado no arranque — dá mais trabalho de configurar agora. Diz-me se
preferires este caminho.

Este guia assume que ficas com a imagem Desktop, agora instalada no SSD.

## 3. Primeiro arranque

1. Liga o teclado/monitor (ou a TV) e a fonte de alimentação.
2. Segue o assistente inicial do Raspberry Pi OS: define idioma/teclado
   (Portugal), password de utilizador (não deixes a password por omitir), e
   liga-te já à rede do hotspot Vodafone quando pedir Wi-Fi.
3. Deixa correr as atualizações que o assistente propuser.
4. Depois de reiniciar, abre um terminal e confirma a versão:
   `cat /etc/os-release` e `uname -a`.

Se preferires configurar tudo sem monitor (headless), usa o **Raspberry Pi
Imager** num PC para reescrever o cartão e, nas opções avançadas (engrenagem),
define à partida hostname, SSH, utilizador/password e credenciais Wi-Fi do
hotspot — assim a Pi já arranca ligada à rede e acessível por SSH.

## 4. Configuração base (via `sudo raspi-config` ou terminal)

- **Hostname**: define algo identificável, ex. `coral-poc-01` (importante já
  a pensar em várias estações no futuro).
- **SSH**: ativa em `Interface Options → SSH` — vai ser a forma normal de
  entrar na Pi depois de instalada atrás da TV.
- **Wi-Fi (hotspot Vodafone)**: `System Options → Wireless LAN`, insere
  SSID/password do hotspot. Confirma que o hotspot está configurado para não
  desligar automaticamente por inatividade (muitos hotspots móveis fazem
  power-save agressivo).
- **Boot**: `System Options → Boot / Auto Login` — define arranque direto
  para desktop com autologin (vai ser preciso para o player arrancar sozinho
  sem alguém ter de fazer login).
- **Localização/timezone**: `Localisation Options` → Europe/Lisbon.

## 5. Resiliência sem UPS (importante)

Sem UPS, um corte de energia = desligar a Pi "a frio". Isto continua a ser um
risco mesmo a arrancar do SSD (mais resistente a desgaste por escritas do que
um cartão SD, mas um corte a meio de uma escrita pode corromper o sistema de
ficheiros na mesma). Duas camadas de proteção:

1. **Overlay filesystem (root read-only)** — em `raspi-config →
   Performance Options → Overlay File System`, ativa o overlay. Isto faz o
   sistema arrancar sempre a partir de uma imagem "congelada" em RAM;
   qualquer escrita durante a sessão desaparece no reboot, e um corte de
   energia a meio não corrompe o storage.
   - ⚠️ No Bookworm este overlay marca **todas** as partições montadas como
     read-only (exceto `/boot/firmware`) — não é só a raiz. Isto tem impacto
     direto no software da fase seguinte: onde vão viver as imagens/vídeos do
     slideshow e a configuração do player? Duas opções a decidir com o
     Claude Code nessa fase:
     - (a) o player descarrega sempre o conteúdo mais recente do portal a
       cada arranque/periodicamente e guarda só em RAM/tmpfs (simples, mas
       depende sempre de rede);
     - (b) criar uma partição extra dedicada, montada à parte e excluída do
       overlay, só para cache de conteúdo (mais robusto offline, mais setup).
   - Só ativa o overlay depois de teres a configuração de rede e SSH já
     estáveis — com overlay ligado, alterações feitas fora da partição de
     boot não persistem sem desativares o overlay primeiro.
2. **Watchdog de hardware** — a Pi 5 tem watchdog embutido; ativar
   (`dtparam=watchdog=on` em `/boot/firmware/config.txt` + systemd
   watchdog) garante reboot automático se o processo do player travar,
   sem precisar de intervenção manual num local remoto.

## 6. TV / HDMI

- Desativa a proteção de ecrã e o DPMS (poupança de energia do ecrã), senão a
  TV "apaga" a imagem ao fim de uns minutos: em `~/.config/lxsession/...` ou
  via `xset s off`, `xset -dpms`, `xset s noblank` no arranque da sessão
  gráfica (o Claude Code vai automatizar isto quando montar o player).
- Se a TV suportar **HDMI-CEC**, vale a pena confirmar que a Pi consegue ligar
  a TV automaticamente ao arrancar (útil para reinícios remotos sem alguém
  no local com o comando da TV).
- Confirma a resolução de saída forçada em `/boot/firmware/config.txt` se a
  deteção automática (EDID) da TV falhar (comum em TVs comerciais/públicas).

## 7. Acesso remoto para gestão futura

Como a ligação é via hotspot móvel, a Pi fica quase de certeza **atrás de
CGNAT** (sem IP público) — não vai ser possível aceder a ela de fora por SSH
direto/port-forward. Vale a pena já nesta fase instalar uma solução de VPN
"mesh" que a Pi inicia por fora (ligação de saída, funciona mesmo atrás de
CGNAT):

```
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Isto dá-te acesso SSH estável à Pi a partir de qualquer sítio, e é também o
caminho mais simples para o portal de gestão (fase seguinte) comunicar com
cada estação sem depender de IP público. Alternativas equivalentes:
WireGuard próprio, ou o player fazer sempre polling de saída a um VPS (sem
VPN, mas menos prático para debug remoto).

## 8. Checklist final da Fase 1

- [ ] Caixa montada com M.2 HAT + SSD
- [ ] SO instalado/clonado no SSD, BOOT_ORDER a priorizar o SSD (arranque
      confirmado sem depender do microSD)
- [ ] SO atualizado, hostname definido, timezone Europe/Lisbon
- [ ] Wi-Fi ligado ao hotspot Vodafone, com reconexão automática testada
- [ ] SSH ativo e acessível
- [ ] Autologin + arranque direto para desktop configurado
- [ ] Tailscale (ou equivalente) instalado e a funcionar
- [ ] Overlay filesystem + watchdog configurados (decidido onde fica o cache
      de conteúdo)
- [ ] Ecrã sem proteção/DPMS, imagem estável na TV
- [ ] Teste de corte de energia "a frio" (desligar a fonte sem shutdown) sem
      corromper o arranque seguinte

Quando isto estiver fechado, avançamos para a fase de software (player +
portal) com o Claude Code.
