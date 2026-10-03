# Pesquisa: organização e leitura da janela de controles

Levantamento feito em 02/10/2026 para a segunda versão da janela (decisão D26 em
[`PROJETO.md`](PROJETO.md)). O que foi aplicado está em
[`monitorctl/app/presentation.py`](../monitorctl/app/presentation.py).

## 1. Como os menus dos próprios monitores ordenam os ajustes

O padrão se repete entre fabricantes: **modo de imagem → brilho/contraste → cor
→ imagem → entrada → áudio → menu/sistema**. Os ajustes finos (R/G/B) ficam
dentro de um modo "User/Custom", e cada grupo tem o próprio reset.

| Fabricante | Ordem do menu principal | Fonte |
|---|---|---|
| Dell P2722H | Brightness/Contrast → Auto Adjust (só VGA) → Input Source → Color (Preset Modes, Custom R/G/B, Hue, Saturation, Reset Color) → Display (Aspect, Position, Sharpness, Clock, Phase) → Menu → Others | [manual](https://dl.dell.com/manuals/all-products/esuprt_electronics_accessories/esuprt_electronics_accessories_monitor/esuprt_monitor_p_series/dell-p2722h-monitor_user's-guide_en-us.pdf) |
| **ASUS VS248** (o monitor daqui) | Splendid (presets) → Color (Brightness, Contrast, Saturation, Color Temp com R/G/B) → Image (Sharpness, Aspect, Position, Focus só VGA) → Input Select → System Setup (Volume, Language) | [manual](https://dlcdnets.asus.com/pub/ASUS/LCD%20Monitors/VS248/VS248_English.pdf) |
| LG 34GL750 | Game Mode → Game Adjust → Picture Adjust (Brightness, Contrast, Sharpness, Gamma, Color Temp, Black Level) → Input → General | [manual](https://glenwing.github.io/docs/manuals/LG-34GL750.pdf) |
| BenQ GW2460 | Display (Input, Position, Clock, Phase) → Picture (Brightness, Contrast, Sharpness, Gamma, Color Temperature R/G/B, Hue, Saturation) → Picture Advanced → Audio → System; **opções indisponíveis ficam esmaecidas** | [manual](https://esupportdownload.benq.com/esupport/LCD%20Monitor/UserManual/GW2460/UM_EN.pdf) |
| Samsung C27F591FD | Picture: MAGIC Bright (preset) → Brightness → Contrast → Sharpness → Color → … ; depois OnScreen Display → System | [manual](https://downloadcenter.samsung.com/content/UM/201602/20160215124244239/BN46-00498A-Eng.pdf) |

## 2. Em que ordem se ajusta um monitor

- **ArgyllCMS `dispcal`** (ajuste interativo): nível de preto → ponto branco
  (temperatura / ganho RGB) → nível de branco (brilho/backlight) → ponto preto
  (offset RGB) → conferência. As dicas são +/−/= por canal R, G e B.
  [docs](https://www.argyllcms.com/doc/dispcal.html)
- **DisplayCAL**: num LCD, o ponto branco (ganho RGB ou temperatura) é o
  primeiro passo, depois o nível de branco pelo backlight. Recomenda deixar o
  contraste no padrão de fábrica; os offsets muitas vezes faltam ou mexem na
  temperatura toda. [displaycal.net](https://displaycal.net/)
- **Lagom LCD test**: ajustar o monitor primeiro e usar gerenciamento de cor
  (a camada da GPU) só para o que sobrar. [lagom.nl](http://www.lagom.nl/lcd-test/)
- **Windows `dccw`**: gama, brilho, contraste, equilíbrio de cor ("mova os
  controles vermelho, verde e azul para tirar o tom dos cinzas"). A ordem exata
  das páginas só foi confirmada por fonte secundária.

## 3. Como outros programas apresentam isso

- **NVIDIA "Adjust desktop color settings"**: seletor de canal (Todos / Vermelho
  / Verde / Azul) comandando um único conjunto de controles; Digital Vibrance ao
  lado; matiz de 0 a 359°.
  [ajuda](https://www.nvidia.com/content/Control-Panel-Help/vLatest/en-gb/mergedProjects/nvdspENG/CS_Adjust_Color_Settings_Advanced.htm)
- **Dell Display Manager**: Brightness/Contrast → Color → Display → Audio, com
  botões "Advanced" (divulgação progressiva).
- **Monitorian**: só brilho. **Twinkle Tray** e **ClickMonitorDDC**: contraste,
  volume e RGB são opcionais, ligados explicitamente pelo usuário.
- Não verificado: se as barras do DisplayCAL são coloridas por canal; o layout do
  LG OnScreen Control.

## 4. Diretrizes gerais

- **NN/g — divulgação progressiva**: mostrar de cara o que se usa sempre; o resto
  atrás de um controle bem rotulado; mais de dois níveis tende a ter baixa
  usabilidade. [artigo](https://www.nngroup.com/articles/progressive-disclosure/)
- **Nielsen — controles inativos**: o que está *permanentemente* indisponível se
  esconde; o que está *temporariamente* inativo fica desabilitado, explicando
  por quê. [artigo](https://www.uxtigers.com/post/inactive-buttons)
- **Microsoft — configurações de app**: agrupar sob cabeçalhos; cada grupo com
  título, descrição e ícone; alteração aplica na hora.
  [guia](https://learn.microsoft.com/en-us/windows/apps/design/app-settings/guidelines-for-app-settings)
- **Ícones**: a fonte do sistema (Segoe MDL2 Assets no Windows 10; Segoe Fluent
  Icons só vem no 11). [doc](https://learn.microsoft.com/en-us/windows/apps/design/style/segoe-fluent-icons-font)
- **WCAG 1.4.1**: cor não pode ser o único meio de transmitir informação; o
  texto continua dizendo. [W3C](https://www.w3.org/WAI/WCAG22/Understanding/use-of-color.html)

## 5. O que foi aplicado

| Recomendação | Como ficou |
|---|---|
| Modo antes do resto | Seção "Picture Mode" na frente: trocar o modo reseta os ajustes finos |
| Ordem de calibração | Brilho/contraste → temperatura → ganho (brancos) → offset (pretos) → imagem → matiz por cor |
| GPU por último, "por cima do monitor" | Seções da GPU no fim, com a dica "set the monitor first" |
| Cor só nos controles de canal, sempre com texto | Ponto e slider na cor do canal (R, G, B; seis eixos na ordem R, Y, G, C, B, M); nome escrito ao lado |
| Ícone no cabeçalho, não em cada controle | Ícone de Segoe MDL2 Assets por seção; sem a fonte, sem ícone |
| Esconder o permanentemente indisponível | Cartão "Not available on this monitor (n)", recolhido, no fim |
| Desabilitar com motivo o temporário | Ajuste só leitura (ex.: vibrance que falhou na validação) fica no lugar, desabilitado |
| Reset para o salvo, não de fábrica | Marca ↺ por controle quando difere do perfil em uso; reset de fábrica continua bloqueado pela política |
| Uma linha dizendo o que o grupo muda | Dica sob o título de cada seção ("Tints the bright end: use it to neutralise whites") |

**Não aplicado (ideias para depois):** unidades (K na temperatura fina, ° no
matiz da GPU), trilhos com gradiente (exigiriam um slider desenhado em canvas),
um expansor "Advanced" por seção, e desabilitar os ganhos RGB quando o preset
não é "User" (o ASUS só aceita ganho no User Mode, mas isso não é detectável de
forma geral).
