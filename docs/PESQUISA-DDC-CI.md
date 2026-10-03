# Pesquisa: DDC/CI, software existente e riscos de hardware

> Notas de pesquisa levantadas em 2026-09-19 para embasar o `monitorctl`.
> Tudo aqui é verificável: há link ou referência de código-fonte para cada afirmação.

---

## 1. Como o DDC/CI realmente funciona

### 1.1 A pilha, de baixo para cima

O cabo de vídeo (VGA/DVI/HDMI/DisplayPort) sempre carregou, além do sinal de imagem,
um par de fios de **I²C** — os pinos DDC. Historicamente serviam só para o PC ler o
**EDID** do monitor (resolução nativa, timings, nome do modelo) no endereço I²C `0x50`.

O **DDC/CI** (*Display Data Channel / Command Interface*) reaproveita esse mesmo barramento
para comunicação **bidirecional**, no endereço I²C **`0x37`**. Sobre ele roda o
**MCCS** (*Monitor Control Command Set*), que é a linguagem de comandos propriamente dita.

```
Aplicação  ──► MCCS (o "quê": brilho = 50)
               └─► DDC/CI (o "como": enquadra em pacotes, checksum, timing)
                     └─► I²C @ 0x37 (transporte elétrico)
                           └─► pinos DDC do cabo HDMI/DP/DVI
                                 └─► scaler/firmware do monitor
```

Padrões: DDC/CI 1.1 (out/2004) e a série V2, cuja última revisão é a **2.2a (jan/2011)**.
Ou seja: **é um padrão congelado há mais de uma década.** Isso explica muita coisa sobre
a qualidade irregular das implementações.

### 1.2 O formato do pacote (nível fio)

Um *Get VCP Feature* típico, em bytes:

| Byte | Valor | Significado |
|---|---|---|
| 0 | `0x6E` | endereço de destino (monitor, `0x37 << 1`) |
| 1 | `0x51` | endereço de origem (host) |
| 2 | `0x82` | comprimento (`0x80 \| 2` bytes de dados) |
| 3 | `0x01` | opcode *Get VCP Feature* |
| 4 | `0x10` | o código VCP pedido (`0x10` = brilho) |
| 5 | `xor` | checksum |

A resposta volta com `0x6E`/`0x88`/opcode `0x02`, um *result code*, o código do recurso,
o tipo, e **valor atual + valor máximo** em 16 bits cada.

Dois detalhes que definem toda a engenharia em cima disso:

- **O valor é de 16 bits.** O máximo absoluto transmissível é `65535`. Na prática o
  monitor devolve o próprio teto (quase sempre `100`) e ignora/satura acima dele.
- **Não existe handshake nem controle de fluxo.** O host escreve, espera um tempo fixo,
  e lê. Se ler cedo demais, recebe lixo. Daí a obsessão dos projetos sérios com *timing*.

### 1.3 VCP codes — o dicionário

Cada "configuração" é um **VCP code** de 1 byte. Extraí a tabela canônica direto do
`src/vcp/vcp_feature_codes.c` do ddcutil. Os que interessam na prática:

| Código | Nome | Tipo | Observação |
|---|---|---|---|
| `0x10` | Brightness (Luminance) | Contínuo | o mais universal |
| `0x12` | Contrast | Contínuo | quase universal |
| `0x14` | Select Color Preset | Não-contínuo | valores 3..10 ≈ 4000K..11500K |
| `0x16/0x18/0x1A` | Video Gain R/G/B | Contínuo | calibração de branco |
| `0x60` | Input Source | Não-contínuo | `0x0F`=DP1, `0x11`=HDMI1, `0x12`=HDMI2 … |
| `0x62` | Audio Speaker Volume | Contínuo | só se o monitor tiver alto-falante |
| `0x6C/0x6E/0x70` | Video Black Level R/G/B | Contínuo | |
| `0x72` | Gamma | Não-contínuo | |
| `0x87` | Sharpness | Contínuo | |
| `0x8D` | Audio Mute | Não-contínuo | |
| `0xAC/0xAE` | Horizontal/Vertical Frequency | **Read-only** | telemetria |
| `0xD6` | Power Mode (DPMS) | Não-contínuo | **perigoso — ver §3.3** |
| `0xDF` | VCP Version | Read-only | |

