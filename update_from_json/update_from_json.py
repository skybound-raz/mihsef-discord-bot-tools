# update_from_json.py
# Red-DiscordBot Cog: "Update From JSON"
# Features:
# - !mihsef update_from_json  (attach a JSON snapshot file)
# - Shows a preview summary (dry run)
# - Adds ✅ / ❌ reactions. If the invoker reacts ✅, applies changes and posts a results summary.
# Notes:
# - Matches by NAME, not by ID. Use consistent naming between snapshot and target.
# - v1 creates/updates roles, categories, channels, and permission overwrites.
# - No deletions performed in v1 (safer). Add deletes later behind a flag if desired.

import json
import asyncio
from typing import Dict, Tuple, List, Optional

import discord
from redbot.core import commands, checks
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, bold

CHECK_MARK = "✅"
CROSS_MARK = "❌"

def _perm_overwrites_from_json(guild: discord.Guild, overwrites_json: Dict[str, Dict[str, Optional[bool]]]) -> Dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    """
    Convert snapshot overwrite schema to discord.PermissionOverwrite mapping.
    Keys look like "role:<id>" or (potentially later) "member:<id>".
    We match by role name if the role-id doesn't exist in target guild:
      - Strategy: We can't know the role name from the key, so we only support role-id lookups
        that exist *or* we skip. To support names, the JSON would need to carry names too.
    Improvement: snapshot should carry overwrite subjects by *name* as well as by id.
    """
    result = {}
    for subject_key, perms in (overwrites_json or {}).items():
        try:
            subject_type, raw_id = subject_key.split(":")
        except ValueError:
            continue

        if subject_type == "role":
            # Try by ID first; if not found, we can't map reliably by name without more data.
            role = guild.get_role(int(raw_id))
            # Fallback: try @everyone special-case
            if role is None and raw_id == str(guild.id):
                role = guild.default_role
            if role is None:
                # Skip unknown role-id; a future enhancement could include a name->id mapping table.
                continue
            # Build PermissionOverwrite
            po = discord.PermissionOverwrite()
            for attr, val in (perms or {}).items():
                if not hasattr(po, attr):
                    # Ignore unknown fields; JSON may include extras we don't map (ok)
                    continue
                setattr(po, attr, val)
            result[role] = po

        elif subject_type == "member":
            # We do not auto-map members in v1 for safety (IDs differ cross-server). Skip.
            continue

    return result

def _collect_current_named(guild: discord.Guild) -> Tuple[Dict[str, discord.Role], Dict[str, discord.CategoryChannel], Dict[str, discord.abc.GuildChannel]]:
    roles_by_name = {r.name: r for r in guild.roles}
    cats_by_name = {c.name: c for c in guild.categories}
    chans_by_name = {c.name: c for c in guild.channels if isinstance(c, (discord.TextChannel, discord.VoiceChannel, discord.ForumChannel))}
    return roles_by_name, cats_by_name, chans_by_name

def _role_position_plan(snapshot_roles: List[dict], guild: discord.Guild, roles_by_name: Dict[str, discord.Role]) -> List[Tuple[discord.Role, int]]:
    """
    Compute desired role positions. Snapshot includes 'position' (descending in Discord UI).
    We map by name. Missing roles will be created first, then positions adjusted.
    Returns list for guild.edit_role_positions().
    """
    plan = []
    # Build a desired order by name -> target_position
    # Note: Discord role positions are tricky; positions are relative. We normalize by index.
    snap_sorted = sorted(snapshot_roles, key=lambda r: r.get("position", 0))
    for snap in snap_sorted:
        name = snap["name"]
        if name in roles_by_name:
            plan.append((roles_by_name[name], snap.get("position", roles_by_name[name].position)))
    return plan

