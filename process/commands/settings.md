# Settings Command

This command is used to view and edit the bot's settings.

## Usage

```markdown
/settings
```

## Scope

- **Direct messages:** Command available in direct messages.
- **Group chats:** Command available in group chats.
- **Not Ephemeral:** Command is ephemeral. The chat is visible to other users.

## Response

```markdown
Drive: <email> | `/login` to upload files to your drive.
Default Save: Library|Review|None|Ask each time
Auto Forward: chat_id1,chat_id2,chat_id3|None
Correct telegram file: Yes|No|Ask each time
Delete Original File: Yes|No|Ask each time
Notifications: All|Errors Only|None
Notification Channel: Current Chat|Private Chat
Music Files: <list of music file formats>

Button: "Edit Settings in Mini App"
```

- **Button Action**: Edit Settings in Mini App will open the mini app settings page.

## Example

```markdown
Drive: username@example.com
Default Save: Library
Auto Forward: 1234567890,-1234567890
Correct telegram file: Yes
Delete Original File: Yes
Notifications: Errors Only
Notification Channel: Current Chat
Music Files: FLAC

Button: "Edit Settings in Mini App"
```

## Caviats

1. If command is invoked in group, the response message will be sent as ephemeral.
2. If command is invoked in the same chat as autoforward, autoforward will not be triggered for that chatId again.

## Default User Settings

```markdown
Default Save: Ask each time
Auto Forward: None
Correct telegram file: Yes
Delete Original File: No
Notifications: All
Notification Channel: Current Chat
Music Files: FLAC, MP3, M4A, WAV
```

## Settings Meanings

User settings are per-user settings.

1. Default Save: The default save location for the user in google drive.
   - Library: Save to the library folder in the user's drive.
   - Review: Save to the review folder in the user's drive.
   - None: Dont upload to google drive.
   - Ask each time: Ask the user for the save location each time.
2. Auto Forward: The chatIds to auto forward the processed files to.
   - chat_id1,chat_id2,chat_id3: Auto forward the processed files to the chatIds.
   - None: Dont auto forward the processed files.
3. Correct telegram file: Whether to correct the telegram file by replacing the file with the corrected version after edits.
   - Yes: Replace the file with the corrected version after edits.
   - No: Keep the original file.
   - Ask each time: Ask the user for the correct telegram file each time.
4. Delete Original File: Whether to delete the original file that was sent by the user.
   - Yes: Delete the original file after processing.
   - No: Keep the original file.
   - Ask each time: Ask the user for the delete original file each time.
5. Notifications: Whether to send notifications to the user.
   - All: Send notifications for all actions.
   - Errors Only: Send notifications for errors only.
   - None: Silent for all actions.
6. Notification Channel: The channel to send notifications to.
   - Current Chat: Send notifications to the chat where the command was invoked.
   - Private Chat: Send notifications to the user's private chat.
   - Reactions Only: Send notifications only as reactions to the message.
7. Music Files: The file formats to process. If the file is not in the list, it will be ignored.

## No Notifications
