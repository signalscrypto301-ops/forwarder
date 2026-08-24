import asyncio
from datetime import datetime
import os

import requests
import aiogram
from aiogram import types, executor
from aiogram.types import (
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ParseMode,
    ContentType,
    MessageEntity,
)
import qrcode
from telethon import TelegramClient

import config
from logger import logger
from database import (
    add_channel,
    add_group_for_channel,
    get_all_channels,
    get_groups_for_channel,
    delete_group_for_channel,
)

bot = aiogram.Bot(config.bot_token)
dp = aiogram.Dispatcher(bot)


bot_client = TelegramClient("bot_client", config.API_ID, config.API_HASH).start(
    bot_token=config.bot_token
)


def generate_qr_code(qr_data):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    image_path = os.path.join(os.getcwd(), f"{qr_data[10]}.png")
    img.save(image_path)
    return image_path


@dp.message_handler(commands=["start"])
async def start_command(message: Message):
    await message.reply("Hello")


@dp.message_handler(commands=["help"])
async def help_command(message: Message):
    await message.reply("Help")


@dp.message_handler(commands=["get_chat_id"])
async def get_chat_id_command(message: Message):
    if not message.chat.type == "private":
        return

    user_id = message.from_user.id
    if not user_id in config.admin_ids:
        return

    args = message.get_args()
    if not args:
        await message.reply("Please provide a group name")
        return

    data = {
        "chatName": args,
        "clientId": "user",
    }

    res = requests.post(url=f"{config.whatsapp_service}/getChatId", json=data)
    res_data = res.json()
    if res.status_code == 200:
        await message.reply(
            f"Group Id : <code>{res_data.get('groupId')}</code>",
            parse_mode=ParseMode.HTML,
        )
    elif res.status_code == 400 or res.status_code == 404:
        await message.reply(f"{res_data.get('message')}", parse_mode=ParseMode.HTML)
    else:
        await message.reply("Something went wrong.")


@dp.message_handler(commands=["login"])
async def login_whatsapp(message: Message):
    data = {
        "clientId": "user",
    }

    res = requests.post(url=f"{config.whatsapp_service}/createsession", json=data)
    res_data = res.json()

    if res.status_code == 200:
        qr_data = res_data.get("qrcode")
        qr_image = generate_qr_code(qr_data)

        with open(qr_image, "rb") as qr_image:
            await message.reply_photo(qr_image)
            os.remove(qr_image.name)

    elif res.status_code == 400:
        await message.reply(f"{res_data.get('message')}")


@dp.message_handler(commands=["listen"])
async def listen_whatsapp(message: Message):

    if not message.chat.type == "private":
        return

    user_id = message.from_user.id
    if not user_id in config.admin_ids:
        return

    data = {
        "clientId": "user",
    }

    res = requests.post(url=f"{config.whatsapp_service}/startlistening", json=data)
    res_data = res.json()
    if res.status_code == 200:
        await message.reply(f"{res_data.get('message')}")
    elif res.status_code == 400:
        await message.reply(f"{res_data.get('message')}")
    else:
        await message.reply("Something went wrong.")


@dp.message_handler(commands=["logout"])
async def logout_whatsapp(message: Message):
    if not message.chat.type == "private":
        return

    user_id = message.from_user.id
    if not user_id in config.admin_ids:
        return

    data = {
        "clientId": "user",
    }

    res = requests.post(url=f"{config.whatsapp_service}/logout", json=data)
    if res.status_code == 200:
        await message.reply("Logged out and session files removed successfully.")
    elif res.status_code == 400:
        await message.reply(f"{res.json().get('message')}")
    else:
        await message.reply("Failed to log out. Please try again later.")


@dp.message_handler(commands=["add_group"])
async def add_group_command(message: Message):
    if not message.chat.type == "private":
        return

    user_id = message.from_user.id
    if not user_id in config.admin_ids:
        return

    args = message.get_args()
    if not args or len(args.split()) != 2:
        await message.reply("Usage: /add_group <channel_id> <group_id>")
        return

    channel_id, group_id = args.split()
    add_group_for_channel(channel_id, group_id)
    await message.reply(f"Group {group_id} added for channel {channel_id}.")


@dp.message_handler(commands=["delete_group"])
async def delete_group_command(message: Message):
    if not message.chat.type == "private":
        return

    user_id = message.from_user.id
    if not user_id in config.admin_ids:
        return

    args = message.get_args()
    if not args or len(args.split()) != 2:
        await message.reply("Usage: /delete_group <channel_id> <group_id>")
        return

    channel_id, group_id = args.split()
    delete_group_for_channel(channel_id, group_id)
    await message.reply(f"Group {group_id} deleted for channel {channel_id}.")


@dp.message_handler(commands=["view_groups"])
async def view_groups_command(message: Message):
    if not message.chat.type == "private":
        return

    user_id = message.from_user.id
    if not user_id in config.admin_ids:
        return

    channels = get_all_channels()
    if not channels:
        await message.reply("No channels found.")
        return

    reply = "Channels and WhatsApp Groups:\n"
    for ch in channels:
        groups = get_groups_for_channel(ch)
        reply += f"Channel: <code>{ch}</code>\nGroups: {', '.join(groups) if groups else 'None'}\n\n"
    await message.reply(reply, parse_mode=ParseMode.HTML)


@dp.message_handler(commands=["add_channel"])
async def add_channel_command(message: Message):
    if not message.chat.type == "private":
        return

    user_id = message.from_user.id
    if not user_id in config.admin_ids:
        return

    args = message.get_args()
    if not args:
        await message.reply("Please provide a channel ID.")
        return

    add_channel(args)
    await message.reply(f"Channel {args} added.")


