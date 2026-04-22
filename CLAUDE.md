# CLAUDE.md — Guia de Desenvolvimento

Fork de [0xAF/openwebrxplus-docker-builder](https://github.com/0xAF/openwebrxplus-docker-builder) que adiciona decodificação TETRA ao OpenWebRX+.

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
    tetra_decoder.py             # Pipeline: DQPSK demod + tetra-rx + áudio
```

---

## Fluxo de Dados Ponta a Ponta

```
OpenWebRX+ → complex float32 IQ @ 36 kS/s
    ↓
TetraDecoderModule (PopenModule) → stdin tetra_decoder.py
    ↓
tetra_decoder.py
  _dqpsk_demod_thread (numpy)
    - lê IQ complex64 do stdin em blocos alinhados a 16 bytes
    - decima por 2: z = iq[::2]  (36kS/s → 18kS/s, 1 amostra/símbolo)
    - fase diferencial: angle(z[k] * conj(z[k-1])) / (pi/4)
    - escreve float32 no stdin do tetra-rx via PIPE
    ↓
tetra-rx -i -a -r -s -e /dev/stdin
    - -i: float_to_bits interno (quantiza floats → símbolos {-3,-1,+1,+3})
    - -a: pseudo-AFC (corrige offset de frequência)
    ↓
  UDP 127.0.0.1:7379 (TETMON)
    ├─ pacotes sem TRA: → _parse_tetmon() → JSON → stderr
    │      → csdr_module_tetra.py lê stderr → pickle → MetaWriter → WebSocket
    └─ pacotes com TRA: (áudio ACELP) → sdecoder → PCM → stdout
```

**CRÍTICO**: `tetra-rx -i` aceita floats de fase pré-demodulados (saída do
demodulador DQPSK), NÃO IQ complexo bruto. A flag `-i` faz apenas
`float_to_bits` internamente — a demodulação pi/4-DQPSK é responsabilidade
do `_dqpsk_demod_thread` em `tetra_decoder.py`.

---

## Cadeia de Build

```bash
./run build-tetra    # FROM slechev/openwebrxplus-softmbe (leve, recomendado)
./run build-softmbe  # reconstrói softmbe + encadeia tetra
./run build          # build completo (pesado, precisa de apt-cache)
```

---

## tetra-rx — Interface

Binário: [osmo-tetra-sq5bpf](https://github.com/sq5bpf/osmo-tetra-sq5bpf)

```
tetra-rx -i -a -r -s -e /dev/stdin
  -i   Aceita float32 por símbolo (NÃO IQ — veja abaixo)
  -a   Pseudo-AFC (só com -i)
  -r   Reagrupa PDUs fragmentados
  -s   SDS desconhecidos como texto
  -e   Processa chamadas encriptadas (metadados válidos, áudio não)
```

**O que -i realmente aceita**: um float por símbolo representando a mudança
de fase em unidades de pi/4. O pipeline correto é:
```
IQ (complex64) → demodulador pi/4-DQPSK → float32/símbolo → tetra-rx -i
```

### Variáveis de ambiente obrigatórias
```python
tetra_env["TETRA_HACK_PORT"] = "7379"
tetra_env["TETRA_HACK_IP"]   = "127.0.0.1"
tetra_env["TETRA_HACK_RXID"] = "1"   # CRÍTICO: atoi(NULL) = SIGSEGV sem isto
```

---

## Arquivos Python

### `tetra_decoder.py`
Pipeline principal. Roda como subprocess do `TetraDecoderModule`.

- **stdin**: IQ complex float32 @ 36 kS/s
- **stdout**: PCM signed 16-bit LE @ 8 kHz
- **stderr**: JSON metadata

Threads internas:
1. `dqpsk-demod`: lê IQ, demodula, escreve floats no tetra-rx stdin
2. `tetmon`: escuta UDP 7379 — audio TRA: → sdecoder; outros → JSON stderr
3. `tetra-rx-log`: loga stderr do tetra-rx
4. Loop principal: drena PCM queue → stdout; silêncio quando vazia

### `csdr_module_tetra.py`
Wrapper `PopenModule`. Override de `_getProcess()` adiciona `stderr=PIPE`
(sem isso, `self.process.stderr` é None e a thread de metadados nunca inicia).

### `csdr_chain_tetra.py`
Chain CSDR. `getInputSampleRate()=36000`, `getOutputSampleRate()=8000`.
`setMetaWriter()` faz forward para `TetraDecoderModule.setMetaWriter()`.

### `patch_tetra.py`
Patcha três arquivos do pacote `owrx` em build time:
- `modes.py`: insere `AnalogMode("tetra", ...)` antes do entry `nxdn`
- `feature.py`: registra feature `tetra_decoder`
- `dsp.py`: insere `elif demod == "tetra":` antes do bloco `nxdn`

---

## sdecoder / cdecoder

Codec ETSI ACELP. **Exigem dois argumentos de arquivo**:
```python
Popen([SDECODER, "/dev/stdin", "/dev/stdout"], stdin=PIPE, stdout=PIPE)
```
Sem argumentos → zombie imediato (exit 1).

Frames TRA: do TETMON já saem channel-decoded → usar **só sdecoder**.
- Entrada: 1380 bytes = 5 frames ACELP (138 × 2 bytes, bit como word 16-bit)
- Saída: PCM signed 16-bit LE @ 8 kHz

---

## Protocolo TETMON

```
TETMON_begin FUNC:<TIPO> CAMPO:valor ... TETMON_end
```

Tipos: `NETINFO1`, `FREQINFO1`, `ENCINFO1`, `DSETUPDEC`, `DCONNECTDEC`,
`DTXGRANTDEC`, `DRELEASEDEC`, `DSTATUSDEC`, `SDSDEC`, `BURST`.

Audio: detectado por `b"TRA:"` no datagrama UDP.

Com `TETRA_HACK_PORT` ativo, stdout do tetra-rx fica vazio → usar `stdout=DEVNULL`.

---

## Caminhos no Container

| Caminho | Conteúdo |
|---------|----------|
| `/opt/openwebrx-tetra/tetra-rx` | Decoder TETRA (sq5bpf) |
| `/opt/openwebrx-tetra/sdecoder` | Decoder ACELP (fala) |
| `/opt/openwebrx-tetra/cdecoder` | Decoder ACELP (canal) |
| `/opt/openwebrx-tetra/tetra_decoder.py` | Pipeline Python |
| `/usr/lib/python3/dist-packages/csdr/chain/tetra.py` | CSDR chain |
| `/usr/lib/python3/dist-packages/csdr/modules/tetra.py` | CSDR module |

---

## Diagnóstico

```bash
# Processos rodando
ps auxw | grep -E 'tetra|sdecoder'

# TETMON recebendo dados
tcpdump -i lo -n udp port 7379 -A 2>/dev/null | head -20

# Env vars do tetra-rx
cat /proc/<PID>/environ | tr '\0' '\n' | grep TETRA

# File descriptors (stdin deve ser PIPE, não fd 0 herdado)
ls -la /proc/<PID>/fd/
```

Se tcpdump vazio: frequência errada, sinal fraco, ou TETRA_HACK_RXID não definido.

---

## Lições Aprendidas

### 1. `TETRA_HACK_RXID` é obrigatório
`atoi(getenv("TETRA_HACK_RXID"))` → SIGSEGV se RXID não estiver definido.
Sempre definir as três variáveis TETRA_HACK_* juntas.

### 2. `tetra-rx -i` aceita floats de fase, NÃO IQ complexo
**Erro anterior**: assumiu-se que `-i` fazia demodulação IQ completa internamente.
**Verdade**: `-i` faz apenas `float_to_bits` (quantiza floats → símbolos).
A demodulação pi/4-DQPSK (IQ → phase floats) ainda é necessária externamente.
**Solução**: `_dqpsk_demod_thread` em `tetra_decoder.py` usa numpy para:
  1. Ler IQ complex64 do stdin
  2. Decimar por 2 (36kS/s → 18kS/s)
  3. Fase diferencial: `angle(z[k]*conj(z[k-1])) / (pi/4)`
  4. Escrever float32 no stdin do tetra-rx via PIPE

### 3. `PopenModule` não captura stderr por padrão
`PopenModule._getProcess()` usa apenas `stdin=PIPE, stdout=PIPE`.
Sem `stderr=PIPE`, `self.process.stderr` é None e a thread de metadados
nunca inicia — silenciosamente, causando painel frontend sempre vazio.
**Solução**: override de `_getProcess()` em `TetraDecoderModule`.

### 4. GNURadio não é necessário
O projeto original (trollminer) usa GNURadio para a demodulação DQPSK.
Não é necessário — numpy é suficiente e está disponível no Debian.

### 5. Com TETMON ativo, stdout do tetra-rx fica vazio
Tudo vai via UDP. Usar `stdout=DEVNULL`; áudio vem de UDP com `TRA:`.

### 6. sdecoder/cdecoder exigem argumentos de arquivo
Chamados sem args → zombie imediato. Usar `/dev/stdin /dev/stdout`.

### 7. `Tetra.setMetaWriter()` deve ser forwarded
Sem forward para `TetraDecoderModule.setMetaWriter()`, `_meta_writer`
fica None e todos os metadados são descartados silenciosamente.

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
