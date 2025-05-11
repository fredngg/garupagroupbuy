import os
import telegram
import asyncio
import nest_asyncio # To handle asyncio loops in environments like GCF
import logging # Use logging for better debugging
import uuid # For generating unique IDs
import json # For handling callback data
import pprint # For pretty printing dictionaries in logs/messages
import re # Import regex for escaping
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
# Import the error class for handling DM failures
from telegram.error import Forbidden, TelegramError
# Import constants and ConversationHandler related classes
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    CommandHandler,
    ContextTypes,
    ConversationHandler, # Import ConversationHandler
    CallbackQueryHandler, # Import CallbackQueryHandler for buttons
    # --- Persistence Imports ---
    FirestorePersistence # For PTB v20+
)
# Requires google-cloud-firestore to be installed
from google.cloud import firestore
# Import escape_markdown helper
from telegram.helpers import escape_markdown

# Apply nest_asyncio early
nest_asyncio.apply()

# Enable logging (optional but recommended)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING) # Reduce httpx noise
logger = logging.getLogger(__name__)

print("--- main.py loaded ---")

# --- Define Conversation States ---
# Using integers for states
(ASKING_ITEM, ASKING_IMAGE_CHOICE, ASKING_PRICE, ASKING_MOQ,
 ASKING_CLOSING_TIME, ASKING_PICKUP, ASKING_PAYMENT_CHOICE,
 ASKING_PAYMENT_DETAILS, ASKING_CONFIRMATION, HANDLE_IMAGE_UPLOAD) = range(10)

# --- Global variable for the Telegram Application ---
application = None
bot = None

# --- INITIALIZE BOT USING ApplicationBuilder ---
# This block runs once per GCF instance startup (cold start)
try:
    BOT_TOKEN = os.environ['BOT_TOKEN']
    logger.info(f"Attempting init with BOT_TOKEN ending: ...{BOT_TOKEN[-4:] if BOT_TOKEN else 'N/A'}")

    # --- Persistence Setup using Firestore ---
    persistence = None
    try:
        # Initialize Firestore client.
        # For GCF, if Firestore is in the same project, credentials should be handled automatically
        # by the runtime service account, provided it has "Cloud Datastore User" role or equivalent.
        # For local testing, ensure GOOGLE_APPLICATION_CREDENTIALS env var is set.
        firestore_client = firestore.AsyncClient() # Use AsyncClient for async PTB
        logger.info("Firestore client initialized successfully (or will be on first use).")

        # collection_name can be customized if needed, e.g., "userBotStates"
        # Default collection name is "ptb_persistence"
        persistence = FirestorePersistence(
            firestore_client=firestore_client,
            store_user_data=True,  # Essential for conversation user data
            store_chat_data=False, # Usually not needed for chat-specific data in this bot
            store_bot_data=True,   # Useful for bot-wide data like our group_info bridge
            collection_name="userBotStates" # Using your defined collection name
        )
        logger.info("Using FirestorePersistence with collection 'userBotStates'.")
    except Exception as e_fs:
        logger.error(f"Failed to initialize Firestore client or persistence: {e_fs}. CONVERSATIONS WILL LIKELY FAIL.", exc_info=True)
        persistence = None # Fallback to no persistence on error
    # --- End Persistence Setup ---

    if BOT_TOKEN:
        builder = Application.builder().token(BOT_TOKEN)
        builder.pool_timeout(30.0)
        builder.connection_pool_size(200)

        # --- Add Persistence to Builder if configured ---
        if persistence:
            builder.persistence(persistence)
            logger.info("FirestorePersistence layer added to ApplicationBuilder.")
        else:
            logger.critical("!!! Persistence layer NOT configured or failed to initialize. Conversation state WILL be lost. THIS IS A CRITICAL ISSUE FOR GCF. !!!")
        # --- End Add Persistence ---

        application = builder.build()
        bot = application.bot
        logger.info(f"Application initialized successfully (Token ending: ...{BOT_TOKEN[-4:]})")
    else:
        logger.error("Bot init failed: BOT_TOKEN environment variable is empty.")
        application = None
        bot = None

except KeyError:
    logger.critical("!!! FATAL ERROR: BOT_TOKEN environment variable not set! Function cannot work. !!!")
    application = None
    bot = None
except Exception as e:
    logger.critical(f"!!! FATAL ERROR during Bot init: {e} !!!", exc_info=True)
    application = None
    bot = None

