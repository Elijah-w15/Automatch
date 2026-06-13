#!/bin/sh
# Linux launcher: installs the wizard prerequisites (python3, curl), then
# hands off to start.py inside project_files/. POSIX sh: this runs before
# anything else exists, so it cannot assume bash.
cd "$(dirname "$0")/project_files" || exit 1

need=""
command -v python3 >/dev/null || need=" python3"
command -v curl    >/dev/null || need="$need curl"

if [ -n "$need" ]; then
    if command -v apt >/dev/null || command -v apt-get >/dev/null; then
        apt="apt"; command -v apt >/dev/null || apt="apt-get"
        echo "installing$need (asks for your password)"
        sudo "$apt" update
        sudo "$apt" install -y $need || exit 1
    else
        echo "install$need with your package manager, then run this file again"
        exit 1
    fi
fi

# AUTOMATCH_INSTALLER=1 marks this as the installer: once setup is complete,
# start.py informs instead of scraping, so re-running this file never kicks off
# a scrape. A direct `python3 start.py` (no flag) still scrapes as normal.
exec env AUTOMATCH_INSTALLER=1 python3 start.py
