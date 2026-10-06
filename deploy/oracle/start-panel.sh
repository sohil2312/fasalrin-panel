#!/usr/bin/env bash
# Start the Fasalrin control panel inside the Remote Desktop session (the scripts' Chrome opens on this desktop).
cd "$(dirname "$0")/../.."
exec .venv/bin/python portal.py
