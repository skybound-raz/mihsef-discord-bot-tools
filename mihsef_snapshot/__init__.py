from .mihsef_snapshot import MiHSEFSnapshot  # adjust class name to whatever your cog class is

async def setup(bot):
    await bot.add_cog(MiHSEFSnapshot(bot))
