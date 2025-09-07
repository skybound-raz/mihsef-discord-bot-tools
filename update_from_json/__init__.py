from .update_from_json import UpdateFromJSON

async def setup(bot):
    await bot.add_cog(UpdateFromJSON(bot))
