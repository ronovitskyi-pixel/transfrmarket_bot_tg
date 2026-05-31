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
# Utilizing a high-availability public developer mirror for stable player data & images
API_BASE_URL = "https://www.thesportsdb.com/api/v1/json/3"

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


# ----------------- Bot Commands & Core Handlers -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greets the user and gives instructions."""
    await update.message.reply_text(
        "⚽ <b>Welcome to the Football Profile Search Bot!</b>\n\n"
        "Type a football player's name below to look up their career profile, "
        "official card portrait, positioning, background biography, and club metadata.",
        parse_mode="HTML"
    )

async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes queries using the data engine pipeline to return clean player structures."""
    query = update.message.text.strip()
    if not query:
        return

    status_msg = await update.message.reply_text(f"🔍 Searching database for <i>'{html.escape(query)}'</i>...", parse_mode="HTML")
    
    url = f"{API_BASE_URL}/searchplayers.php"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, params={"p": query}, timeout=15.0)
            data = response.json()
        except Exception as e:
            logger.error(f"💥 API Connection Exception fetching query '{query}': {e}")
            data = None

    players = []
    if data and data.get("player"):
        for p in data["player"]:
            # Standardize payload object layout schemas
            players.append({
                "id": p.get("idPlayer"),
                "name": p.get("strPlayer"),
                "club": p.get("strTeam", "No Current Club / Retired"),
                "nation": p.get("strNationality", "N/A"),
                "position": p.get("strPosition", "N/A"),
                "number": p.get("strNumber", "N/A"),
                "height": p.get("strHeight", "N/A"),
                "weight": p.get("strWeight", "N/A"),
                "birth_place": p.get("strBirthLocation", "N/A"),
                "birth_date": p.get("dateBorn", "N/A"),
                "wage": p.get("strWage", "N/A"),
                "imageURL": p.get("strThumb") or p.get("strCutout") or "",
                "bio": p.get("strDescriptionEN", "No biological background summary available.")
            })

    if not players:
        await status_msg.edit_text("❌ No players found matching that name. Try checking your spelling or typing a variation.")
        return

    context.user_data['last_search_results'] = players
    await render_results_list(status_msg, players)


async def render_results_list(message, players):
    """Generates an interactive grid selection UI listing up to 10 players found."""
    keyboard = []
    for p in players[:10]:
        p_id = p.get('id')
        p_name = p.get('name', 'Unknown Player')
        p_club = p.get('club', 'No Club')
        btn_text = f"{p_name} ({p_club})"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"sel_{p_id}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await message.edit_text(
        "🎯 <b>Select a player to view their profile card:</b>", 
        reply_markup=reply_markup, 
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )


async def render_player_menu(message, player):
    """Displays the interactive submenu for a chosen player, gracefully rendering their image."""
    image_url = player.get('imageURL', '')
    image_html = f'<a href="{image_url}">&#8205;</a>' if image_url else ""
    
    text = (
        f"{image_html}👤 <b>Player Profile: {html.escape(player['name'])}</b>\n\n"
        f"🏃‍♂️ <b>Position:</b> {html.escape(player['position'])}\n"
        f"🛡️ <b>Current Team:</b> {html.escape(player['club'])}\n"
        f"🌍 <b>Nationality:</b> {html.escape(player['nation'])}\n"
        f"🔢 <b>Squad Number:</b> {html.escape(player['number'])}\n\n"
        f"Choose an option below to view deeper biographical data or statistics:"
    )
    
    keyboard = [
        [InlineKeyboardButton("📊 Physical Metrics", callback_data="view_metrics")],
        [InlineKeyboardButton("📖 Career Biography", callback_data="view_bio")],
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
    """Manages button interaction states and view data swapping logic."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    results = context.user_data.get('last_search_results', [])
    selected_player = context.user_data.get('active_player_data')

    if data.startswith("sel_"):
        selected_id = data.split("_", 1)[1]
        player_match = next((p for p in results if str(p['id']) == selected_id), None)
        
        if not player_match:
            await query.message.edit_text("❌ Error: Player structural data session timed out. Please execute a fresh search.")
            return
            
        context.user_data['active_player_data'] = player_match
        await render_player_menu(query.message, player_match)

    elif data == "view_metrics":
        if not selected_player:
            await query.message.edit_text("❌ Session context expired. Please search again.")
            return

        image_prefix = f'<a href="{selected_player["imageURL"]}">&#8205;</a>' if selected_player["imageURL"] else ""
        text = (
            f"{image_prefix}📊 <b>Physical Profile Card: {html.escape(selected_player['name'])}</b>\n\n"
            f"📏 <b>Height:</b> {html.escape(selected_player['height'])}\n"
            f"⚖️ <b>Weight:</b> {html.escape(selected_player['weight'])}\n"
            f"📅 <b>Birth Date:</b> {html.escape(selected_player['birth_date'])}\n"
            f"📍 <b>Birth Place:</b> {html.escape(selected_player['birth_place'])}\n"
            f"💰 <b>Estimated Wage:</b> {html.escape(selected_player['wage']) or 'Not Publicized'}\n"
        )
        keyboard = [[InlineKeyboardButton("🔙 Back to Player Menu", callback_data="nav_player")]]
        lp_options = LinkPreviewOptions(is_disabled=False, prefer_large_media=True, show_above_text=True) if selected_player["imageURL"] else None
        
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard), link_preview_options=lp_options)

    elif data == "view_bio":
        if not selected_player:
            await query.message.edit_text("❌ Session context expired. Please search again.")
            return

        image_prefix = f'<a href="{selected_player["imageURL"]}">&#8205;</a>' if selected_player["imageURL"] else ""
        
        # Safely wrap and truncate long text to avoid hitting Telegram's 4096-character message limits
        bio_text = selected_player['bio']
        if len(bio_text) > 800:
            bio_text = bio_text[:797] + "..."

        text = (
            f"{image_prefix}📖 <b>Career Biography Summary:</b>\n\n"
            f"<i>{html.escape(bio_text)}</i>"
        )
        keyboard = [[InlineKeyboardButton("🔙 Back to Player Menu", callback_data="nav_player")]]
        lp_options = LinkPreviewOptions(is_disabled=False, prefer_large_media=True, show_above_text=True) if selected_player["imageURL"] else None
        
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard), link_preview_options=lp_options)

    elif data == "nav_player":
        if selected_player:
            await render_player_menu(query.message, selected_player)

    elif data == "nav_results":
        if not results:
            await query.message.edit_text("❌ Archive pool clear. Please send a new text query search.")
            return
        await render_results_list(query.message, results)


# ----------------- Core Initialization Execution -----------------
def main():
    if not TOKEN:
        logger.critical("FATAL: The 'TELEGRAM_BOT_TOKEN' environment setting is completely unassigned!")
        return

    # 1. Spin up web server framework for tracking tools
    start_health_check()
    logger.info("🚀 Health check web server initialized running on port 10000.")

    # 2. Safety Deployment Pause Strategy
    # Essential for Render Web Service configurations to guarantee the old deployment container is dead
    logger.info("⏳ Delaying execution for 60 seconds to safely cycle Render zero-downtime micro-tasks...")
    time.sleep(60)
    logger.info("▶️ Synchronization pause resolved. Constructing Telegram Application context engine...")

    # 3. Spin up and build polling loops
    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))
    application.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("✅ Core application loops running cleanly.")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
