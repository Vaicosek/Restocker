"""MarketSettings — one panel replacing the 17 /market subcommands.

Every action reproduces its command's logic, INCLUDING the guards that matter:

* go_public — a manager-supplied launch price may not exceed 2x the computed
  fundamental unless a full server manager sets it. Without that cap a site manager
  could list their own market at 1,000,000/share and sell into the treasury.
* treasury_withdraw — only `treasury - (shares held x price)` is withdrawable. The
  subtraction is the buyback cover; dropping it drains the money backing shares.
* loyalty — owners are capped (coin_bonus 5,000 / 50% / 3x); only a full manager
  may exceed them, or an owner could mint coins to an accomplice on a 1-item order.
* set_code — never touches leader_discord_id, unlike /market_code.
* delete — requires the market id typed back.

Ephemeral, owner-locked, 5-minute timeout: it moves coins and destroys markets.
"""
import sys

import discord

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
log = core.log
MIN_SHARE_PRICE = getattr(core, "MIN_SHARE_PRICE", 0.01)


def _db():
    import Restocker_db as _d
    return _d


def _mk(mid):
    return core._get_market(mid) or {}


def _is_full_manager(user) -> bool:
    try:
        return bool(core._ai_is_manager(user))
    except Exception:
        return False


def _is_owner(user, mid) -> bool:
    try:
        own = core._market_owner_id(mid)
        return bool(own) and int(own) == int(getattr(user, "id", 0) or 0)
    except Exception:
        return False


def _may_manage(user, mid) -> bool:
    """Full manager, the market's owner, or one of its site managers."""
    if _is_full_manager(user) or _is_owner(user, mid):
        return True
    try:
        return int(getattr(user, "id", 0)) in (_mk(mid).get("manager_ids") or [])
    except Exception:
        return False


def _leads_by_role(user, mid) -> bool:
    """Holds the market's configured leader ROLE. /market_code gated on this rather than
    on manager_ids, so a shop leader who was never registered as a site manager could
    still fetch their code. Folding that command in without honouring the role check
    would have locked exactly those people out."""
    role_name = (_mk(mid).get("discord_role_name") or "").strip()
    if not role_name:
        return False
    try:
        return any(r.name == role_name for r in getattr(user, "roles", []) or [])
    except Exception:
        return False


def _may_view(user, mid) -> bool:
    """Can OPEN the panel. Leader-role holders get in but see no management buttons."""
    return _may_manage(user, mid) or _leads_by_role(user, mid)


async def build_embed(mid: str, user=None) -> discord.Embed:
    d = _db()
    m = _mk(mid)
    e = discord.Embed(title=f"🏪 MarketSettings — {m.get('name', mid)} [{mid}]",
                      color=0x3498DB if m.get("active", True) else 0x95A5A6)
    owner = m.get("owner_id")
    mgrs = m.get("manager_ids") or []
    e.add_field(name="Owner", value=(f"<@{owner}>" if owner else "*unset*"), inline=True)
    e.add_field(name="Status", value=("🟢 active" if m.get("active", True) else "🔴 inactive"), inline=True)
    e.add_field(name="Fee", value=f"`{m.get('platform_fee_pct', 0)}%`", inline=True)
    e.add_field(name="Code", value=f"`{m.get('leader_code') or '—'}`", inline=True)
    rc = m.get("report_channel_id")
    e.add_field(name="Channel", value=(f"<#{rc}>" if rc else "*unbound*"), inline=True)
    e.add_field(name="Site managers",
                value=(", ".join(f"<@{u}>" for u in mgrs[:8]) if mgrs else "*none*"), inline=True)

    try:
        tickers = core.load_yaml("market_tickers.yml", {}) or {}
    except Exception:
        tickers = {}
    listing = d.get_market_shares(mid)
    if listing and listing.get("active"):
        held = sum(float(h.get("shares") or 0) for h in (d.get_holders(mid) or []))
        price = float(listing.get("share_price") or 0)
        tre = float(listing.get("treasury_coins") or 0)
        excess = max(0.0, tre - held * price)
        e.add_field(name="📈 Listed",
                    value=(f"`{tickers.get(mid, '—')}` · `{price:,.2f}`/share · "
                           f"`{float(listing.get('shares_outstanding') or 0):,.0f}` outstanding\n"
                           f"treasury `{tre:,.0f}` · held `{held:,.0f}` shares · "
                           f"**withdrawable `{excess:,.0f}`**"),
                    inline=False)
    else:
        e.add_field(name="📈 Listing", value="*not public*", inline=False)

    try:
        pm, cb, pct = core._market_loyalty_cfg(mid)
        bits = []
        if pm != 1.0:
            bits.append(f"{pm:g}x pts")
        if cb:
            bits.append(f"+{cb:,}c/order")
        if pct:
            bits.append(f"+{pct:g}%/order")
        e.add_field(name="Restock rewards", value=(" · ".join(bits) or "normal"), inline=False)
    except Exception:
        pass

    # Vault line — this is all /vault status showed for one listing, so folding it here
    # retired that subcommand outright rather than giving it a button.
    try:
        due = float(d.get_config(f"vault_due:{mid}") or 0)
        bal = float(d.get_config(f"vault_bal:{mid}") or 0)
        raw = float(d.get_config(f"vault_pledged:{mid}") or 0)
        if due or bal or raw:
            hc = getattr(core, "VAULT_PLEDGE_HAIRCUT", 70.0)
            st = "✅ current" if bal >= due - 1 else f"⚠ arrears `{int(due-bal):,}` (grade capped BBB)"
            e.add_field(name="🏦 Vault",
                        value=(f"due `{int(due):,}` · deposited `{int(bal):,}` · pledged "
                               f"`{int(raw):,}` (counts `{int(raw*hc/100):,}` at {hc:g}%)\n{st}"),
                        inline=False)
    except Exception as ex:
        log.debug("[market panel] vault line failed: %s", ex)

    if user is not None and _may_manage(user, mid):
        try:
            ch = core.bot.get_channel(int(rc)) if rc else None
            hook = await core._csn_webhook_for(ch, m.get("name", mid)) if ch else None
            if hook:
                e.add_field(name="CSN webhook", value=f"||{hook}||", inline=False)
        except Exception:
            pass
    e.set_footer(text="Only this market's owner, its site managers, or a server manager can change things.")
    return e


