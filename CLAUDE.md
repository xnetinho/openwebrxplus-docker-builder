# CLAUDE.md — Guia Completo de Desenvolvimento

Este arquivo documenta em detalhe a arquitetura, decisões de projeto, lições aprendidas e armadilhas do desenvolvimento. Destina-se a assistentes de IA e desenvolvedores que assumam o projeto no futuro.

---

## Visão Geral do Repositório

Fork de [0xAF/openwebrxplus-docker-builder](https://github.com/0xAF/openwebrxplus-docker-builder) que adiciona decodificação de rádio digital TETRA ao OpenWebRX+.

Produz três imagens Docker em cadeia:

```
slechev/openwebrxplus  →  slechev/openwebrxplus-softmbe  →  xnetinho/openwebrxplus-tetra
     (upstream)                  (upstream)                        (este repo)
```

O TETRA (Terrestrial Trunked Radio) é um padrão de rádio troncalizado digital usado por polícia, bombeiros, defesa civil e outros serviços de emergência. Opera em pi/4-DQPSK, 18 kBaud, 25 kHz de canal, 4 slots TDMA por portadora. O áudio usa o codec ACELP (ETSI EN 300 395-2). A rede pode ser encriptada com TEA (TETRA Encryption Algorithm).

---

## Estrutura do Repositório

```
run                              # CLI principal: build, run, dev
buildfiles/
  Dockerfile                     # Imagem principal (pesada, multi-stage, upstream)
  Dockerfile-softmbe             # Imagem SoftMBE (leve, base upstream)
  Dockerfile-tetra               # Imagem TETRA (leve, base softmbe)
  common.sh                      # Utilitários de build e cache compartilhados
  build-softmbe-packages.sh      # Compila mbelib + codecserver-softmbe
  build-tetra-packages.sh        # Compila osmo-tetra + codec ETSI ACELP
  install-softmbe-packages.sh    # Instala softmbe na imagem runtime
  install-tetra-packages.sh      # Instala TETRA na imagem runtime
  files/
    patch_tetra.py               # Patcha arquivos OpenWebRX+ em build time
    csdr_chain_tetra.py          # CSDR chain: IQ → PCM (instala em csdr/chain/)
    csdr_module_tetra.py         # Wrapper subprocess para tetra_decoder.py
    tetra_decoder.py             # Pipeline completo: IQ → tetra-rx → áudio
htdocs/
  plugins/receiver/tetra/
    tetra.js                     # Plugin frontend (publicado no GitHub Pages)
    tetra.css                    # Estilos do painel
.github/workflows/
  pages.yml                      # Publica htdocs/ no GitHub Pages automaticamente
```

---

## Cadeia de Build

### Build completo (pesado — compila OpenWebRX+ do fonte)
```bash
./run build
# Encadeia automaticamente: main → softmbe → tetra
```

### Builds leves (usam imagens do Docker Hub como base)
```bash
./run build-softmbe   # FROM slechev/openwebrxplus → encadeia build-tetra
./run build-tetra     # FROM slechev/openwebrxplus-softmbe (standalone)
```

`build-softmbe` e `build-tetra` **não** requerem servidor APT cache. Ignoram
`SOURCES_SCRIPTS_FINGERPRINT`, `OWRX_REPO_COMMIT` e `FINAL_CACHE_BUSTER`.

---

## Integração TETRA — Arquitetura Completa

### Fluxo de dados ponta a ponta

```
SDR hardware
    │ IQ bruto (taxa SDR, ex. 2.4 MS/s)
    ▼
rtl_connector / sdrplay_connector
    │ IQ bruto
    ▼
OpenWebRX+ (Python)
    │ Filtra banda, reamostrar para 36 kS/s
    │ complex float32 (I e Q intercalados, 8 bytes por amostra)
    ▼
TetraDecoderModule (PopenModule)  ← csdr/modules/tetra.py
    │ stdin: complex float32 IQ @ 36 kS/s
    │ (subprocess.PIPE → stdin do tetra_decoder.py)
    ▼
tetra_decoder.py
    │ Passa IQ diretamente ao tetra-rx via fd 0 (stdin=0)
    ▼
tetra-rx -i -a -r -s -e /dev/stdin
    │ -i: demodulador interno float_to_bits (pi/4-DQPSK)
    │ -a: pseudo-AFC (corrige offset de frequência do SDR)
    │ -r: reagrupa PDUs fragmentados
    │ -s: exibe tipos de SDS desconhecidos como texto
    │ -e: processa pacotes encriptados (retorna metadados, não decifra)
    │
    ├─→ UDP 127.0.0.1:7379 (TETMON) — metadados + áudio
    │       │
    │       ├─→ pacotes sem TRA: → _parse_tetmon() → JSON → stderr
    │       │                                              ↓
    │       │                                   csdr_module_tetra.py lê stderr
    │       │                                   adiciona protocol:"TETRA"
    │       │                                   pickle → meta_writer
    │       │                                   OpenWebRX+ → WebSocket → frontend
    │       │
    │       └─→ pacotes com TRA: (áudio ACELP) → sdecoder → PCM → stdout
    │
    └─→ stdout: DEVNULL (com TETMON ativo, tudo vai via UDP)
```

### Stage 1 — Build dos binários (`build-tetra-packages.sh`)

Roda em `FROM debian:bookworm-slim`:

1. Instala `libosmocore-dev` do apt (não precisa compilar do fonte)
2. Clona [osmo-tetra-sq5bpf](https://github.com/sq5bpf/osmo-tetra-sq5bpf) com `--depth 1`
3. Baixa o codec ETSI ACELP do ZIP oficial ETSI
4. Aplica `codec.diff`, compila `cdecoder` e `sdecoder`
5. Compila `tetra-rx` via `make` em `src/`
6. Exporta para `/build_artifacts/tetra/`

**URL do codec ETSI:**
```
http://www.etsi.org/deliver/etsi_en/300300_300399/30039502/01.03.01_60/en_30039502v010301p0.zip
```
Se a URL mudar, atualizar em `build-tetra-packages.sh`.
O `unzip -q -L` (flag `-L` = lowercase) é obrigatório no Linux para o patch aplicar.

### Stage 2 — Runtime (`install-tetra-packages.sh`)

Roda em `FROM slechev/openwebrxplus-softmbe:latest`:

1. Instala `libosmocore` (runtime, não dev)
2. Copia binários para `/opt/openwebrx-tetra/`
3. Instala `csdr_chain_tetra.py` → `csdr/chain/tetra.py`
4. Instala `csdr_module_tetra.py` → `csdr/modules/tetra.py`
5. Executa `patch_tetra.py` — falha o build se não conseguir patchar

### `patch_tetra.py` — Patcha OpenWebRX+ em build time

Patcha três arquivos do pacote `owrx`:

| Arquivo | Patch | Anchor |
|---------|-------|--------|
| `modes.py` | Insere `AnalogMode("tetra", ...)` | antes do entry `nxdn` |
| `feature.py` | Registra feature `tetra_decoder` | appended ao final |
| `dsp.py` | Insere `elif demod == "tetra":` | antes do bloco `nxdn` |

Cada patch tem estratégia de fallback. O build **falha em voz alta** se impossível patchar.

**Se o build falhar em `patch_tetra.py` após upgrade do upstream:**
- `modes.py`: ainda tem `AnalogMode("nxdn"...)`?
- `dsp.py`: ainda tem `elif demod == "nxdn":`?
- `feature.py`: ainda usa dict `FeatureDetector.features` + padrão `has_*`?

---

## tetra-rx — Interface Completa

O binário central. Versão: [osmo-tetra-sq5bpf](https://github.com/sq5bpf/osmo-tetra-sq5bpf).

### Flags
```
tetra-rx [-i] [-a] [-r] [-s] [-e] [-f filter_constant] [-h] <arquivo>
  -i   Aceita float32 IQ (demodulador interno float_to_bits)
  -a   Pseudo-AFC automático (só com -i) — corrige offset de frequência SDR
  -r   Reagrupa PDUs fragmentados
  -s   Exibe tipos SDS desconhecidos como texto
  -e   Processa pacotes encriptados (metadados válidos, áudio é lixo)
  -f   Constante do filtro de média AFC (default: 0.0001)
  -h   Ajuda
```

### Como chamamos no projeto
```python
[TETRA_RX, "-i", "-a", "-r", "-s", "-e", "/dev/stdin"]
# stdin=0  →  herda fd 0 do processo pai (o pipe IQ do OpenWebRX+)
```

### Variáveis de ambiente obrigatórias para TETMON
```python
tetra_env["TETRA_HACK_PORT"] = "7379"
tetra_env["TETRA_HACK_IP"]   = "127.0.0.1"
tetra_env["TETRA_HACK_RXID"] = "1"    # CRÍTICO: sem isto → atoi(NULL) → SIGSEGV
```

**`TETRA_HACK_RXID` é OBRIGATÓRIO.** O código C em `tetra_rx.c` faz:
```c
if (getenv("TETRA_HACK_PORT")) {
    tetra_hack_rxid = atoi(getenv("TETRA_HACK_RXID"));  // NULL → SIGSEGV
}
```
Se `TETRA_HACK_PORT` está definido mas `TETRA_HACK_RXID` não, o processo faz SIGSEGV imediatamente (exit code 139).

### Por que `-i` e não demodulador externo?
O tetra-rx da variante sq5bpf tem demodulador pi/4-DQPSK **embutido em C** ativado pelo `-i`. Aceita o mesmo formato que o OpenWebRX+ entrega (complex float32 interleaved). Usar `-i` elimina a necessidade de GNURadio ou qualquer demodulador Python externo. **Sempre usar `-i`.**

### Modo TETMON
Com `TETRA_HACK_PORT` definido, tetra-rx envia todos os dados via UDP:
- Pacotes de metadados: `TETMON_begin FUNC:<tipo> ... TETMON_end`
- Pacotes de áudio: contêm marcador binário `TRA:` seguido de 1380 bytes ACELP
- **stdout fica vazio** com TETMON ativo — usar `stdout=subprocess.DEVNULL`

---

## sdecoder / cdecoder — Interface

Binários do codec ETSI ACELP compilados de `en_30039502v010301p0.zip`.

### Interface (CRÍTICO)
```
sdecoder  input_file  output_file
cdecoder  input_file  output_file
```

**Ambos exigem dois argumentos de arquivo.** Sem eles, imprimem usage e saem com código 1 imediatamente (tornam-se zumbis no processo pai).

Para modo streaming (pipe), usar `/dev/stdin` e `/dev/stdout`:
```python
subprocess.Popen(
    [SDECODER, "/dev/stdin", "/dev/stdout"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
)
```

### Papel de cada binário
- **`cdecoder`**: decodificador de canal — converte bits de canal em frames ACELP de fala
- **`sdecoder`**: decodificador de fala — converte frames ACELP em PCM 16-bit @ 8 kHz

### Para o path de áudio TETMON
Os frames `TRA:` do TETMON já saem do tetra-rx **channel-decoded**. Portanto:
- Usar **apenas `sdecoder`** (não o pipeline cdecoder → sdecoder)
- `cdecoder` seria necessário apenas se processando bits de canal brutos

### Formato
- Entrada sdecoder: 138 × 2 bytes por frame (cada bit como word 16-bit, BFI + 137 bits)
- Um frame TRA: = 1380 bytes = 5 frames ACELP no formato 16-bit word
- Saída sdecoder: PCM signed 16-bit LE @ 8 kHz

---

## Protocolo TETMON

Formato UDP enviado pelo tetra-rx para `TETRA_HACK_IP:TETRA_HACK_PORT`.

### Pacotes de metadados
```
TETMON_begin FUNC:<TIPO> CAMPO1:valor1 CAMPO2:valor2 ... TETMON_end
```

Tipos relevantes:

| FUNC | Descrição | Campos |
|------|-----------|--------|
| `NETINFO1` | Info da rede | `MCC`, `MNC`, `DL`, `UL`, `CC`, `LA`, `CRYPT` |
| `FREQINFO1` | Frequências | `DL`, `UL` |
| `ENCINFO1` | Encriptação | `CRYPT`, `ENCMODE` |
| `DSETUPDEC` | Setup de chamada | `SSI`, `SSI2`, `CALLID`, `IDX`, `CRYPT` |
| `DCONNECTDEC` | Conexão | idem |
| `DTXGRANTDEC` | TX concedido | idem |
| `DRELEASEDEC` | Liberação | `SSI`, `CALLID` |
| `DSTATUSDEC` | Status | `SSI`, `SSI2`, `STATUS` |
| `SDSDEC` | SDS (mensagem curta) | `SSI`, `SSI2`, `MSG` |
| `BURST` | Burst decodificado | `IDX`, `AFC`, `RATE` |

### Pacotes de áudio
Detecção: `b"TRA:"` presente nos bytes do datagrama UDP.
```python
tra_pos = data.find(b"TRA:")
frame = data[tra_pos + 4 : tra_pos + 4 + FRAME_BYTES]  # FRAME_BYTES = 1380
```

---

## Arquivos Python — Responsabilidades

### `tetra_decoder.py`
Pipeline principal. Roda como subprocess do `TetraDecoderModule`.

- **stdin**: complex float32 IQ @ 36 kS/s (do OpenWebRX+ via PopenModule)
- **stdout**: PCM signed 16-bit LE @ 8 kHz
- **stderr**: linhas JSON com metadados TETRA

Fluxo interno:
1. Inicia `tetra-rx -i -a -r -s -e /dev/stdin` com `stdin=0` (herda fd 0 = IQ pipe)
2. Thread `tetmon`: escuta UDP 127.0.0.1:7379
   - Pacotes com `TRA:` → `_AudioPipeline.feed()` → PCM queue
   - Outros pacotes → `_parse_tetmon()` → JSON → stderr
3. Thread `tetra-rx-log`: loga stderr do tetra-rx (nível DEBUG)
4. Loop principal: drena PCM queue → stdout; emite silêncio quando vazio

### `csdr_module_tetra.py`
Wrapper `PopenModule` para o `tetra_decoder.py`. Instalado em `csdr/modules/tetra.py`.

Responsabilidades:
- Starta `["python3", "/opt/openwebrx-tetra/tetra_decoder.py"]`
- Thread `tetra-meta-reader`: lê stderr, parseia JSON, adiciona `protocol:"TETRA"` e `mode:"tetra"`, envia via `pickle` ao `_meta_writer`
- `getInputFormat()` → `Format.COMPLEX_FLOAT`
- `getOutputFormat()` → `Format.SHORT`
- `setMetaWriter(writer)` → armazena o writer para envio de metadados

### `csdr_chain_tetra.py`
Chain CSDR que instancia o módulo. Instalado em `csdr/chain/tetra.py`.

- Herda de `BaseDemodulatorChain` (probe em múltiplos paths por compatibilidade de versão)
- `getInputSampleRate()` → 36000
- `getOutputSampleRate()` → 8000
- `setMetaWriter(writer)` → **obrigatório** forwarding para `self._decoder.setMetaWriter(writer)`
  - Sem este método, o DspManager do OpenWebRX+ não consegue conectar o writer e todos os metadados são silenciosamente descartados

---

## Fluxo de Metadados (backend → frontend)

```
tetra_decoder.py
  _parse_tetmon() → _emit() → sys.stderr.buffer.write(json + "\n")
        ↓
csdr_module_tetra.py
  _read_meta() lê stderr linha por linha
  → json.loads(line)
  → msg["protocol"] = "TETRA"
  → msg["mode"] = "tetra"
  → self._meta_writer.write(pickle.dumps(msg))
        ↓
OpenWebRX+ DspManager
  → WebSocket → frontend
        ↓
tetra.js
  TetraMetaPanel.update(data)
  → if (!this.isSupported(data)) return;  // verifica data.protocol === 'TETRA'
  → atualiza painel conforme data.type
```

**Ponto crítico**: o `DspManager` chama `chain.setMetaWriter(writer)` após criar a chain. Se `Tetra.setMetaWriter` não existir ou não forwarding para `TetraDecoderModule.setMetaWriter`, `_meta_writer` fica `None` e todos os metadados são descartados silenciosamente.

---

## Formato das Mensagens de Metadados (backend → frontend)

Todos os objetos JSON incluem `protocol: "TETRA"` e `mode: "tetra"` (adicionados por `csdr_module_tetra.py`).

| `type` | Campos adicionais |
|--------|------------------|
| `netinfo` | `mcc`, `mnc`, `dl_freq`, `ul_freq`, `color_code`, `la`, `encrypted` |
| `freqinfo` | `dl_freq`, `ul_freq` |
| `encinfo` | `encrypted`, `enc_mode` (ex: `"TEA2"`, `"None"`) |
| `burst` | `slot` (0–3), `afc`, `burst_rate` |
| `call_setup` | `issi`, `gssi`, `call_id`, `call_type`, `encrypted`, `slot` |
| `connect` | idem call_setup |
| `tx_grant` | idem call_setup |
| `call_release` | `issi`, `call_id` |
| `status` | `issi`, `to`, `status` |
| `sds` | `from`, `to`, `text` |

### Rate limits (backend, por categoria)

| Categoria | Intervalo mínimo |
|-----------|-----------------|
| `netinfo` | 5 s |
| `freqinfo` | 10 s |
| `encinfo` | 5 s |
| `burst` | 0.25 s |
| `call` (setup/connect/tx_grant) | 0.5 s |
| `release` | 0.1 s |
| `sds` | 1 s |
| `status` | 1 s |

---

## Caminhos dos Arquivos no Container

| Caminho | Conteúdo |
|---------|---------|
| `/opt/openwebrx-tetra/tetra-rx` | Decoder TETRA principal (sq5bpf) |
| `/opt/openwebrx-tetra/cdecoder` | Decoder de canal ETSI ACELP |
| `/opt/openwebrx-tetra/sdecoder` | Decoder de fala ETSI ACELP |
| `/opt/openwebrx-tetra/tetra_decoder.py` | Pipeline Python |
| `/usr/lib/python3/dist-packages/csdr/chain/tetra.py` | CSDR chain |
| `/usr/lib/python3/dist-packages/csdr/modules/tetra.py` | CSDR module |

---

## Frontend (Plugin GitHub Pages)

Hospedado em: `https://xnetinho.github.io/openwebrxplus-docker-builder/plugins/receiver/tetra/tetra.js`

Segue convenções de [0xAF/openwebrxplus-plugins](https://github.com/0xAF/openwebrxplus-plugins):

- **ES5 puro** — `var`, `function () {}`, `.prototype`. Sem classes, sem arrow functions.
- **Indentação com tabs**
- Namespace `Plugins.tetra = Plugins.tetra || {};`
- `init()` retorna `true`/`false`
- Depende de `utils >= 0.1`

### Inicialização
1. `Plugins.utils.on_ready()` → injeta HTML do painel dinamicamente
2. Registra `TetraMetaPanel` em `MetaPanel.types['tetra']`
3. `$('#openwebrx-panel-metadata-tetra').metaPanel()` inicializa o painel

### Exibição
- 4 timeslots TDMA (TS 1–4), cada um com ISSI, GSSI, tipo de chamada
- Seção Network: MCC, MNC, LA, DL Freq, UL Freq, Color Code, Encriptação
- Seção Signal: AFC, Bursts/s
- Seções SDS e Status (aparecem/somem conforme recebem dados)

### Deploy
Qualquer push para `main` que altere `htdocs/` aciona o workflow `pages.yml` e publica no GitHub Pages automaticamente.

---

## Diagnóstico e Debugging

### Verificar se processos estão rodando corretamente
```bash
ps auxw | grep -E 'tetra|sdecoder'
# Esperado:
# python3 /opt/openwebrx-tetra/tetra_decoder.py   (CPU > 0)
# /opt/openwebrx-tetra/tetra-rx -i -a ...          (CPU > 0 com sinal)
# /opt/openwebrx-tetra/sdecoder /dev/stdin /dev/stdout  (CPU 0.0 se TETRA encriptado)
```

### Verificar se TETMON está recebendo pacotes
```bash
tcpdump -i lo -n udp port 7379 -A 2>/dev/null | head -30
# Deve mostrar pacotes com TETMON_begin/TETMON_end
# Se zero pacotes: tetra-rx não está decodificando frames válidos
```

### Verificar variáveis de ambiente do tetra-rx
```bash
cat /proc/<PID_TETRA_RX>/environ | tr '\0' '\n' | grep -E 'TETRA|HACK'
# Deve mostrar: TETRA_HACK_PORT=7379, TETRA_HACK_IP=127.0.0.1, TETRA_HACK_RXID=1
```

### Verificar file descriptors do tetra-rx
```bash
ls -la /proc/<PID_TETRA_RX>/fd/
# fd 0 → pipe (IQ do OpenWebRX+)
# fd 1 → /dev/null
# fd 2 → pipe (stderr)
# fd 3 → mesmo pipe do fd 0 (tetra-rx abriu /dev/stdin)
# fd 4,5,6 → sockets (TETMON UDP criados com TETRA_HACK_PORT)
# Se não tiver fd 4,5,6: TETMON não está ativo (checar env vars)
```

### tetra-rx não envia pacotes UDP (tcpdump vazio)
Causas possíveis, em ordem de probabilidade:
1. Frequência errada — não há sinal TETRA na frequência sintonizada
2. `TETRA_HACK_RXID` não definido → SIGSEGV (checar se processo está vivo)
3. Flags erradas — verificar que `-i` está presente
4. Sinal fraco demais — AFC não consegue travar

### sdecoder / cdecoder tornando-se zumbis imediatamente
**Causa**: chamados sem argumentos. Ambos exigem `input_file output_file`.
**Solução**: usar `[SDECODER, "/dev/stdin", "/dev/stdout"]` no Popen.

### Frontend em branco (painel TETRA não exibe dados)
1. Verificar se `tetra-rx` está enviando TETMON (tcpdump acima)
2. Verificar se `Tetra.setMetaWriter()` existe em `csdr/chain/tetra.py`
3. Para TETRA encriptado: dados de controle (netinfo, burst, chamadas) devem aparecer — áudio não
4. Verificar console do browser (F12) para erros do plugin

---

## Lições Aprendidas (Armadilhas Críticas)

Esta seção documenta os erros cometidos durante o desenvolvimento para que não sejam repetidos.

### 1. `TETRA_HACK_RXID` é obrigatório
**Sintoma**: tetra-rx faz SIGSEGV (exit 139) sempre que `TETRA_HACK_PORT` está definido.
**Causa**: `atoi(getenv("TETRA_HACK_RXID"))` — se `RXID` não estiver definido, `getenv` retorna NULL e `atoi(NULL)` é UB/SIGSEGV.
**Solução**: sempre definir as três variáveis juntas.

### 2. tetra-rx tem demodulador interno — usar `-i`
**Erro**: implementamos um demodulador pi/4-DQPSK em numpy, depois substituímos por GNURadio.
**Verdade**: `tetra-rx -h` mostra `-i accept float values (internal float_to_bits)`. O demodulador em C já estava lá desde o início.
**Solução**: sempre usar `tetra-rx -i` quando a entrada for IQ float32.
**Lição**: **leia o `--help` do binário antes de escrever código que o chama**.

### 3. Com TETMON ativo, stdout do tetra-rx fica vazio
**Erro**: pipeline tentava ler áudio de `tetra_rx.stdout`.
**Causa**: com `TETRA_HACK_PORT` definido, tetra-rx envia tudo via UDP. stdout não recebe dados.
**Solução**: `stdout=subprocess.DEVNULL`; áudio vem de UDP com marcador `TRA:`.

### 4. sdecoder/cdecoder exigem argumentos de arquivo
**Sintoma**: `[cdecoder] <defunct>`, `[sdecoder] <defunct>` imediatamente ao iniciar.
**Causa**: chamados como `Popen([SDECODER], stdin=PIPE, stdout=PIPE)` sem argumentos.
**Verdade**: `sdecoder` e `cdecoder` são ferramentas de linha de comando que exigem `input_file output_file`.
**Solução**: `Popen([SDECODER, "/dev/stdin", "/dev/stdout"], stdin=PIPE, stdout=PIPE)`.

### 5. `Tetra.setMetaWriter()` deve ser forwarded
**Sintoma**: frontend sempre em branco mesmo com TETMON funcionando.
**Causa**: `DspManager` chama `chain.setMetaWriter(writer)`. Sem este método na chain, `TetraDecoderModule._meta_writer` fica `None` e todos os metadados são descartados silenciosamente.
**Solução**: `def setMetaWriter(self, writer): self._decoder.setMetaWriter(writer)` na classe `Tetra`.

### 6. GNURadio não está disponível no apt da imagem base
**Erro**: tentamos instalar `python3-gnuradio` via apt.
**Causa**: o pacote não existe nos repositórios Debian bookworm da imagem base slechev/openwebrxplus-softmbe.
**Solução**: usar `-i` do tetra-rx (item 2 acima). GNURadio não é necessário.

### 7. Para áudio TETMON, usar apenas sdecoder (não cdecoder → sdecoder)
**Causa do erro**: a pipeline de dois estágios (cdecoder | sdecoder) é para processar bits de canal brutos. Os frames `TRA:` do TETMON já saem channel-decoded do tetra-rx.
**Solução**: frames TRA: vão direto para sdecoder.

---

## Atualização de Versão do OpenWebRX+

Quando `slechev/openwebrxplus-softmbe` atualizar no Docker Hub:

1. `./run build-tetra` — o flag `--pull` busca a última base
2. Se o build falhar em `patch_tetra.py`, os anchors mudaram. Verificar:
   - `modes.py`: ainda tem `AnalogMode("nxdn"...)`?
   - `dsp.py`: ainda tem `elif demod == "nxdn":`?
   - `feature.py`: ainda usa dict `FeatureDetector.features` + método `has_*`?
3. Atualizar anchors em `patch_tetra.py`
4. Se `dsp.py` foi refatorado (o `# TODO: move this to Modes` foi resolvido), remover o patch de `dsp.py` e registrar só via `modes.py`

---

## Regras de Desenvolvimento do Frontend

- ES5: `var`, `function () {}`, `.prototype` — sem classes, sem arrow functions
- Tabs, não espaços
- Sempre declarar `Plugins.tetra = Plugins.tetra || {};` no topo
- Atualizar `_version` quando mudar comportamento
- Debug: `Plugins._enable_debug = true` em `init.js`; `Plugins.utils._DEBUG_ALL_EVENTS = true` para eventos
- Push para `main` publica automaticamente via GitHub Pages
