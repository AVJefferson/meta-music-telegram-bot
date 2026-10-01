# List Channels Command

This command is used to list all the channels that the bot is active in.

## Usage

```markdown
/listchannels
```

## Scope

- **Direct messages:** Command available in direct messages.
- **Group chats:** Command available in group chats.
- **Ephemeral:** Command is ephemeral. The chat is hidden from the other users.
- **Admin:** Command is available only to admin user id set in the environment variable.

## Response

```markdown
Channels: <count>

Recent Channels: <List of channels>

Buttons: One for each channel
Buttons: Pagination buttons
```

- **Button Action**: Channel button will toggle block/unblock channel.
- **Button Action**: Pagination buttons will navigate through the channels.

## Example

```markdown
Channels: 100

Recent Channels:
@channelname (-34123241)

Buttons: one for each channel (show channelname, channel_id)
Buttons: Pagination buttons
```

## Caviats

1. If command is invoked in group, the response message will be sent as ephemeral.
2. If no channels are found, no buttons will be shown.
3. Paginations are kept only for short durations. controlled by environment variable.
4. buttons have return_type as "callback_data" and return_data as "channel_id" for the channel. So pagination will not work after long time, but channel buttons will still work.
5. recent channels are listed in the order of most recent first. max Y are listed. (Y is set in environment variable as "MAX_RECENT_CHANNELS_TO_REMEMBER").

## No Notifications