# --- Crash Fast if Bot Init Fails (or persistence fails and is critical) ---
if application is None:
    logger.critical("❌ Application object is None after initialization block. Raising RuntimeError.")
    raise RuntimeError("❌ Application failed to initialize. Telegram bot cannot start without handlers.")
# If persistence is absolutely critical and failed, you might choose to raise an error here too.
# For now, it logs a critical warning but attempts to continue without persistence.
# --- End Crash Fast ---


# === Conversation Handler Functions ===

# --- Workaround Function for Lost State (Less likely with persistence, but good fallback) ---
async def handle_unexpected_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    update_type = "Unknown"
    if update.message: update_type = "Message"
    elif update.callback_query: update_type = "CallbackQuery"

    user_data_str = pprint.pformat(context.user_data)
    bot_data_str = pprint.pformat(context.bot_data)
    logger.warning(
        f"handle_unexpected_state triggered for User {user.id} (type: {update_type}).\n"
        f"This might indicate an issue OR user sending unexpected input.\n"
        f"Current user_data: {user_data_str}\n"
        f"Current bot_data: {bot_data_str}"
    )

    max_len = 300
    user_data_preview = user_data_str[:max_len] + ('...' if len(user_data_str) > max_len else '')
    escaped_user_data_preview = escape_markdown(user_data_preview, version=2)
    escaped_update_type = escape_markdown(update_type, version=2)

    message_text = (
        "Sorry, something unexpected happened or I lost track of our conversation\. 🤔\n"
        "Please restart the process using /newbuy if you were in the middle of setup\.\n\n"
        f"*Debug Info:*\nReceived: {escaped_update_type}\.\nCurrent user data:\n`{escaped_user_data_preview}`"
    )

    try:
        if update.callback_query:
            await update.callback_query.answer()
            if update.callback_query.message:
                 await update.callback_query.edit_message_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
        elif update.message:
            await update.message.reply_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
    except TelegramError as e_tele:
         logger.error(f"Error sending unexpected state message (TelegramError): {e_tele}", exc_info=True)
         try:
             simple_error = "Sorry, something went wrong. Please use /newbuy to restart."
             if update.callback_query and update.callback_query.message:
                 await update.callback_query.edit_message_text(simple_error)
             elif update.message:
                 await update.message.reply_text(simple_error)
         except Exception as e_simple:
              logger.error(f"Error sending simplified unexpected state message: {e_simple}")
    except Exception as e:
        logger.error(f"Error sending unexpected state message: {e}", exc_info=True)

    context.user_data.clear()
    logger.info("Ending conversation via handle_unexpected_state.")
    return ConversationHandler.END

# --- Entry Point 1: /newbuy command directly in DM ---
async def newbuy_start_dm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    logger.info(f"ENTRY POINT: newbuy_start_dm called by User {user.id} ({user.username}).")
    # With persistence, user_data is loaded. Clear it for a fresh start.
    context.user_data.clear()
    # Ensure group-specific keys are not present if starting fresh in DM
    context.user_data.pop('group_chat_id', None)
    context.user_data.pop('group_name', None)
    logger.info(f"User_data at start of newbuy_start_dm (after clear): {context.user_data}")
    try:
        await update.message.reply_text(
            "Let's set up your group buy!\n\n"
            "First, what are you selling?"
        )
        logger.info(f"newbuy_start_dm: Asked first question. Returning state ASKING_ITEM ({ASKING_ITEM}).")
        return ASKING_ITEM
    except Exception as e:
        logger.error(f"Error in newbuy_start_dm for user {user.id}: {e}", exc_info=True)
        try: await update.message.reply_text("Sorry, an error occurred starting the setup (code E1). Please try /newbuy again.")
        except Exception: pass
        return ConversationHandler.END

