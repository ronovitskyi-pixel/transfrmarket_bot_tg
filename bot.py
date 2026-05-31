import os
import logging
import html
import threading
import http.server
import socketserver
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment Configuration
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
# UPDATED: Pointing to a high-availability, mirror-backed endpoint to bypass scraping blocks
API_BASE_URL = "https://www.thesportsdb.com/api/v1/json/3"

# ----------------- Render Health Check Server -----------------
def start_health_check():
    """Starts a lightweight web server immediately to pass Render checks."""
    class HealthHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Bot is alive!")
            else:
                self.send_response(404)
                self.end_headers()

    def run_server():
        port = int(os.environ.get("PORT", 10000))
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("0.0.0.0", port), HealthHandler) as httpd:
            logger.info(f"Health check server serving on port {port}")
            httpd.serve_forever()

    threading.Thread(target=run_server, daemon=True).start()


# ----------------- Transfermarkt API Helpers -----------------
async def api_get(endpoint: str, params: dict = None) -> dict:
    """Helper to safely handle asynchronous API requests with detailed logging."""
    url = f"{API_BASE_URL}{endpoint}"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, params=params, timeout=15.0)
            if response.status_code == 200:
                return response.json()
            # CRITICAL: Log out blocks or invalid status codes from the server directly
            logger.error(f"🚨 API returned status {response.status_code} for URL: {url}")
        except Exception as e:
            logger.error(f"💥 API Connection Exception fetching {url}: {e}")
    return {}


# ----------------- Bot Commands & Core Handlers -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greets the user and gives instructions."""
    await update.message.reply_text(
        "⚽ <b>Welcome to the Transfermarkt Search Bot!</b>\n\n"
        "Type a football player's name below to look up their profile, "
        "face portrait, transfer history, awards, and club statistics.",
        parse_mode="HTML"
    )

