import os
import telegram
import asyncio
import nest_asyncio # To handle asyncio loops in environments like GCF
import logging # Use logging for better debugging
import uuid # For generating unique IDs
import json # For handling callback data
import pprint # For pretty printing dictionaries in logs/messages
import re # Import regex for escaping
from typing import Any, Dict, Optional, Tuple, Set, cast, DefaultDict # For type hinting
from collections import defaultdict # For get_user_data, get_chat_data
from functools import partial # For use with asyncio.to_thread
from datetime import timezone # For UTC timestamps

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
    BasePersistence, # Import BasePersistence to create a custom one
    PersistenceInput # For type hinting in persistence methods
)
# Requires google-cloud-firestore to be installed
from google.cloud import firestore # Using the synchronous client
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


# === Custom Firestore Persistence Class ===
class CustomFirestorePersistence(BasePersistence):
    """
    A custom persistence class for python-telegram-bot using Google Firestore's
    synchronous client, with blocking calls offloaded via asyncio.to_thread.
    Aligned with the user's specified Firestore schema.
    """
    def __init__(
        self,
        project_id: Optional[str],
        database_id: str,
        user_bot_states_collection: str = "userBotStates",
        bot_data_collection: str = "telegramBotGlobalData", 
        store_user_data: bool = True,
        store_chat_data: bool = False, 
        store_bot_data: bool = True,
    ):
        super().__init__()
        self.store_user_data = store_user_data
        self.store_chat_data = store_chat_data
        self.store_bot_data = store_bot_data
        
        try:
            self.firestore_client = firestore.Client(project=project_id, database=database_id)
            logger.info(f"Synchronous Firestore client initialized. Project: {self.firestore_client.project}, DB: {database_id}")
        except Exception as e_client:
            logger.error(f"Failed to initialize synchronous Firestore Client (DB: {database_id}): {e_client}", exc_info=True)
            raise 

        self.user_bot_states_collection_name = user_bot_states_collection
        self.bot_data_collection_name = bot_data_collection
        self._bot_data_doc_id = "shared_bot_data" 

        logger.info(f"CustomFirestorePersistence configured. User/Conv states in: '{user_bot_states_collection}'. Bot data in: '{bot_data_collection}/{self._bot_data_doc_id}'.")

    async def _run_sync(self, func, *args, **kwargs):
        """Helper to run synchronous Firestore methods in a thread pool."""
        return await asyncio.to_thread(func, *args, **kwargs)

    async def get_bot_data(self) -> Dict[Any, Any]:
        if not self.store_bot_data: 
            logger.debug("CustomFirestorePersistence: get_bot_data - store_bot_data is False.")
            return {}
        try:
            doc_ref = self.firestore_client.collection(self.bot_data_collection_name).document(self._bot_data_doc_id)
            doc_snapshot = await self._run_sync(doc_ref.get)
            if doc_snapshot.exists:
                data = doc_snapshot.to_dict()
                logger.debug(f"CustomFirestorePersistence: get_bot_data retrieved {len(data)} keys.")
                return data if data else {}
            logger.debug("CustomFirestorePersistence: get_bot_data - bot_data document not found.")
            return {}
        except Exception as e:
            logger.error(f"CustomFirestorePersistence: Error in get_bot_data: {e}", exc_info=True)
            return {}

    async def update_bot_data(self, data: Dict[Any, Any]) -> None:
        if not self.store_bot_data: 
            logger.debug("CustomFirestorePersistence: update_bot_data - store_bot_data is False.")
            return
        try:
            logger.debug(f"CustomFirestorePersistence: update_bot_data with {len(data)} keys.")
            doc_ref = self.firestore_client.collection(self.bot_data_collection_name).document(self._bot_data_doc_id)
            payload = data if data else {} 
            if not data: 
                 await self._run_sync(doc_ref.set, {}) 
                 logger.info("CustomFirestorePersistence: bot_data cleared (set to empty map).")
            else:
                await self._run_sync(doc_ref.set, payload) 
            logger.debug(f"CustomFirestorePersistence: update_bot_data - bot_data updated.")
        except Exception as e:
            logger.error(f"CustomFirestorePersistence: Error in update_bot_data: {e}", exc_info=True)

    async def get_user_data(self) -> DefaultDict[int, Dict[Any, Any]]:
        if not self.store_user_data: 
            logger.debug("CustomFirestorePersistence: get_user_data - store_user_data is False.")
            return defaultdict(dict)
        all_user_data: DefaultDict[int, Dict[Any, Any]] = defaultdict(dict)
        try:
            logger.debug(f"CustomFirestorePersistence: get_user_data called. Fetching from '{self.user_bot_states_collection_name}'.")
            users_coll_ref = self.firestore_client.collection(self.user_bot_states_collection_name)
            docs_list = await self._run_sync(list, users_coll_ref.stream())

            for doc_snapshot in docs_list:
                try:
                    user_id_str = doc_snapshot.id
                    user_id = int(user_id_str)
                    doc_data = doc_snapshot.to_dict()
                    if doc_data and 'pendingData' in doc_data and isinstance(doc_data['pendingData'], dict):
                         all_user_data[user_id] = doc_data['pendingData']
                    elif doc_data: 
                         all_user_data[user_id] = {} 
                except ValueError:
                    logger.warning(f"CustomFirestorePersistence: Skipping UserBotStates document with non-integer convertible ID: {doc_snapshot.id}")
                except Exception as e_doc:
                    logger.error(f"CustomFirestorePersistence: Error processing UserBotStates document {doc_snapshot.id}: {e_doc}", exc_info=True)
            logger.debug(f"CustomFirestorePersistence: get_user_data retrieved data for {len(all_user_data)} users.")
            return all_user_data
        except Exception as e:
            logger.error(f"CustomFirestorePersistence: Error in get_user_data: {e}", exc_info=True)
            return defaultdict(dict)

    async def update_user_data(self, user_id: int, data: Dict[Any, Any]) -> None:
        if not self.store_user_data: return
        try:
            user_id_str = str(user_id)
            logger.debug(f"CustomFirestorePersistence: update_user_data for user_id {user_id_str}. Has data: {bool(data)}.")
            doc_ref = self.firestore_client.collection(self.user_bot_states_collection_name).document(user_id_str)
            
            update_payload: Dict[str, Any] = {'telegramUserId': user_id} 
            if not data: 
                update_payload['pendingData'] = {}
            else:
                update_payload['pendingData'] = data
            
            await self._run_sync(doc_ref.set, update_payload, merge=True)
            logger.debug(f"CustomFirestorePersistence: user_data (pendingData) for user_id {user_id_str} updated.")
        except Exception as e:
            logger.error(f"CustomFirestorePersistence: Error in update_user_data for user_id {user_id}: {e}", exc_info=True)

    async def get_conversations(self, name: str) -> Dict[Tuple[int, ...], Any]:
        logger.debug(f"CustomFirestorePersistence: get_conversations for ConversationHandler name '{name}' called.")
        conversations: Dict[Tuple[int, ...], Any] = {}
        try:
            user_states_coll_ref = self.firestore_client.collection(self.user_bot_states_collection_name)
            docs_list = await self._run_sync(list, user_states_coll_ref.stream())
            for doc_snapshot in docs_list:
                try:
                    user_id_str = doc_snapshot.id
                    user_id = int(user_id_str)
                    doc_data = doc_snapshot.to_dict()
                    
                    if doc_data and 'currentState' in doc_data:
                        conv_key = (user_id,) 
                        state_from_db = doc_data['currentState']
                        conversations[conv_key] = state_from_db 
                        # logger.debug(f"Loaded conversation state for user {user_id}, key {conv_key}: {state_from_db}") # Can be noisy
                except ValueError:
                    logger.warning(f"CustomFirestorePersistence: Skipping UserBotStates document for conversations with non-integer ID: {doc_snapshot.id}")
                except Exception as e_doc:
                    logger.error(f"Error processing UserBotStates document {doc_snapshot.id} for conversations '{name}': {e_doc}", exc_info=True)
            logger.debug(f"CustomFirestorePersistence: get_conversations for '{name}' retrieved {len(conversations)} entries.")
            return conversations
        except Exception as e:
            logger.error(f"CustomFirestorePersistence: Error in get_conversations for '{name}': {e}", exc_info=True)
            return {}

    async def update_conversation(
        self, name: str, key: Tuple[int, ...], new_state: Optional[object]
    ) -> None:
        if not key:
            logger.error(f"CustomFirestorePersistence: update_conversation called with empty key for name '{name}'.")
            return
        
        user_id = key[0] 
        user_id_str = str(user_id)
        
        logger.debug(f"CustomFirestorePersistence: update_conversation for name '{name}', user_id {user_id_str}, new_state {new_state}")
        try:
            doc_ref = self.firestore_client.collection(self.user_bot_states_collection_name).document(user_id_str)
            
            update_payload: Dict[str, Any] = {'telegramUserId': user_id}
            if new_state is None:
                logger.info(f"CustomFirestorePersistence: Setting currentState to None for user {user_id_str}, conversation '{name}'.")
                update_payload['currentState'] = None 
            else:
                update_payload['currentState'] = new_state
            
            await self._run_sync(doc_ref.set, update_payload, merge=True) 
            logger.debug(f"CustomFirestorePersistence: Conversation state for '{name}', user {user_id_str} updated.")
        except Exception as e:
            logger.error(f"CustomFirestorePersistence: Error in update_conversation for '{name}', user {user_id_str}: {e}", exc_info=True)

    async def refresh_user_data(self, user_id: int, user_data: Dict[Any, Any]) -> None:
        logger.debug(f"CustomFirestorePersistence: refresh_user_data for user_id {user_id}.")
        if not self.store_user_data: user_data.clear(); return
        try:
            doc_ref = self.firestore_client.collection(self.user_bot_states_collection_name).document(str(user_id))
            doc_snapshot = await self._run_sync(doc_ref.get)
            user_data.clear() 
            if doc_snapshot.exists:
                doc_dict = doc_snapshot.to_dict()
                if doc_dict and 'pendingData' in doc_dict and isinstance(doc_dict['pendingData'], dict):
                    user_data.update(doc_dict['pendingData'])
            # else: user_data remains empty
        except Exception as e:
            logger.error(f"Error in refresh_user_data for user_id {user_id}: {e}", exc_info=True)
            user_data.clear()

    async def refresh_bot_data(self, bot_data: Dict[Any, Any]) -> None:
        logger.debug(f"CustomFirestorePersistence: refresh_bot_data.")
        if not self.store_bot_data: bot_data.clear(); return
        try:
            fresh_data = await self.get_bot_data() 
            bot_data.clear()
            bot_data.update(fresh_data)
        except Exception as e:
            logger.error(f"Error in refresh_bot_data: {e}", exc_info=True)
            bot_data.clear()
    
    async def drop_user_data(self, user_id: int) -> None: 
        if not self.store_user_data: return
        logger.info(f"CustomFirestorePersistence: drop_user_data for user_id {user_id} (clearing pendingData and currentState).")
        try:
            doc_ref = self.firestore_client.collection(self.user_bot_states_collection_name).document(str(user_id))
            update_payload = {
                'telegramUserId': user_id, 
                'pendingData': {}, 
                'currentState': None
            }
            await self._run_sync(doc_ref.set, update_payload, merge=True)
        except Exception as e:
            logger.error(f"CustomFirestorePersistence: Error in drop_user_data for user_id {user_id}: {e}", exc_info=True)

    # --- Skeletons for other BasePersistence methods ---
    async def get_chat_data(self) -> DefaultDict[int, Dict[Any, Any]]:
        if not self.store_chat_data: return defaultdict(dict)
        logger.warning("CustomFirestorePersistence: get_chat_data (SKELETON - NOT IMPLEMENTED)")
        return defaultdict(dict)
    async def update_chat_data(self, chat_id: int, data: Dict[Any, Any]) -> None:
        if not self.store_chat_data: return
        logger.warning(f"CustomFirestorePersistence: update_chat_data for chat_id {chat_id} (SKELETON - NOT IMPLEMENTED)")
    async def refresh_chat_data(self, chat_id: int, chat_data: Dict[Any, Any]) -> None:
        if not self.store_chat_data: chat_data.clear(); return
        logger.warning(f"CustomFirestorePersistence: refresh_chat_data for chat_id {chat_id} (SKELETON - NOT IMPLEMENTED)")
        chat_data.clear()
    async def drop_chat_data(self, chat_id: int) -> None:
        if not self.store_chat_data: return
        logger.warning(f"CustomFirestorePersistence: drop_chat_data for chat_id {chat_id} (SKELETON - NOT IMPLEMENTED)")
    async def get_callback_data(self) -> Optional[Any]: 
        logger.warning("CustomFirestorePersistence: get_callback_data (SKELETON - NOT IMPLEMENTED - returning None)")
        return None
    async def update_callback_data(self, data: Any) -> None: 
        logger.warning("CustomFirestorePersistence: update_callback_data (SKELETON - NOT IMPLEMENTED - passing)")
    async def flush(self) -> None:
        logger.debug("CustomFirestorePersistence: flush called (no-op for sync client with to_thread).")
        pass


