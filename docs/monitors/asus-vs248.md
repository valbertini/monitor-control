# ASUS VS248

TN/TFT 24" 1080p 60 Hz. Levantamento de **2026-09-20**.
Convenções das tabelas em [`README.md`](README.md#convenções-das-tabelas).

Este é o monitor mais bem comportado dos dois: capabilities string bem formada,
MCCS 2.1, manual oficial completo. Também é o que expõe mais superfície de risco.

---

## 1. Identidade

| Campo | Valor | Origem |
|---|---|---|
| Fabricante (EDID) | `ACI` (ASUSTek) | `WmiMonitorID` |
| Modelo (EDID) | `VS248` | `WmiMonitorID` |
| Product code | `2498` | `WmiMonitorID` |
| Fabricação | semana 29 de 2017 | `WmiMonitorID` |
| Instância | `DISPLAY\ACI2498\5&2a0761ac&0&UID4356_0` | `WmiMonitorID` |
| Device Windows | `\\.\DISPLAY2` (secundário) | `GetMonitorInfoW` |
| Área de imagem | 53 cm × 30 cm | `WmiMonitorBasicDisplayParams` |
| Horas de uso | **19.606 h** (≈ 2 anos e 3 meses ligado) | VCP `0xC0` |

### Painel e conectividade

| Campo | Valor |
|---|---|
| Painel | TFT LCD, 24" 16:9 |
| Resolução | 1920×1080, pitch 0,2768 mm |
| Brilho | 250 cd/m² |
| Contraste | 1000:1 típico · 50.000.000:1 com ASCR |
| Ângulo de visão | 170°/160° |
| Cores | 16,7 M |
| Resposta | 2 ms (VS248H) · 1 ms (VS248HR) |
| Entradas | D-Sub (VGA), DVI-D com HDCP, HDMI 1.3 |
| Áudio | saída de fone **só no HDMI** |
| Tilt | +20° / −5° |
| Consumo | <32 W ligado · <0,5 W standby |

Fonte: [manual oficial ASUS VS248 (PDF)](https://dlcdnets.asus.com/pub/ASUS/LCD%20Monitors/VS248/VS248_English.pdf), §3.2.

---

## 2. Capabilities string

Bem formada, sem desvios:

```
(prot(monitor)type(LCD)model(VS248)cmds(01 02 03 07 0C E3 F3)vcp(02 04 05 08 0B 0C 10 12 14(05 06 08 0B) 16 18 1A 52 60(01 03 04) 6C 6E 70 AC AE B2 B6 C0 C6 C8 C9 CC(01 02 03 04 05 06 09 0A 0D 14) D6(01 04 05) DF)mswhql(1)asset_eep(40)mccs_ver(2.1))
```

| Campo | Valor |
|---|---|
| `prot` | monitor |
| `type` | LCD |
| `model` | VS248 |
| `mccs_ver` | 2.1 — confere com `0xDF` = `0x0201` |
| `cmds` | `01 02 03 07 0C E3 F3` |
| `asset_eep` | 40 |

---

## 3. Todos os códigos VCP

### 3.1 Controláveis hoje pelo monitorctl

| Código | Nome | Anunciado | Atual | Máx | Notas |
|---|---|---|---|---|---|
| `0x10` | Brilho | sim | 15 | 100 | OSD: `Color → Brightness` |
| `0x12` | Contraste | sim | 80 | 100 | OSD: `Color → Contrast` |
| `0x14` | Preset de temperatura de cor | sim | 6 | 11 | valores `05 06 08 0B`. Mapeamento provável para o OSD: `05`=6500 K→**Warm**, `06`=7500 K→**Normal**, `08`=9300 K→**Cool**, `0B`=User 1→**User Mode**. |
| `0x16` | Ganho vermelho | sim | 100 | 100 | só editável no **User Mode** do OSD |
| `0x18` | Ganho verde | sim | 100 | 100 | idem |
| `0x1A` | Ganho azul | sim | 100 | 100 | idem |
| `0x60` | Fonte de entrada | sim | 4 | ~~15~~ | valores `01 03 04`. ⚠️ Ver nota de rotulagem abaixo. |

> **Nota de rotulagem em `0x60`.** O MCCS chama `0x01`=VGA 1, `0x03`=DVI 1,
> `0x04`=**DVI 2**. Mas o chassi tem VGA, DVI-D e **HDMI** — não dois DVI. A
> terceira entrada física é HDMI, então a interface hoje mostra "DVI 2" onde o
> usuário espera "HDMI". Caso clássico que pede rótulo por perfil de monitor.

### 3.2 Funcionam, mas o monitor não anuncia

| Código | Nome | Atual | Máx | Notas |
|---|---|---|---|---|
| `0x62` | Volume | 80 | 100 | OSD: `System Setup → Volume`. Manual diz que só vale no HDMI. |
| `0x87` | Nitidez | 45 | 100 | OSD: `Image → Sharpness`. **Não anunciado, mas funciona.** |
| `0x8D` | Mudo / apagar tela | 2 | 2 | `1` = mudo, `2` = normal |
| `0xCA` | Controle de OSD / botões | 1 | 2 | permite travar o OSD |
| `0xDC` | Modo de imagem | 0 | 6 | **7 valores.** Candidato ao Splendid (6 modos) — não verificado, ver §6. |
| `0x0E` | Clock (foco VGA) | 159 | ~~100~~ | OSD: `Image → Focus → Clock`, só VGA |
| `0x1E` | Auto setup | 0 | 1 | OSD: ajuste automático, só VGA |
| `0x20` | Posição horizontal | 181 | ~~100~~ | OSD: `Image → Position`, só VGA |
| `0x30` | Posição vertical | 176 | ~~100~~ | OSD: `Image → Position`, só VGA |
| `0x3E` | Fase do clock (foco VGA) | 58 | 100 | OSD: `Image → Focus → Phase`, só VGA |

> Os controles de geometria VGA leem valores **acima do próprio máximo** (181 com
> máximo 100). A entrada ativa é digital, então esses controles estão inativos e
> o firmware devolve lixo. Não escrever neles fora de VGA.

### 3.3 Reset de fábrica — expostos e bloqueados

| Código | Nome | Atual | Máx | Situação |
|---|---|---|---|---|
| `0x04` | Restore factory defaults | 0 | 1 | ⛔ bloqueado por política |
| `0x05` | Restore factory brightness/contrast | 0 | 1 | ⛔ bloqueado por política |
| `0x08` | Restore color defaults | 0 | 1 | ⛔ bloqueado por política |

São comandos momentâneos: a leitura sempre devolve 0. Correspondem ao
`System Setup → All Reset` do OSD. Escrever qualquer um apaga a calibração.

### 3.4 Leitura / telemetria

| Código | Nome | Atual | Máx | Interpretação |
|---|---|---|---|---|
| `0x02` | New control value | 1 | 2 | |
| `0x0B` | Incremento de temperatura de cor | n/s | | não suportado neste modelo |
| `0x0C` | Temperatura de cor solicitada | 100 | 126 | sem `0x0B` não dá para converter com segurança. Máximo 126 → ≈9300 K, que bate com o preset mais frio. |
| `0x52` | Active control | 16 | 255 | |
| `0x6C` | Nível de preto vermelho | 50 | 100 | anunciado |
| `0x6E` | Nível de preto verde | 50 | 100 | anunciado |
| `0x70` | Nível de preto azul | 50 | 100 | anunciado |
| `0xAC` | Frequência horizontal | 2064 | ~~1~~ | ⚠️ máximo inválido e valor não corresponde à taxa de linha esperada |
| `0xAE` | Frequência vertical | 6010 | 65535 | 60,10 Hz |
| `0xB2` | Layout de subpixel | 1 | 1 | 1 = RGB em faixa vertical |
| `0xB6` | Tipo de tecnologia | 3 | 5 | 3 = LCD/TFT |
| `0xC0` | Horas de uso | 19606 | 65535 | |
| `0xC6` | Application enable key | 17868 | 65535 | |
| `0xC8` | Tipo de controlador | 33545 | ~~36~~ | máximo inválido |
| `0xC9` | Versão de firmware | 257 | 65535 | `0x0101` |
| `0xCC` | Idioma do OSD | 2 | 20 | valores `01 02 03 04 05 06 09 0A 0D 14` — **exatamente os 10 idiomas do manual** |
| `0xDF` | Versão VCP | 513 | 65535 | `0x0201` = MCCS 2.1 |

### 3.5 Power mode — exposto e bloqueado

| Código | Nome | Atual | Máx | Situação |
|---|---|---|---|---|
| `0xD6` | Power mode (DPMS) | 1 | 5 | ⛔ **bloqueado por política** |

Anunciado como `D6(01 04 05)`: `01` = ligado, `04` = standby por hardware,
`05` = desligado por hardware. **`04` e `05` são exatamente os valores do
[twinkle-tray#1213](https://github.com/xanderfrangos/twinkle-tray/issues/1213)**,
onde monitores entram em deep power off e só voltam na tomada.

### 3.6 Não suportados

`0x72` gama · `0xDA` scan mode · `0xDB` image mode

---

## 4. Menu OSD completo

Extraído do [manual oficial](https://dlcdnets.asus.com/pub/ASUS/LCD%20Monitors/VS248/VS248_English.pdf), §3.1.2.

### 1. Splendid
Scenery · Standard · Theater · Game · Night View · sRGB
*No Standard Mode, ASCR / Saturation / Sharpness ficam travados.*

### 2. Color
| Item | Faixa / opções |
|---|---|
| Brightness | 0–100 |
| Contrast | 0–100 |
| Saturation | 0–100 |
| Color Temp. | Cool · Normal · Warm · User Mode |
| Skin Tone | Reddish · Natural · Yellowish |
| Smart View | melhora a imagem em ângulo aberto |

*R/G/B (0–100) só ficam editáveis no User Mode.*

### 3. Image
| Item | Faixa / opções |
|---|---|
| Sharpness | 0–100 |
| Trace Free | 0–100 (overdrive) |
| Aspect Control | Full · 4:3 · 1:1 · OverScan |
| ASCR | on/off |
| Position | H/V 0–100 — **só VGA** |
| Focus | Phase e Clock 0–100 — **só VGA** |

### 4. Input Select
VGA · DVI · HDMI

### 5. System Setup
| Item | Faixa / opções |
|---|---|
| Splendid Demo Mode | divide a tela para comparar modos |
| Volume | 0–100 — **só HDMI** |
| ECO Mode | on/off |
| OSD Setup | posição H/V 0–100 · timeout 10–120 s · **DDC/CI on/off** · fundo opaco/transparente |
| Information | dados do monitor |
| Language | 10 idiomas |
| All Reset | restaura os padrões de fábrica |

> `System Setup → OSD Setup → DDC/CI` é o interruptor que desliga tudo que este
> projeto faz. Se o monitor parar de responder, é o primeiro lugar a checar.

---

## 5. Mapa OSD × DDC/CI

| Item de OSD | Código | Alcançável? |
|---|---|---|
| Brightness | `0x10` | ✅ |
| Contrast | `0x12` | ✅ |
| Color Temp. | `0x14` | ✅ (4 opções, batem com o OSD) |
| R/G/B (User Mode) | `0x16`/`0x18`/`0x1A` | ✅ |
| Input Select | `0x60` | ✅ (rótulo da 3ª entrada errado) |
| Sharpness | `0x87` | ✅ (não anunciado, descoberto por sondagem) |
| Volume | `0x62` | ✅ (não anunciado, descoberto por sondagem) |
| Language | `0xCC` | ✅ 10 valores corretos |
| Position (VGA) | `0x20`/`0x30` | ✅ categoria *Geometry*; só faz efeito em VGA |
| Focus (VGA) | `0x3E`/`0x0E` | ✅ categoria *Geometry*; só faz efeito em VGA |
| Auto ajuste (VGA) | `0x1E` | ✅ categoria *Geometry*; só faz efeito em VGA |
| All Reset | `0x04` | ⛔ bloqueado |
| — | `0x05`, `0x08` | ⛔ bloqueados |
| **Splendid (6 modos)** | `0xDC` | ⚠️ exposto como *Picture mode*; que seja o Splendid segue **não verificado** |
| **Saturation** | `0x8A` | ❌ não suportado — ✅ **via GPU** (`gpu_vibrance`) |
| **Skin Tone** | — | ❌ sem código padrão |
| **Smart View** | — | ❌ sem código padrão |
| **Trace Free** | — | ❌ sem código padrão |
| **Aspect Control** | `0x86`? | ❌ não responde |
| **ASCR** | — | ❌ sem código padrão |
| **ECO Mode** | — | ❌ sem código padrão |
| **Splendid Demo Mode** | — | ❌ sem código padrão |
| **OSD Setup** (posição, timeout, fundo) | — | ❌ sem código padrão |
| Gama | `0x72` | ❌ não suportado — ✅ **via GPU** (`gpu_sw_gamma`) |

---

## 6. Lacunas e itens não verificados

- **`0xDC` = Splendid?** Lê 0 com máximo 6, ou seja 7 valores; o Splendid tem 6
  modos. O MCCS define `0xDC` como "Display Application" com uma tabela própria
  (Standard, Productivity, Mixed, Movie, User, Games, Sports) que também tem 7
  entradas. **As duas hipóteses cabem nos números** — só um teste de escrita
  distingue, e isso muda a imagem do monitor de verdade.
- **Rótulo da entrada `0x04`.** Precisa de confirmação física de qual cabo está
  em qual porta para afirmar que `0x04` = HDMI.
- **`0x0C` sem `0x0B`.** Sem o incremento não dá para converter 100 em kelvin com
  confiança.
- **`0xAC`.** Máximo inválido e valor fora do esperado.
- **Controles VGA.** Nunca testados com a entrada VGA ativa.
