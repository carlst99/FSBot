# External Imports
from datetime import datetime as dt, timedelta, time
from datetime import timezone as tz
import discord
from discord.ext import commands, tasks
from logging import getLogger

import asyncio

import display.embeds
# Internal Imports
from modules import discord_obj as d_obj, tools, bot_status, trello, account_usage, loader, elo_ranks_handler as elo
from modules.spam_detector import is_spam
from display import AllStrings as disp, views
from classes import Player, PlayerStats
from classes.match import EndCondition
from modules import database as db

import modules.config as cfg

log = getLogger('fs_bot')


class GeneralCog(commands.Cog, name="GeneralCog"):

    def __init__(self, client):
        self.bot: discord.Bot = client
        self.__ranked_leaderboard_msg = None

    @commands.Cog.listener(name='on_ready')
    async def on_ready(self):
        if loader.is_all_loaded():
            return
        self.bot.add_view(views.RemoveTimeoutView())
        self.activity_update.start()
        self.elo_rank_update.start()

    @commands.slash_command(name="suggestion")
    async def suggestion(self, ctx: discord.ApplicationContext,
                         title: discord.Option(str, "Input your suggestion's title here", required=True),
                         description: discord.Option(str, "Describe your suggestion here", required=True)):
        """Send a suggestion for FSBot to the administration team!"""

        await trello.create_card(title, f"Suggested by [{ctx.user.name}] : " + description)
        await disp.SUGGESTION_ACCEPTED.send_priv(ctx, ctx.user.mention)

    @commands.slash_command(name="freeme")
    async def free_me(self, ctx: discord.ApplicationContext):
        """Used to request freedom if you have been timed out from FSBot."""
        await ctx.defer(ephemeral=True)
        if not (p := d_obj.is_player(ctx.user)):
            return await disp.NOT_PLAYER.send_priv(ctx, ctx.user.mention, d_obj.channels['register'].mention)
        if p.timeout_until != 0 and not p.is_timeout:
            await d_obj.timeout_player(p=p, stamp=0)
            await disp.TIMEOUT_RELEASED.send_priv(ctx)
        elif p.is_timeout:
            await disp.TIMEOUT_STILL.send_priv(ctx, tools.format_time_from_stamp(p.timeout_until, 'R'))
        else:
            await disp.TIMEOUT_FREE.send_priv(ctx)

    @commands.slash_command(name="usage")
    async def psb_usage(self, ctx: discord.ApplicationContext,
                        member: discord.Option(discord.Member, "Member to check usage for", required=True),
                        period_end: discord.Option(str, "Last of day of period, format YYYY-MM-DD.  Defaults to today.",
                                                   required=False)):
        """Command to retrieve all FS Jaeger Account usage by a specific player in an 9-week period."""
        await ctx.defer(ephemeral=True)
        if not ctx.guild:
            return await disp.GUILD_ONLY.send_priv(ctx)

        p = Player.get(member.id)
        if not p:
            await disp.NOT_PLAYER_2.send_priv(ctx, member.mention)
            return

        if period_end:
            try:
                period_end_dt = dt.strptime(period_end, '%Y-%m-%d')
            except ValueError:
                return await disp.USAGE_WRONG_FORMAT.send_priv(ctx, period_end)
        else:
            period_end_dt = dt.now()

        start_stamp, end_stamp = int((period_end_dt - timedelta(weeks=9)).timestamp()), int(period_end_dt.timestamp())

        usages = await account_usage.get_usages_period(p.id, start_stamp, end_stamp)

        await disp.USAGE_PSB.send_priv(ctx, player=p, start_stamp=start_stamp, end_stamp=end_stamp, usages=usages)

    @commands.slash_command(name="stats")
    async def stats_command(self, ctx: discord.ApplicationContext,
                            user: discord.Option(discord.Member, "User to check stats for", required=False)):
        """View your dueling stats"""
        if (user := user or ctx.user) is not ctx.user and not d_obj.is_admin(ctx.user):
            return await disp.STATS_SELF_ONLY.send_priv(ctx)  # Check if admin for requests on other users

        if not (player := d_obj.is_player(user)):
            if user is ctx.user:
                return await disp.NOT_PLAYER.send_priv(ctx, user.mention, d_obj.channels['register'].mention)
            else:
                return await disp.NOT_PLAYER_2.send_priv(ctx, user.mention)

        await ctx.defer(ephemeral=True)

        # This could become rather expensive over time...
        get_player_matches = await db.async_db_call(db.find_elements,
                                                    "matches",
                                                    {
                                                        "$or": [
                                                            {"current_players": player.id},
                                                            {"previous_players": player.id}
                                                        ]
                                                    }
                                                    )
        player_matches = list(get_player_matches)
        player_match_count = len(player_matches)

        if player_match_count == 0:
            return await disp.STAT_NO_MATCHES.send_priv(ctx, user.mention)

        # Sum match time and count times partners appear across all matches
        total_duel_sec = 0
        partners = {}

        for match in player_matches:
            total_duel_sec += match["end_stamp"] - match["start_stamp"]

            for player_id in match["current_players"]:
                if player_id == player.id:
                    continue
                if player_id in partners:
                    partners[player_id] += 1
                else:
                    partners[player_id] = 1

            for player_id in match["previous_players"]:
                if player_id == player.id:
                    continue
                if player_id in partners:
                    partners[player_id] += 1
                else:
                    partners[player_id] = 1

        #  Count up top 3 duel partners
        highest_partner = None
        duel_partners = ""

        for i in range(3):
            if len(partners) == 0:
                break

            # Find the partner with which we've had the most matches
            for partner_id, match_count in partners.items():
                if highest_partner is None or highest_partner[1] < match_count:
                    highest_partner = (partner_id, match_count)

            # Check if we selected a new highest partner
            if highest_partner is None or highest_partner[0] not in partners:
                continue

            # We've found a new highest partner. Add them and move on
            duel_partners += disp.STAT_PARTNER_MATCH_COUNT.value.format(highest_partner[0], highest_partner[1])
            duel_partners += "\n"
            partners.pop(highest_partner[0])

        return await disp.STAT_RESPONSE.send_priv(
            ctx,
            player=player,
            match_count=player_match_count,
            total_duel_sec=total_duel_sec,
            duel_partners=duel_partners)

    @commands.slash_command(name="elo")
    async def elo_command(self, ctx: discord.ApplicationContext,
                          user: discord.Option(discord.Member,
                                               "Admin Only: Member to check ELO for", required=False)):
        """View your own ELO and ELO history"""
        if (user := user or ctx.user) is not ctx.user and not d_obj.is_admin(ctx.user):
            return await disp.STATS_SELF_ONLY.send_priv(ctx)  # Check if admin for requests on other users

        if not (p := await d_obj.registered_check(ctx, user)):
            return

        await ctx.defer(ephemeral=True)
        player_stats = await p.get_or_fetch_stats()
        await disp.ELO_SUMMARY.send_priv(ctx, player_stats=player_stats)

    @tasks.loop(seconds=5)
    async def activity_update(self):
        await bot_status.update_status()

    @tasks.loop(time=[time(hour=i, minute=0, second=0, tzinfo=tz.utc) for i in range(24)])
    async def elo_rank_update(self):
        """Update ELO rankings every hour UTC"""
        await elo.update_player_ranks()
        next_stamp = self.elo_rank_update.next_iteration.timestamp() or (dt.now() + timedelta(hours=1)).timestamp()

        leaderboard_embed = display.embeds.elo_rank_leaderboard(elo.EloRank.get_all(),
                                                                elo.create_rank_dict(),
                                                                tools.timestamp_now(),
                                                                next_stamp,
                                                                self.elo_command.mention
                                                                )

        if not self.__ranked_leaderboard_msg:
            # Retrieve message ID from DB, edit existing message, or send new message if not found
            try:
                msg_id = await db.async_db_call(db.get_field, 'restart_data', 0, 'leaderboard_msg_id')
                self.__ranked_leaderboard_msg = await d_obj.channels['ranked_leaderboard'].fetch_message(msg_id)
                await self.__ranked_leaderboard_msg.edit(embed=leaderboard_embed)

            except (KeyError, discord.NotFound):
                log.info("Leaderboard message not found, sending new message...")
                self.__ranked_leaderboard_msg = await disp.NONE.send(d_obj.channels['ranked_leaderboard'],
                                                                     embed=leaderboard_embed)
            finally:
                await db.async_db_call(db.set_field, 'restart_data', 0,
                                       {'leaderboard_msg_id': self.__ranked_leaderboard_msg.id})
                # Purge all messages except for leaderboard
                await d_obj.channels['ranked_leaderboard'].purge(check=
                                                                 lambda m: m.id != self.__ranked_leaderboard_msg.id)

        else:
            await self.__ranked_leaderboard_msg.edit(embed=leaderboard_embed)

    @commands.Cog.listener(name="on_message")
    async def leaderboard_channel_deleter(self, message: discord.Message):
        if message.author == self.bot.user:
            return
        elif not message.channel.id == cfg.channels['ranked_leaderboard']:
            return
        elif d_obj.is_admin(message.author):
            return

        # Delete message and send
        if await is_spam(message):
            pass
        else:
            await disp.COMMAND_ONLY.send_temp(message)
        await message.delete()


def setup(client):
    client.add_cog(GeneralCog(client))
