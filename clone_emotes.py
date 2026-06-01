#!/usr/bin/env python3
"""
Copy 7TV emotes from one Twitch channel to another.

Modes
-----
  Merge (default)
    Copies emotes from the source's active set into the target's existing active set.

  New set  (--new-set)
    Creates a new named emote set on the target account and copies emotes into it.
    Add --activate to immediately make it the channel's active set.

  Pick source set  (--source-set NAME_OR_ID)
    Uses a specific named/ID set from the source channel instead of its active set.
    Works with both merge and --new-set modes.

Examples
--------
  # Merge source active set → target active set
  python clone_emotes.py --from streamerA --to streamerB

  # List the source channel's available emote sets
  python clone_emotes.py --from streamerA --to streamerB --list-source-sets

  # Copy from a specific source set into target's active set
  python clone_emotes.py --from streamerA --to streamerB --source-set "My Old Set"

  # Clone source set as a new set on the target (but don't activate)
  python clone_emotes.py --from streamerA --to streamerB --new-set

  # Clone and immediately activate
  python clone_emotes.py --from streamerA --to streamerB --new-set --activate

  # All modes accept --emotes and --dry-run
  python clone_emotes.py --from streamerA --to streamerB --emotes PogChamp,Sadge --dry-run
"""

import argparse
import os
import sys
import time

import requests
from dotenv import load_dotenv


class EmoteNameConflictError(Exception):
    """Raised when the target set already contains an emote with the same name."""

class EmoteSetCapacityError(Exception):
    """Raised when the target emote set has no remaining capacity."""

load_dotenv()

_GQL_MAX_RETRIES = 3

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_USERS_URL = "https://api.twitch.tv/helix/users"
SEVENTV_GQL      = "https://7tv.io/v3/gql"
SEVENTV_API      = "https://7tv.io/v3"

# ---------------------------------------------------------------------------
# GraphQL mutations
# ---------------------------------------------------------------------------

_GQL_CHANGE_EMOTE = """
mutation ChangeEmoteInSet($id: ObjectID!, $action: ListItemAction!, $emote_id: ObjectID!, $name: String) {
  emoteSet(id: $id) {
    emotes(id: $emote_id, action: $action, name: $name) {
      id
      name
    }
  }
}
"""

_GQL_CREATE_EMOTE_SET = """
mutation CreateEmoteSet($user_id: ObjectID!, $data: CreateEmoteSetInput!) {
  createEmoteSet(user_id: $user_id, data: $data) {
    id
    name
    capacity
  }
}
"""

_GQL_UPDATE_CONNECTION = """
mutation UpdateUserConnection($id: ObjectID!, $conn_id: String!, $d: UserConnectionUpdate!) {
  userConnection(id: $id, conn_id: $conn_id, d: $d) {
    id
    emote_set_id
  }
}
"""


# ---------------------------------------------------------------------------
# Twitch helpers
# ---------------------------------------------------------------------------