E os que **não** se deve tocar sem saber muito bem o que está fazendo:

| Código | Nome | Por quê |
|---|---|---|
| `0x01` | Degauss | só CRT; em LCD é indefinido |
| `0x04` | Restore Factory Defaults | destrói sua calibração |
| `0x05`/`0x06`/`0x08` | Restore brightness/geometry/color defaults | idem |
| `0xB0` | Settings (store/restore) | **grava na EEPROM** — ver §3.2 |
| `0xE0`–`0xFF` | **Reservado ao fabricante** | modos de fábrica/serviço — ver §3.1 |

### 1.4 É igual para todo monitor? **Não.**

Essa é a resposta curta e importante. O padrão define o *vocabulário*, mas cada firmware
implementa um subconjunto arbitrário dele, com liberdades criativas. O que varia:

**(a) Quais códigos existem.** O monitor declara o que suporta numa *capabilities string*,
obtida por um comando próprio. Formato aninhado, tipo:

```
(prot(monitor)type(LCD)model(VS248)cmds(01 02 03 07 0C E3 F3)
 vcp(02 04 06 08 10 12 14(05 08 0B) 16 18 1A 60(01 03 11) AC AE B6 C6 C8 DF)
 mswhql(1)asset_eep(40)mccs_ver(2.1))
```

Dentro de `vcp(...)`, códigos soltos são contínuos; códigos com parênteses
(ex.: `14(05 08 0B)`) são não-contínuos e listam **exatamente** os valores aceitos.

**(b) A string pode vir corrompida.** É transmitida em fragmentos sobre I²C sem correção
de erro. O próprio comentário do `monitorcontrol` admite: *"No error checking for the
string being valid. String can have bit errors or dropped characters."* Qualquer parser
sério precisa ser tolerante a lixo.

**(c) A faixa não é 0–100.** A API devolve mínimo e máximo *reais*. O Monitorian documenta
isso explicitamente: *"Raw current brightness (not always 0 to 100)"*. Assumir 0–100 é bug.

**(d) Como o monitor sinaliza "não suporto".** O caminho correto é um *result code* de
erro no pacote DDC. Mas:
- Dell AW3418DW responde com **erro de I²C (`EIO`)** em vez de resposta DDC válida;
- outros devolvem **todos os bytes zerados** — o ddcutil tem uma flag inteira só pra isso
  (`DREF_DDC_DOES_NOT_INDICATE_UNSUPPORTED`);
- HP Z22i: o recurso `0x52` nunca devolve o valor de terminação esperado.

**(e) Quirks por modelo.** Alguns Iiyama exigem *Save Current Settings* imediatamente após
cada `setvcp`, senão o valor não persiste. Vários LG ultrawide não têm número de série no
EDID — dois monitores idênticos ficam indistinguíveis.

**(f) O caminho GPU/driver importa.** DisplayPort **MST** (daisy-chain, docks) é
notoriamente problemático: falhas de leitura de EDID e travamentos irrecuperáveis após
suspend/resume. Em Linux, o driver `ddcci` carregado ou o driver proprietário da Nvidia
causam `EBUSY`. Em Windows o acesso é intermediado pelo driver WDDM, o que esconde
parte do problema — mas também esconde o diagnóstico.

---

## 2. Panorama do software existente

### 2.1 Open source