# --- Entry Point 2: Callback query from button sent after group command ---
async def start_setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = update.effective_user
    try:
        await query.answer()
        logger.info(f"ENTRY POINT: start_setup_callback called by User {user.id} ({user.username}). Callback data: {query.data}")
    except Exception as e_ans:
         logger.error(f"Error answering callback query in start_setup_callback: {e_ans}", exc_info=True)

    # Retrieve group info from bot_data (now persistent) and put it in user_data
    group_info_key = f'group_info_{user.id}'
    group_info = context.bot_data.pop(group_info_key, None) # Retrieve and remove

    context.user_data.clear() # Clear user_data before starting this specific conversation flow
    if group_info:
        context.user_data['group_chat_id'] = group_info.get('group_chat_id')
        context.user_data['group_name'] = group_info.get('group_name')
        logger.info(f"Retrieved and stored group info in user_data: {group_info}")
    else:
        logger.warning(f"Could not find group info in bot_data for user {user.id} with key {group_info_key}. Starting setup without group context.")
        context.user_data.pop('group_chat_id', None)
        context.user_data.pop('group_name', None)
    logger.info(f"User_data at start of start_setup_callback (after potential update): {context.user_data}")

    try:
        logger.info("Attempting to edit message in start_setup_callback...")
        await query.edit_message_text(
            text="Okay, let's begin!\n\nWhat are you selling?"
        )
        logger.info(f"start_setup_callback: Successfully edited message. Returning state ASKING_ITEM ({ASKING_ITEM}).")
        return ASKING_ITEM
    except TelegramError as e_edit:
        logger.error(f"TelegramError editing message in start_setup_callback: {e_edit}", exc_info=True)
        try:
            if query.message: await context.bot.send_message(chat_id=query.message.chat_id, text="Sorry, something went wrong starting the setup (code T1). Please try /newbuy again in the group.")
        except Exception as e_send_err: logger.error(f"Error sending error message in start_setup_callback: {e_send_err}")
        return ConversationHandler.END
    except Exception as e_unknown:
        logger.error(f"Unknown error editing message in start_setup_callback: {e_unknown}", exc_info=True)
        try:
             if query.message: await context.bot.send_message(chat_id=query.message.chat_id, text="Sorry, an unexpected error occurred (code U1). Please try /newbuy again in the group.")
        except Exception as e_send_err: logger.error(f"Error sending error message in start_setup_callback: {e_send_err}")
        return ConversationHandler.END

# --- Subsequent Conversation States ---
# ... (Keep all functions from received_item to received_confirmation the same as before,
#      ensuring they use context.user_data for storing and retrieving information) ...
async def received_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    logger.info(f"STATE HANDLER: received_item entered for User {user.id}. User data: {context.user_data}")
    item_name = update.message.text
    context.user_data['item_name'] = item_name
    logger.info(f"User {user.id} set item name: {item_name}")
    keyboard = [[InlineKeyboardButton("Upload Image", callback_data='img_upload'), InlineKeyboardButton("Skip", callback_data='img_skip'),]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"Got it: **{item_name}**\n\nWould you like to upload a product image?",
        reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
    )
    logger.info(f"received_item: Returning state ASKING_IMAGE_CHOICE ({ASKING_IMAGE_CHOICE})")
    return ASKING_IMAGE_CHOICE

async def received_image_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    choice = query.data
    logger.info(f"STATE HANDLER: received_image_choice entered for User {user.id}. Choice: {choice}. User data: {context.user_data}")
    next_state = ASKING_PRICE
    if choice == 'img_upload':
        context.user_data['wants_image'] = True
        await query.edit_message_text(text="Okay, please upload the product image now.")
        next_state = HANDLE_IMAGE_UPLOAD
    else: # img_skip
        context.user_data['wants_image'] = False
        context.user_data['image_file_id'] = None
        await query.edit_message_text(text="Okay, skipping image upload.")
        await context.bot.send_message(chat_id=query.message.chat_id, text="What’s the price per unit? (e.g., $18 or 18.50)")
        next_state = ASKING_PRICE
    logger.info(f"received_image_choice: Returning state {next_state}")
    return next_state

async def received_image_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    logger.info(f"STATE HANDLER: received_image_upload entered for User {user.id}. User data: {context.user_data}")
    next_state = ASKING_PRICE
    if update.message.photo:
        photo_file_id = update.message.photo[-1].file_id
        context.user_data['image_file_id'] = photo_file_id
        logger.info(f"User {user.id} uploaded image with file_id: {photo_file_id}")
        await update.message.reply_text("Image received!")
        await update.message.reply_text("What’s the price per unit? (e.g., $18 or 18.50)")
        next_state = ASKING_PRICE
    elif update.message.text and update.message.text.lower() != '/skip_image':
         await update.message.reply_text("That doesn't look like an image. Please upload a photo or type /skip_image to continue without one.")
         next_state = HANDLE_IMAGE_UPLOAD
    else:
        await update.message.reply_text("Invalid input. Please upload a photo or type /skip_image.")
        next_state = HANDLE_IMAGE_UPLOAD
    logger.info(f"received_image_upload: Returning state {next_state}")
    return next_state

