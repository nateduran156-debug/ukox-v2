from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sqlite3
import tempfile
import zipfile
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks


GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
DB_PATH = Path(os.getenv("BOT_DB_PATH", "bot/data/moderation.sqlite3"))
LOG_CHANNEL_ID = int(
    os.getenv("LOG_CHANNEL_ID", "0") or 0
) or None
ROBLOX_GROUP_ID = int(os.getenv("ROBLOX_GROUP_ID", "396910998") or 0) or 396910998
ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE", "").strip()
TAG_NAMES = (
    "Member",
    "sharingan tag",
    "rockstar",
    "dark",
    "FaZe",
    "fraid",
    "tracemog",
    "flax",
    "x",
    "SUKAA",
    "Admin",
    "Owner",
)
TAG_MANAGER_ROLE_NAME = os.getenv("TAG_MANAGER_ROLE_NAME", "Tag Manager")
MEMBER_ROLE_NAME = os.getenv("MEMBER_ROLE_NAME", "members")
FOUNDER_IDS = {
    *{
        int(value.strip())
        for value in os.getenv("FOUNDER_IDS", "").split(",")
        if value.strip().isdigit()
    },
}
ROLE_RANK = {"staff": 1, "owner": 2, "founder": 3}
DOTS = ["", ".", "..", "..."]
EMBED_COLOR = 0x5A0F14
SUCCESS_COLOR = 0x57F287
WARNING_COLOR = 0xFEE75C
ERROR_COLOR = 0xED4245
BACKUP_FORMAT_VERSION = 1
BACKUP_DATABASE_ENTRY = "bot-data.sqlite3"
BACKUP_MANIFEST_ENTRY = "manifest.json"
MAX_BACKUP_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_BACKUP_DATABASE_BYTES = 100 * 1024 * 1024
BACKUP_TABLES = (
    "whitelist",
    "hardbans",
    "guild_config",
    "strikes",
    "roblox_links",
    "snipe_targets",
    "snipe_presence",
    "raid_points",
    "point_promotions",
    "moderation_events",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_ids(value: str) -> list[int]:
    return [int(item) for item in value.split(",") if item.strip().isdigit()]


def default_config() -> dict[str, Any]:
    return {
        "strike_channel_id": None,
        "snipe_channel_id": int(os.getenv("SNIPE_CHANNEL_ID", "0") or 0) or None,
        "raid_leaderboard_channel_id": int(
            os.getenv("RAID_LEADERBOARD_CHANNEL_ID", "0") or 0
        )
        or None,
        "raid_leaderboard_message_id": None,
        "ticket_category_id": int(os.getenv("TICKET_CATEGORY_ID", "0") or 0) or None,
        "verified_role_id": int(os.getenv("VERIFIED_ROLE_ID", "0") or 0) or None,
        "top5th_role_id": int(os.getenv("TOP5TH_ROLE_ID", "0") or 0) or None,
        "log_channel_id": LOG_CHANNEL_ID,
        "quarantine_role_id": int(os.getenv("QUARANTINE_ROLE_ID", "0") or 0) or None,
        "member_role_id": int(os.getenv("MEMBER_ROLE_ID", "0") or 0) or None,
        "raid_join_threshold": int(os.getenv("RAID_JOIN_THRESHOLD", "8") or 8),
        "raid_window_seconds": int(os.getenv("RAID_WINDOW_SECONDS", "30") or 30),
        "verification_group_ids": parse_ids(os.getenv("VERIFICATION_GROUP_IDS", "")),
        "blacklist_group_ids": parse_ids(os.getenv("BLACKLIST_GROUP_IDS", "")),
        "blacklist_role_ids": parse_ids(os.getenv("BLACKLIST_ROLE_IDS", "")),
    }


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def roblox_link_for(guild_id: int, discord_user_id: int) -> sqlite3.Row | None:
    with connect() as db:
        return db.execute(
            """
            SELECT roblox_user_id, roblox_username
            FROM roblox_links
            WHERE guild_id = ? AND discord_user_id = ?
            """,
            (guild_id, discord_user_id),
        ).fetchone()


def register_roblox_link(
    guild_id: int,
    discord_user_id: int,
    roblox_user_id: int,
    roblox_username: str,
    *,
    allow_reassign: bool = False,
) -> tuple[bool, list[int]]:
    """Save a guild-scoped Roblox link and prevent accidental duplicate owners."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            """
            SELECT discord_user_id
            FROM roblox_links
            WHERE guild_id = ? AND roblox_user_id = ? AND discord_user_id != ?
            """,
            (guild_id, roblox_user_id, discord_user_id),
        ).fetchall()
        previous_owners = [int(row["discord_user_id"]) for row in existing]
        if previous_owners and not allow_reassign:
            return False, previous_owners
        if allow_reassign:
            db.execute(
                """
                DELETE FROM roblox_links
                WHERE guild_id = ? AND roblox_user_id = ?
                  AND discord_user_id != ?
                """,
                (guild_id, roblox_user_id, discord_user_id),
            )
        db.execute(
            """
            INSERT INTO roblox_links (
                guild_id, discord_user_id, roblox_user_id, roblox_username
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, discord_user_id) DO UPDATE SET
                roblox_user_id = excluded.roblox_user_id,
                roblox_username = excluded.roblox_username
            """,
            (guild_id, discord_user_id, roblox_user_id, roblox_username),
        )
    return True, previous_owners


def discord_id_from_text(value: str) -> int | None:
    value = value.strip()
    mention = re.fullmatch(r"<@!?(\d+)>", value)
    if mention:
        return int(mention.group(1))
    return int(value) if value.isdigit() else None


async def resolve_guild_member(
    guild: discord.Guild, identity: str
) -> tuple[discord.Member | None, str | None]:
    """Resolve an exact member username/display name, mention, or numeric ID."""
    user_id = discord_id_from_text(identity)
    if user_id is not None:
        member = guild.get_member(user_id)
        if member is not None:
            return member, None
        try:
            return await guild.fetch_member(user_id), None
        except discord.NotFound:
            return None, "not_found"
        except (discord.Forbidden, discord.HTTPException):
            return None, "unavailable"

    query = identity.strip().casefold()
    if not query:
        return None, "not_found"

    candidates = list(guild.members)
    try:
        candidates.extend(await guild.query_members(query=identity.strip(), limit=100))
    except (discord.Forbidden, discord.HTTPException):
        pass
    matches: dict[int, discord.Member] = {}
    for member in candidates:
        names = {
            member.name.casefold(),
            member.display_name.casefold(),
            str(getattr(member, "global_name", "") or "").casefold(),
            str(member).casefold(),
        }
        if query in names:
            matches[member.id] = member
    if len(matches) == 1:
        return next(iter(matches.values())), None
    if len(matches) > 1:
        return None, "ambiguous"
    return None, "not_found"


def init_db() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS whitelist (
                user_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL CHECK (role IN ('staff', 'owner'))
            );
            CREATE TABLE IF NOT EXISTS hardbans (
                user_id INTEGER PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id INTEGER PRIMARY KEY,
                strike_channel_id INTEGER,
                snipe_channel_id INTEGER,
                raid_leaderboard_channel_id INTEGER,
                raid_leaderboard_message_id INTEGER,
                ticket_category_id INTEGER,
                verified_role_id INTEGER,
                top5th_role_id INTEGER,
                log_channel_id INTEGER,
                quarantine_role_id INTEGER,
                member_role_id INTEGER,
                raid_join_threshold INTEGER NOT NULL DEFAULT 8,
                raid_window_seconds INTEGER NOT NULL DEFAULT 30,
                verification_group_ids TEXT NOT NULL DEFAULT '[]',
                blacklist_group_ids TEXT NOT NULL DEFAULT '[]',
                blacklist_role_ids TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS strikes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                strike_number INTEGER NOT NULL,
                reason TEXT NOT NULL,
                proof TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                consequence TEXT,
                created_at TEXT NOT NULL,
                revoked_at TEXT,
                revoked_by INTEGER
            );
            CREATE TABLE IF NOT EXISTS roblox_links (
                guild_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                roblox_user_id INTEGER NOT NULL,
                roblox_username TEXT NOT NULL,
                PRIMARY KEY (guild_id, discord_user_id)
            );
            CREATE TABLE IF NOT EXISTS snipe_targets (
                guild_id INTEGER NOT NULL,
                roblox_user_id INTEGER NOT NULL,
                roblox_username TEXT NOT NULL,
                added_by INTEGER NOT NULL,
                PRIMARY KEY (guild_id, roblox_user_id)
            );
            CREATE TABLE IF NOT EXISTS snipe_presence (
                guild_id INTEGER NOT NULL,
                roblox_user_id INTEGER NOT NULL,
                last_server_id TEXT,
                last_place_id INTEGER,
                in_game INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, roblox_user_id)
            );
            CREATE TABLE IF NOT EXISTS raid_points (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS point_promotions (
                guild_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                points_needed INTEGER NOT NULL
                    CHECK (points_needed BETWEEN 1 AND 100),
                PRIMARY KEY (guild_id, role_id)
            );
            CREATE TABLE IF NOT EXISTS moderation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                actor_id INTEGER,
                subject_id INTEGER,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


def database_table_counts(database_path: Path) -> dict[str, int]:
    connection = sqlite3.connect(database_path)
    try:
        connection.row_factory = sqlite3.Row
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError("The SQLite file failed its integrity check.")
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing = set(BACKUP_TABLES) - tables
        if missing:
            raise ValueError(
                "The SQLite file is missing required bot tables: "
                + ", ".join(sorted(missing))
            )
        unsafe_objects = connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('trigger', 'view')"
        ).fetchall()
        if unsafe_objects:
            raise ValueError("Bot backups cannot contain custom triggers or views.")
        virtual_tables = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND sql LIKE 'CREATE VIRTUAL TABLE%'
            """
        ).fetchall()
        if virtual_tables:
            raise ValueError("Bot backups cannot contain virtual tables.")
        return {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in BACKUP_TABLES
        }
    finally:
        connection.close()


