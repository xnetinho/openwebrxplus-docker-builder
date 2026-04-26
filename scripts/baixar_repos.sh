#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------
# baixar_repos.sh
# Lista repositórios atualizados e permite clonar/puxar.
# Verifica status local (HEAD SHA via git ls-remote) antes
# de tentar atualizar — evita pulls desnecessários.
# HTTPS com token usando Authorization: Basic (x-access-token:TOKEN)
#
# Uso:
#   ./baixar_repos.sh tokens.txt [DIAS] [CLONE_DIR] [--https]
# ---------------------------------------------------------

TOKENS_FILE="${1:-}"
DAYS="${2:-7}"
CLONE_DIR="${3:-./clones}"
FORCE_HTTPS="${4:-}"

API_BASE="https://api.github.com"
UA_HEADER="User-Agent: gh-repos-check.sh"
ACCEPT_HEADER="Accept: application/vnd.github+json"

need() { command -v "$1" >/dev/null 2>&1 || { echo "Erro: '$1' não encontrado no PATH." >&2; exit 1; }; }
need curl; need jq; need git; need base64

if [[ -z "${TOKENS_FILE}" ]]; then
  echo "Uso: $0 /caminho/para/tokens.txt [DIAS] [CLONE_DIR] [--https]" >&2
  exit 1
fi
[[ -f "${TOKENS_FILE}" ]] || { echo "Erro: arquivo de tokens não existe: ${TOKENS_FILE}" >&2; exit 1; }
mkdir -p "${CLONE_DIR}"

cutoff_iso_utc() {
  local days_back="$1"
  if date -u -d "now" +"%Y-%m-%dT%H:%M:%SZ" >/dev/null 2>&1; then
    date -u -d "-${days_back} days" +"%Y-%m-%dT%H:%M:%SZ"
  elif date -u -v -0S +"%Y-%m-%dT%H:%M:%SZ" >/dev/null 2>&1; then
    date -u -v -"${days_back}"d +"%Y-%m-%dT%H:%M:%SZ"
  else
    echo "Erro: 'date' não suportado." >&2; exit 1
  fi
}
CUTOFF="$(cutoff_iso_utc "${DAYS}")"

readarray -t TOKENS < <(sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "${TOKENS_FILE}" \
  | grep -vE '^\s*$' | grep -vE '^\s*#')
