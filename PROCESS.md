# Process flows

How MetaMusic handles Telegram updates, Mini App HTTP, and Google Drive. Source of truth is the code; this maps the paths a user actually hits.

Private UI (commands, confirms, tag editor, dest/language prompts) is **ephemeral in groups/channels**, else a DM. If ephemeral fails, the bot asks the user to `/start` in private first.

## Entry

```text
Telegram update
  ├─ ignore if it came from this bot (media echo / bot-to-bot)
  ├─ membership / blacklist gate (allow_user)
  └─ first matching router, in this order:
       admin commands → user commands → HiFi picks → private DM
       → /review → /suggest → tag editor → reactions
       → search text / inline → group-channel audio → pending-review buttons
```

HTTP (`PUBLIC_BASE_URL`, port 8080): Mini App `/app/*`, JSON `/api/*` (Telegram `initData` HMAC), Google OAuth `/oauth/start` + `/oauth/callback`.

A background **tagger worker** consumes `Job`s. Identify / tag / cover / Drive never run on the Telegram handler thread.

---

## Shared gates

### Who may talk to the bot

| Context | Allowed if |
| --- | --- |
| Group / channel (`chat_id < 0`) | User not blacklisted; chat not blacklisted. Unknown chats are registered when the bot sees them. |
| Private DM | Env admin **or** member of a known, non-blacklisted group/channel (`DM_REQUIRES_KNOWN_CHAT=true`, default). Deny text: `Private access requires membership in a known group.` |
| Mini App `/api` | Valid `initData`, not blacklisted, API rate limit. |

Env admin (`ADMIN_TELEGRAM_USER_ID`) bypasses blacklist and DM membership.

Bots, including this bot, are ignored on messages / reactions / callbacks (except `my_chat_member`).

### Formats

Default listen-for: **FLAC + MP3**. User can add M4A, OGG, Opus, WAV in Mini App settings. Unknown audio is ignored. Allowed-but-off formats are ignored (no error). Audio is never re-encoded.

### Rate limits

Upload (audio + HiFi pick), search, Mini App API — per-user sliding windows. Over limit → short user error, no stack traces.

---

## 1. Sending text

Plain text is **not** tagged. It is search, an in-progress editor, or ignored.

```text
text message
  ├─ starts with /  → command routers (section 2)
  ├─ private chat
  │    ├─ not a member of a known chat → membership error
  │    ├─ waiting cover editor → treat as image URL, or “send a photo…”
  │    ├─ waiting field editor → write that tag field
  │    └─ else → consumed; no search (private text handler runs first)
  ├─ group / channel
  │    ├─ @mention this bot → song search (currently off)
  │    ├─ forum topic created/edited → remember topic name
  │    └─ else → ignore
  └─ inline @bot query → search results (currently empty; search is off)
```

### Private, not in an editor

The private-text handler matches all non-command DMs, so **typing a song name in DM does not start search**. Use `/get` (also off today) or send audio.

### Group @mention / `/get` / inline (search)

`SEARCH_ENABLED` in `app/user_cmd.py` is **False**. Every search path replies:

> Song search is temporarily off. Send audio instead.

When that flag is flipped True:

1. Rate-limit search.
2. Telethon user session talks to HiFiAudioBot.
3. Private result keyboard (`hf:` opaque ids, never raw HiFi callbacks).
4. Pick downloads audio → same ingest as sending a file (`ingest_local_audio`), still filtered by listen-for formats.
5. No HiFi session → `Service unavailable`; env admin is DMed once.

Inline with an empty query offers “Search in private chat”.

---

## 2. `/commands`

All of these (except admin) require `allow_user`. Replies are private (ephemeral or DM). Mini App buttons need `https://` `PUBLIC_BASE_URL`; localhost falls back to a normal URL.