@dp.message_handler(commands=["add_wp_channel"])
async def add_group_for_channel_command(message: Message):
    if not message.chat.type == "private":
        return

    user_id = message.from_user.id
    if not user_id in config.admin_ids:
        return

    args = message.get_args()
    if not args or len(args.split()) != 2:
        await message.reply("Usage: /add_group_for_channel <channel_id> <group_id>")
        return

    channel_id, group_id = args.split()

    # Check if the channel ID exists
    channels = get_all_channels()
    if channel_id not in channels:
        await message.reply(f"Channel ID {channel_id} does not exist. Please add the channel first.")
        return

    add_group_for_channel(channel_id, group_id)
    await message.reply(f"Group {group_id} added for channel {channel_id}.")


@dp.message_handler(commands=["view_channels"])
async def view_channels_command(message: Message):
    if not message.chat.type == "private":
        return

    user_id = message.from_user.id
    if not user_id in config.admin_ids:
        return

    channels = get_all_channels()
    if not channels:
        await message.reply("No channels found.")
        return

    reply = "Channels and WhatsApp Groups:\n"
    for ch in channels:
        groups = get_groups_for_channel(ch)
        reply += f"Channel: <code>{ch}</code>\nGroups: {', '.join(groups) if groups else 'None'}\n\n"
    await message.reply(reply, parse_mode=ParseMode.HTML)


def format_caption_for_whatsapp(caption: str, entities: list[MessageEntity]) -> str:
    if not caption or not entities:
        return caption

    # Convert caption to UTF-16 to match Telegram's offset logic
    utf16_text = caption.encode("utf-16-le")
    code_units = []
    i = 0
    while i < len(utf16_text):
        unit = utf16_text[i : i + 2]
        code_units.append(unit)
        i += 2

    # Map from UTF-16 unit index to Python string index
    utf16_to_str_idx = {}
    str_idx = 0
    utf16_idx = 0
    for ch in caption:
        utf16_to_str_idx[utf16_idx] = str_idx
        utf16_len = len(ch.encode("utf-16-le")) // 2
        utf16_idx += utf16_len
        str_idx += 1

    # Collect where to insert symbols
    insertions = {}

    for ent in entities:
        symbol = ""
        if ent.type == "bold":
            symbol = "*"
        elif ent.type == "italic":
            symbol = "_"
        elif ent.type == "strikethrough":
            symbol = "~"
        else:
            continue  # unsupported

        start = utf16_to_str_idx.get(ent.offset)
        end = utf16_to_str_idx.get(ent.offset + ent.length)

        if start is not None:
            insertions.setdefault(start, []).append(symbol)
        if end is not None:
            insertions.setdefault(end, []).append(symbol)

    # Build result string with correct offsets
    result = []
    for i, ch in enumerate(caption):
        if i in insertions:
            result.extend(insertions[i])
        result.append(ch)

    # Add any insertions after the last char
    if len(caption) in insertions:
        result.extend(insertions[len(caption)])

    return "".join(result)


@dp.channel_post_handler(
    content_types=[ContentType.TEXT, ContentType.PHOTO, ContentType.VIDEO]
)
async def handle_channel_post(message: types.Message):
    channel_id = str(message.chat.id)
    groups = get_groups_for_channel(channel_id)
    if not groups:
        return

    caption = ""
    downloaded_media = None
    if message.content_type == ContentType.TEXT:
        caption = message.text
    elif message.content_type == ContentType.PHOTO:
        caption = message.caption
        photo = message.photo[-1]  # Get the highest resolution photo
        photo_path = f"{photo.file_unique_id}.jpg"
        await photo.download(destination_file=photo_path)
        downloaded_media = photo_path
    # download the video if it exists, download upto 100 MB only
    elif message.content_type == ContentType.VIDEO:
        caption = message.caption
        media_dir = "media"
        os.makedirs(media_dir, exist_ok=True)
        video_file_path = os.path.join(media_dir, f"{message.video.file_unique_id}.mp4")
        video_message = await bot_client.get_messages(
            message.chat.id, ids=message.message_id
        )

        await video_message.download_media(file=video_file_path)

        # check file size is less than 100 MB
        if os.path.getsize(video_file_path) > 100 * 1024 * 1024:
            os.remove(video_file_path)
            logger.info("Video size exceeds 100 MB limit.")
            return

        downloaded_media = video_file_path

    caption = format_caption_for_whatsapp(caption, message.caption_entities or [])

    for group in groups:
        if downloaded_media:
            # Sending media message
            with open(downloaded_media, "rb") as media_file:
                files = {"media": media_file}
                data = {"clientId": "user", "groupId": group, "caption": caption}
                try:
                    response = requests.post(
                        url=f"{config.whatsapp_service}/sendMedia",
                        data=data,
                        files=files,
                    )
                    if response.status_code == 200:
                        logger.info(
                            f"Media message sent to group {group} successfully."
                        )
                    else:
                        logger.error(
                            f"Failed to send media message to group {group}: {response.json().get('message')}"
                        )
                except Exception as e:
                    logger.error(f"Error sending media message to group {group}: {e}")
        else:
            # Sending text message
            data = {"clientId": "user", "groupId": group, "text": caption}
            try:
                response = requests.post(
                    url=f"{config.whatsapp_service}/sendText", json=data
                )
                if response.status_code == 200:
                    logger.info(f"Text message sent to group {group} successfully.")
                else:
                    logger.error(
                        f"Failed to send text message to group {group}: {response.json().get('message')}"
                    )
            except Exception as e:
                logger.error(f"Error sending text message to group {group}: {e}")

    if downloaded_media:
        os.remove(downloaded_media)


async def main(_):
    print("Bot is up")


if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=main)
