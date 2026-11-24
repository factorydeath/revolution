#!/usr/bin/env bash
set -euo pipefail

# Move to project root
cd "$(dirname "$(readlink -f "$0")")"

echo "=============================================="
echo "    Revolution Bot — 1-Command Environment Restore"
echo "=============================================="

# 1. Directory structure
echo "[1/5] Checking directory structure and symlinks..."
mkdir -p configs data docs logs pic backups screenshots

# Initialize .env if missing
if [ ! -f ".env" ]; then
    if [ -f "configs/.env" ] && [ ! -L "configs/.env" ]; then
        cp configs/.env .env
    elif [ -f ".env.example" ]; then
        echo "Creating .env from .env.example..."
        cp .env.example .env
    elif [ -f "configs/.env.example" ]; then
        echo "Creating .env from configs/.env.example..."
        cp configs/.env.example .env
    fi
fi

# Initialize proxies config if missing
if [ ! -f "configs/proxies.json" ] && [ -f "configs/proxies.json.example" ]; then
    cp configs/proxies.json.example configs/proxies.json
fi
if [ ! -f "configs/proxies.txt" ] && [ -f "configs/proxies.txt.example" ]; then
    cp configs/proxies.txt.example configs/proxies.txt
fi

# Ensure root symlinks
[ ! -e ".env.example" ] && [ -f "configs/.env.example" ] && ln -sf configs/.env.example .env.example || true
[ ! -e "configs/.env" ] && [ -f ".env" ] && ln -sf ../.env configs/.env || true
[ ! -e "services.json" ] && [ -f "configs/services.json" ] && ln -sf configs/services.json services.json || true
[ ! -e "proxies.json" ] && [ -f "configs/proxies.json" ] && ln -sf configs/proxies.json proxies.json || true
[ ! -e "proxies.txt" ] && [ -f "configs/proxies.txt" ] && ln -sf configs/proxies.txt proxies.txt || true
[ ! -e "users.db" ] && [ -f "data/users.db" ] && ln -sf data/users.db users.db || true
[ ! -e "SERVICES.md" ] && [ -f "docs/SERVICES.md" ] && ln -sf docs/SERVICES.md SERVICES.md || true
[ ! -e "service.md" ] && [ -f "docs/service.md" ] && ln -sf docs/service.md service.md || true

# 2. Virtual environment (.venv)
echo "[2/5] Setting up Python virtual environment..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    echo "Virtual environment created in .venv"
else
    echo "Existing .venv detected."
fi

# 3. Pip dependencies
echo "[3/5] Installing Python dependencies..."
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
if [ -f "requirements-dev.txt" ]; then
    .venv/bin/pip install -r requirements-dev.txt || true
fi

# 4. Patchright browsers
echo "[4/5] Installing Patchright browsers..."
.venv/bin/patchright install chromium ffmpeg || true

# 5. Database check
echo "[5/5] Checking SQLite database integrity..."
if [ -f "data/users.db" ]; then
    .venv/bin/python -c "import sqlite3; con = sqlite3.connect('data/users.db'); con.execute('PRAGMA wal_checkpoint(TRUNCATE);'); con.close()" 2>/dev/null || true
fi

echo "=============================================="
echo " Restore completed successfully!"
echo " Quick start:"
echo "   Run tests:      .venv/bin/pytest"
echo "   Restart bot:    ./restart.sh"
echo "=============================================="