# --- Global variable for the Telegram Application ---
application = None
bot = None
persistence = None # Initialize persistence globally

# --- INITIALIZE BOT USING ApplicationBuilder ---
try:
    BOT_TOKEN = os.environ['BOT_TOKEN']
    logger.info(f"Attempting init with BOT_TOKEN ending: ...{BOT_TOKEN[-4:] if BOT_TOKEN else 'N/A'}")
    
    GCP_PROJECT_ID = os.environ.get('GCP_PROJECT') 
    FIRESTORE_DATABASE_ID = "garupa-group-buy" 

    if not GCP_PROJECT_ID:
        logger.error("GCP_PROJECT environment variable not found. Firestore client might use default from credentials if GOOGLE_APPLICATION_CREDENTIALS is set for local testing.")

    # --- Persistence Setup using CustomFirestorePersistence ---
    try:
        persistence = CustomFirestorePersistence(
            project_id=GCP_PROJECT_ID, 
            database_id=FIRESTORE_DATABASE_ID, 
            store_user_data=True,
            store_chat_data=False, 
            store_bot_data=True,   
            user_bot_states_collection="userBotStates", 
            bot_data_collection="telegramBotGlobalData" 
        )
        logger.info(f"CustomFirestorePersistence configured. User/Conv states in: '{persistence.user_bot_states_collection_name}', Bot data in: '{persistence.bot_data_collection_name}'.")
    except Exception as e_fs:
        logger.error(f"!!! CRITICAL FAILURE: Failed to initialize CustomFirestorePersistence class: {e_fs}. Bot will not function correctly. !!!", exc_info=True)
        persistence = None
    # --- End Persistence Setup ---

    if BOT_TOKEN:
        builder = Application.builder().token(BOT_TOKEN)
        builder.pool_timeout(30.0)
        builder.connection_pool_size(200)

        if persistence:
            builder.persistence(persistence)
            logger.info("CustomFirestorePersistence layer added to ApplicationBuilder.")
        else:
            logger.critical("!!! CRITICAL: Persistence layer IS NONE. Will not be added to ApplicationBuilder. Conversation state WILL BE LOST. !!!")

        application = builder.build()
        bot = application.bot
        logger.info(f"Application object built successfully (Token ending: ...{BOT_TOKEN[-4:]})")
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

