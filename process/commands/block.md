# Block Command

This command is used to block a user or a group or a channel.

## Usage

```markdown
/block
or
/block <user_id> or <group_id> or <channel_id>
or
/block @username or @groupname or @channelname
```

## Scope

- **Direct messages:** Command available in direct messages.
- **Group chats:** Command available in group chats.
- **Ephemeral:** Command is ephemeral. The chat is hidden from the other users.
- **Admin:** Command is available only to admin user id set in the environment variable.

## Response

```markdown
Blocked <user_id or group_id or channel_id>
```

## Example

```markdown
Blocked @username (34123241)
```

## Caviats

1. If command is invoked in group, the response message will be sent as ephemeral.
2. if Block command is invoked without any arguments in a channel/group, block that channel/group.
3. if Block command is invoked with a user_id/username in a channel/group, block that user only in that channel/group.
4. if Block command is invoked with a @groupname/@channelname AND user_id/username, block that user only in that @groupname/@channelname.
5. if Block command is invoked with a @groupname/@channelname AND user_id/username, block that user only in that @groupname/@channelname.
6. If user has disabled message notifications, do not send any notifications to the user.

## Notifications

1. If user or group or channel is blocked, send notification to user as "You have been blocked by the admin". This is info.
2. If user or group or channel is already blocked, send notification to admin as "User or group or channel is already blocked". This is info.
3. If user or group or channel is not found, send notification to admin as "User or group or channel not found". This is error.
4. If user is already blocked and trying to block for only a particular channel/group, send notification to admin as "User is already blocked, cannot block for only a particular channel/group". This is error.
