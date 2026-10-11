# monitorctl — documento de projeto

Registro completo do que o monitorctl é, como chegou aqui e **por que** cada
decisão foi tomada. O `README.md` é o manual de uso; este documento é a memória
de engenharia: contexto, alternativas descartadas, evidências e consequências.

Estado descrito: **versão 0.2.0, 2026-10-02.**

---

## Sumário

1. [Visão geral](#1-visão-geral)
2. [Histórico](#2-histórico)
3. [O problema central: o TCL que esquece e mente](#3-o-problema-central-o-tcl-que-esquece-e-mente)
4. [Arquitetura](#4-arquitetura)
5. [Funcionalidades](#5-funcionalidades)
6. [Decisões](#6-decisões)
7. [Medições](#7-medições)
8. [Bugs encontrados e lições](#8-bugs-encontrados-e-lições)
9. [Qualidade e testes](#9-qualidade-e-testes)
10. [Arquivos e estado em disco](#10-arquivos-e-estado-em-disco)
11. [Limitações, pendências e próximos passos](#11-limitações-pendências-e-próximos-passos)
12. [Glossário](#12-glossário)

---

## 1. Visão geral

O monitorctl controla monitores pelo **DDC/CI** — o protocolo pelo qual o
computador lê e escreve, pelo cabo de vídeo, as mesmas configurações do menu
físico do monitor (brilho, contraste, ganhos RGB, entrada, volume…). Ele
complementa isso com ajustes feitos na **GPU** (saturação via NVIDIA, curva de
tom via gamma ramp) quando o monitor não oferece o controle.

Hoje é:

- **Um app de bandeja** (`monitorctl.exe`): fica residente gastando 1–3 MiB,
  mantém as configurações salvas aplicadas e abre uma janela com todos os
  controles quando pedido.
- **Uma CLI** (`monitorctl-cli.exe`): diagnóstico, scripts, leitura e escrita
  pontual.
- **Uma pasta portátil**: nada é instalado; a pasta pode ser copiada para
  qualquer lugar.

Escopo de hardware validado: TCL 25G64 e ASUS VS248, RTX 4070, Windows 10.

Números: ~9.300 linhas de Python no pacote, 266 testes, zero dependências de
runtime além da biblioteca padrão.

---

## 2. Histórico

| Quando | Marco | O que motivou |
|---|---|---|
| 2026-09-19 | **0.1 — PoC** com interface web local e CLI | Validar se valia fazer um software DDC/CI próprio. Pedido explícito de rigor de indústria mesmo sendo uso pessoal |
| 2026-09-20 | Camada GPU (NVAPI + gamma ramp) | O TCL não implementa nenhum código de saturação |
| 2026-09-20 | Save/Restore com snapshot persistente | Calibrar é uma sequência de passos irreversíveis; o reset de fábrica do monitor é grosso demais e bloqueado |
| 2026-09-30 | Relato: "salvo, reinicio, perde as configs" | Investigação revelou o defeito do TCL (seção 3) |
| 2026-09-30 | Identidade por EDID | O Windows trocava `DISPLAY1`/`DISPLAY2`; o restore teria aplicado os valores de um monitor no outro |
| 2026-09-30 | Quirk de controles voláteis + tarefa agendada de restore no logon | Reaplicar o que o TCL esquece; o usuário quis primeiro algo sem processo residente |
| 2026-10-02 | **0.2.0 — app de bandeja** | O usuário pediu um app próprio, residente, com janela, `.exe`, mínimo de RAM e re-set periódico configurável. Interface web removida |

---

## 3. O problema central: o TCL que esquece e mente

### 3.1 Sintoma

O usuário salvava as configurações, desligava e ligava o **monitor** (não o PC)
e a imagem voltava ao padrão de fábrica nos ganhos e offsets RGB. Ao abrir o app,
porém, os sliders mostravam os valores certos.

### 3.2 Investigação, passo a passo

| Experimento | Resultado | Conclusão |
|---|---|---|
| Ler os controles depois de religar | `0x16/0x18/0x1A` e `0x6C/0x6E/0x70` continuavam com os valores salvos | **A leitura mente**: reporta valores que o painel não usa |
| Restore "só o que mudou" | Não escreveu nada | Toda estratégia baseada em comparar com a leitura é cega para esse defeito |
| Reescrever `red_gain` com o mesmo valor | Tela não mudou | **Escrita de valor igual ao reportado é ignorada** |
| Diminuir até ~60 e voltar ao valor certo | Tela corrigida | Uma escrita que *muda* o valor funciona |
| `SaveCurrentSettings` (VCP `0xB0`) com os valores certos | Retornou sucesso; após religar, voltou ao padrão | O firmware aceita e ignora o comando |
| Abrir e fechar o menu OSD após ajustar | Voltou ao padrão | Não há caminho de persistência pelo menu |

### 3.3 Pesquisa externa

- **ddcutil** (referência do ecossistema): o comando `scs` (= `0xB0`) é
  documentado como *"most monitors do not implement this command"*; para manter
  valores, ele oferece `dumpvcp`/`loadvcp`, isto é, salvar e reaplicar.
- **PowerToys Power Display** e **Twinkle Tray**: reaplicam por software ao
  iniciar e ao acordar, residentes na bandeja.

Conclusão: ninguém depende de `0xB0`; o padrão da indústria é reaplicar. Isso
fundamenta as decisões D7–D11 e D12.

### 3.4 O que **não** foi testado

Se brilho, contraste e preset do TCL também se perdem ao religar. Só os seis
controles RGB estão marcados como voláteis.

---

## 4. Arquitetura

### 4.1 Camadas

```
 app/ (tray, window, keeper, worker, settings, win32)     cli.py
            │                                               │
            └──────────────┬────────────────────────────────┘
                           ▼
              ddc/service.py  MonitorService
              lock · pacing · retry · orçamento · verificação
              snapshot · upkeep · ping · identidade
                  │                │              │
        ddc/policy.py        ddc/buslock.py    gpu/controller.py
        allowlist, clamp     mutex entre        NVAPI · gamma ramp
        token bucket         processos
                  │
        ddc/backend.py (interface)  ──►  ddc/windows.py (dxva2 + EDID)
                  │
         DDC/CI → I²C @ 0x37 → monitor
```

| Módulo | Responsabilidade |
|---|---|
| `ddc/vcp.py` | Registro fechado dos 43 controles DDC: código, tipo, categoria, valores, tooltip |
| `ddc/capabilities.py` | Parser tolerante da capabilities string (inclusive a malformada do TCL) |
| `ddc/policy.py` | Allowlist, códigos bloqueados, clamp, orçamento de escrita |
| `ddc/backend.py`, `ddc/windows.py` | Transporte; no Windows, `dxva2.dll` + leitura do EDID pelo registro |
| `ddc/edid.py` | Parser do bloco EDID base → identidade `hardware_id` |
| `ddc/quirks.py` | Defeitos por modelo que sondagem não revela (controles voláteis) |
| `ddc/snapshot.py` | Snapshot persistente, validação de entrada não confiável |
| `ddc/service.py` | Orquestração de tudo acima |
| `ddc/buslock.py` | Mutex nomeado do barramento, entre processos |
| `gpu/` | Ajustes na GPU: NVAPI (vibrance, hue) e gamma ramp (brilho, contraste, gama) |
| `fileio.py` | Escrita atômica e diretório de estado |
| `autostart.py` | Chave `Run` do usuário |
| `cli.py` | Linha de comando |
| `app/` | O app de bandeja (detalhado em 4.2) |

### 4.2 O app de bandeja

```
 thread principal (Win32 + Tk)                    thread "monitorctl-bus"
 ┌───────────────────────────────┐   submit()    ┌─────────────────────────┐
 │ MessageWindow (oculta)        │ ────────────► │ Worker                  │
 │  avisos do Windows ──► post_event()            │  fila de jobs           │
 │ TrayIcon + menu               │               │  eventos agrupados (3 s)│
 │ MainWindow (Tk, sob demanda)  │ ◄──────────── │  verificação periódica  │
 └───────────────────────────────┘ PostMessageW  │  Keeper → MonitorService│
              drain() roda os callbacks           └─────────────────────────┘
```

- **Um laço de mensagens, dois donos.** Antes de a janela existir, a thread
  principal roda `GetMessage`. Ao abrir a janela pela primeira vez, esse laço
  termina e a thread passa a rodar `Tk.mainloop()` até o fim do processo. O
  notifier do Tk no Windows despacha *todas* as mensagens da thread, inclusive as
  da janela oculta, então a bandeja continua respondendo.
- **Sem chamadas cruzadas.** A thread do barramento nunca toca o Tk; ela enfileira
  o resultado e acorda a principal com `PostMessageW`. Isso elimina deadlocks.
- **O Keeper é lógica pura**: recebe `tick()` e `handle_event()`, não dorme, não
  cria thread, não conhece janela. Por isso é testado diretamente.

---

## 5. Funcionalidades

### 5.1 App de bandeja

- Ícone com tooltip (estado da reaplicação e última ação); clique abre a janela;
  menu: Open, Restore, Save, Pause automatic re-apply, Start with Windows, Exit.
  O Save da bandeja relê os monitores antes de gravar (D24); o Exit pergunta se
  a janela tem alterações não salvas.
- Ícone recriado sozinho quando o Explorer reinicia.
- Instância única por sessão; rodar de novo traz a janela da cópia existente;
  `--quit` encerra a cópia existente; `--startup` fica só na bandeja.
- Reaplicação automática (seção 6, D11): eventos do Windows, início do app e
  verificação periódica em modo *detect* ou *rewrite*.

### 5.2 Janela

- Uma aba por monitor; cartões em colunas que se ajustam à largura, na ordem do
  ajuste (D26), cada um com ícone e uma dica; controles de canal na cor do canal;
  indisponíveis num cartão recolhido no fim; ↺ por controle alterado. Dentro de
  cada cartão vale a ordem do registro; a ordem de *escrita* do Restore não muda.
- Slider fino (linha de 4 px, botão redondo, escalados pelo DPI) que vai direto ao
  ponto clicado + campo numérico validado; listas para controles de escolha;
  escrita com debounce de 250 ms; valor confirmado por leitura volta para a tela.
- A roda do mouse só rola a página: nenhum controle muda de valor por ela.
- Perfis por monitor (D23): linha *Profile* em cada aba com seletor (troca e
  aplica na hora, perguntando se houver alterações não salvas), New, Rename,
  Delete (só perfis fora de uso) e o estado daquele perfil.
- Save, Restore, Rescan (`F5`), Calibration; rótulo "saved HH:MM / unsaved
  changes" com legenda listando cada diferença.
- Fechar com alterações não salvas pergunta Salvar / Voltar ao salvo / Cancelar.
- Aba Settings: pausa, intervalo (1–1440 min), modo detect/rewrite, aplicar ao
  iniciar, iniciar com o Windows, abrir a pasta de dados.
- Tema escuro (padrão) e claro, inclusive a barra de título; geometria lembrada e
  descartada se cair fora das telas atuais.
- Teclado: `Ctrl+S`, `F5`, `Esc`, `Ctrl+Tab`; legendas também na barra de status
  ao focar um controle.

### 5.3 Calibração

Guia de 8 passos a olho (D29), cada um com padrão próprio em sRGB exato, o que
olhar e qual controle mexer: preparação → brilho → altas-luzes (barras
235–254 piscando) → sombras (barras 1–25 piscando) → equilíbrio de cinza → gama
(linhas de 1 px, 186 = 2.2) → gradientes → cores (comparar com conteúdo
conhecido). Tela cheia com F11 no monitor em ajuste. Pesquisa em
[`PESQUISA-CALIBRACAO.md`](PESQUISA-CALIBRACAO.md).

### 5.4 CLI

`list`, `features`, `caps`, `get`, `set`, `save`, `restore`,
`autostart enable|disable|status`, `app`; `--log-file` para execução sem console.

### 5.5 Controles

48 controles em 13 categorias: 43 por DDC/CI e 5 pela GPU. Lista completa no
README.

---

## 6. Decisões

Formato: **contexto → decisão → alternativas descartadas → consequências.**

### D1 — Só biblioteca padrão, falando com DLLs por `ctypes`

- **Contexto:** o programa conversa com hardware; dependência é superfície de
  cadeia de suprimentos.
- **Decisão:** nenhuma dependência de runtime. `dxva2`, `gdi32`, `nvapi64`,
  `user32`, `shell32`, `kernel32`, `wtsapi32` via `ctypes`, sempre com
  `argtypes`/`restype` declarados.
- **Alternativas:** `pywin32`, `monitorcontrol` (PyPI), toolkits de GUI externos.
- **Consequências:** mais código de binding, mas nada a auditar fora do projeto.
  Sem `argtypes`, ctypes trunca handles de 64 bits — erro que parece defeito de
  hardware.

### D2 — Allowlist fechada e códigos bloqueados

- **Contexto:** o único caminho documentado de dano permanente por DDC/CI é a
  faixa reservada ao fabricante.
- **Decisão:** só códigos do registro podem ser escritos; `0xE0–0xFF` recusado;
  `0x01, 0x04, 0x05, 0x06, 0x08, 0xB0, 0xCA, 0xD6` bloqueados (degauss, resets de
  fábrica, gravação em EEPROM, travar botões, power mode que pode não acordar).
- **Consequências:** `0xB0` ficou bloqueado na política; o teste dele (seção 3)
  foi uma chamada avulsa, fora do app.

### D3 — A capabilities string é piso, não teto

- **Decisão:** sondar todos os códigos do registro; a string só fornece rótulos e
  valores permitidos.
- **Evidência:** o ASUS passou de 7 para 24 controles.
- **Consequências:** sondagem completa custa ~11 s para dois monitores — mitigado
  por cache e pelo carry-over (D6).

### D4 — Camada GPU como complemento

- **Decisão:** NVAPI (vibrance, hue — IDs privados, com três travas de validação)
  e gamma ramp (brilho, contraste, gama). Cada controle diz sua origem.
- **Consequências:** saturação no TCL, que não tem. Gamma ramp é compartilhado
  com f.lux/Night Light e reseta em hot-plug/driver/reboot, por isso entra no
  upkeep (D10).

### D5 — Um snapshot, atômico e não confiável

- **Decisão:** um único snapshot em JSON versionado; escrita via arquivo
  temporário no mesmo diretório + `os.replace`; todo valor lido do disco passa
  pela política como uma escrita de slider; arquivo corrompido = "nada salvo",
  preservado para inspeção.
- **Alternativas:** perfis nomeados, histórico — fora do escopo pedido.
- **Consequências:** Save sobrescreve; não há desfazer além do último save.

### D6 — Identidade pelo EDID

- **Contexto:** `\\.\DISPLAYn` é posição, e o Windows renumera (o TCL foi
  `DISPLAY1`, `DISPLAY2` e `DISPLAY1` de novo durante o projeto).
- **Decisão:** `hardware_id = fabricante-produto-serial` do EDID, lido da
  instância PnP ligada *agora* ao `DISPLAYn` via `EnumDisplayDevicesW` (o registro
  guarda EDIDs de portas antigas). Pareamento por EDID → nome → device → id, em
  passadas; um identificador só decide se for único dos dois lados; EDIDs
  diferentes vetam o par. EDID inválido (cabeçalho, checksum) é rejeitado.
- **Consequências:** snapshots antigos sem EDID continuam pareando por porta.
  `refresh(probe=False)` preserva os valores de um monitor reconhecido pelo EDID
  (*carry-over*), o que torna re-enumerar barato.

### D7 — Defeitos por modelo num registro de quirks

- **Decisão:** `ddc/quirks.py` indexado por `model_key` do EDID (`TCL-1003`),
  com a lista de controles **voláteis** e a evidência de como foi estabelecido.
  Só controles contínuos podem ser voláteis (teste garante).
- **Alternativas:** detectar automaticamente — impossível, a leitura mente.
- **Consequências:** monitor novo com o mesmo defeito precisa ser cadastrado.

### D8 — Escrita através de valor intermediário: PIVOT 50 e NEIGHBOUR ±1

- **Contexto:** o TCL ignora escrita de valor igual ao reportado.
- **Decisão:** em controles voláteis, escrever primeiro um intermediário:
  **50** (ou vizinho se o alvo for 50; nunca acima do máximo) em Save, Restore e
  quando o monitor volta; **valor−1** (+1 no zero) na manutenção periódica e ao
  redigitar o valor reportado. As duas escritas são debitadas juntas do orçamento.
- **Por que 50:** pedido do usuário — mudança grande e inequívoca em ações que ele
  dispara. **Por que ±1 no periódico:** 50 a cada minuto faria a cor do TCL piscar
  por ~1 s a cada rodada.
- **Consequências:** ao salvar, a tela do TCL dá uma piscada; débito conjunto
  evita deixar o controle no intermediário se o orçamento acabar.

### D9 — Restore relê antes de comparar

- **Contexto:** num app que fica dias aberto, o cache envelhece; mudanças pelos
  botões do monitor ficam invisíveis.
- **Decisão:** o Restore lê cada controle salvo (exceto voláteis, que sempre são
  escritos) e escreve só o que difere.
- **Consequências:** ~40 leituras (~2 s) por Restore; leitura não desgasta EEPROM.

### D10 — Upkeep só toca voláteis e GPU

- **Decisão:** `reassert_saved` escreve apenas controles voláteis e ajustes de
  GPU. Um monitor que guarda as configurações nunca recebe escrita automática.
- **Consequências:** o timer não pode virar loop de desgaste. Monitor ausente não
  vira "problema" no relatório (é rotina).

### D11 — Quando reaplicar: eventos + detect ou rewrite

- **Contexto:** o usuário lembrou que a leitura não serve para detectar a perda e
  pediu "tentar a leitura primeiro, mas pensar no ±1 também".
- **Decisão:**
  - **Eventos do Windows** (monitor conectado via `GUID_DEVINTERFACE_MONITOR`,
    `WM_DISPLAYCHANGE`, retorno da suspensão, telas religadas, desbloqueio) →
    espera 3 s agrupando rajadas → re-enumera → reaplica via 50.
  - **detect** (padrão): a cada intervalo, *uma leitura* por monitor volátil só
    para saber se responde; parou e voltou → reaplica via 50. Enquanto algum
    estiver perdido — fora da lista **ou** listado com handle morto —, re-enumera
    no máximo a cada 5 min (um handle morto nunca mais responde).
  - Num evento, monitor volátil listado que **não responde** (desligado numa
    conexão que o mantém enumerado) não recebe a escrita: fica marcado perdido e
    é reaplicado quando voltar a responder. Uma escrita que falhou não conta como
    feita.
  - **rewrite**: a cada intervalo, reescreve os voláteis via ±1.
  - Suspenso com a janela aberta e alterações não salvas, ou com *Pause*. Mesmo
    suspenso, um evento **re-enumera** (não escreve): os handles antigos morrem
    com a mudança e o serviço ficaria preso a eles.
- **Alternativas:** reescrever via 50 sempre (pisca); só eventos (pode perder o
  religamento de HDMI que não some da lista).
- **Consequências:** zero escrita enquanto nada acontece; zero tráfego sem
  monitor volátil. **Detecção ainda não validada no TCL** (seção 11).

### D12 — De interface web para app de bandeja em Python, um processo

- **Contexto:** o usuário quis um app próprio, residente, `.exe`, com mínimo de
  RAM.
- **Medições que guiaram:** Python + ctypes ~8 MiB privados; + núcleo ~15 MiB;
  + janela Tk ~+8 MiB.
- **Opções apresentadas:** Python em 2 processos (janela como processo à parte),
  **Python em 1 processo** (escolhido pelo usuário), reescrita em Rust (~3–5 MiB,
  semanas de trabalho, risco de regressão).
- **Decisão:** um processo; bandeja em Win32 puro; Tk importado só na primeira
  abertura da janela e mantido carregado (esconder, não destruir).
- **Consequências:** com trim de working set (D21), o residente fica em 1–3 MiB.

### D13 — Uma thread no barramento, resultados por mensagem

- **Decisão:** `Worker` é a única thread que toca o serviço; jobs em ordem;
  exceções viram callback e log, a thread sobrevive; parada termina o job atual
  (nunca metade de uma escrita em dois passos).
- **Consequências:** a janela nunca congela durante sondagens de segundos.

### D14 — Mutex do barramento entre processos

- **Contexto:** app e CLI juntos intercalariam transações I²C.
- **Decisão:** mutex nomeado `Local\monitorctl.ddc-bus` em volta de **cada**
  operação (não de lotes), com a pausa pós-operação dentro do lock; dono que
  morreu (`WAIT_ABANDONED`) não trava; timeout de 10 s segue em frente com aviso.
- **Consequências:** os dois se revezam uma transação por vez.

### D15 — Iniciar com o Windows pela chave Run

- **Contexto:** na 0.1 era uma tarefa agendada que rodava `restore` e saía.
- **Decisão:** para um app residente, a chave `HKCU\…\Run`: sem administrador,
  visível em *Aplicativos de inicialização*, a fonte da verdade é o registro (não
  as configurações do app). O app corrige o caminho **só se a instalação
  registrada não existe mais** (pasta movida), nunca reativa algo que o usuário
  desligou e nunca toma o lugar de outra cópia que ainda existe — abrir o build
  novo em `dist/` ou o código-fonte para testar não pode redirecionar o início com
  o Windows para algo que o próximo build apaga. Ativar remove a tarefa antiga.
- **Atraso:** a chave Run não atrasa; o app espera 10 s antes da primeira
  aplicação quando iniciado com `--startup`.
- O interruptor de *Aplicativos de inicialização* também conta: ver D32a.

### D16 — Pasta portátil com PyInstaller

- **Decisão:** modo pasta (não exe único), dois exes (`monitorctl.exe` sem
  console, `monitorctl-cli.exe` com console) compartilhando `_internal/`;
  `optimize=2`; exclusão de módulos não usados (`ssl`/`hashlib` sozinhos puxavam
  5 MB de `libcrypto`); recurso de versão gerado de `__version__`; PyInstaller
  fixado em venv próprio de build.
- **Alternativas:** exe único (extrai para `%TEMP%` a cada início, processo pai
  extra, mais falso-positivo de antivírus); Nuitka.
- **Consequências:** 22,7 MiB de pasta, 10,8 MiB de zip.

### D17 — DPI de sistema

- **Decisão:** `SetProcessDpiAwarenessContext(SYSTEM_AWARE)` antes de qualquer
  janela.
- **Por que não por monitor:** Tk 8.6 não reescala ao mudar de monitor; ficaria
  do tamanho errado em vez de só levemente desfocado.

### D18 — Remoção da interface web

- **Decisão:** removida na 0.2.0 a pedido do usuário. Junto foi a superfície de
  rede (servidor HTTP local com allowlists de Host/Origin, CSP etc.).
- **Consequências:** a calibração e o aviso de alterações migraram para o Tk.
  Backup do código web no scratchpad da sessão (`monitor-controler-pre-tray.tar`).

### D19 — Configurações lidas campo a campo

- **Decisão:** `settings.json` versionado e atômico; um campo inválido volta ao
  padrão **só ele**; arquivo ilegível vira padrões e é preservado.

### D20 — Log em arquivo, sempre

- **Decisão:** `RotatingFileHandler` de 512 KiB × 1 em
  `%LOCALAPPDATA%\monitorctl\monitorctl.log`; `sys.excepthook`,
  `threading.excepthook` e o handler de callbacks do Tk logam tudo.
- **Por que:** um processo sem console não tem onde mostrar erro; o log é o único
  registro do que ele fez enquanto ninguém olhava.

### D21 — Trim do working set

- **Decisão:** `K32EmptyWorkingSet` depois de cada ação automática e ao esconder
  a janela.
- **Consequências:** é cosmético quanto ao que o processo *pode* usar (páginas
  voltam sob demanda), mas é o número que o Gerenciador de Tarefas mostra:
  26 MiB → ~1 MiB ao fechar a janela.

### D22 — Entrada na janela: nada muda valor sem intenção

- **Contexto:** o usuário mudava ganhos sem querer ao rolar a página (a roda
  parava sobre um campo ou lista e girava o valor); o clique no trilho do slider
  andava um passo em vez de ir ao ponto; o slider do `clam` era uma barra grossa.
- **Decisão:**
  - remover as ligações de roda das classes `TSpinbox` e `TCombobox` no app
    inteiro; a roda sempre rola a página (exceto dentro de uma lista aberta);
  - clique e arraste no slider levam ao ponto do ponteiro (substitui a ligação
    padrão, que repete +1/−1);
  - slider desenhado com elementos de imagem do ttk (linha + botão), repintados
    na troca de tema; fundo fixado em todos os estados, porque o `clam` pinta de
    cinza claro o fundo de qualquer widget desabilitado ou sob o mouse;
  - sair de um campo numérico só força a escrita de controle volátil se o
    usuário digitou ou deu Enter (bug 17);
  - debounce agendado no root, não no widget da linha, para um redesenho não
    perder a escrita pendente.
- **Alternativas:** roda com Ctrl para mudar valor (ninguém pediu, e ainda é
  escrita acidental possível); `tk.Scale` clássico (não segue o tema).
- **Consequências:** valores mudam só por clique, arraste ou teclado.

### D23 — Perfis por monitor

- **Contexto:** o usuário pediu perfis para salvar configurações. Escolhas dele:
  **um monitor por perfil** (cada monitor com sua lista e seu perfil em uso),
  **todos os controles** em cada perfil, troca **só pelo seletor da janela**
  (sem submenu na bandeja, sem atalhos, sem horário).
- **Decisão:**
  - `ddc/profiles.py`: `ProfileBook` imutável → `MonitorProfiles` (identidade +
    perfis + ativo) → `Profile` (nome, hora, valores). Toda mudança é uma função
    `livro → livro` aplicada por `ProfileStore.update` sob lock e gravada de forma
    atômica; recusa (`ProfileError`) não muda nada.
  - O "snapshot" virou **vista**: os perfis em uso de todos os monitores
    (`active_snapshot`). Restore, keeper e upkeep continuam iguais por cima dela.
  - O pareamento monitor ↔ entrada usa **o livro inteiro contra todos os
    monitores conectados** (`pair_monitors`), em Restore, na janela e nas
    operações de perfil: a janela mostra exatamente o que o Restore usa.
  - Save grava no perfil em uso (cria "Default" se não houver); perfis de
    monitores desconectados ficam intactos. Trocar de perfil = marcar ativo +
    Restore só daquele monitor (via 50 nos voláteis). New = Save as + empurrar
    voláteis. O perfil em uso não pode ser apagado.
  - Nomes: até 40 caracteres, sem caracteres de controle, únicos por monitor sem
    diferenciar maiúsculas.
  - Migração: sem `profiles.json`, o `snapshot.json` vira o "Default" de cada
    monitor; o antigo nunca é apagado nem escrito.
  - Arquivo ilegível é guardado como `profiles.unreadable.json` antes do próximo
    save, em vez de sobrescrito.
- **Alternativas:** perfis globais (um perfil = todos os monitores; mais simples,
  mas trocar o TCL arrastaria o ASUS); perfis parciais (só controles marcados;
  mais interface); troca pela bandeja/atalhos/horário (não pedidos).
- **Consequências:** cada aba ganhou a linha *Profile* (seletor, New, Rename,
  Delete, estado). Trocar o perfil de um monitor nunca escreve em outro.

### D24 — Save relê só pela bandeja

- **Contexto:** a revisão de 02/10 achou que o Save grava o cache, que envelhece
  quando se mexe pelos botões do monitor (o Restore já relia, D9).
- **Decisão (do usuário):** o Save **da janela** grava o que a janela mostra; o
  Save **da bandeja**, que não mostra nada, relê cada controle restaurável antes
  (`save_snapshot(reread=True)`). Leitura que falha mantém o valor do cache.
- **Alternativas:** reler sempre (alguns segundos a mais em todo Save); nunca.
- **Consequências:** junto veio a correção de `read_feature`, que transformava
  uma leitura falha em "não suportado" (bug 23).

### D25 — Rampa gama neutra não é nossa; handles da GPU renovados

- **Contexto:** revisão de bloat/bugs de 02/10. O backend de gama devolve um
  estado virtual (50/50/50) e o upkeep reescreve ajustes de GPU a cada evento;
  um perfil salvo em neutro gravava a rampa **identidade** em toda saída (o ASUS
  inclusive) a cada desbloqueio, apagando a curva de um perfil ICC, o Night Light
  e o f.lux. Os handles NVAPI eram enumerados uma vez e nunca renovados.
- **Decisão:**
  - Com os três controles em 50, o gama **não escreve nada**. Ao sair do neutro,
    guarda a rampa que estava no dispositivo; ao voltar, devolve **essa** rampa
    (não a identidade).
  - `GpuBackend.refresh()` (no-op por padrão): a NVAPI re-enumera os handles, e
    o serviço chama em toda re-enumeração. Falha num backend só gera aviso.
  - `GpuAdjustment.writable`: um vibrance que falhou na validação aparece, mas
    não como controle gravável.
- **Alternativas:** não reaplicar GPU no upkeep (perde o ajuste após reload de
  driver, o motivo original do D10); salvar sem os ajustes neutros (o perfil
  deixaria de dizer "neutro").
- **Consequências:** ajustes por software só tomam a rampa quando o usuário
  realmente os usa.

### D26 — Janela 2: ordem do trabalho, cor por canal, indisponíveis recolhidos

- **Contexto:** o usuário pediu uma revisão de usabilidade e interpretabilidade:
  "uma ordenação que faça sentido" e "algo que represente" cada ajuste (cor por
  canal RGB). Também pediu tirar da tela o aviso de capabilities malformada do
  TCL. Pesquisa em [`PESQUISA-UI.md`](PESQUISA-UI.md) (menus de Dell, ASUS, LG,
  BenQ, Samsung; ArgyllCMS, DisplayCAL, Lagom; NN/g, Microsoft, WCAG).
- **Decisão:**
  - Apresentação separada do domínio em `app/presentation.py`: ordem das seções,
    ícone e dica de cada uma, cor de cada controle de canal. O registro (`vcp.py`)
    continua dizendo o que cada controle *é*; a ordem de escrita do Restore não
    muda.
  - Ordem: modo de imagem (seção própria) → brilho/contraste → temperatura →
    ganho → offset → imagem → seis eixos → entrada → áudio → geometria → menu →
    GPU por último ("aplicado pelo PC por cima do monitor").
  - Cor por canal: um estilo de slider por canal (botão na cor, trilho tingido) e
    um ponto antes do nome; texto sempre presente.
  - Indisponíveis: fora das seções, num cartão recolhido no fim (revoga o
    "esmaecidos, não somem" da janela 1, que enterrava 9 controles úteis sob 34
    mortos no TCL). Só leitura temporário continua no lugar, desabilitado.
  - ↺ por controle que difere do perfil em uso.
  - Avisos do parser de capabilities: fora da página; ficam no tooltip da linha
    de identificação do monitor, no log (nível debug) e no `monitorctl list`.
- **Alternativas:** expansor "Advanced" por seção (adiado: com os indisponíveis
  recolhidos as páginas já são curtas); trilhos com gradiente (exigiriam slider
  em canvas); ícone em cada controle (ruído).
- **Consequências:** a página do TCL tem 20 controles úteis e nenhum morto; o
  ASUS segue o mesmo esquema sem nada específico de modelo.

### D27 — Ajuste de GPU neutro não é nosso, nem no upkeep; a rampa volta ao sair

- **Contexto:** segunda revisão de 02/10. O D25 parou de escrever a rampa gama em
  neutro, mas o upkeep continuava reescrevendo vibrance/matiz da NVIDIA neutros a
  cada evento (sobrepondo o painel da NVIDIA ou um jogo); e a rampa ficava
  aplicada depois que o app fechava, de modo que a próxima execução a tomava
  pela rampa "original" do dispositivo.
- **Decisão:**
  - `FeatureState.neutral` para ajustes de GPU; o **upkeep** pula um ajuste cujo
    valor salvo é o neutro. Restore explícito continua aplicando.
  - O backend de gama lembra a rampa que escreveu: só devolve a original se a
    que está lá ainda é a dele; se outra coisa a trocou (reset de driver, Night
    Light, outra saída com esse nome), aquela vira a nova original.
  - **Ao sair, o app devolve a rampa original.** Os ajustes de GPU valem enquanto
    o monitorctl roda; o próximo início reaplica o perfil.
- **Alternativas:** deixar a rampa aplicada ao sair (o comportamento antigo:
  persistia até o reboot, mas corrompia a "original" da próxima execução).
- **Consequências:** fechar o app desfaz brilho/contraste/gama por software até
  ele voltar; um fechamento abrupto (crash) ainda deixa a rampa para trás.

### D28 — Janela 3: ordem por importância, etiqueta monitor × PC, Auto apply

- **Contexto:** pedidos do usuário depois de usar a janela 2: tirar o pontilhado
  da aba selecionada; poder **juntar mudanças e aplicá-las de uma vez**; ordenar
  pelo que ele de fato mexe (brilho/contraste, saturação, equilíbrio de branco e
  preto, cores, temperatura… e no fim idioma e entrada), com a GPU depois dos
  ajustes importantes do monitor; e **distinguir o que muda o hardware do
  monitor do que é software no PC**.
- **Decisão:**
  - Ordem: brilho/contraste → imagem (saturação, nitidez) → ganho → offset →
    seis eixos → temperatura → modo de imagem → GPU (curva, cor) → áudio →
    geometria → entrada → menu (idioma).  Substitui a ordem da pesquisa (D26),
    que punha o modo de imagem primeiro: a preferência de quem usa prevalece.
  - Cada cartão leva uma etiqueta: **monitor** (cinza) ou **PC software**
    (laranja), com legenda explicando a diferença.
  - **Auto apply** (padrão ligado, persistido): desligado, cada mudança fica
    pendente na janela (nome do controle na cor de destaque, "Apply (n)" e
    "changes not applied"); **Apply** escreve tudo na ordem do registro;
    **Discard** desfaz; religar aplica o que esperava; trocar de perfil só marca
    o perfil e deixa os valores dele pendentes (`MonitorService.select_profile`);
    Save e New aplicam antes de salvar; Restore descarta; fechar pergunta;
    trocar de perfil com pendências pergunta e, no "sim", aplica antes de
    salvar. Pendências ficam guardadas pela **identidade EDID** do monitor (não
    pela posição `DISPLAYn`), têm marca além da cor (•), e o ↺ cancela uma
    pendência. Com a janela aberta, Save/Restore da bandeja passam pelos dela.
  - O anel de foco pontilhado do `clam` sai do layout da aba.
- **Alternativas:** Apply por cartão (mais cliques para o caso comum de ajustar
  várias coisas e julgar juntas).
- **Consequências:** com Auto apply desligado, o keeper continua mantendo o
  perfil em uso; as pendências não tocam o hardware até Apply.

### D29 — Calibração como guia a olho, com barras que piscam

- **Contexto:** o usuário pediu para refazer a calibração a partir de pesquisa,
  citando um vídeo (barras numeradas piscando sobre preto e sobre branco), sem
  tons de pele, e com a orientação de comparar cores com conteúdo conhecido.
- **Decisão:** um guia de 8 passos (`calibration.STEPS`), cada um com padrão,
  "o que olhar" e "o que ajustar", na ordem de Argyll/DisplayCAL adaptada ao
  monitor (brilho → contraste → sombras → cinza → gama → gradientes → cores).
  Barras de recorte piscando a 1 Hz contra o próprio fundo (AVS HD 709; abaixo
  do limite da WCAG 2.3.1), mais os quadrados estáticos do Lagom. Tela cheia
  (F11) e navegação pelo teclado; os padrões mantêm as cores exatas na troca de
  tema. O timer do pisca pertence à janela principal, não ao Toplevel.
- **Alternativas:** página única rolável (a anterior; não diz o que fazer nem
  em que ordem); controles do monitor embutidos em cada passo (adiado: exigiria
  saber em qual monitor a janela está e duplicar os controles da janela
  principal).
- **Consequências:** a calibração diz o controle de cada passo; o usuário ajusta
  na janela principal, lado a lado.

### D30 — Handlers da janela oculta nunca chamam o Tk enquanto ele roda o loop

- **Contexto:** o `.exe` caiu três vezes (02/10 18:44 e 19:03, 03/10 00:06) sem
  nada no log. O Windows registrou `0xc0000409` em `ucrtbase.dll`, que é um
  `abort()`. No minidump (`%LOCALAPPDATA%\CrashDumps`) estava a pilha
  `Tcl → _tkinter → PyEval_RestoreThread → _Py_FatalErrorFunc` com a mensagem
  "the GIL is released (the current Python thread state is NULL)". O usuário
  arrastava o slider de brilho rapidamente até o máximo.
- **Causa:** com a janela aberta, quem bombeia as mensagens é o `Tk.mainloop()`.
  A `WM_APP_WORKER` chega ao WNDPROC da janela oculta, que é um callback do
  ctypes, de dentro do `Tcl_DoOneEvent`, e esse handler chamava o Tk
  (`worker.drain` → callbacks da janela).
  - Toda chamada ao Tk termina zerando o *thread state* que o `_tkinter` guardou
    ao entrar no Tcl.
  - O próximo evento Tcl do mesmo `Tcl_DoOneEvent` que chama Python (um
    `<B1-Motion>` do slider, um `after` do debounce) encontra esse estado nulo,
    e o interpretador aborta.
  - Reproduzido com `MessageWindow` + Tk + uma thread inundando mensagens: cai
    em segundos.
- **Decisão:** `app/handoff.py` (`TkHandoff`).
  - Antes de o Tk existir, a ação roda na hora.
  - Depois, entra numa fila que um timer do Tk executa, com o Python entrando
    pelo caminho que o `_tkinter` espera: a cada 50 ms com a janela visível e a
    cada 250 ms escondida.
  - Passam pela fila: o drain do worker, abrir a janela (clique na bandeja e
    segunda instância), `WM_CLOSE` e o comando escolhido no menu da bandeja. O
    menu em si (`TrackPopupMenuEx`) continua no handler, porque não toca o Tk.
  - O `faulthandler` grava em `crash.log` a pilha de qualquer abort futuro.
- **Alternativas:**
  - Chamar o Tk a partir da thread do barramento, que o `_tkinter` encaminha:
    quebra o invariante "o barramento nunca chama a UI" e bloqueia a thread no
    encerramento.
  - Postar `WM_CLOSE` para uma Toplevel oculta e usar o protocolo
    `WM_DELETE_WINDOW` como campainha: depende de detalhe interno do Tk e
    confunde o fechar.
  - Enfileirar um evento Tcl via ctypes no `tcl86t.dll`: frágil.
  - O polling foi a opção mais simples de provar.
- **Consequências:**
  - Até 50 ms a mais para o resultado de uma escrita aparecer no slider.
  - Quatro despertares por segundo com a janela escondida, um custo não
    mensurável.
  - O teste `test_handoff` reproduz o crash num processo filho, com e sem o
    handoff.

### D31 — Calibração: um objetivo concreto por passo e linhas "se X → mexa Y"

- **Contexto:** o usuário achou as instruções longas e vagas ("deveria ser quase
  imperceptível piscar no número X") e não viu diferença no passo de gama, que
  tinha cinco quadrados candidatos.
- **Decisão:** cada `Step` tem um `goal` de uma frase, com o número a observar
  (barra 254 pisca no limite; barra 3 pisca no limite, 1–2 podem sumir), e
  `fixes`, uma linha por sintoma com o controle e o sentido.
  - O passo de gama virou um quadrado 186 grande dentro das listras: "sumiu"
    quer dizer gama 2,2; mais escuro → suba Gamma (software); mais claro →
    desça. A rampa nunca move as listras pretas e brancas, só o quadrado.
  - Na tela cheia, a faixa inferior mostra o objetivo.
  - O equilíbrio de cinza traz o mapa matiz → canal a baixar.
- **Alternativas:** manter a fileira de candidatos 176–196 (mede, mas pede uma
  comparação que o usuário não conseguiu fazer).
- **Consequências:** menos texto e uma pergunta respondível por passo.

### D32 — Interruptor do Gerenciador de Tarefas conta; geometria em entrada digital não existe

Duas causas, achadas na mesma sessão (10/10), de dois sintomas do usuário: "o
início com o Windows está ligado mas o app não abre" e "ao abrir, diz que há
mudanças não salvas, mas não há nada para salvar".

**a) Início com o Windows**

- **Contexto:** nenhum boot desde 03/10 registrou `started with Windows: True`.
  A chave Run estava certa e o `.exe` existia. Em
  `HKCU\…\Explorer\StartupApproved\Run` o valor `monitorctl` era `03…`
  (desligado), gravado em 03/10 às 11:26, segundos depois de várias entradas do
  Edge — alguém desligou em *Aplicativos de inicialização*. O Explorer pula um
  valor Run desligado ali. Em 05/10 o usuário religou pelo app: o
  `enable()` reescreveu só a chave Run, o interruptor continuou desligado, e
  `is_enabled()` (que só olhava a chave Run) mostrava a caixa marcada.
- **Decisão:**
  - `is_enabled()` = valor Run existe **e** o interruptor não está desligado
    (primeiro byte do `REG_BINARY` ímpar = desligado).
  - `enable()` apaga o interruptor desligado: ligar no app é um pedido
    explícito. Apagar, e não escrever `02 00…`, porque um valor Run sem
    entrada no `StartupApproved` inicia — foi assim que o app iniciou com o
    Windows em 02/10, antes de existir a entrada.
  - `disable()` apaga os dois. `repair()` não toca no interruptor: segue uma
    pasta movida, nunca religa o que o usuário desligou.
  - `autostart status` (CLI) diz quando está desligado no Gerenciador.
- **Alternativas:** só avisar na janela que o Gerenciador desligou (o usuário
  teria de achar o interruptor sozinho); ignorar o `StartupApproved` (o
  formato não é documentado) — mas é ele que decide se o Windows inicia o app.
- **Consequências:** a caixa da janela e do menu mostra a verdade; o formato
  não documentado é lido só no primeiro byte, e uma leitura que falha vira
  `AutostartError`, já tratado pelos chamadores.

**b) "Unsaved changes" fantasma**

- **Contexto:** o ASUS está em HDMI (que ele chama de "DVI 2"). Nessa entrada
  os controles de geometria VGA (`0x20`, `0x30`, `0x0E`, `0x3E`…) são inertes e
  o firmware devolve lixo: o perfil guardou `horizontal_position` = 181 (acima
  do máximo de 100), e o monitor passou a ler 189 sozinho. A janela comparava
  e acusava "unsaved changes". Cada Restore do usuário ("7 written" no log: 6
  voláteis do TCL + 1) escrevia 181 num controle que
  `docs/monitors/asus-vs248.md` manda não escrever fora de VGA.
- **Decisão:** `vcp.inert_keys(input_source)`: numa entrada **certamente
  digital** (DVI, DisplayPort, HDMI) os controles da categoria Geometry são
  inertes e:
  - o Save não os guarda;
  - o Restore e a troca de perfil não os escrevem — decidido por uma leitura
    nova da entrada, só quando o perfil tem geometria (a entrada muda pelos
    botões do monitor, e o cache pode ter dias);
  - a janela não os compara (`saved_value`, `differences`), o que cobre a
    etiqueta, o ↺, a pergunta ao sair e a pergunta ao trocar de perfil.
  - Entrada desconhecida (não lida, código fora do MCCS) mantém o
    comportamento anterior.
- **Alternativas:** um quirk por modelo (o defeito é de qualquer monitor com
  VGA, não do ASUS); esconder a geometria da janela (ainda mostraria o lixo
  ao alternar); guardar o valor antigo no Save (o que está nos perfis atuais
  já é lixo lido em HDMI).
- **Consequências:**
  - Os perfis atuais do ASUS ainda guardam a geometria lida em HDMI; ela é
    ignorada enquanto a entrada for digital e sai do perfil no próximo Save.
  - Um perfil salvo em VGA, restaurado com o monitor em HDMI, troca a entrada
    para VGA (o achado 6 de §11) mas não restaura a geometria. Nenhum perfil
    atual está nesse caso.
  - DVI-I analógico seria tratado como digital; nenhum monitor visto o usa.

### D33 — O que fica em espera não se perde; a janela fala em identidade, não em posição

Fecha os achados 8–11 da revisão lógica de 10/10, com as escolhas do usuário
(11/10).

**a) Keeper suspenso fica devendo (achado 8)**

- **Contexto:** com Pause, ou com a janela aberta e alterações não salvas, o
  keeper não escrevia, e o que acontecia nesse meio-tempo se perdia: um evento
  (telas religadas) só re-enumerava, e no modo *detect* nem havia ping. Ao
  despausar, ou quando o drift sumia pelo ↺, nada reaplicava até o próximo
  evento — com o TCL já de fábrica.
- **Decisão (usuário: "só reaplicar tudo funciona"):** o keeper guarda uma
  dívida (`owed`), paga com **um push completo via 50** — o mesmo de um evento —
  assim que fica livre:
  - evento durante a suspensão → deve;
  - Pause observado por qualquer tick ou evento → deve (pausado nada é
    observado, então ao despausar reaplica uma vez);
  - suspenso só pela janela, no modo *detect*: **continua pingando** (leitura,
    não escrita); monitor que voltou → deve. Sem rescan: ele redesenharia a
    janela sob o usuário;
  - sem perfil salvo, nada é devido.
  - Pagamento: `Keeper.catch_up()`, chamado pelo worker quando a janela libera o
    keeper (`set_editing` de verdadeiro para falso → `Worker.nudge`), quando as
    configurações mudam (despausar) e no início de cada tick.
- **Alternativas:** lembrar só o evento e reaplicar só nele (não cobre o
  religamento que só o ping vê); reaplicar ao fim de toda suspensão (piscada a
  cada Save, que já empurra os voláteis).
- **Consequências:** despausar dá uma piscada (pivô 50) se houve tick ou evento
  durante a pausa. Um monitor ainda desligado na hora do pagamento fica
  "perdido" e é empurrado pelos ticks quando responder.

**b) Jobs da janela resolvem o monitor pelo EDID (achado 9)**

- **Contexto:** escrita de slider, Apply, troca/criação/renomeação/exclusão de
  perfil levavam o id de posição (`display1`) para a fila do barramento. Um
  rescan do keeper na frente, com renumeração, fazia o job agir no outro
  monitor — inclusive escrever no ASUS. E o resultado voltava para a linha
  daquela posição, que após o redesenho podia ser do outro monitor.
- **Decisão:** `MonitorService.resolve(hardware_id, monitor_id)` acha o id
  *atual* pela identidade EDID dentro do job; sem identidade (EDID não lido ou
  painéis idênticos) usa a posição; monitor sumido levanta `MonitorNotFound`
  em vez de escrever no que ocupou a posição. Na janela, `locator()` monta isso
  no envio; cada `FeatureRow` guarda a identidade (`ident`), e todo resultado
  volta por ela (`live_row`, `_monitor_id_of`), contra a lista que a janela
  mostra.
- **Alternativas:** bloquear o rescan do keeper enquanto há job da janela (o
  rescan vem de um evento real e renova handles mortos); recarregar a janela
  antes de cada job (não fecha a corrida).
- **Consequências:** os perfis devolvidos pelo barramento podem vir com ids
  novos até a recarga que o keeper já pede; a janela recarrega logo depois.

**c) Save, fechar e sair esperam as mudanças chegarem (achado 11)**

- **Contexto:** um arraste espera 250 ms (debounce) e depois o barramento.
  `Ctrl+S` ou fechar nesse intervalo salvava/comparava os valores de antes.
- **Decisão (ideia do usuário: "os 250 ms são para aplicar, não para
  salvar"):** Save, Apply, fechar, sair, trocar de perfil e New **enviam já** a
  mudança que ainda esperava o debounce (e o número digitado e não confirmado
  no campo) e **esperam** as escritas em voo voltarem antes de agir
  (`_after_edits`). O Restore **descarta** o que esperava o debounce (pousaria
  depois e o desfaria) e também espera o que está em voo. A janela conta as
  escritas em voo e os debounces pendentes de todas as linhas, inclusive as que
  um redesenho substituiu.
- **Alternativas:** o Save gravar os valores da tela sem passar pelo monitor
  (salvaria um valor que o monitor pode ter recusado ou ajustado).
- **Consequências:** o que é salvo é sempre um valor confirmado pelo monitor,
  e é o que a tela mostrava ao apertar Save.

**d) Save/Restore com a janela ocupada (achado 10)**

- **Contexto:** Save/Restore da bandeja (ou `Ctrl+S`) com a janela aberta e
  ocupada — uma releitura, por exemplo — eram ignorados sem aviso.
- **Decisão:** ficam na fila e rodam quando a operação atual termina; a barra
  de status diz "Save will run when the current operation ends…".

**e) Apply sempre clicável; indicador no lugar do cursor ocupado**

- **Pedido do usuário:** um Apply manual, e nada de cursor "carregando" (parece
  travado).
- **Decisão:** com pendências, Apply as escreve como antes. Sem pendências,
  **reenvia o que a janela mostra para os controles que um monitor esquece**
  (voláteis via 50, ajustes de GPU fora do neutro) — `reassert_current`, o
  mesmo empurrão do fim do Save, mas com os valores da tela, salvos ou não.
  Controles que o monitor guarda não são escritos: a leitura deles é a
  verdade, e reescrever só gastaria EEPROM. O cursor não muda mais: um arco
  gira ao lado da mensagem na barra de status e, enquanto não há abas de
  monitor, no lugar delas.

---

## 7. Medições

| Métrica | Valor | Como |
|---|---|---|
| Residente na bandeja | 1–3 MiB privados residentes | `.exe` empacotado, `WorkingSetPrivate` |
| Janela aberta | ~26 MiB | idem |
| Janela fechada de novo | ~1,2 MiB | idem |
| CPU ocioso (detect, 1 min) | sem variação mensurável em 70 s | `TotalProcessorTime` |
| Sondagem completa | ~11 s (dois monitores) | log |
| Aplicação inicial no logon | 16 escritas (6 voláteis + 10 GPU) | log |
| Pacote | 22,7 MiB pasta / 10,8 MiB zip | build |
| Pacing | 40 ms após leitura, 50 ms após escrita, 100 ms após capabilities, +100 ms antes de retry | `Pacing` |
| Orçamento de escrita | 40 de pico, 30/min | `WriteBudget` |

---

## 8. Bugs encontrados e lições

| # | Bug | Como apareceu | Lição |
|---|---|---|---|
| 1 | Handle `0` virava `None` | Primeiro monitor da lista falhava | `ctypes` converte ponteiro nulo em `None`; normalizar |
| 2 | Capabilities do TCL malformada | Parser recusava | Parser tolerante a entrada de firmware |
| 3 | `SO_REUSEADDR` sequestrava porta (0.1) | Dois servidores na mesma porta | No Windows, desligar e usar `SO_EXCLUSIVEADDRUSE` |
| 4 | Restore pareava pela porta | Renumeração `DISPLAYn` | Identidade pelo EDID (D6) |
| 5 | Restore cego ao TCL | Leitura mente | Quirks + intermediário (D7, D8) |
| 6 | Save gravaria snapshot vazio após re-enumeração barata | Revisão do fluxo do app | Ler antes de agir; carry-over por EDID |
| 7 | Restore não desfazia mudança pelos botões | Teste da janela | Reler antes de comparar (D9) |
| 8 | App e CLI juntos no barramento | Medição com dois processos | Mutex entre processos (D14) |
| 9 | Categorias invisíveis na janela | Captura de tela | Colunas vazias num grupo `uniform` do grid ocupam largura cheia |
| 10 | Reaplicação falsa ao abrir | Log mostrou "displays switched back on" | Registrar a notificação de energia entrega o estado atual; reagir só à transição |
| 11 | Varredura dupla ao abrir | Log | Re-enumeração jogava fora valores válidos; carry-over |
| 12 | Janela abria na aba Settings | Captura de tela | Escolher a aba explicitamente após redesenhar |
| 13 | Handle morto nunca era renovado | Revisão (02/10) | O detect só re-enumerava monitor *fora* da lista; um listado com handle morto falhava o ping para sempre |
| 14 | Evento com o TCL desligado zerava o "perdido" | Revisão | A escrita falhava e contava como feita; ao religar, nada reaplicava |
| 15 | Eventos ignorados em pausa deixavam handles mortos | Revisão | Re-enumerar não é escrever: faz-se mesmo suspenso |
| 16 | Escrita pelo id antigo após re-enumerar | Revisão | Resolver alvos pela identidade (EDID) contra a lista atual |
| 17 | Sair do campo de um controle volátil gravava 2× | Revisão | Só forçar a escrita se o usuário digitou ou deu Enter |
| 18 | App não via snapshot salvo pela CLI | Revisão | Store relê o arquivo se ele mudou (id, mtime, tamanho) |
| 19 | Exit da bandeja descartava alterações não salvas | Revisão | O próximo início reaplicava o snapshot antigo; Exit pergunta como o fechar |
| 20 | `repair` sequestrava a chave Run | Revisão | Abrir outra cópia (dist, fonte) repontava o início; só reparar se a registrada sumiu |
| 21 | `Worker.stop` esperava a fila inteira | Revisão | A fila é FIFO; parar descarta os jobs pendentes |
| 22 | Janela e Restore pareavam monitores de jeitos diferentes | Revisão | Uma função só (`pair_monitors`) |
| 23 | Uma leitura falha marcava o controle "não suportado" | Teste dos perfis | `read_feature` estrito: só "unsupported" do monitor muda o cache; o Restore passa a reportar *failed*, não *skipped* |
| 24 | Redesenho da janela perdia a escrita pendente do slider | Revisão (ruído de Tcl nos testes) | `after` agendado no root, não no widget que o redesenho destrói |
| 25 | Fundo cinza claro nos sliders desabilitados | Captura de tela | O `clam` mapeia o fundo de todo widget desabilitado/sob o mouse; fixar no estilo novo |
| 26 | Roda do mouse mudava ganhos ao rolar a página | Usuário | `TSpinbox`/`TCombobox` têm ligação de roda própria; removida (D22) |
| 27 | Rampa identidade sobrescrevia ICC/Night Light a cada evento | Revisão de bloat (02/10) | Neutro = rampa não é nossa (D25) |
| 28 | `autostart enable` pela CLI empacotada registrava `monitorctl-cli.exe --startup` | Revisão | Sempre registrar `monitorctl.exe`; o CLI nem conhece `--startup` |
| 29 | Handles NVAPI nunca renovados | Revisão | `refresh()` em toda re-enumeração (D25) |
| 30 | Vibrance "somente leitura" aparecia gravável | Revisão | `GpuAdjustment.writable` |
| 31 | Erro de I²C na *leitura* não tinha retry | Revisão contra o `winerror.h` | Mapear `0xC0262583/86/87/8C` |
| 32 | Falha ao ler capabilities virava "string vazia" | Revisão | Levantar o erro: ganha retry e o motivo real aparece |
| 33 | `(` perdido descartava o resto da capabilities sem aviso | Revisão | Analisar o texto após o parêntese externo, com aviso |
| 34 | `tools/build.py` fechava a cópia instalada fora de `dist` | Revisão | Fechar só o processo cujo executável está em `dist`, e esperar |
| 35 | `survey_monitor.py` usava o barramento sem o mutex | Revisão | Lock por transação |
| 36 | `status --lines 0` imprimia o log inteiro | Revisão | Validar ≥ 0 |
| 37 | Falha momentânea ao ler `profiles.json` virava livro vazio, e o próximo Save o gravava | Revisão da janela 2 | Leitura que falha não é cacheada; mudança é recusada até ler de novo |
| 38 | Save tirava do perfil um controle cuja leitura falhou | Revisão | `read_failed`: o valor salvo é mantido; o Save da bandeja tenta ler de novo |
| 39 | Rampa original perdida se a devolução falhava; rampa deixada ao sair | Revisão | Descartar só depois de escrever; devolver ao sair (D27) |
| 40 | Dois painéis idênticos sem serial: pivô a cada minuto e valores trocados entre unidades | Revisão | Identidade qualificada pela posição quando o EDID se repete |
| 41 | Modo *rewrite* preso num handle morto | Revisão | Re-enumerar após falha, no intervalo lento |
| 42 | Vibrance/matiz neutros reescritos a cada evento | Revisão | Upkeep pula ajuste de GPU neutro (D27) |
| 43 | ↺ desfeito por um arraste pendente; escrita após redesenho ia para a linha antiga; slider mudava de largura | Revisão da janela 2 | Cancelar o debounce; repassar à linha viva; coluna de largura fixa |
| 44 | Calibração e listas suspensas não seguiam a troca de tema | Revisão | Repintar a calibração; recriar as listas |
| 45 | Recarga pedida com a janela ocupada nunca acontecia; ação confirmada num diálogo sobrepunha outra | Revisão | Recarga adiada roda ao liberar; revalidar depois do diálogo |
| 46 | Pendência guardada por posição ia para o outro monitor após renumeração (podia escrever no ASUS) | Revisão do Auto apply | Guardar pela identidade EDID; descartar órfãs após redesenho |
| 47 | Ligar Auto apply com a janela ocupada deixava pendências velhas que voltavam no Save | Revisão | Caixa bloqueada enquanto ocupada ou com escrita em voo; escrita automática limpa a pendência |
| 48 | Trocar de perfil sem aplicar comparava com o cache; descartava pendências sem perguntar | Revisão | Reler o monitor; incluir pendências na pergunta |
| 49 | Calibração: redesenho de 105 ms a cada evento de redimensionar; Esc fechava o guia | Revisão | Redesenho com debounce, gradiente com 256 faixas; Esc só sai da tela cheia |
| 50 | App abortava ao arrastar o slider rápido (`0xc0000409`, nada no log) | Usuário; Event Viewer + minidump | Handler ctypes da janela oculta chamava o Tk dentro do `mainloop` (D30); `faulthandler` em `crash.log` |
| 51 | Contorno pontilhado em botões e caixas após clique | Usuário | O `clam` desenha o elemento `*.focus`; removido dos layouts de botão, check, radio, menubutton e aba, com foco em cor de destaque |
| 52 | `settings.json` com bytes inválidos impedia o app de iniciar; BOM tornava `profiles.json` ilegível | Revisão (03/10) | Ler bytes, decodificar `utf-8-sig` dentro do `try`; tratar `ValueError/TypeError/OverflowError/RecursionError` como ilegível |
| 53 | Início com o Windows ligado no app, mas o app nunca iniciava (desde 03/10) | Usuário; `StartupApproved\Run` = `03…` | Interruptor desligado no Gerenciador de Tarefas era ignorado por `is_enabled`/`enable` (D32a) |
| 54 | "Unsaved changes" ao abrir sem nada mudado; Restore escrevia geometria VGA no ASUS em HDMI | Usuário; `list` do CLI vs `profiles.json` | Geometria em entrada digital é lixo: fora do Save, do Restore e da comparação (D32b) |
| 55 | Controle cuja leitura falhou na sondagem (monitor ligando) era pulado pelo upkeep e pelo Restore até um Rescan manual — inclusive os ganhos voláteis do TCL | Revisão lógica (10/10) | O carry-over leva o `read_failed` adiante a cada evento; `_put_back` agora relê esse controle antes de decidir, em qualquer modo |
| 56 | Restore dizia "already correct" para o gama por software depois que reset de driver, Night Light ou um jogo trocou a rampa | Revisão lógica | O valor do gama é virtual; `get` passa a reportar neutro quando a rampa no dispositivo não é mais a nossa (D27) |
| 57 | `monitorctl save` (CLI) gravava brilho/contraste/gama por software como 50 em todos os perfis | Revisão lógica | Esses valores vivem no processo do app; o CLI mantém os salvos (`keep_saved`) e recusa `set gpu_sw_*`, que o próprio fechamento desfaria |
| 58 | Com Auto apply desligado, trocar de perfil punha a geometria inerte na fila e o Apply a escrevia no ASUS em HDMI | Revisão lógica | `_stage_profile` ficou fora do D32b; filtra os inertes como o serviço |
| 59 | Debounce/foco disparado depois de um redesenho, com Auto apply desligado: `TclError` e pendência invisível que o próximo Apply/Save escrevia | Revisão lógica | `_commit` repassa a mudança à linha viva, como `_written` já fazia |
| 60 | Tick do keeper entre a escrita do usuário e o drain do resultado desfazia a escrita (rewrite) ou um Save encadeado após Apply | Revisão lógica | Suspender o keeper também enquanto a janela está ocupada ou com escrita em voo |
| 61 | Falha do registro ao alternar o início com o Windows pela bandeja invertia a caixa da janela mesmo assim | Revisão lógica | Atualizar a caixa só com o que o registro aceitou |
| 62 | Religamento do TCL durante Pause ou edição nunca era reaplicado | Revisão lógica (achado 8) | Suspenso não é cego: o keeper fica devendo e paga ao ficar livre (D33a) |
| 63 | Job da janela na fila atrás de um rescan com renumeração agia no outro monitor | Revisão lógica (achado 9) | Resolver pelo EDID dentro do job; resultados voltam pela identidade (D33b) |
| 64 | Save/Restore da bandeja com a janela ocupada sumiam sem aviso | Revisão lógica (achado 10) | Enfileirar até a operação terminar (D33d) |
| 65 | `Ctrl+S`/fechar até 250 ms após um arraste perdia o arraste; Restore logo após um arraste era desfeito por ele | Revisão lógica (achado 11) | Enviar já o que esperava o debounce e esperar as escritas em voo; o Restore descarta (D33c) |

Lição de processo: editar texto com barra invertida por heredoc no bash corrompe
escapes (`\\.\DISPLAY1`, `\n`, `\d`) — usar as ferramentas de edição.

---

## 9. Qualidade e testes

- **458 testes**, sem hardware, em ~60 s. `tests/fakes.py` simula monitores,
  inclusive o TCL (ignora escrita repetida, tem EDID, conta leituras e escritas).
- A janela é testada **de verdade**: Tk real, transparente, com um worker que
  roda os jobs na hora.
- O worker é testado com thread real e esperas limitadas.
- Testes de regressão cobrem cada bug da seção 8 que é testável sem hardware, e
  mutações foram usadas para confirmar que o teste da troca `DISPLAYn` falha com a
  regra antiga.
- `ruff` (lint + format), regras `E, F, W, I, N, UP, B, A, C4, SIM, RUF`, linha
  de 90; exceções por arquivo documentadas no `pyproject.toml`.

| Arquivo | Foco |
|---|---|
| `test_keeper.py` | detect/rewrite, monitor que some/volta, handle morto, monitor desligado durante evento, ids renumerados, re-enumeração lenta, suspensão e o que ela deve, nenhuma escrita em monitor estável |
| `test_worker.py` | ordem, agrupamento de eventos, intervalo, `nudge`, sobrevivência, parada |
| `test_window.py` | render, escrita, validação, Save via 50, diferenças, Restore, fechar, settings, tema, calibração, mudanças em voo antes de Save/Restore, renumeração no meio da sessão, Apply sem pendências |
| `test_snapshot.py` | arquivo, pareamento, voláteis, upkeep, ping, cache vazio/velho, carry-over |
| `test_buslock.py` | exclusão, reentrância, dono morto |
| `test_edid.py` | blocos reais dos dois monitores, entradas inválidas, quirks |
| `test_settings.py`, `test_autostart.py`, `test_policy.py`, `test_service.py`, `test_capabilities.py`, `test_gpu.py` | demais camadas |

---

## 10. Arquivos e estado em disco

| Caminho | Conteúdo |
|---|---|
| `%LOCALAPPDATA%\monitorctl\profiles.json` | Perfis por monitor (versão 1): identidade, perfil em uso, perfis com valores |
| `%LOCALAPPDATA%\monitorctl\profiles.unreadable.json` | Só existe se um `profiles.json` ilegível foi substituído |
| `%LOCALAPPDATA%\monitorctl\snapshot.json` | Legado (antes dos perfis); lido só enquanto `profiles.json` não existe |
| `%LOCALAPPDATA%\monitorctl\settings.json` | Preferências do app |
| `%LOCALAPPDATA%\monitorctl\monitorctl.log` (+ `.1`) | Log do app |
| `%LOCALAPPDATA%\monitorctl\crash.log` | Pilha Python de um abort do interpretador (`faulthandler`); vazio se nunca houve |
| `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\monitorctl` | Iniciar com o Windows |
| Tarefa agendada `monitorctl-restore` | Legado da 0.1; removida ao ativar o início com o Windows |
| `dist/monitorctl/`, `dist/monitorctl-<v>-win64.zip` | Saída do build (apagada a cada build) |
| `.venv-build/` | Ambiente isolado do PyInstaller |

---

## 11. Limitações, pendências e próximos passos

**Pendências imediatas**

- Validar a **detecção do religamento** do TCL com o app na bandeja; se falhar,
  usar o modo *rewrite*.
- Copiar a pasta para um local fixo e ativar o **início com o Windows**.
- Decidir se o snapshot de 30/09 deve ser substituído pelo estado atual.
- A chave Run aponta para `dist\monitorctl` (religada em 05/10), que o próximo
  build apaga; o interruptor do Gerenciador de Tarefas está desligado desde
  03/10 (D32a). Ligar de novo pelo app, de preferência de uma cópia fora de
  `dist/`.

**Achados da revisão de 03/10 ainda abertos** (pedem decisão do usuário):

1. Uma resposta "não suportado" com bytes zerados (comum logo após ligar)
   marca o controle como sem suporte pela sessão inteira. O Save da bandeja
   tira o controle do perfil, e o keeper deixa de reaplicá-lo.
2. Uma escrita em dois passos que falha no segundo deixa o controle no pivô
   (50), com o cache dizendo o valor final. No modo *detect* nada repete até o
   próximo evento.
3. Um único ping falho conta como "desligou". Ao voltar, há um push completo:
   12 escritas e um flash de cor visível.
4. Um fim abrupto (como o crash do D30) deixa a rampa gama de software na
   tela. O próximo início a adota como "original". O reaplicar também apaga a
   rampa morna do Night Light até ele reescrever.
5. O autostart, a partir de `dist/` ou da fonte, se aponta para lá. Nada
   instala uma cópia fora de `dist/`. O `repair` não remove a tarefa legada.
6. Restore e troca de perfil gravam `input_source`: um perfil salvo em HDMI
   tira o TCL desta entrada.
7. Um monitor esquecível removido de vez provoca uma releitura das
   capabilities a cada 5 min, para sempre (só leitura).

Os achados 8–11 da revisão lógica de 10/10 foram resolvidos em 11/10 (D33,
bugs 62–65).

**Limitações conhecidas**

- Só Windows; saturação/matiz por GPU só NVIDIA.
- Quirks cadastrados à mão; brilho/contraste/preset do TCL não testados quanto
  à volatilidade.
- Perfis sem histórico (salvar substitui os valores do perfil).
- Painéis idênticos sem serial no EDID são ambíguos.
- DPI de sistema (Tk não é por monitor).
- Exe não assinado (SmartScreen pode avisar); sem atualização automática.
- `0xDC` (picture mode) não mapeado por monitor.
- Não é repositório git.

**Ideias levantadas e não feitas** (exigem decisão do usuário): troca de
perfil pela bandeja, por atalho global ou por horário; perfis parciais; rótulos de entrada por modelo (o ASUS chama HDMI de "DVI 2"), atalhos
globais, backend Linux, assinatura de código, instalador.

---

## 12. Glossário

| Termo | Significado aqui |
|---|---|
| **DDC/CI** | Protocolo de comando do monitor sobre I²C no cabo de vídeo |
| **VCP** | Código de um controle no padrão MCCS (ex.: `0x10` brilho) |
| **Capabilities string** | Texto em que o monitor declara o que suporta (incompleto na prática) |
| **EDID** | Bloco de identidade que o monitor envia (fabricante, produto, serial, nome) |
| **hardware_id** | Identidade estável derivada do EDID |
| **Probe / sondagem** | Ler todos os controles do registro de um monitor |
| **Snapshot** | O conjunto de valores salvo por *Save* |
| **Volátil** | Controle que o monitor esquece ao desligar mas continua reportando |
| **PIVOT** | Reasserção passando por 50 |
| **NEIGHBOUR** | Reasserção passando por valor−1 |
| **Upkeep** | Reaplicação automática restrita a voláteis e GPU |
| **Keeper** | Política que decide quando fazer upkeep |
| **detect / rewrite** | Os dois modos da verificação periódica |
| **Carry-over** | Manter valores já lidos de um monitor reconhecido pelo EDID ao re-enumerar |
| **Orçamento de escrita** | Token bucket que limita escritas (40 de pico, 30/min) |
| **Pacing** | Pausas obrigatórias entre operações DDC |
