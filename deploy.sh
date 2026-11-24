#!/bin/sh
# Run on the VPS inside the project folder: gets the latest code from GitHub and restarts.
# Your .env and data/ are never touched (they are not in git).
set -e
cd "$(dirname "$0")"
git pull --ff-only
docker compose up -d --build
docker compose ps
