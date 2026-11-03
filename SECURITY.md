# Security Policy

This project is designed to hold personal and professional data, so security
reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Instead, use
GitHub's private reporting: the repository's **Security** tab →
**Report a vulnerability**. Include what you found, how to reproduce it, and
the impact you think it has.

I'll acknowledge reports as soon as I can. This is a personal project
maintained by one person, so there is no formal SLA.

## Scope

In scope: anything in this repository, especially

- bypassing the Telegram user allowlist or private-chat restriction
- leaking secrets (bot token, API keys) into logs, messages or files
- prompt injection via fetched web/RSS content that causes the agents to act
  on embedded instructions
- SSRF through URL fetching (reaching internal or private addresses)
- any path where the system takes an external action (sending a message,
  email or application) without explicit owner approval

Out of scope: vulnerabilities in Telegram, Gemini or other third-party
services themselves, and issues that require an attacker to already control
the host machine or its `.env` file.

## If you run your own copy

- Keep `.env` out of version control (it is git-ignored by default).
- Set `TELEGRAM_ALLOWED_USER_IDS` to your own numeric ID only.
- Back up `data/` regularly and store backups encrypted, off the machine.