async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes queries with deep fallback tree parsing logic to catch players."""
    query = update.message.text.strip()
    if not query:
        return

    status_msg = await update.message.reply_text(f"🔍 Searching for <i>'{html.escape(query)}'</i>...", parse_mode="HTML")
    
    # Query parameters map directly to target route engine
    data = await api_get("/players/search", params={"query": query})
    
    players = []
    if isinstance(data, dict):
        players = data.get("results") or data.get("players") or data.get("resultsList") or []
    elif isinstance(data, list):
        players = data

    if not players:
        await status_msg.edit_text("❌ No players found matching that name. Try another spelling or common variation.")
        return

    context.user_data['last_search_results'] = players
    await render_results_list(status_msg, players)


async def render_results_list(message, players):
    """Generates an interactive inline grid listing found players."""
    keyboard = []
    for p in players[:10]:
        p_id = p.get('id')
        p_name = p.get('name', 'Unknown Player')
        p_club = p.get('club', 'No Club')
        btn_text = f"{p_name} ({p_club})"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"sel_{p_id}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await message.edit_text(
        "🎯 <b>Select a player to view details:</b>", 
        reply_markup=reply_markup, 
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )


async def render_player_menu(message, player_name, image_url):
    """Displays the interactive submenu for a chosen player, embedding their face picture."""
    image_html = f'<a href="{image_url}">&#8205;</a>' if image_url else ""
    text = f"{image_html}👤 <b>Player Profile: {html.escape(player_name)}</b>\n\nChoose an option below to view details:"
    
    keyboard = [
        [InlineKeyboardButton("🔄 Transfer History", callback_data="view_transfers")],
        [InlineKeyboardButton("🏆 Trophies & Awards", callback_data="view_trophies")],
        [InlineKeyboardButton("📊 Statistics & Goals", callback_data="view_stats")],
        [InlineKeyboardButton("🔙 Back to Search Results", callback_data="nav_results")]
    ]
    
    lp_options = LinkPreviewOptions(is_disabled=False, prefer_large_media=True, show_above_text=True) if image_url else None

    await message.edit_text(
        text, 
        parse_mode="HTML", 
        reply_markup=InlineKeyboardMarkup(keyboard),
        link_preview_options=lp_options
    )


# ----------------- Dynamic Callback Query Processing -----------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manages button presses and dynamic context navigation views."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    player_id = context.user_data.get('current_player_id')
    player_name = context.user_data.get('current_player_name', 'Player')
    image_url = context.user_data.get('current_player_image', '')

    lp_options = LinkPreviewOptions(is_disabled=False, prefer_large_media=True, show_above_text=True) if image_url else None
    image_prefix = f'<a href="{image_url}">&#8205;</a>' if image_url else ""

    if data.startswith("sel_"):
        selected_id = data.split("_", 1)[1]
        context.user_data['current_player_id'] = selected_id
        
        await query.message.edit_text("⏳ Fetching live player profile...", link_preview_options=LinkPreviewOptions(is_disabled=True))
        
        profile_data = await api_get(f"/players/{selected_id}/profile")
        
        matched_name = profile_data.get('name', 'Selected Player')
        found_image = profile_data.get('imageURL') or profile_data.get('imageUrl') or profile_data.get('image_url', '')
        
        context.user_data['current_player_name'] = matched_name
        context.user_data['current_player_image'] = found_image
        
        await render_player_menu(query.message, matched_name, found_image)

    elif data == "view_transfers":
        if not player_id:
            await query.message.edit_text("❌ Session data lost. Please search again.")
            return

        text = f"{image_prefix}🔄 <b>Transfer History: {html.escape(player_name)}</b>\n\n"
        api_data = await api_get(f"/players/{player_id}/transfers")
        transfers = api_data.get("transfers") if isinstance(api_data, dict) else []

        if not transfers:
            text += "<i>No record of transfers discovered for this player.</i>"
        else:
            for t in transfers[:6]:
                season = t.get('season', 'N/A')
                date = t.get('date', 'N/A')
                
                f_club = t.get('from', {})
                from_club = f_club.get('name', 'Unknown') if isinstance(f_club, dict) else t.get('from', 'Unknown')
                t_club = t.get('to', {})
                to_club = t_club.get('name', 'Unknown') if isinstance(t_club, dict) else t.get('to', 'Unknown')
                
                fee = t.get('fee', 'N/A')
                mv = t.get('marketValue', 'N/A')

                text += (
                    f"🗓️ <b>Season:</b> {html.escape(season)} ({html.escape(date)})\n"
                    f"├ ❌ <b>From:</b> {html.escape(from_club)}\n"
                    f"├ ✅ <b>To:</b> {html.escape(to_club)}\n"
                    f"├ 💰 <b>Fee:</b> {html.escape(fee)}\n"
                    f"└ 📈 <b>MV:</b> {html.escape(mv)}\n\n"
                )

        keyboard = [[InlineKeyboardButton("🔙 Back to Player Menu", callback_data="nav_player")]]
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard), link_preview_options=lp_options)

    elif data == "view_trophies":
        if not player_id:
            await query.message.edit_text("❌ Session data lost. Please search again.")
            return

        text = f"{image_prefix}🏆 <b>Achievements: {html.escape(player_name)}</b>\n\n"
        api_data = await api_get(f"/players/{player_id}/achievements")
        achievements = api_data.get("achievements") if isinstance(api_data, dict) else []

        if not achievements:
            text += "<i>No records of standard titles or personal awards found.</i>"
        else:
            for a in achievements[:12]:
                title = a.get('title', a.get('achievement', 'Trophy'))
                count = a.get('count', '1')
                seasons_data = a.get('seasons', [])
                seasons = ", ".join(seasons_data) if isinstance(seasons_data, list) else str(seasons_data)
                text += f"🥇 <b>{html.escape(title)}</b> (x{html.escape(str(count))})\n✨ <i>Years:</i> {html.escape(seasons or 'N/A')}\n\n"

        keyboard = [[InlineKeyboardButton("🔙 Back to Player Menu", callback_data="nav_player")]]
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard), link_preview_options=lp_options)

    elif data == "view_stats":
        if not player_id:
            await query.message.edit_text("❌ Session data lost. Please search again.")
            return

        text = f"{image_prefix}📊 <b>Performance Stats: {html.escape(player_name)}</b>\n\n"
        api_data = await api_get(f"/players/{player_id}/stats")
        stats = api_data.get("stats") if isinstance(api_data, dict) else []

        if not stats:
            text += "<i>No metrics compiled for this player context.</i>"
        else:
            if isinstance(stats, list):
                for s in stats[:8]:
                    comp_obj = s.get('competition', {})
                    comp = comp_obj.get('name', 'Unknown Comp') if isinstance(comp_obj, dict) else s.get('competition', 'Unknown Comp')
                    club_obj = s.get('club', {})
                    club = club_obj.get('name', 'Unknown Club') if isinstance(club_obj, dict) else s.get('club', 'Unknown Club')
                    
                    matches = s.get('matches', s.get('appearances', '0'))
                    goals = s.get('goals', '0')
                    assists = s.get('assists', '0')
                    
                    text += (
                        f"🏟️ <b>{html.escape(comp)}</b> ({html.escape(club)})\n"
                        f"├ 👟 Matches: {html.escape(str(matches))}\n"
                        f"├ ⚽ Goals: {html.escape(str(goals))}\n"
                        f"└ 🅰️ Assists: {html.escape(str(assists))}\n\n"
                    )

        keyboard = [[InlineKeyboardButton("🔙 Back to Player Menu", callback_data="nav_player")]]
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard), link_preview_options=lp_options)

    elif data == "nav_player":
        await render_player_menu(query.message, player_name, image_url)

    elif data == "nav_results":
        players = context.user_data.get('last_search_results', [])
        if not players:
            await query.message.edit_text("❌ Search archive expired. Please type a player name again.")
            return
        await render_results_list(query.message, players)


# ----------------- Core Initialization Execution -----------------
import time

def main():
    if not TOKEN:
        logger.critical("FATAL error: TELEGRAM_BOT_TOKEN environment variable is missing!")
        return

    # 1. Start the health check web server IMMEDIATELY.
    # This immediately satisfies Render so it knows the container is online.
    start_health_check()
    logger.info("🚀 Health check web server running on port 10000.")

    # 2. FORCE A DEPLOYMENT PAUSE
    # We sleep for 60 seconds here. The web server stays responsive in the background 
    # thread, but our main thread waits out Render's old container teardown period.
    logger.info("⏳ Pausing for 60 seconds to allow Render to terminate the old container...")
    time.sleep(60)
    logger.info("▶️ Pause complete. Initializing Telegram application instance...")

    # 3. Initialize and start the Telegram engine safely
    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))
    application.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("✅ Bot infrastructure fully online. Starting polling loops.")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
