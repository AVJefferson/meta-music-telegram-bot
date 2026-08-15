"""OAuth scopes. Kept separate so `python -m app.drive_auth` needs no googleapiclient."""

DRIVE_SCOPE = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
]