class _LocationModal(discord.ui.Modal, title="Delivery location"):
    """Was /market_set_location. Blank clears the override back to /la spawn <mid>."""
    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel
        self.loc = discord.ui.TextInput(
            label="Where workers deliver", required=False, max_length=100,
            placeholder=f"/la spawn {panel.mid}  (blank = reset to default)",
            default=(_db().get_config(f"sell_loc:{panel.mid}") or ""))
        self.add_item(self.loc)

    async def on_submit(self, i: discord.Interaction):
        d = _db()
        loc = str(self.loc.value or "").strip()[:100]
        if not loc:
            d.delete_config(f"sell_loc:{self.panel.mid}")
            return await self.panel.refresh(
                i, f"✅ Delivery location reset to the default `/la spawn {self.panel.mid}`.")
        d.set_config(f"sell_loc:{self.panel.mid}", loc)
        await self.panel.refresh(
            i, f"✅ Workers are now told to deliver to `{loc}` — shows on order cards, "
               f"`/orders`, and the website.")




class _RegisterModal(discord.ui.Modal, title="Register a new market"):
    """Was /market add — full server managers only, same as the command."""
    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel
        self.mid = discord.ui.TextInput(label="Market id (a-z 0-9 _ -)", required=True, max_length=32)
        self.name = discord.ui.TextInput(label="Display name", required=True, max_length=64)
        self.owner = discord.ui.TextInput(label="Owner Discord user id (optional)", required=False)
        self.fee = discord.ui.TextInput(label="Platform fee % (default 3)", required=False, default="3")
        # WITHOUT this, the market has no leader role and nobody can ever fetch its CSN
        # code — the old /market add omitted it, which is why /create_market existed.
        self.role = discord.ui.TextInput(
            label="Leader role name (for CSN code access)", required=False,
            placeholder="e.g. Goldmart Leader")
        self.add_item(self.mid); self.add_item(self.name)
        self.add_item(self.owner); self.add_item(self.fee); self.add_item(self.role)

    async def on_submit(self, i: discord.Interaction):
        import re as _re
        mid = str(self.mid.value or "").strip().lower()
        if not _re.match(r"^[a-z0-9_-]{1,32}$", mid):
            return await i.response.send_message(
                "❌ Market id must be lowercase letters, digits, hyphens or underscores (max 32).",
                ephemeral=True)
        data = core._load_markets()
        markets = data.setdefault("markets", {})
        if mid in markets:
            return await i.response.send_message(
                f"❌ Market `{mid}` already exists — pick it in the selector above.", ephemeral=True)
        raw = str(self.owner.value or "").strip().strip("<@!>")
        try:
            fee = round(max(0.0, min(50.0, float(str(self.fee.value or "3").strip() or 3))), 4)
        except Exception:
            return await i.response.send_message("❌ Fee must be a number.", ephemeral=True)
        markets[mid] = {
            "name": str(self.name.value).strip(),
            "owner_id": int(raw) if raw.isdigit() else None,
            "manager_ids": [],
            "discord_role_name": str(self.role.value or "").strip(),
            "leader_discord_id": None,
            "leader_code": None,
            "platform_fee_pct": fee,
            "csn_history_file": (core.CSN_HISTORY_FILE if mid == core.DEFAULT_MARKET_ID
                                 else f"csn_history_{mid}.yml"),
            "active": True,
            "created_at": core.utcnow_iso(),
            "created_by": i.user.id,
        }
        core._save_markets(data)
        self.panel.mid = mid
        await self.panel.refresh(
            i, f"✅ Registered **{markets[mid]['name']}** (`{mid}`) at {fee:g}% fee. "
               f"Assign the leader role, then they open this panel and hit **Get CSN code**."
               + ("" if str(self.role.value or "").strip() else
                  "\n⚠️ No leader role set — only managers will be able to fetch its code."))


