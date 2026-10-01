# Get Command

This command is used to get the files from the user's google drive.

## Usage

```markdown
/get <song name>
```

## Scope

- **Direct messages:** Command available in direct messages.
- **Group chats:** Command available in group chats.
- **Ephemeral:** Command is ephemeral. The chat is hidden from the other users.

## Response

```markdown
Files Found: <count>

Buttons: One for each file
Buttons: Pagination buttons
```

- **Button Action**: File button will download the file and send it to the user in the same chat. Send a placeholder message with a loading animation. Once the file is downloaded, replace the placeholder message with the file+song card caption.
- **Button Action**: Pagination buttons will navigate through the files.

## Caviats

1. If command is invoked in group, the response message will be sent as ephemeral.
2. If no Files are found, no buttons will be shown.
3. Song files sent back are not ephemeral.
4. song files sent back have a song card caption along with telegram specific song card formatting.
5. If google drive is not connected, try using local file cache.
6. If user deletes the placeholder message, cancel the download and any associated actions as well.
7. Paginations are kept only for short durations. controlled by environment variable.
8. buttons have return_type as "callback_data" and return_data as "file_id" for the file. So pagination will not work after long time, but file buttons will still work.

## Notifications

1. if empty search query is provided, send notification to user as "Please provide a valid search query". This is error.
2. If user deletes the placeholder message, send notification to user as "Download cancelled". This is info.
3. If file not found despite button being clicked, send notification to user as "File not found". This is error.
4. if refresh token is invalid, send notification to user as "Google Drive connection expired. Please connect again.". This is error. If message notification is enabled by user, send ephimeral message with button "Connect Google Drive" to connect again.
