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
    """One-field modal used for ticker / code / item removal / role name."""

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
        if self.kind == "role":
            if not _is_full_manager(i.user):
                return await i.response.send_message("⛔ Server managers only.", ephemeral=True)
            data = core._load_markets()
            data["markets"][self.p.mid]["discord_role_name"] = v
            core._save_markets(data)
            return await self.p.refresh(i, f"✅ Leader role → {v}.")
        if self.kind == "item":
            r = core._remove_market_item(self.p.mid, v, adjust_totals=True)
            return await self.p.refresh(i, f"✅ Removed **{v}** ({r})." if r else f"❌ `{v}` not found.")
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
        if self.kind == "owner":
            if not _is_full_manager(i.user):
                return await i.response.send_message("⛔ Server managers only.", ephemeral=True)
            mkt["owner_id"] = int(raw)
            core._save_markets(data)
            return await self.p.refresh(i, f"✅ Owner → <@{raw}>.")
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


class _ParamsModal(discord.ui.Modal, title="Tune listing parameters"):
    """Was /stock set_params. Two behaviours kept:
      * shares outstanding can NEVER drop below what holders already own;
      * a treasury/asset change re-anchors the price with full_move=True, because it's a
        deliberate management action rather than a market tick.
    """

    def __init__(self, p):
        super().__init__(timeout=300)
        self.p = p
        d = _db()
        L = d.get_market_shares(p.mid) or {}
        self.shares = discord.ui.TextInput(label="Shares outstanding", required=False,
                                           default=str(L.get("shares_outstanding") or ""))
        self.pe = discord.ui.TextInput(label="P/E multiplier", required=False,
                                       default=str(L.get("pe_multiplier") or ""))
        self.treasury = discord.ui.TextInput(label="Treasury (cash on hand)", required=False,
                                             default=str(L.get("treasury_coins") or ""))
        self.assets = discord.ui.TextInput(label="Asset book value (0 clears)", required=False)
        self.sellable = discord.ui.TextInput(label="Sellable assets (0 clears)", required=False)
        for f in (self.shares, self.pe, self.treasury, self.assets, self.sellable):
            self.add_item(f)

    async def on_submit(self, i: discord.Interaction):
        if not _is_full_manager(i.user):
            return await i.response.send_message("⛔ Server managers only.", ephemeral=True)
        d = _db()
        mid = self.p.mid
        if not d.get_market_shares(mid):
            return await i.response.send_message(f"❌ `{mid}` has never been public.", ephemeral=True)

        def num(f):
            t = str(f.value or "").strip()
            if not t:
                return None
            try:
                return float(t)
            except Exception:
                return "bad"
        so, pe, tre, ast_, sell = (num(self.shares), num(self.pe), num(self.treasury),
                                   num(self.assets), num(self.sellable))
        if "bad" in (so, pe, tre, ast_, sell):
            return await i.response.send_message("❌ Those must all be numbers.", ephemeral=True)
        if all(v is None for v in (so, pe, tre, ast_, sell)):
            return await i.response.send_message("❌ Change at least one field.", ephemeral=True)

        if so is not None:
            held = sum(float(h.get("shares") or 0) for h in (d.get_holders(mid) or []))
            if so < held:
                return await i.response.send_message(
                    f"❌ Holders already own {held:,.0f} shares — outstanding can't go below that. "
                    f"Buy shares back first, or pick a number >= {held:,.0f}.", ephemeral=True)
        if tre is not None:
            d.upsert_market_shares(mid, treasury_coins=tre)
        for val, key in ((ast_, "asset_value"), (sell, "sellable_assets")):
            if val is None:
                continue
            if val > 0:
                d.set_config(f"{key}:{mid}", str(val))
            else:
                d.delete_config(f"{key}:{mid}")
        if so is not None or pe is not None:
            d.upsert_market_shares(mid, shares_outstanding=so, pe_multiplier=pe)
            price = core._recompute_share_price(mid, reason="params_changed")
        else:
            # treasury/assets moved — deliberate, so re-anchor fully (no per-event clamp)
            price = core._recompute_share_price(mid, reason="params_changed", full_move=True)
        await self.p.refresh(
            i, f"✅ Parameters updated." + (f" Share price now {price:,.2f}." if price is not None else ""))