| Command | Who | What happens |
| --- | --- | --- |
| `/start` | anyone allowed | Stats: first seen, songs edited, Drive email or `not connected`, idle months. Lists commands. Does **not** start Google login. |
| `/login` | anyone allowed | One-time ticket URL (`oauth_ticket_ttl_seconds`, default 10 min). Button: Mini App `/app/login` or the ticket. **Never paste the OAuth URL in a public group.** |
| `/settings` | anyone allowed | Drive email + idle policy. Button: Mini App `/app/settings`. Formats / unlink / save prefs live there, not as Telegram commands. |
| `/review` `/reviews` | anyone allowed | Mini App review button, plus Telegram list of **this user’s** review tracks (syncs Drive review folder if logged in). Pick a row → song card; react on that card. |
| `/suggest` `[query]` | anyone allowed | Mini App suggest button, plus Last.fm similar tracks seeded from **this user’s** library (Drive index if sqlite is empty). Needs `LASTFM_API_KEY`. Empty library → `Library is empty. Upload audio first.` |
| `/get <song>` | anyone allowed | Search (currently the “search is off” text). |
| `/chatid` | anyone who can message | `chat_id` + `thread_id` (debug). |
| `/listusers` `/listgroups` `/listchannels` `/blockuser` `/unblockuser` | env admin only | Private lists + block/unblock. Silent no-op for everyone else. |

Unknown `/foo` is ignored (no help dump).

### Mini App pages (same commands, HTTPS)