| Projeto | Lic. | Plataforma | Stack | O que faz bem | Onde peca |
|---|---|---|---|---|---|
| **[ddcutil](https://github.com/rockowitz/ddcutil)** | GPL-2.0+ | Linux/BSD | C | **A referência técnica absoluta.** Fala I²C direto, sem intermediário. Sleep multiplier dinâmico, retry adaptativo, tabela de quirks por modelo, parser de capabilities completo, `libddcutil`. | Sem GUI. Sem Windows. Curva de aprendizado alta. |
| **[Monitorian](https://github.com/emoacht/Monitorian)** | MIT | Windows | C#/WPF | **A melhor implementação Windows.** <1 MB. Trata os códigos de erro `ERROR_GRAPHICS_DDCCI_*`, faz retry em falha transiente, `SafeHandle` para os handles, parser de capabilities robusto. | Escopo deliberadamente pequeno: brilho + contraste. Máx. 4 monitores na versão livre; extras são add-on pago na Store. |
| **[Twinkle Tray](https://github.com/xanderfrangos/twinkle-tray)** | MIT | Windows | Electron | Mais features: hotkeys, agendamento por horário, perfis por aplicativo, contraste e volume. UI agradável. | **~88 MB de instalador** (Electron) e o consumo de RAM correspondente. |
| **[monitorcontrol](https://github.com/newAM/monitorcontrol)** | MIT | Win/Linux | Python | API Python limpa, multiplataforma, boa para script. | Wrapper fino: sem retry, sem *pacing*, sem parser de capabilities, e os `ctypes` vão **sem `argtypes`/`restype`** — frágil. |
| **[ClickMonitorDDC](https://clickmonitorddc.com/)** | freeware | Windows | C++ | <2 MB, scripting poderoso, veterano. | **Não é open source** (só gratuito), descontinuado, UI datada. |
| **[MonitorControl](https://github.com/MonitorControl/MonitorControl)** | MIT | macOS | Swift | Padrão de facto no macOS livre. | Só macOS. |

### 2.2 Pagos

| Produto | Preço | Diferenciais reais |
|---|---|---|
| **[Lunar](https://lunar.fyi/)** | free / Pro ~US$23 | *Sub-zero dimming* via tabela de gama (escurece além do 0% do hardware), modo Sensor (sensor de luz externo), modo Location (nascer/pôr do sol), Sync com o brilho do MacBook. **Free limita a 100 ajustes/dia.** |
| **[DisplayBuddy](https://displaybuddy.app/)** | US$24,99 vitalício | Mac + Windows, presets, sync multi-monitor, agendamento, CLI, Shortcuts/Siri, *gamma fallback*, e **controle por Wi-Fi de TVs LG webOS / Samsung Tizen** (fora do DDC). |
| **[BetterDisplay](https://github.com/waydabber/BetterDisplay)** | free / Pro | Vai muito além do DDC: monitores virtuais, override de resolução/HiDPI, XDR/HDR brightness. |
| **DisplayFusion** | ~US$39 | DDC é item secundário; o produto é gestão de janelas multi-monitor. |

### 2.3 Então os grátis são "bem piores"? — resposta honesta

**No controle DDC/CI em si: não.** É a mesma chamada de API. O `SetVCPFeature` do Monitorian
é bit a bit equivalente ao do DisplayBuddy. Não existe molho secreto no protocolo, e o
ddcutil é tecnicamente **superior** a qualquer produto pago em profundidade de protocolo.

**A diferença paga está em quatro lugares, e nenhum deles é o DDC:**

1. **Automação** — agendamento, perfis por app, sensor de luz ambiente, sync entre telas.
   É software comum em volta do DDC, não protocolo.
2. **Fallbacks quando o DDC não existe ou não basta** — manipulação de tabela de gama para
   escurecer abaixo do mínimo do hardware (o *sub-zero* do Lunar), overlay de software.
   Isso é o valor real do Lunar/BetterDisplay, e é caro de fazer bem.
3. **Canais alternativos** — DisplayBuddy falando Wi-Fi com TVs LG/Samsung. Nada a ver com DDC.
4. **Polimento e suporte** — UI, onboarding, e alguém pra reclamar quando seu monitor exótico
   não funciona.

**Conclusão para o nosso caso:** para controlar brilho/contraste/volume/entrada de dois
monitores, um projeto próprio bem-feito empata com o pago. O que é caro de replicar é
automação sofisticada e fallback por gama — e nada disso é necessário agora.

Onde quase todo mundo (grátis *e* pago) é fraco, e onde dá pra ser melhor: **diagnóstico
honesto de erro** (dizer *por que* falhou, não só "falhou") e **política explícita de
segurança de escrita** (§3).

---

## 3. Risco de hardware: um bug de software pode danificar o monitor?

Essa foi a pergunta central. A resposta tem nuance, então vai por partes.

### 3.0 A preocupação específica: "alguma config vai pra +infinito"

**Esse cenário específico não é possível.** Três barreiras independentes:

1. **O protocolo é de largura fixa.** O valor de um VCP é um inteiro de **16 bits**. O maior
   número representável é `65535`. Não existe "infinito", não existe float, não existe
   overflow para além disso — o campo simplesmente não comporta.
2. **O monitor publica e impõe o próprio teto.** Todo *get* devolve `(atual, máximo)`. Mandar
   acima do máximo faz o firmware saturar ou rejeitar o comando. Quem define o limite é o
   monitor, não o PC.
3. **O brilho não é elétrico direto.** `0x10` não controla corrente de LED. Ele é um número
   que o **scaler do monitor** interpreta e mapeia para a faixa de PWM/corrente que o próprio
   fabricante projetou. Você está mexendo no mesmo potenciômetro lógico que os botões do
   painel frontal mexem — e ninguém queima um monitor apertando o botão "+" muitas vezes.

Ou seja: **o caminho "valor absurdo → sobrecarga elétrica → painel queimado" está fechado.**

Os riscos reais são outros, e são mais chatos que dramáticos.

### 3.1 Risco real #1 — códigos reservados ao fabricante (`0xE0`–`0xFF`)

**Este é o único risco documentado de dano permanente.** A faixa `0xE0`–`0xFF` é reservada
e não padronizada; fabricantes escondem ali modos de fábrica, ajustes de serviço e
comandos de firmware.

O ddcutil mantém uma tabela de quirks (`src/base/monitor_quirks.c`) justamente para isso.
Ela tem **uma única entrada** hoje:

```c
static Monitor_Quirk_Table_Entry quirk_table[] = {
   {{ "XMI", "Mi Monitor", 13380, true}, { MQ_NO_SETTING,   NULL}},
};
```

E a mensagem correspondente, em `src/ddc/ddc_display_ref_reports.c:464-467`:

> `"WARNING: Setting feature values has been reported to permanently cripple this monitor!"`
> `"WARNING: Setting manufacturer reserved features has been reported to permanently cripple this monitor!"`

**Leitura:** o projeto com a maior base instalada e o histórico mais longo de relatos de bug
em DDC/CI conhece **um** modelo que se destrói por escrita DDC (um Xiaomi Mi Monitor). Em
uma década. E o vetor é justamente a faixa reservada.

➡️ **Mitigação: nunca escrever em `0xE0`–`0xFF`.** É barato e elimina o risco de dano
permanente conhecido. No `monitorctl` isso é bloqueado no código, sem flag de override.

### 3.2 Risco real #2 — desgaste de EEPROM

Alguns monitores (tipicamente os mais baratos/antigos) persistem configurações numa
**EEPROM**, com vida útil de ~**100.000 ciclos de escrita**. Monitores mais novos tendem a
usar NAND, sem essa limitação prática.

O cenário de perigo **não é** o uso manual — é o bug de software:

- um slider de brilho que dispara um `SetVCPFeature` por evento `mousemove` → centenas de
  escritas por segundo de arrasto;
- um loop de sincronização de brilho ambiente rodando a cada 100 ms;
- um retry mal feito que reenvia em rajada.

A 10 escritas/segundo, 100.000 ciclos acabam em **menos de 3 horas de arrasto acumulado**.

Vale a ressalva: há poucos casos documentados de monitor morto por isso, e não está provado
que todo monitor persiste *toda* escrita na EEPROM (muitos só gravam no *Save Settings*
explícito ou após um timeout de inatividade). Mas o custo de se proteger é praticamente zero.

➡️ **Mitigações no `monitorctl`:**
- **debounce** do slider: só escreve após o valor estabilizar (padrão 250 ms);
- **intervalo mínimo** entre escritas no mesmo monitor (padrão 120 ms);
- **orçamento de escritas** por janela de tempo, com recusa explícita ao estourar;
- **nunca** chamar `SaveCurrentSettings` / VCP `0xB0` — deixar a persistência a critério
  do firmware, que é o comportamento de menor desgaste.

### 3.3 Risco real #3 — `0xD6` (Power Mode) e estados de energia

Risco **operacional**, não destrutivo, mas é o que mais morde na prática.

Caso concreto e bem documentado: [twinkle-tray#1213](https://github.com/xanderfrangos/twinkle-tray/issues/1213).
Ao enviar VCP `0xD6` com valor 4 ou 5 (standby/off por hardware), alguns monitores entram
num "deep power off" do qual **não acordam** com o sinal normal do Windows. Só resolve
tirando da tomada ou no botão físico.

➡️ **Mitigação: `0xD6` fica fora do conjunto padrão.** Se um dia for habilitado, deve ser
opt-in explícito e com aviso, jamais no caminho feliz da UI.

### 3.4 Risco real #4 — travamento por I²C e *timing*

Rajada de comandos sem respeitar os tempos do DDC gera respostas corrompidas e, em firmwares
frágeis, trava o barramento até o monitor ser religado. Os tempos que o ddcutil usa
(`src/base/parms.h`) são a melhor referência empírica pública:

```c
#define DDC_TIMEOUT_MILLIS_DEFAULT                      50  // timeout normal do spec
#define DDC_TIMEOUT_MILLIS_BETWEEN_GETVCP_WRITE_READ    40  // entre request e reply
#define DDC_TIMEOUT_MILLIS_POST_SETVCP_WRITE            50  // após um Set VCP
#define DDC_TIMEOUT_MILLIS_POST_SAVE_SETTINGS          200  // após Save Settings
#define INITIAL_MAX_WRITE_READ_EXCHANGE_TRIES           10
```

E uma nota reveladora do release 2.1.0 do ddcutil:

> *"Always set sleep multiplier to at least 1.0 for commands setvcp and scs. Addresses
> reports that aggressive optimization caused setvcp to fail."*

Traduzindo: **eles tentaram otimizar o timing e quebrou.** Timing agressivo é bug.

➡️ **Mitigações:** *pacing* de ≥50 ms pós-escrita, ≥40 ms entre escrita e leitura, **lock
global** serializando todo acesso DDC (o barramento é compartilhado e não é reentrante),
e no máximo 1 retry em erro transiente.

### 3.5 O que o Windows nos entrega de graça

Em Windows não falamos I²C direto — vai tudo pela **Monitor Configuration API**
(`dxva2.dll`), que passa pelo driver WDDM. Isso adiciona uma camada de sanidade: o driver
valida o enquadramento dos pacotes e não expõe I²C cru.

O ganho concreto é o conjunto de códigos de erro específicos (de `winerror.h`), que o
Monitorian mapeia e quase todo mundo ignora:

| Código | Constante | Significado |
|---|---|---|
| `0xC0262582` | `ERROR_GRAPHICS_I2C_ERROR_TRANSMITTING_DATA` | falha elétrica no barramento — **transiente, vale retry** |
| `0xC0262584` | `ERROR_GRAPHICS_DDCCI_VCP_NOT_SUPPORTED` | o monitor não tem esse recurso |
| `0xC0262585` | `ERROR_GRAPHICS_DDCCI_INVALID_DATA` | resposta corrompida |
| `0xC0262589` | `ERROR_GRAPHICS_DDCCI_INVALID_MESSAGE_COMMAND` | **transiente** |
| `0xC026258A` | `ERROR_GRAPHICS_DDCCI_INVALID_MESSAGE_LENGTH` | **transiente** |
| `0xC026258B` | `ERROR_GRAPHICS_DDCCI_INVALID_MESSAGE_CHECKSUM` | **transiente** |
| `0xC026258D` | `ERROR_GRAPHICS_MONITOR_NO_LONGER_EXISTS` | monitor desconectado — reenumerar |

Uma armadilha documentada pelo Monitorian, no código:

```csharp
// SetMonitorBrightness function may return true even when it actually failed.
```

➡️ Por isso o `monitorctl` prefere o caminho **low-level** (`SetVCPFeature`) ao high-level
(`SetMonitorBrightness`), e confirma por leitura.

### 3.6 Veredito

| Cenário | Risco | Por quê |
|---|---|---|
| Valor "infinito" queimando o painel | ❌ **Impossível** | campo de 16 bits, teto imposto pelo firmware, brilho mediado pelo scaler |
| Dano permanente por escrita em `0xE0`–`0xFF` | ⚠️ **Real, documentado, raro** | 1 modelo conhecido em ~10 anos; **evitável 100% por allowlist** |
| Desgaste de EEPROM por rajada de escritas | ⚠️ **Plausível** | ~100k ciclos; **evitável por debounce + rate limit** |
| Monitor não acorda após `0xD6` | ⚠️ **Real e comum** | firmware; **evitável não usando `0xD6`** |
| Travar o barramento com timing agressivo | ⚠️ **Real, recuperável** | resolve religando; **evitável por pacing + lock** |
| Perder a calibração com `0x04` | ✅ **Chato, não é dano** | reconfigurável no menu |

**Em resumo:** com uma allowlist de códigos, limite de taxa de escrita e *pacing* correto,
o risco residual de dano permanente é essencialmente zero — e essas três defesas custam
menos de 200 linhas de código. É exatamente o que está implementado em
`src/monitorctl/ddc/policy.py`.

---

## 4. O que foi aproveitado de cada referência

Os três repositórios foram clonados e lidos. Nenhuma linha foi copiada — o ddcutil é
**GPL-2.0+** e copiá-lo contaminaria a licença deste projeto. O que foi extraído são
*fatos* (constantes de timing, códigos de erro do Windows, códigos VCP do padrão MCCS) e
*decisões de arquitetura*, que não são protegidos por copyright.

| Fonte | Lic. | O que foi aproveitado |
|---|---|---|
| ddcutil | GPL-2.0+ | constantes de timing (`parms.h`), tabela VCP do MCCS, tabela de quirks, conceito de retry adaptativo |
| Monitorian | MIT | conjunto de erros `ERROR_GRAPHICS_DDCCI_*`, classificação transiente vs. permanente, estratégia de parsing de capabilities, uso de `SafeHandle`, alerta sobre `SetMonitorBrightness` mentir |
| monitorcontrol | MIT | mapeamento das assinaturas `dxva2.dll` em `ctypes` — e o contraexemplo do que **não** fazer (sem `argtypes`, `__del__` fechando handle) |

---

## 5. Fontes

- [VESA DDC/CI 1.1 (PDF)](https://glenwing.github.io/docs/VESA-DDCCI-1.1.pdf)
- [VESA MCCS (PDF)](https://milek7.pl/ddcbacklight/mccs.pdf)
- [Display Data Channel — Wikipedia](https://en.wikipedia.org/wiki/Display_Data_Channel)
- [Monitor Configuration Functions — Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/monitor/monitor-configuration-functions)
- [GetVCPFeatureAndVCPFeatureReply — Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/api/lowlevelmonitorconfigurationapi/nf-lowlevelmonitorconfigurationapi-getvcpfeatureandvcpfeaturereply)
- [ddcutil — release notes (quirks, sleep multiplier)](https://www.ddcutil.com/release_notes/)
- [ddcutil — FAQ](https://www.ddcutil.com/faq/)
- [ddcutil — `monitor_quirks.c`](https://github.com/rockowitz/ddcutil/blob/master/src/base/monitor_quirks.c)
- [ddcutil — `vcp_feature_codes.c`](https://github.com/rockowitz/ddcutil/blob/master/src/vcp/vcp_feature_codes.c)
- [Monitorian — `MonitorConfiguration.cs`](https://github.com/emoacht/Monitorian/blob/master/Source/Monitorian.Core/Models/Monitor/MonitorConfiguration.cs)
- [monitorcontrol — `vcp_windows.py`](https://github.com/newAM/monitorcontrol/blob/main/monitorcontrol/vcp/vcp_windows.py)
- [twinkle-tray#1213 — monitores que não acordam após VCP 0xD6](https://github.com/xanderfrangos/twinkle-tray/issues/1213)
- [Hacker News — EEPROM e DDC/CI](https://news.ycombinator.com/item?id=24344696)
- [Lunar](https://lunar.fyi/) · [DisplayBuddy](https://displaybuddy.app/) · [ClickMonitorDDC](https://clickmonitorddc.com/)
