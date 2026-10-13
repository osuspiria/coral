"""Remote commands from the closed list (spec 5.2): sync-now, restart-player, reboot.

Each command id is written to state.json before the command runs and is
confirmed in the next heartbeat's `acked_commands`. An id already executed is
never executed again, so a reboot cannot loop even if the server resends it.
Anything outside the list is never executed (it is acknowledged so the server
stops sending it, and logged).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from .changes import ChangeSource
from .client import Command
from .state import State
from .system import SystemActions

log = logging.getLogger(__name__)

ALLOWED = ("sync-now", "restart-player", "reboot")
REMEMBER_IDS = 200


class CommandExecutor:
    def __init__(
        self,
        state: Callable[[], State],
        save: Callable[[], None],
        changes: ChangeSource,
        actions: SystemActions,
    ) -> None:
        self._state = state
        self._save = save
        self._changes = changes
        self._actions = actions

    def acks_to_send(self) -> list[int]:
        return sorted(self._state().pending_acks)

    def acks_delivered(self, ids: Iterable[int]) -> None:
        """The server got these acks; stop sending them."""
        state = self._state()
        sent = set(ids)
        state.pending_acks = [i for i in state.pending_acks if i not in sent]
        self._save()

    async def handle(self, commands: Iterable[Command]) -> None:
        reboot: Command | None = None
        for command in commands:
            state = self._state()
            if command.id in state.executed_command_ids:
                continue  # resent before our ack arrived: never run twice
            # Persist first: a power cut after this point never repeats the command.
            state.executed_command_ids = (state.executed_command_ids + [command.id])[-REMEMBER_IDS:]
            state.pending_acks = sorted(set(state.pending_acks) | {command.id})
            self._save()
            if command.name not in ALLOWED:
                log.error(
                    "Refusing command %s: %r is not in the closed list", command.id, command.name
                )
                continue
            log.warning("Executing remote command %s: %s", command.id, command.name)
            if command.name == "sync-now":
                self._changes.wake(full=True)
            elif command.name == "restart-player":
                if not await self._actions.restart_mpv():
                    log.error("restart-player failed (check the polkit rule)")
            else:
                reboot = command  # after the other commands of this heartbeat
        if reboot is not None and not await self._actions.reboot():
            log.error("reboot failed (check the polkit rule)")
