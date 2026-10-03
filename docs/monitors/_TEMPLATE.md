# <Fabricante> <Modelo>

<Uma linha: painel, tamanho, resolução, taxa.> Levantamento de **<AAAA-MM-DD>**.
Convenções das tabelas em [`README.md`](README.md#convenções-das-tabelas).

---

## 1. Identidade

| Campo | Valor | Origem |
|---|---|---|
| Fabricante (EDID) | | `WmiMonitorID` |
| Modelo (EDID) | | `WmiMonitorID` |
| Product code | | `WmiMonitorID` |
| Fabricação | | `WmiMonitorID` |
| Instância | | `WmiMonitorID` |
| Device Windows | | `GetMonitorInfoW` |
| Área de imagem | | `WmiMonitorBasicDisplayParams` |
| Horas de uso | | VCP `0xC0` |

### Painel e conectividade

| Campo | Valor |
|---|---|
| Painel | |
| Tamanho / resolução | |
| Taxa de atualização | |
| Brilho / contraste | |
| Entradas | |
| Áudio | |

Fontes: <links do fabricante>

---

## 2. Capabilities string

```
<cole aqui a saída crua de `monitorctl caps <n>`>
```

Anote qualquer desvio do formato padrão — parêntese faltando, valores colados,
campos ausentes. São esses desvios que quebram parser ingênuo.

| Campo | Valor |
|---|---|
| `prot` | |
| `type` | |
| `model` | |
| `mccs_ver` | |
| `cmds` | |

Confira `mccs_ver` contra o VCP `0xDF`: os dois podem discordar.

---

## 3. Todos os códigos VCP

### 3.1 Controláveis hoje pelo monitorctl

| Código | Nome | Anunciado | Atual | Máx | Notas |
|---|---|---|---|---|---|
| | | | | | |

### 3.2 Funcionam, mas o monitor não anuncia

Descobertos por sondagem direta. Riscar o máximo com `~~…~~` quando for inválido.

| Código | Nome | Atual | Máx | Notas |
|---|---|---|---|---|
| | | | | |

### 3.3 Leitura / telemetria

| Código | Nome | Atual | Máx | Interpretação |
|---|---|---|---|---|
| | | | | |

### 3.4 Não suportados

<lista dos códigos que respondem `ERROR_GRAPHICS_DDCCI_VCP_NOT_SUPPORTED`>

### 3.5 Bloqueados ou não lidos por política

| Código | Motivo |
|---|---|
| | |

---

## 4. Menu OSD completo

Transcreva a árvore do manual do fabricante, com faixas e opções de cada item.
Sem isso não dá para distinguir "o monitor não tem" de "tem, mas só no botão".

---

## 5. Mapa OSD × DDC/CI

| Item de OSD | Código | Alcançável? |
|---|---|---|
| | | ✅ / ⚠️ / ❌ / ❓ |

Legenda: ✅ exposto · ⚠️ funciona mas não exposto · ❌ inalcançável · ❓ candidato
não verificado.

---

## 6. Segurança específica deste monitor

| Item | Situação |
|---|---|
| `0xD6` power mode | |
| Faixa reservada `0xE0`–`0xFF` | |
| Resets de fábrica (`0x04`/`0x05`/`0x06`/`0x08`) | |
| `0xB0` store/restore de EEPROM | |

Cheque também a [tabela de quirks do ddcutil](https://github.com/rockowitz/ddcutil/blob/master/src/base/monitor_quirks.c).

---

## 6. Lacunas e itens não verificados

- <o que responde à leitura mas nunca foi testado na escrita>
- <o que o manual descreve mas não foi encontrado em código>
- <valores que não fazem sentido>
