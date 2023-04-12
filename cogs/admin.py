"""Admin cog, handles admin functions of FSBot"""

# External Imports
import auraxium
import discord
from discord.ext import commands, tasks
from logging import getLogger
import asyncio
from datetime import datetime as dt, time, timedelta
from pytz import timezone

# Internal Imports
import modules.config as cfg
import modules.accounts_handler as accounts
import modules.discord_obj as d_obj
from modules import census
from modules import tools
from modules import loader
from classes import Player
from classes.lobby import Lobby
from classes.match import BaseMatch, EndCondition, RankedMatch
from display import AllStrings as disp, embeds
import cogs.register as register

log = getLogger('fs_bot')


class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot: discord.Bot = bot
        self.online_cache = set()
        self.census_watchtower: asyncio.Task | None = None

    admin = discord.SlashCommandGroup(
        name='admin',
        description='Admin Only Commands',
        guild_ids=[cfg.general['guild_id']]
    )

    @admin.command()
    async def loader(self, ctx: discord.ApplicationContext,
                     action: discord.Option(str, "Lock or Unlock FSBot", choices=("Unlock", "Lock"),
                                            required=True)):
        """Unlock or Lock bot.  Only admins will be able to use any features."""
        match action:
            case "Unlock":
                loader.unlock_all()
                await disp.LOADER_TOGGLE.send_priv(ctx, action)
            case "Lock":
                loader.lock_all()
                await disp.LOADER_TOGGLE.send_priv(ctx, action)

    @admin.command()
    async def contentplug(self, ctx: discord.ApplicationContext,
                          action: discord.Option(str, "Enable, Disable or check status of the #contentplug filter",
                                                 choices=("Enable", "Disable", "Status"),
                                                 required=True)):
        """Enable or disable the #contentplug filter"""
        channel = d_obj.channels['content-plug']
        cog = self.bot.cogs['ContentPlug']
        if action == "Enable":
            cog.enabled = True
        elif action == "Disable":
            cog.enabled = False
        await d_obj.d_log(f"{action}ed {channel.mention}'s content filter")
        await ctx.respond(f"{action}ed {channel.mention}'s content filter", ephemeral=True)

    @admin.command(name="censusonlinecheck")
    async def manual_census(self, ctx: discord.ApplicationContext):
        """Runs a REST census online check, to catch any login/logouts that the websocket may have missed"""
        ran = await self.census_rest()
        await disp.MANUAL_CENSUS.send_priv(ctx, "successful." if ran else "failed.")

    @admin.command(name='census')
    async def census_control(self, ctx: discord.ApplicationContext,
                             action: discord.Option(str, "Enable, Disable, or check status of the Census Loop",
                                                    choices=("Enable", "Disable", "Status"),
                                                    required=False)):
        """Control the Census loop"""

        match action:

            case "Enable" if self.census_rest.is_running():
                self.census_rest.restart()
                await disp.CENSUS_LOOP_CHANGED.send_priv(ctx, "Running", "restarted")
            case "Enable":
                self.census_rest.start()
                await disp.CENSUS_LOOP_CHANGED.send_priv(ctx, "Stopped", "started")
            case "Disable" if self.census_rest.is_running():
                self.census_rest.stop()
                await disp.CENSUS_LOOP_CHANGED.send_priv(ctx, "Running", "stopped")
            case _:
                await disp.CENSUS_LOOP_STATUS.send_priv(ctx, "Running" if self.census_rest.is_running() else "Stopped")


    @admin.command(name="rulesinit")
    async def rulesinit(self, ctx: discord.ApplicationContext,
                        message_id: discord.Option(str, "Existing FSBot Rules message", required=False)):
        """Posts Rules Message in current channel or replaces message at given ID"""
        if message_id:
            msg = await d_obj.channels['rules'].fetch_message(int(message_id))
            try:
                await msg.edit(content="", view=register.RulesView(), embed=embeds.fsbot_rules_embed())
            except discord.Forbidden:
                await ctx.respond(content="Selected Message not owned by the bot!", ephemeral=True)
                return
        else:
            await ctx.channel.send(content="", view=register.RulesView(),
                                   embed=embeds.fsbot_rules_embed())
        await ctx.respond(content="Rules Message Posted", ephemeral=True)

    @admin.command(name="registerinit")
    async def registerinit(self, ctx: discord.ApplicationContext,
                           message_id: discord.Option(str, "Existing FSBot Register message", required=False)):
        """Posts Register Message in current channel or replaces message at given ID"""
        if message_id:

            msg = await d_obj.channels['register'].fetch_message(int(message_id))
            try:
                await msg.edit(content="", view=register.RegisterView(), embed=embeds.fsbot_info_embed())
            except discord.Forbidden:
                await ctx.respond(content="Selected Message not owned by the bot!", ephemeral=True)
                return
        else:
            await ctx.channel.send(content="", view=register.RegisterView(), embed=embeds.fsbot_info_embed())
        await ctx.respond(content="Register and Settings Message Posted", ephemeral=True)

    ##########################################################

    match_admin = admin.create_subgroup(
        name="match", description="Admin Match Commands"
    )

    @match_admin.command(name="addplayer")
    async def add_player(self, ctx: discord.ApplicationContext,
                         member: discord.Option(discord.Member, "User to invite to match", required=True),
                         match_channel: discord.Option(discord.TextChannel, "Match Channel to invite member to",
                                                       required=False)
                         ):
        """Add a player to a match. If a channel isn't provided, current is used."""
        p = Player.get(member.id)
        match_channel = match_channel or ctx.channel

        try:
            match = BaseMatch.active_match_channel_ids()[match_channel.id]
        except KeyError:
            await disp.MATCH_NOT_FOUND.send_priv(ctx, match_channel.mention)
            return
        if p.match:
            await disp.MATCH_ALREADY.send_priv(ctx, p.name, p.match.str_id)
            return

        await match.join_match(p)
        if p.lobby:
            await p.lobby.lobby_leave(player=p, match=match)
        await disp.MATCH_JOIN_2.send_priv(ctx, p.name, match.thread.mention)

    @match_admin.command(name="removeplayer")
    async def remove_player(self, ctx: discord.ApplicationContext,
                            member: discord.Option(discord.Member, "User to remove from match", required=True)):
        """Remove a player from a match.  If the owner is removed from a match, the match will end."""
        p = Player.get(member.id)

        if p.match:
            await disp.MATCH_LEAVE_2.send_priv(ctx, p.name, p.match.thread.mention)
            await p.match.leave_match(p.active)

        else:
            await disp.MATCH_NOT_IN_2.send_priv(ctx, p.name)

    @match_admin.command(name="create")
    async def create_match(self, ctx: discord.ApplicationContext,
                           member: discord.Option(discord.Member, "User to add to match",
                                                  required=True),
                           owner: discord.Option(discord.Member, "Match Owner, defaults to you",
                                                  required=False),
                           match_type: discord.Option(str, "Type of match to create, defaults to Casual",
                                                      default="casual",
                                                      choices=["casual", "ranked"])):
        """Creates a match with the given arguments.  """
        await ctx.defer(ephemeral=True)
        # set up Player Objects
        if not (invited := await d_obj.is_registered(ctx, member))\
                or not (owner := await d_obj.is_registered(ctx, owner or ctx.user)):
            return
        # check / update player status
        if invited.match or owner.match:
            return await disp.ADMIN_MATCH_CREATE_ERROR.send_priv(ctx)
        if invited.lobby:
            await invited.lobby.lobby_leave(invited)
        if owner.lobby:
            await owner.lobby.lobby_leave(owner)


        match_class = BaseMatch if match_type == "casual" else RankedMatch
        lobby = Lobby.get(match_type)
        match = await lobby.accept_invite(owner, invited)
        await disp.MATCH_CREATE.send_priv(ctx, match.thread.mention, match.id_str)


    @match_admin.command(name="end")
    async def end_match(self, ctx: discord.ApplicationContext,
                        match_id: discord.Option(int, "Match ID to end",
                                                 required=False),
                                                 is_ranked: discord.Option(bool, "Is match a ranked match?",
                                                 required=False)):
        """End a given match forcibly.  Uses current channel if no ID provided"""
        await ctx.defer(ephemeral=True)
        match = BaseMatch.active_matches_dict().get(match_id) or BaseMatch.active_match_channel_ids().get(
            ctx.channel_id)

        if not match:
            await disp.MATCH_NOT_FOUND.send_priv(ctx, (match_id or ctx.channel.mention))

        await disp.MATCH_END.send_priv(ctx, match.id_str)
        await match.end_match(EndCondition.EXTERNAL)

    #########################################################

    accounts_admin = admin.create_subgroup(
        name="accounts", description="Admin Accounts Commands"
    )

    @accounts_admin.command(name="assign")
    async def assign(self, ctx: discord.ApplicationContext,
                     member: discord.Option(discord.Member, "Recipients @mention", required=True),
                     acc_id: discord.Option(int, "A specific account ID to assign, 1-24", min_value=1, max_value=24,
                                            required=False)):
        """Assign an account to a user, with optional specific account ID"""
        await ctx.defer(ephemeral=True)
        p = Player.get(member.id)
        if not p:
            await disp.NOT_PLAYER_2.send_priv(ctx, member.mention)
            return
        if p.account:
            await accounts.terminate(p.account)
        if not acc_id:
            acc = accounts.pick_account(p)
        else:
            acc = accounts.all_accounts[acc_id]
            if acc.a_player:
                await disp.ACCOUNT_IN_USE.send_priv(ctx, acc.id)
                return
            accounts.set_account(p, acc)
        await accounts.send_account(acc, p)
        await disp.ACCOUNT_SENT_2.send_priv(ctx, p.mention, acc.id)

    @accounts_admin.command(name='info')
    async def account_info(self, ctx: discord.ApplicationContext):
        """Provide info on FSBot's connected Jaeger Accounts"""
        num_available = len(accounts._available_accounts)
        assigned = accounts._busy_accounts.values()
        num_used = len(assigned)
        online = [acc for acc in accounts.all_accounts.values() if acc.online_id]
        await disp.ACCOUNT_INFO.send_priv(ctx, num_available=num_available, num_used=num_used, assigned=assigned,
                                          online=online)

    @accounts_admin.command(name='watchtower')
    async def watchtower_toggle(self, ctx: discord.ApplicationContext,
                                action: discord.Option(str,
                                                       "Enable, Disable or check status of the accounts watchtower",
                                                       choices=("Enable", "Disable", "Status"), required=True)
                                ):
        """Accounts Watchtower Control"""
        running = accounts.UNASSIGNED_ONLINE_WARN
        changed = False

        if action == "Enable" and not running:
            accounts.UNASSIGNED_ONLINE_WARN = True
            changed = True
        elif action == "Disable" and running:
            accounts.UNASSIGNED_ONLINE_WARN = False
            changed = True
        string = f"Accounts watchtower was {'running' if running else 'stopped'}."
        if changed:
            string += f" It is now {'started' if accounts.UNASSIGNED_ONLINE_WARN else 'stopped'}."
            await d_obj.d_log(string)
        await ctx.respond(string, ephemeral=True)

    @accounts_admin.command(name='reload')
    async def accounts_reload(self, ctx: discord.ApplicationContext):
        """Run the Accounts Initializer Manually"""
        await ctx.defer(ephemeral=True)
        info = await accounts.init(cfg.GAPI_SERVICE)
        await d_obj.d_log(info)
        await disp.ANY.send_priv(ctx, info)

    @commands.message_command(name="Assign Account")
    @commands.max_concurrency(number=1, wait=True)
    async def msg_assign_account(self, ctx: discord.ApplicationContext, message: discord.Message):
        """
            Assign an account via Message Interaction
        """
        await ctx.defer(ephemeral=True)

        if not d_obj.is_admin(ctx.user):
            return await disp.CANT_USE.send_priv(ctx)

        p = Player.get(message.author.id)
        if not p:  # if not a player
            await disp.NOT_PLAYER_2.send_priv(ctx, message.author.mention)
            await message.add_reaction("\u274C")
            return

        if p.account:  # if already has account
            await disp.ACCOUNT_ALREADY_2.send_priv(ctx, p.mention, p.account.id)
            await message.add_reaction("\u274C")
            return

        acc = accounts.pick_account(p)
        if not acc:  # if no accounts available
            await disp.ACCOUNT_NO_ACCOUNT.send_priv(ctx)
            await message.add_reaction("\u274C")
            return

        # if all checks passed, send account
        await accounts.send_account(acc, p)
        await disp.ACCOUNT_SENT_2.send_priv(ctx, p.mention, acc.id)
        await message.add_reaction("\u2705")

    @msg_assign_account.error
    async def msg_assign_account_concurrency_error(self, ctx, error):
        if isinstance(error, commands.MaxConcurrencyReached):
            await ctx.respond('Someone else is using this command right now, try again soon!', ephemeral=True)

    ##########################################################

    player_admin = admin.create_subgroup(
        name="player", description="Admin Player Commands"
    )

    @player_admin.command(name='info')
    async def player_info(self, ctx: discord.ApplicationContext,
                          member: discord.Option(discord.Member, "@mention to get info on", required=True)):
        """Provide info on a given player"""
        p = Player.get(member.id)
        if not p:
            await disp.NOT_PLAYER_2.send_priv(ctx, member.mention)
            return

        await disp.REG_INFO.send_priv(ctx, player=p)

    @player_admin.command(name='rename')
    async def player_rename(self, ctx: discord.ApplicationContext,
                            member: discord.Option(discord.Member, "@mention to get info on", required=True),
                            name: discord.Option(str, "New name for Player, must be alphanumeric", required=True)):
        """Rename a given player"""
        p = Player.get(member.id)
        if not p:
            await disp.NOT_PLAYER_2.send_priv(ctx, member.mention)
            return
        if p.rename(name):
            await disp.REGISTER_RENAME.send_priv(ctx, member.mention, name)
            return
        await disp.REGISTER_INVALID_NAME.send_priv(ctx, name)

    @player_admin.command(name='clean')
    async def player_clean(self, ctx: discord.ApplicationContext,
                           member: discord.Option(discord.Member, "@mention to clean", required=True)):
        """Remove a player from their active commitments: lobbies, matches, accounts."""
        await ctx.defer(ephemeral=True)
        p = Player.get(member.id)
        if not p:
            await disp.NOT_PLAYER_2.send_priv(ctx, member.mention)
            return

        await p.clean()

        await disp.ADMIN_PLAYER_CLEAN.send_priv(ctx, p.mention)

    register_admin = admin.create_subgroup(
        name="register", description="Admin Registration Commands"
    )

    @register_admin.command(name="noaccount")
    async def register_no_acc(self, ctx: discord.ApplicationContext,
                              member: discord.Option(discord.Member, "@mention to modify", required=True)):
        """Force register a specific player as not having a personal Jaeger account."""
        await ctx.defer(ephemeral=True)
        if (p := Player.get(member.id)) is None:
            return await disp.NOT_PLAYER_2.send_priv(ctx, member.mention)

        if await p.register(None):
            return await disp.AS.send_priv(ctx, member.mention, disp.REG_SUCCESFUL_NO_CHARS())
        return await disp.AS.send_priv(ctx, member.mention, disp.REG_ALREADY_NO_CHARS())

    @register_admin.command(name="personal")
    async def register_personal_acc(self, ctx: discord.ApplicationContext,
                                    member: discord.Option(discord.Member, "@mention to modify", required=True)):

        """Force register a specific player with their personal jaeger account."""
        if (p := Player.get(member.id)) is None:
            return await disp.NOT_PLAYER_2.send_priv(ctx, member.mention)
        await ctx.send_modal(register.RegisterCharacterModal(player=p))

    ###############################################################

    timeout_admin = admin.create_subgroup(
        name="timeout", description="Admin Timeout Commands"
    )

    @timeout_admin.command(name='check')
    async def timeout_check(self, ctx: discord.ApplicationContext,
                            member: discord.Option(discord.Member, "@mention to check timeout for", required=True)):
        """Check the timeout status of a player"""
        if (p := Player.get(member.id)) and p.is_timeout:
            timestamps = [tools.format_time_from_stamp(p.timeout_until, x) for x in ("R", "F")]
            return await disp.TIMEOUT_UNTIL.send_priv(ctx, p.mention, p.name, *timestamps)
        await disp.TIMEOUT_NOT.send_priv(ctx, p.mention, p.name)

    @timeout_admin.command(name='until')
    async def timeout_until(self, ctx: discord.ApplicationContext,
                            member: discord.Option(discord.Member, "@mention to timeout", required=True),
                            reason: discord.Option(str, name="reason", description="Reason for timeout"),
                            date: discord.Option(str, name="date",
                                                 description="Format YYYY-MM-DD", required=True,
                                                 min_length=10, max_length=10),
                            time_str: discord.Option(str, name="time",
                                                     description="Format HH:MM", default="00:00",
                                                     min_length=5, max_length=5),
                            zone: discord.Option(str, name="timezone",
                                                 description="Defaults to UTC", default="UTC",
                                                 choices=tools.pytz_discord_options())
                            ):
        """Timeout a player until a specific date/time, useful for long timeouts.  Timezone defaults to UTC."""
        await ctx.defer(ephemeral=True)
        full_dt_str = ' '.join([date, time_str])
        try:
            timeout_dt = dt.strptime(full_dt_str, '%Y-%m-%d %H:%M')
            timeout_dt = timezone(zone).localize(timeout_dt)
            stamp = int(timeout_dt.timestamp())
        except ValueError:
            return await disp.TIMEOUT_WRONG_FORMAT.send_priv(ctx, full_dt_str)

        if p := Player.get(member.id):
            # Check if timeout is in the past
            relative, short_time = tools.format_time_from_stamp(stamp, "R"), tools.format_time_from_stamp(stamp, "f")
            if stamp < tools.timestamp_now():
                return await disp.TIMEOUT_PAST.send_priv(ctx, short_time)

            # Set timeout, clear player
            await d_obj.timeout_player(p=p, stamp=stamp, mod=ctx.user, reason=reason)
            return await disp.TIMEOUT_NEW.send_priv(ctx, p.mention, p.name, relative, short_time)
        await disp.NOT_PLAYER_2.send_priv(ctx, member.mention)

    @timeout_admin.command(name='for')
    async def timeout_for(self, ctx: discord.ApplicationContext,
                          member: discord.Option(discord.Member, "@mention to timeout", required=True),
                          reason: discord.Option(str, name="reason", description="Reason for timeout"),
                          minutes: discord.Option(int, "Minutes to timeout", default=0, min_value=0),
                          hours: discord.Option(int, "Hours to timeout", default=0, min_value=0),
                          days: discord.Option(int, "Days to timeout", default=0, min_value=0),
                          weeks: discord.Option(int, "Weeks to timeout", default=0, min_value=0)
                          ):
        """Timeout a player for a set period of time"""
        await ctx.defer(ephemeral=True)

        if (delta := timedelta(minutes=minutes, hours=hours, days=days, weeks=weeks)) + dt.now() == dt.now():
            return await disp.TIMEOUT_NO_TIME.send_priv(ctx)

        timeout_dt = dt.now() + delta

        if p := Player.get(member.id):
            # Set timeout, clear player
            await d_obj.timeout_player(p=p, stamp=int(timeout_dt.timestamp()), mod=ctx.user, reason=reason)

            timestamps = [tools.format_time_from_stamp(p.timeout_until, x) for x in ("R", "f")]
            return await disp.TIMEOUT_NEW.send_priv(ctx, p.mention, p.name, *timestamps)
        await disp.NOT_PLAYER_2.send_priv(ctx, member.mention)

    @timeout_admin.command(name='clear')
    async def timeout_clear(self, ctx: discord.ApplicationContext,
                            member: discord.Option(discord.Member, "@mention to end timeout for", required=True)):
        """Clear a specific players timeout"""
        await ctx.defer(ephemeral=True)
        if p := Player.get(member.id):
            if not p.is_timeout:
                return await disp.TIMEOUT_NOT.send_priv(ctx, p.mention, p.name)

            # Set Timeout to 0 (clearing it)
            await d_obj.timeout_player(p=p, stamp=0, mod=ctx.user)
            return await disp.TIMEOUT_CLEAR.send_priv(ctx, p.mention, p.name)
        await disp.NOT_PLAYER_2.send_priv(ctx, member.mention)

    ##############################################################

    @commands.Cog.listener('on_ready')
    async def on_ready(self):
        #  Wait until the bot is ready before starting loops, ensure account_handler has finished init
        await asyncio.sleep(5)
        self.account_sheet_reload.start()
        self.census_rest.start()
        self.census_watchtower = self.bot.loop.create_task(census.online_status_updater(Player.map_chars_to_players))

    @tasks.loop(seconds=15)
    async def census_rest(self):
        """Backup census method for checking accounts online status"""
        for _ in range(5):
            if await census.online_status_rest(Player.map_chars_to_players()):
                return True
        log.warning("Could not reach REST api during census REST after 5 tries...")
        return False

    @tasks.loop(minutes=30)
    async def wss_restart(self):
        """Restart the census_watchtower regularly in order to stop it from dying?"""
        if self.census_watchtower and not self.census_watchtower.done():
            self.census_watchtower.cancel()
        self.census_watchtower = self.bot.loop.create_task(census.online_status_updater(Player.map_chars_to_players))

    # @census_watchtower.after_loop
    # async def after_census_watchtower(self):
    #     if self.census_watchtower.failed():
    #         await d_obj.log(f"{d_obj.colin.mention} Census Watchtower has failed")
    #     if self.census_watchtower.is_being_cancelled():
    #         await census.EVENT_CLIENT.close()

    @tasks.loop(time=time(hour=11, minute=0, second=0))
    async def account_sheet_reload(self):
        log.info("Reinitialized Account Sheet and Account Characters")
        await accounts.init(cfg.GAPI_SERVICE)


def setup(client):
    client.add_cog(AdminCog(client))
