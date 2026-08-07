from flask import Flask, request
import requests
import discord
from discord import app_commands
from discord.ext import commands
import threading
import secrets

# ============ FILL THESE IN ============
CLIENT_ID = ""
CLIENT_SECRET = ""
BOT_TOKEN = ""
REDIRECT_URI = "http://localhost:5000/callback"
# =======================================

API_BASE = "https://discord.com/api/v10"
pending_joins = {}  # state → server_id

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
        "client_id": "",
        "client_secret": "",
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    token_res = requests.post(f"{API_BASE}/oauth2/token", data=data, headers=headers)

    if token_res.status_code != 200:
        return f"Token exchange failed:<br><pre>{token_res.text}</pre>", 400

    access_token = token_res.json()["access_token"]

    # Get user
    user_res = requests.get(
        f"{API_BASE}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    user_res.raise_for_status()
    user = user_res.json()
    user_id = user["id"]
    username = user.get("global_name") or user["username"]

    # Add to guild
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

# ---------- Start both ----------
def run_flask():
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    bot.run(BOT_TOKEN)