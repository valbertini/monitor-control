# monitorctl

Controle de monitores via **DDC/CI** no Windows: um app pequeno que fica na
bandeja, mantém as configurações salvas aplicadas e tem sua própria janela —
mais uma CLI para diagnóstico.

Zero dependências de runtime — só a biblioteca padrão do Python, falando com
`dxva2.dll`, `gdi32.dll`, `nvapi64.dll`, `user32.dll` e `shell32.dll` por
`ctypes`. Distribuído como **pasta portátil**: nada é instalado.

> **Idioma:** o código e a interface são em **inglês**. A documentação em `docs/`
> e este README seguem em português.
>
> O **porquê** de cada decisão, o histórico, as medições e os bugs encontrados
> estão em [`docs/PROJETO.md`](docs/PROJETO.md).

```
 ┌────────────── monitorctl.exe (um processo) ───────────────────────────┐
 │                                                                        │
 │  thread principal                         thread do barramento         │
 │  ┌──────────────────────────┐   jobs     ┌──────────────────────────┐  │
 │  │ ícone da bandeja (Win32) │ ─────────► │ Worker                   │  │
 │  │ janela oculta: avisos do │            │  ├ Keeper (quando        │  │
 │  │   Windows (vídeo, energia│ ◄───────── │  │  reaplicar)           │  │
 │  │   sessão, monitor novo)  │ PostMessage│  └ MonitorService        │  │
 │  │ janela Tk (sob demanda)  │            │     lock · pacing ·      │  │
 │  └──────────────────────────┘            │     retry · orçamento    │  │
 │                                          └───────────┬──────────────┘  │
 └──────────────────────────────────────────────────────┼─────────────────┘
                      mutex nomeado do barramento ──────┤ (compartilhado c/ a CLI)
                                              ┌─────────▼──────────┐
                                              │ policy.py allowlist│
                                              │ WindowsDdcBackend  │ dxva2.dll
                                              └─────────┬──────────┘
                                                DDC/CI → I²C @ 0x37 → monitor
```

---

## Uso

### O app

Baixe/gere `monitorctl-<versão>-win64.zip`, extraia a pasta `monitorctl` para
onde quiser (ex.: `%LOCALAPPDATA%\Programs\monitorctl`) e rode
**`monitorctl.exe`**.

- **Duplo clique** abre a janela. **Fechar a janela** a esconde: o app continua
  na bandeja. Rodar `monitorctl.exe` de novo só traz a janela de volta — nunca há
  duas cópias.
- **Ícone da bandeja**: clique abre a janela; botão direito mostra o menu —
  *Restore saved settings*, *Save current settings*, *Pause automatic re-apply*,
  *Start with Windows*, *Exit*. Passe o mouse para ver o estado e a última ação.
- **Iniciar com o Windows**: marque em *Settings* (ou no menu da bandeja). Fica
  na chave `Run` do usuário — aparece em *Gerenciador de Tarefas → Aplicativos
  de inicialização*, onde também dá para desligar. Se a pasta for movida, o app
  corrige o caminho sozinho na próxima vez que abrir. Abrir *outra* cópia que
  ainda existe (o build em `dist/`, o código-fonte) não mexe no registro; para
  trocar de cópia, desmarque e marque de novo nela.

### A janela

- **Uma aba por monitor**, em **cartões lado a lado** (quantas colunas couberem),
  do que mais se mexe ao que quase nunca se mexe: **brilho e contraste** →
  imagem (saturação, nitidez) → **ganho R/G/B** (brancos) → **offset R/G/B**
  (pretos) → cor por matiz → temperatura → modo de imagem → o que **o PC aplica
  por cima** (GPU) → áudio, geometria, **entrada e idioma por último**. Cada
  cartão tem um ícone, uma linha dizendo o que aquilo muda e uma etiqueta:
  **monitor** (o hardware do monitor guarda) ou **PC software** (a placa de vídeo
  altera o sinal, só enquanto o monitorctl roda). Pesquisa e decisões em
  [`docs/PESQUISA-UI.md`](docs/PESQUISA-UI.md) e D26/D28.