(( ${#TOKENS[@]} > 0 )) || { echo "Erro: nenhum token válido em ${TOKENS_FILE}" >&2; exit 1; }

declare -A TOKEN_BY_ACCOUNT

api_get_all_pages() {
  local token="$1" url="$2" page=1 per_page=100 out="[]"
  while :; do
    local resp
    resp="$(curl -sS --fail -H "${UA_HEADER}" -H "${ACCEPT_HEADER}" \
      -H "Authorization: token ${token}" "${url}?per_page=${per_page}&page=${page}")"
    local count; count="$(jq 'length' <<<"${resp}")" || count=0
    (( count == 0 )) && break
    out="$(jq -c --slurp 'add' <(echo "${out}") <(echo "${resp}"))"
    (( count < per_page )) && break
    ((page++))
  done
  echo "${out}"
}

basic_auth_header() {
  local token="$1" raw="x-access-token:${token}" b64
  if base64 --wrap=0 </dev/null >/dev/null 2>&1; then
    b64="$(printf "%s" "${raw}" | base64 --wrap=0)"
  elif base64 -w0 </dev/null >/dev/null 2>&1; then
    b64="$(printf "%s" "${raw}" | base64 -w0)"
  else
    b64="$(printf "%s" "${raw}" | base64 | tr -d '\n')"
  fi
  printf "Authorization: Basic %s" "${b64}"
}

TMP_DIR="$(mktemp -d)"; trap 'rm -rf "${TMP_DIR}"' EXIT
ALL_REPOS_JSON="${TMP_DIR}/all_repos.json"; echo "[]" > "${ALL_REPOS_JSON}"

for token in "${TOKENS[@]}"; do
  USER_JSON="$(curl -sS --fail -H "${UA_HEADER}" -H "${ACCEPT_HEADER}" \
    -H "Authorization: token ${token}" "${API_BASE}/user")" || {
    echo "Aviso: token falhou em /user. Ignorando." >&2; continue; }
  LOGIN="$(jq -r '.login' <<< "${USER_JSON}")"
  [[ -n "${LOGIN}" && "${LOGIN}" != "null" ]] || {
    echo "Aviso: não obtive login. Ignorando token." >&2; continue; }
  TOKEN_BY_ACCOUNT["${LOGIN}"]="${token}"

  echo "Coletando repositórios da conta: ${LOGIN} ..."
  REPOS_JSON="$(api_get_all_pages "${token}" "${API_BASE}/user/repos")"
  REPOS_WITH_ACCOUNT="$(jq --arg acc "${LOGIN}" 'map(. + {account: $acc})' <<< "${REPOS_JSON}")"
  MERGED="$(jq -c --slurp 'add' <(cat "${ALL_REPOS_JSON}") <(echo "${REPOS_WITH_ACCOUNT}"))"
  echo "${MERGED}" > "${ALL_REPOS_JSON}"
done

FILTERED_JSON="${TMP_DIR}/filtered.json"
jq --arg cutoff "${CUTOFF}" '
  map(select(.updated_at >= $cutoff))
  | sort_by(.updated_at) | reverse
  | map({
      account, full_name, name, private, archived, disabled, fork,
      default_branch, updated_at, pushed_at, ssh_url, clone_url, html_url
    })
' "${ALL_REPOS_JSON}" > "${FILTERED_JSON}"

COUNT="$(jq 'length' "${FILTERED_JSON}")"
if (( COUNT == 0 )); then
  echo -e "\nNenhum repositório atualizado nos últimos ${DAYS} dia(s)."
  exit 0
fi

# Compara HEAD local com HEAD remoto via git ls-remote (sem baixar objetos).
# Saída: not_cloned | up_to_date | needs_update | unknown
check_local_status() {
  local dest="$1" branch="$2" token="$3" https_url="$4"

  [[ ! -d "${dest}/.git" ]] && { echo "not_cloned"; return; }

  local local_head
  local_head="$(git -C "${dest}" rev-parse HEAD 2>/dev/null)" || { echo "unknown"; return; }

  local auth_header=""
  [[ -n "${token}" ]] && auth_header="$(basic_auth_header "${token}")"

  local remote_head
  if [[ -n "${auth_header}" ]]; then
    remote_head="$(git -c credential.helper= -c "http.extraheader=${auth_header}" \
      ls-remote "${https_url}" "refs/heads/${branch}" 2>/dev/null | awk '{print $1}')"
  else
    remote_head="$(git -c credential.helper= \
      ls-remote "${https_url}" "refs/heads/${branch}" 2>/dev/null | awk '{print $1}')"
  fi

  [[ -z "${remote_head}" ]] && { echo "unknown"; return; }
  [[ "${local_head}" == "${remote_head}" ]] && { echo "up_to_date"; return; }
  echo "needs_update"
}

echo -e "\nVerificando status local dos ${COUNT} repositório(s)..."
declare -a REPO_STATUSES=()
for (( i=0; i<COUNT; i++ )); do
  ITEM="$(jq -r ".[$i]" "${FILTERED_JSON}")"
  ACCOUNT="$(jq -r '.account' <<< "${ITEM}")"
  NAME="$(jq -r '.name' <<< "${ITEM}")"
  BRANCH="$(jq -r '.default_branch' <<< "${ITEM}")"
  HTTPS_URL="$(jq -r '.clone_url' <<< "${ITEM}")"
  DEST="${CLONE_DIR}/${ACCOUNT}/${NAME}"
  TOKEN="${TOKEN_BY_ACCOUNT[${ACCOUNT}]:-}"

  STATUS="$(check_local_status "${DEST}" "${BRANCH}" "${TOKEN}" "${HTTPS_URL}")"
  REPO_STATUSES+=("${STATUS}")
  printf "  [%d/%d] %-40s  %s\n" "$((i+1))" "${COUNT}" "${ACCOUNT}/${NAME}" "${STATUS}"
done

CNT_NOVO=0; CNT_ATUAL=0; CNT_PENDENTE=0; CNT_UNKNOWN=0
for s in "${REPO_STATUSES[@]}"; do
  case "${s}" in
    not_cloned)   ((CNT_NOVO++))     ;;
    up_to_date)   ((CNT_ATUAL++))    ;;
    needs_update) ((CNT_PENDENTE++)) ;;
    *)            ((CNT_UNKNOWN++))  ;;
  esac