async def _rotate_code(panel, i: discord.Interaction) -> str:
    """Was /market_code. Reproduces the ownership guard verbatim: leader_discord_id gates
    market-OWNERSHIP rights, so once a leader is on record only a full manager may move
    it. Everyone else just rotates the code without touching ownership."""
    import secrets, string as _s
    mid = panel.mid
    data = core._load_markets()
    markets = data.get("markets") or {}
    if mid not in markets:
        return f"❌ Market `{mid}` not found."
    code = "".join(secrets.choice(_s.ascii_uppercase + _s.digits) for _ in range(10))
    existing = str(markets[mid].get("leader_discord_id") or "").strip()
    if not existing:
        markets[mid]["leader_discord_id"] = str(i.user.id)
    markets[mid]["leader_code"] = code
    core._save_markets(data)
    csn_file = markets[mid].get("csn_history_file") or f"csn_history_{mid}.yml"
    return (f"🔑 **{markets[mid].get('name', mid)}**\n"
            f"**Market ID:** `{mid}`\n**Code:** ||`{code}`||\n\n"
            f"Enter both in the **CSN Export Settings** screen in-game.\n"
            f"📁 Sales record to `{csn_file}`\n"
            f"⚠️ This replaced the previous code — the old one no longer works.")


class _BindModal(discord.ui.Modal, title="Bind a channel"):
    """Was /bind_market + /unbind_market. Keeps the one-channel-one-market check: two
    markets sharing a report channel makes CSN routing ambiguous."""
    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel
        self.ch = discord.ui.TextInput(
            label="Channel id (blank = UNBIND)", required=False,
            placeholder="right-click the channel → Copy Channel ID")
        self.add_item(self.ch)

    async def on_submit(self, i: discord.Interaction):
        mid = self.panel.mid
        data = core._load_markets()
        markets = data.get("markets") or {}
        raw = str(self.ch.value or "").strip().strip("<#>")
        if not raw:
            markets[mid]["report_channel_id"] = None
            core._save_markets(data)
            try:
                import Restocker_db as _d
                with _d.db() as conn:
                    conn.execute("UPDATE markets SET report_channel_id=NULL WHERE market_id=?", (mid,))
            except Exception as ex:
                log.error("[market panel] unbind db write failed: %s", ex)
            return await self.panel.refresh(
                i, "✅ Unbound. This market falls back to the in-game verification code.")
        if not raw.isdigit():
            return await i.response.send_message("❌ That isn't a channel id.", ephemeral=True)
        for other, m in markets.items():
            if other != mid and str(m.get("report_channel_id") or "") == raw:
                return await i.response.send_message(
                    f"❌ <#{raw}> is already bound to `{other}`. Unbind it there first.",
                    ephemeral=True)
        markets[mid]["report_channel_id"] = raw
        core._save_markets(data)
        await self.panel.refresh(
            i, f"✅ CSN reports in <#{raw}> now record to **{markets[mid].get('name', mid)}**. "
               f"No in-game Market Code is needed for this market anymore.")


