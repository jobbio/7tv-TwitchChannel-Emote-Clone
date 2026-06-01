# 7TV Twitch Channel Emote Clone

Copy emotes from one Twitch channel's 7TV emote set to another — all missing emotes, specific ones, or an entire set cloned as a new named set.

## Requirements

- Python 3.10+
- A free [Twitch developer app](https://dev.twitch.tv/console/apps) (for username resolution)
- A 7TV session token for the **target** account (see below)

```sh
pip install -r requirements.txt
cp .env.example .env
# fill in .env, then:
python clone_emotes.py --from <source> --to <target>
```

---

## Credentials

### Twitch app credentials

These are used only to resolve Twitch usernames to numeric IDs. They are not tied to either channel's identity.

1. Enable two-factor authentication on your Twitch account — Twitch requires this before allowing developer app registration. You can do this under **Settings → Security and Privacy** on twitch.tv.
2. Go to [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps) and click **Register Your Application**.
3. Name it anything (e.g. `emote-clone`), set the OAuth Redirect URL to `http://localhost`, set the Client Type to **Confidential**, and choose any category.
4. Click **Manage** on the created app and copy the **Client ID** and generate a **Client Secret**.
5. Add both to your `.env`:

```
TWITCH_CLIENT_ID=your_client_id_here
TWITCH_CLIENT_SECRET=your_client_secret_here
```

### 7TV session token

This token represents the **target** account — the one you are adding emotes to. It must belong to either the owner of that 7TV account or someone granted editor access on it. The source channel is read publicly and requires no credentials.

To obtain the token:

1. Open [7tv.app](https://7tv.app) in your browser and log in with the **target** Twitch account.
2. Open DevTools (`F12`) and go to the **Network** tab.
3. Filter requests by `gql` (or trigger any action on the site to generate a request).
4. Click any request to `7tv.io/v3/gql` and open the **Headers** pane.
5. Find the `Authorization` header — its value starts with `Bearer `. Copy everything **after** `Bearer `.
6. Add it to your `.env`:

```
SEVENTV_TOKEN=your_token_here
```

> The token is tied to your browser session and will expire when you log out or the session expires. Re-extract it if you get 401 errors.

---

## Usage

```sh
# Merge source's active set into target's active set
python clone_emotes.py --from streamerA --to streamerB

# Preview without making changes (no SEVENTV_TOKEN needed)
python clone_emotes.py --from streamerA --to streamerB --dry-run

# Copy only specific emotes
python clone_emotes.py --from streamerA --to streamerB --emotes PogChamp,Sadge,OMEGALUL

# List all emote sets on the source channel
python clone_emotes.py --from streamerA --to streamerB --list-source-sets

# Copy from a specific source set (not the active one)
python clone_emotes.py --from streamerA --to streamerB --source-set "My Old Set"

# Clone source set as a brand-new named set on the target (does not affect active set)
python clone_emotes.py --from streamerA --to streamerB --new-set

# Clone and immediately activate the new set
python clone_emotes.py --from streamerA --to streamerB --new-set --activate

# Custom name for the new set
python clone_emotes.py --from streamerA --to streamerB --new-set --set-name "Imported from A"
```

---

## Output

```
  +  PogChamp              copied successfully
  =  Sadge                 name already exists in target set — skipped
  !  OMEGALUL — <reason>   failed
```

Emotes already present in the target set are detected both before the run (from the set snapshot) and at write time (via API error). In both cases they are flagged and skipped without counting as failures.

---

## Authentication model

| Credential | Purpose | Tied to which identity |
|---|---|---|
| `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` | Resolve Twitch usernames to IDs | Neither — app-level only |
| `SEVENTV_TOKEN` | Write to the target emote set | Target account (owner or editor) |

The source channel is always read from the public 7TV API — no credentials required.