async def skip_image_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    logger.info(f"STATE HANDLER: skip_image_command entered for User {user.id}. User data: {context.user_data}")
    context.user_data['wants_image'] = False
    context.user_data['image_file_id'] = None
    await update.message.reply_text("Okay, skipping image upload.")
    await update.message.reply_text("What’s the price per unit? (e.g., $18 or 18.50)")
    logger.info(f"skip_image_command: Returning state ASKING_PRICE ({ASKING_PRICE})")
    return ASKING_PRICE

async def received_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    logger.info(f"STATE HANDLER: received_price entered for User {user.id}. User data: {context.user_data}")
    price_text = update.message.text
    context.user_data['price'] = price_text
    logger.info(f"User {user.id} set price: {price_text}")
    await update.message.reply_text(
        f"Price set: **{price_text}**\n\nWhat’s the minimum order quantity (MOQ)?\n(Reply with a number, or type 'No MOQ')",
        parse_mode=ParseMode.MARKDOWN
    )
    logger.info(f"received_price: Returning state ASKING_MOQ ({ASKING_MOQ})")
    return ASKING_MOQ

async def received_moq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    logger.info(f"STATE HANDLER: received_moq entered for User {user.id}. User data: {context.user_data}")
    moq_text = update.message.text
    context.user_data['moq'] = moq_text
    logger.info(f"User {user.id} set MOQ: {moq_text}")
    await update.message.reply_text(
        f"MOQ set: **{moq_text}**\n\nWhen should we close this group buy?\n(e.g., Sat 8pm, or 24 Apr 10pm)",
        parse_mode=ParseMode.MARKDOWN
    )
    logger.info(f"received_moq: Returning state ASKING_CLOSING_TIME ({ASKING_CLOSING_TIME})")
    return ASKING_CLOSING_TIME

async def received_closing_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    logger.info(f"STATE HANDLER: received_closing_time entered for User {user.id}. User data: {context.user_data}")
    closing_time_text = update.message.text
    context.user_data['closing_time'] = closing_time_text
    logger.info(f"User {user.id} set closing time: {closing_time_text}")
    await update.message.reply_text(
        f"Closing time: **{closing_time_text}**\n\nWhere is the pickup location?\n(e.g., Lobby A, Sat 4–6pm or I will deliver to units.)",
        parse_mode=ParseMode.MARKDOWN
    )
    logger.info(f"received_closing_time: Returning state ASKING_PICKUP ({ASKING_PICKUP})")
    return ASKING_PICKUP

async def received_pickup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    logger.info(f"STATE HANDLER: received_pickup entered for User {user.id}. User data: {context.user_data}")
    pickup_text = update.message.text
    context.user_data['pickup'] = pickup_text
    logger.info(f"User {user.id} set pickup location: {pickup_text}")
    keyboard = [[InlineKeyboardButton("Digital Payment (PayNow)", callback_data='pay_digital'), InlineKeyboardButton("Manual Collection", callback_data='pay_manual'),]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"Pickup location: **{pickup_text}**\n\nHow would you like to collect payment?",
        reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
    )
    logger.info(f"received_pickup: Returning state ASKING_PAYMENT_CHOICE ({ASKING_PAYMENT_CHOICE})")
    return ASKING_PAYMENT_CHOICE

async def received_payment_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    choice = query.data
    logger.info(f"STATE HANDLER: received_payment_choice entered for User {user.id}. Choice: {choice}. User data: {context.user_data}")
    next_state = ASKING_CONFIRMATION
    if choice == 'pay_digital':
        context.user_data['payment_method'] = 'Digital'
        await query.edit_message_text(
            text="Okay, digital payment selected.\n\nPlease upload your PayNow QR code image, or reply with your PayNow UEN / Phone number.\n(This will be shown to buyers. Type /skip_payment_details if you want to add this later.)"
        )
        next_state = ASKING_PAYMENT_DETAILS
    else: # pay_manual
        context.user_data['payment_method'] = 'Manual'
        context.user_data['payment_details'] = None
        await query.edit_message_text(text="Okay, payment will be collected manually.")
        next_state = await show_confirmation(update, context)
    logger.info(f"received_payment_choice: Returning state {next_state}")
    return next_state

