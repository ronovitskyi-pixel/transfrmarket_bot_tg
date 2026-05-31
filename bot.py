import os
import logging
import html
import threading
import http.server
import socketserver
import time
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

# Enable system logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global Configuration
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
# Fixed URL pointing to an unthrottled live scraping engine instance
API_BASE_URL = "https://transfermarkt-api.vercel.app"
RESULTS_PER_PAGE = 5

# ----------------- Render Health Check Server -----------------
def start_health_check():
    """Starts a lightweight web server immediately to satisfy Render's port check."""
    class HealthHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Bot is alive and healthy!")
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


# ----------------- Unthrottled Live API Requests -----------------
async def api_get(endpoint: str, params: dict = None) -> dict:
    """Helper to cleanly poll the scraping engine using real browser headers."""
    url = f"{API_BASE_URL}{endpoint}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        try:
            response = await client.get(url, params=params, timeout=20.0)
            if response.status_code == 200:
                return response.json()
            logger.error(f"🚨 API Engine returned status {response.status_code} for {url}")
        except Exception as e:
            logger.error(f"💥 Network failure connecting to API endpoint {url}: {e}")
    return {}


# ----------------- Data Parsing Helpers -----------------
def generate_text_chart(market_values: list) -> str:
    """Constructs an accurate vertical bar chart out of live career price history."""
    if not market_values:
        return "<i>No market value history logs recorded for this player.</i>"
        
    text = ""
    # Sort chronological or take up to 6 key milestones
    for mv in market_values[-7:]:
        year = mv.get("age", mv.get("date", "N/A"))
        val_str = mv.get("value", mv.get("marketValue", "N/A"))
        
        # Calculate visual bar sizing dynamically from string value (e.g., "€180.00m")
        clean_val = val_str.replace("€", "").replace("m", "").replace("k", "").strip()
        try:
            val_float = float(clean_val)
            bar_count = max(1, min(12, int(val_float / 15))) if "m" in val_str else 1
        except ValueError:
            bar_count = 2
            
        bars = "■" * bar_count
        text += f"<code>{year}</code> | {bars} <b>{val_str}</b>\n"
    return text


# ----------------- Bot Commands & Core Handlers -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greets the user and gives instructions."""
    await update.message.reply_text(
        "⚽ <b>Welcome to the Premium Football Search Bot!</b>\n\n"
        "Type a football player's name below to search. The bot will pull a full list of all "
        "matching players, their physical attributes, real trophy milestones, and full market value graphs.",
        parse_mode="HTML"
    )

