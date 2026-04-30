# CLAUDE.md — OpenWebRX+ TETRA Docker Builder

Cadeia de imagens Docker:
```
slechev/openwebrxplus → slechev/openwebrxplus-softmbe → xnetinho/openwebrxplus-tetra
```

---

## Estrutura do Repositório

```
run                              # CLI: build, run, dev
buildfiles/
  Dockerfile-tetra               # Imagem TETRA (stage builder + runtime)
  build-tetra-packages.sh        # Compila osmo-tetra + ETSI ACELP
  install-tetra-packages.sh      # Instala TETRA na imagem runtime
  common.sh
  files/
    patch_tetra.py               # Patcha OpenWebRX+ em build time
    csdr_chain_tetra.py          # CSDR chain (input: IQ 36kS/s, output: PCM 8kHz)
    csdr_module_tetra.py         # Wrapper PopenModule para tetra_decoder.py
    tetra_decoder.py             # Pipeline: demod + tetra-rx + codec
    tetra_demod.py               # Demodulador GNURadio pi/4-DQPSK
```

---

## Fluxo de Dados Ponta a Ponta

```
OpenWebRX+ → complex float32 IQ @ 36 kS/s
    ↓
TetraDecoderModule (PopenModule) → stdin tetra_decoder.py
    ↓
tetra_decoder.py
  ├→ inicia tetra_demod.py (stdin=0, herda IQ do fd 0)
  │     ↓
  │   tetra_demod.py (GNURadio)
  │     AGC → FLL → Clock Recovery (PFB/RRC) → Equalizer CMA
  │     → Diff Phasor → Constellation Decoder → bits (1 bit/byte)
  │     → stdout PIPE
  │
  ├→ inicia tetra-rx (stdin = demod.stdout)
  │     tetra-rx -r -s -e /dev/stdin
  │     → TETMON UDP 127.0.0.1:<porta_dinâmica>
  │
  ├→ UDP listener (porta dinâmica)
  │     ├─ pacotes sem TRA: → parse_metadata_from_udp() → JSON → stderr
  │     │      → csdr_module_tetra.py lê stderr → pickle → MetaWriter → WebSocket
  │     └─ pacotes com TRA: (ACELP frames) → CodecPipeline (cdecoder|sdecoder) → PCM
  │
  └→ Main loop: PCM ou silêncio → stdout
```

**IMPORTANTE**: `tetra-rx` NÃO usa a flag `-i` com o demodulador GNURadio.
A flag `-i` é para floats de fase pré-demodulados (modo numpy, removido).
O GNURadio produz BYTES (bits descompactados, 1 bit por byte) → tetra-rx lê direto.

---

## Cadeia de Build

```bash
./run build-tetra    # FROM slechev/openwebrxplus-softmbe (recomendado)
./run build-softmbe  # reconstrói softmbe + encadeia tetra
./run build          # build completo (pesado, precisa de apt-cache)
```

---

## Dependências de Runtime

| Pacote | Propósito |
|--------|-----------|
| `gnuradio` | Demodulador DQPSK (AGC, FLL, Clock, EQ, Constellation) |
| `python3-numpy` | Dependência indireta do GNURadio |
| `libosmocore` | Biblioteca base para tetra-rx |

---

## tetra-rx — Interface

