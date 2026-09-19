# MetaMusic (Telegram FLAC tagger)

Multi-user, multi-group/channel bot. Fingerprints FLACs, writes a tight Vorbis-comment allowlist, optional per-user Google Drive, HiFiAudioBot name search.

Audio is never re-encoded. FLAC uses Vorbis comments (not ID3).

Friends-only by obscurity + admin blacklist. The bot username is **not** a security boundary.

## What you need

1. Bot from [@BotFather](https://t.me/BotFather)
   - `/setprivacy` → **Disable**
   - Add to groups/channels. Grant Photos and Files. Keep it **admin** so reaction updates arrive.
   - Enable group reactions: 👍 👎 💩 🙉 🙏 ✍️
   - Mini App: BotFather domain = `PUBLIC_BASE_URL` host (`music.avje.in`). Keep `/app` and `/api` on that origin (Telegram same-origin). Public policies: `www.music.avje.in`.
2. `api_id` / `api_hash` from [my.telegram.org](https://my.telegram.org) (local Bot API + Telethon HiFi session)
3. Free [AcoustID](https://acoustid.org/new-application) key
4. MusicBrainz user-agent with a real contact email
5. Optional [Last.fm](https://www.last.fm/api/account/create) key for `/suggest`
6. Google Cloud **Web application** OAuth client (not Desktop)
   - Enable Drive API
   - Scopes: `drive.file`, `drive.install`, `userinfo.email`, `openid`
   - Redirect URI: `${PUBLIC_BASE_URL}/oauth/callback`
   - Consent **Testing** + friends as test users (or publish later)
   - `GOOGLE_CLIENT_SECRET` stays on the bot server only
7. Reverse proxy TLS in front of `127.0.0.1:8080` (Mini App + `/api` + OAuth) and `127.0.0.1:8082` (www). Samples: `samples/music.nginx.conf`, `samples/www.music.nginx.conf`. Do not put raw HTTP on the WAN. Do not put APIs on a second host.

Set `ADMIN_TELEGRAM_USER_ID` to your Telegram user id.

## Configure

```bash
cp .env.example .env
```

`DM_REQUIRES_KNOWN_CHAT=true` (default): DMs only work if the user is in a known, non-blacklisted group/channel (or is the env admin).

`PUBLIC_BASE_URL` must be `https://…` (or `http://localhost` for dev). OAuth/Mini App links are built from this value only; `Host` is ignored. Telegram Mini App **buttons** need HTTPS; localhost falls back to a normal link (works on this machine’s Telegram Desktop, not on a phone).

## Run

```bash
docker compose up -d --build
docker compose logs -f bot
```

HiFi user session (full Telegram account — use a dummy if you can):

```bash
docker compose run --rm -it bot python -m app.hifi_login
```

FTPS is **off**. Enable only if you accept a shared FLAC dump:

```bash
docker compose --profile ftp up -d
```

That profile mounts `/data/cache` only. Never sqlite, refresh tokens, or `hifi.session`.

## Commands

Admin (env admin only, private/ephemeral lists): `/listusers` `/listgroups` `/listchannels` `/blockuser` `/unblockuser`

Everyone allowed: `/start` `/login` `/settings` `/review` `/suggest`  
Search: **off** until a user Telethon session can talk to HiFiAudioBot (`SEARCH_ENABLED` in `app/user_cmd.py`). Send a FLAC instead. `/get`, DM song names, and `@bot` search reply with that.

Mini App for login/settings/review/suggest. DM fallback if Mini App fails. Never paste an OAuth URL in a public group.

## Reactions (on the public audio+caption)

- 👍 / 👎 ask first: copy **current** Telegram file into **your** library or review, or **move** a Drive copy you already have (library ↔ review). Same pile → skip.
- 💩 delete from **your** library/review only (group message stays)
- 🙏 re-identify and replace the **group/channel audio**. Drive copies stay until someone 👍 or 👎.
- ✍️ tag editor. On commit the **group audio is replaced** for everyone; Drive is untouched. Later 👍 copies that new file.

Confirm UI is private (ephemeral or DM).

## Retention

Daily 03:00 UTC (`CLEANUP_CRON`): Drive retry for logged-in users. Drop a local Drive-confirmed file, logged-out local, unused `/data/cache` entry, or tmp dir only after **1 week for that item**. Forget users idle longer than `USER_INACTIVE_MONTHS` (default **3 months**; tokens first). Skip `processing`/`uploading`. Drive copies you already saved stay in your Drive.

## Threats

- **Open username:** anyone who finds the bot can abuse HiFi/disk/quota until you blacklist. Keep `DM_REQUIRES_KNOWN_CHAT=true`.
- **Poisoned group file:** any member who ✍️ can replace the canonical audio; later 👍 copies it into others’ Drive. Caption stores `editor:<id>`. Accepted risk.
- **Telethon `hifi.session`:** theft is full takeover of that Telegram account. Dedicated dummy + 2FA. Mode `0600`. Not on FTP.
- **OAuth Testing:** refresh tokens die ~7 days. Failures say “use /login again”, never dump tokens.
- **No FTP of sqlite/tokens/session.** Shared cache FTP is everyone’s FLACs.
- **HTTPS for OAuth.** Compose binds `127.0.0.1:8080`. Wrong `PUBLIC_BASE_URL` or WAN HTTP can leak auth codes.
- Mini App APIs trust `initData` HMAC only, scoped to that user. HiFi keyboards use opaque `hf:` ids, never raw HiFi `callback_data`.

## Error handling

Failures map to short user strings (`needs_login`, `timeout`, `busy`, …). Telegram/HTML never get stack traces, tokens, or Google bodies. Asker gets ephemeral/DM; infra (HiFi session dead, disk full, HTTP bind fail) goes to the env admin DM. Worker/dispatcher/HTTP keep running after one job dies.

## Manual checklist (not CI)

Google Web login on HTTPS, Mini App on phone, ephemeral in a real group vs old Bot API DM fallback, Telethon login, one HiFi search, two-user 👍 copy, blacklist, unlink, 7-day purge dry-run.

## Tags written

`TITLE`, `ALBUM`, `ARTIST`, `ALBUMARTIST`, `COMPOSER`, `GENRE`, `DATE`, `TRACKNUMBER`, `DISCNUMBER`, `LYRICS` (synced LRC only), one front cover. Everything else is stripped.

Artist fields use `A, B, C & D`. Genre is `genre | mood | language | instrument`. Language tags come from Last.fm / MusicBrainz (not group topics). If more than one language is found, all stay on genre and the user picks the Drive/library folder.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -t .
.venv/bin/ruff check .
```

No live Google, HiFi, or Telegram in CI.

## Troubleshooting

**Private chat says membership required.** Join a group that already has the bot, or set `DM_REQUIRES_KNOWN_CHAT=false` (open abuse).

**Login link expired.** `/login` tickets are one-time and short-lived.

**`invalid_grant` / needs_login.** Testing-mode token died. `/login` again.

**Ephemeral missing.** Local Bot API older than 10.3; UI falls back to DM. User must `/start` the bot first.

**Cover picker shows no images.** Bot lacks photo permission in that group.

**Search is off.** `/get` and song-name DMs are gated by `SEARCH_ENABLED` in `app/user_cmd.py` until HiFi has a user Telethon session. Send a FLAC.

**Search says Service unavailable.** No HiFi session at `/data/hifi.session`. Run `docker compose run --rm -it bot python -m app.hifi_login` with a **user phone**, not `BOT_TOKEN`.

**`@bot` opens a search panel.** Search results are empty while search is off. To mention as a normal group message: BotFather → `/setinline` → Disable.

## License

see [LICENSE](LICENSE).
