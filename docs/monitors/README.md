# Documentação por monitor

Um arquivo por monitor físico, descrevendo **todo aspecto controlável** dele: o que
o DDC/CI alcança, o que só existe no menu OSD, e o que é mentira do firmware.

| Monitor | Arquivo | Conectado em | MCCS | Códigos que respondem (controláveis) |
|---|---|---|---|---|
| TCL 25G64 (24,5" QD-Mini LED, 300 Hz) | [`tcl-25g64.md`](tcl-25g64.md) | `\\.\DISPLAY1` (DisplayPort 1) | 2.0 / 2.1¹ | 29 (15 controláveis) |
| ASUS VS248 (24" TN/TFT, 60 Hz) | [`asus-vs248.md`](asus-vs248.md) | `\\.\DISPLAY2` | 2.1 | 37 (24 controláveis) |

¹ o TCL se contradiz: a capabilities string diz `mccs_ver(2.0)`, o VCP `0xDF` responde `0x0201` (2.1).

Levantamento de **2026-09-20**. Para adicionar um monitor, veja
[Como documentar um monitor novo](#como-documentar-um-monitor-novo).

---

## Por que esta pasta existe

O DDC/CI define um **vocabulário**, não um contrato. Dois monitores na mesma
máquina, falando o mesmo protocolo pela mesma API, divergem em tudo que importa:
quais códigos existem, quais são anunciados, o que o valor significa, e se o
máximo reportado quer dizer alguma coisa.

Sem um documento por modelo, esse conhecimento fica espalhado em tentativa e erro.
Com ele, dá para escrever um perfil por monitor que rotula cada controle
corretamente em vez de chutar a partir do padrão.

---

## Metodologia

Cada documento cruza **três fontes independentes**, porque nenhuma sozinha é
confiável:

### 1. A capabilities string do próprio monitor

O que o firmware *diz* que suporta. É o ponto de partida, e é um **piso, não um
teto** — veja o achado nº 1 abaixo.

```bash
python -m monitorctl caps 1
```

### 2. Sondagem direta dos códigos não anunciados

Leitura de códigos MCCS que o manual do fabricante sugere existir mas a
capabilities string omite. Só leitura, e a faixa reservada `0xE0`–`0xFF` nunca é
tocada — nem para ler.

```bash
python tools/survey_monitor.py --json survey.json
```

### 3. O manual do fabricante

Para mapear item de menu OSD ↔ código VCP, e para descobrir o que existe no
monitor mas **não** é alcançável por DDC/CI. Sem isso não dá para distinguir
"o monitor não tem esse recurso" de "o recurso existe mas só pelo botão".

---

## Achados que valem para os dois monitores

Estes saíram do levantamento e são o motivo de a doc existir.

### 1. A capabilities string é um piso, não um teto

**Os dois monitores implementam códigos que não anunciam.** Não é caso raro:

| Monitor | Códigos que funcionam mas não são anunciados |
|---|---|
| TCL 25G64 | `0x6C`/`0x6E`/`0x70` nível de preto RGB, **`0xD6` power mode**, `0xCA` controle de OSD, `0xCC` idioma, `0xDC` modo de imagem, e telemetria (`0x52`, `0xB2`, `0xB6`, `0xC0`, `0xC6`, `0xC9`) |
| ASUS VS248 | `0x0E`, `0x1E`, `0x20`, `0x30`, `0x3E` (geometria VGA), `0x62` volume, `0x87` nitidez, `0x8D` mudo, `0xCA` controle de OSD, `0xDC` modo de imagem |

Consequência prática: filtrar recursos só pela capabilities string **esconde
controles que funcionam**. Foi por isso que o `monitorctl` passou a **sondar todo
código do registro**, anunciado ou não, e usar a capabilities string apenas para
rótulos e listas de valores permitidos. O ganho foi concreto: o ASUS saltou de 7
para 24 recursos controláveis, e a nitidez e o volume dele apareceram.

### 2. O `maximum` é lixo para códigos não-contínuos e de leitura

O `GetVCPFeatureAndVCPFeatureReply` devolve um máximo para qualquer código, e
para vários ele não significa nada:

| Caso | Atual | Máximo | Por que é absurdo |
|---|---|---|---|
| TCL `0x60` fonte de entrada | 15 | **3** | o valor atual é maior que o máximo |
| ASUS `0xAC` frequência horizontal | 2064 | **1** | idem |
| ASUS `0xC8` tipo de controlador | 33545 | **36** | idem |

**Nunca valide um código não-contínuo pelo máximo.** Use a lista de valores da
capabilities string. É exatamente o que `policy.check_write` faz, e estes três
casos são a prova de que a decisão estava certa.

### 3. Os dois expõem `0xD6` (power mode) — e o TCL nem anuncia

O TCL responde `0xD6` com máximo 4 sem listar o código; o ASUS anuncia
`D6(01 04 05)`. Esse é o código que deixa monitores em "deep power off" do qual
não acordam ([twinkle-tray#1213](https://github.com/xanderfrangos/twinkle-tray/issues/1213)).

A política do projeto bloqueia `0xD6` incondicionalmente. Este levantamento
mostra que o bloqueio não é teórico: **está disponível nos dois monitores agora**.

### 4. O TCL anuncia `0xFF`, na faixa reservada ao fabricante

A capabilities string do TCL termina com `... 60(11120F)FF)`. O `0xFF` está em
`0xE0`–`0xFF`, a única faixa com caso documentado de dano permanente. O
levantamento **não leu** esse código e a política **não escreve** nele.

É provável que boa parte dos recursos de marketing do TCL (mira, local dimming,
dark field control, timer de jogo) esteja justamente ali. Não vale o risco.

### 5. Recursos do OSD que simplesmente não têm código padrão

Nenhum dos dois expõe `0x72` (gama). O ASUS tem Saturação, Aspect Control, ASCR,
Trace Free, Skin Tone e Smart View no menu, e **nenhum** deles responde em código
MCCS. O TCL tem nove modos de imagem no OSD e o único candidato (`0xDC`) só aceita
quatro valores.

Ou seja: "controlar todo aspecto do monitor" por DDC/CI **não é possível** nestes
dois. O alcance real está documentado em cada arquivo, item por item.

---

## Convenções das tabelas

| Coluna | Significado |
|---|---|
| **Código** | código VCP em hexadecimal |
| **Anunciado** | `sim` se aparece na capabilities string; `não` se só foi descoberto por sondagem |
| **Atual / Máx** | leitura crua no momento do levantamento; valores mudam conforme o uso |
| **Status** | `ok` responde · `n/s` responde "não suportado" · `bloqueado` recusado por política · `não lido` faixa reservada |
| **Escrita** | `permitida` está na allowlist · `bloqueada` política · `fora da allowlist` funcionaria, mas não está exposto |

Um código marcado **`não verificado`** significa que ele responde à leitura mas
o efeito da escrita nunca foi testado — testar exige escrever no monitor e mudar
uma configuração de verdade.

---

## Como documentar um monitor novo

1. Conecte o monitor e confirme que o DDC/CI está ligado no menu OSD dele
   (no ASUS: `System Setup → OSD Setup → DDC/CI`).
2. Rode o levantamento:
   ```bash
   python -m monitorctl list
   python -m monitorctl caps <n>
   python tools/survey_monitor.py --monitor <n> --json survey.json
   ```
3. Procure o manual do fabricante e extraia a árvore completa do menu OSD.
4. Copie [`_TEMPLATE.md`](_TEMPLATE.md), preencha, e adicione uma linha na tabela
   do topo deste arquivo.
5. Marque explicitamente o que **não** é alcançável. Essa é a parte mais útil do
   documento e a que mais dá trabalho para redescobrir depois.

> A sondagem é só de leitura e pula a faixa reservada. Ainda assim são dezenas de
> transações no barramento: rode uma vez e guarde o JSON, não em laço.
