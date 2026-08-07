from flask import Flask, request
import requests
import discord
from discord import app_commands
from discord.ext import commands
import threading
import secrets
import json
import os
import asyncio
from datetime import datetime, timedelta

# ============ FILL THESE IN ============
CLIENT_ID = "client id"
CLIENT_SECRET = "client secret"
BOT_TOKEN = "bot token"
REDIRECT_URI = "redirect uri"
# =======================================

API_BASE = "https://discord.com/api/v10"
pending_joins = {}          # state → server_id
MEMBERS_FILE = "authorized_members.json"

# ---------- Persistence ----------
def load_members():
    if os.path.exists(MEMBERS_FILE):
        try:
            with open(MEMBERS_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_members(data):
    with open(MEMBERS_FILE, "w") as f:
        json.dump(data, f, indent=2)

authorized_members = load_members()   # user_id → {access_token, username, expires_at, ...}

# ---------- Flask (OAuth callback) ----------
app = Flask(__name__)

@app.route("/")
def index():
    return "Bot is running."

@app.route("/callback")
def callback():
    code = request.args.get("code")
    state = request.args.get("state")

    if not code or not state:
        return "Missing code or state", 400

    guild_id = pending_joins.pop(state, None)
    if not guild_id:
        return "Invalid or expired request. Please try again.", 400

    # Exchange code
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    token_res = requests.post(f"{API_BASE}/oauth2/token", data=data, headers=headers)

    if token_res.status_code != 200:
        return f"Token exchange failed:<br><pre>{token_res.text}</pre>", 400

    token_data = token_res.json()
    access_token = token_data["access_token"]
    expires_in = token_data.get("expires_in", 604800)  # default 7 days

    # Get user
    user_res = requests.get(
        f"{API_BASE}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    user_res.raise_for_status()
    user = user_res.json()
    user_id = user["id"]
    username = user.get("global_name") or user["username"]

    # === SAVE TOKEN FOR RECOVERY ===
    expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
    authorized_members[user_id] = {
        "access_token": access_token,
        "username": username,
        "expires_at": expires_at,
        "last_used": datetime.utcnow().isoformat()
    }
    save_members(authorized_members)

    # Add to the requested guild
    add_res = requests.put(
        f"{API_BASE}/guilds/{guild_id}/members/{user_id}",
        headers={
            "Authorization": f"Bot {BOT_TOKEN}",
            "Content-Type": "application/json"
        },
        json={"access_token": access_token}
    )

    if add_res.status_code in (201, 204):
        return f"""
            <h2>Success!</h2>
            <p><b>{username}</b> has been added to the server.</p>
            <p>You can close this tab.</p>
        """
    else:
        return f"""
            <h2>Failed</h2>
            <p>Status: {add_res.status_code}</p>
            <pre>{add_res.text}</pre>
            <p>Make sure the bot is already in that server.</p>
        """, 400

# ---------- Discord Bot ----------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    print(f"Loaded {len(authorized_members)} previously authorized members")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print("Sync error:", e)

def create_auth_url(server_id: str) -> str:
    state = secrets.token_urlsafe(16)
    pending_joins[state] = server_id
    return (
        f"https://discord.com/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify%20guilds.join"
        f"&state={state}"
        f"&prompt=consent"
    )

@bot.tree.command(name="join", description="Get a private link to join a server")
@app_commands.describe(server_id="The server ID you want to join")
async def join(interaction: discord.Interaction, server_id: str):
    if not server_id.isdigit():
        await interaction.response.send_message("Invalid server ID.", ephemeral=True)
        return

    auth_url = create_auth_url(server_id)

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Authorize & Join", url=auth_url, style=discord.ButtonStyle.link))

    await interaction.response.send_message(
        f"Click below to join server `{server_id}`:",
        view=view,
        ephemeral=True
    )

@bot.tree.command(name="announce-join", description="Post a public join button for a server (Owner/Admin)")
@app_commands.describe(server_id="The server ID people should join")
@app_commands.default_permissions(administrator=True)
async def announce_join(interaction: discord.Interaction, server_id: str):
    if not server_id.isdigit():
        await interaction.response.send_message("Invalid server ID.", ephemeral=True)
        return

    auth_url = create_auth_url(server_id)

    embed = discord.Embed(
        title="Join Another Server",
        description=f"Click the button below to join the server.\n\nServer ID: `{server_id}`",
        color=discord.Color.blurple()
    )

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Join Server", url=auth_url, style=discord.ButtonStyle.link))

    await interaction.response.send_message(embed=embed, view=view)

# ========== RECOVERY COMMANDS ==========

@bot.tree.command(name="recover", description="Add ALL previously authorized members to a new server (Admin)")
@app_commands.describe(server_id="The NEW server ID to recover members into")
@app_commands.default_permissions(administrator=True)
async def recover(interaction: discord.Interaction, server_id: str):
    if not server_id.isdigit():
        await interaction.response.send_message("Invalid server ID.", ephemeral=True)
        return

    if not authorized_members:
        await interaction.response.send_message("No authorized members stored yet.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    success = 0
    failed = 0
    expired = 0
    total = len(authorized_members)

    status_msg = await interaction.followup.send(
        f"Starting recovery of **{total}** members into `{server_id}`...\nThis may take a while.",
        ephemeral=True
    )

    for user_id, data in list(authorized_members.items()):
        access_token = data["access_token"]
        username = data.get("username", user_id)

        # Optional: skip clearly expired tokens
        try:
            expires_at = datetime.fromisoformat(data.get("expires_at", "2000-01-01"))
            if datetime.utcnow() > expires_at:
                expired += 1
                continue
        except:
            pass

        try:
            add_res = requests.put(
                f"{API_BASE}/guilds/{server_id}/members/{user_id}",
                headers={
                    "Authorization": f"Bot {BOT_TOKEN}",
                    "Content-Type": "application/json"
                },
                json={"access_token": access_token},
                timeout=10
            )

            if add_res.status_code in (201, 204):
                success += 1
            else:
                failed += 1
                # If token is bad, remove it so we don't keep trying forever
                if add_res.status_code in (400, 401, 403):
                    authorized_members.pop(user_id, None)
        except Exception:
            failed += 1

        # Small delay to respect rate limits
        await asyncio.sleep(1.2)

    save_members(authorized_members)

    await status_msg.edit(
        content=(
            f"**Recovery finished for server `{server_id}`**\n"
            f"✅ Successfully added: **{success}**\n"
            f"❌ Failed: **{failed}**\n"
            f"⏰ Skipped (expired token): **{expired}**\n"
            f"Remaining stored members: **{len(authorized_members)}**"
        )
    )

@bot.tree.command(name="recovery-stats", description="Show how many members are stored for recovery")
@app_commands.default_permissions(administrator=True)
async def recovery_stats(interaction: discord.Interaction):
    count = len(authorized_members)
    await interaction.response.send_message(
        f"**Recovery database**\nStored authorized members: **{count}**",
        ephemeral=True
    )

@bot.tree.command(name="clear-recovery", description="Wipe the stored member tokens (Admin)")
@app_commands.default_permissions(administrator=True)
async def clear_recovery(interaction: discord.Interaction):
    authorized_members.clear()
    save_members(authorized_members)
    await interaction.response.send_message("Recovery database cleared.", ephemeral=True)

# ---------- Start both ----------
def run_flask():
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    bot.run(BOT_TOKEN)
