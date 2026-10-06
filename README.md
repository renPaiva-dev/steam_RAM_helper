# steamlite

Faz o Steam usar uma quantidade ridiculamente baixa de RAM, no **Linux e no Windows**,
sem root/admin e sem dependências (só Python 3).

## Como funciona

O grosso da RAM do Steam é o `steamwebhelper` (o Chromium que desenha a interface).
O `steamlite` abre o Steam com flags enxutas do CEF (`-silent -nofriendsui -nochatui
-cef-disable-gpu ...`) e, **enquanto um jogo está aberto**, espreme o Steam para fora da RAM.
Jogos nunca são limitados. Quando o jogo fecha, tudo volta ao normal.

| | Linux | Windows |
|---|---|---|
| Isolamento | scope do systemd com cgroups `core` / `web` / `games` | por processo (`steam.exe`, `steamwebhelper.exe`) |
| Detectar jogo | processos filhos do `reaper` do Steam | `RunningAppID` no registro + filhos do `steam.exe` |
| Tirar da RAM | `memory.reclaim` → swap/zram | corte do working set → standby list / pagefile |
| Limite da UI | `memory.high` | teto rígido de working set |
| Prioridade | — | prioridade de memória VERY_LOW, CPU IDLE, EcoQoS |
| `--freeze` | `cgroup.freeze` | `NtSuspendProcess` |

## Instalação e uso

**Linux**
```bash
./install.sh                 # opcional: comando `steamlite` + atalho "Steam (lite)" no menu
steamlite launch --freeze
```

**Windows** (precisa do Python 3 do python.org, com "Add to PATH" marcado)
```powershell
powershell -ExecutionPolicy Bypass -File install.ps1   # atalho "Steam (lite)" no Menu Iniciar
steamlite launch --freeze                              # ou pelo terminal
```
Pelo atalho não aparece janela; o log fica em `%LOCALAPPDATA%\steamlite\steamlite.log`.

**Comandos**
```bash
steamlite launch [opções] [-- args extras do Steam]
steamlite status     # quanto o Steam está usando agora
steamlite squeeze    # empurra o Steam pra fora da RAM agora (ex.: Steam na bandeja)
steamlite thaw       # desfaz congelamento/limites (se o steamlite foi morto no meio)
```

Feche o Steam antes do `launch`; as flags só valem quando o Steam é iniciado. (No Windows,
se ele já estiver aberto, o steamlite governa mesmo assim, só sem as flags.)

| Opção | Padrão | O que faz |
|---|---|---|
| `--profile safe/aggressive` | safe | `aggressive` adiciona `-cef-single-process` etc. Menos processos, pode ser instável |
| `--ui-limit` | max | Limite da UI fora do jogo. Ex.: `400M`. Muito baixo deixa a UI lenta |
| `--game-web-limit` | 32M | Limite da UI durante o jogo |
| `--game-core-limit` | max | Limite do cliente durante o jogo (`150M` se não precisa de downloads) |
| `--freeze` | off | Congela a UI durante o jogo. **Quebra o overlay Shift+Tab** e notificações |
| `--no-reclaim-core` | off | Não tira o cliente da RAM quando o jogo abre |
| `--steam` | auto | Caminho do executável do Steam |

## Dicas e limitações

- **Linux:** a memória vai para o swap; **zram** deixa isso bem mais rápido
  (`sudo apt install systemd-zram-generator`). Memória alocada antes de um processo ser
  classificado continua contada no `core`, por isso o `core` também passa por reclaim.
- **Windows:** a memória cortada vai primeiro para a *standby list*: o Gerenciador de
  Tarefas mostra o Steam usando quase nada e o jogo pode usar essa RAM; se o Steam precisar
  dela de volta, ela retorna sem custo de disco enquanto não for reaproveitada.
- Ao voltar para a UI depois de um jogo, a primeira interação pode demorar um instante.
- Se o steamlite for morto à força com a UI congelada, rode `steamlite thaw`.
