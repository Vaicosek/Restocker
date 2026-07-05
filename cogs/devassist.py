"""Dev-Assist (/propose) — a SAFE "ask the bot to change its own code" pipeline.

Flow: a trusted user runs `/propose file:<path> request:<what>`. The bot pulls that
file from GitHub, asks Claude to rewrite it, then opens a **Pull Request** on a new
branch and posts the link in Discord. It NEVER pushes to the default branch and NEVER
merges/deploys on its own — you review the diff on GitHub, merge if you like it, and
restart the bot (wispbyte's startup `git pull` picks it up).

Safety:
  * Only IDs in _dev_allowed_ids() may invoke it (owner + optional DEV_ALLOW_IDS env).
    This is deliberately separate from the AI-chat allow-list.
  * Protected files (.env, Mconfig.yml, session/login files, .gitignore) can never be edited.
  * One file per request; large files are refused (use Cowork for those).
  * Requires GITHUB_PR_TOKEN (classic PAT, `repo` scope) in the environment.

Requires env: GITHUB_PR_TOKEN  (and reuses ANTHROPIC_API_KEY via the bot's shared client).
Optional env: GITHUB_OWNER, GITHUB_REPO, GITHUB_BASE_BRANCH, DEV_AI_MODEL, DEV_ALLOW_IDS.
"""
import os
import re
import sys
import json
import time
import base64
import asyncio

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
log = core.log

# ---------------------------------------------------------------- config
GH_OWNER = os.getenv("GITHUB_OWNER", "Vaicosek")
GH_REPO = os.getenv("GITHUB_REPO", "Restocker")
GH_BASE = os.getenv("GITHUB_BASE_BRANCH", "main")
GH_TOKEN = os.getenv("GITHUB_PR_TOKEN")
GH_API = "https://api.github.com"

DEV_MODEL = os.getenv("DEV_AI_MODEL", "claude-sonnet-4-6")  # stronger than the chat model for code
MAX_FILE_BYTES = 45_000            # refuse huge files (e.g. Restocker_main.py) — use Cowork for those

_OWNER_ID = 1203738126850461738    # you — always allowed

# secrets / config the bot must never touch
_PROTECTED = re.compile(
    r"(^|/)(\.env(\..*)?|env|Mconfig\.yml|web_sessions\.yml|web_login_codes\.yml|\.gitignore)$",
    re.IGNORECASE,
)

_SYSTEM = (
    "You are a careful senior Python engineer editing the Restocker discord.py bot codebase. "
    "You are given ONE file's full current contents and a change request. "
    "Respond with ONLY a JSON object and nothing else: "
    '{"content": "<the COMPLETE new file contents>", "summary": "<one short line describing the change>"}. '
    "Rules: modify only this one file; output its ENTIRE new content (never a diff or a fragment); "
    "keep it valid, runnable Python consistent with the existing style; make the smallest change that "
    "satisfies the request; never read, write, or reference secrets, tokens, .env, or config files."
)


class _DevError(Exception):
    """User-facing failure — its message is shown in Discord."""


def _dev_allowed_ids() -> set:
    ids = {_OWNER_ID}
    for tok in re.split(r"[,\s]+", os.getenv("DEV_ALLOW_IDS", "")):
        if tok.strip().isdigit():
            ids.add(int(tok))
    return ids


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    elif not text.startswith("{"):
        i, j = text.find("{"), text.rfind("}")
        if i != -1 and j != -1:
            text = text[i:j + 1]
    return json.loads(text)


