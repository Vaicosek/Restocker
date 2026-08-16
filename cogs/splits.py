"""cogs/splits.py — the configuration surface for `split_rules.py`.

WHY THIS FILE EXISTS
--------------------
`split_rules.py` shipped with its EXECUTION path wired (`land_settle` calls
`run_split`, `cogs/loops.py` runs `resume_pending` and `stuck_runs`) and its
CONFIGURATION path wired to nothing at all: `add_rule`, `deactivate_rule`,
`set_short_policy`, `list_rules` and `get_run` had zero callers outside
`tests/`. John could not write a standing rule through any product surface — the
only way to turn the feature on was hand-written SQL against `split_rules`, which
is also the one route that bypasses the over-100% guard.

"A mechanism built is not a mechanism wired" is the fifth instance of this in
this project, so this cog is deliberately boring: eight subcommands, no new money
logic and no new state. Every command is staff-only and every reply is ephemeral.

WHAT IT DOES NOT DO
-------------------
* It moves no coins. `/splits` writes RULES; the money is moved by the settle
  path and by the resume sweep, both of which already existed.
* It has no "run this split now" button. A split is triggered by an income
  event, not by an operator, and a manual fire would be an unkeyed second offer
  of somebody else's trigger.
* It never deletes a rule (`deactivate_rule` sets a flag). A rule that paid coins
  last week is the audit trail of why those coins moved.

THE FOUR RULES THIS SURFACE IS BUILT AROUND
-------------------------------------------
1. **Percentages are basis points, typed as integers.** 10000 = 100%. No float
   ever reaches a coin here, and a percent box that accepts `12.5` is how one
   arrives. The percentage is rendered from the integer (`_pct`) by division and
   remainder, never by `bps / 100`.

2. **The 100% cap is enforced HERE TOO, with the figures.** The authority is
   still the check inside `add_rule`'s `BEGIN IMMEDIATE` — that is what makes two
   concurrent admins safe, and nothing here weakens it. But a surface that lets
   an operator press a button and then shows them a raised exception has told
   them the number too late: `_cap_refusal` names the allocated total, the
   proposed share and the exact bps that are free, BEFORE the confirm dialog is
   even offered. Both layers, and the transaction is the one that decides.

3. **Anything irreversible shows the figures it is about to move, in the same
   view as the button.** A rule edit moves no coins today and every coin
   tomorrow, so the preview shows the CURRENT split and the PROPOSED split side
   by side, in percent and in coins per 10,000 of income, with the retained
   remainder as its own row. Nothing is written until Confirm is pressed.

4. **A rule edit bumps the ruleset version, and the operator is told what that
   means for money already in flight** — in words, on the confirm screen, every
   time. The engine pins the FIRST run minted for an income event and keeps
   executing that plan (`split_rules.run_split` point 3, `_run_for_trigger`): a
   sale already settled does not re-settle, and a run parked in `pending_funds`
   will still pay the plan it pinned, including a beneficiary retired since. That
   is the property `/splits remove` warns about by name, because it is the one
   that surprises people.

REAL NAMES, EVERYWHERE A HUMAN LOOKS
------------------------------------
`_display` renders a role as its role name, a user id as their display name (or
their IGN, via `panel_skus._member_name`) and a `treasury:*` account as what that
account is FOR. The raw id is kept beside the name rather than instead of it,
because the operator has to be able to type it back into `source:`.
"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import Any, Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
log = getattr(core, "log", None)

import split_rules  # noqa: E402

#: The account this exists for on day one: the land commission holding account.
#: Every command defaults to it so the common case is `/splits list`.
try:
    import land_settle as _ls
    DEFAULT_SOURCE = _ls.COMMISSION_SOURCE
except Exception:  # noqa: BLE001 — the cog must load even if land_settle does not
    DEFAULT_SOURCE = "treasury:estates"

#: What the house accounts are FOR, in words. A `treasury:*` id is not a name.
ACCOUNT_LABELS = {
    "treasury:estates": "Land commission holding",
    "treasury:vtech": "V Tech house account",
}

#: The worked example on every preview. Percentages are abstract; coins are not.
SAMPLE_INCOME = 10_000

_COLOR = 0x2B2D31
_COLOR_WARN = 0xF1C40F
_COLOR_BAD = 0xE74C3C


def _is_staff(interaction: discord.Interaction) -> bool:
    try:
        return bool(core.is_manager(interaction))
    except Exception:  # noqa: BLE001
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and (perms.administrator or perms.manage_guild))


def _pct(bps: int) -> str:
    """Basis points as a percentage string, without float arithmetic."""
    b = int(bps)
    sign = "-" if b < 0 else ""
    b = abs(b)
    return f"{sign}{b // 100}.{b % 100:02d}%"


def _display(kind: str, ref: str, *, guild=None) -> str:
    """A beneficiary as a human reads it. Falls back to the id, never to nothing."""
    ref = str(ref)
    if kind == "role":
        try:
            if guild is not None:
                role = guild.get_role(int(ref))
                if role is not None:
                    return f"@{role.name} (role)"
        except Exception:  # noqa: BLE001
            pass
        return f"role {ref}"
    label = ACCOUNT_LABELS.get(ref)
    if label:
        return label
    if ref.startswith("treasury:"):
        return ref.split(":", 1)[1].replace("_", " ").title() + " treasury"
    try:
        import panel_skus
        return panel_skus._member_name(guild, ref)
    except Exception:  # noqa: BLE001
        return f"user {ref}"


def _source_label(src: str) -> str:
    lbl = ACCOUNT_LABELS.get(src)
    return f"{lbl} (`{src}`)" if lbl else f"`{src}`"


async def _deny(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "Managers only — `/splits` writes the standing rules that route real coins.",
        ephemeral=True)


# ── The preview ─────────────────────────────────────────────────────────────
def _row_tuples(rules, *, guild=None) -> list[tuple[str, int]]:
    """[(display name, bps)] in the order the engine will apply them."""
    return [(_display(r["beneficiary_kind"], r["beneficiary_ref"], guild=guild),
             int(r["bps"])) for r in rules]


def _change_table(before: list[tuple[str, int]],
                  after: list[tuple[str, int]]) -> str:
    """Current split and proposed split, side by side, in one fixed-width block.

    He confirms figures, not intentions (the rule `cogs/rollback.py` was built
    around). The coins column is the one that makes a share concrete: what the
    row takes out of a 10,000-coin income event, floor-divided exactly as
    `plan_split` does it, so the number on the screen is the number the planner
    will compute.
    """
    names: list[str] = []
    for who, _ in before + after:
        if who not in names:
            names.append(who)
    b = {}
    a = {}
    for who, bps in before:
        b[who] = b.get(who, 0) + bps
    for who, bps in after:
        a[who] = a.get(who, 0) + bps
    names.append("— retained in source —")
    b[names[-1]] = 10000 - sum(x for _, x in before)
    a[names[-1]] = 10000 - sum(x for _, x in after)

    width = min(max([len(n) for n in names] + [8]), 26)
    out = ["{:<{w}}  {:>8}  {:>8}   {:>9}".format("Who", "now", "after", "coins/10k", w=width),
           "-" * (width + 32)]
    for n in names:
        nb, na = int(b.get(n, 0)), int(a.get(n, 0))
        cb = (SAMPLE_INCOME * nb) // 10000
        ca = (SAMPLE_INCOME * na) // 10000
        mark = "" if nb == na else "  <-"
        out.append("{:<{w}}  {:>8}  {:>8}   {:>4,} -> {:>4,}{}".format(
            n[:width], _pct(nb), _pct(na), cb, ca, mark, w=width))
    return "```\n" + "\n".join(out) + "\n```"


_PIN_NOTE = (
    "A rule edit bumps the **ruleset version**. The new rules govern the NEXT "
    "income event. An event that already has a split run keeps the plan it was "
    "pinned with and will not be re-planned or re-paid — including a run parked "
    "in `pending_funds`, which will still pay its pinned beneficiaries when the "
    "coins arrive. `/splits runs` lists anything currently parked."
)


def build_change_embed(src: str, before: list[tuple[str, int]],
                       after: list[tuple[str, int]], *, title: str,
                       what: str, warning: str = "",
                       policy: tuple[str, str] | None = None) -> discord.Embed:
    """The confirm screen. Rule 3: the figures live in the same view as the button."""
    e = discord.Embed(title=title, description=what,
                      colour=_COLOR_WARN if warning else _COLOR)
    e.add_field(name=f"Where {_source_label(src)} income goes",
                value=_change_table(before, after)[:1024], inline=False)
    if policy and policy[0] != policy[1]:
        e.add_field(name="Short-source policy",
                    value=f"**{policy[0]}** -> **{policy[1]}**", inline=False)
    if warning:
        e.add_field(name="⚠ Read this", value=warning[:1024], inline=False)
    e.add_field(name="What this does to money already in flight",
                value=_PIN_NOTE, inline=False)
    e.set_footer(text="Nothing has been written yet. This is a preview.")
    return e


def _cap_refusal(src: str, total_bps: int, want_bps: int) -> Optional[str]:
    """Rule 2. The surface's half of the 100% cap, with the figures named.

    Returns the refusal text, or None if it fits. The transaction inside
    `add_rule` remains the authority — this cannot be relied on for correctness
    under concurrency and does not try to be. It exists so the operator sees the
    arithmetic before they press anything.
    """
    free = 10000 - int(total_bps)
    if int(want_bps) <= free:
        return None
    return (f"Refused — that would take {_source_label(src)} past 100%.\n"
            f"Allocated now **{_pct(total_bps)}** · you asked for "
            f"**{_pct(want_bps)}** · free **{_pct(max(0, free))}** "
            f"({max(0, free)} bps).\n"
            f"Retire or shrink a rule first (`/splits list`, `/splits remove`).")


# ── The confirm view ────────────────────────────────────────────────────────
class ConfirmSplitChange(discord.ui.View):
    """Ephemeral, 120s, one writer. The write happens on Confirm, never on render.

    `interaction_check` pins the view to the operator who opened it: an ephemeral
    message is only visible to them anyway, but the check is what makes that a
    property of the code rather than of Discord's delivery.
    """

    def __init__(self, opener_id: int, apply: Callable[[], Any], *,
                 confirm_label: str = "Confirm"):
        super().__init__(timeout=120)
        self.opener_id = int(opener_id)
        self._apply = apply
        self.result: Optional[str] = None
        # The button's own claim. `result` alone cannot be it: it is not set
        # until the write RETURNS, and the whole defect is the window while the
        # write is still running. See `_press()`.
        self._pressed = False
        self.confirm.label = confirm_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0)) == self.opener_id:
            return True
        await interaction.response.send_message(
            "This preview belongs to whoever opened it.", ephemeral=True)
        return False

    def _press(self) -> bool:
        """CLAIM-FIRST, AT THE BUTTON — and read the rowcount.

        Discord dispatches every component interaction as its own task, so a
        double-click is TWO concurrent callbacks. Disabling the children after
        the write returns is a claim on the wrong side of the work: the second
        press arrives while the first is still inside `asyncio.to_thread`, finds
        a live button and no guard, and writes the same rule again — one intent,
        two rules, double the beneficiary's share of ALL future income.

        This is the same claim-first the write transaction below it already does,
        moved to the surface. The read and the write are in one synchronous block
        with **no await between them**, which on one event loop is atomic: of two
        concurrently dispatched presses exactly one gets True. Returning that
        boolean and acting on it is reading the rowcount.

        `_apply` is not idempotent and cannot be — `/splits add` carries no
        per-intent key, so two calls are two genuinely different rules. The guard
        has to be here.
        """
        if self._pressed or self.result is not None:
            return False
        self._pressed = True
        return True

    async def _already(self, interaction: discord.Interaction) -> None:
        """Tell the loser of the race the truth: this preview writes once."""
        msg = ("This preview has already been used — it writes **once**. "
               "`/splits list` shows what was written.")
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception:  # noqa: BLE001 — already responded/expired
            try:
                await interaction.followup.send(msg, ephemeral=True)
            except Exception:  # noqa: BLE001
                pass

    def _spent(self) -> discord.ui.View:
        for child in self.children:
            child.disabled = True
        self.stop()
        return self

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction,
                      button: discord.ui.Button):
        # BEFORE the defer. Every await is a place the second press can overtake.
        if not self._press():
            return await self._already(interaction)
        await interaction.response.defer(ephemeral=True)
        try:
            msg = await asyncio.to_thread(self._apply)
        except split_rules.SplitError as e:
            # A DEFINITE refusal: the guard inside the write transaction rolled
            # the whole thing back, so nothing was written. Say that, rather than
            # showing a traceback over an unstated outcome.
            msg = f"❌ Refused — **nothing was written**.\n{e}"
        except Exception as e:  # noqa: BLE001
            msg = (f"⚠ Failed: {e}\nRun `/splits list` before retrying — this "
                   f"outcome is not certain either way.")
        self.result = msg
        await interaction.edit_original_response(view=self._spent())
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        # Cancel takes the same claim. Without it, Cancel pressed while Confirm's
        # write is in flight overwrites `result` and tells the operator "nothing
        # was written" over a write that is landing.
        if not self._press():
            return await self._already(interaction)
        self.result = "Cancelled — nothing was written."
        await interaction.response.edit_message(view=self._spent())
        await interaction.followup.send(self.result, ephemeral=True)


class SplitsCog(commands.Cog):
    """Standing split rules: who gets what share of an income account."""

    def __init__(self, bot):
        self.bot = bot

    splits = app_commands.Group(
        name="splits",
        description="Standing split rules — route an income account by percentage",
    )

    # ── read ────────────────────────────────────────────────────────────
    @splits.command(name="list", description="Show the standing rules on an account")
    @app_commands.describe(source="Account the rules read FROM (default: land commission)")
    async def splits_list(self, interaction: discord.Interaction,
                          source: Optional[str] = None):
        if not _is_staff(interaction):
            return await _deny(interaction)
        src = (source or DEFAULT_SOURCE).strip()
        await interaction.response.defer(ephemeral=True)
        try:
            rs = await asyncio.to_thread(split_rules.list_rules, src)
        except Exception as e:  # noqa: BLE001
            return await interaction.followup.send(f"Could not read `{src}`: {e}",
                                                   ephemeral=True)
        guild = interaction.guild
        emb = discord.Embed(
            title=f"Split rules · {ACCOUNT_LABELS.get(src, src)}",
            description=(f"`{src}`\npolicy **{rs['short_policy']}** · ruleset "
                         f"**v{rs['version']}** · allocated "
                         f"**{_pct(rs['total_bps'])}** · retained "
                         f"**{_pct(rs['retained_bps'])}**"),
            colour=_COLOR)
        if not rs["rules"]:
            emb.add_field(
                name="No rules — nothing is routed",
                value=("This account behaves exactly as it did before the split "
                       "engine existed: the commission stays where it lands and "
                       "no split run is ever minted. `/splits add` turns it on."),
                inline=False)
        for position, r in enumerate(rs["rules"], start=1):
            who = _display(r["beneficiary_kind"], r["beneficiary_ref"], guild=guild)
            bits = [f"{who} · `{r['beneficiary_ref']}`"]
            if r["label"]:
                bits.append(str(r["label"]))
            if int(r["floor_coins"] or 0):
                bits.append(f"skipped below {int(r['floor_coins']):,} coins")
            bits.append(f"{(SAMPLE_INCOME * int(r['bps'])) // 10000:,} coins per "
                        f"{SAMPLE_INCOME:,} of income")
            emb.add_field(name=f"{position}. #{r['id']} · {_pct(r['bps'])}",
                          value="\n".join(bits)[:1024], inline=False)
        emb.set_footer(text="Order matters: under the `prorate` policy the LAST rule "
                            "absorbs the remainder of a short run. /splits reorder.")
        await interaction.followup.send(embed=emb, ephemeral=True)

    # ── write ───────────────────────────────────────────────────────────
    @splits.command(name="add", description="Add one standing rule (basis points)")
    @app_commands.describe(
        bps="Share in BASIS POINTS — 10000 = 100%, 2500 = 25%",
        account="Pay a ledger account (a user id or `treasury:*`)",
        role="…or share the leg evenly between the holders of this role",
        source="Account the coins come FROM (default: land commission)",
        floor_coins="Skip this leg entirely if its share would be under this many coins",
        label="What this rule is for, in words")
    async def splits_add(self, interaction: discord.Interaction, bps: int,
                         account: Optional[str] = None,
                         role: Optional[discord.Role] = None,
                         source: Optional[str] = None,
                         floor_coins: int = 0, label: str = ""):
        if not _is_staff(interaction):
            return await _deny(interaction)
        if bool(account) == bool(role):
            return await interaction.response.send_message(
                "Give exactly one beneficiary: `account` **or** `role`.", ephemeral=True)
        if not 1 <= int(bps) <= 10000:
            return await interaction.response.send_message(
                f"`bps` must be 1..10000 — that is 0.01% to 100%. You gave {bps}.",
                ephemeral=True)
        kind = "role" if role else "account"
        ref = str(role.id) if role else str(account).strip()
        src = (source or DEFAULT_SOURCE).strip()
        b = int(bps)
        fl = max(0, int(floor_coins))
        await interaction.response.defer(ephemeral=True)
        try:
            rs = await asyncio.to_thread(split_rules.list_rules, src)
        except Exception as e:  # noqa: BLE001
            return await interaction.followup.send(f"Could not read `{src}`: {e}",
                                                   ephemeral=True)
        refusal = _cap_refusal(src, int(rs["total_bps"]), b)
        if refusal:
            return await interaction.followup.send(refusal, ephemeral=True)

        guild = interaction.guild
        who = _display(kind, ref, guild=guild)
        before = _row_tuples(rs["rules"], guild=guild)
        after = before + [(who, b)]
        # A new rule goes LAST, so it cannot silently take over the remainder
        # absorption from a rule that already had it. `/splits reorder` moves it.
        seq = max([int(r["seq"]) for r in rs["rules"]] + [0]) + 1

        warn = ""
        if b < 100:
            warn = (f"**{b} bps is {_pct(b)}** — under one percent. If you meant "
                    f"{b}%, that is `{b * 100}` bps. Cancel and re-enter if so.")
        if kind == "role":
            warn = (warn + "\n" if warn else "") + (
                "A **role** leg is shared evenly between whoever holds the role at "
                "the moment the event is planned, and the plan is then pinned. If "
                "the bot cannot enumerate the role it refuses the run rather than "
                "guessing — no coins move.")

        def apply():
            res = split_rules.add_rule(src, kind, ref, b, seq=seq, floor_coins=fl,
                                       label=label,
                                       created_by=str(interaction.user.id))
            return (f"✅ Rule **#{res['rule_id']}** added on {_source_label(src)}: "
                    f"{who} takes **{_pct(res['bps'])}** of every income event, "
                    f"applied last. Allocated now **{_pct(res['total_bps'])}**, "
                    f"ruleset **v{res['ruleset_version']}**.\n"
                    f"Events already settled keep the plan they were settled under.")

        view = ConfirmSplitChange(interaction.user.id, apply,
                                  confirm_label=f"Add rule — {_pct(b)} to {who}"[:80])
        floor_line = (f" Skipped entirely if its share is under "
                      f"{fl:,} coins." if fl else "")
        emb = build_change_embed(
            src, before, after, title="Confirm — add a standing rule",
            what=(f"**{who}** (`{ref}`) would take **{_pct(b)}** of every income "
                  f"event booked to {_source_label(src)}, applied **last**."
                  f"{floor_line}"),
            warning=warn)
        await interaction.followup.send(embed=emb, view=view, ephemeral=True)

    @splits.command(name="remove", description="Retire a rule (a flag, never a delete)")
    @app_commands.describe(rule_id="The #id shown by /splits list",
                           source="Account the rule is on (default: land commission)")
    async def splits_remove(self, interaction: discord.Interaction, rule_id: int,
                            source: Optional[str] = None):
        if not _is_staff(interaction):
            return await _deny(interaction)
        src = (source or DEFAULT_SOURCE).strip()
        await interaction.response.defer(ephemeral=True)
        try:
            rs = await asyncio.to_thread(split_rules.list_rules, src)
        except Exception as e:  # noqa: BLE001
            return await interaction.followup.send(f"Could not read `{src}`: {e}",
                                                   ephemeral=True)
        target = next((r for r in rs["rules"] if int(r["id"]) == int(rule_id)), None)
        if target is None:
            return await interaction.followup.send(
                f"Rule #{rule_id} is not an active rule on {_source_label(src)}. "
                f"`/splits list` shows the ones that are.", ephemeral=True)
        guild = interaction.guild
        who = _display(target["beneficiary_kind"], target["beneficiary_ref"],
                       guild=guild)
        before = _row_tuples(rs["rules"], guild=guild)
        after = _row_tuples([r for r in rs["rules"] if int(r["id"]) != int(rule_id)],
                            guild=guild)

        def apply():
            won = split_rules.deactivate_rule(int(rule_id),
                                              by=str(interaction.user.id))
            if not won:
                return (f"Rule #{rule_id} was already retired by someone else — "
                        f"nothing changed.")
            return (f"✅ Rule **#{rule_id}** retired. {who} takes nothing from "
                    f"{_source_label(src)} from the next income event on. The row "
                    f"stays in the table because it explains coins it has already "
                    f"moved.")

        view = ConfirmSplitChange(interaction.user.id, apply,
                                  confirm_label=f"Retire #{rule_id} — {who}"[:80])
        emb = build_change_embed(
            src, before, after, title="Confirm — retire a standing rule",
            what=(f"Rule **#{rule_id}**: **{who}** (`{target['beneficiary_ref']}`) "
                  f"stops taking **{_pct(target['bps'])}**. Those coins stay in "
                  f"{_source_label(src)} unless another rule claims them."),
            warning=("A run already parked in `pending_funds` keeps its PINNED plan "
                     f"and can still pay {who} once, when the coins arrive. That is "
                     "deliberate — it is a payment that was already planned, not a "
                     "new one — but check `/splits runs` if it matters."))
        await interaction.followup.send(embed=emb, view=view, ephemeral=True)

    @splits.command(name="reorder", description="Change the order the rules apply in")
    @app_commands.describe(
        order="Rule ids, best first, e.g. `4 1 7` — every active rule, no gaps",
        source="Account the rules are on (default: land commission)")
    async def splits_reorder(self, interaction: discord.Interaction, order: str,
                             source: Optional[str] = None):
        if not _is_staff(interaction):
            return await _deny(interaction)
        src = (source or DEFAULT_SOURCE).strip()
        raw = str(order).replace(",", " ").split()
        try:
            ids = [int(x) for x in raw]
        except ValueError:
            return await interaction.response.send_message(
                f"`order` is a list of rule ids, like `4 1 7`. Could not read "
                f"{order!r}.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            rs = await asyncio.to_thread(split_rules.list_rules, src)
        except Exception as e:  # noqa: BLE001
            return await interaction.followup.send(f"Could not read `{src}`: {e}",
                                                   ephemeral=True)
        live = [int(r["id"]) for r in rs["rules"]]
        if sorted(ids) != sorted(live):
            return await interaction.followup.send(
                f"Give the COMPLETE order for {_source_label(src)}. Active rules: "
                f"`{' '.join(str(i) for i in live) or '(none)'}` · you gave "
                f"`{' '.join(str(i) for i in ids) or '(none)'}`.\n"
                f"A partial order would leave the rules you left out on whatever "
                f"position they had, which is not an order anybody chose.",
                ephemeral=True)
        guild = interaction.guild
        by_id = {int(r["id"]): r for r in rs["rules"]}
        before = _row_tuples(rs["rules"], guild=guild)
        after = _row_tuples([by_id[i] for i in ids], guild=guild)
        last_now = _display(rs["rules"][-1]["beneficiary_kind"],
                            rs["rules"][-1]["beneficiary_ref"], guild=guild)
        last_after = _display(by_id[ids[-1]]["beneficiary_kind"],
                              by_id[ids[-1]]["beneficiary_ref"], guild=guild)

        def apply():
            res = split_rules.reorder_rules(src, ids, by=str(interaction.user.id))
            return (f"✅ Order set: `{' '.join(str(i) for i in res['order'])}`. "
                    f"Ruleset **v{res['ruleset_version']}**. **{last_after}** is "
                    f"now last and absorbs the odd coins when the source is short "
                    f"under `prorate`.")

        view = ConfirmSplitChange(interaction.user.id, apply,
                                  confirm_label="Confirm new order")
        emb = build_change_embed(
            src, before, after, title="Confirm — re-order the rules",
            what=(f"New order: `{' '.join(str(i) for i in ids)}`.\nThe percentages "
                  f"do not change; the ORDER does, and the order decides who "
                  f"absorbs the remainder when the source cannot fund everyone."),
            warning=(f"Last rule now: **{last_now}** -> **{last_after}**. Under the "
                     f"`prorate` policy the last rule takes the leftover coins of a "
                     f"short run; under `strict` nothing is paid at all, so the "
                     f"order only shows up in the leg numbering."))
        await interaction.followup.send(embed=emb, view=view, ephemeral=True)

    @splits.command(name="policy", description="What happens when the source is short")
    @app_commands.describe(policy="strict = pay nobody · prorate = scale everyone · "
                                  "defer = park and retry",
                           source="Account the rules read FROM")
    @app_commands.choices(policy=[
        app_commands.Choice(name="strict — refuse the whole run, move nothing", value="strict"),
        app_commands.Choice(name="prorate — scale every leg to the coins present", value="prorate"),
        app_commands.Choice(name="defer — park and let the sweep retry", value="defer"),
    ])
    async def splits_policy(self, interaction: discord.Interaction,
                            policy: app_commands.Choice[str],
                            source: Optional[str] = None):
        if not _is_staff(interaction):
            return await _deny(interaction)
        src = (source or DEFAULT_SOURCE).strip()
        want = policy.value if hasattr(policy, "value") else str(policy)
        await interaction.response.defer(ephemeral=True)
        try:
            rs = await asyncio.to_thread(split_rules.list_rules, src)
        except Exception as e:  # noqa: BLE001
            return await interaction.followup.send(f"Could not read `{src}`: {e}",
                                                   ephemeral=True)
        now = str(rs["short_policy"])
        if now == want:
            return await interaction.followup.send(
                f"{_source_label(src)} is already **{now}** — nothing to change.",
                ephemeral=True)
        rows = _row_tuples(rs["rules"], guild=interaction.guild)
        meaning = {
            "strict": ("Nobody is paid when the source cannot fund the whole plan. "
                       "The run refuses and the coins stay put."),
            "prorate": ("Every leg is scaled down to the coins actually present, the "
                        "last rule absorbs the remainder, and the difference is "
                        "recorded as `shortfall_coins` so you can see who was "
                        "underpaid and by how much."),
            "defer": ("The run parks in `pending_funds` and the sweep retries it "
                      "every minute, forever. Only correct on an account somebody "
                      "actually tops up — anywhere else it is a run that never "
                      "completes."),
        }

        def apply():
            p = split_rules.set_short_policy(src, want,
                                             note=f"set by {interaction.user}")
            return (f"✅ {_source_label(src)} short-source policy is now **{p}**. "
                    f"This governs income events booked from now on.")

        view = ConfirmSplitChange(interaction.user.id, apply,
                                  confirm_label=f"Set policy to {want}")
        emb = build_change_embed(
            src, rows, rows, title="Confirm — change the short-source policy",
            what=f"**{want}** — {meaning[want]}",
            policy=(now, want))
        await interaction.followup.send(embed=emb, view=view, ephemeral=True)

    # ── runs ────────────────────────────────────────────────────────────
    @splits.command(name="runs", description="Split runs a human should look at")
    @app_commands.describe(older_than_minutes="Only flag claimed/pending runs older than this")
    async def splits_runs(self, interaction: discord.Interaction,
                          older_than_minutes: int = 15):
        """Two lists, because they are two different situations.

        STUCK is `split_rules.stuck_runs` — ambiguous or abandoned, the sweep
        cannot finish them and a human must. PARKED is `split_rules.parked_runs`
        — `pending_funds` runs doing exactly what the `defer` policy told them
        to, waiting on a top-up. Merging them would either raise a false alarm
        every five minutes over a healthy run, or bury a real one; and before
        this command existed, `pending_funds` had no surface of any kind.
        """
        if not _is_staff(interaction):
            return await _deny(interaction)
        await interaction.response.defer(ephemeral=True)
        window = float(max(0, older_than_minutes)) * 60.0
        try:
            stuck = await asyncio.to_thread(split_rules.stuck_runs, window)
            parked = await asyncio.to_thread(split_rules.parked_runs, 0.0)
            unrouted = await asyncio.to_thread(split_rules.unrouted_runs)
        except Exception as e:  # noqa: BLE001
            return await interaction.followup.send(f"Failed: {e}", ephemeral=True)
        if not stuck and not parked and not unrouted:
            return await interaction.followup.send(
                "No parked or stuck split runs, and no income event whose "
                "commission went unrouted. (`unknown` runs, runs claimed for "
                "longer than the window, runs waiting on funds, and events whose "
                "only run refused would all be listed here.)", ephemeral=True)

        def _acct(a):
            return ACCOUNT_LABELS.get(a, a)

        emb = discord.Embed(
            title=(f"Split runs · {len(stuck)} stuck · {len(parked)} parked · "
                   f"{len(unrouted)} unrouted"),
            colour=_COLOR_BAD if (stuck or unrouted) else _COLOR_WARN)
        for r in stuck[:10]:
            emb.add_field(
                name=(f"⛔ {r['state']} · {int(r['amount_in']):,} from "
                      f"{_acct(r['source_account'])}"),
                value=(f"`{r['run_id']}`\nattempts {r['attempts']} · "
                       f"{r['reason'] or 'no reason recorded'}"),
                inline=False)
        for r in parked[:10]:
            mins = max(0, int((time.time() - float(r["created_at"])) // 60))
            emb.add_field(
                name=(f"⏳ waiting for coins · {int(r['amount_in']):,} from "
                      f"{_acct(r['source_account'])}"),
                value=(f"`{r['run_id']}`\n{r['trigger_kind']} "
                       f"#{r['trigger_row_id']} · parked {mins:,} min · attempts "
                       f"{r['attempts']}\nIt pays its PINNED plan the moment the "
                       f"source can fund it — top the account up, or `/splits run` "
                       f"to see exactly who it will pay."),
                inline=False)
        for r in unrouted[:10]:
            emb.add_field(
                name=(f"🚫 unrouted · {int(r['amount_in']):,} from "
                      f"{_acct(r['source_account'])}"),
                value=(f"`{r['run_id']}`\n{r['trigger_kind']} "
                       f"#{r['trigger_row_id']} · refused: "
                       f"{r['reason'] or 'no reason recorded'}\n"
                       f"**No coins moved** — the {int(r['amount_in']):,} is still "
                       f"in {_acct(r['source_account'])}. Fix what refused it "
                       f"(`/splits list`), and the NEXT offer of this event plans "
                       f"it again from scratch; a settled lot is offered once, so "
                       f"this one needs you."),
                inline=False)
        emb.set_footer(text="⛔ needs a human. ⏳ is the `defer` policy working — "
                            "unless nobody is going to top that account up. "
                            "🚫 refused, nothing moved, nobody paid.")
        await interaction.followup.send(embed=emb, ephemeral=True)

    @splits.command(name="run", description="One split run, with its legs")
    @app_commands.describe(run_id="The split:… id from /splits runs")
    async def splits_run(self, interaction: discord.Interaction, run_id: str):
        if not _is_staff(interaction):
            return await _deny(interaction)
        await interaction.response.defer(ephemeral=True)
        try:
            row = await asyncio.to_thread(split_rules.get_run, run_id.strip())
        except Exception as e:  # noqa: BLE001
            return await interaction.followup.send(f"Failed: {e}", ephemeral=True)
        if row is None:
            return await interaction.followup.send(
                f"No run `{run_id}`. Note that a settle that never minted a row has "
                f"nothing here — that is a REFUSAL, not a pending run.", ephemeral=True)
        guild = interaction.guild
        emb = discord.Embed(
            title=f"{row['state']} · {row['trigger_kind']} #{row['trigger_row_id']}",
            description=(f"`{row['run_id']}`\n"
                         f"in **{int(row['amount_in']):,}** · allocated "
                         f"**{int(row['allocated']):,}** · shortfall "
                         f"**{int(row['shortfall_coins']):,}**\n"
                         f"source {_source_label(row['source_account'])} · ruleset v"
                         f"{row['ruleset_version']} · policy {row['short_policy']} · "
                         f"attempts {row['attempts']}"),
            colour=_COLOR if row["state"] == "applied" else _COLOR_BAD)
        if row.get("reason"):
            emb.add_field(name="reason", value=str(row["reason"])[:1000], inline=False)
        legs = row.get("leg_rows") or []
        if legs:
            emb.add_field(
                name=f"{len(legs)} leg(s)",
                value="\n".join(
                    f"{_display(l['kind'], l['to_account'], guild=guild)} "
                    f"— {int(l['amount']):,} · {l['state']}"
                    for l in legs[:20])[:1024],
                inline=False)
        emb.set_footer(text="A run pins its plan. These legs are what it will pay, "
                            "whatever the rules say now.")
        await interaction.followup.send(embed=emb, ephemeral=True)

    @splits.command(name="source", description="Which account /splits defaults to")
    async def splits_source(self, interaction: discord.Interaction):
        if not _is_staff(interaction):
            return await _deny(interaction)
        await interaction.response.send_message(
            f"Default source account: {_source_label(DEFAULT_SOURCE)} — where a land "
            f"sale's commission sits after capture. Pass `source:` to any `/splits` "
            f"command to configure a different income account (a hive float, a "
            f"market's takings).", ephemeral=True)


async def setup(bot):
    await bot.add_cog(SplitsCog(bot))
