# Red-DiscordBot Cog: Unified MiHSEF tools
# - !mihsef snapshot            -> dump roles/categories/channels/overwrites to JSON
# - !mihsef update_from_json    -> preview + confirm, then apply JSON to the current guild
#
# Notes:
# - Matches by NAME across servers (IDs differ). Overwrites attempt ID-first then fall back to ROLE NAME
#   using the snapshot's roles list (id->name).
# - v1 creates/updates roles, categories, channels, and overwrites. Now includes safe role deletion.
# - Stores snapshots in /data/mihsef_snapshots (good for Dockerized Red).

import asyncio
import datetime
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import discord
from redbot.core import commands, checks, Red
import logging

logger = logging.getLogger("red.mihsef")

CHECK_MARK = "✅"
CROSS_MARK = "❌"

# ------------ helpers (mapping & utilities) ------------

def _overwrite_to_dict(perms: discord.PermissionOverwrite) -> Dict[str, bool]:
    """
    Convert a PermissionOverwrite to a dict of {permission_name: True/False},
    skipping entries that are None.
    """
    out = {}
    # Complete list of known permission attributes (compatible with Discord.py 1.x and 2.x)
    permission_attrs = [
        "create_instant_invite", "kick_members", "ban_members", "administrator",
        "manage_channels", "manage_guild", "add_reactions", "view_audit_log",
        "priority_speaker", "stream", "read_messages", "view_channel",
        "send_messages", "send_tts_messages", "manage_messages", "embed_links",
        "attach_files", "read_message_history", "mention_everyone",
        "use_external_emojis", "external_emojis", "view_guild_insights",
        "connect", "speak", "mute_members", "deafen_members",
        "move_members", "use_voice_activation", "change_nickname",
        "manage_nicknames", "manage_roles", "manage_webhooks",
        "manage_emojis", "use_slash_commands", "use_application_commands",
        "request_to_speak", "manage_events", "manage_threads",
        "create_public_threads", "use_public_threads", "create_private_threads",
        "use_private_threads", "use_external_stickers", "external_stickers",
        "send_messages_in_threads", "use_embedded_activities", "moderate_members",
        "create_events", "send_polls", "use_external_apps", "use_external_sounds",
        "use_soundboard", "send_voice_messages"
    ]
    for attr in permission_attrs:
        value = getattr(perms, attr, None)
        if value is not None:
            out[attr] = value
    return out


def _perm_overwrites_from_json(
    guild: discord.Guild,
    overwrites_json: Dict[str, Dict[str, Optional[bool]]],
    snapshot_roles_by_id: Optional[Dict[int, str]] = None,
) -> Dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    """
    Convert snapshot overwrite schema to a mapping usable by Channel/Category .edit(overwrites=...).

    Keys look like "role:<id>" or "member:<id>" (members are intentionally skipped in v1).
    Strategy:
      1) Try matching role by ID in the current guild.
      2) If not found and we have a snapshot id->name table, try matching by NAME.
      3) Special-case @everyone (role id == guild.id).
    """
    result = {}
    if not overwrites_json:
        return result

    for subject_key, perms_dict in overwrites_json.items():
        try:
            subject_type, raw_id = subject_key.split(":")
        except ValueError:
            continue

        if subject_type != "role":
            # We don't apply member-specific overwrites cross-server in v1.
            continue

        role: Optional[discord.Role] = None
        # 1) Try ID
        try:
            rid = int(raw_id)
            role = guild.get_role(rid)
        except ValueError:
            role = None

        # 2) Special-case @everyone
        if role is None and raw_id == str(guild.id):
            role = guild.default_role

        # 3) Fallback by NAME using snapshot table
        if role is None and snapshot_roles_by_id:
            try:
                rid = int(raw_id)
                snap_name = snapshot_roles_by_id.get(rid)
                if snap_name:
                    role = discord.utils.get(guild.roles, name=snap_name)
            except ValueError:
                pass

        if role is None:
            # Skip unknown roles; safer than guessing.
            continue

        po = discord.PermissionOverwrite()
        for attr, val in (perms_dict or {}).items():
            if hasattr(po, attr):
                setattr(po, attr, val)
        result[role] = po

    return result