- **Auto apply** (na barra, ligado por padrão): cada mudança vai para o monitor
  na hora. Desligado, as mudanças ficam **pendentes** (nome em destaque,
  "Apply (n)"): **Apply** escreve todas, **Discard** desfaz. Trocar de perfil com
  ele desligado só mostra os valores do perfil, sem aplicar; Save e New aplicam
  antes de salvar; fechar a janela pergunta o que fazer com as pendências. Uma
  pendência tem um • no nome, e o ↺ ao lado dela a cancela.
- **Controles de uma cor ficam naquela cor**: ponto e slider vermelhos no ganho
  vermelho, e assim por diante (seis eixos na ordem do círculo cromático). O nome
  continua escrito — a cor nunca é a única pista.
- O que o monitor **não tem** sai do caminho: fica num cartão recolhido no fim,
  "Not available on this monitor (n)", que lista os nomes ao abrir.
- **↺ ao lado de um controle** que difere do perfil em uso; clicar devolve o
  valor salvo só daquele controle.
- **Slider + campo numérico** em cada controle contínuo; lista para os de
  escolha. Campo inválido fica vermelho e volta ao último valor confirmado.
  Arrastar o slider só escreve quando o valor para por 250 ms.
- **Legenda ao passar o mouse** no nome do controle (explicação + código VCP);
  pelo teclado, a mesma legenda aparece na barra de status.
