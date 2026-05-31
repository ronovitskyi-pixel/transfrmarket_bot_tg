import os
import logging
import html
import threading
import http.server
import socketserver
import urllib.parse
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
API_BASE_URL = os.environ.get("TRANSFERMARKT_API_URL", "https://transfermarkt-api.vercel.app")

# ----------------- Render Health Check Server -----------------
def start_health_check():
    """Starts a lightweight web server to satisfy Render's port binding health checks."""
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
        port = int(os.environ.get("PORT", 8080))
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("0.0.0.0", port), HealthHandler) as httpd:
            logger.info(f"Health check server serving on port {port}")
            httpd.serve_forever()

    threading.Thread(target=run_server, daemon=True).start()


# ----------------- Transfermarkt API Helpers -----------------
async def api_get(endpoint: str) -> dict:
    """Helper to safely handle asynchronous API requests."""
    url = f"{API_BASE_URL}{endpoint}"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, timeout=12.0)
            if response.status_code == 200:
                return response.json()
            logger.warning(f"API returned status {response.status_code} for {url}")
        except Exception as e:
            logger.error(f"API Error fetching {url}: {e}")
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
    """Processes search queries typed by the user with proper URL encoding."""
    query = update.message.text.strip()
    if not query:
        return

    status_msg = await update.message.reply_text(f"🔍 Searching for <i>'{html.escape(query)}'</i>...", parse_mode="HTML")
    
    # FIX: Explicitly URL-encode spaces and special characters (e.g., "Lamine Yamal" -> "Lamine%20Yamal")
    safe_query = urllib.parse.quote(query)
    
    data = await api_get(f"/players/search/{safe_query}")
    players = data.get("results", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])

    if not players:
        await status_msg.edit_text("❌ No players found matching that name. Try another spelling.")
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
    # Clear out previous dynamic image preview configurations when dropping back to standard textual menus
    await message.edit_text(
        "🎯 <b>Select a player to view details:</b>", 
        reply_markup=reply_markup, 
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )


async def render_player_menu(message, player_name, image_url):
    """Displays the interactive submenu for a chosen player, embedding their face picture."""
    # Injecting a zero-width space linked to the image forces Telegram to render it as a profile banner
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

    # Shared reusable preview layout mapping
    lp_options = LinkPreviewOptions(is_disabled=False, prefer_large_media=True, show_above_text=True) if image_url else None
    image_prefix = f'<a href="{image_url}">&#8205;</a>' if image_url else ""

    # Event: Player Selected
    if data.startswith("sel_"):
        selected_id = data.split("_", 1)[1]
        context.user_data['current_player_id'] = selected_id
        
        await query.message.edit_text("⏳ Fetching live player profile...", link_preview_options=LinkPreviewOptions(is_disabled=True))
        
        # Pull profile directly to capture exact portrait asset URL
        profile_data = await api_get(f"/players/{selected_id}/profile")
        
        matched_name = profile_data.get('name', 'Selected Player')
        found_image = profile_data.get('imageURL') or profile_data.get('imageUrl', '')
        
        context.user_data['current_player_name'] = matched_name
        context.user_data['current_player_image'] = found_image
        
        await render_player_menu(query.message, matched_name, found_image)

    # Event: View Transfer History
    elif data == "view_transfers":
        if not player_id:
            await query.message.edit_text("❌ Session data lost. Please search again.")
            return

        text = f"{image_prefix}🔄 <b>Transfer History: {html.escape(player_name)}</b>\n\n"
        api_data = await api_get(f"/players/{player_id}/transfers")
        transfers = api_data.get("transfers", []) if isinstance(api_data, dict) else []

        if not transfers:
            text += "<i>No record of transfers discovered for this player.</i>"
        else:
            for t in transfers[:6]:  # Safely constrained to stay within Telegram block length limits
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

    # Event: View Trophies Won
    elif data == "view_trophies":
        if not player_id:
            await query.message.edit_text("❌ Session data lost. Please search again.")
            return

        text = f"{image_prefix}🏆 <b>Achievements: {html.escape(player_name)}</b>\n\n"
        api_data = await api_get(f"/players/{player_id}/achievements")
        achievements = api_data.get("achievements", []) if isinstance(api_data, dict) else []

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

    # Event: View Stats & Goals
    elif data == "view_stats":
        if not player_id:
            await query.message.edit_text("❌ Session data lost. Please search again.")
            return

        text = f"{image_prefix}📊 <b>Performance Stats: {html.escape(player_name)}</b>\n\n"
        api_data = await api_get(f"/players/{player_id}/stats")
        stats = api_data.get("stats", []) if isinstance(api_data, dict) else []

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

    # Navigation Event: Return to Menu Profile
    elif data == "nav_player":
        await render_player_menu(query.message, player_name, image_url)

    # Navigation Event: Return to Main Search Results List
    elif data == "nav_results":
        players = context.user_data.get('last_search_results', [])
        if not players:
            await query.message.edit_text("❌ Search archive expired. Please type a player name again.")
            return
        await render_results_list(query.message, players)


# ----------------- Core Initialization Execution -----------------
def main():
    if not TOKEN:
        logger.critical("FATAL error: TELEGRAM_BOT_TOKEN environment variable is missing!")
        return

    start_health_check()

    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))
    application.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot infrastructure running with image rendering engine updates.")
    application.run_polling()

if __name__ == "__main__":
    main()
