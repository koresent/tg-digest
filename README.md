# TG Digest

A stateful, automated Telegram message forwarder. `tg-digest` collects messages from specified channels or Telegram folders and forwards them to a private channel or Saved Messages. 

It is designed to run autonomously in a Docker container using `supercronic`, pulling only new messages, preserving media albums, and strictly respecting Telegram's API rate limits.

## Deployment

Telegram requires SMS/OTP verification when logging in from a new device/script for the first time. You **must** run the script interactively once to generate the `.session` file.

```bash
docker compose run --rm -it tg-digest python main.py
```