Binário: [osmo-tetra-sq5bpf](https://github.com/sq5bpf/osmo-tetra-sq5bpf)

```
tetra-rx -r -s -e /dev/stdin
  -r   Reagrupa PDUs fragmentados
  -s   SDS desconhecidos como texto
  -e   Processa chamadas encriptadas (metadados válidos, áudio não)
```

**NÃO usar `-i` com GNURadio.** A flag `-i` aceita floats, não bits.

### Variáveis de ambiente obrigatórias
```python
env['TETRA_HACK_PORT'] = str(udp_port)  # porta dinâmica via find_free_port()
env['TETRA_HACK_IP']   = '127.0.0.1'
env['TETRA_HACK_RXID'] = '1'   # CRÍTICO: atoi(NULL) = SIGSEGV sem isto
```

---

## Arquivos Python

### `tetra_demod.py`
Demodulador GNURadio pi/4-DQPSK. Cadeia completa:
1. `file_descriptor_source` (fd 0 = stdin, IQ complex float32)
2. `feedforward_agc_cc` (AGC)
3. `fll_band_edge_cc` (sincronização de frequência)
4. `pfb_clock_sync_ccf` (clock recovery com filtro RRC polifásico)
5. `linear_equalizer` (equalização adaptativa CMA)
6. `diff_phasor_cc` (extração de fase diferencial)
7. `constellation_decoder_cb` + `map_bb` + `unpack_k_bits_bb` (decodificação)
8. `file_descriptor_sink` (fd 1 = stdout, bytes)

AFC Probe: lê frequência do FLL, emite JSON no stderr a cada 2s.

### `tetra_decoder.py`
Pipeline principal. Roda como subprocess do `TetraDecoderModule`.

- **stdin**: IQ complex float32 @ 36 kS/s (via PopenModule PIPE)
- **stdout**: PCM signed 16-bit LE @ 8 kHz
- **stderr**: JSON metadata

Subprocessos internos:
1. `tetra_demod.py`: GNURadio DQPSK (stdin=0 herda IQ, stdout=PIPE → tetra-rx)
2. `tetra-rx`: decodifica bits L1/L2/L3, emite TETMON via UDP
3. `CodecPipeline`: cdecoder|sdecoder para frames ACELP

Threads:
1. `parse_tetra_rx_stdout`: lê stdout do tetra-rx para timeslot/call info
2. `read_demod_stderr`: lê AFC do demodulador
3. Loop principal: UDP listener para TETMON + emissão de metadados/PCM

### `csdr_module_tetra.py`
Wrapper `PopenModule`. Override de `_getProcess()` adiciona `stderr=PIPE`
para a thread de metadados poder ler JSON do `tetra_decoder.py`.

### `csdr_chain_tetra.py`
Chain CSDR. `getInputSampleRate()=36000`, `getOutputSampleRate()=8000`.
`setMetaWriter()` faz forward para `TetraDecoderModule.setMetaWriter()`.

### `patch_tetra.py`
Patcha três arquivos do pacote `owrx` em build time:
- `modes.py`: insere `AnalogMode("tetra", ...)` antes do entry `nxdn`
- `feature.py`: registra feature `tetra_decoder`
- `dsp.py`: insere `elif demod == "tetra":` antes do bloco `nxdn`

---

## CodecPipeline (cdecoder | sdecoder)

Pipeline persistente: `cdecoder → sdecoder`
- **cdecoder**: channel decoding (entrada: 1380 bytes ACELP, saída: intermediate)
- **sdecoder**: speech decoding (entrada: intermediate, saída: 640 bytes PCM = 2 × 320-byte sub-frames)
- Ambos requerem argumentos: `/dev/stdin /dev/stdout`
- Sem argumentos → zombie imediato (exit 1)

---

## Protocolo TETMON

```
TETMON_begin FUNC:<TIPO> CAMPO:valor ... TETMON_end
```

Tipos: `NETINFO1`, `FREQINFO1`, `ENCINFO1`, `DSETUPDEC`, `DCONNECTDEC`,
`DTXGRANTDEC`, `DRELEASEDEC`, `DSTATUSDEC`, `SDSDEC`, `BURST`.

Audio: detectado por `b"TRA:"` no datagrama UDP, seguido de header e 1380 bytes ACELP.
  - v1 (nosso build): `TRA:HH RX:HH\x00` (13-byte header, sem DECR)
  - v2 (sq5bpf-2): `TRA:HH RX:HH DECR:i\x00` (20-byte header, com DECR)

---

## Caminhos no Container

| Caminho | Conteúdo |
|---------|----------|
| `/opt/openwebrx-tetra/tetra-rx` | Decoder TETRA (sq5bpf) |
| `/opt/openwebrx-tetra/sdecoder` | Decoder ACELP (fala) |
| `/opt/openwebrx-tetra/cdecoder` | Decoder ACELP (canal) |
| `/opt/openwebrx-tetra/tetra_decoder.py` | Pipeline Python |
| `/opt/openwebrx-tetra/tetra_demod.py` | Demodulador GNURadio |
| `/usr/lib/python3/dist-packages/csdr/chain/tetra.py` | CSDR chain |
| `/usr/lib/python3/dist-packages/csdr/module/tetra.py` | CSDR module |

---

## Diagnóstico

```bash
# Processos rodando (todos devem ter CPU > 0 com sinal presente)
ps auxw | grep -E 'tetra|sdecoder|cdecoder|gnuradio'

# GNURadio instalado?
python3 -c 'from gnuradio import gr; print(gr.version())'

# TETMON recebendo dados (usar porta do find_free_port)
# Descobrir a porta: ss -ulnp | grep python
tcpdump -i lo -n udp port <porta> -A 2>/dev/null | head -20

# Env vars do tetra-rx
cat /proc/<PID>/environ | tr '\0' '\n' | grep TETRA

# File descriptors
ls -la /proc/<PID>/fd/
```

Se tcpdump vazio: frequência errada, sinal fraco, ou demodulador falhando.

---

## Lições Aprendidas

### 1. GNURadio É necessário
O demodulador numpy simplificado (decimação + fase diferencial) é insuficiente
para sinal TETRA real. A cadeia GNURadio completa (AGC → FLL → Clock Recovery
→ Equalizer → DQPSK) é necessária para sincronização robusta.

### 2. `TETRA_HACK_RXID` é obrigatório
`atoi(getenv("TETRA_HACK_RXID"))` → SIGSEGV se RXID não estiver definido.

### 3. `PopenModule` não captura stderr por padrão
Override de `_getProcess()` em `TetraDecoderModule` é necessário.

### 4. A porta UDP é dinâmica
O `tetra_decoder.py` usa `find_free_port()` para evitar conflitos quando
múltiplas instâncias rodam em paralelo (múltiplos SDRs).

### 5. `tetra-rx -i` vs sem `-i`
- **Com GNURadio**: NÃO usar `-i`. GNURadio produz bits (bytes), tetra-rx lê direto.
- **Com numpy (descontinuado)**: usava `-i` para floats de fase.

### 6. `stdin=0` herda o fd do parent
`tetra_demod.py` usa `stdin=0` (file descriptor 0 do parent). Como
`tetra_decoder.py` nunca lê do seu stdin, o demod herda a PIPE do
PopenModule e recebe IQ diretamente.

---

## Atualização do OpenWebRX+

Ao atualizar `slechev/openwebrxplus-softmbe`:
1. `./run build-tetra` — `--pull` busca a última base
2. Se `patch_tetra.py` falhar, os anchors mudaram:
   - `modes.py`: ainda tem `AnalogMode("nxdn"...)`?
   - `dsp.py`: ainda tem `elif demod == "nxdn":`?
   - `feature.py`: ainda usa `FeatureDetector.features` dict?

---

## Regras do Frontend

- ES5: `var`, `function(){}`, `.prototype` — sem classes, sem arrow functions
- Tabs, não espaços
- `Plugins.tetra = Plugins.tetra || {};` no topo
- Push para `main` publica via GitHub Pages automaticamente
