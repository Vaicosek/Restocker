"""cogs/rollback.py — the undo button on an audit row.

WHAT SUPPORT SEES AT 02:00
--------------------------
An action posts one embed to the ops log. The embed says, in real names and
integer coins, what happened. Under it is one button. Pressing it does not move
money — it shows the *figures* the undo would move and asks for a confirmation.
Confirming claims the row and writes the compensating entries. The button then
disables in place, on the original message, labelled with who did it.

THE BUTTON IS NOT ALWAYS CALLED "ROLLBACK", AND THAT IS THE POINT
----------------------------------------------------------------
`undo_label()` picks the wording from `action_log.undo_kind()`, which reads the
op list rather than the intent:

    ↩ Rollback                       the button moves the coins itself
    ↩ Reverse status · coins by hand it reverses the RECORD; a named staff task
                                     carries the coin legs, with the figures
    📋 Open staff task               nothing is automatic

The middle one is a land sale under escrow. The coins were captured into
`treasury:estates` and paid out to a seller; crediting them back with
`adjust_balance` would MINT, so the producer emits a `manual` op carrying the
exact transfers instead (`land_exchange._sale_reverse_ops`). A button labelled
↩ Rollback over that is a promise of a refund that does not happen — the buyer
is down `price` on a lot they can no longer receive, and nothing on the screen
said so. Owner's decision, 15 Aug: rename the button to what it does, and do not
half-build the executor. So the label, the confirm title, the confirm button and
the "Coins this will move" field all say `status` where they used to say money,
and the by-hand figure is shown FIRST because it is the larger one.

THE BUG THIS AVOIDS
-------------------
The implementation this was studied from reads `if log["rolled_back"]: return`,
then defers, then applies the reverse ops, then marks the row. Two staff on the
same message — or one impatient double-click — both pass the check and both
refund. Here nothing is applied until `action_log.claim()` has changed a row with
the state in its WHERE clause. The loser is told who has it, and moves nothing.

NO NEW COMMANDS
---------------
Everything here is buttons on messages the bot already posts: the rollback
confirm, the staff task card, and "Mark done". `/go` is the only slash command
this whole change adds.
"""
from __future__ import annotations

import re
import sys

import discord
from discord.ext import commands

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
log = getattr(core, "log", None)

import action_log  # noqa: E402
import panel_skus  # noqa: E402

_COLOR_OK = 0x2ECC71
_COLOR_WARN = 0xF1C40F
_COLOR_BAD = 0xE74C3C
_COLOR_NEUTRAL = 0x2B2D31


def _is_staff(interaction: discord.Interaction) -> bool:
    try:
        return bool(core.is_manager(interaction))
    except Exception:
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and (perms.administrator or perms.manage_guild))


def _ops_channel(client):
    try:
        import Restocker_db as _db
        cid = int(_db.get_config("ops_log_channel_id") or 0)
    except Exception:
        cid = 0
    if not cid:
        cid = int(getattr(core, "FUNDS_REPORT_CHANNEL_ID", 0) or 0)
    return client.get_channel(cid) if cid else None


# ── Embeds ──────────────────────────────────────────────────────────────────
def build_action_embed(action: dict, *, guild=None) -> discord.Embed:
    """The audit row as a human reads it. Real names, integer coins, no ids."""
    state = action.get("state") or "open"
    colour = {"open": _COLOR_NEUTRAL, "claimed": _COLOR_WARN, "done": _COLOR_OK,
              "partial": _COLOR_WARN, "failed": _COLOR_BAD}.get(state, _COLOR_NEUTRAL)
    e = discord.Embed(title=action["summary"][:256], colour=colour)
    if action.get("actor_name"):
        e.add_field(name="By", value=action["actor_name"], inline=True)
    e.add_field(name="When", value=str(action.get("created_at") or "")[:19] + " UTC", inline=True)
    coins = int(action.get("money_coins") or 0)
    if coins:
        e.add_field(name="Money moved", value=f"{coins:,} coins", inline=True)

    pv = action_log.preview(int(action["id"]), guild=guild)
    if pv["lines"]:
        body = "\n".join(f"• {ln}" for ln in pv["lines"][:12])
        if len(pv["lines"]) > 12:
            body += f"\n• …and {len(pv['lines']) - 12} more"
        # The field NAME carries the caveat, because a reader who stops at the
        # heading must not walk away thinking the coins come back on their own.
        name = ("Undo would (coins BY HAND — see below)"
                if int(pv.get("manual_coins") or 0) else "Rollback would")
        e.add_field(name=name, value=body[:1024], inline=False)
    else:
        # Empty states are EMPTY — no fake "nothing to show" field with a dash in it.
        e.add_field(name="Rollback", value="Not automatically reversible — "
                                           "the button opens a staff task instead.", inline=False)

    if state in ("done", "partial", "failed"):
        who = action.get("claimed_name") or action.get("claimed_by") or "staff"
        e.set_footer(text=f"Rolled back by {who} · {str(action.get('finished_at') or '')[:19]} UTC")
    return e


