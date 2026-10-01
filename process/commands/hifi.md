# Hifi Command

This command is used to find songs from @HiFiAudioBot.

## Usage

```markdown
/hifi <song name or artist name or album name>
```

## Scope

- **Direct messages:** Command available in direct messages.
- **Group chats:** Command available in group chats.
- **Ephemeral:** Command is ephemeral. The chat is hidden from the other users.

## Response

```markdown
Found <count> songs in @HiFiAudioBot.

Buttons: One for each song
Buttons: Pagination buttons
```

- **Button Action**: Song button will click the same button in @HiFiAudioBot. @HiFiAudioBot will respond with the song file. MetaMusic will process the song file and send it back to the user. Send a placeholder message with a loading animation. Once the file is downloaded, start processing the file and keep user updated on the same placeholder card. Once the file is processed, replace the placeholder message with the file+song card caption.
- **Button Action**: Pagination buttons will navigate through the songs.

## Example

## Caviats

1. If command is invoked in group, the response message will be sent as ephemeral.
2. If no songs are found, no buttons will be shown.
3. Song files sent back are not ephemeral.
4. song files sent back will go through the process of cleanup and correction.
5. If user deletes the placeholder message, cancel the download and any associated actions as well.
6. Paginations are kept only for short durations. controlled by environment variable.
7. song buttons may not work after long time as well.

## Notifications

1. if empty search query is provided, send notification to user as "Please provide a valid search query". This is error.
2. If user deletes the placeholder message, send notification to user as "Download cancelled". This is info.
3. If file not found despite button being clicked, send notification to user as "File not found". This is error.
4. If @HiFiAudioBot is not enabled, send notification to user as "@HiFiAudioBot has not been enabled by admin". This is error.