def create_database_backup_archive() -> tuple[bytes, dict[str, Any]]:
    """Create a consistent ZIP snapshot of the entire bot SQLite database."""
    init_db()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="discord-bot-backup-") as temp_dir:
        snapshot_path = Path(temp_dir) / BACKUP_DATABASE_ENTRY
        source = sqlite3.connect(DB_PATH)
        snapshot = sqlite3.connect(snapshot_path)
        try:
            source.backup(snapshot)
        finally:
            snapshot.close()
            source.close()

        table_counts = database_table_counts(snapshot_path)
        manifest = {
            "format_version": BACKUP_FORMAT_VERSION,
            "exported_at": now_iso(),
            "table_counts": table_counts,
        }
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(
            archive_buffer, mode="w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.write(snapshot_path, arcname=BACKUP_DATABASE_ENTRY)
            archive.writestr(
                BACKUP_MANIFEST_ENTRY,
                json.dumps(manifest, indent=2, sort_keys=True),
            )
        return archive_buffer.getvalue(), manifest


def prepare_database_restore(data: bytes) -> tuple[Path, dict[str, Any]]:
    """Validate an exported ZIP and stage its database beside the live file."""
    if len(data) > MAX_BACKUP_UPLOAD_BYTES:
        raise ValueError("The backup ZIP is larger than the supported upload limit.")
    with zipfile.ZipFile(io.BytesIO(data), mode="r") as archive:
        entries = archive.infolist()
        entry_names = [entry.filename for entry in entries]
        if (
            len(entries) != 2
            or len(set(entry_names)) != 2
            or set(entry_names)
            != {BACKUP_DATABASE_ENTRY, BACKUP_MANIFEST_ENTRY}
        ):
            raise ValueError(
                "Upload the ZIP created by `/z`; it must contain the bot database and manifest."
            )
        database_entry = archive.getinfo(BACKUP_DATABASE_ENTRY)
        manifest_entry = archive.getinfo(BACKUP_MANIFEST_ENTRY)
        if database_entry.file_size > MAX_BACKUP_DATABASE_BYTES:
            raise ValueError("The database inside this backup exceeds the supported size.")
        if manifest_entry.file_size > 64 * 1024:
            raise ValueError("The backup manifest is too large.")
        manifest = json.loads(archive.read(BACKUP_MANIFEST_ENTRY).decode("utf-8"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("format_version") != BACKUP_FORMAT_VERSION
        ):
            raise ValueError("This backup format is not supported by this bot version.")
        database_bytes = archive.read(BACKUP_DATABASE_ENTRY)

    if len(database_bytes) > MAX_BACKUP_DATABASE_BYTES:
        raise ValueError("The database inside this backup exceeds the supported size.")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, staged_name = tempfile.mkstemp(
        prefix=".bot-restore-stage-",
        suffix=".sqlite3",
        dir=DB_PATH.parent,
    )
    staged_path = Path(staged_name)
    try:
        with os.fdopen(file_descriptor, "wb") as staged_file:
            staged_file.write(database_bytes)
        counts = database_table_counts(staged_path)
        declared_counts = manifest.get("table_counts")
        if declared_counts is not None and declared_counts != counts:
            raise ValueError("The database contents do not match the backup manifest.")
        return staged_path, counts
    except Exception:
        staged_path.unlink(missing_ok=True)
        raise


def save_pre_restore_database() -> Path | None:
    """Keep the current database as a local safety copy before replacement."""
    if not DB_PATH.exists():
        return None
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    safety_path = DB_PATH.with_name(DB_PATH.name + ".pre-restore")
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".bot-pre-restore-",
        suffix=".sqlite3",
        dir=DB_PATH.parent,
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    source = sqlite3.connect(DB_PATH)
    destination = sqlite3.connect(temporary_path)
    try:
        source.backup(destination)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        destination.close()
        source.close()
    os.replace(temporary_path, safety_path)
    return safety_path


class RestoreConfirmationView(discord.ui.View):
    def __init__(
        self, owner_id: int, staged_path: Path, counts: dict[str, int]
    ):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.staged_path: Path | None = staged_path
        self.counts = counts

    def discard_staged_file(self) -> None:
        if self.staged_path is not None:
            self.staged_path.unlink(missing_ok=True)
            self.staged_path = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who started this restore can confirm it.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Confirm restore",
        style=discord.ButtonStyle.danger,
        custom_id="database_restore:confirm",
    )
    async def confirm_restore(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.staged_path is None:
            await interaction.response.send_message(
                "This restore is no longer available. Run `/r` again.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        staged_path = self.staged_path
        try:
            safety_path = save_pre_restore_database()
            Path(f"{DB_PATH}-wal").unlink(missing_ok=True)
            Path(f"{DB_PATH}-shm").unlink(missing_ok=True)
            os.replace(staged_path, DB_PATH)
            self.staged_path = None
        except (OSError, sqlite3.Error):
            self.stop()
            self.discard_staged_file()
            await interaction.edit_original_response(
                content=(
                    "Restore failed before the new database was installed. "
                    "The current data was not intentionally replaced; check the bot logs and disk permissions."
                ),
                view=None,
                embed=None,
            )
            return

        self.stop()
        safety_note = (
            f" A safety copy of the previous data was saved as `{safety_path.name}`."
            if safety_path
            else ""
        )
        try:
            await interaction.edit_original_response(
                content=(
                    "Restore complete. The uploaded backup replaced the bot's current data."
                    + safety_note
                ),
                view=None,
                embed=None,
            )
        except discord.HTTPException:
            await interaction.followup.send(
                "Restore complete. The uploaded backup replaced the bot's current data."
                + safety_note,
                ephemeral=True,
            )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
        custom_id="database_restore:cancel",
    )
    async def cancel_restore(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.discard_staged_file()
        self.stop()
        await interaction.response.edit_message(
            content="Restore cancelled. The current database was not changed.",
            view=None,
            embed=None,
        )

    async def on_timeout(self) -> None:
        self.discard_staged_file()


def config_for(guild_id: int) -> dict[str, Any]:
    # Apply small SQLite migrations before creating a first row, so an older
    # database with no config row still receives the new columns safely.
    with connect() as db:
        existing = {
            row["name"]
            for row in db.execute("PRAGMA table_info(guild_config)").fetchall()
        }
        migrations = {
            "raid_leaderboard_channel_id": "INTEGER",
            "raid_leaderboard_message_id": "INTEGER",
            "top5th_role_id": "INTEGER",
            "log_channel_id": "INTEGER",
            "quarantine_role_id": "INTEGER",
            "member_role_id": "INTEGER",
            "raid_join_threshold": "INTEGER NOT NULL DEFAULT 8",
            "raid_window_seconds": "INTEGER NOT NULL DEFAULT 30",
        }
        for column, data_type in migrations.items():
            if column not in existing:
                db.execute(f"ALTER TABLE guild_config ADD COLUMN {column} {data_type}")
    with connect() as db:
        row = db.execute(
            "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        if row is None:
            defaults = default_config()
            db.execute(
                """
                INSERT INTO guild_config (
                    guild_id, strike_channel_id, snipe_channel_id,
                    raid_leaderboard_channel_id, raid_leaderboard_message_id,
                    ticket_category_id, verified_role_id, top5th_role_id,
                    log_channel_id, quarantine_role_id, member_role_id,
                    raid_join_threshold, raid_window_seconds,
                    verification_group_ids, blacklist_group_ids,
                    blacklist_role_ids
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    defaults["strike_channel_id"],
                    defaults["snipe_channel_id"],
                    defaults["raid_leaderboard_channel_id"],
                    defaults["raid_leaderboard_message_id"],
                    defaults["ticket_category_id"],
                    defaults["verified_role_id"],
                    defaults["top5th_role_id"],
                    defaults["log_channel_id"],
                    defaults["quarantine_role_id"],
                    defaults["member_role_id"],
                    defaults["raid_join_threshold"],
                    defaults["raid_window_seconds"],
                    json.dumps(defaults["verification_group_ids"]),
                    json.dumps(defaults["blacklist_group_ids"]),
                    json.dumps(defaults["blacklist_role_ids"]),
                ),
            )
            return defaults

    with connect() as db:
        row = db.execute(
            "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
        ).fetchone()

    result = dict(row)
    for key in (
        "verification_group_ids",
        "blacklist_group_ids",
        "blacklist_role_ids",
    ):
        result[key] = json.loads(result[key] or "[]")
    return result


def set_config_value(guild_id: int, key: str, value: Any) -> None:
    allowed = {
        "strike_channel_id",
        "snipe_channel_id",
        "raid_leaderboard_channel_id",
        "raid_leaderboard_message_id",
        "ticket_category_id",
        "verified_role_id",
        "top5th_role_id",
        "log_channel_id",
        "quarantine_role_id",
        "member_role_id",
        "raid_join_threshold",
        "raid_window_seconds",
        "verification_group_ids",
        "blacklist_group_ids",
        "blacklist_role_ids",
    }
    if key not in allowed:
        raise ValueError(f"Unsupported configuration key: {key}")
    if isinstance(value, list):
        value = json.dumps(value)
    with connect() as db:
        db.execute(
            f"UPDATE guild_config SET {key} = ? WHERE guild_id = ?",
            (value, guild_id),
        )


def record_moderation_event(
    guild_id: int,
    event_type: str,
    summary: str,
    actor_id: int | None = None,
    subject_id: int | None = None,
) -> None:
    with connect() as db:
        db.execute(
            """
            INSERT INTO moderation_events (
                guild_id, event_type, actor_id, subject_id, summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (guild_id, event_type, actor_id, subject_id, summary[:1000], now_iso()),
        )


def point_promotions_for(guild_id: int) -> list[sqlite3.Row]:
    with connect() as db:
        return db.execute(
            """
            SELECT role_id, points_needed
            FROM point_promotions
            WHERE guild_id = ?
            ORDER BY points_needed, role_id
            """,
            (guild_id,),
        ).fetchall()


async def sync_point_promotion_roles(
    guild: discord.Guild, member: discord.Member, points: int
) -> tuple[list[str], list[str], list[str]]:
    """Keep configured point-reward roles aligned with a member's current total."""
    promotions = point_promotions_for(guild.id)
    if not promotions:
        return [], [], []

    bot_member = guild.me
    if bot_member is None:
        return [], [], ["The bot's server member record is unavailable."]
    if not bot_member.guild_permissions.manage_roles:
        return [], [], ["The bot needs the Manage Roles permission."]

    granted: list[str] = []
    removed: list[str] = []
    errors: list[str] = []
    member_role_ids = {role.id for role in member.roles}
    for promotion in promotions:
        role = guild.get_role(promotion["role_id"])
        if role is None:
            errors.append(f"Configured role {promotion['role_id']} no longer exists.")
            continue
        if role == guild.default_role or role.managed:
            errors.append(f"{role.name} cannot be managed as a promotion role.")
            continue
        if role.position >= bot_member.top_role.position:
            errors.append(f"The bot's top role must be above {role.name}.")
            continue

        should_have_role = points >= promotion["points_needed"]
        already_has_role = role.id in member_role_ids
        if should_have_role == already_has_role:
            continue
        try:
            if should_have_role:
                await member.add_roles(role, reason="Raid point promotion threshold reached")
                granted.append(role.name)
            else:
                await member.remove_roles(
                    role, reason="Raid points fell below the promotion threshold"
                )
                removed.append(role.name)
        except (discord.Forbidden, discord.HTTPException):
            errors.append(f"Could not update {role.name}.")
    return granted, removed, errors


async def log_channel_for(guild: discord.Guild) -> discord.TextChannel | None:
    config = config_for(guild.id)
    channel_id = config.get("log_channel_id") or LOG_CHANNEL_ID
    if not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            fetched = await guild.fetch_channel(channel_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None
        channel = fetched
    return channel if isinstance(channel, discord.TextChannel) else None


async def audit_log(
    guild: discord.Guild,
    event_type: str,
    summary: str,
    details: list[tuple[str, str]] | None = None,
    actor_id: int | None = None,
    subject_id: int | None = None,
) -> None:
    """Write a durable record and send the same event to the configured log channel."""
    record_moderation_event(guild.id, event_type, summary, actor_id, subject_id)
    channel = await log_channel_for(guild)
    if channel is None:
        return
    try:
        await channel.send(
            view=styled_view(
                f"Moderation log • {event_type}",
                summary[:4000],
                details=details,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.Forbidden, discord.HTTPException):
        return


def find_named_role(
    guild: discord.Guild, name: str
) -> discord.Role | None:
    wanted = name.casefold().strip()
    return discord.utils.find(
        lambda role: role.name.casefold().strip() == wanted,
        guild.roles,
    )


def tag_manager_role(guild: discord.Guild) -> discord.Role | None:
    return find_named_role(guild, TAG_MANAGER_ROLE_NAME)


def is_tag_manager(member: discord.Member) -> bool:
    manager_role = tag_manager_role(member.guild)
    return bool(manager_role and manager_role in member.roles)


def tag_manager_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        allowed = isinstance(interaction.user, discord.Member) and (
            interaction.user.id in FOUNDER_IDS
            or is_tag_manager(interaction.user)
        )
        if not allowed:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    view=styled_view(
                        "Permission denied",
                        f"Only founders and members with the **{TAG_MANAGER_ROLE_NAME}** role can use this command.",
                    ),
                    ephemeral=True,
                )
            return False
        return True

    return app_commands.check(predicate)


def tag_roles(guild: discord.Guild) -> list[discord.Role]:
    roles: list[discord.Role] = []
    for tag in TAG_NAMES:
        role = find_named_role(guild, tag)
        if role:
            roles.append(role)
    return roles


def member_role(guild: discord.Guild, config: dict[str, Any]) -> discord.Role | None:
    configured_id = config.get("member_role_id")
    return guild.get_role(configured_id) if configured_id else find_named_role(
        guild, MEMBER_ROLE_NAME
    )


async def send_ticket_transcript(
    channel: discord.TextChannel, reason: str
) -> bool:
    """Export a ticket before deletion and send it to the moderation log channel."""
    guild = channel.guild
    log_channel = await log_channel_for(guild)
    if log_channel is None:
        return False
    lines = [
        f"Ticket transcript: #{channel.name}",
        f"Guild: {guild.name} ({guild.id})",
        f"Channel ID: {channel.id}",
        f"Closed: {now_iso()}",
        f"Reason: {reason}",
        "",
    ]
    try:
        history = channel.history(limit=None, oldest_first=True)
        async for message in history:
            content = message.content or "[no text content]"
            if message.attachments:
                content += " | Attachments: " + ", ".join(
                    attachment.url for attachment in message.attachments
                )
            lines.append(
                f"[{message.created_at.isoformat()}] "
                f"{message.author} ({message.author.id}): {content}"
            )
    except (discord.Forbidden, discord.HTTPException):
        return False
    payload = io.BytesIO("\n".join(lines).encode("utf-8", errors="replace"))
    payload.seek(0)
    try:
        await log_channel.send(
            view=styled_view(
                "Ticket transcript saved",
                f"The transcript for **#{channel.name}** was saved before closure.",
                details=[("Reason", reason[:1024])],
            ),
            file=discord.File(payload, filename=f"{channel.name}-transcript.txt"),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.Forbidden, discord.HTTPException):
        return False
    return True


async def close_ticket(channel: discord.TextChannel, reason: str) -> None:
    await send_ticket_transcript(channel, reason)
    await channel.delete(reason=reason)


def verification_escalation_roles(
    guild: discord.Guild, config: dict[str, Any]
) -> list[discord.Role]:
    """Return the configured Top 5th role and every role above it."""
    anchor_id = config.get("top5th_role_id")
    anchor = guild.get_role(anchor_id) if anchor_id else None
    if anchor is None:
        return []
    return [
        role
        for role in guild.roles
        if role != guild.default_role and role.position >= anchor.position
    ]


def verification_escalation_mentions(
    guild: discord.Guild, config: dict[str, Any]
) -> str:
    return " ".join(role.mention for role in verification_escalation_roles(guild, config))


def get_role(user_id: int) -> str | None:
    if user_id in FOUNDER_IDS:
        return "founder"
    with connect() as db:
        row = db.execute(
            "SELECT role FROM whitelist WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["role"] if row else None


def protected_from_moderation(user_id: int) -> bool:
    role = get_role(user_id)
    return user_id in FOUNDER_IDS or ROLE_RANK.get(role, 0) >= ROLE_RANK["staff"]


def role_check(required: str):
    async def predicate(interaction: discord.Interaction) -> bool:
        actual = get_role(interaction.user.id)
        if ROLE_RANK.get(actual, 0) < ROLE_RANK[required]:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    view=styled_view(
                        "Permission denied",
                        f"This command requires the **{required}** role.",
                    ),
                    ephemeral=True,
                )
            return False
        return True

    return app_commands.check(predicate)


def staff_check():
    return role_check("staff")


def owner_check():
    return role_check("owner")


def founder_check():
    return role_check("founder")


def moderation_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        role = get_role(interaction.user.id)
        has_permission = bool(
            interaction.guild
            and (
                interaction.user.guild_permissions.manage_guild
                or ROLE_RANK.get(role, 0) >= ROLE_RANK["staff"]
            )
        )
        if not has_permission:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    view=styled_view(
                        "Permission denied",
                        "You need Manage Server or staff access to use this command.",
                    ),
                    ephemeral=True,
                )
            return False
        return True

    return app_commands.check(predicate)


def add_styled_content(
    view: discord.ui.LayoutView,
    title: str,
    description: str = "",
    details: list[tuple[str, str]] | None = None,
    thumbnail: str | None = None,
) -> None:
    title_block = discord.ui.TextDisplay(title)
    if thumbnail:
        top = discord.ui.Section(
            title_block,
            accessory=discord.ui.Thumbnail(
                discord.UnfurledMediaItem(url=thumbnail)
            ),
        )
    else:
        top = title_block

    items: list[discord.ui.Item] = [
        top,
        discord.ui.Separator(spacing=discord.SeparatorSpacing.small, visible=True),
    ]
    if description:
        items.append(discord.ui.TextDisplay(description))
    if details:
        items.extend(
            [
                discord.ui.Separator(
                    spacing=discord.SeparatorSpacing.small, visible=True
                ),
                discord.ui.TextDisplay(
                    "\n".join(f"> {label}: {value}" for label, value in details)
                ),
            ]
        )
    view.add_item(discord.ui.Container(*items, accent_color=EMBED_COLOR))


def styled_view(
    title: str,
    description: str = "",
    details: list[tuple[str, str]] | None = None,
    thumbnail: str | None = None,
) -> discord.ui.LayoutView:
    """Render every response using the original group-check card layout."""
    view = discord.ui.LayoutView()
    add_styled_content(view, title, description, details, thumbnail)
    return view


def proof_text(proof: str) -> str:
    if proof.startswith(("https://", "http://")):
        return f"[Open proof]({proof})"
    return proof


def active_strikes(guild_id: int, user_id: int) -> list[sqlite3.Row]:
    with connect() as db:
        return db.execute(
            """
            SELECT * FROM strikes
            WHERE guild_id = ? AND user_id = ? AND status = 'active'
            ORDER BY id ASC
            """,
            (guild_id, user_id),
        ).fetchall()


async def publish_strike(
    interaction: discord.Interaction,
    strike: sqlite3.Row,
    total_active: int,
) -> None:
    guild = interaction.guild
    if guild is None:
        return
    config = config_for(guild.id)
    channel: discord.abc.Messageable = interaction.channel
    if config["strike_channel_id"]:
        configured = guild.get_channel(config["strike_channel_id"])
        if configured:
            channel = configured
    member = guild.get_member(strike["user_id"])
    subject = member.mention if member else f"<@{strike['user_id']}>"
    announcement = styled_view(
        f"Strike {strike['strike_number']}/3 issued",
        f"{subject} has been striked.",
        details=[("Active strikes", f"{total_active}/3")],
    )
    await channel.send(
        view=announcement,
        allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False),
    )


class GroupPageView(discord.ui.LayoutView):
    def __init__(
        self, groups: list[dict[str, Any]], display_name: str, avatar_url: str = ""
    ):
        super().__init__(timeout=300)
        self.groups = groups
        self.display_name = display_name
        self.avatar_url = avatar_url
        self.page = 0
        self._render()

    def _render(self) -> None:
        self.clear_items()
        group = self.groups[self.page]
        title_block = discord.ui.TextDisplay(
            f"[{group['name']}](https://www.roblox.com/groups/{group['id']})\n"
            f"-# **{self.display_name}**'s Joined groups"
        )
        top = (
            discord.ui.Section(
                title_block,
                accessory=discord.ui.Thumbnail(
                    discord.UnfurledMediaItem(url=group["iconUrl"])
                ),
            )
            if group.get("iconUrl")
            else title_block
        )
        items: list[discord.ui.Item] = []
        if self.avatar_url:
            items.extend(
                [
                    discord.ui.Section(
                        discord.ui.TextDisplay(
                            f"**Roblox user**\n{self.display_name}"
                        ),
                        accessory=discord.ui.Thumbnail(
                            discord.UnfurledMediaItem(url=self.avatar_url)
                        ),
                    ),
                    discord.ui.Separator(
                        spacing=discord.SeparatorSpacing.small, visible=True
                    ),
                ]
            )
        items.extend(
            [
            top,
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small, visible=True),
            discord.ui.TextDisplay(
                f"Est. Unknown\n"
                f"{group.get('description') or 'No description'}"
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small, visible=True),
            discord.ui.TextDisplay(
                f"> Members: {group['memberCount']}\n"
                f"> Public: {'True' if group.get('publicEntryAllowed') else 'False'}\n"
                f"> Rank: {group['role'].get('name', 'Unknown')}\n"
                f"> Group Id: `{group['id']}`"
            ),
            ]
        )
        self.add_item(discord.ui.Container(*items, accent_color=EMBED_COLOR))
        self.add_item(
            discord.ui.ActionRow(
                discord.ui.Button(
                    label="◄",
                    style=discord.ButtonStyle.secondary,
                    custom_id="group_previous",
                    disabled=self.page == 0,
                ),
                discord.ui.Button(
                    label="►",
                    style=discord.ButtonStyle.secondary,
                    custom_id="group_next",
                    disabled=self.page == len(self.groups) - 1,
                ),
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        custom_id = interaction.data.get("custom_id")
        if custom_id == "group_previous" and self.page > 0:
            self.page -= 1
        elif custom_id == "group_next" and self.page < len(self.groups) - 1:
            self.page += 1
        else:
            return False
        self._render()
        await interaction.response.edit_message(view=self)
        return False


async def fetch_roblox_user(session: aiohttp.ClientSession, username: str) -> dict[str, Any] | None:
    async with session.post(
        "https://users.roblox.com/v1/usernames/users",
        json={"usernames": [username], "excludeBannedUsers": False},
    ) as response:
        response.raise_for_status()
        users = (await response.json()).get("data", [])
    return users[0] if users else None


async def fetch_user_groups(
    session: aiohttp.ClientSession, user_id: int
) -> list[dict[str, Any]]:
    async with session.get(
        f"https://groups.roblox.com/v2/users/{user_id}/groups/roles"
    ) as response:
        response.raise_for_status()
        return (await response.json()).get("data", [])


async def fetch_group_metadata(
    session: aiohttp.ClientSession, group_id: int
) -> dict[str, Any]:
    async with session.get(f"https://groups.roblox.com/v1/groups/{group_id}") as response:
        response.raise_for_status()
        return await response.json()


async def assign_roblox_group_role(
    session: aiohttp.ClientSession,
    group_id: int,
    user_id: int,
    role_name: str,
) -> dict[str, Any]:
    """Assign an existing Roblox group role using the bot account's session."""
    if not ROBLOX_COOKIE:
        raise RuntimeError("ROBLOX_COOKIE is not configured.")

    async with session.get(
        f"https://groups.roblox.com/v1/groups/{group_id}/roles"
    ) as response:
        response.raise_for_status()
        roles = (await response.json()).get("roles", [])

    requested_name = role_name.casefold().strip()
    role = next(
        (
            item
            for item in roles
            if str(item.get("name", "")).casefold().strip() == requested_name
        ),
        None,
    )
    if role is None:
        raise LookupError(
            f'No Roblox group role named "{role_name}" exists in group {group_id}.'
        )

    endpoint = f"https://groups.roblox.com/v1/groups/{group_id}/users/{user_id}"
    headers = {
        "Cookie": f".ROBLOSECURITY={ROBLOX_COOKIE}",
        "Content-Type": "application/json",
        "User-Agent": "RobloxDiscordBot/1.0",
    }
    payload = {"roleId": int(role["id"])}

    for attempt in range(2):
        async with session.patch(endpoint, headers=headers, json=payload) as response:
            if response.status == 403 and attempt == 0:
                csrf_token = response.headers.get("x-csrf-token")
                if csrf_token:
                    headers["x-csrf-token"] = csrf_token
                    continue

            if response.status < 200 or response.status >= 300:
                try:
                    error_body = await response.json()
                    errors = error_body.get("errors", [])
                    message = errors[0].get("message") if errors else None
                except (ValueError, aiohttp.ContentTypeError):
                    message = None
                raise RuntimeError(
                    message
                    or f"Roblox returned HTTP {response.status} while assigning the role."
                )
            break

    return {
        "role_id": int(role["id"]),
        "role_name": role.get("name", role_name),
    }


async def fetch_roblox_avatar(
    session: aiohttp.ClientSession, user_id: int
) -> str:
    async with session.get(
        "https://thumbnails.roblox.com/v1/users/avatar-headshot",
        params={
            "userIds": user_id,
            "size": "150x150",
            "format": "Png",
            "isCircular": "true",
        },
    ) as response:
        response.raise_for_status()
        data = (await response.json()).get("data", [])
    return data[0].get("imageUrl", "") if data else ""


async def fetch_group_icon(
    session: aiohttp.ClientSession, group_id: int
) -> str:
    async with session.get(
        "https://thumbnails.roblox.com/v1/groups/icons",
        params={
            "groupIds": group_id,
            "size": "150x150",
            "format": "Png",
            "isCircular": "false",
        },
    ) as response:
        response.raise_for_status()
        data = (await response.json()).get("data", [])
    return data[0].get("imageUrl", "") if data else ""


async def group_results(
    session: aiohttp.ClientSession, user_id: int
) -> list[dict[str, Any]]:
    raw_groups = await fetch_user_groups(session, user_id)
    if not raw_groups:
        return []
    async def enrich(item: dict[str, Any]) -> dict[str, Any]:
        group = item["group"]
        try:
            details = await fetch_group_metadata(session, group["id"])
        except aiohttp.ClientError:
            details = {}
        icon_url = details.get("iconUrl", "")
        if not icon_url:
            try:
                icon_url = await fetch_group_icon(session, group["id"])
            except (aiohttp.ClientError, asyncio.TimeoutError):
                icon_url = ""
        return {
            "id": group["id"],
            "name": group["name"],
            "description": details.get("description", ""),
            "memberCount": details.get("memberCount", group.get("memberCount", 0)),
            "publicEntryAllowed": details.get("publicEntryAllowed", False),
            "role": item["role"],
            "iconUrl": icon_url,
        }
    return await asyncio.gather(*(enrich(item) for item in raw_groups))


async def configured_group_results(
    session: aiohttp.ClientSession,
    memberships: list[dict[str, Any]],
    group_ids: list[int],
) -> list[dict[str, Any]]:
    """Return a card-ready check for every configured group, joined or not."""
    joined = {item["group"]["id"]: item for item in memberships}

    async def enrich(group_id: int) -> dict[str, Any]:
        membership = joined.get(group_id)
        group = membership["group"] if membership else {"id": group_id, "name": str(group_id)}
        try:
            details = await fetch_group_metadata(session, group_id)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            details = {}
        icon_url = details.get("iconUrl", "")
        if not icon_url:
            try:
                icon_url = await fetch_group_icon(session, group_id)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                icon_url = ""
        return {
            "id": group_id,
            "name": details.get("name", group.get("name", str(group_id))),
            "description": details.get("description", ""),
            "memberCount": details.get("memberCount", group.get("memberCount", 0)),
            "publicEntryAllowed": details.get("publicEntryAllowed", False),
            "role": membership["role"] if membership else None,
            "iconUrl": icon_url,
            "joined": membership is not None,
        }

    return await asyncio.gather(*(enrich(group_id) for group_id in group_ids))


def group_check_view(
    display_name: str,
    groups: list[dict[str, Any]],
    avatar_url: str = "",
    leave_groups: list[dict[str, Any]] | None = None,
) -> discord.ui.LayoutView:
    """Render the verification group audit using the bot's standard card."""
    view = discord.ui.LayoutView()
    header = discord.ui.TextDisplay(f"**{display_name}** • Roblox group check")
    header_section = (
        discord.ui.Section(
            header,
            accessory=discord.ui.Thumbnail(
                discord.UnfurledMediaItem(url=avatar_url)
            ),
        )
        if avatar_url
        else header
    )
    items: list[discord.ui.Item] = [
        header_section,
        discord.ui.Separator(spacing=discord.SeparatorSpacing.small, visible=True),
        discord.ui.TextDisplay("**Configured group check**"),
    ]
    if groups:
        for group in groups:
            membership = (
                f"Joined • {group['role'].get('name', 'Unknown')}"
                if group.get("joined") or group.get("role")
                else "Not joined"
            )
            label = discord.ui.TextDisplay(
                f"[{group['name']}](https://www.roblox.com/groups/{group['id']})\n"
                f"> {membership} • Group ID: `{group['id']}`"
            )
            if group.get("iconUrl"):
                items.append(
                    discord.ui.Section(
                        label,
                        accessory=discord.ui.Thumbnail(
                            discord.UnfurledMediaItem(url=group["iconUrl"])
                        ),
                    )
                )
            else:
                items.append(label)
    else:
        items.append(discord.ui.TextDisplay("No verification groups are configured."))

    leave_groups = leave_groups or []
    items.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small, visible=True))
    if leave_groups:
        items.append(
            discord.ui.TextDisplay(
                "**Groups to leave before verification**\n"
                + "\n".join(
                    f"• [{group['name']}](https://www.roblox.com/groups/{group['id']})"
                    for group in leave_groups
                )
            )
        )
    else:
        items.append(discord.ui.TextDisplay("**Groups to leave before verification**\nNone"))
    view.add_item(discord.ui.Container(*items, accent_color=EMBED_COLOR))
    return view


class VerificationModal(discord.ui.Modal, title="Roblox Verification"):
    username = discord.ui.TextInput(
        label="Roblox username",
        placeholder="Enter your exact Roblox username",
        min_length=3,
        max_length=20,
        required=True,
    )

    def __init__(self, ticket_channel: discord.TextChannel):
        super().__init__()
        self.ticket_channel = ticket_channel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                view=styled_view(
                    "Verification unavailable",
                    "This verification flow is only available in a server.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        username = str(self.username.value).strip()
        config = config_for(guild.id)
        try:
            timeout = aiohttp.ClientTimeout(total=25)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                user = await fetch_roblox_user(session, username)
                if not user:
                    await interaction.followup.send(
                        view=styled_view(
                            "Roblox account not found",
                            "Check the spelling and submit your exact Roblox username again.",
                        ),
                        ephemeral=True,
                    )
                    return
                linked, _ = register_roblox_link(
                    guild.id,
                    interaction.user.id,
                    user["id"],
                    user["name"],
                )
                if linked:
                    await audit_log(
                        guild,
                        "registration",
                        f"{interaction.user} registered Roblox account {user['name']} through a verification ticket.",
                        details=[("Roblox user ID", str(user["id"]))],
                        actor_id=interaction.user.id,
                        subject_id=interaction.user.id,
                    )
                groups = await fetch_user_groups(session, user["id"])
                check_group_ids = list(
                    dict.fromkeys(
                        config["verification_group_ids"]
                        + config["blacklist_group_ids"]
                    )
                )
                checked_groups = await configured_group_results(
                    session, groups, check_group_ids
                )
                joined_groups = await group_results(session, user["id"])
                try:
                    avatar_url = await fetch_roblox_avatar(session, user["id"])
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    avatar_url = ""
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await interaction.followup.send(
                view=styled_view(
                    "Roblox unavailable",
                    "Roblox did not respond in time. Please try again shortly.",
                ),
                ephemeral=True,
            )
            return

        joined_ids = {item["group"]["id"] for item in groups}
        blocked = joined_ids.intersection(config["blacklist_group_ids"])
        blocked_groups = [
            group for group in checked_groups if group["id"] in blocked
        ]
        if joined_groups:
            await self.ticket_channel.send(
                view=GroupPageView(joined_groups, user["name"], avatar_url)
            )
        else:
            await self.ticket_channel.send(
                view=group_check_view(
                    user["name"],
                    checked_groups,
                    avatar_url,
                    leave_groups=blocked_groups,
                )
            )
        if not linked:
            await self.ticket_channel.send(
                content=verification_escalation_mentions(guild, config) or None,
                view=styled_view(
                    "Roblox account already registered",
                    "This Roblox account is linked to another server member. Staff must review it before verification can continue.",
                ),
                allowed_mentions=discord.AllowedMentions(
                    users=False, roles=True, everyone=False
                ),
            )
            await interaction.followup.send(
                view=styled_view(
                    "Verification pending",
                    "This Roblox account is already registered to another member. Staff have been asked to review the ticket.",
                ),
                ephemeral=True,
            )
            return
        if blocked:
            blocked_names = ", ".join(
                f"[{group['name']}](https://www.roblox.com/groups/{group['id']})"
                for group in blocked_groups
            )
            await self.ticket_channel.send(
                view=styled_view(
                    "Verification declined",
                    f"**{user['name']}** is associated with a restricted Roblox group. "
                    f"Leave the restricted group(s): {blocked_names or 'see the group check above'}, "
                    "then ask HR to review this ticket.",
                )
            )
            hr_mentions = verification_escalation_mentions(guild, config)
            await self.ticket_channel.send(
                content=hr_mentions or None,
                view=styled_view(
                    "HR review requested",
                    "This member could not be verified automatically. A member of HR "
                    "or a role above the configured Top 5th role must review this ticket.",
                ),
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=True, everyone=False
                ),
            )
            await interaction.followup.send(
                view=styled_view(
                    "Verification declined",
                    "Your ticket now contains the group check and has been sent to HR for review.",
                ),
                ephemeral=True,
            )
            return

        required_groups = config["verification_group_ids"]
        if required_groups and not joined_ids.intersection(required_groups):
            await self.ticket_channel.send(
                content=verification_escalation_mentions(guild, config) or None,
                view=styled_view(
                    "HR review requested",
                    "The account is not in an approved verification group. "
                    "HR should review this ticket before verification can continue.",
                ),
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=True, everyone=False
                ),
            )
            await interaction.followup.send(
                view=styled_view(
                    "Verification pending",
                    "You are not a member of an approved Roblox group. HR has been pinged in the ticket.",
                ),
                ephemeral=True,
            )
            return

        role = guild.get_role(config["verified_role_id"]) if config["verified_role_id"] else None
        if role:
            member = guild.get_member(interaction.user.id) or await guild.fetch_member(
                interaction.user.id
            )
            try:
                await member.add_roles(role, reason="Roblox verification completed")
            except discord.Forbidden:
                await interaction.followup.send(
                    view=styled_view(
                        "Verification setup issue",
                        "Your Roblox account passed, but I cannot assign the verified role.",
                    ),
                    ephemeral=True,
                )
                return
        await self.ticket_channel.send(
            view=styled_view(
                "Verification approved",
                f"**{user['name']}** has been verified successfully. This ticket will close shortly.",
                thumbnail=avatar_url,
            )
        )
        # Keep compatibility with servers that use a separate verification
        # module listening for the conventional .verify handoff.
        await self.ticket_channel.send(f".verify {interaction.user.mention}")
        await interaction.followup.send(
            view=styled_view(
                "Verification approved",
                "Verification completed successfully.",
            ),
            ephemeral=True,
        )
        await asyncio.sleep(3)
        await audit_log(
            guild,
            "verification",
            f"{interaction.user} completed Roblox verification as {user['name']}.",
            details=[
                ("Roblox user ID", str(user["id"])),
                ("Ticket", self.ticket_channel.mention),
            ],
            actor_id=interaction.user.id,
            subject_id=interaction.user.id,
        )
        await close_ticket(self.ticket_channel, "Verification completed")


async def begin_verification(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            view=styled_view(
                "Panel unavailable",
                "This panel can only be used in a server.",
            ),
            ephemeral=True,
        )
        return
    existing = discord.utils.get(
        guild.text_channels,
        name=f"verify-{interaction.user.name.lower().replace(' ', '-')[:20]}",
    )
    if existing:
        await interaction.response.send_message(
            view=styled_view(
                "Verification ticket already open",
                f"You already have an open verification ticket: {existing.mention}",
            ),
            ephemeral=True,
        )
        return
    config = config_for(guild.id)
    category = (
        guild.get_channel(config["ticket_category_id"])
        if config["ticket_category_id"]
        else None
    )
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True
        ),
    }
    for role in verification_escalation_roles(guild, config):
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )
    channel = await guild.create_text_channel(
        f"verify-{interaction.user.name.lower().replace(' ', '-')[:20]}",
        category=category if isinstance(category, discord.CategoryChannel) else None,
        overwrites=overwrites,
        reason="Verification ticket created",
    )
    await interaction.response.send_modal(VerificationModal(channel))


class BeginVerificationButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Verify",
            style=discord.ButtonStyle.primary,
            custom_id="moderation:begin-verification",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await begin_verification(interaction)


class VerificationPanel(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        add_styled_content(self, "Verify")
        self.add_item(
            discord.ui.ActionRow(BeginVerificationButton())
        )


class WhitelistGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="whitelist", description="Manage staff access")

    @app_commands.command(name="add", description="Add a staff member or owner")
    @app_commands.describe(user="Discord user", role="staff or owner")
    @owner_check()
    async def add(self, interaction: discord.Interaction, user: discord.User, role: str):
        role = role.lower().strip()
        caller = get_role(interaction.user.id)
        if role not in ("staff", "owner"):
            await interaction.response.send_message(
                view=styled_view("Invalid role", "Role must be `staff` or `owner`."),
                ephemeral=True,
            )
            return
        if ROLE_RANK.get(caller, 0) <= ROLE_RANK[role] and caller != "founder":
            await interaction.response.send_message(
                view=styled_view(
                    "Permission denied",
                    "You can only assign roles below your own.",
                ),
                ephemeral=True,
            )
            return
        with connect() as db:
            db.execute(
                """
                INSERT INTO whitelist (user_id, role) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET role = excluded.role
                """,
                (user.id, role),
            )
        await interaction.response.send_message(
            view=styled_view(
                "Whitelist updated",
                f"{user.mention} is now recognized as **{role}**.",
            )
        )

    @app_commands.command(name="remove", description="Remove a staff member or owner")
    @app_commands.describe(user="Discord user")
    @owner_check()
    async def remove(self, interaction: discord.Interaction, user: discord.User):
        if user.id in FOUNDER_IDS:
            await interaction.response.send_message(
                view=styled_view(
                    "Permission denied",
                    "Founder access cannot be removed.",
                ),
                ephemeral=True,
            )
            return
        with connect() as db:
            db.execute("DELETE FROM whitelist WHERE user_id = ?", (user.id,))
        await interaction.response.send_message(
            view=styled_view(
                "Whitelist updated",
                f"{user.mention} no longer has staff access.",
            )
        )

    @app_commands.command(name="list", description="List recognized staff members")
    @owner_check()
    async def list(self, interaction: discord.Interaction):
        with connect() as db:
            rows = db.execute(
                "SELECT user_id, role FROM whitelist ORDER BY role DESC, user_id"
            ).fetchall()
        lines = [f"<@{uid}> — founder" for uid in sorted(FOUNDER_IDS)]
        lines += [f"<@{row['user_id']}> — {row['role']}" for row in rows]
        await interaction.response.send_message(
            view=styled_view(
                "Recognized staff",
                "\n".join(lines) if lines else "No staff members are configured.",
            ),
            ephemeral=True,
        )


class StrikeGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="strike", description="Review and manage member strikes")

    @app_commands.command(name="add", description="Issue the next active strike")
    @app_commands.describe(
        user="Member receiving the strike",
        reason="Professional reason for the strike",
        proof="Proof link or concise proof reference",
    )
    @staff_check()
    async def add(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str,
        proof: str = "No proof provided",
    ):
        assert interaction.guild is not None
        if protected_from_moderation(user.id):
            await interaction.response.send_message(
                view=styled_view(
                    "Strike safeguard",
                    "Protected staff and founder accounts cannot receive strikes.",
                ),
                ephemeral=True,
            )
            return
        current = active_strikes(interaction.guild.id, user.id)
        if len(current) >= 2:
            await interaction.response.send_message(
                view=styled_view(
                    "Strike limit reached",
                    "This member already has two active strikes. Use `/strike3` to record the final strike and consequence.",
                ),
                ephemeral=True,
            )
            return
        number = len(current) + 1
        with connect() as db:
            cursor = db.execute(
                """
                INSERT INTO strikes (
                    guild_id, user_id, moderator_id, strike_number,
                    reason, proof, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (
                    interaction.guild.id,
                    user.id,
                    interaction.user.id,
                    number,
                    reason,
                    proof,
                    now_iso(),
                ),
            )
            strike = db.execute(
                "SELECT * FROM strikes WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        await publish_strike(interaction, strike, number)
        await audit_log(
            interaction.guild,
            "strike",
            f"{interaction.user} issued strike {number}/3 to {user}.",
            details=[
                ("Reason", reason[:1024]),
                ("Proof", proof_text(proof)[:1024]),
            ],
            actor_id=interaction.user.id,
            subject_id=user.id,
        )
        await interaction.response.send_message(
            view=styled_view(
                f"Strike {number}/3 recorded",
                f"{user.mention} now has **{number}/3** active strikes.",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="status", description="Show active strikes")
    @app_commands.describe(user="Member to review")
    @staff_check()
    async def status(self, interaction: discord.Interaction, user: discord.Member):
        assert interaction.guild is not None
        rows = active_strikes(interaction.guild.id, user.id)
        if not rows:
            description = f"{user.mention} has no active strikes."
        else:
            description = "\n".join(
                f"**{row['strike_number']}/3** — {row['reason']}" for row in rows
            )
        await interaction.response.send_message(
            view=styled_view(f"Active strikes • {user}", description),
            ephemeral=True,
        )

    @app_commands.command(name="history", description="Show the complete strike history")
    @app_commands.describe(user="Member to review")
    @staff_check()
    async def history(self, interaction: discord.Interaction, user: discord.Member):
        assert interaction.guild is not None
        with connect() as db:
            rows = db.execute(
                """
                SELECT * FROM strikes
                WHERE guild_id = ? AND user_id = ? ORDER BY id DESC
                """,
                (interaction.guild.id, user.id),
            ).fetchall()
        if not rows:
            description = "No strike records exist for this member."
        else:
            description = "\n".join(
                f"**{row['strike_number']}/3** • `{row['status']}` • {row['reason']}"
                for row in rows[:15]
            )
        await interaction.response.send_message(
            view=styled_view(f"Strike history • {user}", description),
            ephemeral=True,
        )

    @app_commands.command(name="proof", description="Show the latest stored proof")
    @app_commands.describe(user="Member to review")
    @staff_check()
    async def proof(self, interaction: discord.Interaction, user: discord.Member):
        assert interaction.guild is not None
        with connect() as db:
            row = db.execute(
                """
                SELECT * FROM strikes
                WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1
                """,
                (interaction.guild.id, user.id),
            ).fetchone()
        await interaction.response.send_message(
            view=styled_view(
                f"Latest proof • {user}",
                proof_text(row["proof"]) if row else "No stored proof exists.",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="expire", description="Mark the latest active strike expired")
    @app_commands.describe(user="Member whose strike should expire")
    @staff_check()
    async def expire(self, interaction: discord.Interaction, user: discord.Member):
        assert interaction.guild is not None
        with connect() as db:
            row = db.execute(
                """
                SELECT id FROM strikes
                WHERE guild_id = ? AND user_id = ? AND status = 'active'
                ORDER BY id DESC LIMIT 1
                """,
                (interaction.guild.id, user.id),
            ).fetchone()
            if not row:
                await interaction.response.send_message(
                    view=styled_view(
                        "No active strikes",
                        "That member has no active strikes.",
                    ),
                    ephemeral=True,
                )
                return
            db.execute(
                "UPDATE strikes SET status = 'expired', revoked_at = ?, revoked_by = ? WHERE id = ?",
                (now_iso(), interaction.user.id, row["id"]),
            )
        await interaction.response.send_message(
            view=styled_view(
                "Strike expired",
                f"The latest strike for {user.mention} is now expired.",
            )
        )

    @app_commands.command(name="boost-forgive", description="Forgive the latest strike through a boost benefit")
    @app_commands.describe(user="Member whose latest active strike should be forgiven")
    @staff_check()
    async def boost_forgive(
        self, interaction: discord.Interaction, user: discord.Member
    ):
        assert interaction.guild is not None
        with connect() as db:
            row = db.execute(
                """
                SELECT id FROM strikes
                WHERE guild_id = ? AND user_id = ? AND status = 'active'
                ORDER BY id DESC LIMIT 1
                """,
                (interaction.guild.id, user.id),
            ).fetchone()
            if not row:
                await interaction.response.send_message(
                    view=styled_view(
                        "No active strikes",
                        "That member has no active strikes.",
                    ),
                    ephemeral=True,
                )
                return
            db.execute(
                "UPDATE strikes SET status = 'boost_forgiven', revoked_at = ?, revoked_by = ? WHERE id = ?",
                (now_iso(), interaction.user.id, row["id"]),
            )
        await interaction.response.send_message(
            view=styled_view(
                "Strike forgiven",
                f"The latest strike for {user.mention} has been forgiven under the boost policy.",
            )
        )

class SnipeGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="snipe", description="Track Roblox game servers")

    @app_commands.command(name="add", description="Track a Roblox username")
    @app_commands.describe(roblox_username="Roblox username to track")
    @staff_check()
    async def add(self, interaction: discord.Interaction, roblox_username: str):
        assert interaction.guild is not None
        try:
            async with aiohttp.ClientSession() as session:
                user = await fetch_roblox_user(session, roblox_username)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            user = None
        if not user:
            await interaction.response.send_message(
                view=styled_view(
                    "Roblox account not found",
                    "That Roblox account could not be found.",
                ),
                ephemeral=True,
            )
            return
        with connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO snipe_targets (
                    guild_id, roblox_user_id, roblox_username, added_by
                ) VALUES (?, ?, ?, ?)
                """,
                (interaction.guild.id, user["id"], user["name"], interaction.user.id),
            )
            db.execute(
                """
                INSERT OR IGNORE INTO snipe_presence (
                    guild_id, roblox_user_id, in_game
                ) VALUES (?, ?, 0)
                """,
                (interaction.guild.id, user["id"]),
            )
        await interaction.response.send_message(
            view=styled_view(
                "Roblox tracking enabled",
                f"Now tracking **{user['name']}**. You will be notified when they enter a new server or rejoin a previous one.",
            )
        )

    @app_commands.command(name="remove", description="Stop tracking a Roblox username")
    @app_commands.describe(roblox_username="Roblox username to stop tracking")
    @staff_check()
    async def remove(self, interaction: discord.Interaction, roblox_username: str):
        assert interaction.guild is not None
        with connect() as db:
            deleted = db.execute(
                """
                DELETE FROM snipe_targets
                WHERE guild_id = ? AND lower(roblox_username) = lower(?)
                """,
                (interaction.guild.id, roblox_username),
            ).rowcount
        await interaction.response.send_message(
            view=styled_view(
                "Roblox tracking updated",
                "Tracking has been removed." if deleted else "That username was not being tracked.",
            ),
            ephemeral=True,
        )


class RaidPointGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="raid-point", description="Manage raid leaderboard points")

    @app_commands.command(name="add", description="Add 1 to 20 raid points")
    @app_commands.describe(user="Member receiving points", amount="Amount from 1 to 20")
    @staff_check()
    async def add(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 20],
    ):
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        with connect() as db:
            db.execute(
                """
                INSERT INTO raid_points (guild_id, user_id, points, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    points = raid_points.points + excluded.points,
                    updated_at = excluded.updated_at
                """,
                (interaction.guild.id, user.id, amount, now_iso()),
            )
            row = db.execute(
                "SELECT points FROM raid_points WHERE guild_id = ? AND user_id = ?",
                (interaction.guild.id, user.id),
            ).fetchone()
        granted, removed, errors = await sync_point_promotion_roles(
            interaction.guild, user, row["points"]
        )
        details = [("New total", f"{row['points']} pts")]
        if granted:
            details.append(("Promoted", ", ".join(granted)))
        if removed:
            details.append(("Promotion roles removed", ", ".join(removed)))
        if errors:
            details.append(("Role update issue", errors[0]))
        await refresh_raid_leaderboard(interaction.guild, interaction.channel)
        await interaction.followup.send(
            view=styled_view(
                "Raid points added",
                f"Added **{amount}** point(s) to {user.mention}.",
                details=details,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="remove", description="Remove 1 to 20 raid points")
    @app_commands.describe(user="Member losing points", amount="Amount from 1 to 20")
    @staff_check()
    async def remove(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 20],
    ):
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        with connect() as db:
            db.execute(
                """
                INSERT INTO raid_points (guild_id, user_id, points, updated_at)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(guild_id, user_id) DO NOTHING
                """,
                (interaction.guild.id, user.id, now_iso()),
            )
            db.execute(
                """
                UPDATE raid_points
                SET points = MAX(0, points - ?), updated_at = ?
                WHERE guild_id = ? AND user_id = ?
                """,
                (amount, now_iso(), interaction.guild.id, user.id),
            )
            row = db.execute(
                "SELECT points FROM raid_points WHERE guild_id = ? AND user_id = ?",
                (interaction.guild.id, user.id),
            ).fetchone()
        granted, removed, errors = await sync_point_promotion_roles(
            interaction.guild, user, row["points"]
        )
        details = [("New total", f"{row['points']} pts")]
        if granted:
            details.append(("Promotion roles granted", ", ".join(granted)))
        if removed:
            details.append(("Demoted", ", ".join(removed)))
        if errors:
            details.append(("Role update issue", errors[0]))
        await refresh_raid_leaderboard(interaction.guild, interaction.channel)
        await interaction.followup.send(
            view=styled_view(
                "Raid points removed",
                f"Removed **{amount}** point(s) from {user.mention}.",
                details=details,
            ),
            ephemeral=True,
        )


class ModerationBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.messages = True
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.recent_joins: defaultdict[int, deque[datetime]] = defaultdict(deque)
        self.raid_alerted_until: dict[int, datetime] = {}

    async def setup_hook(self):
        init_db()
        self.add_view(VerificationPanel())
        self.poll_snipes.start()

    @tasks.loop(seconds=60)
    async def poll_snipes(self):
        timeout = aiohttp.ClientTimeout(total=25)
        with connect() as db:
            targets = db.execute("SELECT * FROM snipe_targets").fetchall()
        if not targets:
            return
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for target in targets:
                try:
                    async with session.post(
                        "https://presence.roblox.com/v1/presence/users",
                        json={"userIds": [target["roblox_user_id"]]},
                    ) as response:
                        response.raise_for_status()
                        presence = (await response.json()).get("userPresences", [{}])[0]
                    in_game = presence.get("userPresenceType") == 2
                    server_id = presence.get("gameId") if in_game else None
                    place_id = presence.get("placeId") if in_game else None
                    with connect() as db:
                        previous = db.execute(
                            """
                            SELECT * FROM snipe_presence
                            WHERE guild_id = ? AND roblox_user_id = ?
                            """,
                            (target["guild_id"], target["roblox_user_id"]),
                        ).fetchone()
                        should_notify = bool(
                            in_game
                            and (
                                previous is None
                                or not previous["in_game"]
                                or previous["last_server_id"] != server_id
                            )
                        )
                        db.execute(
                            """
                            INSERT INTO snipe_presence (
                                guild_id, roblox_user_id, last_server_id,
                                last_place_id, in_game
                            ) VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(guild_id, roblox_user_id) DO UPDATE SET
                                last_server_id = excluded.last_server_id,
                                last_place_id = excluded.last_place_id,
                                in_game = excluded.in_game
                            """,
                            (
                                target["guild_id"],
                                target["roblox_user_id"],
                                server_id,
                                place_id,
                                int(in_game),
                            ),
                        )
                    if not should_notify:
                        continue
                    guild = self.get_guild(target["guild_id"])
                    if guild is None:
                        continue
                    config = config_for(guild.id)
                    channel_id = config["snipe_channel_id"]
                    channel = guild.get_channel(channel_id) if channel_id else None
                    if channel is None:
                        continue
                    game_name = "Roblox experience"
                    if place_id:
                        try:
                            async with session.get(
                                f"https://apis.roblox.com/universes/v1/places/{place_id}/universe"
                            ) as universe_response:
                                universe_response.raise_for_status()
                                universe_id = (await universe_response.json()).get("universeId")
                            if universe_id:
                                async with session.get(
                                    "https://games.roblox.com/v1/games",
                                    params={"universeIds": universe_id},
                                ) as game_response:
                                    game_response.raise_for_status()
                                    games = (await game_response.json()).get("data", [])
                                    if games:
                                        game_name = games[0].get("name", game_name)
                        except (aiohttp.ClientError, asyncio.TimeoutError):
                            pass
                    join_url = (
                        f"https://www.roblox.com/games/start?placeId={place_id}"
                        f"&gameInstanceId={server_id}"
                    )
                    tracker = discord.Object(id=target["added_by"])
                    try:
                        avatar_url = await fetch_roblox_avatar(
                            session, target["roblox_user_id"]
                        )
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        avatar_url = ""
                    notice = styled_view(
                        f"{target['roblox_username']} is in a game",
                        f"<@{tracker.id}>\n[Join the exact server]({join_url})",
                        details=[
                            ("Experience", game_name),
                            ("Place ID", f"`{place_id}`"),
                            ("Server ID", f"`{server_id}`"),
                        ],
                        thumbnail=avatar_url,
                    )
                    await channel.send(
                        view=notice,
                        allowed_mentions=discord.AllowedMentions(users=[tracker]),
                    )
                except discord.HTTPException as error:
                    print(
                        f"Could not send snipe alert for guild "
                        f"{target['guild_id']}: {error}"
                    )
                    continue
                except (aiohttp.ClientError, asyncio.TimeoutError, KeyError):
                    continue

    @poll_snipes.before_loop
    async def before_poll_snipes(self):
        await self.wait_until_ready()


bot = ModerationBot()
tree = bot.tree
tree.add_command(WhitelistGroup())
tree.add_command(StrikeGroup())
tree.add_command(SnipeGroup())
tree.add_command(RaidPointGroup())


@tree.command(name="pset", description="Set the point threshold for a promotion role")
@app_commands.describe(
    role="Role awarded when a member reaches the point threshold",
    points_needed="Points needed to receive the role (1 to 100)",
)
@staff_check()
async def pset(
    interaction: discord.Interaction,
    role: discord.Role,
    points_needed: app_commands.Range[int, 1, 100],
):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            view=styled_view("Server only", "Use this command inside a server."),
            ephemeral=True,
        )
        return
    bot_member = guild.me
    if role == guild.default_role or role.managed:
        await interaction.response.send_message(
            view=styled_view(
                "Role unavailable",
                "Choose a regular server role, not @everyone or an integration-managed role.",
            ),
            ephemeral=True,
        )
        return
    if bot_member is None or not bot_member.guild_permissions.manage_roles:
        await interaction.response.send_message(
            view=styled_view(
                "Role setup issue",
                "The bot needs the Manage Roles permission before it can promote members.",
            ),
            ephemeral=True,
        )
        return
    if role.position >= bot_member.top_role.position:
        await interaction.response.send_message(
            view=styled_view(
                "Role hierarchy issue",
                "Move the bot's highest role above the selected promotion role, then run `/pset` again.",
            ),
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    with connect() as db:
        db.execute(
            """
            INSERT INTO point_promotions (guild_id, role_id, points_needed)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, role_id) DO UPDATE SET
                points_needed = excluded.points_needed
            """,
            (guild.id, role.id, points_needed),
        )
        tracked_members = db.execute(
            """
            SELECT user_id, points FROM raid_points
            WHERE guild_id = ?
            """,
            (guild.id,),
        ).fetchall()

    checked = 0
    granted_count = 0
    removed_count = 0
    sync_errors: list[str] = []
    for record in tracked_members:
        member = guild.get_member(record["user_id"])
        if member is None:
            continue
        checked += 1
        granted, removed, errors = await sync_point_promotion_roles(
            guild, member, record["points"]
        )
        granted_count += len(granted)
        removed_count += len(removed)
        sync_errors.extend(errors)

    details = [
        ("Threshold", f"{points_needed} points"),
        ("Tracked members checked", str(checked)),
    ]
    if granted_count:
        details.append(("Roles granted", str(granted_count)))
    if removed_count:
        details.append(("Roles removed", str(removed_count)))
    if sync_errors:
        details.append(("Role update issue", sync_errors[0]))
    await interaction.followup.send(
        view=styled_view(
            "Promotion threshold saved",
            f"Members will receive **{role.name}** at **{points_needed}** raid points.",
            details=details,
        ),
        ephemeral=True,
    )


@tree.command(name="z", description="Export all bot data as a backup ZIP")
@owner_check()
async def z(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            view=styled_view("Server only", "Use this command inside a server."),
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    try:
        config_for(interaction.guild.id)
        archive_bytes, counts = await asyncio.to_thread(
            create_database_backup_archive
        )
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"Could not create bot data backup: {error!r}")
        await interaction.followup.send(
            view=styled_view(
                "Backup failed",
                "I could not create the database snapshot. Check the bot logs and available disk space.",
            ),
            ephemeral=True,
        )
        return
    if len(archive_bytes) > MAX_BACKUP_UPLOAD_BYTES:
        await interaction.followup.send(
            view=styled_view(
                "Backup too large",
                f"The backup is {len(archive_bytes) / 1024 / 1024:.1f} MB, above the bot's {MAX_BACKUP_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.",
            ),
            ephemeral=True,
        )
        return
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    file = discord.File(
        io.BytesIO(archive_bytes),
        filename=f"bot-data-backup-{timestamp}.zip",
    )
    try:
        await interaction.followup.send(
            content=(
                f"Complete bot data backup. It contains **{counts['roblox_links']}** registered Roblox links and the rest of the database records."
            ),
            file=file,
            ephemeral=True,
        )
    except discord.HTTPException as error:
        print(f"Could not send bot data backup: {error!r}")
        await interaction.followup.send(
            view=styled_view(
                "Backup upload failed",
                "Discord could not send the ZIP. Check the file-size limit for this server.",
            ),
            ephemeral=True,
        )


@tree.command(name="r", description="Restore bot data from a `/z` backup ZIP")
@app_commands.describe(backup_file="ZIP file created by `/z`")
@owner_check()
async def r(interaction: discord.Interaction, backup_file: discord.Attachment):
    if interaction.guild is None:
        await interaction.response.send_message(
            view=styled_view("Server only", "Use this command inside a server."),
            ephemeral=True,
        )
        return
    if not backup_file.filename.lower().endswith(".zip"):
        await interaction.response.send_message(
            view=styled_view(
                "ZIP file required",
                "Upload the ZIP file created by `/z`.",
            ),
            ephemeral=True,
        )
        return
    if backup_file.size > MAX_BACKUP_UPLOAD_BYTES:
        await interaction.response.send_message(
            view=styled_view(
                "Backup too large",
                f"The backup must be no larger than {MAX_BACKUP_UPLOAD_BYTES // (1024 * 1024)} MB.",
            ),
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    try:
        backup_data = await backup_file.read()
        staged_path, counts = await asyncio.to_thread(
            prepare_database_restore, backup_data
        )
    except (
        discord.HTTPException,
        zipfile.BadZipFile,
        json.JSONDecodeError,
        ValueError,
        OSError,
        sqlite3.DatabaseError,
        RuntimeError,
    ) as error:
        print(f"Could not validate uploaded bot backup: {error!r}")
        await interaction.followup.send(
            view=styled_view(
                "Backup could not be restored",
                "Upload an unmodified ZIP created by `/z` from this bot version.",
            ),
            ephemeral=True,
        )
        return

    summary = (
        "**Restore preview**\n"
        f"> Staff whitelist records: {counts['whitelist']}\n"
        f"> Hardban records: {counts['hardbans']}\n"
        f"> Server configurations: {counts['guild_config']}\n"
        f"> Strike records: {counts['strikes']}\n"
        f"> Registered Roblox links: {counts['roblox_links']}\n"
        f"> Roblox presence targets: {counts['snipe_targets']}\n"
        f"> Presence records: {counts['snipe_presence']}\n"
        f"> Raid point records: {counts['raid_points']}\n"
        f"> Point promotion rules: {counts['point_promotions']}\n"
        f"> Moderation records: {counts['moderation_events']}\n\n"
        "Confirming replaces the bot's current database with this backup. "
        "A local safety copy of the current database will be kept first."
    )
    view = RestoreConfirmationView(
        interaction.user.id,
        staged_path,
        counts,
    )
    try:
        await interaction.followup.send(
            content=summary,
            view=view,
            ephemeral=True,
        )
    except discord.HTTPException:
        view.discard_staged_file()
        raise


TAG_CHOICES = [app_commands.Choice(name=tag, value=tag) for tag in TAG_NAMES]


class TagManagerGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="tag-manager", description="Manage the server tag roles")

    @app_commands.command(
        name="grant", description="Give a member the Tag Manager role"
    )
    @app_commands.describe(user="Member who should receive Tag Manager access")
    @founder_check()
    async def grant(self, interaction: discord.Interaction, user: discord.Member):
        assert interaction.guild is not None
        role = tag_manager_role(interaction.guild)
        if role is None:
            await interaction.response.send_message(
                view=styled_view(
                    "Tag Manager role not found",
                    f"Create a role named **{TAG_MANAGER_ROLE_NAME}** before granting access.",
                ),
                ephemeral=True,
            )
            return
        if interaction.guild.me and role >= interaction.guild.me.top_role:
            await interaction.response.send_message(
                view=styled_view(
                    "Tag Manager role is too high",
                    "Move the bot's role above the Tag Manager role before trying again.",
                ),
                ephemeral=True,
            )
            return
        try:
            await user.add_roles(role, reason=f"Tag Manager granted by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                view=styled_view(
                    "Tag Manager update failed",
                    "Discord rejected the role change. Check the bot role position and permissions.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            view=styled_view(
                "Tag Manager access granted",
                f"{role.mention} was added to {user.mention}.",
            )
        )
        await audit_log(
            interaction.guild,
            "tag-manager",
            f"{interaction.user} granted Tag Manager access to {user}.",
            actor_id=interaction.user.id,
            subject_id=user.id,
        )

    @app_commands.command(
        name="revoke", description="Remove the Tag Manager role from a member"
    )
    @app_commands.describe(user="Member losing Tag Manager access")
    @founder_check()
    async def revoke(self, interaction: discord.Interaction, user: discord.Member):
        assert interaction.guild is not None
        role = tag_manager_role(interaction.guild)
        if role is None:
            await interaction.response.send_message(
                view=styled_view(
                    "Tag Manager role not found",
                    f"Create a role named **{TAG_MANAGER_ROLE_NAME}** before changing access.",
                ),
                ephemeral=True,
            )
            return
        try:
            await user.remove_roles(role, reason=f"Tag Manager revoked by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                view=styled_view(
                    "Tag Manager update failed",
                    "Discord rejected the role change. Check the bot role position and permissions.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            view=styled_view(
                "Tag Manager access revoked",
                f"{role.mention} was removed from {user.mention}.",
            )
        )
        await audit_log(
            interaction.guild,
            "tag-manager",
            f"{interaction.user} revoked Tag Manager access from {user}.",
            actor_id=interaction.user.id,
            subject_id=user.id,
        )

    @app_commands.command(name="add", description="Give a member one of the available tags")
    @app_commands.describe(user="Member receiving the tag", tag="Tag role to add")
    @app_commands.choices(tag=TAG_CHOICES)
    @tag_manager_check()
    async def add(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        tag: app_commands.Choice[str],
    ):
        assert interaction.guild is not None
        role = find_named_role(interaction.guild, tag.value)
        if role is None:
            await interaction.response.send_message(
                view=styled_view(
                    "Tag role not found",
                    f"Create a role named `{tag.value}` before using the tag manager.",
                ),
                ephemeral=True,
            )
            return
        if interaction.guild.me and role >= interaction.guild.me.top_role:
            await interaction.response.send_message(
                view=styled_view(
                    "Tag role is too high",
                    "Move the bot's role above the tag roles before trying again.",
                ),
                ephemeral=True,
            )
            return
        try:
            await user.add_roles(role, reason=f"Tag added by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                view=styled_view(
                    "Tag update failed",
                    "Discord rejected the role change. Check the bot role position and permissions.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            view=styled_view("Tag added", f"{role.mention} was added to {user.mention}.")
        )
        await audit_log(
            interaction.guild,
            "tag",
            f"{interaction.user} added {role.name} to {user}.",
            actor_id=interaction.user.id,
            subject_id=user.id,
        )

    @app_commands.command(name="remove", description="Remove one of the available tags")
    @app_commands.describe(user="Member losing the tag", tag="Tag role to remove")
    @app_commands.choices(tag=TAG_CHOICES)
    @tag_manager_check()
    async def remove(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        tag: app_commands.Choice[str],
    ):
        assert interaction.guild is not None
        role = find_named_role(interaction.guild, tag.value)
        if role is None:
            await interaction.response.send_message(
                view=styled_view(
                    "Tag role not found",
                    f"Create a role named `{tag.value}` before using the tag manager.",
                ),
                ephemeral=True,
            )
            return
        try:
            await user.remove_roles(role, reason=f"Tag removed by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                view=styled_view(
                    "Tag update failed",
                    "Discord rejected the role change. Check the bot role position and permissions.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            view=styled_view("Tag removed", f"{role.mention} was removed from {user.mention}.")
        )
        await audit_log(
            interaction.guild,
            "tag",
            f"{interaction.user} removed {role.name} from {user}.",
            actor_id=interaction.user.id,
            subject_id=user.id,
        )