# --- Crash Fast if Bot Init Fails OR Persistence Fails ---
if application is None:
    logger.critical("❌ Application object is None after initialization block. Raising RuntimeError to fail GCF startup.")
    raise RuntimeError("❌ Application failed to initialize. Telegram bot cannot start.")
if persistence is None: 
     logger.critical("❌ CustomFirestorePersistence is None. This is critical for GCF stateful conversations. Raising RuntimeError to fail GCF startup.")
     raise RuntimeError("❌ CustomFirestorePersistence failed to initialize. Bot cannot maintain conversation state reliably.")
# --- End Crash Fast ---


# === Conversation Handler Functions ===
# ... (All conversation state functions: handle_unexpected_state, newbuy_start_dm, start_setup_callback, received_item ... cancel_conversation remain IDENTICAL to your last provided version) ...
# --- Workaround Function for Lost State (Should be less frequent with persistence) ---
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
    # Escape for MarkdownV2
    escaped_user_data_preview = escape_markdown(user_data_preview, version=2)
    escaped_update_type = escape_markdown(update_type, version=2)


    # Correctly escape dots for MarkdownV2 in the f-string itself
    message_text = (
        "Sorry, something unexpected happened or I lost track of our conversation\\. 🤔\n"
        "Please restart the process using /newbuy\\.\n\n"
        f"*Debug Info:*\nReceived: {escaped_update_type}\\.\nCurrent user data:\n`{escaped_user_data_preview}`"
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

    context.user_data.clear() # This will now also clear it from Firestore for this user
    logger.info("Ending conversation via handle_unexpected_state.")
    return ConversationHandler.END

# --- Entry Point 1: /newbuy command directly in DM ---
async def newbuy_start_dm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    logger.info(f"ENTRY POINT: newbuy_start_dm called by User {user.id} ({user.username}).")
    # With persistence, user_data is loaded. Clear it for a fresh start for this conversation.
    context.user_data.clear()
    context.user_data.pop('group_chat_id', None) # Ensure these are not carried over if user starts fresh in DM
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
    # Use get() for bot_data as well to avoid KeyError if not found, though pop is fine too.
    group_info = context.bot_data.get(group_info_key)
    if group_info:
        context.bot_data.pop(group_info_key) # Remove after retrieval

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
    user_data = context.user_data # This data is now loaded by persistence
    logger.info(f"STATE HANDLER: received_confirmation entered for User {user.id}. Choice: {choice}. User data: {user_data}")

    if choice == 'confirm_post':
        logger.info(f"User {user.id} confirmed posting. User data: {user_data}")
        group_chat_id = user_data.get('group_chat_id')
        item_name = user_data.get('item_name', 'N/A')
        # ... (rest of the variable assignments)
        image_file_id = user_data.get('image_file_id')
        price = user_data.get('price', 'N/A')
        moq = user_data.get('moq', 'N/A')
        closing_time = user_data.get('closing_time', 'N/A')
        pickup = user_data.get('pickup', 'N/A')
        payment_method = user_data.get('payment_method', 'N/A')
        payment_details = user_data.get('payment_details', 'N/A')
        payment_qr_file_id = user_data.get('payment_qr_file_id')
        organizer_mention = user.mention_html()
        group_buy_id = str(uuid.uuid4()) 

        firestore_group_buy_data = {
            'group_id': group_buy_id, 
            'itemName': item_name,
            'itemPrice': user_data.get('price_numeric', price), 
            'currency': user_data.get('currency', 'SGD'),
            'description': user_data.get('description', ''),
            'imageFileId': image_file_id,
            'initiatorUserId': str(user.id),
            'initiatorUsername': user.username or "",
            'createdAt': firestore.SERVER_TIMESTAMP,
            'deadline': closing_time,
            'status': 'open',
            'minParticipants': user_data.get('moq_numeric', 0 if str(moq).lower() == 'no moq' else moq),
            'maxParticipants': user_data.get('max_participants', 0),
            'currentParticipantCount': 0,
            'currentQuantityCount': 0,
            'pickupAddress': pickup,
            'paymentMethod': payment_method,
            'paymentDetails': payment_details,
            'paymentQRFileID': payment_qr_file_id,
            'telegramGroupChatID': str(group_chat_id) if group_chat_id else None,
            'telegramGroupName': user_data.get('group_name'),
            'telegramPostMessageID': None 
        }
        firestore_group_buy_data_cleaned = {k: v for k, v in firestore_group_buy_data.items() if v is not None}

        post_caption = (
            f"🎉 **New Group Buy!** 🎉\n\n"
            f"🆔 `{group_buy_id[:8]}`\n" 
            f"**Item:** {item_name}\n"
            f"**Price:** {price}\n"
            f"**MOQ:** {moq}\n"
            f"**Closing:** {closing_time}\n"
            f"**Pickup/Delivery:** {pickup}\n"
            f"**Payment:** {payment_method}"
        )
        if payment_method == 'Digital': post_caption += f" ({payment_details})"
        post_caption += (f"\n\nOrganized by: {organizer_mention}\n\n👇 Click below to order or see details!")

        order_callback_data = json.dumps({'a': 'order', 'gid': group_buy_id}) 
        view_callback_data = json.dumps({'a': 'view', 'gid': group_buy_id})

        if len(order_callback_data.encode('utf-8')) > 64 or len(view_callback_data.encode('utf-8')) > 64:
            logger.error("Callback data for order/view buttons is too long!")
            post_keyboard = None
        else:
            post_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🛒 Order Now", callback_data=order_callback_data),
                    InlineKeyboardButton("👀 See Who Ordered", callback_data=view_callback_data),
                ]
            ])

        if group_chat_id:
            logger.info(f"Attempting to post group buy {group_buy_id} to chat ID: {group_chat_id}")
            sent_message = None
            try:
                if image_file_id:
                    sent_message = await context.bot.send_photo(
                        chat_id=group_chat_id, photo=image_file_id, caption=post_caption,
                        parse_mode=ParseMode.HTML, reply_markup=post_keyboard
                    )
                else:
                    sent_message = await context.bot.send_message(
                        chat_id=group_chat_id, text=post_caption,
                        parse_mode=ParseMode.HTML, reply_markup=post_keyboard
                    )
                
                if payment_method == 'Digital' and payment_qr_file_id:
                    await context.bot.send_photo(
                        chat_id=group_chat_id, photo=payment_qr_file_id, caption="PayNow QR for payment."
                    )
                
                if sent_message:
                    firestore_group_buy_data_cleaned['telegramPostMessageID'] = str(sent_message.message_id)
                    if await add_group_buy_to_firestore(group_buy_id, firestore_group_buy_data_cleaned, context): 
                        await query.edit_message_text(text=f"✅ Done! I've posted the group buy for '{item_name}' in the group.")
                        logger.info(f"Successfully posted and saved group buy {group_buy_id} to group {group_chat_id}")
                    else:
                        await query.edit_message_text(text=f"✅ Posted to group, but failed to save details for '{item_name}' to database. Please check logs.")
                        logger.error(f"Failed to save group buy {group_buy_id} to Firestore after posting.")
                else:
                    logger.error(f"Failed to send group buy message to group {group_chat_id}, sent_message is None.")
                    await query.edit_message_text(text="❌ Error: Could not send the message to the group.")

            except Forbidden: # ... (rest of exception handling)
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
    user = update.effective_user
    logger.info(f"FALLBACK: cancel_conversation entered for User {user.id}.")
    try:
        if update.message:
            await update.message.reply_text("Okay, the group buy setup has been cancelled.")
        elif update.callback_query:
            if update.callback_query.message: 
                await update.callback_query.edit_message_text("Okay, the group buy setup has been cancelled.")
            await update.callback_query.answer() 
        else:
             logger.warning("cancel_conversation called with neither message nor callback_query.")
    except Exception as e:
        logger.error(f"Error sending/editing cancel confirmation message: {e}", exc_info=True)

    context.user_data.clear()
    logger.info("Ending conversation via /cancel. Returning ConversationHandler.END")
    return ConversationHandler.END