done

echo -e "\nEncontrados ${COUNT} repositório(s) atualizados desde ${CUTOFF}:\n"
printf "%-4s  %-11s  %-20s  %-45s  %-7s\n" "IDX" "STATUS" "UPDATED_AT" "REPO" "PRIVATE"
printf '%0.s-' {1..93}; echo

for (( i=0; i<COUNT; i++ )); do
  ITEM="$(jq -r ".[$i]" "${FILTERED_JSON}")"
  ACCOUNT="$(jq -r '.account' <<< "${ITEM}")"
  NAME="$(jq -r '.name' <<< "${ITEM}")"
  UPDATED="$(jq -r '.updated_at' <<< "${ITEM}")"
  PRIVATE="$(jq -r '.private' <<< "${ITEM}")"

  case "${REPO_STATUSES[$i]}" in
    not_cloned)   LABEL="[NOVO]    " ;;
    up_to_date)   LABEL="[ATUAL]   " ;;
    needs_update) LABEL="[PENDENTE]" ;;
    *)            LABEL="[?]       " ;;
  esac

  printf "%-4s  %-11s  %-20s  %-45s  %-7s\n" \
    "${i}" "${LABEL}" "${UPDATED:0:19}" "${ACCOUNT}/${NAME}" "${PRIVATE}"
done

echo ""
printf "Resumo: %d para clonar  |  %d com atualizações pendentes  |  %d já atualizados" \
  "${CNT_NOVO}" "${CNT_PENDENTE}" "${CNT_ATUAL}"
(( CNT_UNKNOWN > 0 )) && printf "  |  %d desconhecido(s)" "${CNT_UNKNOWN}"
echo ""

echo -e "\nOpções de seleção:"
echo "  Índices   →  ex.: 0 3 5   (clonar/atualizar os selecionados)"
echo "  all       →  todos"
echo "  new       →  apenas [NOVO]     (${CNT_NOVO})"
echo "  pending   →  apenas [PENDENTE] (${CNT_PENDENTE})"
echo "  needed    →  [NOVO] + [PENDENTE] ($(( CNT_NOVO + CNT_PENDENTE )))"
echo "  q         →  sair"
echo ""
read -rp "Sua escolha: " CHOICE
[[ "${CHOICE,,}" == "q" ]] && { echo "Saindo."; exit 0; }

SELECTED_IDX=()
case "${CHOICE,,}" in
  all)
    mapfile -t SELECTED_IDX < <(seq 0 $((COUNT-1)))
    ;;
  new)
    for (( i=0; i<COUNT; i++ )); do
      [[ "${REPO_STATUSES[$i]}" == "not_cloned" ]] && SELECTED_IDX+=("$i")
    done
    ;;
  pending)
    for (( i=0; i<COUNT; i++ )); do
      [[ "${REPO_STATUSES[$i]}" == "needs_update" ]] && SELECTED_IDX+=("$i")
    done
    ;;
  needed)
    for (( i=0; i<COUNT; i++ )); do
      [[ "${REPO_STATUSES[$i]}" == "not_cloned" || "${REPO_STATUSES[$i]}" == "needs_update" ]] \
        && SELECTED_IDX+=("$i")
    done
    ;;
  *)
    for idx in ${CHOICE}; do
      if [[ "${idx}" =~ ^[0-9]+$ ]] && (( idx >= 0 && idx < COUNT )); then
        SELECTED_IDX+=("${idx}")
      else
        echo "Aviso: índice inválido ignorado: ${idx}" >&2
      fi
    done
    ;;