async def fetch_tag_group_description() -> tuple[str, str, str]:
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            group = await fetch_group_metadata(session, ROBLOX_GROUP_ID)
            icon_url = group.get("iconUrl", "")
            if not icon_url:
                try:
                    icon_url = await fetch_group_icon(session, ROBLOX_GROUP_ID)
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    icon_url = ""
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return (
            f"Roblox community {ROBLOX_GROUP_ID}",
            "The Roblox community description could not be loaded right now.",
            "",
        )
    return (
        group.get("name", f"Roblox community {ROBLOX_GROUP_ID}"),
        group.get("description") or "This community has not added a description yet.",
        icon_url,
    )


@tree.command(
    name="tag",
    description="Give a selected community tag to a member",
)
@app_commands.describe(
    tag="Choose one of the configured community tags",
    username="Discord member whose linked Roblox account receives the role",
)
@app_commands.choices(tag=TAG_CHOICES)
@tag_manager_check()
async def tag_command(
    interaction: discord.Interaction,
    tag: app_commands.Choice[str],
    username: discord.Member,
):
    assert interaction.guild is not None
    await interaction.response.defer()
    link = roblox_link_for(interaction.guild.id, username.id)
    if link is None:
        await interaction.followup.send(
            view=styled_view(
                "Roblox account not linked",
                f"{username.mention} must complete Roblox verification before receiving a group role.",
            ),
            ephemeral=True,
        )
        return

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20)
        ) as session:
            assignment = await assign_roblox_group_role(
                session,
                ROBLOX_GROUP_ID,
                int(link["roblox_user_id"]),
                tag.value,
            )
    except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError) as error:
        await interaction.followup.send(
            view=styled_view(
                "Roblox role assignment failed",
                str(error) or "Roblox did not respond in time. Try again shortly.",
            ),
            ephemeral=True,
        )
        return
    except LookupError as error:
        await interaction.followup.send(
            view=styled_view(
                "Roblox role not found",
                str(error),
            ),
            ephemeral=True,
        )
        return

    group_name, group_description, group_icon = await fetch_tag_group_description()
    await interaction.followup.send(
        view=styled_view(
            f"{assignment['role_name']} role assigned",
            f"Roblox role **{assignment['role_name']}** was assigned to the linked Roblox account for {username.mention}.",
            details=[
                ("Community", group_name),
                ("Community description", group_description[:1024]),
                ("Roblox account", f"{link['roblox_username']} (`{link['roblox_user_id']}`)"),
                ("Assigned by", interaction.user.mention),
            ],
            thumbnail=group_icon,
        )
    )
    await audit_log(
        interaction.guild,
        "tag",
        f"{interaction.user} assigned the Roblox {assignment['role_name']} role to {username}.",
        details=[
            ("Community", group_name),
            ("Community description", group_description[:1024]),
            ("Roblox account", link["roblox_username"]),
        ],
        actor_id=interaction.user.id,
        subject_id=username.id,
    )


async def tagwipe_impl(interaction: discord.Interaction, confirmation: str) -> None:
    assert interaction.guild is not None
    roles = tag_roles(interaction.guild)
    config = config_for(interaction.guild.id)
    member_default = member_role(interaction.guild, config)
    try:
        members = [
            member async for member in interaction.guild.fetch_members(limit=None)
        ]
    except (discord.Forbidden, discord.HTTPException):
        members = list(interaction.guild.members)
    targets = [
        member
        for member in members
        if not member.bot and not protected_from_moderation(member.id)
        and any(role in member.roles for role in roles)
    ]
    if confirmation.strip().upper() != "CONFIRM":
        await interaction.response.send_message(
            view=styled_view(
                "Tag wipe preview",
                f"This would update **{len(targets)}** member(s). It removes only the supported tag roles and then adds the Members role when configured.",
                details=[
                    ("Protected", "Staff, founders, and non-tag roles are left alone."),
                    ("To apply", "Run the command again with confirmation `CONFIRM`."),
                ],
            ),
            ephemeral=True,
        )
        return
    if not roles:
        await interaction.response.send_message(
            view=styled_view("Tag wipe skipped", "No supported tag roles exist."),
            ephemeral=True,
        )
        return
    changed = 0
    failed = 0
    bot_top = interaction.guild.me.top_role if interaction.guild.me else None
    for member in targets:
        removable = [
            role
            for role in member.roles
            if role in roles and (bot_top is None or role < bot_top)
        ]
        try:
            if removable:
                await member.remove_roles(*removable, reason="Tag wipe")
            if (
                member_default
                and member_default not in member.roles
                and (bot_top is None or member_default < bot_top)
            ):
                await member.add_roles(member_default, reason="Tag wipe")
            changed += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1
    await interaction.response.send_message(
        view=styled_view(
            "Tag wipe completed",
            f"Updated **{changed}** member(s); **{failed}** member(s) could not be updated.",
            details=[
                ("Removed", ", ".join(role.name for role in roles)),
                ("Default role", member_default.name if member_default else "Not configured"),
            ],
        ),
        ephemeral=True,
    )
    await audit_log(
        interaction.guild,
        "tagwipe",
        f"{interaction.user} completed a tag wipe.",
        details=[("Updated", str(changed)), ("Failed", str(failed))],
        actor_id=interaction.user.id,
    )


@tree.command(name="tagwipe", description="Remove supported tags from members")
@app_commands.describe(
    confirmation="Use CONFIRM to apply; any other value shows a preview"
)
@owner_check()
async def tagwipe(interaction: discord.Interaction, confirmation: str = "PREVIEW"):
    await tagwipe_impl(interaction, confirmation)


@tree.command(name="strip", description="Reset supported tags to the Members role")
@app_commands.describe(
    confirmation="Use CONFIRM to apply; any other value shows a preview"
)
@owner_check()
async def strip(interaction: discord.Interaction, confirmation: str = "PREVIEW"):
    await tagwipe_impl(interaction, confirmation)


for removed_command in ("tag", "tag-manager", "tagwipe", "tag-wipe", "strip"):
    tree.remove_command(removed_command)