# ── modals ───────────────────────────────────────────────────────────────────
class _EditModal(discord.ui.Modal, title="Edit market"):
    def __init__(self, p):
        super().__init__(timeout=300)
        self.p = p
        m = _mk(p.mid)
        self.name = discord.ui.TextInput(label="Display name", required=False,
                                         default=str(m.get("name") or ""))
        self.fee = discord.ui.TextInput(label="Platform fee % (0-50)", required=False,
                                        default=str(m.get("platform_fee_pct") or ""))
        self.active = discord.ui.TextInput(label="Active? yes/no", required=False,
                                           default="yes" if m.get("active", True) else "no")
        self.land = discord.ui.TextInput(label="Land claim to bind (optional)", required=False)
        for f in (self.name, self.fee, self.active, self.land):
            self.add_item(f)

    async def on_submit(self, i: discord.Interaction):
        data = core._load_markets()
        markets = data.get("markets") or {}
        if self.p.mid not in markets:
            return await i.response.send_message("❌ Market vanished.", ephemeral=True)
        mkt = markets[self.p.mid]
        ch = []
        if str(self.name.value).strip():
            mkt["name"] = str(self.name.value).strip(); ch.append("name")
        fee = str(self.fee.value).strip()
        if fee:
            try:
                f = float(fee)
            except Exception:
                return await i.response.send_message("❌ Fee must be a number.", ephemeral=True)
            if not (0 <= f <= 50):
                return await i.response.send_message("❌ Fee must be 0-50.", ephemeral=True)
            mkt["platform_fee_pct"] = round(f, 4); ch.append("fee")
        act = str(self.active.value).strip().lower()
        if act in ("yes", "y", "true", "1", "no", "n", "false", "0"):
            mkt["active"] = act in ("yes", "y", "true", "1"); ch.append("active")
        core._save_markets(data)
        lname = str(self.land.value).strip()
        if lname:
            d = _db()
            d.set_config(f"land_map:{lname.lower()}", self.p.mid)
            snap = d.get_land_balance(lname)
            if snap:
                d.upsert_market_shares(self.p.mid, treasury_coins=float(snap["balance"]))
                core._recompute_share_price(self.p.mid, reason="land_treasury")
            ch.append(f"land={lname}")
        await self.p.refresh(i, "✅ Updated: " + (", ".join(ch) or "nothing"))


class _LoyaltyModal(discord.ui.Modal, title="Restock rewards"):
    def __init__(self, p):
        super().__init__(timeout=300)
        self.p = p
        self.mult = discord.ui.TextInput(label="Loyalty x points (1 = normal)", required=False, default="1")
        self.coin = discord.ui.TextInput(label="Coin bonus per order", required=False, default="0")
        self.pct = discord.ui.TextInput(label="% of order value", required=False, default="0")
        for f in (self.mult, self.coin, self.pct):
            self.add_item(f)

    async def on_submit(self, i: discord.Interaction):
        try:
            pm = float(str(self.mult.value or 1) or 1)
            cb = int(float(str(self.coin.value or 0) or 0))
            pc = float(str(self.pct.value or 0) or 0)
        except Exception:
            return await i.response.send_message("❌ All three must be numbers.", ephemeral=True)
        if pm <= 0 or cb < 0 or pc < 0:
            return await i.response.send_message("❌ Values can't be negative (multiplier > 0).", ephemeral=True)
        # Owner caps — unbounded bonuses let an owner mint coins to an accomplice.
        if not _is_full_manager(i.user):
            if cb > 5_000 or pc > 50.0 or pm > 3.0:
                return await i.response.send_message(
                    "❌ Owner caps: coin bonus <= 5,000, percent <= 50%, multiplier <= 3x. "
                    "Ask a server manager for anything above.", ephemeral=True)
        core._set_market_loyalty(self.p.mid, pm, cb, pc)
        await self.p.refresh(i, f"✅ Rewards: {pm:g}x pts · +{cb:,}c · +{pc:g}%.")