class DevAssist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="propose",
        description="(Owner) Draft a code change with AI and open a GitHub PR for your review",
    )
    @app_commands.describe(
        file="Path in the repo, e.g. cogs/market.py",
        request="Describe the change you want made to that file",
    )
    async def propose(self, interaction: discord.Interaction, file: str, request: str):
        uid = interaction.user.id
        if uid not in _dev_allowed_ids():
            return await interaction.response.send_message(
                "⛔ You're not authorized to propose code changes.", ephemeral=True)
        if not GH_TOKEN:
            return await interaction.response.send_message(
                "⚠️ `GITHUB_PR_TOKEN` isn't set in the environment — add it to `.env` first.",
                ephemeral=True)

        file = file.strip().lstrip("/")
        if not file or ".." in file or _PROTECTED.search(file):
            return await interaction.response.send_message(
                "⛔ That file is protected and can't be edited by the bot.", ephemeral=True)

        await interaction.response.defer(thinking=True)
        try:
            pr_url, summary = await self._run(interaction, file, request)
        except _DevError as e:
            return await interaction.followup.send(f"❌ {e}")
        except Exception as e:  # noqa: BLE001
            log.exception("devassist: /propose failed")
            return await interaction.followup.send(
                f"❌ Something went wrong: `{type(e).__name__}: {e}`")

        log.info("devassist: %s (%s) proposed change to %s -> %s", interaction.user, uid, file, pr_url)
        await interaction.followup.send(
            f"📝 Drafted a change to `{file}`\n"
            f"**{summary}**\n"
            f"Review & merge → {pr_url}\n"
            f"_Nothing goes live until you merge it and restart the bot._")

    # -------------------------------------------------------------- pipeline
    async def _run(self, interaction: discord.Interaction, file: str, request: str):
        headers = {
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "restocker-devassist",
        }
        async with aiohttp.ClientSession(headers=headers) as s:
            # 1) current file + blob sha
            contents_url = f"{GH_API}/repos/{GH_OWNER}/{GH_REPO}/contents/{file}"
            async with s.get(contents_url, params={"ref": GH_BASE}) as r:
                if r.status == 404:
                    raise _DevError(f"`{file}` doesn't exist on `{GH_BASE}`.")
                if r.status == 401:
                    raise _DevError("GitHub rejected the token (check `GITHUB_PR_TOKEN` / `repo` scope).")
                if r.status != 200:
                    raise _DevError(f"GitHub read failed ({r.status}).")
                meta = await r.json()
            if meta.get("encoding") != "base64":
                raise _DevError("That path isn't a text file I can edit.")
            current = base64.b64decode(meta["content"]).decode("utf-8", "replace")
            if len(current.encode()) > MAX_FILE_BYTES:
                raise _DevError(
                    f"`{file}` is {len(current) // 1024} KB — too large for safe one-shot editing. "
                    f"Use Cowork for files this big.")

            # 2) Claude rewrites the file (sync client -> worker thread)
            client = core._get_anthropic_client()
            if client is None:
                raise _DevError("AI isn't configured (missing `ANTHROPIC_API_KEY`).")
            out_tokens = max(4000, min(24000, len(current.encode()) // 3 + 3000))

            def _call():
                return client.messages.create(
                    model=DEV_MODEL,
                    max_tokens=out_tokens,
                    system=_SYSTEM,
                    messages=[{
                        "role": "user",
                        "content": (f"FILE: {file}\nCHANGE REQUEST: {request}\n\n"
                                    f"--- CURRENT CONTENTS ---\n{current}"),
                    }],
                )

            try:
                msg = await asyncio.to_thread(_call)
            except Exception as e:  # noqa: BLE001
                raise _DevError(f"AI call failed: `{type(e).__name__}`.")
            raw = "".join(getattr(b, "text", "") for b in msg.content)
            try:
                data = _extract_json(raw)
                new_content = data["content"]
                summary = str(data.get("summary") or f"update {file}")[:120]
            except Exception:
                raise _DevError("The AI didn't return a usable change — try rewording the request.")
            if not new_content.strip():
                raise _DevError("The AI returned an empty file — aborting.")
            if new_content == current:
                raise _DevError("The AI made no change to the file.")

            # 3) new branch off the base
            async with s.get(f"{GH_API}/repos/{GH_OWNER}/{GH_REPO}/git/ref/heads/{GH_BASE}") as r:
                if r.status != 200:
                    raise _DevError(f"Couldn't read `{GH_BASE}` ({r.status}).")
                base_sha = (await r.json())["object"]["sha"]
            slug = re.sub(r"[^a-z0-9]+", "-", request.lower()).strip("-")[:28] or "change"
            branch = f"bot/{slug}-{int(time.time()) % 100000}"
            async with s.post(
                f"{GH_API}/repos/{GH_OWNER}/{GH_REPO}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            ) as r:
                if r.status not in (200, 201):
                    raise _DevError(f"Couldn't create the branch ({r.status}).")

            # 4) commit the new content onto the branch
            async with s.put(
                contents_url,
                json={
                    "message": f"bot: {summary}",
                    "content": base64.b64encode(new_content.encode()).decode(),
                    "sha": meta["sha"],
                    "branch": branch,
                },
            ) as r:
                if r.status not in (200, 201):
                    raise _DevError(f"Committed nothing — GitHub returned {r.status}.")

            # 5) open the PR
            async with s.post(
                f"{GH_API}/repos/{GH_OWNER}/{GH_REPO}/pulls",
                json={
                    "title": f"[bot] {summary}",
                    "head": branch,
                    "base": GH_BASE,
                    "body": (f"Requested via `/propose` by <@{interaction.user.id}>:\n\n"
                             f"> {request}\n\n"
                             f"**File:** `{file}`\n\n"
                             f"⚠️ AI-drafted — read the diff before merging."),
                },
            ) as r:
                if r.status not in (200, 201):
                    raise _DevError(f"Committed to `{branch}` but couldn't open the PR ({r.status}).")
                pr_url = (await r.json())["html_url"]

        return pr_url, summary


async def setup(bot):
    await bot.add_cog(DevAssist(bot))
