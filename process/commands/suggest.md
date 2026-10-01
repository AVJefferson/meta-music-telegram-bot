# Suggest Command

This command is used to suggest songs to the user.

## Usage

```markdown
/suggest <search query>
```

## Scope

- **Direct messages:** Command available in direct messages.
- **Group chats:** Command available in group chats.
- **Not Ephemeral:** Command is ephemeral. The chat is visible to other users.

## Response

```markdown
Found <count> songs in @HiFiAudioBot.

Buttons: One for each song
Buttons: Open suggestions | Pagination buttons
```

- **Button Action**: Song button will open the song suggestion card in the mini app.
- **Button Action**: Open suggestions will open the suggestions in the mini app with same search query.
- **Button Action**: Pagination buttons will navigate through the songs.

## Caviats

1. If command is invoked in group, the response message will be as normal message. Not ephemeral.
2. If no songs are found, no buttons will be shown.
3. Song files sent back are not ephemeral.
4. Paginations are kept only for short durations. controlled by environment variable.
5. Buttons may not work after long time as well.

## Notifications

1. if empty search query is provided, send notification to user as "Please provide a valid search query". This is error.
