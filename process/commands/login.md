# Login Command

This command is used to login to google drive. This allows the bot to upload files to the user's drive.

## Usage

```markdown
/login
```

## Scope

- **Direct messages:** Command available in direct messages.
- **Group chats:** Command available in group chats.
- **Ephemeral:** Command is ephemeral. The chat is hidden from the other users.

## Response

### If user is not logged in

```markdown
Connect to Google Drive. This bot can see only the files you share with it.
Button: "Connect Google Drive"
```

- **Button Action**: Connect Google Drive will open the mini app login page.

### If user is logged in and refresh token is valid

```markdown
Google Drive already connected.
```

### If user is logged in and refresh token is invalid

```markdown
Google Drive connection expired. Please connect again.
Button: "Connect Google Drive"
```

- **Button Action**: Connect Google Drive will open the mini app login page.

## Caviats

1. If command is invoked in group, the response message will be sent as ephemeral.

## No Notifications
