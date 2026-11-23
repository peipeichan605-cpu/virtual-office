#!/bin/sh
cd -P "$(dirname "$0")" || exit 1
office_python=${OFFICE_PHONE_PYTHON:-python3}
if ! command -v "$office_python" >/dev/null 2>&1; then
    echo 'Open this folder with the Python installation used by Virtual Office.'
    exit 1
fi
"$office_python" phone_setup_assistant.py
result=$?
printf '\nPress Return to close this window. '
read -r answer
exit "$result"
