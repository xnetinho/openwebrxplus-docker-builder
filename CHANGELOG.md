# CHANGELOG

## [Release] v1.0.0 — Restauração do GNURadio (2026-04-22)

### O que foi modificado

- [x] `buildfiles/files/tetra_demod.py` — **[NOVO]** Demodulador GNURadio pi/4-DQPSK (original do trollminer/SP8MB) com cadeia completa: AGC → FLL → Clock Recovery → Equalizer CMA → Diff Phasor → Constellation Decoder
- [x] `buildfiles/files/tetra_decoder.py` — **[REESCRITO]** Restaurado pipeline original: tetra_demod.py (subprocess GNURadio) → tetra-rx (sem -i) → TETMON UDP → CodecPipeline (cdecoder|sdecoder) → PCM
- [x] `buildfiles/Dockerfile-tetra` — Adicionado `gnuradio` como dependência de runtime; adicionado COPY do `tetra_demod.py`
- [x] `CLAUDE.md` — Reescrito com nova arquitetura (GNURadio em vez de numpy)

### Motivo

O demodulador NumPy simplificado (adicionado pela IA anterior na branch `claude/debug-backend-issues-eKgu6`) realizava apenas decimação por 2 e fase diferencial. Faltavam completamente: AGC, FLL (sincronização de frequência), Clock Recovery com filtro RRC polifásico, e Equalização adaptativa CMA. Sem esses estágios de DSP, os símbolos produzidos eram inválidos e o `tetra-rx` nunca conseguia sincronizar com o sinal TETRA real, resultando em 0% de CPU e zero pacotes TETMON UDP.

### Diferenças chave em relação à versão anterior

| Aspecto | Versão Anterior (numpy) | Versão Atual (GNURadio) |
|---------|------------------------|------------------------|
| Demodulação | 2 operações (decimate+phase) | 7 estágios DSP completos |
| tetra-rx flags | `-i -a` (float input + AFC) | `-r -s -e` (reassemble + SDS + encrypted) |
| Dependência runtime | `python3-numpy` (~5MB) | `gnuradio` (~200MB) |
| Porta TETMON | fixa 7379 | dinâmica via `find_free_port()` |
| Codec | sdecoder apenas | cdecoder → sdecoder (pipeline completo) |