def _collect_current_named(
    guild: discord.Guild,
) -> Tuple[Dict[str, discord.Role], Dict[str, discord.CategoryChannel], Dict[str, discord.abc.GuildChannel]]:
    roles_by_name = {r.name: r for r in guild.roles}
    cats_by_name = {c.name: c for c in guild.categories}
    chans_by_name = {
        c.name: c for c in guild.channels if isinstance(c, (discord.TextChannel, discord.VoiceChannel, discord.ForumChannel))
    }
    return roles_by_name, cats_by_name, chans_by_name


def _role_position_plan(
    snapshot_roles: List[dict],
    guild: discord.Guild,
    roles_by_name: Dict[str, discord.Role],
) -> List[Tuple[discord.Role, int]]:
    """
    Compute desired role positions.
    Snapshot includes 'position' (Discord-style integer). We'll attempt to honor it by matching role names.
    """
    plan = []
    snap_sorted = sorted(snapshot_roles, key=lambda r: r.get("position", 0))
    for snap in snap_sorted:
        name = snap.get("name")
        if not name:
            continue
        role = roles_by_name.get(name)
        if role:
            plan.append((role, snap.get("position", role.position)))
    return plan


def _resolve_parent_category(
    snap_ch: dict,
    cats_by_name: Dict[str, discord.CategoryChannel],
    snapshot_categories_by_id: Dict[int, str],
) -> Optional[discord.CategoryChannel]:
    """
    Resolve the correct CategoryChannel object in the current guild for a snapshot channel.
    Prefer mapping parent_id->snapshot_name, then find that name in current guild.
    """
    parent_id = snap_ch.get("parent_id")
    if not parent_id:
        return None
    try:
        pid = int(parent_id)
    except Exception:
        return None
    snap_cat_name = snapshot_categories_by_id.get(pid)
    if not snap_cat_name:
        return None
    return cats_by_name.get(snap_cat_name)


# ------------ the cog ------------

