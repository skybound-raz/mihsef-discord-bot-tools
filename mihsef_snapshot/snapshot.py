import json
import os
from datetime import datetime, timezone
from typing import Dict, Any

import discord
from redbot.core import commands, checks


SNAP_ROOT = "/data/mihsef_snapshots"  # persists on Red's data volume


def snowflake(obj) -> int:
    return getattr(obj, "id", None)


def serialize_perms(perm_overwrite: discord.PermissionOverwrite) -> Dict[str, bool]:
    # Convert PermissionOverwrite to dict of {perm_name: true/false/null}
    raw = {}
    for name, value in perm_overwrite:
        raw[name] = value  # True / False / None
    return raw


class MiHSEFSnapshot(commands.Cog):
    """MiHSEF snapshot & stepwise admin tools."""

    def __init__(self, bot):
        self.bot = bot

    @commands.group(name="mihsef", invoke_without_command=True)
    @checks.admin_or_permissions(manage_guild=True)
    async def mihsef(self, ctx: commands.Context):
        """MiHSEF tools (snapshot/migration)."""
        await ctx.send_help()

    @mihsef.group(name="snapshot", invoke_without_command=True)
    @checks.admin_or_permissions(manage_guild=True)
    async def snapshot(self, ctx: commands.Context):
        """Snapshot the current guild structure to JSON."""
        await ctx.send_help()

    @snapshot.command(name="now")
    @checks.admin_or_permissions(manage_guild=True)
    async def snapshot_now(self, ctx: commands.Context):
        """Write a full JSON snapshot of roles/channels/categories/overwrites."""
        await ctx.typing()
        guild: discord.Guild = ctx.guild

        # Build data
        data: Dict[str, Any] = {
            "meta": {
                "guild_id": guild.id,
                "guild_name": guild.name,
                "created_at": guild.created_at.replace(tzinfo=timezone.utc).isoformat(),
                "snapshot_at": datetime.now(timezone.utc).isoformat(),
                "icon": str(guild.icon) if guild.icon else None,
                "owner_id": guild.owner_id,
                "features": list(getattr(guild, "features", [])),
            },
            "roles": [],
            "categories": [],
            "channels": [],  # non-category channels only
        }

        # Roles (top->bottom order as Discord stores)
        for role in sorted(guild.roles, key=lambda r: r.position, reverse=False):
            data["roles"].append({
                "id": role.id,
                "name": role.name,
                "position": role.position,
                "color": role.color.value,
                "hoist": role.hoist,
                "mentionable": role.mentionable,
                "managed": role.managed,
                "permissions": role.permissions.value,  # bitfield
            })

        # Categories
        for cat in guild.categories:
            cat_overwrites = {}
            for target, ow in cat.overwrites.items():
                key = f"role:{target.id}" if isinstance(target, discord.Role) else f"user:{target.id}"
                cat_overwrites[key] = serialize_perms(ow)

            data["categories"].append({
                "id": cat.id,
                "name": cat.name,
                "position": cat.position,
                "nsfw": cat.nsfw,
                "overwrites": cat_overwrites
            })

        # Channels (text/voice/thread/stage—anything with a parent or not)
        for ch in guild.channels:
            if isinstance(ch, discord.CategoryChannel):
                continue

            overwrites = {}
            for target, ow in ch.overwrites.items():
                key = f"role:{target.id}" if isinstance(target, discord.Role) else f"user:{target.id}"
                overwrites[key] = serialize_perms(ow)

            parent_id = ch.category.id if ch.category else None
            base = {
                "id": ch.id,
                "name": ch.name,
                "type": str(ch.type),  # "text", "voice", ...
                "position": ch.position,
                "parent_id": parent_id,
                "overwrites": overwrites,
                "nsfw": getattr(ch, "nsfw", False),
                "slowmode_delay": getattr(ch, "slowmode_delay", 0),
            }

            # Optional, only on text & forum
            if isinstance(ch, (discord.TextChannel, discord.ForumChannel, discord.VoiceChannel, discord.StageChannel)):
                base["topic"] = getattr(ch, "topic", None)

            data["channels"].append(base)

        # Ensure path
        gpath = os.path.join(SNAP_ROOT, str(guild.id))
        os.makedirs(gpath, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        fname = os.path.join(gpath, f"{ts}.json")

        with open(fname, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        # Create a file attachment with a user-friendly name
        attachment_filename = f"{guild.name}_snapshot_{ts}.json".replace(" ", "_").replace("/", "_")
        file = discord.File(fname, filename=attachment_filename)

        await ctx.send(
            f"✅ Snapshot saved locally: `{fname}`\n"
            f"Roles: **{len(data['roles'])}** · Categories: **{len(data['categories'])}** · Channels: **{len(data['channels'])}**\n"
            f"JSON file attached below.",
            file=file
        )

    @snapshot.command(name="path")
    @checks.admin_or_permissions(manage_guild=True)
    async def snapshot_path(self, ctx: commands.Context):
        """Show the directory where snapshots are written."""
        await ctx.send(f"Snapshots are written to: `{SNAP_ROOT}/<guild_id>/YYYY-MM-DDTHH-MM-SS.json` (inside the bot container).")