# === Handler for /newbuy in Groups (initiates DM) ===
async def newbuy_command_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot: logger.error("!!! ERROR in newbuy_command_group: context.bot is not available! !!!"); return
    chat = update.effective_chat
    user = update.effective_user
    logger.info(f"Processing /newbuy command from group {chat.id} (type: {chat.type}) by user {user.id} ({user.username})")
    
    # With persistence, user_data is loaded. Clear it for a fresh start for this user's interaction.
    context.user_data.clear() 
    logger.info(f"Cleared user_data for user {user.id} at start of newbuy_command_group.")
    
    group_chat_id = chat.id
    user_id = user.id
    user_mention = user.mention_html()
    bot_username = context.bot.username
    temp_group_info = {'group_chat_id': group_chat_id, 'group_name': chat.title if chat.title else "this group"}
    group_info_key = f'group_info_{user_id}'
    
    context.bot_data[group_info_key] = temp_group_info # This will be persisted by FirestorePersistence
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
        context.bot_data.pop(group_info_key, None) # Clean up if DM failed
        await context.bot.send_message(chat_id=group_chat_id, text=group_reply_fail, parse_mode=ParseMode.HTML)
        logger.info(f"Sent group instruction (DM failed) to {group_chat_id}")
    except Exception as e_dm:
        logger.error(f"ERROR sending initial DM to user {user.id}: {e_dm}", exc_info=True)
        context.bot_data.pop(group_info_key, None) # Clean up
        await context.bot.send_message(chat_id=group_chat_id, text=f"Sorry {user_mention}, an error occurred trying to contact you privately.", parse_mode=ParseMode.HTML)

