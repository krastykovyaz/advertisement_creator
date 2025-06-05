import os
import logging
from typing import Dict, List
from PIL import Image
# Add pytz import for timezone handling
import pytz
from telegram import InputMediaPhoto, Update, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler, CallbackQueryHandler,
    ApplicationBuilder
)
from google import genai
from google.genai.types import GenerateContentConfig, Part
# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
GEMINI_API_KEY = 'AIzaSyACHH5mPmNSqyYasIsRnI7QPx-roM_Tk0Q'
# TELEGRAM_TOKEN = '1640633632:AAF5JR4iBRkn4lVw--cI6-ZIxRxCI87IMx8'
TELEGRAM_TOKEN = '7624621135:AAEyU65amsdMtNCYFlD0C-BXt_hGpbW_KbE'
MODEL_NAME = "gemini-2.0-flash"

ATTRIBUTION_TEXT = "\n\nСделано с помощью [Рекламный агент](https://t.me/advoffer_bot)"

# Define conversation states
PHOTO, DESCRIPTION, SUGGESTION, CONFIRMATION = range(4)


class PostGeneratorBot:
    def __init__(self):

        # Initialize Gemini
        self.gemini = genai.Client(api_key=GEMINI_API_KEY)

        # Create temp directory if it doesn't exist
        if not os.path.exists('temp'):
            os.makedirs('temp')

        # Set up Telegram bot with new Application builder pattern
        # Explicitly disable the job queue to avoid APScheduler issues
        builder = ApplicationBuilder().token(TELEGRAM_TOKEN)
        self.application = builder.build()

        # Add handlers to application
        self.application.add_handler(
            CommandHandler('done', self.handle_done_command))

        # Update conversation handler to include the command in DESCRIPTION state
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('start', self.start)],
            states={
                PHOTO: [
                    MessageHandler(filters.PHOTO, self.receive_photo),
                    CallbackQueryHandler(
                        self.handle_add_description, pattern='^add_description$')
                ],
                DESCRIPTION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND,
                                   self.handle_description),
                    CommandHandler('done', self.handle_done_command)
                ],
                CONFIRMATION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND,
                                   self.handle_confirmation),
                    CallbackQueryHandler(self.handle_confirmation)
                ],
                SUGGESTION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND,
                                   self.receive_correction)
                ],
            },
            fallbacks=[CommandHandler('cancel', self.cancel)],
        )

        self.application.add_handler(conv_handler)
        self.application.add_error_handler(self.error_handler)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Start the conversation and ask for photos."""
        user = update.effective_user
        if update.message:
            await update.message.reply_markdown_v2(
                fr'Hi {user.mention_markdown_v2()}\! Send me one or more photos for your post\.',
                reply_markup=ForceReply(selective=True),
            )

        # Initialize photo list
        context.user_data['photos'] = []
        return PHOTO

    async def handle_add_description(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        context.user_data['awaiting_description'] = True
        await query.edit_message_text(
            "✍️ *Please send your description now:*\n"
            "(Features, price, contact info, etc.)",
            parse_mode='Markdown'
        )
        return DESCRIPTION

    async def receive_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Receive and process photos with individual progress notifications."""
        try:
            # Initialize user_data if not exists
            if 'photos' not in context.user_data:
                context.user_data['photos'] = []
                context.user_data['processing_msgs'] = []

            current_count = len(context.user_data['photos']) + 1

            # Send processing notification
            processing_msg = await update.message.reply_text(
                f"🖼️ Processing photo {current_count}...",
                reply_to_message_id=update.message.message_id
            )

            # Ensure processing_msgs list exists
            if 'processing_msgs' not in context.user_data:
                context.user_data['processing_msgs'] = []
            context.user_data['processing_msgs'].append(processing_msg)

            # Download and process photo
            photo_file = await update.message.photo[-1].get_file()
            photo_path = f"temp/{update.effective_user.id}_{update.message.message_id}.jpg"
            await photo_file.download_to_drive(photo_path)

            # Новый способ: отправить изображение в Gemini для генерации подписи
            with open(photo_path, "rb") as img_file:
                image_bytes = img_file.read()
            image = Part.from_bytes(
                data=image_bytes, mime_type="image/jpeg"
            )

            # Используем Gemini для генерации подписи к изображению
            prompt = f"Проанализируй изображение и напиши ясное и краткое описание (одно предложение), так что бы в дальнейшем его можно было использовать для генерации текста рекламы"
            response = self.gemini.models.generate_content(
                model=MODEL_NAME,
                contents=[image, prompt],
                config=GenerateContentConfig(
                    temperature=0.7,
                    top_p=0.9,
                    max_output_tokens=100
                )
            )
            gemini_caption = response.text if hasattr(response, "text") else ""

            # Store results
            context.user_data['photos'].append({
                'path': photo_path,
                'caption': gemini_caption
            })

            # Update status
            await processing_msg.edit_text(
                f"✅ Photo {current_count} processed.\n\nYou can send more photos or type /done to continue."
            )

            # Prompt for more photos
            # if current_count == 1:
            #     await update.message.reply_text(
            #         "You can send more photos or type /done to continue.",
            #         reply_markup=ForceReply(selective=True),
            #     )
            return PHOTO

        except Exception as e:
            logger.error(f"Error processing photo: {e}")
            if 'processing_msg' in locals():
                await processing_msg.edit_text("❌ Failed to process this photo")
            await update.message.reply_text("Please try sending the photo again or /cancel to start over")
            return PHOTO

    def process_all_photos(self, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Final processing after all photos are received."""
        try:
            photos = context.user_data.get('photos', [])
            if not photos:
                return "no photos"

            # Generate combined description
            captions = [photo['caption']
                        for photo in context.user_data['photos']]
            combined_description = " ".join(captions)

            # Clean up
            self.cleanup_temp_files(context)

            return combined_description

        except Exception as e:
            logger.error(f"Error combining photos: {e}")
            return "the photos"

    def cleanup_temp_files(self, context: ContextTypes.DEFAULT_TYPE):
        """Clean up temporary files and status messages."""
        try:
            # Delete photo files
            for photo in context.user_data.get('photos', []):
                try:
                    if os.path.exists(photo['path']):
                        os.remove(photo['path'])
                except Exception as e:
                    logger.error(f"Error deleting {photo['path']}: {e}")

            # Clear data
            context.user_data.pop('photos', None)
            context.user_data.pop('processing_msgs', None)

        except Exception as e:
            logger.error(f"Cleanup error: {e}")

    async def receive_correction(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Receive user's corrected version of the post."""
        corrected_text = update.message.text
        context.user_data['corrected_text'] = corrected_text

        keyboard = [
            [InlineKeyboardButton("👍 Post It", callback_data="accept"),
             InlineKeyboardButton("✏️ Edit Again", callback_data="edit")]
        ]
        # await update.message.reply_text(
        #     f"Your edited version:\n\n{corrected_text}\n\nReady to post?",
        #     reply_markup=InlineKeyboardMarkup(keyboard)
        # )
        await update.message.reply_media_group(
            media=[
                InputMediaPhoto(
                    media=open(photo['path'], 'rb'),
                ) for photo in context.user_data.get('photos', [])
            ],
            caption=f"{corrected_text}{ATTRIBUTION_TEXT}",
            parse_mode='Markdown'
        )
        await update.message.reply_text(
            "What would you like to do with this post?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CONFIRMATION

    def generate_suggestion(self, user_data: Dict) -> str:
        """Generate post content using Gemini including the user's description."""
        try:
            image_caption = user_data.get('image_caption', '')
            logger.info(f"Image caption: {image_caption}")
            if not image_caption:
                captions = [photo['caption'] for photo in user_data['photos']]
                logger.info(f"Captions: {captions}")
                combined_description = " ".join(captions)
                image_caption = combined_description
            prompt = f"""
            *   **Визуальный контент:** {image_caption}
            *   **Описание от пользователя:** {user_data.get('description', '')}

            **Требования:**

            1.  Слей воедино визуальный контент и описание пользователя, чтобы создать мощный эффект.
            2.  Максимум 1-3 предложения – каждое слово должно цеплять!
            3.  Включи 3-5 "бьющих" хештегов, которые заставят людей кликнуть.
            4.  Используй дерзкий, провокационный тон.
            5.  Выдели самые "горячие" детали из описания пользователя, чтобы вызвать максимальный интерес.

            Пост должен быть как удар молнии – от изображений к описанию, не оставляя шанса пройти мимо!
            """

            response = self.gemini.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=GenerateContentConfig(
                    temperature=0.7,
                    top_p=0.9,
                    max_output_tokens=500
                )
            )
            return response.text

        except Exception as e:
            logger.error(f"Error generating suggestion: {e}")
            return "Check out my post! #social #post"

    async def handle_done_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle the /done command to finish photo uploads."""
        if not context.user_data.get('photos'):
            await update.message.reply_text("You haven't sent any photos yet! Please send at least one photo.")
            return PHOTO

        # Create inline keyboard with options
        keyboard = [
            [InlineKeyboardButton("✏️ Add description",
                                  callback_data="add_description")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "Would you like to add a description to your post?",
            reply_markup=reply_markup
        )
        return CONFIRMATION

    async def handle_confirmation(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle all button callbacks in confirmation state."""
        if update.callback_query:
            query = update.callback_query
            await query.answer()

            if query.data == "no_description":
                context.user_data['description'] = ""
                await query.edit_message_text("⏳ Generating your post without description...")
                return await self.generate_final_post(update, context)

            elif query.data == "add_description":
                # Store that we're waiting for description
                context.user_data['awaiting_description'] = True
                await query.edit_message_text(
                    "✍️ *Please send your description now:*\n"
                    "(Features, price, contact info, etc.)",
                    parse_mode='Markdown'
                )
                return DESCRIPTION

            elif query.data == "accept":
                await query.edit_message_text(
                    f"✅ Post approved!\n\n"
                    "Use /start to create another post."
                )
                return ConversationHandler.END

            elif query.data == "edit":
                current_text = context.user_data.get('suggestion', '')
                await query.edit_message_text(
                    f"✏️ Current post text:\n\n{current_text}\n\n"
                    "Please send your corrected version in the chat."
                )
                return SUGGESTION

            elif query.data == "regenerate":
                await query.edit_message_text("🔄 Generating new version...")
                new_suggestion = self.generate_suggestion(context.user_data)
                context.user_data['suggestion'] = new_suggestion

                keyboard = [
                    [InlineKeyboardButton("👍 Accept", callback_data="accept"),
                     InlineKeyboardButton("✏️ Edit", callback_data="edit"),
                     InlineKeyboardButton("🔄 Regenerate", callback_data="regenerate")]
                ]
                await query.edit_message_text(
                    f"📝 New Version generated!",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                await query.message.reply_media_group(
                    media=[
                        InputMediaPhoto(
                            media=open(photo['path'], 'rb'),
                        ) for photo in context.user_data.get('photos', [])
                    ],
                    caption=f"{new_suggestion}{ATTRIBUTION_TEXT}",
                )
                await query.message.reply_text(
                    "What would you like to do with this post?",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return CONFIRMATION
            else:
                await query.edit_message_text("⚠️ Unknown action. Please try again.")
                return CONFIRMATION
        else:
            # Handle text message in confirmation state
            return CONFIRMATION

    async def receive_description(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        logger.info("Receiving description")
        user_text = update.message.text.strip()
        context.user_data['description'] = user_text
        logger.info(f"User description received: {user_text}")
        try:
            if 'image_caption' not in context.user_data:
                logger.info("Generating image caption")
                image_description = self.process_all_photos(context)
                context.user_data['image_caption'] = image_description
                logger.info(f"Image caption: {image_description}")

            suggestion = self.generate_suggestion(context.user_data)
            context.user_data['suggestion'] = suggestion
            logger.info(f"Generated suggestion: {suggestion}")

            keyboard = [
                [InlineKeyboardButton("👍 Accept", callback_data="accept"),
                 InlineKeyboardButton("✏️ Edit", callback_data="edit"),
                 InlineKeyboardButton("🔄 Regenerate", callback_data="regenerate")]
            ]
            # await update.message.reply_text(
            #     f"📝 Post Suggestion:\n\n{suggestion}\n\n"
            #     "What would you like to do?",
            #     reply_markup=InlineKeyboardMarkup(keyboard)
            # )
            await update.message.reply_media_group(
                media=[
                    InputMediaPhoto(
                        media=open(photo['path'], 'rb'),
                    ) for photo in context.user_data.get('photos', [])
                ],
                caption=f"{suggestion}{ATTRIBUTION_TEXT}",
                parse_mode='Markdown'
            )
            await update.message.reply_text(
                "What would you like to do with this post?",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return CONFIRMATION

        except Exception as e:
            logger.error(f"Error in receive_description: {e}")
            await update.message.reply_text("⚠️ Something went wrong while generating the post.")
            return ConversationHandler.END

    async def generate_final_post(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Process and generate final post when description is skipped."""
        try:
            # Generate image captions if not already done
            if 'image_caption' not in context.user_data:
                image_description = self.process_all_photos(context)
                context.user_data['image_caption'] = image_description

            # Generate suggestion
            suggestion = self.generate_suggestion(context.user_data)
            context.user_data['suggestion'] = suggestion

            # Show final suggestion with options
            keyboard = [
                [InlineKeyboardButton("👍 Accept", callback_data="accept"),
                 InlineKeyboardButton("✏️ Edit", callback_data="edit"),
                 InlineKeyboardButton("🔄 Regenerate", callback_data="regenerate")]
            ]
            await update.callback_query.message.reply_text(
                f"{suggestion}{ATTRIBUTION_TEXT}\n\n"
                "What would you like to do?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
            return CONFIRMATION

        except Exception as e:
            logger.error(f"Error in generate_final_post: {e}")
            await update.callback_query.message.reply_text("⚠️ Failed to generate post. Please try again.")
            return ConversationHandler.END

    async def handle_description(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()

        # Сохраняем описание
        context.user_data['description'] = text
        context.user_data['awaiting_description'] = False

        # Создаём итоговый пост
        suggestion = self.generate_suggestion(context.user_data)
        context.user_data['suggestion'] = suggestion

        # Если хочешь — предложи кнопки: "Редактировать", "Опубликовать", "Отменить"
        keyboard = [
            [
                InlineKeyboardButton("✅ Publish", callback_data="accept"),
                InlineKeyboardButton("✏️ Edit", callback_data="edit"),
                InlineKeyboardButton(
                    "🔄 Regenerate", callback_data="regenerate"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        # Edit existing message if possible
        # if update.callback_query:
        #     await update.callback_query.edit_message_text(
        #         f"📝 *Generated Post:*\n\n{suggestion}\n\n"
        #         "What would you like to do?",
        #         reply_markup=InlineKeyboardMarkup(keyboard),
        #         parse_mode='Markdown'
        #     )
        # else:
        #     await update.message.reply_text(
        #         f"📝 *Generated Post:*\n\n{suggestion}\n\n"
        #         "What would you like to do?",
        #         reply_markup=InlineKeyboardMarkup(keyboard),
        #         parse_mode='Markdown'
        #     )
        messages = await update.message.reply_media_group(
            media=[
                InputMediaPhoto(
                    media=open(photo['path'], 'rb')
                ) for photo in context.user_data.get('photos', [])
            ],
            caption=f"{suggestion}{ATTRIBUTION_TEXT}",
            parse_mode='Markdown'
        )

        # await messages[0].edit_reply_markup (
        #     reply_markup=InlineKeyboardMarkup(keyboard)
        # )
        await update.message.reply_text(
            "What would you like to do with this post?",
            reply_markup=reply_markup
        )

        return CONFIRMATION

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Cancel the conversation."""
        await update.message.reply_text('Operation cancelled. Send /start to begin again.')
        return ConversationHandler.END

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle errors."""
        logger.error(f"Update {update} caused error {context.error}")
        if update and update.message:
            await update.message.reply_text("An error occurred. Please try again.")

    def run(self):
        """Run the bot."""
        self.application.run_polling()


if __name__ == '__main__':
    bot = PostGeneratorBot()
    bot.run()
