# Releases do player: chaves, assinatura e publicação

A partir da 0.3.0 o código do player atualiza-se sozinho (spec §6): o Pi descarrega a release, **verifica a assinatura minisign**, corre o selftest, troca de versão e, se a versão nova não confirmar um arranque saudável em ~3 min, volta sozinho à anterior.

A segurança assenta numa regra: **a chave privada nunca sai da máquina de desenvolvimento**. Nem o VPS nem o repositório a têm. Um VPS comprometido não consegue fazer um Pi instalar código, porque não consegue assinar.

## 1. Gerar o par de chaves (uma vez)

No WSL (onde o minisign foi compilado em `~/.local/bin`):

```bash
mkdir -p ~/.minisign && chmod 700 ~/.minisign
minisign -G -p ~/.minisign/coral-release.pub -s ~/.minisign/coral-release.key
# pede uma password: usar uma forte e guardá-la no gestor de passwords
```

- `coral-release.key` (privada, cifrada com a password): **fazer cópia de segurança offline** (pen USB guardada, gestor de passwords). Sem ela não há novas releases; quem a tiver (e a password) pode instalar código em todos os players.
- `coral-release.pub` (pública): copiar para o repositório em `deploy/pi/release-pubkey.pub` e fazer commit. Vai para cada Pi em `/etc/signage/release-pubkey.pub`.

## 2. Construir e assinar uma release

1. Atualizar `__version__` em `player/coral_player/__init__.py` (ex.: `0.3.1`), testes a passar, commit.
2. No WSL, na raiz do repositório:
   ```bash
   python3 tools/release.py 0.3.1 --key ~/.minisign/coral-release.key
   # pede a password da chave; produz dist/coral-player-0.3.1.tar.gz e .minisig
   ```
   O script recusa uma árvore git com alterações por commitar, uma versão diferente da do código e uma versão já construída. O *trusted comment* da assinatura é `coral-player 0.3.1`: o Pi recusa a release se pedir outra versão.

## 3. Publicar no dashboard

```bash
scp dist/coral-player-0.3.1.tar.gz* <admin>@<vps>:
# no VPS:
cd /opt/coral
sudo -u coral PYTHONPATH=dashboard:tools .venv/bin/python -m signagectl \
    --env-file /etc/coral/dashboard.env release add \
    ~/coral-player-0.3.1.tar.gz ~/coral-player-0.3.1.tar.gz.minisig
```

As releases são imutáveis: uma versão publicada não pode ser substituída (publica-se uma versão nova).

## 4. Distribuir: canary primeiro, depois stable

No dashboard, **Releases**:

1. Pôr o canal **canary** na versão nova. Os players canary (página do player → Channel) atualizam-se no heartbeat seguinte.
2. Confirmar na página de cada player canary: versão nova a correr, estado do update `idle`/`committed`, ecrã a funcionar.
3. Pôr o canal **stable** na mesma versão.

Um player pode ter a sua própria versão ("Own version"), que se sobrepõe ao canal (útil para testar num só ecrã).

Se uma versão falhar num player (rollback ou rejeitada), fica marcada como **Failed version** nesse player e não é pedida outra vez até a versão desejada mudar ou até carregar em "Try again".

## 5. Rodar a chave

Se a chave privada for comprometida (ou por rotina): gerar um par novo, instalar a pública nova em todos os Pis (manual, como na instalação) e só depois começar a assinar com a privada nova. Um Pi só aceita releases assinadas com a chave pública que tem.

## Release de teste partida (só para o teste de aceitação)

```bash
python3 tools/release.py 0.3.2 --key ~/.minisign/coral-release.key --test-broken
```

O agente desta release sai logo ao arrancar, por isso nunca confirma o update e o Pi volta à versão anterior sozinho. Não a pôr em produção (só num player de teste, como versão própria).