class UpdateFromJSON(commands.Cog):
    """Apply guild structure updates from a JSON snapshot (roles/categories/channels/overwrites)."""

    def __init__(self, bot: Red):
        self.bot = bot

    @commands.group(name="mihsef")
    @checks.admin_or_permissions(manage_guild=True)
    async def mihsef_group(self, ctx: commands.Context):
        """MIHSEF utilities."""
        pass

    @mihsef_group.command(name="update_from_json")
    @checks.admin_or_permissions(manage_guild=True)
    async def update_from_json(self, ctx: commands.Context):
        """
        Use with a JSON snapshot attached.
        Flow: parse -> preview summary -> add ✅/❌ -> on ✅ apply changes -> final summary.
        """
        if not ctx.message.attachments:
            return await ctx.send("Please attach a JSON snapshot file to this command.")

        att = ctx.message.attachments[0]
        if not att.filename.lower().endswith(".json"):
            return await ctx.send("The attachment must be a .json file.")

        # Download & parse JSON
        raw = await att.read()
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            return await ctx.send(f"Could not parse JSON: `{e}`")

        guild: discord.Guild = ctx.guild

        # Pull sections we support
        snap_roles = data.get("roles", [])
        snap_categories = data.get("categories", [])
        snap_channels = data.get("channels", [])

        roles_by_name, cats_by_name, chans_by_name = _collect_current_named(guild)

        # PREVIEW: build a change summary
        creates_roles = []
        updates_roles = []
        for r in snap_roles:
            name = r["name"]
            if name == "@everyone":
                continue
            if name not in roles_by_name:
                creates_roles.append(name)
            else:
                # simplistic: if color/hoist/mentionable differ, mark update
                cur = roles_by_name[name]
                if (cur.color.value != r.get("color", 0)
                    or cur.hoist != r.get("hoist", False)
                    or cur.mentionable != r.get("mentionable", False)):
                    updates_roles.append(name)

        creates_cats = []
        for c in snap_categories:
            name = c["name"]
            if name not in cats_by_name:
                creates_cats.append(name)

        creates_chans = []
        overwrite_updates = []
        for ch in snap_channels:
            name = ch["name"]
            if name not in chans_by_name:
                creates_chans.append(name)
            else:
                # mark that we'll update topic/nsfw/slowmode + overwrites
                overwrite_updates.append(name)

        # Show preview embed
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
            desc_lines.append(f"**Update Overwrites/Props (channels):** {', '.join(overwrite_updates[:10])}" + (" …" if len(overwrite_updates) > 10 else ""))

        if not desc_lines:
            desc_lines.append("No changes detected (based on name matching).")

        preview = discord.Embed(
            title="Update From JSON — Preview",
            description="\n".join(desc_lines),
            color=discord.Color.blurple(),
        )
        preview.set_footer(text="React ✅ to apply, ❌ to cancel (invoker only).")
        msg = await ctx.send(embed=preview)
        for r in (CHECK_MARK, CROSS_MARK):
            try:
                await msg.add_reaction(r)
            except discord.HTTPException:
                pass

        def check(reaction: discord.Reaction, user: discord.User):
            return (
                reaction.message.id == msg.id
                and str(reaction.emoji) in (CHECK_MARK, CROSS_MARK)
                and user.id == ctx.author.id
            )

        try:
            reaction, user = await self.bot.wait_for("reaction_add", timeout=120.0, check=check)
        except asyncio.TimeoutError:
            return await ctx.send("Timed out. No changes applied.")

        if str(reaction.emoji) == CROSS_MARK:
            return await ctx.send("Cancelled. No changes applied.")

        # APPLY CHANGES
        results = {"roles_created": 0, "roles_updated": 0, "role_position_updates": 0,
                   "categories_created": 0, "channels_created": 0, "channel_updates": 0, "errors": 0}
        # 1) Ensure roles exist / update basic props
        try:
            for r in snap_roles:
                name = r["name"]
                if name == "@everyone":
                    # optionally: adjust default perms via role.edit(permissions=...)
                    continue

                role = roles_by_name.get(name)
                if role is None:
                    role = await guild.create_role(
                        name=name,
                        colour=discord.Colour(r.get("color", 0)),
                        hoist=r.get("hoist", False),
                        mentionable=r.get("mentionable", False),
                        reason="UpdateFromJSON: create role",
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
                            reason="UpdateFromJSON: update role",
                        )
                        results["roles_updated"] += 1
            # Role positions
            # Refresh roles_by_name (new roles added)
            roles_by_name, _, _ = _collect_current_named(guild)
            pos_plan = _role_position_plan(snap_roles, guild, roles_by_name)
            if pos_plan:
                mapping = {role: pos for role, pos in pos_plan}
                try:
                    await guild.edit_role_positions(positions=mapping)
                    results["role_position_updates"] = len(mapping)
                except Exception:
                    # Not fatal; permissions may restrict moving managed roles, etc.
                    pass
        except Exception:
            results["errors"] += 1

        # 2) Ensure categories
        try:
            # Create missing categories first
            for c in snap_categories:
                name = c["name"]
                if name not in cats_by_name:
                    cat = await guild.create_category(name=name, reason="UpdateFromJSON: create category")
                    cats_by_name[name] = cat
                    results["categories_created"] += 1

            # Apply category overwrites when possible (role-id based; best effort)
            for c in snap_categories:
                name = c["name"]
                cat = cats_by_name.get(name)
                if not cat:
                    continue
                overwrites = _perm_overwrites_from_json(guild, c.get("overwrites"))
                if overwrites:
                    try:
                        await cat.edit(overwrites=overwrites, reason="UpdateFromJSON: category overwrites")
                    except Exception:
                        results["errors"] += 1
        except Exception:
            results["errors"] += 1

        # 3) Ensure channels (+ basic props + overwrites)
        try:
            for ch in snap_channels:
                name = ch["name"]
                ch_type = ch.get("type", "text")
                parent_id = ch.get("parent_id")
                parent_obj = None

                # Prefer parent *name* from snapshot if available (not provided in v1), else map known IDs to names manually if you extend snapshot.
                # Here we match parent by ID -> name only if ID matches an existing category (rare across servers).
                for cat in cats_by_name.values():
                    if str(cat.id) == str(parent_id) or cat.name in (c["name"] for c in snap_categories if c["id"] == parent_id):
                        parent_obj = cat
                        break

                existing = chans_by_name.get(name)
                if existing is None:
                    # create
                    if ch_type == "voice":
                        created = await guild.create_voice_channel(name=name, category=parent_obj, reason="UpdateFromJSON: create voice")
                    else:
                        created = await guild.create_text_channel(name=name, category=parent_obj, reason="UpdateFromJSON: create text")
                        # text-specific props
                        if "topic" in ch and ch["topic"]:
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
                    # update props
                    try:
                        kwargs = {}
                        if isinstance(existing, discord.TextChannel):
                            if "topic" in ch and (existing.topic or "") != (ch["topic"] or ""):
                                kwargs["topic"] = ch["topic"]
                            if "nsfw" in ch and existing.nsfw != bool(ch["nsfw"]):
                                kwargs["nsfw"] = bool(ch["nsfw"])
                            if "slowmode_delay" in ch and existing.slowmode_delay != int(ch["slowmode_delay"]):
                                kwargs["slowmode_delay"] = int(ch["slowmode_delay"])
                        if parent_obj and existing.category != parent_obj:
                            kwargs["category"] = parent_obj
                        if kwargs:
                            await existing.edit(**kwargs, reason="UpdateFromJSON: channel props")
                        results["channel_updates"] += 1
                    except Exception:
                        results["errors"] += 1

                # Overwrites
                target = chans_by_name.get(name)
                if target:
                    overwrites = _perm_overwrites_from_json(guild, ch.get("overwrites"))
                    if overwrites:
                        try:
                            await target.edit(overwrites=overwrites, reason="UpdateFromJSON: channel overwrites")
                        except Exception:
                            results["errors"] += 1
        except Exception:
            results["errors"] += 1

        # FINAL SUMMARY
        lines = [
            f"Roles created: {results['roles_created']}",
            f"Roles updated: {results['roles_updated']}",
            f"Role positions updated: {results['role_position_updates']}",
            f"Categories created: {results['categories_created']}",
            f"Channels created: {results['channels_created']}",
            f"Channels updated/overwrites set: {results['channel_updates']}",
            f"Errors: {results['errors']}",
        ]
        await ctx.send(embed=discord.Embed(
            title="Update From JSON — Completed",
            description="\n".join(lines),
            color=discord.Color.green() if results["errors"] == 0 else discord.Color.orange()
        ))

async def setup(bot: Red):
    await bot.add_cog(UpdateFromJSON(bot))