async def received_payment_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    logger.info(f"STATE HANDLER: received_payment_details entered for User {user.id}. User data: {context.user_data}")
    payment_details_text = "Not set"
    proceed_to_confirm = False
    if update.message.photo:
        qr_file_id = update.message.photo[-1].file_id
        payment_details_text = f"PayNow QR Code provided"
        context.user_data['payment_details'] = payment_details_text
        context.user_data['payment_qr_file_id'] = qr_file_id
        logger.info(f"User {user.id} uploaded PayNow QR (File ID: {qr_file_id}).")
        await update.message.reply_text("PayNow QR received.")
        proceed_to_confirm = True
    elif update.message.text and update.message.text.lower() != '/skip_payment_details':
        payment_details_text = update.message.text
        context.user_data['payment_details'] = payment_details_text
        context.user_data['payment_qr_file_id'] = None
        logger.info(f"User {user.id} entered payment text: {payment_details_text}")
        await update.message.reply_text("PayNow details received.")
        proceed_to_confirm = True
    else:
        await update.message.reply_text("Invalid input. Please upload a PayNow QR image, enter your UEN/Phone number, or type /skip_payment_details.")
        logger.info(f"received_payment_details: Returning state ASKING_PAYMENT_DETAILS ({ASKING_PAYMENT_DETAILS}) due to invalid input")
        return ASKING_PAYMENT_DETAILS
    if proceed_to_confirm:
        return await show_confirmation(update, context)
    else:
        logger.info(f"received_payment_details: Fallback returning state ASKING_PAYMENT_DETAILS ({ASKING_PAYMENT_DETAILS})")
        return ASKING_PAYMENT_DETAILS

async def skip_payment_details_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    logger.info(f"STATE HANDLER: skip_payment_details_command entered for User {user.id}. User data: {context.user_data}")
    context.user_data['payment_details'] = "Details to be provided later by organizer"
    context.user_data['payment_qr_file_id'] = None
    await update.message.reply_text("Okay, skipping payment details for now.")
    return await show_confirmation(update, context)