class _TextModal(discord.ui.Modal):
    """One-field modal: ticker, CSN code, delist confirmation, delete confirmation."""

    def __init__(self, p, title, label, kind, placeholder=""):
        super().__init__(title=title, timeout=300)
        self.p, self.kind = p, kind
        self.val = discord.ui.TextInput(label=label, required=True, placeholder=placeholder)
        self.add_item(self.val)

    async def on_submit(self, i: discord.Interaction):
        v = str(self.val.value or "").strip()
        d = _db()
        if self.kind == "ticker":
            sym = "".join(c for c in v.upper() if c.isalnum())[:6]
            if not sym:
                return await i.response.send_message("❌ Needs a letter or digit.", ephemeral=True)
            t = core.load_yaml("market_tickers.yml", {}) or {}
            t[self.p.mid] = sym
            core.save_yaml("market_tickers.yml", t)
            return await self.p.refresh(i, f"✅ Ticker → {sym}.")
        if self.kind == "code":
            if not _is_full_manager(i.user):
                return await i.response.send_message("⛔ Server managers only.", ephemeral=True)
            code = v.upper()
            if not code.isalnum() or len(code) > 32:
                return await i.response.send_message("❌ Letters/digits only, max 32.", ephemeral=True)
            data = core._load_markets()
            # NEVER touches leader_discord_id — that's what /market_code did.
            data["markets"][self.p.mid]["leader_code"] = code
            core._save_markets(data)
            return await self.p.refresh(i, f"✅ Code → `{code}`. Set the mod's Market Code to match.")
        if self.kind == "delist_confirm":
            if v.strip().lower() != self.p.mid.lower():
                return await i.response.send_message(
                    f"❌ Type `{self.p.mid}` exactly to confirm.", ephemeral=True)
            d.upsert_market_shares(self.p.mid, active=0)
            return await self.p.refresh(
                i, "✅ Delisted. Holders' shares freeze at the last price until it goes public again.")
        if self.kind == "delete":
            if not _is_full_manager(i.user):
                return await i.response.send_message("⛔ Server managers only.", ephemeral=True)
            if v.strip().lower() != self.p.mid.lower():
                return await i.response.send_message(
                    f"❌ Type `{self.p.mid}` exactly to confirm.", ephemeral=True)
            counts = d.delete_market(self.p.mid)
            data = core._load_markets()
            (data.get("markets") or {}).pop(self.p.mid, None)
            core._save_markets(data)
            return await i.response.edit_message(
                content=f"🗑️ **{self.p.mid}** deleted. {counts}", embed=None, view=None)


class _PeopleModal(discord.ui.Modal):
    """add_mgr adds; anything else removes (rm_mgr falls through)."""

    def __init__(self, p, title, kind):
        super().__init__(title=title, timeout=300)
        self.p, self.kind = p, kind
        self.uid = discord.ui.TextInput(label="Discord user id", required=True,
                                        placeholder="123456789012345678")
        self.add_item(self.uid)

    async def on_submit(self, i: discord.Interaction):
        raw = str(self.uid.value or "").strip().strip("<@!>")
        if not raw.isdigit():
            return await i.response.send_message("❌ That isn't a user id.", ephemeral=True)
        data = core._load_markets()
        markets = data.get("markets") or {}
        if self.p.mid not in markets:
            return await i.response.send_message("❌ Market vanished.", ephemeral=True)
        mkt = markets[self.p.mid]
        mgrs = mkt.setdefault("manager_ids", [])
        if self.kind == "add_mgr":
            if int(raw) not in mgrs:
                mgrs.append(int(raw))
            core._save_markets(data)
            return await self.p.refresh(i, f"✅ <@{raw}> is now a site manager.")
        if int(raw) in mgrs:
            mgrs.remove(int(raw))
            core._save_markets(data)
            return await self.p.refresh(i, f"✅ <@{raw}> removed as site manager.")
        return await self.p.refresh(i, f"❌ <@{raw}> isn't a site manager here.")


class _BankruptModal(discord.ui.Modal, title="Bankrupt & pay out holders"):
    """Was /stock delist. NOT the same as the Delist button: that one just freezes the
    listing and keeps everyone's shares. This one PAYS SHAREHOLDERS OUT of the market's
    cash backing and removes the stock — irreversible and it moves real coins, so it
    needs the market id typed back."""
    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel
        self.confirm = discord.ui.TextInput(
            label=f"Type {panel.mid} to confirm"[:45], required=True)
        self.add_item(self.confirm)

    async def on_submit(self, i: discord.Interaction):
        if str(self.confirm.value or "").strip().lower() != self.panel.mid.lower():
            return await i.response.send_message(
                f"❌ Type `{self.panel.mid}` exactly to confirm.", ephemeral=True)
        import sys as _sys
        sc = _sys.modules.get("cogs.stock")
        if sc is None or not hasattr(sc, "run_stock_delist"):
            return await i.response.send_message(
                "⚠️ The stock cog isn't loaded — can't run a bankruptcy delist.", ephemeral=True)
        await sc.run_stock_delist(i, self.panel.mid, confirm=True)


