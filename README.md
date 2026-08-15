# Telegram FLAC tagger

Watches a Telegram forum group, fingerprints FLACs, writes a tight Vorbis-comment allowlist, uploads to Google Drive, and wipes local copies weekly.

Audio is never re-encoded. FLAC uses Vorbis comments (not ID3).

## What you need

1. Bot from [@BotFather](https://t.me/BotFather)
   - `/setprivacy` → **Disable** (otherwise the bot only sees commands)
   - Add the bot to the forum group
2. `api_id` and `api_hash` from [my.telegram.org](https://my.telegram.org) (local Bot API, required for files over 20MB)
3. Free [AcoustID application key](https://acoustid.org/new-application)
4. MusicBrainz user-agent with a real contact email
5. Optional free [Last.fm API key](https://www.last.fm/api/account/create)
6. Google Cloud project (personal Drive)
   - Enable **Google Drive API**
   - OAuth consent: **External**, test user = your Gmail
   - Scopes to add: `drive.file` and `userinfo.email` (not full `drive` — that needs Google verification)
   - Credentials → OAuth client ID → **Desktop app**
   - Put `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` in `.env`
   - Run `python -m app.drive_auth` (or `--manual`)
   - Paste printed `GOOGLE_REFRESH_TOKEN`, `GDRIVE_FOLDER_ID`, `GDRIVE_REVIEW_FOLDER_ID`
   - Script creates **Telegram Music** and **Telegram Music Review**. Move them into your Music folder in Drive if you want; bot keeps access
   - Old folder IDs from before this app **will not work** (`drive.file` cannot see them)
   - Do **not** use a service account for personal My Drive

## Configure

```bash
cp .env.example .env
```

Paste secrets into `.env`. Service-account JSON is optional (Shared Drive only).

Get `ALLOWED_CHAT_ID` by adding the bot and sending `/chatid` in the group. Forum General topic is usually `ALERT_THREAD_ID=1`. OAuth apps in **Testing** can expire the refresh token after ~7 days — re-run `python -m app.drive_auth` if uploads start failing with invalid_grant.

## Run

```bash
docker compose up -d --build
docker compose logs -f bot
```

Local Bot API can take a minute to start; the bot retries.

Data:

- `/data/library` and `/data/review` — staging until weekly cleanup
- `/data/state.sqlite` — catalog (survives wipes; used for dedup)
- Drive music folder — library layout `{Topic}/{AlbumArtist}/{Year} - {Album}/{Track} - {Title}.flac`
- Drive review folder — low-confidence matches plus a `.json` sidecar

Sunday 03:00 UTC (`CLEANUP_CRON`): retry failed uploads, alert General topic if still failing, delete locals that are confirmed on Drive.

## Tags written

`TITLE`, `ALBUM`, `ARTIST`, `ALBUMARTIST`, `COMPOSER`, `GENRE`, `DATE`, `TRACKNUMBER`, `DISCNUMBER`, `LYRICS` (synced LRC only), one front cover. Everything else is stripped.

Artist fields use `A, B, C & D`. Genre is `genre | mood | language | instrument`.

## Identification

1. Chromaprint / AcoustID (audio fingerprint)
2. MusicBrainz for canonical metadata
3. Cover Art Archive, then iTunes art
4. LRCLIB for synced lyrics
5. iTunes + Last.fm tags filtered through `genre_map.yaml`

High AcoustID score and a single recording → library. Anything else → review folder.
