# List Groups Command

This command is used to list all the groups that the bot is active in.

## Usage

```markdown
/listgroups
```

## Scope

- **Direct messages:** Command available in direct messages.
- **Group chats:** Command available in group chats.
- **Ephemeral:** Command is ephemeral. The chat is hidden from the other users.
- **Admin:** Command is available only to admin user id set in the environment variable.

## Response

```markdown
Groups: <count>

Recent Groups: <List of groups>

Buttons: One for each group
Buttons: Pagination buttons
```

- **Button Action**: Group button will toggle block/unblock group.
- **Button Action**: Pagination buttons will navigate through the groups.

## Example

```markdown
Groups: 100

Recent Groups:
@groupname (-34123241)

Buttons: one for each group (show groupname, group_id)
Buttons: Pagination buttons
```

## Caviats

1. If command is invoked in group, the response message will be sent as ephemeral.
2. If no groups are found, no buttons will be shown.
3. Paginations are kept only for short durations. controlled by environment variable.
4. buttons have return_type as "callback_data" and return_data as "group_id" for the group. So pagination will not work after long time, but group buttons will still work.
5. recent groups are listed in the order of most recent first. max Y are listed. (Y is set in environment variable as "MAX_RECENT_GROUPS_TO_REMEMBER").

## No Notifications
