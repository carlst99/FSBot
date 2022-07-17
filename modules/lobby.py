"""Module to handle lobby and invites """



# External Imports
import discord
from discord.ext import commands, tasks
from datetime import datetime as dt, timedelta
from logging import getLogger

# Internal Imports
import modules.config as cfg
import modules.discord_obj as d_obj
from modules.spam_detector import is_spam
from classes.players import Player
from classes.match import BaseMatch
from display import AllStrings as disp, embeds, views
import modules.tools as tools

log = getLogger('fs_bot')


## Lobby Variables

# lists for lobby usage
_lobbied_players: list[Player] = []
_invites: dict[Player, list[Player]] = dict()   # list of invites by owner.id: list[invited players]


# Logs
logs: list[(int, str)] = []  # lobby logs recorded as a list of tuples, (timestamp, message)
recent_log_length: int = 8
longer_log_length: int = 25

# Lobby Timeout
timeout_minutes: int = 30
_warned_players: list[Player] = []


# Functions

# logs
def logs_recent():
    return logs[-recent_log_length:]

def logs_longer():
    return logs[-longer_log_length:]

def lobby_log(message):
    logs.append((tools.timestamp_now(), message))
    log.info(f'Lobby Log: {message}')

def lobbied():
    return _lobbied_players

# lobby interaction
def lobby_timeout(player):
    """Removes from lobby list, executes player lobby leave method, returns True if removed"""
    if player in _lobbied_players:
        player.on_lobby_leave()
        _lobbied_players.remove(player)
        self._warned_players.remove(player)
        lobby_log(f'{player.name} was removed from the lobby by timeout.')
        return True
    else:
        return False

def lobby_timeout_reset(player):
    """Resets player lobbied timestamp, returns True if player was in lobby"""
    if player in _lobbied_players:
        player.reset_lobby_timestamp()
        if player in self._warned_players:
            self._warned_players.remove(player)
        return True
    return False

def lobby_leave(player):
    """Removes from lobby list, executes player lobby leave method, returns True if removed"""
    if player in _lobbied_players:
        player.on_lobby_leave()
        _lobbied_players.remove(player)
        lobby_log(f'{player.name} left the lobby.')
        return True
    else:
        return False

def lobby_join(player):
    """Adds to lobby list, executes player lobby join method, returns True if added"""
    if player not in _lobbied_players:
        player.on_lobby_add()
        _lobbied_players.append(player)
        lobby_log(f'{player.name} joined the lobby.')
        return True
    else:
        return False

def invite(owner:Player, invited:Player):
    """Invite Player to match, if match already existed returns match.  If player in match but not owner, returns false"""
    for match in BaseMatch._active_matches:
        if owner.active and match.owner.id == owner.id:
            match.invite(invited)
            return match
        else:
            return False
    if owner.id not in [match.owner.id for match in BaseMatch._active_matches]:
        try:
            _invites[owner.id].append(invited)
        except KeyError:
            _invites[owner.id] = [invited]

def accept_invite(owner, player):
    """Accepts invite from owner to player, if match doesn't exist then creates it and returns created match.
    If owner has since joined a different match, returns false.
    If existing match joined, returns True"""
    for match in BaseMatch._active_matches:
        if match.owner.id == owner.id:
            match.join_match(player)
            return True
    if owner.active:
        return False
    else:
        BaseMatch.create(owner, [player])