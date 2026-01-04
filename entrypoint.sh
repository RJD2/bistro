#!/bin/sh
set -e

echo "Running migrations..."
python -m migrations.init_db
python -m migrations.add_questions

echo "Starting bot..."
python bot.py