class MarketSettingsView(discord.ui.View):
    def __init__(self, mid: str, user_id: int):
        super().__init__(timeout=300)
        self.mid = str(mid)
        self.user_id = int(user_id)
        self._build()

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if int(i.user.id) != self.user_id:
            await i.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        if not _may_manage(i.user, self.mid):
            await i.response.send_message(
                "⛔ You need to be this market's owner, a site manager, or a server manager.",
                ephemeral=True)
            return False
        return True

    def _build(self):
        self.clear_items()
        markets = (core._load_markets().get("markets", {}) or {})
        opts = [discord.SelectOption(label=str(v.get("name", k))[:80], value=k,
                                     default=(k == self.mid))
                for k, v in list(markets.items())[:25] if isinstance(v, dict)]
        if opts:
            sel = discord.ui.Select(placeholder="Market…", options=opts, row=0)

            async def _pick(i: discord.Interaction):
                self.mid = sel.values[0]
                await self.refresh(i)
            sel.callback = _pick
            self.add_item(sel)

        listed = bool((_db().get_market_shares(self.mid) or {}).get("active"))
        spec = [
            ("Edit", discord.ButtonStyle.primary, self._edit, 1),
            ("Rewards", discord.ButtonStyle.secondary, self._loyalty, 1),
            ("Ticker", discord.ButtonStyle.secondary, self._ticker, 1),
            ("CSN code", discord.ButtonStyle.secondary, self._code, 1),
            ("Leader role", discord.ButtonStyle.secondary, self._role, 1),
            ("Set owner", discord.ButtonStyle.secondary, self._owner, 2),
            ("Add manager", discord.ButtonStyle.secondary, self._add_mgr, 2),
            ("Remove manager", discord.ButtonStyle.secondary, self._rm_mgr, 2),
            ("Remove item", discord.ButtonStyle.secondary, self._rm_item, 2),
            ("V Tech group", discord.ButtonStyle.secondary, self._vtech, 2),
            ("Delist" if listed else "Go public",
             discord.ButtonStyle.danger if listed else discord.ButtonStyle.success,
             self._delist if listed else self._go_public, 3),
            ("Withdraw treasury", discord.ButtonStyle.secondary, self._withdraw, 3),
            ("Tune params", discord.ButtonStyle.secondary, self._params, 3),
            ("Delete market", discord.ButtonStyle.danger, self._delete, 3),
        ]
        for label, style, cb, row in spec:
            b = discord.ui.Button(label=label, style=style, row=row)
            b.callback = cb
            self.add_item(b)

    async def refresh(self, i: discord.Interaction, note: str = ""):
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
    async def _role(self, i):    await i.response.send_modal(_TextModal(self, "Leader role", "Role name", "role"))
    async def _rm_item(self, i): await i.response.send_modal(_TextModal(self, "Remove item", "Item name", "item"))
    async def _delete(self, i):  await i.response.send_modal(_TextModal(self, "DELETE market", f"Type {self.mid} to confirm", "delete"))
    async def _owner(self, i):   await i.response.send_modal(_PeopleModal(self, "Set owner", "owner"))
    async def _add_mgr(self, i): await i.response.send_modal(_PeopleModal(self, "Add site manager", "add_mgr"))
    async def _rm_mgr(self, i):  await i.response.send_modal(_PeopleModal(self, "Remove site manager", "rm_mgr"))
    async def _go_public(self, i): await i.response.send_modal(_GoPublicModal(self))
    async def _params(self, i):    await i.response.send_modal(_ParamsModal(self))

    async def _vtech(self, i: discord.Interaction):
        if not _is_full_manager(i.user):
            return await i.response.send_message("⛔ Server managers only — V Tech-wide setting.",
                                                 ephemeral=True)
        cur = set(core._vtech_group_markets() or [])
        if self.mid in cur:
            cur.discard(self.mid); note = f"➖ `{self.mid}` removed from the V Tech group."
        else:
            cur.add(self.mid); note = f"➕ `{self.mid}` added to the V Tech group."
        core._set_vtech_group_markets(sorted(cur))
        await self.refresh(i, note)

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
