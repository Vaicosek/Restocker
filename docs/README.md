# Restocker bot — subsystem reference (for the AI)

These files are the bot's own knowledge base. The AI loads them as context so it can
answer questions about how the bot works and point users to the right command — instead
of guessing. Keep each file concise and **accurate to the code**; if behavior changes,
update the doc in the same commit.

## Index

| Doc | Subsystem | One-liner |
|-----|-----------|-----------|
| [hive.md](hive.md) | Hive harvest wages | Pays harvesters for honey/comb sold to company chest shops |
| [csn.md](csn.md) | CSN mod + import | Scrapes ChestShop sales → CSVs → webhook → bot records money + hive wages |
| [markets.md](markets.md) | Markets | A shop with its own sales history, dashboard tab, optional stock listing |
| [stock.md](stock.md) | Stock exchange | Public markets trade as shares, priced off CSN earnings |
| [orders.md](orders.md) | Orders | Production requests workers claim and fulfill |

## How the AI should use these
- Treat these as ground truth about the bot's capabilities. Never claim a feature doesn't
  exist if it's documented here.
- When asked "how do I…" or "who can…", cite the exact command (e.g. `/hive payout`).
- These cover the core subsystems. Others (loyalty, lands, bonds, brew, enchant, money,
  team, projects, voting, valuation) exist too — say so and offer to check specifics.

## Conventions
- **Verb semantics (critical):** in ChestShop/CSN, `bought` = a customer bought FROM you =
  **your income/sale**; `sold` = you bought FROM someone = **your expense**. This is the
  opposite of the intuitive reading. All money logic follows it.
- Access to the AI itself is an allow-list managed with `/ai_allow add|remove|list`,
  separate from Discord roles.
