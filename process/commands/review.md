# Review Command

This command is used to review the files in the user's review pile.

## Usage

```markdown
/review
```

## Scope

- **Direct messages:** Command available in direct messages.
- **Group chats:** Command available in group chats.
- **Ephemeral:** Command is ephemeral. The chat is hidden from the other users.

## Response

```markdown
Review Pile: <count>

Buttons: One for each file
Buttons: Open Review | Pagination buttons
```

- **Button Action**: Open Review will open the review pile in the mini app.
- **Button Action**: File button will download the file and send it to the user in the same chat. Send a placeholder message with a loading animation. Once the file is downloaded, replace the placeholder message with the file+song card caption.
- **Button Action**: Pagination buttons will navigate through the files.

## Caviats

1. If command is invoked in group, the response message will be sent to private chat of the user. Not in group.
2. If no files are found, no buttons will be shown.
3. Files sent back are not ephemeral.
4. Files sent back have a song card caption along with telegram specific song card formatting.
5. If user deletes the placeholder message, cancel the download and any associated actions as well.
6. Paginations are kept only for short durations. controlled by environment variable.
7. buttons have return_type as "callback_data" and return_data as "file_id" for the file. So pagination will not work after long time, but file buttons will still work.

## Notifications

1. If user deletes the placeholder message, send notification to user as "Download cancelled". This is info.
2. If file not found despite button being clicked, send notification to user as "File not found". This is error.
3. if refresh token is invalid, send notification to user as "Google Drive connection expired. Please connect again.". This is error. If message notification is enabled by user, send ephimeral message with button "Connect Google Drive" to connect again.
