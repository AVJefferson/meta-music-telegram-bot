# Unblock Command

This command is used to unblock a user or a group or a channel.

## Usage

```markdown
/unblock
or
/unblock <user_id> or <group_id> or <channel_id>
or
/unblock @username or @groupname or @channelname
```

## Scope

- **Direct messages:** Command available in direct messages.
- **Group chats:** Command available in group chats.
- **Ephemeral:** Command is ephemeral. The chat is hidden from the other users.
- **Admin:** Command is available only to admin user id set in the environment variable.

## Response

```markdown
Unblocked <user_id or group_id or channel_id>
```

## Example

```markdown
Unblocked @username (34123241)
```

## Caviats

1. If command is invoked in group, the response message will be sent as ephemeral.
2. if Unblock command is invoked without any arguments in a channel/group, unblock that channel/group.
3. if Unblock command is invoked with a user_id/username in a channel/group, unblock that user only in that channel/group.
4. if Unblock command is invoked with a @groupname/@channelname AND user_id/username, unblock that user only in that @groupname/@channelname.
5. if Unblock command is invoked with a @groupname/@channelname AND user_id/username, unblock that user only in that @groupname/@channelname.
6. if user is already blocked, cannot unblock that user for only a particular channel/group.
7. If user has disabled message notifications, do not send any notifications to the user.

## Notifications

1. If user or group or channel is unblocked, send notification to user as "You have been unblocked by the admin". This is info.
2. If user or group or channel is already unblocked, send notification to admin as "User or group or channel is already unblocked". This is info.
3. If user or group or channel is not found, send notification to admin as "User or group or channel not found". This is error.
4. If user is blocked and, send notification to admin as "User is blocked, cannot unblock for only a particular channel/group". This is error.