def _get_twitch_app_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(TWITCH_TOKEN_URL, params={
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def _resolve_twitch_id(username: str, client_id: str, token: str) -> str:
    resp = requests.get(
        TWITCH_USERS_URL,
        headers={"Client-Id": client_id, "Authorization": f"Bearer {token}"},
        params={"login": username},
    )
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if not data:
        raise ValueError(f"Twitch user '{username}' not found")
    return data[0]["id"]


# ---------------------------------------------------------------------------
# 7TV helpers
# ---------------------------------------------------------------------------

def _gql(query: str, variables: dict, token: str) -> dict:
    for attempt in range(_GQL_MAX_RETRIES + 1):
        resp = requests.post(
            SEVENTV_GQL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"query": query, "variables": variables},
        )
        if resp.status_code == 429:
            if attempt == _GQL_MAX_RETRIES:
                resp.raise_for_status()
            # Honour Retry-After if present, otherwise exponential backoff (2s, 4s, 8s).
            wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
            print(f"    [rate limited — waiting {wait}s before retry {attempt + 1}/{_GQL_MAX_RETRIES}]")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        body = resp.json()
        if "errors" in body:
            error   = body["errors"][0]
            message = error.get("message", "")
            code    = error.get("extensions", {}).get("code")
            if code == 70403 or any(k in message.lower() for k in ("already enabled", "name taken", "already in use")):
                raise EmoteNameConflictError(message)
            if any(k in message.lower() for k in ("capacity", "no more room", "set is full")):
                raise EmoteSetCapacityError(message)
            raise RuntimeError(message)
        return body["data"]


def _get_seventv_user(twitch_id: str) -> dict:
    """Fetch a 7TV user by their Twitch numeric ID."""
    resp = requests.get(f"{SEVENTV_API}/users/twitch/{twitch_id}")
    if resp.status_code == 404:
        raise ValueError(f"No 7TV account linked to Twitch ID {twitch_id}")
    resp.raise_for_status()
    return resp.json()


def _get_emote_set(set_id: str) -> dict:
    """Fetch a full emote set (including its emotes) by ID."""
    resp = requests.get(f"{SEVENTV_API}/emote-sets/{set_id}")
    resp.raise_for_status()
    return resp.json()


def _resolve_source_set(user: dict, name_or_id: str | None) -> dict:
    """
    Return the emote set to use from the source user.

    If name_or_id is None, return the active channel set.
    Otherwise match against the user's owned sets by name (case-insensitive) or ID,
    fetching full emote data if needed.
    """
    if name_or_id is None:
        active = user.get("emote_set")
        if not active:
            raise ValueError("Source channel has no active 7TV emote set.")
        return active

    # The user response includes emote_sets: list of sets owned by this user.
    owned: list[dict] = user.get("emote_sets") or []

    # Also include the active set in the search pool.
    active = user.get("emote_set")
    if active and not any(s.get("id") == active.get("id") for s in owned):
        owned = [active] + owned

    needle = name_or_id.lower()
    match  = next(
        (s for s in owned if s.get("id") == name_or_id or (s.get("name") or "").lower() == needle),
        None,
    )
    if not match:
        names = [f"  {s.get('name', '?')} ({s.get('id', '?')})" for s in owned]
        hint  = "\n".join(names) if names else "  (none found)"
        raise ValueError(
            f"No emote set matching '{name_or_id}' found for this channel.\n"
            f"Available sets:\n{hint}"
        )

    # The set entry in emote_sets may not include the full emotes list; fetch it.
    if not match.get("emotes"):
        match = _get_emote_set(match["id"])

    return match


def _add_emote(set_id: str, emote_id: str, alias: str | None, token: str) -> None:
    _gql(_GQL_CHANGE_EMOTE, {
        "id": set_id,
        "action": "ADD",
        "emote_id": emote_id,
        "name": alias,
    }, token)


def _create_emote_set(user_id: str, name: str, token: str) -> dict:
    data = _gql(_GQL_CREATE_EMOTE_SET, {
        "user_id": user_id,
        "data": {"name": name},
    }, token)
    return data["createEmoteSet"]


def _activate_set(seventv_user_id: str, twitch_conn_id: str, set_id: str, token: str) -> None:
    _gql(_GQL_UPDATE_CONNECTION, {
        "id": seventv_user_id,
        "conn_id": twitch_conn_id,
        "d": {"emote_set_id": set_id},
    }, token)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy 7TV emotes from one Twitch channel to another",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--from", dest="source", required=True, metavar="CHANNEL",
                        help="Source Twitch channel name")
    parser.add_argument("--to",   dest="target", required=True, metavar="CHANNEL",
                        help="Target Twitch channel name")
    parser.add_argument("--source-set", metavar="NAME_OR_ID",
                        help="Use a specific emote set from the source channel (default: active set)")
    parser.add_argument("--list-source-sets", action="store_true",
                        help="List all emote sets on the source channel and exit")
    parser.add_argument("--emotes", metavar="NAMES",
                        help="Comma-separated emote names to copy (default: all missing)")
    parser.add_argument("--new-set", action="store_true",
                        help="Create a new emote set on the target rather than merging into its active set")
    parser.add_argument("--set-name", metavar="NAME",
                        help="Name for the new set (default: source set name). Requires --new-set")
    parser.add_argument("--activate", action="store_true",
                        help="Make the new set the active channel set after copying. Requires --new-set")
    parser.add_argument("--delay", type=float, default=0.2, metavar="SECONDS",
                        help="Seconds to wait between emote additions (default: 0.2)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without making any changes")
    args = parser.parse_args()

    if args.activate and not args.new_set:
        parser.error("--activate requires --new-set")
    if args.set_name and not args.new_set:
        parser.error("--set-name requires --new-set")

    seventv_token        = os.environ.get("SEVENTV_TOKEN")
    twitch_client_id     = os.environ.get("TWITCH_CLIENT_ID")
    twitch_client_secret = os.environ.get("TWITCH_CLIENT_SECRET")

    if not args.dry_run and not args.list_source_sets and not seventv_token:
        sys.exit("Error: SEVENTV_TOKEN is required for writes. Use --dry-run to preview without it.")
    if not twitch_client_id or not twitch_client_secret:
        sys.exit("Error: TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are required.")

    # ── Resolve Twitch IDs ───────────────────────────────────────────────────
    print("Authenticating with Twitch...")
    try:
        twitch_token     = _get_twitch_app_token(twitch_client_id, twitch_client_secret)
        source_twitch_id = _resolve_twitch_id(args.source, twitch_client_id, twitch_token)
        target_twitch_id = _resolve_twitch_id(args.target, twitch_client_id, twitch_token)
    except (requests.HTTPError, ValueError) as e:
        sys.exit(f"Twitch lookup failed: {e}")

    # ── Fetch 7TV users ──────────────────────────────────────────────────────
    print(f"Fetching 7TV data for '{args.source}' and '{args.target}'...")
    try:
        source_user = _get_seventv_user(source_twitch_id)
        target_user = _get_seventv_user(target_twitch_id)
    except (requests.HTTPError, ValueError) as e:
        sys.exit(f"7TV lookup failed: {e}")

    # ── --list-source-sets ───────────────────────────────────────────────────
    if args.list_source_sets:
        owned: list[dict] = source_user.get("emote_sets") or []
        active = source_user.get("emote_set") or {}
        if not any(s.get("id") == active.get("id") for s in owned):
            owned = [active] + owned
        print(f"\nEmote sets for '{args.source}':")
        for s in owned:
            active_marker = " [active]" if s.get("id") == active.get("id") else ""
            emote_count   = len(s.get("emotes") or [])
            print(f"  {s.get('name', '(unnamed)'):<30}  id: {s.get('id', '?')}  "
                  f"emotes: {emote_count}{active_marker}")
        return

    # ── Resolve source set ───────────────────────────────────────────────────
    try:
        source_set = _resolve_source_set(source_user, args.source_set)
    except ValueError as e:
        sys.exit(str(e))

    # ── Resolve target set ───────────────────────────────────────────────────
    target_set = target_user.get("emote_set") or {}
    if not args.new_set and not target_set:
        sys.exit(f"'{args.target}' has no active 7TV emote set. Use --new-set to create one.")

    # ── Build emote maps ─────────────────────────────────────────────────────
    source_emotes: dict[str, dict] = {e["name"]: e for e in source_set.get("emotes", [])}

    if args.emotes:
        requested = {n.strip() for n in args.emotes.split(",")}
        not_in_source = requested - source_emotes.keys()
        if not_in_source:
            print(f"Warning: not found in source set: {', '.join(sorted(not_in_source))}")
        source_emotes = {k: v for k, v in source_emotes.items() if k in requested}

    if args.new_set:
        target_names = set()
        new_set_name = args.set_name or source_set.get("name", f"{args.source}-emotes")
        dest_set_id  = None  # created at write time
    else:
        target_names = {e["name"] for e in target_set.get("emotes", [])}
        dest_set_id  = target_set["id"]

    to_copy         = {name: e for name, e in source_emotes.items() if name not in target_names}
    already_present = [name for name in source_emotes if name in target_names]

    # ── Print plan ───────────────────────────────────────────────────────────
    print(f"\nSource : {source_set.get('name', '(unnamed)')} "
          f"— {len(source_set.get('emotes', []))} emotes total")
    if args.new_set:
        print(f"Target : new set '{new_set_name}' on '{args.target}'")
        if args.activate:
            print("         (will be activated after copy)")
    else:
        print(f"Target : {target_set.get('name', '(unnamed)')} "
              f"— {len(target_set.get('emotes', []))} emotes total")

    print(f"\n  To copy        : {len(to_copy)}")
    if not args.new_set:
        print(f"  Already present: {len(already_present)}")

    if not to_copy and not already_present:
        print("\nNothing to copy.")
        return

    if args.dry_run:
        if already_present:
            print("\n[dry-run] Already in target set (would skip):")
            for name in sorted(already_present):
                print(f"  = {name}")
        if to_copy:
            print("\n[dry-run] Would copy:")
            for name in sorted(to_copy):
                original   = to_copy[name].get("data", {}).get("name", name)
                alias_note = f"  (alias for '{original}')" if original != name else ""
                print(f"  + {name}{alias_note}")
        if args.new_set:
            print(f"\n[dry-run] Would create set '{new_set_name}' on '{args.target}'")
            if args.activate:
                print(f"[dry-run] Would activate '{new_set_name}' as the channel set")
        return

    if not to_copy:
        print("\nNothing to copy.")
        return

    # ── Create new set if requested ──────────────────────────────────────────
    if args.new_set:
        print(f"\nCreating emote set '{new_set_name}' on '{args.target}'...")
        try:
            new_set     = _create_emote_set(target_user["id"], new_set_name, seventv_token)
            dest_set_id = new_set["id"]
            print(f"  Created: {dest_set_id}")
        except (requests.HTTPError, RuntimeError) as e:
            sys.exit(f"Failed to create emote set: {e}")

    # ── Copy emotes ──────────────────────────────────────────────────────────
    print()
    copied, skipped, failed = [], [], []
    capacity_hit = False
    emote_list   = sorted(to_copy.items())

    for i, (name, emote) in enumerate(emote_list):
        emote_id      = emote["id"]
        original_name = emote.get("data", {}).get("name", name)
        alias         = name if name != original_name else None
        try:
            _add_emote(dest_set_id, emote_id, alias, seventv_token)
            print(f"  + {name}")
            copied.append(name)
        except EmoteNameConflictError:
            print(f"  = {name}  (name already in target set — skipped)")
            skipped.append(name)
        except EmoteSetCapacityError:
            remaining = len(emote_list) - i
            print(f"  ! {name}  (set is at capacity — stopping)")
            print(f"    {remaining} emote(s) not attempted due to full set.")
            failed.extend(name for name, _ in emote_list[i:])
            capacity_hit = True
            break
        except (requests.HTTPError, RuntimeError) as e:
            print(f"  ! {name} — {e}")
            failed.append(name)

        if args.delay and i < len(emote_list) - 1:
            time.sleep(args.delay)

    parts = [f"{len(copied)} copied"]
    if skipped:
        parts.append(f"{len(skipped)} skipped (name conflict)")
    if failed:
        label = "not attempted (set full)" if capacity_hit else "failed"
        parts.append(f"{len(failed)} {label}")
    print(f"\n{', '.join(parts)}.")
    if capacity_hit:
        print("The target set has reached its emote capacity. Upgrade the channel's 7TV subscription to increase the limit.")

    # ── Activate new set ─────────────────────────────────────────────────────
    if args.new_set and args.activate:
        if failed:
            print("Skipping activation because some emotes failed to copy.")
        else:
            print(f"Activating '{new_set_name}'...")
            try:
                _activate_set(target_user["id"], target_twitch_id, dest_set_id, seventv_token)
                print("  Done — new set is now the active channel set.")
            except (requests.HTTPError, RuntimeError) as e:
                print(f"  Activation failed: {e}")
                sys.exit(1)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
