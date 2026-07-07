#!/bin/sh
docker exec -w /app -e PYTHONPATH=/app audiobook-request python /tmp/check_scraper.py