def parse_message_link(link: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(
        r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/"
        r"(\d+)/(\d+)/(\d+)",
        link.strip(),
    )
    if not match:
        return None
    return tuple(int(value) for value in match.groups())


@tree.command(
    name="kactivity",
    description="Kick members with a role who did not react to a message",
)
@app_commands.describe(
    message_link="Discord message link to check",
    role="Members with this role are checked",
    confirmation="Use CONFIRM to apply; any other value shows a preview",
)
@owner_check()
async def kactivity(
    interaction: discord.Interaction,
    message_link: str,
    role: discord.Role,
    confirmation: str = "PREVIEW",
):
    assert interaction.guild is not None
    parsed = parse_message_link(message_link)
    if parsed is None or parsed[0] != interaction.guild.id:
        await interaction.response.send_message(
            view=styled_view(
                "Invalid message link",
                "Use a message link from this server in the format copied by Discord.",
            ),
            ephemeral=True,
        )
        return
    _, channel_id, message_id = parsed
    channel = interaction.guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(
            view=styled_view("Message unavailable", "I could not access that text channel."),
            ephemeral=True,
        )
        return
    try:
        message = await channel.fetch_message(message_id)
        reacted_ids: set[int] = set()
        for reaction in message.reactions:
            async for reactor in reaction.users(limit=None):
                reacted_ids.add(reactor.id)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        await interaction.response.send_message(
            view=styled_view(
                "Activity check failed",
                "I could not read that message or its reactions.",
            ),
            ephemeral=True,
        )
        return
    members = [
        member
        for member in interaction.guild.members
        if role in member.roles
        and not member.bot
        and member.id not in reacted_ids
        and member.premium_since is None
        and not protected_from_moderation(member.id)
    ]
    if confirmation.strip().upper() != "CONFIRM":
        await interaction.response.send_message(
            view=styled_view(
                "Activity kick preview",
                f"**{len(members)}** member(s) with {role.mention} did not react.",
                details=[
                    ("Boosters", "Always excluded."),
                    ("Protected staff", "Always excluded."),
                    ("To apply", "Run again with confirmation `CONFIRM`."),
                ],
            ),
            ephemeral=True,
        )
        return
    if not interaction.guild.me or not interaction.guild.me.guild_permissions.kick_members:
        await interaction.response.send_message(
            view=styled_view(
                "Kick unavailable",
                "The bot needs the Kick Members permission.",
            ),
            ephemeral=True,
        )
        return
    kicked = 0
    failed = 0
    for member in members:
        if member.top_role >= interaction.guild.me.top_role:
            failed += 1
            continue
        try:
            await member.kick(reason=f"Did not react to {message.id}")
            kicked += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1
    await interaction.response.send_message(
        view=styled_view(
            "Activity kick completed",
            f"Kicked **{kicked}** member(s); **{failed}** member(s) could not be kicked.",
            details=[("Role checked", role.name), ("Message", message.jump_url)],
        ),
        ephemeral=True,
    )
    await audit_log(
        interaction.guild,
        "kactivity",
        f"{interaction.user} ran an activity kick for {role.name}.",
        details=[("Kicked", str(kicked)), ("Failed", str(failed)), ("Message", message.jump_url)],
        actor_id=interaction.user.id,
    )


def raid_leaderboard_view(
    rows: list[sqlite3.Row],
) -> discord.ui.LayoutView:
    if rows:
        description = "\n".join(
            f"**{index}.** <@{row['user_id']}> — **{row['points']} pts**"
            for index, row in enumerate(rows, start=1)
        )
    else:
        description = "No raid points have been recorded yet."
    return styled_view("Raid Leaderboard", description)


async def refresh_raid_leaderboard(
    guild: discord.Guild,
    channel_hint: discord.abc.Messageable | None = None,
) -> discord.Message | None:
    """Edit the live board after every point change, creating it if needed."""
    config = config_for(guild.id)
    with connect() as db:
        rows = db.execute(
            """
            SELECT user_id, points FROM raid_points
            WHERE guild_id = ? AND points > 0
            ORDER BY points DESC, updated_at ASC, user_id ASC
            LIMIT 10
            """,
            (guild.id,),
        ).fetchall()
    board = raid_leaderboard_view(rows)
    channel = (
        guild.get_channel(config["raid_leaderboard_channel_id"])
        if config["raid_leaderboard_channel_id"]
        else channel_hint
    )
    if channel is None or not hasattr(channel, "send"):
        return None

    message = None
    message_id = config.get("raid_leaderboard_message_id")
    if message_id and hasattr(channel, "fetch_message"):
        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None
    if message is not None:
        await message.edit(view=board)
        return message

    message = await channel.send(view=board)
    set_config_value(guild.id, "raid_leaderboard_channel_id", channel.id)
    set_config_value(guild.id, "raid_leaderboard_message_id", message.id)
    return message


@tree.command(name="revoke", description="Revoke a specific active strike")
@app_commands.describe(
    user="Member whose strike should be revoked",
    strike_number="Strike number to revoke (1, 2, or 3)",
)
@staff_check()
async def revoke(
    interaction: discord.Interaction,
    user: discord.Member,
    strike_number: app_commands.Range[int, 1, 3],
):
    assert interaction.guild is not None
    with connect() as db:
        strike = db.execute(
            """
            SELECT id FROM strikes
            WHERE guild_id = ? AND user_id = ? AND strike_number = ?
              AND status = 'active'
            ORDER BY id DESC LIMIT 1
            """,
            (interaction.guild.id, user.id, strike_number),
        ).fetchone()
        if not strike:
            await interaction.response.send_message(
                view=styled_view(
                    "No active strike",
                    f"{user.mention} does not have an active strike numbered {strike_number}.",
                ),
                ephemeral=True,
            )
            return
        db.execute(
            """
            UPDATE strikes SET status = 'revoked', revoked_at = ?, revoked_by = ?
            WHERE id = ?
            """,
            (now_iso(), interaction.user.id, strike["id"]),
        )
    await interaction.response.send_message(
        view=styled_view(
            "Strike revoked",
            f"{user.mention} has had strike {strike_number} revoked.",
        ),
    )


@tree.command(name="groupcheck", description="Review a Roblox user's groups")
@app_commands.describe(roblox_username="Roblox username to review")
@staff_check()
async def groupcheck(interaction: discord.Interaction, roblox_username: str):
    await interaction.response.defer()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            user = await fetch_roblox_user(session, roblox_username)
            if not user:
                await interaction.followup.send(
                    view=styled_view(
                        "Roblox account not found",
                        f"No account matched `{roblox_username}`.",
                    )
                )
                return
            groups = await group_results(session, user["id"])
            try:
                avatar_url = await fetch_roblox_avatar(session, user["id"])
            except (aiohttp.ClientError, asyncio.TimeoutError):
                avatar_url = ""
    except (aiohttp.ClientError, asyncio.TimeoutError):
        await interaction.followup.send(
            view=styled_view(
                "Roblox unavailable",
                "Roblox did not respond in time. Please try again.",
            )
        )
        return
    if not groups:
        await interaction.followup.send(
            view=styled_view(
                "No groups found",
                f"**{user['name']}** is not a member of any Roblox groups.",
            )
        )
        return
    view = GroupPageView(groups, user["name"], avatar_url)
    await interaction.followup.send(view=view)


def registered_account_embed(
    discord_user_id: int, roblox_username: str
) -> discord.Embed:
    embed = discord.Embed(title="Registered account", color=EMBED_COLOR)
    embed.description = (
        f"> **Roblox username:** {discord.utils.escape_markdown(roblox_username)}\n"
        f"> **Discord username:** <@{discord_user_id}>"
    )
    return embed


@tree.command(name="register", description="Register a member's Roblox account")
@app_commands.describe(
    discord_user="Discord username, user ID, or mention",
    roblox_username="Roblox username to link",
)
@staff_check()
async def register(
    interaction: discord.Interaction,
    discord_user: str,
    roblox_username: str,
):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            view=styled_view("Server only", "Use this command inside a server."),
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    member, lookup_error = await resolve_guild_member(guild, discord_user)
    if member is None:
        messages = {
            "ambiguous": "More than one member matches that name. Use their Discord ID or mention.",
            "unavailable": "I could not search server members. Try their Discord ID or mention.",
            "not_found": "I could not find that member in this server. Use their exact username, ID, or mention.",
        }
        await interaction.followup.send(
            view=styled_view(
                "Discord member not found",
                messages.get(lookup_error, "I could not find that member."),
            ),
            ephemeral=True,
        )
        return
    if member.bot:
        await interaction.followup.send(
            view=styled_view(
                "Registration unavailable",
                "Roblox accounts can only be registered to human server members.",
            ),
            ephemeral=True,
        )
        return

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=25)
        ) as session:
            roblox_user = await fetch_roblox_user(session, roblox_username.strip())
    except (aiohttp.ClientError, asyncio.TimeoutError):
        roblox_user = None
        await interaction.followup.send(
            view=styled_view(
                "Roblox unavailable",
                "Roblox did not respond in time. Try the registration again shortly.",
            ),
            ephemeral=True,
        )
        return
    if not roblox_user:
        await interaction.followup.send(
            view=styled_view(
                "Roblox account not found",
                f"No Roblox account matched `{discord.utils.escape_markdown(roblox_username.strip())}`.",
            ),
            ephemeral=True,
        )
        return

    saved, previous_owners = register_roblox_link(
        guild.id,
        member.id,
        roblox_user["id"],
        roblox_user["name"],
        allow_reassign=True,
    )
    if not saved:
        await interaction.followup.send(
            view=styled_view(
                "Registration failed",
                "I could not save that account link. Try again or check the bot logs.",
            ),
            ephemeral=True,
        )
        return
    await audit_log(
        guild,
        "registration",
        f"{interaction.user} registered {roblox_user['name']} to {member}.",
        details=[
            ("Discord user ID", str(member.id)),
            ("Roblox user ID", str(roblox_user["id"])),
            (
                "Previous registration reassigned",
                str(len(previous_owners)) if previous_owners else "No",
            ),
        ],
        actor_id=interaction.user.id,
        subject_id=member.id,
    )
    details = [("Roblox username", roblox_user["name"])]
    if previous_owners:
        details.append(("Previous link reassigned", str(len(previous_owners))))
    await interaction.followup.send(
        view=styled_view(
            "Registration saved",
            f"Linked **{member.display_name}** to **{roblox_user['name']}**.",
            details=details,
        ),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="info", description="Look up a registered Roblox or Discord account")
