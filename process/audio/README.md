# Audio Process

## Scope

- **Direct messages:** Command available in direct messages.
- **Group chats:** Command available in group chats.

## Caviats

1. If user is blocked, response is they are blocked. Handled before reaching this handler.

## Steps

1. [Download](1. Download.md)
2. [Checks](2. Checks.md)
3. [Identify](3. Identify.md)
4. [Albumart](4. Albumart.md)
5. [Upload](5. Upload.md)

## Multiple Songs in same chat

If Multiple songs are sent on the same chat then

1. download max 1 at a time for the same chat_id
2. Checks can happen in parallel, but be mindful of API ratelimits of external APIs.
3. Identify can happpen in parallel, but be mindful of API ratelimits of external APIs.
4. Only one albumart selection at a time for the same chat_id. If multiple songs of the same album uploaded at the same time, ensure the second song skips asking for albumart again by picking the albumart from previous selection.
5. Upload only one at a time per user.
