# Roblox verification and raid-points Discord bot

This Python bot uses Discord slash commands and a local SQLite database. It
keeps the uploaded bot's Roblox group-check card, creates private verification
tickets, tracks raid points, and awards configured Discord roles automatically.
The verification panel says **Verify**, and its blue button is labeled **Verify**
with no emoji.

## Main commands

- `/groupcheck roblox_username` — show a Roblox user's joined groups in the
  paginated card layout from the supplied screenshot.
- `/register discord_user roblox_username` — staff-only manual registration.
  The Discord member can be entered by exact username, ID, or mention.
- `/info account` — look up a registered Roblox username or Discord username,
  ID, or mention. Successful lookups are public and show the link without
  sending a mention notification.
- `/z` — owner-only export of the complete local bot database as a ZIP file.
- `/r backup_file` — owner-only restore from a `/z` ZIP. The bot validates the
  archive, shows a preview, and requires confirmation before replacing current
  data. It first keeps a local safety copy of the database being replaced.
- `/ticketpanel` — post the verification panel.
- `/verification-config` — set approved Roblox groups, restricted groups,
  verified role, ticket category, and escalation settings.
- `/raid-point add` and `/raid-point remove` — change a member's point total and
  refresh the live leaderboard.
- `/pset role points_needed` — choose a server role and a threshold from 1 to
  100. The bot grants the role at or above the threshold and removes it if the
  member's points later fall below it.
- `/raid-leaderboard` — post or refresh the live top-ten board.
- `/rreset` — clear raid points and remove point-promotion roles from cached
  members.
- `/ticket-close` — save the ticket transcript to the configured log channel
  and close the ticket.

The automatic ticket check sends the same paginated Roblox group card used by
`/groupcheck`, followed by the verification result. Restricted-group matches
are sent for staff review; the bot does not automatically kick those users.
Entering a valid Roblox username in a verification ticket saves that Roblox
account to the submitting Discord member, even if the group policy later sends
the ticket to staff review. This is a self-reported link and does not prove
Roblox account ownership. Staff can use `/register` to correct or reassign a
link. `/info` intentionally lets server members look up these registered links.
The uploaded moderation, strike, staff, tag, and Roblox presence commands are
also retained. They are slash commands as well.

## Install and run

1. Install Python 3.11 or newer.
2. Install dependencies from this folder:

   ```bash
   python -m pip install -r requirements.txt
   ```

3. Create a Discord application and bot in the Discord Developer Portal. Invite
   it with the `bot` and `applications.commands` scopes.
4. Enable **Server Members Intent**. Enable **Message Content Intent** if you
   want the existing message-edit/delete logs to include message text.
5. Set `DISCORD_TOKEN` in the host's environment/secrets manager. Set
   `FOUNDER_IDS` to one or more Discord user IDs (comma-separated) so the
   founder can configure staff and run owner commands. Do not put the bot token
   in this folder or commit it.
6. Optionally set `GUILD_ID` to sync slash commands to one server immediately;
   otherwise global command sync can take longer.
7. Start the bot:

   ```bash
   python main.py
   ```

## Bot permissions

Grant only the permissions needed for the features you plan to use. Verification
needs **Manage Channels** and permission to view/send messages in tickets.
Verification and point promotions need **Manage Roles**, with the bot's highest
role placed above the verified and promotion roles. Ticket transcripts need
**Read Message History** and **Attach Files** in the ticket and log channels.
The `/z` backup command needs **Attach Files** in the channel where it is run.
Moderation commands need their corresponding kick/ban permissions.

## Configuration

Set these environment variables before starting the bot. The verification
settings can also be changed with `/verification-config`.

| Variable | Purpose |
| --- | --- |
| `DISCORD_TOKEN` | Required Discord bot token |
| `FOUNDER_IDS` | Required founder Discord user IDs, separated by commas |
| `GUILD_ID` | Optional server ID for immediate slash-command sync |
| `VERIFICATION_GROUP_IDS` | Optional Roblox group IDs accepted for verification, separated by commas |
| `BLACKLIST_GROUP_IDS` | Optional Roblox group IDs that require staff review |
| `VERIFIED_ROLE_ID` | Optional role assigned after verification |
| `TICKET_CATEGORY_ID` | Optional category for new verification tickets |
| `TOP5TH_ROLE_ID` | Optional escalation role; it and higher roles can see review tickets |
| `LOG_CHANNEL_ID` | Optional channel for moderation logs and ticket transcripts |
| `RAID_LEADERBOARD_CHANNEL_ID` | Optional channel for the persistent leaderboard |
| `QUARANTINE_ROLE_ID` | Optional role applied by the join-spike safeguard |
| `RAID_JOIN_THRESHOLD` | Join-spike threshold; default is 8 |
| `RAID_WINDOW_SECONDS` | Join-spike window; default is 30 seconds |
| `BOT_DB_PATH` | Optional SQLite database path; default is `bot/data/moderation.sqlite3` |

The bot uses public Roblox APIs for account lookup and group checks. No Roblox
cookie is needed for verification or group checks. The local SQLite database
stores verification links, configuration, raid points, and moderation records;
no separate hosted database is required. `/z` downloads a copy of that data,
and `/r` replaces the current database with a validated backup rather than
merging records.
