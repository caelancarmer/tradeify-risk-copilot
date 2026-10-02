"""
bot.py -- Discord integration surface (Tradeify explicitly names Discord bots
as the kind of thing they want shipped: "deploying a Discord bot by Friday").

Commands:
  /ask <question>        -- ask the Risk Copilot (RAG + citations)
  /risk <account_key>    -- run the deterministic rule check on a tracked account
  /accounts              -- list tracked account keys

Setup: pip install discord.py ; export DISCORD_BOT_TOKEN=...
Run:   python3 src/bot.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from agent import run_agent, default_clients
from retrieval import HybridRetriever, load_corpus
from rule_engine import ACCOUNT_SPECS, AccountState, summarize, evaluate_all

import discord
from discord.ext import commands

# In-memory tracked accounts for the demo. Production: Postgres via api.py.
TRACKED: dict[str, AccountState] = {
    "demo_growth_50k": AccountState.new("growth_funded_50k"),
}

retriever = HybridRetriever(load_corpus())
clients = default_clients(mock=os.environ.get("MOCK_LLM", "1") == "1")

bot = commands.Bot(command_prefix="/", intents=discord.Intents.default())


@bot.event
async def on_ready():
    print(f"Risk Copilot online as {bot.user}")


@bot.command(name="ask")
async def ask(ctx, *, question: str):
    """Ask the copilot anything about Tradeify rules."""
    async with ctx.typing():
        res = run_agent(question, retriever, clients)
    embed = discord.Embed(title="Risk Copilot",
                          description=res.answer[:4000],
                          color=0x2ecc71 if not res.refused else 0xe74c3c)
    embed.set_footer(text=f"citations: {', '.join(res.citations) or 'none'}"
                          f" | precision={res.citation_precision}")
    await ctx.send(embed=embed)


@bot.command(name="risk")
async def risk(ctx, account_key: str):
    """Run the deterministic rule check. Usage: /risk demo_growth_50k"""
    state = TRACKED.get(account_key)
    if not state:
        await ctx.send(f"Unknown account. Tracked: {', '.join(TRACKED)}")
        return
    # NOTE: production pulls live positions/PnL from the broker API (Tradovate/
    # Rithmic) via the BullMQ worker; here we use the last synced snapshot.
    snap = SNAPSHOTS.get(account_key, {"day_pnl": 0.0,
                                       "current_equity": state.dd_floor + 1500,
                                       "daily_profits": [], "trades": [],
                                       "current_balance": state.start_balance})
    rep = summarize(evaluate_all(state, **snap))
    color = 0xe74c3c if rep["account_failed"] else (0xf39c12 if rep["session_paused"] or rep["n_breached"] else 0x2ecc71)
    lines = [f"**{f['rule']}** [{f['status']}] {f['message'][:160]}"
             for f in rep["all"] if f["status"] != "OK"]
    desc = "\n".join(lines) or "All checks green."
    embed = discord.Embed(title=f"Risk check: {account_key}", description=desc[:4000], color=color)
    await ctx.send(embed=embed)


@bot.command(name="accounts")
async def accounts(ctx):
    await ctx.send("Tracked: " + ", ".join(
        f"{k} ({ACCOUNT_SPECS[v.spec_key].family} {ACCOUNT_SPECS[v.spec_key].size//1000}k)"
        for k, v in TRACKED.items()))


# Last synced broker snapshot per account (demo values; worker overwrites).
SNAPSHOTS: dict[str, dict] = {}

if __name__ == "__main__":
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_BOT_TOKEN first.")
    bot.run(token)
