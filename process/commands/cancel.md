# Cancel Command

The cancel comand is used to cancel an ongoing process triggered by the user.

## Usage

```markdown
/cancel
```

## Scope

- **Direct messages:** Command available in direct messages.
- **Group chats:** Command available in group chats.
- **Ephemeral:** Command is ephemeral. The chat is hidden from the other users.

## Response

```markdown
Cancelled <count> ongoing process|processes.
```

## Example

```markdown
Cancelled 1 ongoing process.
```

## Caviats

1. If command is invoked in group, the response message will be sent as ephemeral.
2. If no ongoing processes are found, no message will be sent.

## Notifications

1. If user has disabled message notifications, do not send any notifications to the user.
2. If user has reactions as notifications, react to the message to confirm the cancellation.