- **Perfis por monitor**, **Save / Restore**: ver
  [Salvar, restaurar e perfis](#salvar-restaurar-e-perfis). O rótulo da barra
  de ferramentas acende **unsaved changes**; passe o mouse para ver *quais*
  valores diferem.
- **Slider fino** que vai direto para onde você clica. A **roda do mouse só
  rola a página**: nenhum controle muda de valor por ela.
- **Fechar com alterações não salvas** pergunta: salvar, voltar ao salvo, ou
  cancelar.
- **Calibration**: um guia de 8 passos para calibrar **a olho**, cada um com o
  padrão, o que olhar e qual controle mexer — preparação → brilho → altas-luzes
  (barras 235–254 **piscando** sobre branco: ajuste o Contrast até todas
  piscarem) → sombras (barras 1–25 piscando sobre preto) → equilíbrio de cinza
  (ganho e offset) → gama (linhas de 1 px; 186 = 2.2) → gradientes → cores
  (compare com uma foto ou app que você conhece bem). Arraste a janela até o
  monitor em ajuste e use **F11** para tela cheia; ← → trocam de passo. Pesquisa
  em [`docs/PESQUISA-CALIBRACAO.md`](docs/PESQUISA-CALIBRACAO.md).
- **Settings**: intervalo e modo da reaplicação automática, pausa, aplicar ao
  iniciar, iniciar com o Windows, pasta de dados.
- Tema **escuro por padrão** (barra de título inclusa), ☀/☾ alterna. A janela
  lembra tamanho e posição.
- Atalhos: `Ctrl+S` salvar, `F5` reler tudo, `Esc` esconder, `Ctrl+Tab` trocar
  de aba.

### A CLI

`monitorctl-cli.exe` (ou `python -m monitorctl` a partir do código):

```bash
monitorctl-cli list                      # monitores, EDID, recursos e valores
monitorctl-cli list --all                # inclui os não suportados
monitorctl-cli features                  # catálogo completo, por categoria
monitorctl-cli caps 1                    # capabilities string de um monitor
monitorctl-cli get 1 brightness          # lê um recurso
monitorctl-cli set 1 brightness 60       # escreve um recurso
monitorctl-cli save                      # guarda as configurações atuais
monitorctl-cli restore                   # volta para o último save
monitorctl-cli autostart enable|disable|status
monitorctl-cli app                       # roda o app da bandeja
```

O monitor pode ser referenciado por posição (`1`, `2`), por `id` (`display1`) ou
por nome (`VS248`). CLI e app podem rodar juntos: eles se revezam no
barramento, uma transação de cada vez (ver [Segurança do hardware](#segurança-do-hardware)).

`monitorctl.exe --quit` pede para a cópia em execução encerrar — útil antes de
substituir a pasta por uma versão nova.

### A partir do código

Requer **Python 3.11+** e **Windows**. `launch_app.pyw` roda o app sem console
(duplo clique); `python -m monitorctl ...` é a CLI.

---

## Manter as configurações aplicadas

Este é o motivo de o app existir. O TCL 25G64 **esquece ganhos e offsets RGB
quando é desligado no botão, mas continua reportando os valores esquecidos**, e
ignora a escrita de um valor igual ao que reporta. Ler de volta nunca revela a
perda. Detalhes e testes em
[`docs/monitors/tcl-25g64.md`](docs/monitors/tcl-25g64.md#51-ganhos-e-offsets-voláteis-2026-09-30).

Isso é registrado por modelo em `monitorctl/ddc/quirks.py`, indexado pelo EDID
(`TCL-1003`): os controles **voláteis**. Para eles a escrita sempre passa por um
valor intermediário, para que a escrita final seja uma mudança de verdade:

| Quando | Intermediário | Por quê |
|---|---|---|
| **Save** e **Restore** | **50** (ou vizinho, se o alvo for 50) | Mudança grande e inequívoca: o que foi salvo passa a ser o que a tela mostra |
| Monitor que **voltou** (detectado) | **50** | A tela acabou de ligar; não há o que esconder |
| Reaplicação **periódica** (modo *rewrite*) | **valor−1** | Invisível na tela a cada rodada |
| Digitar no campo o valor que o monitor já reporta | **valor−1** | O mínimo para a escrita não ser descartada |

As duas escritas são reservadas juntas no orçamento: parar no meio deixaria o
controle no valor intermediário.

### Quando o app reaplica

O `Keeper` (`app/keeper.py`) decide; ele **só escreve controles voláteis e
ajustes de GPU**. Um monitor que guarda as próprias configurações, como o ASUS,
**nunca recebe escrita automática** — um timer de um minuto não pode virar um
loop de desgaste de EEPROM.

| Gatilho | O que faz |
|---|---|
| **Avisos do Windows** — monitor conectado, configuração de vídeo mudou, retorno da suspensão, telas religadas, sessão desbloqueada | Espera 3 s o barramento assentar (agrupando rajadas), re-enumera e reaplica via 50 |
| **Início do app** (se *Apply saved settings when monitorctl starts*) | Idem; 10 s de espera quando iniciado com o Windows |
| **Verificação periódica**, modo **detect** (padrão) | **Só lê**: uma leitura por monitor volátil para ver se ele responde. Quem parou de responder e voltou recebe os valores via 50. Sem monitor volátil conectado, zero tráfego |
| **Verificação periódica**, modo **rewrite** | Reescreve os voláteis via valor−1 a cada rodada — para um monitor cujo religamento a detecção não perceba |

O intervalo é configurável (1 min a 24 h). Enquanto a janela tem **alterações não
salvas**, a reaplicação automática fica suspensa — senão o timer desfaria cada
slider que você move. *Pause automatic re-apply* suspende tudo.

> **A validar:** a detecção do religamento do TCL ainda não foi testada desligando
> o monitor no botão com o app aberto. O log
> (`%LOCALAPPDATA%\monitorctl\monitorctl.log`) registra cada aviso do Windows
> (`Windows reported: ...`), cada monitor que parou de responder e cada
> reaplicação. Se a detecção falhar, o modo *rewrite* cobre o caso.

Desgaste: no TCL as escritas vão para RAM (é exatamente por isso que ele
esquece), então não há EEPROM a gastar. Mesmo supondo que fossem, 2 escritas por
controle a cada religamento dariam décadas para os 100 mil ciclos.

---

## Recursos

**48 recursos em 13 categorias** — 43 pelo monitor (DDC/CI) e 5 pela GPU. A
allowlist é fechada: só o que está no registro pode ser escrito.

| Categoria | Recursos |
|---|---|
| Brightness & Contrast | brightness `0x10`, contrast `0x12`, backlight `0x13` |
| Image | sharpness `0x87`, colour saturation `0x8A`, hue `0x90`, aspect `0x86`, picture mode `0xDC` |
| Colour Temperature | preset `0x14`, fine `0x0C` |
| White Balance — Gain | R `0x16`, G `0x18`, B `0x1A` |
| Black Level — Offset | R `0x6C`, G `0x6E`, B `0x70` |
| Six-Axis Saturation | R `0x59`, Y `0x5A`, G `0x5B`, C `0x5C`, B `0x5D`, M `0x5E` |
| Six-Axis Hue | R `0x9B`, Y `0x9C`, G `0x9D`, C `0x9E`, B `0x9F`, M `0xA0` |
| Input | input source `0x60` |
| Audio | volume `0x62`, mute `0x8D`, treble `0x8F`, bass `0x91`, balance `0x93` |
| Geometry (analógico) | auto setup `0x1E`, H/V position `0x20`/`0x30`, H/V size `0x22`/`0x32`, clock `0x0E`, phase `0x3E` |
| On-Screen Display | language `0xCC`, orientation `0xAA` |
| **GPU Colour (driver)** | saturation (vibrance), hue angle |
| **GPU Tone Curve (software)** | brightness, contrast, gamma |

`monitorctl-cli features` imprime a lista completa com as descrições.

### A camada GPU

Nem tudo que parece "configuração de monitor" é. Quando o firmware não
implementa o código MCCS, o ajuste ainda pode ser feito um passo antes, na GPU.
Cada controle diz de onde vem: código VCP (`0x10`) para o monitor, ou uma
etiqueta (`NVIDIA driver`, `software LUT`) para a GPU.

| Mecanismo | O que faz | Limite |
|---|---|---|
| **NVAPI** (`nvapi64.dll`) | Saturação (Digital Vibrance) e matiz | Só NVIDIA. Usa IDs privados — ver abaixo |
| **Gamma ramp** (`gdi32.dll`) | Brilho, contraste e gama por software | LUT 1D por canal: **não faz saturação nem matiz**, que exigem misturar canais |

Ressalvas: o ajuste é no **sinal**, não no painel (uma captura de tela sai sem
o efeito); o gamma ramp é **estado compartilhado** com f.lux, Night Light e o
painel da NVIDIA, e **reseta** em reload de driver, hot-plug e reboot — é por
isso que o app o reaplica nesses eventos. Com brilho, contraste e gama por
software **em 50, o app não escreve rampa nenhuma**: a curva de um perfil ICC,
o Night Light e o f.lux ficam intactos. Ao sair do neutro ele guarda a rampa que
encontrou e a devolve quando você volta tudo para 50 — ou quando o app fecha (os
ajustes por software valem enquanto o monitorctl roda). Saturação e matiz da
NVIDIA salvos no neutro também não são reaplicados automaticamente, para não
brigar com o painel da NVIDIA. Nada disso toca o I²C nem gasta orçamento de
escrita.

#### Por que a NVAPI exige cuidado

`nvapi64.dll` exporta **uma única função**, `nvapi_QueryInterface`; todo o resto
se obtém por ID mágico de 32 bits, e os de Digital Vibrance e Hue são do conjunto
privado. Chamar um ponteiro com o protótipo errado derruba o processo. Três
travas: todo ID precisa resolver para ponteiro não-nulo; o getter é chamado e o
descritor conferido (`min <= default <= max`, máximo pequeno) antes de expor
qualquer escrita — senão o backend vira somente-leitura; e toda escrita é
confirmada por leitura. Validado numa RTX 4070 (driver 32.0.16.1062).

### A capabilities string é um piso, não um teto

O `monitorctl` **sonda todo código do registro**, anunciado ou não, e usa a
capabilities string só para rótulos e valores permitidos. Os dois monitores
testados implementam códigos que não anunciam — o ASUS VS248 saiu de 7 para
**24** recursos controláveis. A sondagem completa custa ~11 s para dois
monitores, uma vez; o resultado fica em cache e sobrevive a re-enumerações
(ver abaixo). *Rescan* (`F5`) refaz.

Levantamento completo por monitor em **[`docs/monitors/`](docs/monitors/)**.

---

## Salvar, restaurar e perfis

Cada monitor tem **seus próprios perfis** (ex.: *Calibrated*, *Night*,
*Games*) e **um perfil em uso**. Um perfil guarda todos os controles
restauráveis daquele monitor, inclusive os ajustes de GPU. Os perfis em uso de
todos os monitores são o que o Restore aplica e o que a reaplicação automática
mantém.

- **Save** guarda o valor atual de cada monitor **no perfil que ele está
  usando** (um monitor sem perfil ganha o *Default*) e em seguida **empurra os
  controles voláteis via 50**, para que o que foi salvo seja o que a tela
  mostra. Na janela, grava o que a janela mostra; **no menu da bandeja, relê
  os monitores antes**, porque lá não há valores à vista e um ajuste feito
  pelos botões do monitor não chega ao cache.
- **Restore** relê cada controle do perfil em uso e **escreve só o que mudou**,
  mais os voláteis (via 50), que sempre são escritos porque a leitura deles não
  vale. A releitura é o que pega uma mudança feita **pelos botões do monitor**.
- **Na aba de cada monitor**, a linha *Profile* tem:
  - o **seletor**: escolher outro perfil o aplica naquele monitor na hora
    (semântica de Restore). Com alterações não salvas, pergunta antes:
    salvar no perfil atual, descartar ou ficar;
  - **New…**: salva os valores atuais como um perfil novo e passa a usá-lo;
  - **Rename…**: renomeia o perfil em uso, sem escrever no monitor;
  - **Delete ▾**: lista só os perfis **fora de uso**. O perfil em uso não pode
    ser apagado; troque antes;
  - à direita, a hora do último save daquele perfil ou **unsaved changes**.
- Trocar o perfil de um monitor **nunca escreve em outro**. Os nomes são
  únicos por monitor, sem diferenciar maiúsculas, com até 40 caracteres.

Os perfis ficam em `%LOCALAPPDATA%\monitorctl\profiles.json`. App e CLI
compartilham o arquivo: `monitorctl-cli save` grava no perfil em uso de cada
monitor, e o app percebe a mudança sem reiniciar. Na primeira execução depois da
atualização, o `snapshot.json` antigo vira o perfil *Default* de cada monitor.
Ele não é apagado, só deixa de ser lido.

| Decisão | Por quê |
|---|---|
| O arquivo **não é confiável** | Todo valor lido do disco passa pela mesma `check_write` de qualquer slider. Um perfil editado à mão não consegue pedir nada que a UI não pudesse |
| Escrita atômica: arquivo temporário no mesmo diretório + `os.replace` (`fileio.py`) | Um save interrompido deixa o arquivo antigo **ou** o novo, nunca truncado. Vale também para as configurações do app |
| Arquivo corrompido = "nada salvo" | É preservado: antes do próximo save ele é guardado como `profiles.unreadable.json`, em vez de sobrescrito |
| Perfil ativo que aponta para nome inexistente | Aquele monitor fica sem perfil em uso; os outros perfis continuam |
| Monitor desconectado mantém os perfis | O Save de um monitor não toca nos perfis dos outros, conectados ou não |
| Falha é **por valor**, não do lote | Monitor desconectado, controle que parou de responder, valor recusado pela política: cada um vira uma linha no relatório |
| Orçamento estourado interrompe o restante | As próximas escritas falhariam igual |
| Ordem de escrita vem do registro, não do arquivo | Controle grosso antes do fino que ele reseta: *colour preset* antes dos ganhos |
| Save e Restore com cache sem valores leem antes | O app re-enumera sem sondar a cada evento; sem isso o Save gravaria um snapshot vazio e o Restore acharia que tudo mudou |
| Monitor é reencontrado por EDID → nome → `device` → `id` | O EDID é o único identificador que pertence ao **painel**; `device` e `id` pertencem à **porta**, e o Windows renumera `DISPLAYn` — o TCL já foi `DISPLAY1`, `DISPLAY2` e `DISPLAY1` de novo durante este projeto. Cada identificador só decide quando é único **dos dois lados**; dois EDIDs diferentes vetam o par |

---

## Consumo de recursos

Medido com o `.exe` empacotado, dois monitores, RTX 4070:

| Situação | Memória privada residente | Observação |
|---|---|---|
| Na bandeja, depois da aplicação inicial | **1–3 MiB** | Páginas devolvidas ao Windows após cada ação |
| Janela aberta | ~26 MiB | Tcl/Tk carregado |
| Janela fechada de novo | ~1 MiB | O Tk fica carregado (abrir de novo é instantâneo), mas suas páginas voltam ao Windows |
| CPU parado, modo *detect* | ≈ 0 | Uma leitura DDC por minuto por monitor volátil; nenhuma sem monitor volátil |

O que sustenta isso:

- **Tk só é importado quando a janela é aberta pela primeira vez.** Até lá o
  processo é o interpretador mais `ctypes`.
- **Nada faz polling.** A thread do barramento dorme numa fila com timeout até o
  próximo evento ou verificação; a thread principal dorme em `GetMessage`.
  Resultados voltam por `PostMessageW`, não por timers.
- **Um processo, uma instância** (mutex de sessão). Pasta portátil, não exe
  único: um exe único se extrai para `%TEMP%` a cada início e mantém um
  segundo processo vivo.
- Bytecode com `optimize=2` e sem módulos que nada usa (`ssl`/`hashlib` sozinhos
  custariam 5 MB de `libcrypto`). Pasta: 22,7 MiB; zip: 10,8 MiB.

---

## Segurança do hardware

A pesquisa que embasa estas decisões está em
[`docs/PESQUISA-DDC-CI.md`](docs/PESQUISA-DDC-CI.md).

| Defesa | Onde | Contra o quê |
|---|---|---|
| Allowlist de VCP; faixa `0xE0`–`0xFF` recusada sem override | `ddc/policy.py` | o único caminho documentado para dano permanente |
| `0x01`, `0x04`, `0x05`, `0x06`, `0x08`, `0xB0`, `0xCA`, `0xD6` bloqueados | `ddc/policy.py` | reset de fábrica, escrita em EEPROM, travar os botões físicos, monitor que não acorda |
| Token bucket (40 de pico, 30/min) | `ddc/policy.py` | desgaste de EEPROM por rajada |
| Debounce de 250 ms nos sliders | `app/window.py` | idem, na origem |
| Reaplicação automática só em controles voláteis e GPU | `app/keeper.py`, `ddc/service.py` | timer virando loop de desgaste |
| Restore escreve só o que derivou; valor do snapshot passa pela política | `ddc/service.py` | idem, num lote — e snapshot editado à mão não vira bypass |
| Clamp ao máximo que o monitor informou | `ddc/policy.py` | valor fora de faixa |
| Lock serializando todo I/O DDC **no processo** | `ddc/service.py` | barramento I²C não é reentrante |
| **Mutex nomeado** em volta de cada operação, **entre processos** | `ddc/buslock.py` | app e CLI intercalando transações I²C. Pausa pós-operação feita dentro do lock; dono que morreu não trava os outros |
| Pacing de 40/50/100 ms entre operações | `ddc/service.py` | respostas corrompidas, travamento do barramento |
| 1 retry só em falha transiente classificada | `ddc/service.py` | glitch de I²C sem mascarar erro real |
| Leitura de confirmação após escrita | `ddc/service.py` | a API do Windows reporta sucesso falso |

Os dois monitores testados **expõem `0xD6` (power mode)** — o TCL nem anuncia.
É o código que deixa monitor em deep power off. Bloqueado.

A interface web foi removida na 0.2.0: sem servidor HTTP, não há porta local
nem superfície de rede a defender.

---

## Arquitetura do app

| Módulo | Papel |
|---|---|
| `app/main.py` | Ponto de entrada: log em arquivo rotativo, instância única, `--startup`, `--quit` |
| `app/tray.py` | Une tudo na thread principal: ícone, menu, avisos do Windows, janela sob demanda |
| `app/win32.py` | `ctypes`: ícone da bandeja (versão 4, funciona por teclado), janela oculta *top-level* (janelas *message-only* não recebem `WM_DISPLAYCHANGE` nem `TaskbarCreated`), notificações de monitor/energia/sessão, DPI, trim |
| `app/worker.py` | A única thread que fala com o barramento: jobs em ordem, eventos agrupados, verificações periódicas; sobrevive a qualquer exceção |
| `app/keeper.py` | Política de reaplicação, lógica pura — testada sem thread, janela ou monitor |
| `app/window.py`, `calibration.py`, `theme.py` | A janela Tk |
| `app/settings.py` | Preferências, gravadas atomicamente e lidas campo a campo (um campo inválido não descarta os outros) |
| `autostart.py` | Chave `Run` do usuário; corrige o caminho se a instalação registrada sumiu; remove a tarefa agendada da 0.1 |
| `ddc/profiles.py` | Perfis por monitor: o livro imutável, o arquivo `profiles.json`, a migração do `snapshot.json` |

Um laço de mensagens, dois donos: até a janela ser aberta, a thread principal
roda um `GetMessage` simples. Ao abrir, entrega a thread ao `Tk.mainloop()`, que
despacha todas as mensagens da thread — inclusive as da janela oculta —, então
a bandeja continua respondendo e o Tk nunca roda fora da thread principal.

Resiliência: o ícone volta sozinho quando o Explorer reinicia (`TaskbarCreated`);
exceções de callbacks, threads e do Tk vão para o log em vez de sumir num
console que não existe; logoff remove o ícone; configurações e perfis
corrompidos viram padrão sem derrubar o app.

---

## Gerar o pacote

```bash
python tools/build.py
```

Cria `.venv-build` com o PyInstaller fixado (6.22.3), gera o recurso de versão a
partir de `monitorctl.__version__`, roda `packaging/monitorctl.spec` e compacta
`dist/monitorctl/` em `dist/monitorctl-<versão>-win64.zip`. A pasta tem
`monitorctl.exe` (sem console), `monitorctl-cli.exe` e `_internal/`. Se houver
uma cópia rodando a partir de `dist`, o build pede para ela encerrar antes.

O ícone é desenhado por `tools/make_icon.py` (requer Pillow só para isso), cada
tamanho separadamente para ficar nítido a 16 px.

---

## Testes

```bash
python -m unittest discover -s tests -t .
python -m ruff check . && python -m ruff format --check .
```

398 testes, sem hardware — o backend é substituído por um falso
(`tests/fakes.py`), que também simula o TCL: ignora escrita de valor repetido e
conta leituras, para afirmar "zero tráfego".

| Arquivo | Cobre |
|---|---|
| `test_keeper.py` | detect/rewrite, monitor que some e volta, re-enumeração lenta, suspensão por pausa/edição, nenhuma escrita num monitor que guarda configurações |
| `test_worker.py` | jobs em ordem numa thread, eventos agrupados com o motivo mais específico, verificações no intervalo, sobrevivência a exceções, parada sem interromper escrita |
| `test_window.py` | a janela Tk de verdade (transparente): render, ordem dos controles, roda do mouse não muda valor, clique no slider, escrita, valor inválido, Save via 50, aviso de alteração, Restore, perfis (criar, trocar com pergunta, renomear, apagar), fechar e sair com pergunta, configurações, tema, calibração |
| `test_profiles.py` | nomes, livro (criar/trocar/renomear/apagar, recusas sem efeito), arquivo (corrompido guardado à parte, outro processo, migração do snapshot antigo), serviço (troca escreve só naquele monitor, Save relendo, leitura falha não vira "não suportado") |
| `test_snapshot.py` | troca de `DISPLAYn`, painel diferente na mesma porta, voláteis via 50/±1, upkeep, cache vazio, cache velho, carry-over por EDID |
| `test_buslock.py` | exclusão entre "processos", reentrância, dono que morreu |
| `test_settings.py`, `test_autostart.py`, `test_edid.py`, `test_gpu.py`, `test_policy.py`, `test_service.py`, `test_capabilities.py` | o resto |

---

## Ferramentas

`tools/survey_monitor.py` faz um levantamento **somente-leitura** de todo código
VCP de cada monitor, pulando a faixa reservada. É o que gera os dados de
[`docs/monitors/`](docs/monitors/).

---

## Estado atual e limitações conhecidas

Validado em hardware real: TCL 25G64 e ASUS VS248, RTX 4070 — app empacotado
iniciando na bandeja, reaplicando os 6 controles voláteis do TCL e os ajustes
de GPU, janela abrindo e fechando, instância única, `--quit`.

- **Detecção do religamento do TCL ainda não validada** (ver acima). O modo
  *rewrite* é o plano B.
- **Só Windows.** As interfaces `DdcBackend` e `GpuBackend` estão prontas para
  outros backends; ninguém implementou.
- **GPU só NVIDIA** para saturação e matiz.
- **Quirks são cadastrados à mão.** Um monitor novo que esquece valores só é
  tratado depois de entrar em `ddc/quirks.py`.
- **Perfis sem histórico.** Salvar num perfil substitui os valores dele.
- **Painéis idênticos sem serial no EDID** compartilham a identidade; o
  pareamento trata isso como ambíguo e cai para a porta.
- **Tk 8.6 não é DPI-aware por monitor.** O app usa DPI de sistema: nítido no
  monitor principal; em outro monitor com escala diferente, o Windows estica.
- **Sem atualização automática** nem assinatura de código: o SmartScreen pode
  avisar na primeira execução de um exe não assinado.
- **`0xDC` (picture mode) não verificado.**

### Quirks reais encontrados durante a validação

1. **Handle de valor `0` é válido.** `GetPhysicalMonitorsFromHMONITOR` devolve
   um índice pequeno, não um ponteiro; `ctypes` converte o primeiro em `None`.
2. **O TCL 25G64 emite capabilities fora do formato**: sem o parêntese de
   abertura, e com `60(11120F)` em vez de `60(11 12 0F)`.
3. **`SO_REUSEADDR` no Windows permite sequestrar uma porta** (da época da
   interface web).
4. **O TCL 25G64 esquece ganhos e offsets e mente sobre isso**, e ignora
   escrita repetida. `SaveCurrentSettings` (`0xB0`) devolve sucesso e não grava.
5. **O registro guarda EDID de toda porta por onde o monitor já passou.** O EDID
   é lido da instância ligada *agora* ao `DISPLAYn`, via `EnumDisplayDevicesW`.
6. **Colunas vazias num grupo `uniform` do grid do Tk ocupam a mesma largura
   das cheias** — 8 colunas reservadas viraram 3.300 px de layout invisível.
7. **Registrar a notificação de estado da tela entrega o estado atual na hora.**
   Tratar isso como "telas religadas" disparava uma reaplicação falsa ao abrir.

---

## Licença

MIT. Nenhum código de terceiros foi copiado — em particular, o ddcutil é
GPL-2.0+ e foi lido apenas como referência conceitual. A procedência de cada
ideia está registrada em [§4 da pesquisa](docs/PESQUISA-DDC-CI.md).