def _figures_block(movements) -> str:
    """A fixed-width before/after table. He confirms numbers, not intentions."""
    if not movements:
        return ""
    width = max(len(m[0]) for m in movements)
    width = min(max(width, 8), 24)
    rows = ["{:<{w}}  {:>12}  {:>10}  {:>12}".format("Who", "Before", "Change", "After", w=width),
            "-" * (width + 40)]
    for who, before, delta, after, short in movements:
        rows.append("{:<{w}}  {:>12,}  {:>+10,}  {:>12,}{}".format(
            who[:width], int(before), int(delta), int(after),
            f"   ! {abs(int(short)):,} short" if short else "", w=width))
    return "```\n" + "\n".join(rows) + "\n```"


def build_confirm_embed(action: dict, pv: dict) -> discord.Embed:
    """The confirm dialog. Rule: anything irreversible shows the figures it is
    about to move, in the same view as the button.

    TWO figures, not one, and the by-hand one goes FIRST when it exists. The
    single-figure version summed `pv["movements"]`, which only ever contains
    automatic legs — so a 40,000-coin land sale whose reverse is a staff task
    plus a 2,000-coin reporting mirror rendered **"Coins this will move: 2,000"**
    on an irreversible dialog. Wrong by a factor of 20, and wrong in the
    reassuring direction. `manual_coins` is the exposure the button will NOT
    move; it is shown as its own field, above the table, in the same view.
    """
    coins = sum(abs(int(m[2])) for m in pv["movements"])
    by_hand = int(pv.get("manual_coins") or 0)
    e = discord.Embed(
        title="Confirm — reverse status only" if by_hand else "Confirm rollback",
        description=f"**{action['summary']}**",
        colour=_COLOR_WARN if (coins or by_hand) else _COLOR_NEUTRAL)
    if by_hand:
        e.add_field(
            name=f"⚠ This button does NOT move {by_hand:,} coins",
            value=(f"It reverses the **record**. **{by_hand:,} coins** stay where "
                   f"they are and must be moved by a human — the steps and the "
                   f"exact figures are in the staff task below. Do not press this "
                   f"expecting a refund to go out."),
            inline=False)
    if pv["movements"]:
        e.add_field(name=f"Coins this button will move: {coins:,}",
                    value=_figures_block(pv["movements"])[:1024], inline=False)
    other = [ln for ln in pv["lines"] if not ln.lstrip("+-").split(" ")[0].replace(",", "").isdigit()]
    if other:
        e.add_field(name="Also", value="\n".join(f"• {o}" for o in other[:8])[:1024], inline=False)
    short_total = sum(abs(int(m[4])) for m in pv["movements"])
    if short_total:
        e.add_field(name="⚠ Cannot fully claw back",
                    value=f"{short_total:,} coins have already been spent. That part will "
                          f"open a staff task rather than drive a balance negative.",
                    inline=False)
    if pv["manual"]:
        e.add_field(name="⚠ Needs a human",
                    value="\n".join(f"• {m}" for m in pv["manual"][:5])[:1024], inline=False)
    if pv["already_done"]:
        e.add_field(name="Resuming",
                    value=f"{pv['already_done']} step(s) already applied by an earlier "
                          f"attempt will be skipped.", inline=False)
    if pv.get("stale_claim"):
        who = action.get("claimed_name") or action.get("claimed_by") or "someone"
        e.add_field(
            name="⚠ Taking over a stalled rollback",
            value=f"**{who}** claimed this at "
                  f"{str(action.get('claimed_at') or '')[:19]} UTC and it never "
                  f"finished — most likely a restart mid-rollback. Confirming takes "
                  f"the claim over. Every step whose money already committed is "
                  f"skipped; the figures above are only the steps still outstanding.",
            inline=False)
    e.set_footer(text="Nothing has moved yet. This is a preview.")
    return e


