#!/bin/sh
cd "$(dirname "$0")"
python3 -m adaptivedns --demo --open "$@"