class _VaultModal(discord.ui.Modal, title="Vault — deposits & pledges"):
    """Was /vault deposit + /vault pledge. Status is folded into the panel embed, so the
    third subcommand needed no button at all.

    Both fields are optional and both are DELTAS, not absolutes — that matches the
    commands, where each call added to a running total. Pledges are recorded at FULL
    market value; the haircut is applied when the backing is read, never on write.
    """
    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel
        self.deposit = discord.ui.TextInput(
            label="Coins deposited (adds to balance)", required=False)
        self.pledge = discord.ui.TextInput(
            label="Item value pledged (full market value)", required=False)
        self.unpledge = discord.ui.TextInput(
            label="Item value RETURNED (removes pledge)", required=False)
        self.add_item(self.deposit); self.add_item(self.pledge); self.add_item(self.unpledge)

    async def on_submit(self, i: discord.Interaction):
        if not _is_full_manager(i.user):
            return await i.response.send_message("⛔ Server managers only.", ephemeral=True)
        d = _db()
        mid = self.panel.mid

        def _amt(field):
            raw = str(field.value or "").strip().replace(",", "")
            if not raw:
                return None
            try:
                v = int(float(raw))
            except Exception:
                return "bad"
            return v if 0 < v <= 1_000_000_000 else "bad"

        dep, pl, un = _amt(self.deposit), _amt(self.pledge), _amt(self.unpledge)
        if "bad" in (dep, pl, un):
            return await i.response.send_message(
                "❌ Amounts must be whole numbers between 1 and 1,000,000,000.", ephemeral=True)
        if dep is None and pl is None and un is None:
            return await i.response.send_message("❌ Nothing to record.", ephemeral=True)

        notes = []
        if dep is not None:
            bal = float(d.get_config(f"vault_bal:{mid}") or 0) + dep
            d.set_config(f"vault_bal:{mid}", str(bal))
            due = float(d.get_config(f"vault_due:{mid}") or 0)
            notes.append(f"deposited `{dep:,}` → balance `{int(bal):,}` vs due `{int(due):,}`"
                         + (" ✅" if bal >= due - 1 else f" ⚠ arrears `{int(due-bal):,}` (grade capped BBB)"))
        if pl is not None or un is not None:
            raw = float(d.get_config(f"vault_pledged:{mid}") or 0)
            if pl is not None:
                raw += pl
            if un is not None:
                raw = max(0.0, raw - un)
            d.set_config(f"vault_pledged:{mid}", str(raw))
            hc = core.VAULT_PLEDGE_HAIRCUT
            notes.append(f"pledged `{int(raw):,}` market value → counts `{int(raw*hc/100):,}` "
                         f"backing ({hc:g}% liquidation)")
        await self.panel.refresh(i, "🏦 " + " · ".join(notes))


class _GoPublicModal(discord.ui.Modal, title="List on the exchange"):
    def __init__(self, p):
        super().__init__(timeout=300)
        self.p = p
        self.shares = discord.ui.TextInput(label="Shares outstanding (default 1000)", required=False)
        self.pe = discord.ui.TextInput(label="P/E multiplier (default 12)", required=False)
        self.price = discord.ui.TextInput(label="Launch price override (optional)", required=False)
        for f in (self.shares, self.pe, self.price):
            self.add_item(f)

    async def on_submit(self, i: discord.Interaction):
        d = _db()
        mid = self.p.mid
        existing = d.get_market_shares(mid)
        if existing and existing.get("active"):
            return await i.response.send_message(
                f"❌ `{mid}` is already public at {float(existing['share_price']):,.2f}/share.",
                ephemeral=True)

        def _num(field):
            t = str(field.value or "").strip()
            if not t:
                return None
            try:
                return float(t)
            except Exception:
                return "bad"
        so, pe, ip = _num(self.shares), _num(self.pe), _num(self.price)
        if "bad" in (so, pe, ip):
            return await i.response.send_message("❌ Those must be numbers.", ephemeral=True)

        d.upsert_market_shares(mid, active=1, shares_outstanding=so, pe_multiplier=pe)
        price = core._recompute_share_price(mid, reason="ipo")
        if ip is not None and ip > 0:
            # CRITICAL: an unbounded override lets a site manager list at any price and
            # sell into the treasury. Cap at 2x the computed fundamental for anyone who
            # isn't a full server manager. Price is earned from CSN history, not typed.
            cap = 2.0 * float(price or MIN_SHARE_PRICE)
            if ip > cap and not _is_full_manager(i.user):
                return await i.response.send_message(
                    f"❌ Launch price {ip:,.2f} exceeds 2x the computed fundamental ({cap:,.2f}). "
                    f"Launch prices come from real CSN history — record earnings instead of "
                    f"typing a number.", ephemeral=True)
            price = round(ip, 2)
            d.upsert_market_shares(mid, share_price=price)
            d.log_stock_price(mid, price, "ipo_override")
        listing = d.get_market_shares(mid)
        await self.p.refresh(
            i, f"📈 Listed at {float(listing['share_price']):,.2f}/share · "
               f"{float(listing['shares_outstanding']):,.0f} outstanding.")