# --- Function to add Group Buy to Firestore ---
async def add_group_buy_to_firestore(group_buy_id: str, group_buy_details: Dict[str, Any], context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Adds a new group buy document to the 'groupBuys' collection in Firestore.
    Uses the firestore_client from the application's persistence object if available.
    """
    logger.info(f"Attempting to add group buy {group_buy_id} to Firestore. Details: {pprint.pformat(group_buy_details)}")

    # Get Firestore client from persistence object if it's our custom one
    firestore_db_client = None
    # Ensure persistence and its firestore_client attribute exist
    if application and application.persistence and hasattr(application.persistence, 'firestore_client') and application.persistence.firestore_client:
        firestore_db_client = application.persistence.firestore_client
    else:
        logger.error("add_group_buy_to_firestore: Could not get Firestore client from application.persistence object or it's None.")
        # As a fallback, try to initialize a new client, but this is not ideal and might have loop issues
        try:
            gcp_project = os.environ.get('GCP_PROJECT')
            db_id = "garupa-group-buy" # Your named database
            if gcp_project:
                firestore_db_client = firestore.Client(project=gcp_project, database=db_id) # Use sync client for this helper
            else:
                firestore_db_client = firestore.Client(database=db_id)
            logger.warning("add_group_buy_to_firestore: Initialized a new SYNC Firestore client as it was not found on persistence object.")
        except Exception as e_fallback_fs:
            logger.error(f"add_group_buy_to_firestore: Failed to initialize fallback Firestore client: {e_fallback_fs}")
            return False

    if not firestore_db_client:
        logger.error("add_group_buy_to_firestore: Firestore client is not available.")
        return False

    try:
        doc_ref = firestore_db_client.collection("groupBuys").document(group_buy_id)
        # CustomFirestorePersistence uses a sync client, so Firestore operations need to be run in a thread
        await asyncio.to_thread(doc_ref.set, group_buy_details)
        logger.info(f"Successfully added group buy {group_buy_id} to Firestore collection 'groupBuys'.")
        return True
    except Exception as e:
        logger.error(f"Error adding group buy {group_buy_id} to Firestore: {e}", exc_info=True)
        return False


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
        async with application: # This ensures persistence data is loaded before handlers and flushed after
            await application.process_update(update_obj)
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
            # --- Use asyncio.run for cleaner loop management with nest_asyncio ---
            result, status_code = asyncio.run(_async_logic_ext(update_data))
            # ---
            logger.info(f"Async logic finished, sync wrapper returning result: '{result}', status: {status_code}")
            return result, status_code
        except Exception as e:
            logger.critical(f"!!! FATAL ERROR during JSON parsing or run_until_complete/asyncio.run: {e} !!!", exc_info=True)
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
        persistent=True if persistence else False, 
    )
    application.add_handler(conv_handler)
    logger.info(f"Added: Conversation handler for PRIVATE setup (persistent={conv_handler.persistent})")


    # --- Handler Count Logging ---
    logger.info(f"Handler count in application.handlers[0]: {len(application.handlers.get(0, []))}")

    # --- Cold Start Logging ---
    logger.info("✅ GCF Cold Start Completed: Application handlers initialized and ready.")

else:
    logger.warning("--- Skipping handler setup because Application initialization failed ---")