class UpdateFromJSON(commands.Cog):
    """MiHSEF: snapshot current guild and apply updates from a JSON snapshot (roles/categories/channels/overwrites)."""

    def __init__(self, bot: Red):
        self.bot = bot

    # ---------- GROUP ----------

    @commands.group(name="mihsef")
    @checks.admin_or_permissions(manage_guild=True)
    async def mihsef_group(self, ctx: commands.Context):
        """MiHSEF utilities (snapshot / migration)."""
        pass

    # ---------- SNAPSHOT ----------

    @mihsef_group.command(name="snapshot")
    @checks.admin_or_permissions(manage_guild=True)
    async def snapshot_now(self, ctx: commands.Context):
        """
        Snapshot the current guild (roles/categories/channels/overwrites) to a JSON file,
        save it in /data/mihsef_snapshots and upload it.
        """
        guild = ctx.guild
        data = {
            "meta": {
                "guild_id": guild.id,
                "guild_name": guild.name,
                "snapshot_at": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "owner_id": guild.owner_id,
            },
            "roles": [],
            "categories": [],
            "channels": [],
        }

        # Roles
        for r in guild.roles:
            data["roles"].append({
                "id": r.id,
                "name": r.name,
                "position": r.position,
                "color": r.color.value,
                "hoist": r.hoist,
                "mentionable": r.mentionable,
                "managed": r.managed,
                "permissions": r.permissions.value,
            })

        # Categories
        for c in guild.categories:
            overwrites = {}
            for target, perms in c.overwrites.items():
                key = f"role:{target.id}" if isinstance(target, discord.Role) else f"member:{target.id}"
                overwrites[key] = _overwrite_to_dict(perms)
            data["categories"].append({
                "id": c.id,
                "name": c.name,
                "position": c.position,
                "nsfw": getattr(c, "nsfw", False) or getattr(c, "is_nsfw", lambda: False)(),
                "overwrites": overwrites,
            })

        # Channels (text/voice/forum)
        for ch in guild.channels:
            if isinstance(ch, discord.CategoryChannel):
                continue
            overwrites = {}
            for target, perms in ch.overwrites.items():
                key = f"role:{target.id}" if isinstance(target, discord.Role) else f"member:{target.id}"
                overwrites[key] = _overwrite_to_dict(perms)

            if isinstance(ch, discord.VoiceChannel):
                ch_type = "voice"
                topic = None
                nsfw = False
                slowmode = 0
            elif isinstance(ch, discord.ForumChannel):
                ch_type = "forum"
                topic = getattr(ch, "topic", None)
                nsfw = getattr(ch, "nsfw", False)
                slowmode = getattr(ch, "slowmode_delay", 0)
            else:
                ch_type = "text"
                topic = getattr(ch, "topic", None)
                nsfw = getattr(ch, "nsfw", False)
                slowmode = getattr(ch, "slowmode_delay", 0)

            data["channels"].append({
                "id": ch.id,
                "name": ch.name,
                "type": ch_type,
                "position": ch.position,
                "parent_id": ch.category_id,
                "overwrites": overwrites,
                "nsfw": nsfw,
                "slowmode_delay": slowmode,
                "topic": topic,
            })

        # Save & upload
        outdir = Path("/data/mihsef_snapshots")
        outdir.mkdir(parents=True, exist_ok=True)
        filename = f"{guild.name.replace(' ', '_')}_snapshot_{datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%S')}Z.json"
        filepath = outdir / filename

        with filepath.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        try:
            await ctx.send(f"Snapshot saved: `{filename}`", file=discord.File(str(filepath)))
        except Exception:
            await ctx.send(f"Snapshot saved to `{filepath}` (upload failed).")

    # ---------- UPDATE FROM JSON ----------

    @mihsef_group.command(name="update_from_json")
    @checks.admin_or_permissions(manage_guild=True)
    async def update_from_json_cmd(self, ctx: commands.Context):
        """
        Use with a JSON snapshot attached.
        Flow: parse -> preview summary -> add ✅/❌ -> on ✅ apply changes -> final summary.
        """
        if not ctx.message.attachments:
            return await ctx.send("Please attach a JSON snapshot file to this command.")

        att = ctx.message.attachments[0]
        if not att.filename.lower().endswith(".json"):
            return await ctx.send("The attachment must be a .json file.")

        raw = await att.read()
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            return await ctx.send(f"Could not parse JSON: `{e}`")

        guild: discord.Guild = ctx.guild

        snap_roles: List[dict] = data.get("roles", [])
        snap_categories: List[dict] = data.get("categories", [])
        snap_channels: List[dict] = data.get("channels", [])

        snapshot_roles_by_id = {}
        for r in snap_roles:
            rid = r.get("id")
            name = r.get("name")
            if isinstance(rid, int) and isinstance(name, str):
                snapshot_roles_by_id[rid] = name

        snapshot_categories_by_id = {}
        for c in snap_categories:
            cid = c.get("id")
            name = c.get("name")
            if isinstance(cid, int) and isinstance(name, str):
                snapshot_categories_by_id[cid] = name

        roles_by_name, cats_by_name, chans_by_name = _collect_current_named(guild)

        # ------- Build preview -------
        creates_roles, updates_roles = [], []
        for r in snap_roles:
            name = r.get("name")
            if not name or name == "@everyone":
                continue
            cur = roles_by_name.get(name)
            if cur is None:
                creates_roles.append(name)
            elif (
                cur.color.value != r.get("color", cur.color.value)
                or cur.hoist != r.get("hoist", cur.hoist)
                or cur.mentionable != r.get("mentionable", cur.mentionable)
                or cur.permissions.value != r.get("permissions", cur.permissions.value)
            ):
                updates_roles.append(name)

        creates_cats = [c["name"] for c in snap_categories if c.get("name") not in cats_by_name]
        creates_chans = [ch["name"] for ch in snap_channels if ch.get("name") not in chans_by_name]
        overwrite_updates = []
        for ch in snap_channels:
            name = ch.get("name")
            if not name:
                continue
            cur = chans_by_name.get(name)
            if cur:
                current_overwrites = {}
                for target, perms in cur.overwrites.items():
                    if isinstance(target, discord.Role):
                        current_overwrites[f"role:{target.id}"] = _overwrite_to_dict(perms)
                snapshot_overwrites = ch.get("overwrites", {})
                logger.debug(f"Channel {name}: Current overwrites: {current_overwrites}, Snapshot overwrites: {snapshot_overwrites}")
                if (
                    cur.position != ch.get("position", cur.position)
                    or cur.category_id != ch.get("parent_id")
                    or current_overwrites != snapshot_overwrites
                ):
                    overwrite_updates.append(name)

        desc_lines = []
        if creates_roles:
            desc_lines.append(f"**Create Roles:** {', '.join(creates_roles)}")
        if updates_roles:
            desc_lines.append(f"**Update Roles:** {', '.join(updates_roles)}")
        if creates_cats:
            desc_lines.append(f"**Create Categories:** {', '.join(creates_cats)}")
        if creates_chans:
            desc_lines.append(f"**Create Channels:** {', '.join(creates_chans)}")
        if overwrite_updates:
            head = ", ".join(overwrite_updates[:50])  # Increased limit to 50
            tail = " …" if len(overwrite_updates) > 50 else ""
            desc_lines.append(f"**Update Overwrites/Props (channels):** {head}{tail}")
        if not desc_lines:
            desc_lines.append("No changes detected (based on name and attributes).")

        preview = discord.Embed(
            title="Update From JSON — Preview",
            description="\n".join(desc_lines),
            color=discord.Color.blurple(),
        )
        if desc_lines[0] != "No changes detected (based on name and attributes).":
            preview.set_footer(text="React ✅ to apply, ❌ to cancel (invoker only).")
            msg = await ctx.send(embed=preview)
            for r in (CHECK_MARK, CROSS_MARK):
                try:
                    await msg.add_reaction(r)
                except discord.HTTPException:
                    pass
        else:
            await ctx.send(embed=preview)
            return  # Exit early if no changes

        def check(reaction: discord.Reaction, user: discord.User):
            return (
                reaction.message.id == msg.id
                and str(reaction.emoji) in (CHECK_MARK, CROSS_MARK)
                and user.id == ctx.author.id
            )

        try:
            reaction, _ = await self.bot.wait_for("reaction_add", timeout=180.0, check=check)
        except asyncio.TimeoutError:
            return await ctx.send("Timed out. No changes applied.")

        if str(reaction.emoji) == CROSS_MARK:
            return await ctx.send("Cancelled. No changes applied.")

        # ------- APPLY -------
        results = {
            "roles_created": 0,
            "roles_updated": 0,
            "role_position_updates": 0,
            "categories_created": 0,
            "channels_created": 0,
            "channel_updates": 0,
            "roles_deleted": 0,
            "errors": 0,
        }

        # 1) Roles (create/update basic props)
        try:
            for r in snap_roles:
                name = r.get("name")
                if not name or name == "@everyone":
                    continue

                role = roles_by_name.get(name)
                if role is None:
                    role = await guild.create_role(
                        name=name,
                        colour=discord.Colour(r.get("color", 0)),
                        hoist=r.get("hoist", False),
                        mentionable=r.get("mentionable", False),
                        reason="MiHSEF update_from_json: create role",
                    )
                    roles_by_name[name] = role
                    results["roles_created"] += 1
                else:
                    need_edit = (
                        role.color.value != r.get("color", role.color.value)
                        or role.hoist != r.get("hoist", role.hoist)
                        or role.mentionable != r.get("mentionable", role.mentionable)
                    )
                    if need_edit:
                        await role.edit(
                            colour=discord.Colour(r.get("color", role.color.value)),
                            hoist=r.get("hoist", role.hoist),
                            mentionable=r.get("mentionable", role.mentionable),
                            reason="MiHSEF update_from_json: update role",
                        )
                        results["roles_updated"] += 1

            # Role positions (best effort)
            roles_by_name, _, _ = _collect_current_named(guild)
            pos_plan = _role_position_plan(snap_roles, guild, roles_by_name)
            if pos_plan:
                mapping = {role: pos for role, pos in pos_plan}
                try:
                    await guild.edit_role_positions(positions=mapping)
                    results["role_position_updates"] = len(mapping)
                except Exception:
                    # Non-fatal: managed roles or permission constraints can block some moves.
                    pass

            # Delete roles not in the snapshot (skip @everyone and managed roles)
            existing_roles = {r.name: r for r in guild.roles}
            snapshot_role_names = {r.get("name") for r in snap_roles}
            roles_to_delete = [r for n, r in existing_roles.items() if n not in snapshot_role_names and n != "@everyone" and not r.managed]
            for role in roles_to_delete:
                try:
                    await role.delete(reason="MiHSEF update_from_json: remove unused role")
                    results["roles_deleted"] += 1
                except Exception:
                    results["errors"] += 1

        except Exception:
            results["errors"] += 1

        # 2) Categories (create + overwrites)
        try:
            for c in snap_categories:
                name = c.get("name")
                if not name:
                    continue
                if name not in cats_by_name:
                    cat = await guild.create_category(
                        name=name, reason="MiHSEF update_from_json: create category"
                    )
                    cats_by_name[name] = cat
                    results["categories_created"] += 1

            # Overwrites
            for c in snap_categories:
                name = c.get("name")
                if not name:
                    continue
                cat = cats_by_name.get(name)
                if not cat:
                    continue
                overwrites = _perm_overwrites_from_json(
                    guild, c.get("overwrites"), snapshot_roles_by_id
                )
                if overwrites:
                    try:
                        await cat.edit(overwrites=overwrites, reason="MiHSEF: category overwrites")
                    except Exception:
                        results["errors"] += 1
        except Exception:
            results["errors"] += 1

        # 3) Channels (create/update props + overwrites)
        try:
            for ch in snap_channels:
                name = ch.get("name")
                if not name:
                    continue
                ch_type = ch.get("type", "text")
                parent_obj = _resolve_parent_category(ch, cats_by_name, snapshot_categories_by_id)

                existing = chans_by_name.get(name)
                if existing is None:
                    # Create
                    if ch_type == "voice":
                        created = await guild.create_voice_channel(
                            name=name, category=parent_obj, reason="MiHSEF: create voice"
                        )
                    elif ch_type == "forum":
                        # Basic forum create; detailed forum settings are out-of-scope in v1
                        created = await guild.create_forum_channel(
                            name=name, category=parent_obj, reason="MiHSEF: create forum"
                        )
                    else:
                        created = await guild.create_text_channel(
                            name=name, category=parent_obj, reason="MiHSEF: create text"
                        )
                        # text-specific props
                        if "topic" in ch and ch["topic"] is not None:
                            try:
                                await created.edit(topic=ch["topic"])
                            except Exception:
                                pass
                        if "nsfw" in ch:
                            try:
                                await created.edit(nsfw=bool(ch["nsfw"]))
                            except Exception:
                                pass
                        if "slowmode_delay" in ch:
                            try:
                                await created.edit(slowmode_delay=int(ch["slowmode_delay"]))
                            except Exception:
                                pass
                    chans_by_name[name] = created
                    results["channels_created"] += 1
                else:
                    # Update props
                    try:
                        kwargs = {}
                        # Move into correct category if needed
                        if parent_obj and existing.category != parent_obj:
                            kwargs["category"] = parent_obj

                        if isinstance(existing, discord.TextChannel):
                            topic = ch.get("topic")
                            if topic is not None and (existing.topic or "") != (topic or ""):
                                kwargs["topic"] = topic
                            if "nsfw" in ch and existing.nsfw != bool(ch["nsfw"]):
                                kwargs["nsfw"] = bool(ch["nsfw"])
                            if "slowmode_delay" in ch and existing.slowmode_delay != int(ch["slowmode_delay"]):
                                kwargs["slowmode_delay"] = int(ch["slowmode_delay"])

                        if kwargs:
                            await existing.edit(**kwargs, reason="MiHSEF: channel props")
                        results["channel_updates"] += 1
                    except Exception:
                        results["errors"] += 1

                # Overwrites
                target = chans_by_name.get(name)
                if target:
                    overwrites = _perm_overwrites_from_json(
                        guild, ch.get("overwrites"), snapshot_roles_by_id
                    )
                    if overwrites:
                        try:
                            await target.edit(overwrites=overwrites, reason="MiHSEF: channel overwrites")
                        except Exception:
                            results["errors"] += 1

        except Exception:
            results["errors"] += 1

        # ------- FINAL SUMMARY -------
        lines = [
            f"Roles created: {results['roles_created']}",
            f"Roles updated: {results['roles_updated']}",
            f"Role positions updated: {results['role_position_updates']}",
            f"Categories created: {results['categories_created']}",
            f"Channels created: {results['channels_created']}",
            f"Channels updated/overwrites set: {results['channel_updates']}",
            f"Roles deleted: {results['roles_deleted']}",
            f"Errors: {results['errors']}",
        ]
        await ctx.send(
            embed=discord.Embed(
                title="Update From JSON — Completed",
                description="\n".join(lines),
                color=discord.Color.green() if results["errors"] == 0 else discord.Color.orange(),
            )
        )