# OpenWebRX+ TETRA Docker Builder

> Fork of [0xAF/openwebrxplus-docker-builder](https://github.com/0xAF/openwebrxplus-docker-builder) with added TETRA digital radio decoding support.

---

## English

### What is this?

This repository builds Docker images for [OpenWebRX+](https://github.com/luarvique/openwebrx), a web-based SDR (Software Defined Radio) receiver. On top of the original builder, it adds support for **TETRA** (Terrestrial Trunked Radio) — a digital trunked radio standard used by emergency services, public safety agencies, and utilities worldwide.

### Docker Images

| Image | Based on | Includes |
|-------|----------|----------|
| `slechev/openwebrxplus` | Debian Bookworm | OpenWebRX+ core |
| `slechev/openwebrxplus-softmbe` | above | + DMR/D-STAR/YSF/NXDN voice (AMBE) |
| `xnetinho/openwebrxplus-tetra` | above | + TETRA decoding (ACELP voice + signaling) |

### Quick Start

**Run the TETRA image:**
```bash
docker run --rm \
  --device /dev/bus/usb \
  -p 8073:8073 \
  -v ./config:/etc/openwebrx \
  -v ./data:/var/lib/openwebrx \
  xnetinho/openwebrxplus-tetra
```

Then open `http://localhost:8073` in your browser.

**Enable the TETRA frontend panel:**

Add the following line to your `htdocs/plugins/receiver/init.js`:
```javascript
await Plugins.load('https://xnetinho.github.io/openwebrxplus-docker-builder/plugins/receiver/tetra/tetra.js');
```

If you don't have an `init.js` yet, copy the sample:
```bash
cp /var/lib/openwebrx/htdocs/plugins/receiver/init.js.sample \
   /var/lib/openwebrx/htdocs/plugins/receiver/init.js
```

### TETRA Panel

The TETRA panel appears on the left side of the receiver when a TETRA signal is detected, styled similarly to the DMR panel. It shows:

- **Network info**: MCC/MNC, downlink/uplink frequencies, color code, encryption status
- **Signal quality**: AFC offset, burst rate
- **4 TDMA timeslots**: per-slot caller identity (ISSI), group (GSSI), call type (group/individual/emergency)

### Building the Images

```bash
# Full build chain (compiles OpenWebRX+ from source, then chains softmbe → tetra)
./run build

# Build only softmbe + tetra (uses pre-built images from Docker Hub — much faster)
./run build-softmbe

# Build only the tetra image (uses slechev/openwebrxplus-softmbe from Docker Hub)
./run build-tetra
```

### Requirements

- Docker with BuildX support
- Multi-platform builds target: `linux/amd64`, `linux/arm64`, `linux/arm/v7`
- Internet access during build (downloads ETSI ACELP codec and osmo-tetra)

### Technical Details

The TETRA decoding pipeline:

```
SDR IQ (36 kS/s) → GNU Radio π/4-DQPSK → osmo-tetra-sq5bpf → {
    Audio:    ETSI ACELP decoder (cdecoder) → 8 kHz PCM
    Metadata: TETMON UDP → JSON → WebSocket → frontend panel
}
```

Audio codec: [ETSI EN 300 395-2](http://www.etsi.org/deliver/etsi_en/300300_300399/30039502/01.03.01_60/en_30039502v010301p0.zip) (ACELP) — downloaded automatically during Docker build.

Protocol decoder: [osmo-tetra-sq5bpf](https://github.com/sq5bpf/osmo-tetra-sq5bpf)

### Credits

- [OpenWebRX+](https://github.com/luarvique/openwebrx) by LU7DID / luarvique
- [openwebrxplus-docker-builder](https://github.com/0xAF/openwebrxplus-docker-builder) by 0xAF (Stanislav Lechev)
- [OpenWebRX-Tetra-Plugin](https://github.com/mbbrzoza/OpenWebRX-Tetra-Plugin) by SP8MB (mbbrzoza) — original TETRA integration reference
- [openwebrxplus-plugins](https://github.com/0xAF/openwebrxplus-plugins) by 0xAF — plugin system and conventions

---

## Português

### O que é isso?

Este repositório constrói imagens Docker para o [OpenWebRX+](https://github.com/luarvique/openwebrx), um receptor SDR (Rádio Definido por Software) baseado em web. Além do builder original, adiciona suporte ao **TETRA** (Terrestrial Trunked Radio) — um padrão de rádio troncalizado digital utilizado por serviços de emergência, segurança pública e concessionárias de serviços ao redor do mundo.

### Imagens Docker

| Imagem | Baseada em | Inclui |
|--------|-----------|--------|
| `slechev/openwebrxplus` | Debian Bookworm | OpenWebRX+ principal |
| `slechev/openwebrxplus-softmbe` | acima | + voz DMR/D-STAR/YSF/NXDN (AMBE) |
| `xnetinho/openwebrxplus-tetra` | acima | + decodificação TETRA (voz ACELP + sinalização) |

### Início Rápido

**Executar a imagem TETRA:**
```bash
docker run --rm \
  --device /dev/bus/usb \
  -p 8073:8073 \
  -v ./config:/etc/openwebrx \
  -v ./data:/var/lib/openwebrx \
  xnetinho/openwebrxplus-tetra
```

Abra `http://localhost:8073` no navegador.

**Ativar o painel TETRA no frontend:**

Adicione a linha abaixo no seu `htdocs/plugins/receiver/init.js`:
```javascript
await Plugins.load('https://xnetinho.github.io/openwebrxplus-docker-builder/plugins/receiver/tetra/tetra.js');
```

Se ainda não tiver um `init.js`, copie o exemplo:
```bash
cp /var/lib/openwebrx/htdocs/plugins/receiver/init.js.sample \
   /var/lib/openwebrx/htdocs/plugins/receiver/init.js
```

### Painel TETRA

O painel TETRA aparece no lado esquerdo do receptor quando um sinal TETRA é detectado, com visual semelhante ao painel DMR. Exibe:

- **Informações de rede**: MCC/MNC, frequências downlink/uplink, color code, status de criptografia
- **Qualidade do sinal**: desvio AFC, taxa de bursts
- **4 slots TDMA**: por slot — identidade do chamador (ISSI), grupo (GSSI), tipo de chamada (grupo/individual/emergência)

### Construindo as Imagens

```bash
# Build completo (compila OpenWebRX+ do fonte, encadeia softmbe → tetra)
./run build

# Constrói apenas softmbe + tetra (usa imagens prontas do Docker Hub — muito mais rápido)
./run build-softmbe

# Constrói apenas a imagem tetra (usa slechev/openwebrxplus-softmbe do Docker Hub)
./run build-tetra
```

### Requisitos

- Docker com suporte a BuildX
- Builds multiplataforma: `linux/amd64`, `linux/arm64`, `linux/arm/v7`
- Acesso à internet durante o build (baixa o codec ETSI ACELP e o osmo-tetra)

### Detalhes Técnicos

Pipeline de decodificação TETRA:

```
IQ do SDR (36 kS/s) → GNU Radio π/4-DQPSK → osmo-tetra-sq5bpf → {
    Áudio:     decoder ETSI ACELP (cdecoder) → PCM 8 kHz
    Metadados: UDP TETMON → JSON → WebSocket → painel frontend
}
```

Codec de áudio: [ETSI EN 300 395-2](http://www.etsi.org/deliver/etsi_en/300300_300399/30039502/01.03.01_60/en_30039502v010301p0.zip) (ACELP) — baixado automaticamente durante o build Docker.

Decoder de protocolo: [osmo-tetra-sq5bpf](https://github.com/sq5bpf/osmo-tetra-sq5bpf)

### Créditos

- [OpenWebRX+](https://github.com/luarvique/openwebrx) por LU7DID / luarvique
- [openwebrxplus-docker-builder](https://github.com/0xAF/openwebrxplus-docker-builder) por 0xAF (Stanislav Lechev)
- [OpenWebRX-Tetra-Plugin](https://github.com/mbbrzoza/OpenWebRX-Tetra-Plugin) por SP8MB (mbbrzoza) — referência original da integração TETRA
- [openwebrxplus-plugins](https://github.com/0xAF/openwebrxplus-plugins) por 0xAF — sistema de plugins e convenções
