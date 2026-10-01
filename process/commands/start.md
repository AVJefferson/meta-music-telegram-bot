# Start Command

Welcome message + stats + How to use the bot.

## Usage

```markdown
/start
or
/stats
```

## Scope

- **Direct messages:** Command available in direct messages.
- **Group chats:** Command available in group chats.
- **Not Ephemeral:** Command is ephemeral. The chat is visible to other users.

## Response

```markdown
Hi @<username>, welcome to the MetaMusic bot!
User Since YYYY MMM DD
Songs Processed: <number_of_songs> Songs Edited: <number_of_songs_edited>
Drive: <email> | `/login` to upload files to your drive.
Commands: <list_of_commands>, or send audio file to process.

**Your account will be deleted if inactive for <days> days**
```

## Example

```markdown
Hi @username, welcome to the MetaMusic bot!
User Since 2026 Sep 30
Songs Processed: 100 Songs Edited: 10
Drive: username@example.com
**Your account will be deleted if inactive for 90 days**

Commands: /start, /settings, /get, /review, /suggest, or send audio file to process.
```

## Caviats

1. If command is invoked in group, the response message will be sent as ephemeral.
2. If user is admin, only then admin commands will be listed.
3. if user is not logged in, only then `/login` command will be listed in the drive section.

## No Notifications