# ── Views ───────────────────────────────────────────────────────────────────
def _disabled_view(label: str) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    # A neutral custom_id: it must NOT match the RollbackButton template, or a
    # re-registered dynamic item would try to handle a button that is spent.
    v.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.secondary,
                                 disabled=True, custom_id="vtspent"))
    return v


class RollbackButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"vtrb:(?P<aid>\d+)"):
    """Persistent. Holds NOTHING but the action id, which it reads back out of
    its own custom_id on the message — every handler below re-resolves the audit
    row from the database before it looks at it."""

    # The default label is only ever used by `from_custom_id`, which rebuilds
    # this item to DISPATCH a click on a message Discord already rendered — the
    # label on screen is whatever `rollback_view` posted. Every construction
    # that renders passes `undo_label(...)` explicitly.
    def __init__(self, action_id: int, label: str = "↩ Rollback"):
        self.action_id = int(action_id)
        super().__init__(discord.ui.Button(
            label=label, style=discord.ButtonStyle.danger,
            custom_id=f"vtrb:{int(action_id)}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["aid"]))

    async def callback(self, interaction: discord.Interaction):
        await handle_rollback_click(interaction, self.action_id)


#: Button wording per `action_log.undo_kind()`. One place, so the label on the
#: ops-log message and the label on the confirm dialog cannot drift apart.
_UNDO_LABELS = {
    action_log.UNDO_NONE:    "📋 Open staff task",
    action_log.UNDO_BY_HAND: "↩ Reverse status · coins by hand",
    action_log.UNDO_COINS:   "↩ Rollback",
}
_CONFIRM_LABELS = {
    action_log.UNDO_BY_HAND: "Confirm — reverse status, no coins move",
    action_log.UNDO_COINS:   "Confirm rollback",
}


def undo_label(action_id: int, *, reversible: bool | None = None) -> str:
    """The button's wording, read off the OP LIST rather than off the intent.

    `reversible=False` forces the staff-task wording without a DB read — the
    one caller that already knows the answer passes it, so this stays a pure
    rename of an existing decision, not a second query per posted message.
    """
    if reversible is False:
        return _UNDO_LABELS[action_log.UNDO_NONE]
    try:
        kind = action_log.undo_kind(int(action_id))
    except Exception:  # noqa: BLE001
        # A label is not worth an exception on the posting path. The safe
        # default is the WEAKER claim: promise the record, not the coins.
        return _UNDO_LABELS[action_log.UNDO_BY_HAND]
    return _UNDO_LABELS.get(kind, _UNDO_LABELS[action_log.UNDO_COINS])


def rollback_view(action_id: int, *, reversible: bool = True) -> discord.ui.View:
    """One button. It says what it will actually do: an action with no automatic
    reverse ops offers a staff task, and one whose coin legs are a staff task
    says `coins by hand` rather than `Rollback` — see the module docstring. In
    both cases the face of the button is the truth, not the intention."""
    v = discord.ui.View(timeout=None)
    v.add_item(RollbackButton(
        action_id, label=undo_label(action_id, reversible=reversible)))
    return v


class ConfirmRollbackView(discord.ui.View):
    """Ephemeral, 120s. The claim happens on Confirm, not on render, so a preview
    left open on someone's screen reserves nothing."""

    def __init__(self, action_id: int, origin: discord.Message | None):
        super().__init__(timeout=120)
        self.action_id = int(action_id)
        self.origin = origin
        # Which button spoke for this dialog. See `_press()`.
        self._pressed_by: str | None = None
        # The confirm button carries the same truth as the one that opened it.
        # Two labels for one act is how "↩ Rollback" survived on a dialog whose
        # own body said the coins were a staff task.
        try:
            self.confirm.label = _CONFIRM_LABELS.get(
                action_log.undo_kind(self.action_id),
                _CONFIRM_LABELS[action_log.UNDO_COINS])
        except Exception:  # noqa: BLE001
            self.confirm.label = _CONFIRM_LABELS[action_log.UNDO_BY_HAND]

    def _press(self, who: str) -> bool:
        """CLAIM-FIRST, AT THE BUTTON — the same shape as
        `ConfirmSplitChange._press()` in `cogs/splits.py`.

        `action_log.claim()` below is the DURABLE claim and it holds: two
        concurrent Confirms move the coins exactly once, from two dialogs or two
        processes, and the loser is told so. What it cannot arbitrate is Confirm
        against Cancel, because Cancel takes no DB claim at all — it only edits
        the dialog. So a Cancel dispatched alongside a Confirm that was landing
        wrote "Cancelled. Nothing moved." as the STANDING TEXT of the dialog over
        a rollback that had just moved 12,000 coins, while Confirm's
        contradictory reply arrived as a separate ephemeral followup. The money
        was right; the report was not, and an operator who reads the dialog and
        not the followup reverses that sale a second time by hand.

        Discord dispatches every component interaction as its own task, so both
        presses are concurrent callbacks. This is a synchronous read-and-set with
        **no await between the read and the write**, called **before** the defer
        in both callbacks: on one event loop that is atomic, so of two
        concurrently dispatched presses exactly one gets True and exactly one
        gets to speak for this dialog. Recording WHICH one lets the loser be told
        what actually happened instead of the opposite.
        """
        if self._pressed_by is not None:
            return False
        self._pressed_by = who
        return True

    async def _already(self, interaction: discord.Interaction) -> None:
        """Tell the loser what the WINNER did — never the opposite of it."""
        if self._pressed_by == "cancel":
            msg = ("This dialog was cancelled by the first press, so nothing "
                   "moved. Press ↩ Rollback on the log message if you still "
                   "want it.")
        else:
            msg = ("This dialog has already been confirmed: the rollback is "
                   "running right now, or has already finished. Nothing was "
                   "started a second time — the ops log has the outcome.")
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception:  # noqa: BLE001 — already responded/expired
            try:
                await interaction.followup.send(msg, ephemeral=True)
            except Exception:  # noqa: BLE001
                pass

    @discord.ui.button(label="Confirm rollback", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # BEFORE the defer. Every await is a place the other press can overtake.
        if not self._press("confirm"):
            return await self._already(interaction)
        await interaction.response.defer(ephemeral=True)

        # ── THE CLAIM. One atomic UPDATE with the state in the WHERE clause. ──
        won, row = action_log.claim(self.action_id, interaction.user.id,
                                    getattr(interaction.user, "display_name", ""))
        if not won:
            state = (row or {}).get("state")
            holder = (row or {}).get("claimed_name") or (row or {}).get("claimed_by") or "someone"
            if state in ("done", "partial"):
                msg = f"Already rolled back by **{holder}**. Nothing moved."
            elif state == "failed":
                msg = (f"A rollback by **{holder}** failed part-way. Reopen it from the "
                       f"staff task rather than starting a second one.")
            else:
                mins = max(1, action_log.STALE_CLAIM_SECONDS // 60)
                msg = (f"**{holder}** is rolling this back right now. Nothing moved. "
                       f"If they never finish, this becomes reclaimable {mins} minutes "
                       f"after they started.")
            for c in self.children:
                c.disabled = True
            await interaction.edit_original_response(view=self)
            return await interaction.followup.send(msg, ephemeral=True)

        # The in-flight card goes up BEFORE the first op moves anything, and it
        # carries ↩ Retry rollback. Product review §3: a process death mid-apply
        # used to leave the action `claimed` with no card in the channel, no
        # button that would win a claim, and nothing to press but a hand-written
        # UPDATE. It is deleted below on a clean finish, so the ops log is only
        # littered by rollbacks that actually stalled.
        run_tid = action_log.open_run_task(
            self.action_id, interaction.user.id,
            getattr(interaction.user, "display_name", ""))
        run_msg = await post_staff_task(interaction.client, run_tid)

        report = action_log.apply_rollback(
            self.action_id, staff_id=interaction.user.id,
            staff_name=getattr(interaction.user, "display_name", ""),
            guild=interaction.guild)

        if run_msg is not None:
            try:
                await run_msg.delete()
            except Exception as e:  # noqa: BLE001
                if log:
                    log.warning("[rollback] in-flight card %s not deleted: %s", run_tid, e)

        fresh = action_log.get(self.action_id) or {}
        await _disable_origin(interaction, fresh, self.origin)

        for c in self.children:
            c.disabled = True
        await interaction.edit_original_response(view=self)

        _by_hand = 0
        try:
            _by_hand = action_log.manual_total(action_log.ops_of(self.action_id))
        except Exception:  # noqa: BLE001
            pass
        bits = [(f"Status reversed on **{fresh.get('summary', '')}** — "
                 f"**{_by_hand:,} coins have NOT moved** and are yours to move "
                 f"by hand; the staff task below has the exact steps."
                 if _by_hand else f"Rolled back **{fresh.get('summary', '')}**."),
                f"{len(report['done'])} step(s) applied."]
        if report["skipped"]:
            bits.append(f"{len(report['skipped'])} already done — skipped.")
        if report["failed"]:
            bits.append(f"⚠ {len(report['failed'])} failed.")
        if report["tasks"]:
            bits.append(f"📋 {len(report['tasks'])} staff task(s) opened.")
        await interaction.followup.send(" ".join(bits), ephemeral=True)

        for tid in report["tasks"]:
            await post_staff_task(interaction.client, tid)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Cancel takes the same claim, and it matters MORE here than on Confirm:
        # this reply is `edit_message`, so "Nothing moved." becomes the standing
        # text of the dialog rather than one ephemeral line among several.
        if not self._press("cancel"):
            return await self._already(interaction)
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(
            content="Cancelled. Nothing moved.", embed=None, view=self)


class StaffTaskView(discord.ui.View):
    """Persistent. `Mark done` is claim-first as well — two staff cannot both
    close the same task and each believe they were the one who handled it."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Mark done", style=discord.ButtonStyle.success,
                       custom_id="vttask:done")
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_staff(interaction):
            return await interaction.response.send_message(
                "Staff only.", ephemeral=True)
        # Re-resolve the subject from the message: a persistent view is
        # re-registered with placeholder state and knows nothing about itself.
        tid = _task_id_from_message(interaction.message)
        if tid is None:
            return await interaction.response.send_message(
                "I cannot tell which task this card is for any more.", ephemeral=True)
        if not action_log.close_task(tid, interaction.user.id):
            return await interaction.response.send_message(
                "Already closed by someone else.", ephemeral=True)
        e = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        e.colour = _COLOR_OK
        # KEEP the `Task #N` marker: it is the only thing that ties this card back
        # to its row, and every future handler on this message re-resolves from it.
        e.set_footer(text=f"Task #{tid} · done by "
                          f"{getattr(interaction.user, 'display_name', 'staff')}")
        await interaction.response.edit_message(
            embed=e, view=_disabled_view("✅ Done"))

    @discord.ui.button(label="↩ Retry rollback", style=discord.ButtonStyle.danger,
                       custom_id="vttask:retry")
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Unstick a rollback that died part-way.

        The button on the original log message is disabled by then, so without
        this the only route back is a hand-written UPDATE. Re-resolves the task
        from the card, the action from the task, and reopens claim-first — ops
        already `done` stay done, so nothing is paid twice.
        """
        if not _is_staff(interaction):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        tid = _task_id_from_message(interaction.message)
        task = action_log.get_task(tid) if tid is not None else None
        if not task or not task.get("action_id"):
            return await interaction.response.send_message(
                "This task is not attached to a rollback, so there is nothing to retry. "
                "Finish it by hand and press Mark done.", ephemeral=True)

        aid = int(task["action_id"])
        await interaction.response.defer(ephemeral=True)
        won, row = action_log.reopen(aid, interaction.user.id)
        if not won:
            state = (row or {}).get("state") or "gone"
            if state == "open":
                msg = "Someone already reopened it — press ↩ Rollback on the log message."
            elif state in ("done",):
                msg = "That rollback finished; there is nothing left to retry."
            elif state == "claimed":
                holder = (row or {}).get("claimed_name") or (row or {}).get("claimed_by") \
                    or "someone"
                mins = max(1, action_log.STALE_CLAIM_SECONDS // 60)
                msg = (f"**{holder}** is rolling this back right now, so Retry would race "
                       f"them. If they never finish, this unlocks {mins} minutes after "
                       f"they started — press Retry again then.")
            else:
                msg = f"Cannot retry: the rollback is `{state}` right now."
            return await interaction.followup.send(msg, ephemeral=True)

        action = action_log.get(aid) or {}
        pv = action_log.preview(aid, guild=interaction.guild)
        await interaction.followup.send(
            content=f"Reopened. **{pv['already_done']}** step(s) already applied will be "
                    f"skipped — only the unfinished ones are shown below.",
            embed=build_confirm_embed(action, pv),
            view=ConfirmRollbackView(aid, None), ephemeral=True)


_TASK_RE = re.compile(r"Task #(\d+)")


def _task_id_from_message(message: discord.Message | None):
    if message is None:
        return None
    for e in message.embeds:
        m = _TASK_RE.search((e.footer.text if e.footer else "") or "")
        if m:
            return int(m.group(1))
        m = _TASK_RE.search(e.title or "")
        if m:
            return int(m.group(1))
    return None


# ── Handlers ────────────────────────────────────────────────────────────────
async def handle_rollback_click(interaction: discord.Interaction, action_id: int):
    if not _is_staff(interaction):
        return await interaction.response.send_message(
            "Only managers can roll an action back.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)   # well inside 3s

    action = action_log.get(action_id)                 # re-resolve from the DB
    if not action:
        return await interaction.followup.send(
            "That audit row is gone — nothing to roll back.", ephemeral=True)

    state = action.get("state")
    if state in ("done", "partial"):
        who = action.get("claimed_name") or "staff"
        await _disable_origin(interaction, action, interaction.message)
        return await interaction.followup.send(
            f"Already rolled back by **{who}**.", ephemeral=True)

    # A claim that is genuinely in flight belongs to whoever holds it — say so
    # and stop, rather than showing a confirm whose Confirm button is guaranteed
    # to lose. A claim that has gone stale (the process died mid-rollback) falls
    # through to the confirm below, which now carries a takeover banner: that is
    # the route out of `claimed` that used to require a hand-written UPDATE.
    if state == "claimed" and not action_log.is_stale_claim(action):
        who = action.get("claimed_name") or action.get("claimed_by") or "someone"
        mins = max(1, action_log.STALE_CLAIM_SECONDS // 60)
        return await interaction.followup.send(
            f"**{who}** is rolling this back right now. Nothing moved.\n"
            f"If they crashed part-way, this becomes reclaimable {mins} minutes "
            f"after they started — press this button again then and it will "
            f"resume from where they stopped.", ephemeral=True)

    if not action_log.reversible(action_id):
        tid = action_log.open_task(
            f"Manual reversal: {action['summary']}",
            f"This action stored no automatic reverse operations, so it cannot be undone "
            f"by the bot. Audit row #{action_id}, run by "
            f"{action.get('actor_name') or 'the system'} at "
            f"{str(action.get('created_at'))[:19]} UTC.",
            action_id=action_id, op_index=-1, opened_by=interaction.user.id)
        await post_staff_task(interaction.client, tid)
        return await interaction.followup.send(
            "This one cannot be reversed automatically — I have opened a staff task "
            "with the details instead of pretending to undo it.", ephemeral=True)

    pv = action_log.preview(action_id, guild=interaction.guild)
    await interaction.followup.send(
        embed=build_confirm_embed(action, pv),
        view=ConfirmRollbackView(action_id, interaction.message),
        ephemeral=True)


async def _disable_origin(interaction, action: dict, fallback_message):
    """Disable the button on the ORIGINAL log message, in place.

    Prefers the ids stored on the audit row (the confirm happens in an ephemeral
    follow-up whose own `message` is the wrong one), and falls back to whatever
    message the click came from.
    """
    label = "↩ Rolled back"
    who = action.get("claimed_name")
    if who:
        label = f"↩ Rolled back by {who}"[:80]
    state = action.get("state")
    if state == "partial":
        label = "⚠ Rolled back — see staff task"
    elif state == "failed":
        label = "⚠ Rollback failed — see staff task"

    msg = None
    try:
        if action.get("channel_id") and action.get("message_id"):
            ch = interaction.client.get_channel(int(action["channel_id"]))
            if ch is not None:
                msg = await ch.fetch_message(int(action["message_id"]))
    except Exception:
        msg = None
    msg = msg or fallback_message
    if msg is None:
        return
    try:
        await msg.edit(embed=build_action_embed(action, guild=interaction.guild),
                       view=_disabled_view(label))
    except Exception as e:  # noqa: BLE001
        if log:
            log.warning("[rollback] could not disable origin message: %s", e)


# ── Posting ─────────────────────────────────────────────────────────────────
async def post_action_log(client, action_id: int, *, channel=None):
    """Post an audit row's embed + Rollback button and remember where it went."""
    action = action_log.get(action_id)
    if not action:
        return None
    ch = channel or _ops_channel(client)
    if ch is None:
        return None
    view = rollback_view(action_id, reversible=action_log.reversible(action_id))
    msg = await ch.send(embed=build_action_embed(action, guild=getattr(ch, "guild", None)),
                        view=view)
    action_log.attach_message(action_id, ch.id, msg.id)
    return msg


async def post_staff_task(client, task_id: int, *, channel=None):
    t = action_log.get_task(int(task_id))
    if not t:
        return None
    ch = channel or _ops_channel(client)
    if ch is None:
        return None
    e = discord.Embed(title=f"📋 {t['title']}"[:256], description=t["body"][:4000],
                      colour=_COLOR_WARN)
    e.set_footer(text=f"Task #{t['id']} · opened because the rollback could not "
                      f"finish this part automatically")
    return await ch.send(embed=e, view=StaffTaskView())


async def log_and_post(client, *, kind: str, summary: str, ops: list,
                       actor=None, guild_id=None, action_key: str = None,
                       channel=None) -> int:
    """One call for an action that wants an undo button. Returns the action id.

    For an ASYNC producer that has the ops in hand. A producer that is
    synchronous — every headless money core in this bot is, because the satellite
    and the web layer call them off the gateway — cannot use this: it calls
    `action_log.record()` itself with a caller-minted `action_key`, and its async
    caller then calls `post_by_key()` with the same key. Splitting it that way is
    what lets the audit row be written on EVERY path (including the ones with no
    Discord client at all) while the button is posted only where there is a
    channel to post it in.
    """
    aid = action_log.record(
        kind, summary, ops,
        actor_id=getattr(actor, "id", None), actor_name=getattr(actor, "display_name", ""),
        guild_id=guild_id, action_key=action_key)
    try:
        await post_action_log(client, aid, channel=channel)
    except Exception as e:  # noqa: BLE001
        if log:
            log.warning("[rollback] audit row %s recorded but not posted: %s", aid, e)
    return aid


async def post_by_key(client, action_key: str, *, channel=None):
    """Post the log embed + Rollback button for an audit row a SYNC producer wrote.

    Resolves the row by the producer's own idempotency key, so the async side
    needs to know nothing except the key it can re-derive from the domain event
    (`land:sale:412`). Returns the message, or None when there is no such row —
    which is the honest outcome for a path that did not actually record anything.

    Never posts twice for one row: `attach_message` is what post_action_log
    writes, and a row that already carries a message_id is left alone.
    """
    row = action_log.by_key(str(action_key))
    if not row:
        return None
    if row.get("message_id"):
        return None
    return await post_action_log(client, int(row["id"]), channel=channel)


class RollbackCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot


async def setup(bot):
    action_log.ensure_schema()
    panel_skus.ensure_schema()
    try:
        bot.add_dynamic_items(RollbackButton)
    except Exception as e:  # noqa: BLE001
        if log:
            log.warning("[rollback] dynamic item registration failed: %s", e)
    try:
        bot.add_view(StaffTaskView())
    except Exception:
        pass
    await bot.add_cog(RollbackCog(bot))