@app_commands.describe(
    account="Roblox username or Discord username, user ID, or mention"
)
async def info(interaction: discord.Interaction, account: str):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            view=styled_view("Server only", "Use this command inside a server."),
            ephemeral=True,
        )
        return
    query = account.strip()
    if not query:
        await interaction.response.send_message(
            view=styled_view("Account required", "Enter a Roblox or Discord username."),
            ephemeral=True,
        )
        return

    discord_user_id = discord_id_from_text(query)
    if discord_user_id is not None:
        link = roblox_link_for(guild.id, discord_user_id)
        if link is None:
            await interaction.response.send_message(
                view=styled_view(
                    "No registered account",
                    "That Discord user does not have a Roblox account registered in this server.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=registered_account_embed(discord_user_id, link["roblox_username"]),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    with connect() as db:
        roblox_matches = db.execute(
            """
            SELECT discord_user_id, roblox_username
            FROM roblox_links
            WHERE guild_id = ? AND roblox_username = ? COLLATE NOCASE
            """,
            (guild.id, query),
        ).fetchall()
    if len(roblox_matches) == 1:
        row = roblox_matches[0]
        await interaction.response.send_message(
            embed=registered_account_embed(
                row["discord_user_id"], row["roblox_username"]
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    if len(roblox_matches) > 1:
        await interaction.response.send_message(
            view=styled_view(
                "Registration needs review",
                "That Roblox account has multiple stored links. Ask staff to correct it with `/register`.",
            ),
            ephemeral=True,
        )
        return

    member, lookup_error = await resolve_guild_member(guild, query)
    if member is None:
        messages = {
            "ambiguous": "More than one member matches that name. Use their Discord ID or mention.",
            "unavailable": "I could not search server members. Try their Discord ID or mention.",
            "not_found": "No registered account matched that Roblox or Discord username.",
        }
        await interaction.response.send_message(
            view=styled_view(
                "Account not found",
                messages.get(lookup_error, "No registered account matched that value."),
            ),
            ephemeral=True,
        )
        return
    link = roblox_link_for(guild.id, member.id)
    if link is None:
        await interaction.response.send_message(
            view=styled_view(
                "No registered account",
                f"{member.display_name} does not have a Roblox account registered in this server.",
            ),
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        embed=registered_account_embed(member.id, link["roblox_username"]),
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="hb", description="Hardban a member")
@app_commands.describe(user="Member to hardban", reason="Reason for the hardban")
@owner_check()
async def hardban(
    interaction: discord.Interaction,
    user: discord.Member,
    reason: str = "No reason provided",
):
    assert interaction.guild is not None
    if protected_from_moderation(user.id):
        await interaction.response.send_message(
            view=styled_view(
                "Permission denied",
                "You cannot hardban another protected staff member.",
            ),
            ephemeral=True,
        )
        return
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO hardbans (user_id) VALUES (?)", (user.id,))
    try:
        await user.send(
            view=styled_view(
                f"Hardban notice • {interaction.guild.name}",
                f"You have been permanently removed from **{interaction.guild.name}**.",
            )
        )
    except (discord.Forbidden, discord.HTTPException):
        pass
    try:
        await interaction.guild.ban(user, reason=f"Hardban: {reason}", delete_message_seconds=0)
    except (discord.Forbidden, discord.HTTPException):
        with connect() as db:
            db.execute("DELETE FROM hardbans WHERE user_id = ?", (user.id,))
        await interaction.response.send_message(
            view=styled_view(
                "Hardban failed",
                "Discord rejected the ban. Check the bot's role position and Ban Members permission.",
            ),
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        view=styled_view(
            "Hardban completed",
            f"{user.mention} was banned successfully.\n\n**Reason:** {reason}",
        )
    )
    await audit_log(
        interaction.guild,
        "hardban",
        f"{interaction.user} hardbanned {user}.",
        details=[("Reason", reason[:1024])],
        actor_id=interaction.user.id,
        subject_id=user.id,
    )


@tree.command(name="unhb", description="Remove a hardban")
@app_commands.describe(user_id="Discord user ID to unban")
@owner_check()
async def unhardban(interaction: discord.Interaction, user_id: str):
    assert interaction.guild is not None
    if not user_id.isdigit():
        await interaction.response.send_message(
            view=styled_view("Invalid user ID", "Enter a valid Discord user ID."),
            ephemeral=True,
        )
        return
    uid = int(user_id)
    with connect() as db:
        removed = db.execute("DELETE FROM hardbans WHERE user_id = ?", (uid,)).rowcount
    if not removed:
        await interaction.response.send_message(
            view=styled_view(
                "Hardban not found",
                "That user is not on the hardban list.",
            ),
            ephemeral=True,
        )
        return
    try:
        await interaction.guild.unban(discord.Object(id=uid), reason="Hardban removed")
    except discord.NotFound:
        pass
    await interaction.response.send_message(
        view=styled_view(
            "Hardban removed",
            f"<@{uid}> may be unbanned and rejoin the server.",
        )
    )


@tree.command(name="strike3", description="Record the final strike and apply blacklist roles")
@app_commands.describe(
    user="Member receiving the final strike",
    consequence="Consequence being applied",
    reason="Reason for the final strike",
    proof="Proof link or concise proof reference",
)
@staff_check()
async def strike3(
    interaction: discord.Interaction,
    user: discord.Member,
    consequence: str,
    reason: str,
    proof: str = "No proof provided",
):
    assert interaction.guild is not None
    if protected_from_moderation(user.id):
        await interaction.response.send_message(
            view=styled_view(
                "Strike safeguard",
                "Protected staff and founder accounts cannot receive a final strike.",
            ),
            ephemeral=True,
        )
        return
    current = active_strikes(interaction.guild.id, user.id)
    if len(current) != 2:
        await interaction.response.send_message(
            view=styled_view(
                "Final strike unavailable",
                "The final strike can only be recorded after exactly two active strikes.",
            ),
            ephemeral=True,
        )
        return
    with connect() as db:
        cursor = db.execute(
            """
            INSERT INTO strikes (
                guild_id, user_id, moderator_id, strike_number,
                reason, proof, status, consequence, created_at
            ) VALUES (?, ?, ?, 3, ?, ?, 'active', ?, ?)
            """,
            (
                interaction.guild.id,
                user.id,
                interaction.user.id,
                reason,
                proof,
                consequence,
                now_iso(),
            ),
        )
        strike = db.execute(
            "SELECT * FROM strikes WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    config = config_for(interaction.guild.id)
    applied_roles: list[str] = []
    for role_id in config["blacklist_role_ids"]:
        role = interaction.guild.get_role(role_id)
        if role:
            try:
                await user.add_roles(role, reason="Third strike blacklist consequence")
                applied_roles.append(role.name)
            except discord.Forbidden:
                continue
    await publish_strike(interaction, strike, 3)
    announcement = styled_view(
        "Final strike consequence applied",
        f"{user.mention} has reached **3/3 active strikes**.",
        details=[
            ("Consequence", consequence[:1024]),
            (
                "Blacklist roles",
                ", ".join(applied_roles)
                if applied_roles
                else "No configured roles were available.",
            ),
        ],
    )
    channel = interaction.channel
    if config["strike_channel_id"] and interaction.guild.get_channel(config["strike_channel_id"]):
        channel = interaction.guild.get_channel(config["strike_channel_id"])
    await channel.send(view=announcement)
    await interaction.response.send_message(
        view=styled_view(
            "Final strike recorded",
            f"{user.mention} is now at 3/3.",
        ),
        ephemeral=True,
    )
    await audit_log(
        interaction.guild,
        "strike3",
        f"{interaction.user} recorded the final strike for {user}.",
        details=[
            ("Reason", reason[:1024]),
            ("Consequence", consequence[:1024]),
            ("Proof", proof_text(proof)[:1024]),
        ],
        actor_id=interaction.user.id,
        subject_id=user.id,
    )


@tree.command(name="revokeall", description="Revoke every active strike in this server")
@owner_check()
async def revokeall(interaction: discord.Interaction):
    assert interaction.guild is not None
    with connect() as db:
        count = db.execute(
            """
            UPDATE strikes SET status = 'revoked', revoked_at = ?, revoked_by = ?
            WHERE guild_id = ? AND status = 'active'
            """,
            (now_iso(), interaction.user.id, interaction.guild.id),
        ).rowcount
    await interaction.response.send_message(
        view=styled_view(
            "Active strikes revoked",
            f"Revoked **{count}** active strike record(s) across this server.",
        )
    )


@tree.command(name="set-channel", description="Set the public strike announcement channel")
@app_commands.describe(channel="Channel used for public strike announcements")
@owner_check()
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    assert interaction.guild is not None
    config_for(interaction.guild.id)
    set_config_value(interaction.guild.id, "strike_channel_id", channel.id)
    await interaction.response.send_message(
        view=styled_view(
            "Strike channel updated",
            f"Public strike announcements will go to {channel.mention}.",
        )
    )


@tree.command(name="snipe-channel", description="Set the Roblox snipe notification channel")
@app_commands.describe(channel="Channel used for Roblox server alerts")
@owner_check()
async def snipe_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    assert interaction.guild is not None
    config_for(interaction.guild.id)
    set_config_value(interaction.guild.id, "snipe_channel_id", channel.id)
    await interaction.response.send_message(
        view=styled_view(
            "Snipe channel updated",
            f"Roblox server alerts will go to {channel.mention}.",
        )
    )


@tree.command(name="raid-start", description="Send a server-wide raid announcement")
@app_commands.describe(text="Plain-text message to DM to each member")
@owner_check()
async def raid_start(interaction: discord.Interaction, text: str):
    assert interaction.guild is not None
    text = text.strip()
    if not text:
        await interaction.response.send_message(
            view=styled_view(
                "Message required",
                "Provide a message to send by direct message.",
            ),
            ephemeral=True,
        )
        return
    if len(text) > 1900:
        await interaction.response.send_message(
            view=styled_view(
                "Message too long",
                "Keep the message to 1,900 characters or fewer so it fits in a direct message.",
            ),
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    try:
        members = [
            member async for member in interaction.guild.fetch_members(limit=None)
        ]
    except (discord.Forbidden, discord.HTTPException):
        # If member-list fetching is not enabled for the bot, use the available
        # guild cache rather than posting a public announcement.
        members = list(interaction.guild.members)

    recipients = [member for member in members if not member.bot]
    delivered = 0
    failed = 0
    semaphore = asyncio.Semaphore(5)

    async def deliver(member: discord.Member) -> None:
        nonlocal delivered, failed
        async with semaphore:
            try:
                recipient_ping = discord.Object(id=member.id)
                await member.send(
                    content=f"<@{recipient_ping.id}> {text}",
                    allowed_mentions=discord.AllowedMentions(
                        users=[recipient_ping],
                        roles=False,
                        everyone=False,
                    ),
                    suppress_embeds=True,
                )
                delivered += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1

    await asyncio.gather(*(deliver(member) for member in recipients))
    await interaction.followup.send(
        view=styled_view(
            "Raid announcement sent",
            f"Delivered the announcement by DM to **{delivered}** member(s). "
            f"{failed} member(s) could not receive a DM.",
        ),
        ephemeral=True,
    )


@tree.command(name="raid-leaderboard", description="Show the top raid point totals")
@owner_check()
async def raid_leaderboard(interaction: discord.Interaction):
    assert interaction.guild is not None
    message = await refresh_raid_leaderboard(
        interaction.guild, interaction.channel
    )
    if message:
        await interaction.response.send_message(
            view=styled_view(
                "Raid leaderboard updated",
                f"The live leaderboard is posted in {message.channel.mention}.",
            ),
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            view=styled_view(
                "Raid leaderboard unavailable",
                "I could not find a channel where the live leaderboard can be posted.",
            ),
            ephemeral=True,
        )


@tree.command(name="rreset", description="Reset all raid leaderboard points")
@owner_check()
async def rreset(interaction: discord.Interaction):
    assert interaction.guild is not None
    await interaction.response.defer(ephemeral=True)
    with connect() as db:
        members = db.execute(
            "SELECT user_id FROM raid_points WHERE guild_id = ?",
            (interaction.guild.id,),
        ).fetchall()
        count = db.execute(
            "DELETE FROM raid_points WHERE guild_id = ?",
            (interaction.guild.id,),
        ).rowcount
    removed_count = 0
    errors: list[str] = []
    for row in members:
        member = interaction.guild.get_member(row["user_id"])
        if member is None:
            continue
        _, removed, member_errors = await sync_point_promotion_roles(
            interaction.guild, member, 0
        )
        removed_count += len(removed)
        errors.extend(member_errors)
    await refresh_raid_leaderboard(interaction.guild, interaction.channel)
    details = [("Promotion roles removed", str(removed_count))]
    if errors:
        details.append(("Role update issue", errors[0]))
    await interaction.followup.send(
        view=styled_view(
            "Raid points reset",
            f"Reset raid points for **{count}** member record(s).",
            details=details,
        ),
        ephemeral=True,
    )


@tree.command(name="ticketpanel", description="Post the Roblox verification ticket panel")
@owner_check()
async def ticketpanel(interaction: discord.Interaction):
    await interaction.response.send_message(view=VerificationPanel())


@tree.command(name="ticket-close", description="Save a transcript and close the current ticket")
@app_commands.describe(reason="Reason shown in the transcript")
@staff_check()
async def ticket_close(interaction: discord.Interaction, reason: str = "Closed by staff"):
    if not isinstance(interaction.channel, discord.TextChannel) or not interaction.channel.name.startswith(
        "verify-"
    ):
        await interaction.response.send_message(
            view=styled_view(
                "Not a verification ticket",
                "Use this command inside a channel created by the verification panel.",
            ),
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        view=styled_view(
            "Ticket closing",
            "The transcript is being saved to the moderation log channel before this ticket is deleted.",
        ),
        ephemeral=True,
    )
    await audit_log(
        interaction.guild,
        "ticket",
        f"{interaction.user} closed {interaction.channel.name}.",
        details=[("Reason", reason[:1024])],
        actor_id=interaction.user.id,
    )
    await close_ticket(interaction.channel, reason)


@tree.command(
    name="verification-config",
    description="Configure Roblox verification and blacklist rules",
)
@app_commands.describe(
    approved_group_ids="Comma-separated Roblox group IDs allowed for verification",
    blacklist_group_ids="Comma-separated Roblox group IDs that trigger a kick",
    verified_role="Role assigned after successful verification",
    blacklist_role_ids="Comma-separated Discord role IDs applied by strike3",
    ticket_category="Category where verification tickets are created",
    top5th_role="Top 5th role; this role and every higher role are pinged for HR review",
    log_channel="Channel where moderation logs and ticket transcripts are sent",
    quarantine_role="Role applied to members joining during a detected raid spike",
    raid_join_threshold="Joins inside the window that trigger the raid safeguard",
    raid_window_seconds="Length of the raid detection window",
)
@owner_check()
async def verification_config(
    interaction: discord.Interaction,
    approved_group_ids: str = "",
    blacklist_group_ids: str = "",
    verified_role: discord.Role | None = None,
    blacklist_role_ids: str = "",
    ticket_category: discord.CategoryChannel | None = None,
    top5th_role: discord.Role | None = None,
    log_channel: discord.TextChannel | None = None,
    quarantine_role: discord.Role | None = None,
    raid_join_threshold: app_commands.Range[int, 2, 100] = 8,
    raid_window_seconds: app_commands.Range[int, 10, 300] = 30,
):
    assert interaction.guild is not None
    config_for(interaction.guild.id)
    set_config_value(
        interaction.guild.id,
        "verification_group_ids",
        parse_ids(approved_group_ids),
    )
    set_config_value(
        interaction.guild.id,
        "blacklist_group_ids",
        parse_ids(blacklist_group_ids),
    )
    set_config_value(
        interaction.guild.id,
        "blacklist_role_ids",
        parse_ids(blacklist_role_ids),
    )
    set_config_value(
        interaction.guild.id,
        "verified_role_id",
        verified_role.id if verified_role else None,
    )
    set_config_value(
        interaction.guild.id,
        "ticket_category_id",
        ticket_category.id if ticket_category else None,
    )
    set_config_value(
        interaction.guild.id,
        "top5th_role_id",
        top5th_role.id if top5th_role else None,
    )
    set_config_value(
        interaction.guild.id,
        "log_channel_id",
        log_channel.id if log_channel else LOG_CHANNEL_ID,
    )
    set_config_value(
        interaction.guild.id,
        "quarantine_role_id",
        quarantine_role.id if quarantine_role else None,
    )
    set_config_value(interaction.guild.id, "raid_join_threshold", raid_join_threshold)
    set_config_value(interaction.guild.id, "raid_window_seconds", raid_window_seconds)
    await interaction.response.send_message(
        view=styled_view(
            "Verification policy updated",
            "The Roblox verification workflow is now using the supplied group and role configuration.",
            details=[
                ("Approved groups", approved_group_ids or "None configured"),
                ("Blacklisted groups", blacklist_group_ids or "None configured"),
                (
                    "Verified role",
                    verified_role.mention if verified_role else "None configured",
                ),
                ("Ticket category", ticket_category.name if ticket_category else "None configured"),
                (
                    "HR escalation",
                    f"{top5th_role.name} and above"
                    if top5th_role
                    else "None configured",
                ),
                ("Log channel", log_channel.mention if log_channel else "Default log channel"),
                (
                    "Raid safeguard",
                    f"{raid_join_threshold} joins / {raid_window_seconds}s",
                ),
            ],
        )
    )


@bot.listen("on_member_join")
async def raid_safeguard_on_join(member: discord.Member):
    now = datetime.now(timezone.utc)
    config = config_for(member.guild.id)
    joins = bot.recent_joins[member.guild.id]
    joins.append(now)
    window = max(10, int(config.get("raid_window_seconds") or 30))
    while joins and (now - joins[0]).total_seconds() > window:
        joins.popleft()
    threshold = max(2, int(config.get("raid_join_threshold") or 8))
    if len(joins) < threshold:
        await audit_log(
            member.guild,
            "member-join",
            f"{member} joined the server.",
            details=[("Account created", member.created_at.isoformat())],
            subject_id=member.id,
        )
        return
    quarantine = (
        member.guild.get_role(config["quarantine_role_id"])
        if config.get("quarantine_role_id")
        else None
    )
    if quarantine and (
        not member.guild.me or quarantine < member.guild.me.top_role
    ):
        try:
            await member.add_roles(quarantine, reason="Raid safeguard")
        except (discord.Forbidden, discord.HTTPException):
            pass
    last_alert = bot.raid_alerted_until.get(member.guild.id)
    if last_alert and (now - last_alert).total_seconds() < 600:
        return
    bot.raid_alerted_until[member.guild.id] = now
    await audit_log(
        member.guild,
        "raid-safeguard",
        f"Raid safeguard triggered after {len(joins)} joins in {window} seconds.",
        details=[
            ("Latest member", member.mention),
            ("Quarantine role", quarantine.name if quarantine else "Not configured"),
            ("Action", "Member left in verification/quarantine flow; no automatic kick"),
        ],
        subject_id=member.id,
    )


@bot.listen("on_member_remove")
async def log_member_remove(member: discord.Member):
    await audit_log(
        member.guild,
        "member-remove",
        f"{member} left or was removed from the server.",
        subject_id=member.id,
    )


@bot.listen("on_member_update")
async def log_member_role_update(
    before: discord.Member, after: discord.Member
):
    before_ids = {role.id for role in before.roles}
    after_ids = {role.id for role in after.roles}
    added = [role.name for role in after.roles if role.id not in before_ids]
    removed = [role.name for role in before.roles if role.id not in after_ids]
    if not added and not removed:
        return
    await audit_log(
        after.guild,
        "role-update",
        f"Roles changed for {after}.",
        details=[
            ("Added", ", ".join(added) if added else "None"),
            ("Removed", ", ".join(removed) if removed else "None"),
        ],
        subject_id=after.id,
    )


@bot.listen("on_message_delete")
async def log_message_delete(message: discord.Message):
    if message.guild is None:
        return
    await audit_log(
        message.guild,
        "message-delete",
        f"A message by {message.author} was deleted in {message.channel.mention}.",
        details=[("Content", (message.content or "[no text content]")[:1024])],
        subject_id=message.author.id,
    )


@bot.listen("on_message_edit")
async def log_message_edit(before: discord.Message, after: discord.Message):
    if before.guild is None or before.content == after.content:
        return
    await audit_log(
        before.guild,
        "message-edit",
        f"A message by {before.author} was edited in {before.channel.mention}.",
        details=[
            ("Before", (before.content or "[no text content]")[:500]),
            ("After", (after.content or "[no text content]")[:500]),
        ],
        subject_id=before.author.id,
    )


@bot.listen("on_member_ban")
async def log_member_ban(guild: discord.Guild, user: discord.User):
    await audit_log(guild, "ban", f"{user} was banned.", subject_id=user.id)


@bot.listen("on_member_unban")
async def log_member_unban(guild: discord.Guild, user: discord.User):
    await audit_log(guild, "unban", f"{user} was unbanned.", subject_id=user.id)


@bot.listen("on_app_command_completion")
async def log_app_command(
    interaction: discord.Interaction, command: app_commands.Command[Any, Any, Any]
):
    if interaction.guild:
        await audit_log(
            interaction.guild,
            "command",
            f"{interaction.user} used `/{command.qualified_name}`.",
            actor_id=interaction.user.id,
        )


@bot.event
async def on_ready():
    if getattr(bot, "has_synced", False):
        return
    bot.has_synced = True
    if GUILD_ID:
        try:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} commands to guild {GUILD_ID}")
        except discord.Forbidden:
            synced = await bot.tree.sync()
            print(f"Guild {GUILD_ID} is not accessible; synced {len(synced)} global commands.")
    else:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} global commands.")
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.CheckFailure):
        return
    print(f"App command error: {error!r}")
    message = "Something went wrong while running that command."
    if interaction.response.is_done():
        await interaction.followup.send(
            view=styled_view("Command error", message),
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            view=styled_view("Command error", message),
            ephemeral=True,
        )


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not configured.")
    bot.run(token)