#!/bin/sh
# LINUX_START_HERE: installs the wizard's own prerequisites (python3, curl)
# via apt, then hands off to start.py. POSIX sh on purpose: this runs before
# anything else exists, so it can't assume bash.
cd "$(dirname "$0")/project_files" || exit 1

need=""
command -v python3 >/dev/null || need=" python3"
command -v curl    >/dev/null || need="$need curl"

if [ -n "$need" ]; then
    if command -v apt >/dev/null || command -v apt-get >/dev/null; then
        apt="apt"; command -v apt >/dev/null || apt="apt-get"
        echo "installing$need (asks for your password)"
        sudo "$apt" install -y $need || exit 1
    else
        echo "install$need with your package manager, then rerun: sh LINUX_START_HERE.sh"
        exit 1
    fi
fi

exec python3 start.py
