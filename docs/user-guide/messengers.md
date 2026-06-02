# Messengers

Hermes can be reached from outside the Web-UI via two messenger
channels.

## Signal

Signal is connected as a **primary device** (not linked) because
linked secondary devices in signal-cli don't receive Note-to-Self sync
messages. The operator pairs Signal during installation. Once paired,
sending a message to the configured Signal number reaches Hermes.

## Telegram

A Telegram bot exposes a single chat per configured user. Messages
sent to the bot are routed through the `telegram` channel.

## Per-channel tone

Each messenger has its own prompt overlay (see Channels in
`/settings/preferences`). Defaults are tuned for a chat-app context:
1–3 short sentences, plain text, no Markdown or code fences.

## See also

- `personas` — change the voice that responds across all channels.