esac

(( ${#SELECTED_IDX[@]} > 0 )) || { echo "Nada a fazer."; exit 0; }

echo -e "\nProcessando em: ${CLONE_DIR}\n"

FORCE_HTTPS_FLAG="0"
[[ "${FORCE_HTTPS}" == "--https" ]] && FORCE_HTTPS_FLAG="1"

git_clone_or_update() {
  local account="$1" name="$2" ssh_url="$3" https_url="$4" dest="$5" \
        force_https="$6" pre_status="$7"
  local token="${TOKEN_BY_ACCOUNT[${account}]:-}"
  local auth_header=""
  [[ -n "${token}" ]] && auth_header="$(basic_auth_header "${token}")"

  mkdir -p "$(dirname "${dest}")"

  if [[ -d "${dest}/.git" ]]; then
    if [[ "${pre_status}" == "up_to_date" ]]; then
      echo "  Já está atualizado — nada a fazer."
      return 0
    fi
    echo "  Atualizando..."
    if [[ -n "${auth_header}" ]]; then
      git -C "${dest}" -c credential.helper= -c http.extraheader="${auth_header}" pull --ff-only || {
        echo "  Falha no pull de ${account}/${name}." >&2; return 1; }
    else
      git -C "${dest}" -c credential.helper= pull --ff-only || {
        echo "  Falha no pull de ${account}/${name}." >&2; return 1; }
    fi
    return 0
  fi

  echo "  Clonando..."

  if [[ "${force_https}" == "1" ]]; then
    if [[ -n "${auth_header}" ]]; then
      git -c credential.helper= -c http.extraheader="${auth_header}" \
        clone --depth=1 "${https_url}" "${dest}" && return 0
    fi
    git -c credential.helper= clone --depth=1 "${https_url}" "${dest}" && return 0
    echo "  Falha no clone HTTPS." >&2; return 1
  fi

  git -c credential.helper= clone --depth=1 "${ssh_url}" "${dest}" 2>/dev/null && return 0

  if [[ -n "${auth_header}" ]]; then
    git -c credential.helper= -c http.extraheader="${auth_header}" \
      clone --depth=1 "${https_url}" "${dest}" && return 0
  fi

  git -c credential.helper= clone --depth=1 "${https_url}" "${dest}" && return 0

  echo "  Falha no clone. Verifique acesso (token/perm) ou SSH." >&2
  return 1
}

for idx in "${SELECTED_IDX[@]}"; do
  ITEM="$(jq -r ".[$idx]" "${FILTERED_JSON}")"
  ACCOUNT="$(jq -r '.account' <<< "${ITEM}")"
  NAME="$(jq -r '.name' <<< "${ITEM}")"
  SSH_URL="$(jq -r '.ssh_url' <<< "${ITEM}")"
  HTTPS_URL="$(jq -r '.clone_url' <<< "${ITEM}")"
  DEST="${CLONE_DIR}/${ACCOUNT}/${NAME}"
  PRE_STATUS="${REPO_STATUSES[$idx]}"

  echo "[${idx}] ${ACCOUNT}/${NAME}  (${PRE_STATUS})"
  if ! git_clone_or_update "${ACCOUNT}" "${NAME}" "${SSH_URL}" "${HTTPS_URL}" \
       "${DEST}" "${FORCE_HTTPS_FLAG}" "${PRE_STATUS}"; then
    echo "  >>> Dica: para forçar HTTPS com token, rode com: --https"
  fi
done

echo -e "\nConcluído."