async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Queries live global databases and stores complete, real result objects."""
    query = update.message.text.strip()
    if not query:
        return

    status_msg = await update.message.reply_text(f"🔍 Searching live Transfermarkt database for <i>'{html.escape(query)}'</i>...", parse_mode="HTML")
    
    # Requesting the updated unblocked global search route
    data = await api_get("/search/players", params={"query": query})
    
    raw_players = []
    if isinstance(data, dict):
        raw_players = data.get("results") or data.get("players") or data.get("resultsList") or []
    elif isinstance(data, list):
        raw_players = data

    if not raw_players:
        await status_msg.edit_text("❌ No players found matching that name. Try checking your spelling or typing a variation.")
        return

    # Normalize Transfermarkt fields
    players = []
    for p in raw_players:
        players.append({
            "id": p.get("id"),
            "name": p.get("name", "Unknown Player"),
            "club": p.get("club", "Retired / Free Agent"),
            "nation": p.get("nationality", "N/A"),
            "position": p.get("position", "N/A"),
            "imageURL": p.get("imageURL") or p.get("imageUrl") or ""
        })

    context.user_data['last_search_results'] = players
    context.user_data['current_page'] = 0
    
    await render_results_list(status_msg, players, page=0)


async def render_results_list(message, players, page=0):
    """Generates an accurate, multi-page layout with structural arrow button rows."""
    start_idx = page * RESULTS_PER_PAGE
    end_idx = start_idx + RESULTS_PER_PAGE
    page_slice = players[start_idx:end_idx]
    
    keyboard = []
    for p in page_slice:
        btn_text = f"{p['name']} ({p['club']})"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"sel_{p['id']}")])
        
    # Build control navigation row dynamically
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"nav_page_{page - 1}"))
    
    total_pages = (len(players) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE
    if end_idx < len(players):
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"nav_page_{page + 1}"))
        
    if nav_row:
        keyboard.append(nav_row)

    reply_markup = InlineKeyboardMarkup(keyboard)
    info_text = f"🎯 <b>Multiple matches found (Page {page + 1}/{total_pages}):</b>"
    
    await message.edit_text(
        info_text, 
        reply_markup=reply_markup, 
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )


async def render_player_menu(message, player):
    """Displays the main player selection dashboard."""
    image_url = player.get('imageURL', '')
    image_html = f'<a href="{image_url}">&#8205;</a>' if image_url else ""
    
    text = (
        f"{image_html}👤 <b>Player Profile: {html.escape(player['name'])}</b>\n\n"
        f"🏃‍♂️ <b>Main Position:</b> {html.escape(player['position'])}\n"
        f"🛡️ <b>Current Team:</b> {html.escape(player['club'])}\n"
        f"🌍 <b>Nationality:</b> {html.escape(player['nation'])}\n\n"
        f"Select an option below to view real metrics, trophy milestones, or career price graphs:"
    )
    
    keyboard = [
        [InlineKeyboardButton("📊 Physical Metrics", callback_data="view_metrics")],
        [InlineKeyboardButton("🏆 Trophies & Awards", callback_data="view_trophies")],
        [InlineKeyboardButton("📈 Transfermarkt Price Graph", callback_data="view_chart")],
        [InlineKeyboardButton("🔙 Back to Search Results", callback_data="nav_results")]
    ]
    
    await message.edit_text(
        text, 
        parse_mode="HTML", 
        reply_markup=InlineKeyboardMarkup(keyboard),
        link_preview_options=LinkPreviewOptions(is_disabled=False, prefer_large_media=True, show_above_text=True) if image_url else None
    )


# ----------------- Dynamic Callback Query Processing -----------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manages button states and injects deep profile attributes dynamically on demand."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    results = context.user_data.get('last_search_results', [])
    selected_player = context.user_data.get('active_player_data')
    current_page = context.user_data.get('current_page', 0)

    if data.startswith("nav_page_"):
        target_page = int(data.split("_")[2])
        context.user_data['current_page'] = target_page
        await render_results_list(query.message, results, page=target_page)

    elif data.startswith("sel_"):
        selected_id = data.split("_", 1)[1]
        player_match = next((p for p in results if str(p['id']) == selected_id), None)
        
        if not player_match:
            await query.message.edit_text("❌ Error: Player session timed out. Please run a fresh search.")
            return
            
        await query.message.edit_text("⏳ Fetching deep profile telemetry directly from Transfermarkt...", link_preview_options=LinkPreviewOptions(is_disabled=True))
        
        # Fetch the live profile data
        profile = await api_get(f"/players/{selected_id}/profile")
        
        # Inject live profile attributes directly into our active player record
        player_match['height'] = profile.get("height", "N/A")
        player_match['weight'] = profile.get("weight", "N/A")
        player_match['birth_date'] = profile.get("dateOfBirth", "N/A")
        player_match['birth_place'] = profile.get("placeOfBirth", {}).get("city", "N/A") if isinstance(profile.get("placeOfBirth"), dict) else "N/A"
        player_match['shirt_number'] = profile.get("shirtNumber", "N/A")
        if profile.get("imageURL"):
            player_match['imageURL'] = profile.get("imageURL")

        context.user_data['active_player_data'] = player_match
        await render_player_menu(query.message, player_match)

    elif data == "view_metrics":
        if not selected_player:
            return
        
        image_prefix = f'<a href="{selected_player["imageURL"]}">&#8205;</a>' if selected_player["imageURL"] else ""
        text = (
            f"{image_prefix}📊 <b>Real Physical Profiles: {html.escape(selected_player['name'])}</b>\n\n"
            f"📏 <b>Height:</b> {html.escape(selected_player.get('height', 'N/A'))}\n"
            f"⚖️ <b>Weight:</b> {html.escape(selected_player.get('weight', 'N/A'))}\n"
            f"📅 <b>Date of Birth:</b> {html.escape(selected_player.get('birth_date', 'N/A'))}\n"
            f"📍 <b>Birthplace City:</b> {html.escape(selected_player.get('birth_place', 'N/A'))}\n"
            f"🔢 <b>Registered Number:</b> {html.escape(selected_player.get('shirt_number', 'N/A'))}\n"
        )
        keyboard = [[InlineKeyboardButton("🔙 Back to Player Menu", callback_data="nav_player")]]
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard), link_preview_options=LinkPreviewOptions(is_disabled=False))

    elif data == "view_trophies":
        if not selected_player:
            return

        await query.message.edit_text("⏳ Gathering historic title data...", link_preview_options=LinkPreviewOptions(is_disabled=True))
        
        # Query live trophies endpoint directly
        achievements_data = await api_get(f"/players/{selected_player['id']}/achievements")
        achievements = achievements_data.get("achievements", []) if isinstance(achievements_data, dict) else []

        image_prefix = f'<a href="{selected_player["imageURL"]}">&#8205;</a>' if selected_player["imageURL"] else ""
        text = f"{image_prefix}🏆 <b>Live Trophy Records for {html.escape(selected_player['name'])}:</b>\n\n"
        
        if not achievements:
            text += "<i>No official top-tier trophies discovered in active competition records.</i>"
        else:
            for a in achievements[:10]:
                title = a.get("title") or a.get("achievement", "Winner")
                count = a.get("count", "1")
                seasons = a.get("seasons", [])
                seasons_str = ", ".join(seasons) if isinstance(seasons, list) else str(seasons)
                
                text += (
                    f"🥇 <b>{html.escape(title)}</b> (x{html.escape(str(count))})\n"
                    f"└ 🗓️ <i>Seasons:</i> {html.escape(seasons_str or 'N/A')}\n\n"
                )

        keyboard = [[InlineKeyboardButton("🔙 Back to Player Menu", callback_data="nav_player")]]
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard), link_preview_options=LinkPreviewOptions(is_disabled=False))

    elif data == "view_chart":
        if not selected_player:
            return

        await query.message.edit_text("⏳ Generating text chart from pricing arrays...", link_preview_options=LinkPreviewOptions(is_disabled=True))
        
        # Pull down raw career value tracking history objects
        market_data = await api_get(f"/players/{selected_player['id']}/market_value")
        mv_history = market_data.get("marketValueHistory", []) if isinstance(market_data, dict) else []

        image_prefix = f'<a href="{selected_player["imageURL"]}">&#8205;</a>' if selected_player["imageURL"] else ""
        text = (
            f"{image_prefix}📈 <b>Transfermarkt Valuation Trend Graph</b>\n"
            f"👤 Player: <b>{html.escape(selected_player['name'])}</b>\n"
            f"───────────────────\n\n"
        )
        
        text += generate_text_chart(mv_history)
        text += "\n───────────────────\n<i>*Graph charts valuation trajectory metrics pulled over career milestones.</i>"

        keyboard = [[InlineKeyboardButton("🔙 Back to Player Menu", callback_data="nav_player")]]
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard), link_preview_options=LinkPreviewOptions(is_disabled=False))

    elif data == "nav_player":
        if selected_player:
            await render_player_menu(query.message, selected_player)

    elif data == "nav_results":
        await render_results_list(query.message, results, page=current_page)


# ----------------- Core Initialization Execution -----------------
def main():
    if not TOKEN:
        logger.critical("FATAL: The 'TELEGRAM_BOT_TOKEN' environment setting is unassigned!")
        return

    start_health_check()
    logger.info("🚀 Health check web server initialized running on port 10000.")

    # Safety Deployment Pause Strategy to cleanly cycle containers on Render Web Services
    logger.info("⏳ Delaying execution for 60 seconds to safely cycle Render zero-downtime tasks...")
    time.sleep(60)
    logger.info("▶️ Synchronization pause resolved. Constructing Telegram Application context engine...")

    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))
    application.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("✅ Core application loops running cleanly.")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