async def show_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    user_data = context.user_data
    logger.info(f"STATE HANDLER: show_confirmation entered for User {user.id}. User data: {user_data}")
    group_name = user_data.get('group_name', 'the group')
    summary = (
        "Okay, let's review your group buy:\n\n"
        f"**Item:** {user_data.get('item_name', 'Not set')}\n"
        f"**Image:** {'Yes' if user_data.get('image_file_id') else 'No'}\n"
        f"**Price:** {user_data.get('price', 'Not set')}\n"
        f"**MOQ:** {user_data.get('moq', 'Not set')}\n"
        f"**Closing:** {user_data.get('closing_time', 'Not set')}\n"
        f"**Pickup:** {user_data.get('pickup', 'Not set')}\n"
        f"**Payment:** {user_data.get('payment_method', 'Not set')}"
    )
    if user_data.get('payment_method') == 'Digital':
        summary += f" ({user_data.get('payment_details', 'N/A')})"
    summary += (
        f"\n\nI'll post this in **{group_name}**"
        f"{' where you initiated the /newbuy command' if user_data.get('group_chat_id') else ''}.\n\n"
        "Ready to go?"
    )
    keyboard = [[InlineKeyboardButton("✅ Post Group Buy", callback_data='confirm_post'), InlineKeyboardButton("❌ Cancel Setup", callback_data='confirm_cancel'),]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(text=summary, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        elif update.message:
             await update.message.reply_text(text=summary, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        logger.info(f"show_confirmation: Confirmation message sent/edited. Returning state ASKING_CONFIRMATION ({ASKING_CONFIRMATION})")
        return ASKING_CONFIRMATION
    except Exception as e:
        logger.error(f"Error sending/editing confirmation message: {e}", exc_info=True)
        chat_id_to_notify = update.effective_chat.id
        if chat_id_to_notify:
             try: await context.bot.send_message(chat_id=chat_id_to_notify, text="An error occurred showing the confirmation. Please /cancel and try again.")
             except Exception as e_send: logger.error(f"Failed to send error message in show_confirmation: {e_send}")
        return ConversationHandler.END

async def received_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the final confirmation: Posts to group and ends conversation."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    choice = query.data
    user_data = context.user_data
    logger.info(f"STATE HANDLER: received_confirmation entered for User {user.id}. Choice: {choice}. User data: {user_data}")
    if choice == 'confirm_post':
        logger.info(f"User {user.id} confirmed posting. User data: {user_data}")
        group_chat_id = user_data.get('group_chat_id')
        logger.info(f"Value of group_chat_id from user_data: {group_chat_id}")
        item_name = user_data.get('item_name', 'N/A')
        image_file_id = user_data.get('image_file_id')
        price = user_data.get('price', 'N/A')
        moq = user_data.get('moq', 'N/A')
        closing_time = user_data.get('closing_time', 'N/A')
        pickup = user_data.get('pickup', 'N/A')
        payment_method = user_data.get('payment_method', 'N/A')
        payment_details = user_data.get('payment_details', 'N/A')
        payment_qr_file_id = user_data.get('payment_qr_file_id')
        organizer_mention = user.mention_html()
        post_caption = (
            f"🎉 **New Group Buy!** 🎉\n\n"
            f"**Item:** {item_name}\n"
            f"**Price:** {price}\n"
            f"**MOQ:** {moq}\n"
            f"**Closing:** {closing_time}\n"
            f"**Pickup/Delivery:** {pickup}\n"
            f"**Payment:** {payment_method}"
        )
        if payment_method == 'Digital': post_caption += f" ({payment_details})"
        post_caption += (f"\n\nOrganized by: {organizer_mention}\n\nReact to join or ask questions below! 👇")
        if group_chat_id:
            logger.info(f"Attempting to post group buy to chat ID: {group_chat_id}")
            post_successful = False
            try:
                if image_file_id:
                    logger.info(f"Sending photo {image_file_id} with caption to group {group_chat_id}")
                    await context.bot.send_photo(chat_id=group_chat_id, photo=image_file_id, caption=post_caption, parse_mode=ParseMode.HTML)
                    post_successful = True
                else:
                    logger.info(f"Sending text message to group {group_chat_id}")
                    await context.bot.send_message(chat_id=group_chat_id, text=post_caption, parse_mode=ParseMode.HTML)
                    post_successful = True
                if payment_method == 'Digital' and payment_qr_file_id:
                    logger.info(f"Sending PayNow QR {payment_qr_file_id} to group {group_chat_id}")
                    await context.bot.send_photo(chat_id=group_chat_id, photo=payment_qr_file_id, caption="PayNow QR for payment.")
                if post_successful:
                    await query.edit_message_text(text=f"✅ Done! I've posted the group buy for '{item_name}' in the group.")
                    logger.info(f"Successfully posted group buy to group {group_chat_id}")
            except Forbidden:
                logger.error(f"Forbidden: Failed to post to group {group_chat_id}. Bot might lack permissions or be banned.")
                await query.edit_message_text(text="❌ Error: I couldn't post to the group. Please ensure I have permission to send messages (and photos if applicable) in that group.")
            except TelegramError as e_post:
                 logger.error(f"TelegramError: Failed to post to group {group_chat_id}: {e_post}")
                 await query.edit_message_text(text=f"❌ Error posting to group: {e_post}. Please check the group or contact support.")
            except Exception as e_unknown:
                 logger.error(f"Unknown Error: Failed to post to group {group_chat_id}: {e_unknown}", exc_info=True)
                 await query.edit_message_text(text="❌ An unexpected error occurred while posting to the group. Please try again later.")
        else:
             await query.edit_message_text(text=f"✅ Setup complete! Group buy for '{item_name}' is ready. (Normally posted to group if started there).")
             logger.info("Group buy setup completed in DM, no group posting needed because group_chat_id was missing.")
        context.user_data.clear()
        logger.info("received_confirmation: Ending conversation after posting/confirming. Returning ConversationHandler.END")
        return ConversationHandler.END
    else: # confirm_cancel
        logger.info(f"User {user.id} cancelled setup.")
        await query.edit_message_text(text="Okay, group buy setup cancelled.")
        context.user_data.clear()
        logger.info("received_confirmation: Ending conversation after cancellation. Returning ConversationHandler.END")
        return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the entire conversation via /cancel command."""
    user = update.effective_user
    logger.info(f"FALLBACK: cancel_conversation entered for User {user.id}.")
    try:
        if update.message:
            await update.message.reply_text("Okay, the group buy setup has been cancelled.")
        elif update.callback_query:
            if update.callback_query.message: # Ensure message exists to edit
                await update.callback_query.edit_message_text("Okay, the group buy setup has been cancelled.")
            await update.callback_query.answer() # Answer callback regardless
        else:
             logger.warning("cancel_conversation called with neither message nor callback_query.")
    except Exception as e:
        logger.error(f"Error sending/editing cancel confirmation message: {e}", exc_info=True)

    context.user_data.clear()
    logger.info("Ending conversation via /cancel. Returning ConversationHandler.END")
    return ConversationHandler.END

# === Handler for /newbuy in Groups (initiates DM) ===
async def newbuy_command_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /newbuy in groups, attempting to start a DM with a button."""
    if not context.bot: logger.error("!!! ERROR in newbuy_command_group: context.bot is not available! !!!"); return
    chat = update.effective_chat
    user = update.effective_user
    logger.info(f"Processing /newbuy command from group {chat.id} (type: {chat.type}) by user {user.id} ({user.username})")
    # --- Clear user_data when starting from group ---
    context.user_data.clear() # Ensure a clean slate for this user's data before DM bridge
    logger.info(f"Cleared user_data for user {user.id} at start of newbuy_command_group.")
    # ---
    group_chat_id = chat.id
    user_id = user.id
    user_mention = user.mention_html()
    bot_username = context.bot.username
    temp_group_info = {'group_chat_id': group_chat_id, 'group_name': chat.title if chat.title else "this group"}
    group_info_key = f'group_info_{user_id}'
    # bot_data is persistent if persistence is configured for it
    context.bot_data[group_info_key] = temp_group_info
    logger.info(f"Stored temporary group info for user {user.id} in bot_data (key: {group_info_key}): {temp_group_info}")
    dm_text = (f"Hi {user.first_name}! You started a new group buy in '{temp_group_info['group_name']}'.\n\nClick the button below to start setting it up here.")
    keyboard = [[InlineKeyboardButton("🚀 Start Setup", callback_data='start_setup')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    group_reply_success = f"Okay {user_mention}, I've sent you a DM to continue setting up the group buy!"
    group_reply_fail = (f"Sorry {user_mention}, I couldn't send you a DM. Please start a chat with me directly (@{bot_username} or click my name) and then send /newbuy there to continue.")
    try:
        logger.info(f"Attempting to send DM with button to user {user_id} to start conversation.")
        await context.bot.send_message(chat_id=user_id, text=dm_text, reply_markup=reply_markup)
        logger.info(f"DM with button sent successfully to user {user.id}")
        await context.bot.send_message(chat_id=group_chat_id, text=group_reply_success, parse_mode=ParseMode.HTML)
        logger.info(f"Sent group confirmation (DM success) to {group_chat_id}")
    except Forbidden:
        logger.warning(f"FAILED to send DM to user {user.id} (Forbidden)")
        context.bot_data.pop(group_info_key, None)
        await context.bot.send_message(chat_id=group_chat_id, text=group_reply_fail, parse_mode=ParseMode.HTML)
        logger.info(f"Sent group instruction (DM failed) to {group_chat_id}")
    except Exception as e_dm:
        logger.error(f"ERROR sending initial DM to user {user.id}: {e_dm}", exc_info=True)
        context.bot_data.pop(group_info_key, None)
        await context.bot.send_message(chat_id=group_chat_id, text=f"Sorry {user_mention}, an error occurred trying to contact you privately.", parse_mode=ParseMode.HTML)

# --- Asynchronous Logic to Process Updates ---
async def _async_logic_ext(update_data):
    """Core async logic called by the GCF entry point."""
    logger.info("--- _async_logic_ext entered ---")
    global application
    if not application: logger.error("!!! ERROR in async logic: Application not initialized. Check startup logs. !!!"); return "ERROR: Application not initialized", 500
    try:
        logger.info(f"Attempting to process update_id: {update_data.get('update_id', 'N/A')} via application.process_update...")
        update_obj = Update.de_json(update_data, application.bot)
        update_type = "Unknown"
        if update_obj.message: update_type = "Message"
        elif update_obj.callback_query: update_type = "CallbackQuery"
        elif update_obj.inline_query: update_type = "InlineQuery"
        logger.info(f"Update object created. Type: {update_type}, Update ID: {update_obj.update_id}")
        user_id = update_obj.effective_user.id if update_obj.effective_user else "N/A"
        async with application:
            # Log user_data BEFORE processing if persistence is on (it would be loaded)
            # if application.persistence:
            #    loaded_user_data = await application.persistence.get_user_data()
            #    logger.info(f"User data BEFORE process_update for user {user_id} (from persistence): {loaded_user_data.get(user_id, {})}")

            await application.process_update(update_obj)

            # Log user_data AFTER processing if persistence is on (it would be updated)
            # if application.persistence:
            #    updated_user_data = await application.persistence.get_user_data()
            #    logger.info(f"User data AFTER process_update for user {user_id} (from persistence): {updated_user_data.get(user_id, {})}")
        logger.info("--- Application processed update successfully ---")
    except Exception as e:
        logger.error(f"!!! ERROR during application.process_update: {e} !!!", exc_info=True)
    finally:
        logger.info("--- _async_logic_ext finished ---")
        return "ok", 200

# --- Synchronous Google Cloud Function Entry Point ---
def telegram_webhook(request):
    """Synchronous GCF entry point for Google Cloud Functions."""
    logger.info("--- Sync telegram_webhook entry point called ---")
    global application
    if not application: logger.critical("!!! ERROR in sync wrapper: Application not initialized. Check GCF cold start logs. !!!"); return "ERROR: Bot not initialized", 500
    if request.method == "POST":
        logger.info("--- Received POST request ---")
        try:
            update_data = request.get_json(force=True)
            logger.info(f"Received update data (keys): {list(update_data.keys()) if isinstance(update_data, dict) else 'N/A'}")
            loop = asyncio.get_event_loop()
            result, status_code = loop.run_until_complete(_async_logic_ext(update_data))
            logger.info(f"Async logic finished, sync wrapper returning result: '{result}', status: {status_code}")
            return result, status_code
        except Exception as e:
            logger.critical(f"!!! FATAL ERROR during JSON parsing or run_until_complete: {e} !!!", exc_info=True)
            return "Internal Server Error", 500
    else:
        logger.warning(f"Received non-POST request ({request.method}), returning Method Not Allowed")
        return "Method Not Allowed", 405

# --- Add Handlers to the Application ---
if application:
    logger.info("--- Adding handlers to application ---")

    # 1. Handler for /newbuy in GROUPS (starts DM process)
    application.add_handler(CommandHandler("newbuy", newbuy_command_group, filters=filters.ChatType.GROUPS))
    logger.info("Added: /newbuy Command handler for GROUPS")

    # 2. Conversation Handler for the multi-step setup in DMs
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("newbuy", newbuy_start_dm, filters=filters.ChatType.PRIVATE),
            CallbackQueryHandler(start_setup_callback, pattern='^start_setup$')
        ],
        states={
            ASKING_ITEM: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_item)],
            ASKING_IMAGE_CHOICE: [CallbackQueryHandler(received_image_choice, pattern='^img_(upload|skip)$')],
            HANDLE_IMAGE_UPLOAD: [
                MessageHandler(filters.PHOTO, received_image_upload),
                CommandHandler("skip_image", skip_image_command),
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_image_upload)
            ],
            ASKING_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_price)],
            ASKING_MOQ: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_moq)],
            ASKING_CLOSING_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_closing_time)],
            ASKING_PICKUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_pickup)],
            ASKING_PAYMENT_CHOICE: [CallbackQueryHandler(received_payment_choice, pattern='^pay_(digital|manual)$')],
            ASKING_PAYMENT_DETAILS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_payment_details),
                MessageHandler(filters.PHOTO, received_payment_details),
                CommandHandler("skip_payment_details", skip_payment_details_command),
            ],
            ASKING_CONFIRMATION: [CallbackQueryHandler(received_confirmation, pattern='^confirm_(post|cancel)$')],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            MessageHandler(filters.ALL, handle_unexpected_state),
            CallbackQueryHandler(handle_unexpected_state)
            ],
        name="newbuy_conversation", # Name is required for persistence
        # persistent=True, # Enable if persistence object is configured above
    )
    application.add_handler(conv_handler)
    logger.info("Added: Conversation handler for PRIVATE setup")

    # --- Handler Count Logging ---
    logger.info(f"Handler count in application.handlers[0]: {len(application.handlers.get(0, []))}")

    # --- Cold Start Logging ---
    logger.info("✅ GCF Cold Start Completed: Application handlers initialized and ready.")

else:
    logger.warning("--- Skipping handler setup because Application initialization failed ---")
