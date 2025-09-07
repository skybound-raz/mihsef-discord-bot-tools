from .snapshot import MiHSEFSnapshot

async def setup(bot):
    await bot.add_cog(MiHSEFSnapshot(bot))