class _WithdrawModal(discord.ui.Modal, title="Withdraw excess treasury"):
    def __init__(self, p, excess):
        super().__init__(timeout=300)
        self.p = p
        self.amt = discord.ui.TextInput(label=f"Coins (max {int(excess):,})", required=True)
        self.add_item(self.amt)

    async def on_submit(self, i: discord.Interaction):
        d = _db()
        mid = self.p.mid
        try:
            amt = int(float(str(self.amt.value).strip()))
        except Exception:
            return await i.response.send_message("❌ Must be a whole number.", ephemeral=True)
        if amt < 1:
            return await i.response.send_message("❌ Must be at least 1.", ephemeral=True)
        listing = d.get_market_shares(mid)
        if not listing:
            return await i.response.send_message(f"❌ `{mid}` isn't listed.", ephemeral=True)
        # Withdrawable = treasury MINUS the cost of buying back every held share.
        # That subtraction is what keeps shareholders covered — never drop it.
        treasury = float(listing.get("treasury_coins") or 0)
        price = float(listing.get("share_price") or 0)
        held = sum(float(h.get("shares") or 0) for h in (d.get_holders(mid) or []))
        excess = max(0.0, treasury - held * price)
        if amt > excess:
            return await i.response.send_message(
                f"❌ Only {excess:,.0f} is withdrawable (treasury {treasury:,.0f} minus "
                f"buyback cover {held * price:,.0f}).", ephemeral=True)
        applied = d.adjust_treasury(mid, -float(amt), allow_negative=False)
        moved = int(round(-applied))
        core.add_coins(i.user.id, moved, counts_as_principal=True)
        await self.p.refresh(i, f"✅ Withdrew {moved:,} coins to your wallet.")