| Page | API | Notes |
| --- | --- | --- |
| `/app` home | `GET /api/me` | Drive on/off, library/review counts, tiles. |
| `/app/login` | `GET /api/login` → ticket | Opens Google in the browser; polls `/api/me` until `logged_in`. Already logged in → redirects to settings. |
| `/app/settings` | `POST /api/settings` | See [Settings](#5-settings). |
| `/app/review` | `GET /api/review`, `POST /api/review/{id}/action` | `library` / `tags` relocate; `cancel` deletes **your** copy. |
| `/app/suggest` | `GET /api/suggest`, `/api/suggest/art` | Same Last.fm path as `/suggest`. |

---

## 3. Audio files

Group, channel post, and private DM all funnel into `ingest_local_audio` after download.

```text
audio or document
  ├─ not a known audio type → ignore
  ├─ from this bot / anonymous → ignore
  ├─ allow_user fail → ignore (DM: membership error)
  ├─ format not in user’s listen-for list → ignore
  ├─ private + another pending action in that DM → “Finish or cancel current action first.”
  ├─ upload rate exceeded → user error
  ├─ SHA already in this user’s library/review
  │     → send public audio again, caption “already in library/review”; stop
  └─ else
        send public playable audio, caption “identifying…”
        insert pending phase=intake, status=queued
        enqueue Job → worker
```

Group/channel keeps the user’s original message. The bot’s **new** audio+caption is the public card later reactions bind to.

### Worker (`process_job`)

```text
Job
  copy/download into tmp (no re-encode)
  read embedded tags (hints)
  AcoustID fingerprint → MusicBrainz identity
  enrich: Last.fm genre/mood, LRCLIB synced lyrics, Cover Art Archive / iTunes later
  optional authenticity sample on FLAC
  │
  ├─ confidence low
  │     group: tag-diff keyboard (file tags vs recommended)
  │     private: field-by-field editor
  │
  ├─ confidence high + Drive dest + same MBID already in this user’s library
  │     new quality ≤ old → skip, caption “Duplicate — already in library”
  │     new quality > old → replace that copy (cover → dest → upload)
  │
  └─ else commit path:
        1. language prompt if ≥2 languages on genre (folder name only; tags keep all)
        2. cover picker if album is shareable and >1 candidate
             (same-album followers wait on the leader pick)
        3. dest prompt (Drive) — skipped when not logged in, or “do not ask again”
        4. Drive name conflict → Replace / Keep both / Skip
        5. write tags locally; upload if dest is library/review
        6. update public caption; optionally replace Telegram media
        7. optionally delete the user’s original message
```

Pending review rows expire ~24h (`expire_pending`). Interrupted `queued`/`processing` jobs are re-queued on startup.

### After a successful save

Caption becomes `Saved (library|review)` or `Tagged (not copied to Drive)` / `Updated the group file`, plus tags, optional Drive link, `editor:<id>` if someone later ✍️-commits.

---

## 4. Reactions

Only on a **public audio+caption card** the catalog (or caption/Drive/index probe) can resolve. Bot reactions ignored. User must pass `allow_user`. Confirm UI is private.

```text
message_reaction
  resolve track (sqlite by chat+message, else caption / Drive id / library index)
  ├─ 👍  → plan copy/move to YOUR library
  ├─ 👎  → plan copy/move to YOUR review
  ├─ 💩 or 🙉 → plan delete YOUR copy only (group message stays)
  ├─ 🙏  → plan re-identify the group/channel file (Drive copies untouched)
  └─ ✍️  added → private tag editor on a staged copy
      ✍️  removed → commit/cancel prompt
```

👍 / 👎 first look at **your** existing copy of that recording (SHA, then MBID, then same `user_id`):

| Your copy | 👍 | 👎 |
| --- | --- | --- |
| none | confirm: copy Telegram file → your library | confirm: copy → your review |
| already library, uploaded | “Already in your library.” | confirm: **move** that Drive copy library → review |
| already review, uploaded | confirm: **move** review → library | “Already in your review folder.” |

Copy uses the **current Telegram file** when possible, else local/Drive. Confirm Yes runs `copy_track_for_user` / `relocate_track` / `delete_track`. No cancels. Drive copies of other users are never touched.

🙏 sets dest to Telegram-only (`drive_dest=none`) and `correct_telegram=true`, then re-runs the identify worker against a staged original (Telegram → local → Drive). Group/channel media is replaced after identify. In a DM, a listen copy is sent first.

✍️ commit **replaces the group/channel audio for everyone** and stamps `last_editor_user_id`. Drive files stay until someone 👍/👎 copies the new file. Cancel / 24h timeout discards the staged copy.

If ✍️ is still open, 👍/👎/💩/🙏 reply “Finish or cancel ✍️ first.”

---

## 5. Settings

Stored per Telegram user in `users.settings_json`. Changed in Mini App (`POST /api/settings`) or by answering a dest prompt (which writes the same prefs).

| Setting | Values | Default | Effect |
| --- | --- | --- | --- |
| **Google Drive** | connected / unlink | off | See [Google login](#6-google-login). Unlink clears refresh token + folder ids; tagging continues. |
| **Default save (`default_dest`)** | `library` / `review` / `none` | `none` | Where a **new** identify job goes **if Drive is connected**. Forced to `none` while logged out. |
| **Correct Telegram file** | on / off | off | After save, `editMessageMedia` replaces the public audio with the tagged file. Off: caption-only on first save. Later ✍️ / 🙏 still replace media. |
| **Delete original file** | on / off | off | After a successful save, delete the **user’s original** audio message (not the bot’s public card). |
| **Do not ask again (`skip_save_prompt`)** | on / off | off | Logged-in only. Skip the dest keyboard; use the toggles above. Forced **off** while logged out so a later login still prompts. |
| **Listen-for formats** | flac, mp3, m4a, ogg, opus, wav | flac+mp3 | Which files ingest. Others silently ignored. HiFi picks use the same list. |
| **Suggest similarity** | 0.0–1.0 | 0.5 | Last.fm familiarity slider (Explorer ↔ Familiar). |
| **Allow songs unlike library** | on / off | off | Loosen `/suggest` filtering. |

### Dest prompt (Telegram, after identify)

Shown only when logged in **and** `skip_save_prompt` is false **and** this job has not already confirmed dest (🙏 forces confirmed `none`).

Toggles: Drive library / Drive review / No Drive upload, Correct Telegram file, Delete original file, Do not ask again. **OK** commits and **persists** those prefs for next time. Cancel aborts the pending row.

### Setting × login matrix (identify / save)

| | Logged out | Logged in, dest `none` | Logged in, dest `library` or `review` |
| --- | --- | --- | --- |
| Dest prompt | **skipped**; dest locked `none` | shown unless “do not ask again” | same |
| Drive upload | never | no | yes, into `Telegram Music` or `Telegram Music Review` |
| Language folder | still used for caption/index; no Drive tree | Drive path `/{language}/…` when dest is library | same |
| Correct Telegram file | caption-only unless 🙏/✍️ | honors toggle | honors toggle |
| Delete original | honors toggle | honors toggle | honors toggle |
| 👍 / 👎 copy | local user copy only (no Drive root) | upload to that user’s Drive | same |
| `/review` Drive sync | no remote listing | lists Drive review extras | same |
| Daily cleanup | drop local after **7 days**; no Drive retry | keep local 7 days after Drive-confirmed upload, then drop if Drive still has the file | Drive retry on failed rows |

---

## 6. Google login

`logged_in` ⇔ non-empty `google_refresh_token`.

### Connect

```text
/login or Mini App Connect
  issue one-time ticket (user_id bound)
  GET /oauth/start?token=…  → consume ticket
  redirect Google auth (PKCE, drive.file + drive.install + email + openid, prompt=consent, offline)
  GET /oauth/callback
  ├─ error=access_denied → “Google denied access. Use /login again.”
  ├─ bad/expired state → “This login session expired.”
  ├─ token exchange missing refresh_token → needs_login
  ├─ email ≠ previously linked email → forbidden (old token kept)
  └─ ok
        ensure Drive folders “Telegram Music” + “Telegram Music Review”
        store refresh token, email, folder ids
        drop cached Drive client for that user
        HTML: “Google Drive connected…”
```

Ticket/state TTL default 600s, single use. `GOOGLE_CLIENT_SECRET` stays on the server. Testing-mode Google tokens die ~7 days → `Google login expired. Use /login again.` (`invalid_grant` never dumps the token).

Second Google account on the same Telegram user is refused.

### Logged out (never connected, unlinked, or token cleared)

- Identify / tag / public caption still run.
- No dest prompt; no Drive create/replace/delete.
- 👍/👎/💩 operate on **local** per-user copies only; `relocate_track` skips Drive when folder ids are empty.
- Cleanup: no upload retry; locals older than 7 days deleted; idle users forgotten after `USER_INACTIVE_MONTHS` (default 3; tokens first if any remain).
- Mini App home: “Tagging still works in Telegram without Drive.” + Connect button.

### Logged in

- Dest prompt (unless skipped) can copy tagged audio into that user’s Drive only (`drive.file` — folders this app created after login).
- 👍 copies the current Telegram file into **your** library Drive; 👎 into **your** review Drive; 💩 deletes **your** Drive+local copy.
- 🙏 / ✍️ do **not** rewrite Drive until a later 👍/👎.
- Name conflict on upload: Replace / Keep both / Skip (Skip keeps local).
- Daily 03:00 UTC: retry failed Drive uploads; after 7 days drop a local whose Drive copy still exists.
- Unlink: `POST /api/settings` `{unlink: true}` — Drive files already saved stay in Google until the user deletes them.

### Login failures the user sees

| Symptom | Cause |
| --- | --- |
| Login link expired | Ticket reused or TTL passed. `/login` again. |
| Google denied access | Consent cancelled. |
| Google login expired. Use /login again. | Refresh token dead / `invalid_grant`. |
| Access denied / forbidden | Blacklisted, or a different Google email than the one already linked. |
| Private access requires membership… | DM without a known group (and not env admin). |

---

## Identify pipeline (shared by audio, HiFi pick, 🙏)

1. **Hints** from filename + existing tags.
2. **AcoustID** → recordings; **MusicBrainz** for credits, album, track/disc, composer.
3. **Enrich**: Last.fm tags → `genre \| mood \| language \| instrument`; LRCLIB **synced** lyrics only; cover URLs.
4. **Normalize** artist lists (`A, B, C & D`) and genre allowlist.
5. **Low confidence** → human review (group diff keyboard or DM editor) before dest/cover.
6. **High confidence** → language → cover → dest → tags on disk → Drive (if dest ≠ `none`) → public caption/media.

Tags written: `TITLE`, `ALBUM`, `ARTIST`, `ALBUMARTIST`, `COMPOSER`, `GENRE`, `DATE`, `TRACKNUMBER`, `DISCNUMBER`, `LYRICS`, one front cover. Everything else stripped.

---

## Error / privacy notes

- User-facing strings are short codes (`needs_login`, `timeout`, `busy`, …). Telegram and HTML never get stack traces, tokens, or Google bodies.
- Asker gets ephemeral/DM. Infra (dead HiFi session, disk full, HTTP bind) DMs the env admin.
- One failed job does not kill the worker, dispatcher, or HTTP server.
- Mini App APIs trust `initData` HMAC only, scoped to that user.
