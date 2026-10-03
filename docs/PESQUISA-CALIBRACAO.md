# Pesquisa: calibrar o monitor a olho

Levantamento de 02/10/2026 para a janela **Calibration** (decisão D29 em
[`PROJETO.md`](PROJETO.md)). O que foi aplicado está em
[`monitorctl/app/calibration.py`](../monitorctl/app/calibration.py).

## 1. O vídeo de referência

**"How To Calibrate Your Monitor Without Testing Equipment"**, canal *House Dad
Life* (Dave Wiebe), 9:09, 28/02/2025 —
<https://www.youtube.com/watch?v=5_tM7vSr_8M>. Usa o disco de testes **AVS HD
709** (v2c, padrões "based on original by Mark Sydow") e segue a ordem: desligar
realces → nível de preto (Brightness) → Contrast → conferir os dois → nitidez →
conferência de cor.

Os padrões que piscam, segundo o manual do AVS HD 709
(<https://w6rz.net/avshd709/Patterns-Manual.pdf>, p. 3–5), em níveis de vídeo
(16 = preto, 235 = branco):

- **Black Clipping**: barras numeradas 2–25 sobre preto de referência; 17–25
  piscam. "Ajuste o preto no menor valor em que 17–25 piscam"; abaixo de 16 elas
  se fundem.
- **White Clipping**: barras 230–253 piscam. Subir o contraste até barras pararem
  de piscar ou aparecer um tom rosado; "no mínimo 230–234 devem sempre piscar", e
  ver 244 piscando é um bom meio-termo.

No PC o sinal é 0–255 de faixa cheia: o 16 do vídeo é o 0 daqui e o 235 é o 255.
A frequência do pisca não está documentada; usamos **1 Hz**, bem abaixo do
limite de 3 flashes/s da **WCAG 2.3.1**
(<https://www.w3.org/TR/WCAG22/#three-flashes-or-below-threshold>).

## 2. Os testes a olho

| Teste | Padrão | Como usar | Fonte |
|---|---|---|---|
| Nível de preto | quadrados 1–15, 20, 25, 30, 40 sobre 0 | sala escura, tela cheia; num LCD o Brightness costuma ser só o backlight — os quadrados escuros dependem de contraste/gama; painéis 6-bit+FRC costumam mostrar 0–3 como preto | [Lagom black](http://www.lagom.nl/lcd-test/black.php) |
| PLUGE | faixas ~2% abaixo e acima do preto | baixar até a de baixo sumir com a de cima visível; no PC (sem "abaixo do preto"): baixar até a mais fraca sumir e subir "um ou dois passos" | [ITU-R BT.814](https://www.itu.int/dms_pubrec/itu-r/rec/bt/R-REC-BT.814-4-201807-I!!PDF-E.pdf), [Spears & Munsil](https://spearsandmunsil.com/portfolio-item/setting-the-brightness-control-2/) |
| Saturação do branco | xadrez 244–254 sobre 255 | cada um distinguível; olhar de longe | [Lagom white](http://www.lagom.nl/lcd-test/white.php) |
| Gama | linhas de 1 px pretas/brancas ao lado de cinzas sólidos | afastar-se ou apertar os olhos até as linhas virarem cinza; precisa de resolução nativa, sem escala. Linhas 0/255 dão 50% de luz = cinza **186** em gama 2.2 (188 na curva sRGB) | [Lagom gamma](http://www.lagom.nl/lcd-test/gamma_calibration.php) |
| Equilíbrio de cinza | rampa de cinza; manchas de ~80% e ~25% | **ganho** age nas altas-luzes (70–100%), **offset** nas sombras (20–30%); ganho primeiro, depois offset, repetir | [Portrait Displays](https://www.portrait.com/resource-center/2-point-grayscale-adjust/) |
| Gradientes | rampas 0–255 em cinza, R, G, B | sem faixas; correções feitas antes podem criar faixas | [Lagom gradient](http://www.lagom.nl/lcd-test/gradient.php) |
| Preparação | — | ligar ~30 min antes, luz normal (escura para as sombras) | [EIZO Monitor Test](https://www.eizo.eu/software/monitor-test) |

## 3. Ordem e controle de cada passo

- **ArgyllCMS / DisplayCAL**: nível de preto → ponto branco (ganho RGB) → nível de
  branco → ponto preto (offset RGB) → conferir, repetindo.
  [dispcal](https://www.argyllcms.com/doc/dispcal.html)
- No LCD o "contraste" é muitas vezes simulado no sinal e o "brilho" é o
  backlight, que sobe branco e preto juntos (Argyll).
- **Monitor primeiro, GPU só para o que sobrar**: toda correção na curva da GPU
  custa suavidade de gradiente (Lagom).

## 4. O que foi aplicado

| Passo | Padrão | Ajustar |
|---|---|---|
| Antes de começar | lista: aquecer 30 min, luz, realces desligados, preset Standard/sRGB/User, cartões "PC software" no neutro | — |
| 1 Brilho | branco cheio com um cinza 188 no centro | Brightness (backlight) |
| 2 Altas-luzes | barras 235, 240–254 piscando contra 255 + quadrados 244–254 | Contrast |
| 3 Sombras | barras 1–25 piscando contra 0 + quadrados 1–15, 20, 25, 30, 40 | Black level / offset (os três), preset de gama; só então GPU; reconferir o passo 2 |
| 4 Equilíbrio de cinza | rampa de 11 passos + manchas 204 (80%) e 64 (25%) | preset de temperatura → ganho (baixar o canal forte) → offset; repetir |
| 5 Gama | linhas de 1 px ao lado de sólidos 176–196, com 186 marcado | gama do monitor, senão Gamma (software) |
| 6 Gradientes | rampas cinza, R, G, B | recuar correções da GPU se surgirem faixas |
| 7 Cores | barras a 75% + "abra ao lado uma foto ou app cujas cores você conhece bem, com boa variedade" | só para erro evidente: vibrance ou seis eixos |

Tela cheia (F11) no monitor em ajuste, ← → entre passos, pisca desligável.
**Fora**: tons de pele (pedido do usuário).

## 5. Não verificado

- A frequência de pisca do AVS HD 709.
- Fonte primária para "comparar com conteúdo conhecido" (prática comum, citada
  pelo vídeo) e para "baixar o canal forte em vez de subir os outros".
