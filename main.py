import asyncio
import logging
import os
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiosqlite
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import GetDialogFiltersRequest

load_dotenv(encoding="utf-8")

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


# --- CONFIGURATION ---
@dataclass
class Config:
    api_id: int
    api_hash: str
    data_dir: Path
    session_name: str
    db_file: Path
    source_channels: List[str]
    source_folder: str
    destination_channel: int | str
    chunk_size: int
    delay_min: float
    delay_max: float


def load_config() -> Config:
    try:
        api_id = int(os.environ["API_ID"])
        api_hash = os.environ["API_HASH"]
    except KeyError as e:
        logging.critical(f"Missing mandatory environment variable: {e}")
        sys.exit(1)
    except ValueError:
        logging.critical("API_ID must be a valid integer.")
        sys.exit(1)

    data_dir = Path(os.environ.get("DATA_DIR", "/app/data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    session_name = str(data_dir / os.environ.get("SESSION_NAME", "Digest"))
    db_file = data_dir / os.environ.get("DB_FILE", "db.sqlite3")

    raw_channels = os.environ.get("SOURCE_CHANNELS", "")
    source_channels = [ch.strip() for ch in raw_channels.split(",") if ch.strip()]
    source_folder = os.environ.get("SOURCE_FOLDER", "").strip()

    if not source_channels and not source_folder:
        logging.critical(
            "Both SOURCE_CHANNELS and SOURCE_FOLDER are empty. Nothing to do."
        )
        sys.exit(1)

    raw_dest = os.environ.get("DESTINATION_CHANNEL", "me").strip()
    destination_channel = int(raw_dest) if raw_dest.lstrip("-").isdigit() else raw_dest

    return Config(
        api_id=api_id,
        api_hash=api_hash,
        data_dir=data_dir,
        session_name=session_name,
        db_file=db_file,
        source_channels=source_channels,
        source_folder=source_folder,
        destination_channel=destination_channel,
        chunk_size=int(os.environ.get("CHUNK_SIZE", "50")),
        delay_min=float(os.environ.get("DELAY_MIN", "2.0")),
        delay_max=float(os.environ.get("DELAY_MAX", "5.0")),
    )


# --- DATABASE OPERATIONS ---
class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.db_path)
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_states (
                channel TEXT PRIMARY KEY,
                last_id INTEGER NOT NULL
            )
        """)
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def cleanup(self, active_channels: List[str]):
        async with self.conn.execute("SELECT channel FROM channel_states") as cursor:
            stored_channels = [row[0] for row in await cursor.fetchall()]

        channels_to_remove = set(stored_channels) - set(active_channels)

        if channels_to_remove:
            for channel in channels_to_remove:
                await self.conn.execute(
                    "DELETE FROM channel_states WHERE channel = ?", (channel,)
                )
                logging.info(
                    f"Database cleanup: removed inactive channel ID '{channel}'."
                )
            await self.conn.commit()

    async def get_last_id(self, channel: str) -> Optional[int]:
        async with self.conn.execute(
            "SELECT last_id FROM channel_states WHERE channel = ?", (channel,)
        ) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else None

    async def update_last_id(self, channel: str, last_id: int):
        await self.conn.execute(
            """
            INSERT INTO channel_states (channel, last_id)
            VALUES (?, ?)
            ON CONFLICT(channel) DO UPDATE SET last_id = excluded.last_id
        """,
            (channel, last_id),
        )
        await self.conn.commit()


# --- UTILS ---
async def get_latest_message_id(
    client: TelegramClient, channel_id: int
) -> Optional[int]:
    try:
        async for message in client.iter_messages(channel_id, limit=1):
            return message.id
    except Exception as e:
        logging.error(f"Could not fetch latest message for ID {channel_id}: {e}")
        return None


def chunk_messages(
    messages: List[Tuple[int, Optional[int]]], chunk_size: int
) -> List[List[int]]:
    chunks = []
    current_chunk = []
    last_grouped_id = None

    for msg_id, grouped_id in messages:
        if len(current_chunk) >= chunk_size and (
            grouped_id is None or grouped_id != last_grouped_id
        ):
            chunks.append(current_chunk)
            current_chunk = []

        current_chunk.append(msg_id)
        last_grouped_id = grouped_id

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


# --- CORE LOGIC ---
async def resolve_sources(client: TelegramClient, config: Config) -> Dict[int, str]:
    """Resolves channel IDs and names from environment variables and Telegram folders."""
    final_channels = {}

    for ch in config.source_channels:
        try:
            if isinstance(ch, str) and ch.lstrip("-").isdigit():
                ch = int(ch)
            entity = await client.get_entity(ch)
            title = getattr(entity, "title", None) or getattr(
                entity, "username", str(entity.id)
            )
            final_channels[entity.id] = title
        except Exception as e:
            logging.error(f"Could not resolve channel {ch}: {e}")

    if config.source_folder:
        try:
            response = await client(GetDialogFiltersRequest())
            folder_filter = None
            target_folder = config.source_folder.lower()

            for f in response.filters:
                raw_title = getattr(f, "title", "")
                folder_name = (
                    raw_title.text if hasattr(raw_title, "text") else str(raw_title)
                )

                if folder_name.strip().lower() == target_folder:
                    folder_filter = f
                    break

            if folder_filter is not None:
                logging.info(
                    f"Found folder '{config.source_folder}'. Processing rules..."
                )

                if getattr(folder_filter, "broadcasts", False):
                    logging.info(
                        "Folder includes 'All Channels' flag. Scanning dialogs..."
                    )
                    async for dialog in client.iter_dialogs():
                        if dialog.is_channel:
                            final_channels[dialog.entity.id] = dialog.name

                peers = []
                if hasattr(folder_filter, "pinned_peers"):
                    peers.extend(folder_filter.pinned_peers)
                if hasattr(folder_filter, "include_peers"):
                    peers.extend(folder_filter.include_peers)

                for peer in peers:
                    try:
                        entity = await client.get_entity(peer)
                        if getattr(entity, "broadcast", False) or getattr(
                            entity, "megagroup", False
                        ):
                            title = getattr(entity, "title", str(entity.id))
                            final_channels[entity.id] = title
                    except Exception as e:
                        logging.warning(f"Could not resolve entity in folder: {e}")
            else:
                logging.warning(
                    f"Folder '{config.source_folder}' not found in your Telegram account."
                )
        except Exception as e:
            logging.error(f"Error fetching folder '{config.source_folder}': {e}")

    return final_channels


async def process_channel(
    client: TelegramClient,
    db: Database,
    config: Config,
    channel_id: int,
    channel_name: str,
) -> int:
    """Processes a single channel, forwards messages, and returns the forwarded count."""
    channel_str_id = str(channel_id)
    logging.info(f"Processing channel: {channel_name} (ID: {channel_str_id})")

    last_processed_id = await db.get_last_id(channel_str_id)

    if last_processed_id is None:
        logging.info(f"Channel {channel_name} is new. Initializing state...")
        latest_id = await get_latest_message_id(client, channel_id)

        if latest_id is not None:
            await db.update_last_id(channel_str_id, latest_id)
            logging.info(
                f"Initialized {channel_name} with ID: {latest_id}. Will forward new messages next run."
            )
        else:
            logging.error(f"Failed to initialize {channel_name}. Skipping.")
        return 0

    messages_to_forward = []
    max_id_in_channel = last_processed_id

    try:
        async for message in client.iter_messages(
            channel_id, min_id=last_processed_id, reverse=True
        ):
            if not message.action and (message.message or message.media):
                messages_to_forward.append((message.id, message.grouped_id))

            if message.id > max_id_in_channel:
                max_id_in_channel = message.id

        if not messages_to_forward:
            logging.info(f"No new messages found in {channel_name}")
            if max_id_in_channel > last_processed_id:
                await db.update_last_id(channel_str_id, max_id_in_channel)
            return 0

        chunks = chunk_messages(messages_to_forward, config.chunk_size)
        forwarded_in_channel = 0

        for chunk in chunks:
            retries = 3
            chunk_max_id = max(chunk)

            while retries > 0:
                try:
                    await client.forward_messages(
                        config.destination_channel, chunk, channel_id
                    )
                    forwarded_in_channel += len(chunk)
                    logging.info(
                        f"Successfully forwarded chunk of {len(chunk)} messages from {channel_name}"
                    )

                    await db.update_last_id(channel_str_id, chunk_max_id)

                    delay = random.uniform(config.delay_min, config.delay_max)
                    logging.info(
                        f"Sleeping for {delay:.2f} seconds to avoid rate limits."
                    )
                    await asyncio.sleep(delay)
                    break

                except FloodWaitError as e:
                    logging.warning(f"Rate limited. Sleeping for {e.seconds} seconds.")
                    await asyncio.sleep(e.seconds)

                except Exception as e:
                    logging.error(f"Error forwarding chunk: {e}")
                    retries -= 1
                    if retries > 0:
                        await asyncio.sleep(5)
                    else:
                        logging.error(
                            f"Failed to forward chunk from {channel_name} after max retries. Moving to next channel."
                        )
                        return forwarded_in_channel

        return forwarded_in_channel

    except Exception as e:
        logging.error(f"Fatal error processing channel {channel_name}: {e}")
        return 0


async def main():
    config = load_config()
    db = Database(config.db_file)
    await db.connect()

    total_forwarded = 0

    async with TelegramClient(
        config.session_name, config.api_id, config.api_hash
    ) as client:
        final_channels = await resolve_sources(client, config)

        if not final_channels:
            logging.critical(
                "No valid channels resolved from variables or folder. Exiting."
            )
            await db.close()
            return

        active_channel_ids = [str(cid) for cid in final_channels.keys()]
        await db.cleanup(active_channel_ids)

        for channel_id, channel_name in final_channels.items():
            forwarded = await process_channel(
                client, db, config, channel_id, channel_name
            )
            total_forwarded += forwarded

        try:
            if total_forwarded > 0:
                status_message = f"✅ Digest ready. Forwarded {total_forwarded} posts"
            else:
                status_message = "🤔 Scanned. No new posts found"

            await client.send_message(config.destination_channel, status_message)
            logging.info(f"Sent heartbeat notification: {status_message}")
        except Exception as e:
            logging.error(f"Failed to send heartbeat notification: {e}")

    await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Process interrupted by user.")
