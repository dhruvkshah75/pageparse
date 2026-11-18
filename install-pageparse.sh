#!/bin/sh
# Install the PageParse client into a fresh Linux checkout.
set -eu

usage() {
    printf '%s\n' 'Usage: sh install-pageparse.sh [DIRECTORY]' \
        'Clones PageParse into DIRECTORY (default: ./pageparse) and installs its client in .venv.' \
        'Requires Linux, Git, and Python 3.12 with venv support.' \
        'Set PAGEPARSE_REPO_URL to install from a fork or another Git remote.'
}

case ${1:-} in
    -h|--help) usage; exit 0 ;;
    -*) usage >&2; exit 2 ;;
esac

if [ "$#" -gt 1 ]; then
    usage >&2
    exit 2
fi

if [ "$(uname -s)" != Linux ]; then
    printf '%s\n' 'PageParse requires Linux.' >&2
    exit 1
fi

for command in git python3.12; do
    if ! command -v "$command" >/dev/null 2>&1; then
        printf 'Install %s before running the installer.\n' "$command" >&2
        exit 1
    fi
done

destination=${1:-./pageparse}
case $destination in
    /*) ;;
    *) destination=$PWD/$destination ;;
esac

if [ -e "$destination" ] || [ -L "$destination" ]; then
    printf 'Destination already exists: %s\nChoose a new directory or follow the developer setup in the existing checkout.\n' "$destination" >&2
    exit 1
fi

repository=${PAGEPARSE_REPO_URL:-https://github.com/dhruvkshah75/pageparse.git}
git clone --depth 1 -- "$repository" "$destination"

if ! python3.12 -m venv "$destination/.venv"; then
    printf '%s\n' 'Could not create the Python environment. On Ubuntu, install python3.12-venv and follow the developer setup in the new checkout.' >&2
    exit 1
fi

"$destination/.venv/bin/python" -m pip install "$destination"
"$destination/run-pageparse" --help >/dev/null

printf '\nInstalled PageParse in %s\n' "$destination"
printf '%s\n' 'From that directory, run ./run-pageparse for Nemotron or ./run-pageparse --chat for Ollama.' \
    'See the checkout README for model setup.'
