"""
Cog built to handle interaction with the duel lobby.

"""
# External Imports
import asyncio
import discord
from discord import Status
from discord.ext import commands, tasks
from logging import getLogger

# Internal Imports
import modules.config as cfg
import modules.discord_obj as d_obj
from classes.lobby import Lobby
from classes.players import Player
from display import AllStrings as disp
from modules import tools

log = getLogger('fs_bot')


class DuelLobbyCog(commands.Cog, name="DuelLobbyCog", command_attrs=dict(guild_ids=[cfg.general['guild_id']],
                                                                         default_permission=True)):
    def __init__(self, bot):
        #  Statics
        self.bot = bot
        self.dashboard_channel: discord.TextChannel = d_obj.channels['dashboard']
        # Dynamics
        self.dashboard_msg: discord.Message | None = None
        self.dashboard_embed = None

        self.dashboard_loop.start()

    def cog_check(self, ctx):
        player = Player.get(ctx.user.id)
        return True if player else False

    @tasks.loop(seconds=10)
    async def dashboard_loop(self):
        """Loop to check lobby timeouts, also updates dashboard in-case preference changes are made"""
        lobby_updates = []
        for lobby in Lobby.all_lobbies.values():
            lobby_updates.append(lobby.update())
        await asyncio.gather(*lobby_updates)

    @dashboard_loop.before_loop
    async def before_lobby_loop(self):
        if Lobby.all_lobbies.get("casual"):
            return
        casual_lobby = await Lobby.create_lobby("casual", d_obj.channels['dashboard'])

    @commands.Cog.listener('on_status_update')
    async def lobby_timeout_updater(self, before, after):
        #  Return if not player
        if p := d_obj.is_player(before) is False:
            return
        #  Return if status hasn't changed, or p not in lobby
        if before.status == after.status or not p.lobby:
            return
        if after.status in [Status.online, Status.dnd, Status.streaming]:
            p.set_lobby_timeout(0)
        elif after.status in [Status.idle, Status.offline]:
            p.set_lobby_timeout(tools.timestamp_now() + p.lobby.timeout_minutes * 60)

    @commands.user_command(name="Invite To Match")
    async def user_match_invite(self, ctx: discord.ApplicationContext, user: discord.User):
        p = Player.get(user.id)
        owner = Player.get(ctx.user.id)
        lobby = p.lobby or Lobby.channel_to_lobby(ctx.channel)
        if not lobby:
            await disp.LOBBY_CANT_INVITE.send_priv(ctx)
            return
        if p.lobby is not lobby:
            await disp.LOBBY_NOT_IN_2.send_priv(ctx, p.mention)
            return
        if owner.match and owner.match.owner != owner:
            await disp.LOBBY_NOT_OWNER.send_priv(ctx)
            return
        sent = await lobby.send_invite(owner, p)
        if sent and owner.match:
            await disp.LOBBY_INVITED_MATCH.send_priv(ctx, owner.mention, p.mention, owner.match.id_str)
            lobby.lobby_log(lobby.lobby_log(f'{owner.name} invited {p.name} to Match: {owner.match.id_str}'))
        elif sent:
            await disp.LOBBY_INVITED.send_priv(ctx, owner.mention, p.mention)
            lobby.lobby_log(lobby.lobby_log(f'{owner.name} invited {p.name} to a match.'))
        else:
            await disp.LOBBY_NO_DM.send_priv(ctx, p.mention)


def setup(client: discord.Bot):
    client.add_cog(DuelLobbyCog(client))
