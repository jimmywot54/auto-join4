from flask import Flask, request
import requests
import discord
from discord import app_commands
from discord.ext import commands
import threading
import secrets
import json
import os
import time
from datetime import datetime, timedelta

# ============ RAILWAY ENVIRONMENT VARIABLES ============
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
BOT_TOKEN = os.getenv("BOT_TOKEN")
REDIRECT_URI = os.getenv("REDIRECT_URI")  # https://your-app.up.railway.app/callback
# =======================================================

API_BASE = "https://discord.com/api/v10"
pending_joins = {}
MEMBERS_FILE = "authorized_members.json"
CONFIG_FILE = "config.json"

def load_json(file, default=None):
    if default is None:
        default = {}
    if os.path.exists(file):
        try:
            with open(file, "r") as f:
                return json.load(f)
        except:
            return default
    return default

def save_json(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=2)

authorized_members = load_json(MEMBERS_FILE)
config = load_json(CONFIG_FILE, {"recovery_target": None})

app = Flask(__name__)

@app.route("/")
def index():
    return "Bot is running."

def do_auto_recovery(guild_id: str, reason: str = "manual"):
    """Adds all stored members to the given guild."""
    if not authorized_members:
        print(f"[RECOVERY] No members to recover ({reason})")
        return

    print(f"[RECOVERY] Starting ({reason}) → target: {guild_id} | members: {len(authorized_members)}")

    success = failed = expired = 0

    for user_id, data in list(authorized_members.items()):
        access_token = data.get("access_token")
        if not access_token:
            continue

        # Skip expired
        try:
            expires_at = datetime.fromisoformat(data.get("expires_at", "2000-01-01"))
            if datetime.utcnow() > expires_at:
                expired += 1
                continue
        except:
            pass

        try:
            add_res = requests.put(
                f"{API_BASE}/guilds/{guild_id}/members/{user_id}",
                headers={
                    "Authorization": f"Bot {BOT_TOKEN}",
                    "Content-Type": "application/json"
                },
                json={"access_token": access_token},
                timeout=10
            )

            if add_res.status_code in (201, 204):
                success += 1
                print(f"[RECOVERY] ✅ Added {data.get('username', user_id)}")
            else:
                failed += 1
                if add_res.status_code in (400, 401, 403):
                    authorized_members.pop(user_id, None)
                print(f"[RECOVERY] ❌ Failed {data.get('username', user_id)} → {add_res.status_code}")
        except Exception as e:
            failed += 1
            print(f"[RECOVERY] ❌ Error {user_id}: {e}")

        time.sleep(1.3)

    save_json(MEMBERS_FILE, authorized_members)
    print(f"[RECOVERY] Finished ({reason}) → Success: {success} | Failed: {failed} | Expired: {expired}")


@app.route("/callback")
def callback():
    code = request.args.get("code")
    state = request.args.get("state")

    if not code or not state:
        return "Missing code or state", 400

    guild_id = pending_joins.pop(state, None)
    if not guild_id:
        return "Invalid or expired request. Please try again.", 400

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
    expires_in = token_data.get("expires_in", 604800)

    user_res = requests.get(
        f"{API_BASE}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    user_res.raise_for_status()
    user = user_res.json()
    user_id = user["id"]
    username = user.get("global_name") or user["username"]

    # Save member
    expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
    authorized_members[user_id] = {
        "access_token": access_token,
        "username": username,
        "expires_at": expires_at,
        "last_used": datetime.utcnow().isoformat()
    }
    save_json(MEMBERS_FILE, authorized_members)

    # Add current user
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
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    print(f"Loaded {len(authorized_members)} members")
    print(f"Recovery target: {config.get('recovery_target')}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print("Sync error:", e)

@bot.event
async def on_guild_remove(guild):
    """Triggered when the bot is removed from a server OR the server is deleted."""
    print(f"[DETECT] Bot left / server deleted: {guild.name} ({guild.id})")

    target = config.get("recovery_target")
    if not target:
        print("[DETECT] No recovery target set. Use /set-recovery-target first.")
        return

    if str(guild.id) == str(target):
        print("[DETECT] The recovery target itself was deleted. Doing nothing.")
        return

    print(f"[DETECT] Starting automatic recovery to {target}")
    threading.Thread(
        target=do_auto_recovery,
        args=(target, f"server-deleted:{guild.id}"),
        daemon=True
    ).start()


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

@bot.tree.command(name="announce-join", description="Post a public join button (Admin)")
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

@bot.tree.command(name="set-recovery-target", description="Set the server members should be moved to when a server is deleted (Admin)")
@app_commands.describe(server_id="The NEW server ID that will receive all members")
@app_commands.default_permissions(administrator=True)
async def set_recovery_target(interaction: discord.Interaction, server_id: str):
    if not server_id.isdigit():
        await interaction.response.send_message("Invalid server ID.", ephemeral=True)
        return

    config["recovery_target"] = server_id
    save_json(CONFIG_FILE, config)

    await interaction.response.send_message(
        f"✅ Recovery target set to `{server_id}`\n\n"
        f"If any server the bot is in gets deleted, all saved members will automatically be moved here.",
        ephemeral=True
    )

@bot.tree.command(name="recover", description="Manually recover all members to a server (Admin)")
@app_commands.describe(server_id="The server ID to recover members into")
@app_commands.default_permissions(administrator=True)
async def recover(interaction: discord.Interaction, server_id: str):
    if not server_id.isdigit():
        await interaction.response.send_message("Invalid server ID.", ephemeral=True)
        return

    if not authorized_members:
        await interaction.response.send_message("No authorized members stored yet.", ephemeral=True)
        return

    await interaction.response.send_message(
        f"Starting recovery of **{len(authorized_members)}** members into `{server_id}`...\n"
        f"Check Railway logs for progress.",
        ephemeral=True
    )
    threading.Thread(target=do_auto_recovery, args=(server_id, "manual"), daemon=True).start()

@bot.tree.command(name="recovery-stats", description="Show recovery status")
@app_commands.default_permissions(administrator=True)
async def recovery_stats(interaction: discord.Interaction):
    target = config.get("recovery_target") or "Not set"
    count = len(authorized_members)
    await interaction.response.send_message(
        f"**Recovery Status**\n"
        f"Stored members: **{count}**\n"
        f"Auto-recovery target: `{target}`",
        ephemeral=True
    )

@bot.tree.command(name="clear-recovery", description="Wipe stored member tokens (Admin)")
@app_commands.default_permissions(administrator=True)
async def clear_recovery(interaction: discord.Interaction):
    authorized_members.clear()
    save_json(MEMBERS_FILE, authorized_members)
    await interaction.response.send_message("Recovery database cleared.", ephemeral=True)

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    if not all([CLIENT_ID, CLIENT_SECRET, BOT_TOKEN, REDIRECT_URI]):
        print("ERROR: Missing environment variables!")
        exit(1)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    bot.run(BOT_TOKEN)
