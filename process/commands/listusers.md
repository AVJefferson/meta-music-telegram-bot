# List Users Command

This command is used to list all the users who are active in the bot. Bot forgets users after X days of inactivity. X is set in the environment variable.

## Usage

```markdown
/listusers
```

## Scope

- **Direct messages:** Command available in direct messages.
- **Group chats:** Command available in group chats.
- **Ephemeral:** Command is ephemeral. The chat is hidden from the other users.
- **Admin:** Command is available only to admin user id set in the environment variable.

## Response

```markdown
Users: <count>

Recent Users: <list of recent users>

Buttons: one for each user (show username, user_id)
Buttons: Pagination buttons
```

- **Button Action**: User button will toggle block/unblock user.
- **Button Action**: Pagination buttons will navigate through the users.

## Example

```markdown
Users: 100

Recent Users:
@username (34123241)

Buttons: one for each user (show userid, user_id)
Buttons: Pagination buttons
```

## Caviats

1. If command is invoked in group, the response message will be sent as ephemeral.
2. If no users are found, no buttons will be shown.
3. Paginations are kept only for short durations. controlled by environment variable.
4. buttons have return_type as "callback_data" and return_data as "user_id" for the user. So pagination will not work after long time, but user buttons will still work.
5. admin user (set in environment variable) is not listed.
6. recent users are listed in the order of most recent first. max Y are listed. (Y is set in environment variable as "MAX_RECENT_USERS_TO_REMEMBER").

## No Notifications