class MarketSettingsView(discord.ui.View):
    def __init__(self, mid: str, user_id: int, user=None):
        super().__init__(timeout=300)
        self.mid = str(mid)
        self.user_id = int(user_id)
        # Kept so _build() can role-check for manager-only buttons on the FIRST render,
        # before any interaction has arrived. Falls back to id-only if not supplied.
        self.user_obj = user
        self._build()

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if int(i.user.id) != self.user_id:
            await i.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        if not _may_view(i.user, self.mid):
            await i.response.send_message(
                "⛔ You need to be this market's owner, a site manager, a server manager, "
                "or hold its leader role.", ephemeral=True)
            return False
        return True

    def _build(self):
        self.clear_items()
        markets = (core._load_markets().get("markets", {}) or {})
        _u = self.user_obj
        _visible = [(k, v) for k, v in markets.items()
                    if isinstance(v, dict) and (_u is None or _may_view(_u, k))]
        opts = [discord.SelectOption(label=str(v.get("name", k))[:80], value=k,
                                     default=(k == self.mid))
                for k, v in _visible[:25]]
        if opts:
            sel = discord.ui.Select(placeholder="Market…", options=opts, row=0)

            async def _pick(i: discord.Interaction):
                self.mid = sel.values[0]
                await self.refresh(i)
            sel.callback = _pick
            self.add_item(sel)

        listed = bool((_db().get_market_shares(self.mid) or {}).get("active"))
        # A leader-role holder who is NOT a manager can open this panel purely to fetch
        # their CSN code. Giving them the management buttons would be a privilege
        # escalation the old /market_code never granted.
        if self.user_obj is not None and not _may_manage(self.user_obj, self.mid):
            b = discord.ui.Button(label="Get CSN code", style=discord.ButtonStyle.primary, row=1)
            b.callback = self._code_get
            self.add_item(b)
            return
        spec = [
            ("Edit", discord.ButtonStyle.primary, self._edit, 1),
            ("Rewards", discord.ButtonStyle.secondary, self._loyalty, 1),
            ("Ticker", discord.ButtonStyle.secondary, self._ticker, 1),
            ("Set code manually", discord.ButtonStyle.secondary, self._code, 1),
            ("Add manager", discord.ButtonStyle.secondary, self._add_mgr, 2),
            ("Remove manager", discord.ButtonStyle.secondary, self._rm_mgr, 2),
            ("Delist" if listed else "Go public",
             discord.ButtonStyle.danger if listed else discord.ButtonStyle.success,
             self._delist if listed else self._go_public, 3),
            ("Withdraw treasury", discord.ButtonStyle.secondary, self._withdraw, 3),
            ("Delete market", discord.ButtonStyle.danger, self._delete, 3),
            ("Bankrupt & pay out", discord.ButtonStyle.danger, self._bankrupt, 3),
            ("Vault", discord.ButtonStyle.secondary, self._vault, 4),
            ("Delivery location", discord.ButtonStyle.secondary, self._location, 4),
            ("Bind/unbind channel", discord.ButtonStyle.secondary, self._bind, 4),
            ("Get CSN code", discord.ButtonStyle.primary, self._code_get, 3),
        ]
        if self.user_obj is not None and _is_full_manager(self.user_obj):
            spec.append(("Register new market", discord.ButtonStyle.success, self._register, 4))
        for label, style, cb, row in spec:
            b = discord.ui.Button(label=label, style=style, row=row)
            b.callback = cb
            self.add_item(b)

    async def refresh(self, i: discord.Interaction, note: str = ""):
        self.user_obj = i.user
        self._build()
        e = await build_embed(self.mid, i.user)
        if note:
            e.description = note
        try:
            if i.response.is_done():
                await i.edit_original_response(embed=e, view=self)
            else:
                await i.response.edit_message(embed=e, view=self)
        except Exception as ex:
            log.debug("[market panel] refresh failed: %s", ex)

    # ── button handlers ──────────────────────────────────────────────────────
    async def _edit(self, i):    await i.response.send_modal(_EditModal(self))
    async def _loyalty(self, i): await i.response.send_modal(_LoyaltyModal(self))
    async def _ticker(self, i):  await i.response.send_modal(_TextModal(self, "Set ticker", "Symbol (1-6)", "ticker", "GEX"))
    async def _code(self, i):    await i.response.send_modal(_TextModal(self, "Set CSN code", "Exact code", "code"))
    async def _delete(self, i):  await i.response.send_modal(_TextModal(self, "DELETE market", f"Type {self.mid} to confirm", "delete"))
    async def _add_mgr(self, i): await i.response.send_modal(_PeopleModal(self, "Add site manager", "add_mgr"))
    async def _rm_mgr(self, i):  await i.response.send_modal(_PeopleModal(self, "Remove site manager", "rm_mgr"))
    async def _go_public(self, i): await i.response.send_modal(_GoPublicModal(self))
    async def _location(self, i):  await i.response.send_modal(_LocationModal(self))
    async def _bind(self, i):      await i.response.send_modal(_BindModal(self))
    async def _vault(self, i):     await i.response.send_modal(_VaultModal(self))

    async def _bankrupt(self, i: discord.Interaction):
        if not _is_full_manager(i.user) and not _is_owner(i.user, self.mid):
            return await i.response.send_message(
                "⛔ Only a server manager or this market's owner can bankrupt it.", ephemeral=True)
        listing = _db().get_market_shares(self.mid)
        if not listing or not listing.get("active"):
            return await i.response.send_message(
                f"❌ `{self.mid}` isn't a listed stock.", ephemeral=True)
        await i.response.send_modal(_BankruptModal(self))

    async def _code_get(self, i: discord.Interaction):
        if not _may_view(i.user, self.mid):
            return await i.response.send_message(
                "⛔ You don't lead this market.", ephemeral=True)
        # Ephemeral and NOT folded into the panel embed: rotating invalidates the old
        # code, so it must be a deliberate press with the result shown once.
        await i.response.send_message(await _rotate_code(self, i), ephemeral=True)

    async def _register(self, i: discord.Interaction):
        if not _is_full_manager(i.user):
            return await i.response.send_message("⛔ Server managers only.", ephemeral=True)
        await i.response.send_modal(_RegisterModal(self))


    async def _delist(self, i: discord.Interaction):
        d = _db()
        holders = d.get_holders(self.mid) or []
        if holders:
            # Delisting freezes real holdings, so make it a typed confirmation.
            # Discord caps a TextInput label at 45 chars — keep it short.
            return await i.response.send_modal(
                _TextModal(self, f"Delist — {len(holders)} holder(s)",
                           f"Type {self.mid} to confirm"[:45], "delist_confirm"))
        d.upsert_market_shares(self.mid, active=0)
        await self.refresh(i, "✅ Delisted. Holdings are kept and unfreeze if it goes public again.")

    async def _withdraw(self, i: discord.Interaction):
        d = _db()
        listing = d.get_market_shares(self.mid)
        if not listing:
            return await i.response.send_message(f"❌ `{self.mid}` isn't listed.", ephemeral=True)
        treasury = float(listing.get("treasury_coins") or 0)
        price = float(listing.get("share_price") or 0)
        held = sum(float(h.get("shares") or 0) for h in (d.get_holders(self.mid) or []))
        excess = max(0.0, treasury - held * price)
        if excess < 1:
            return await i.response.send_message(
                f"❌ Nothing withdrawable — treasury {treasury:,.0f} is fully committed to "
                f"buyback cover ({held * price:,.0f}).", ephemeral=True)
        await i.response.send_modal(_WithdrawModal(self, excess))
