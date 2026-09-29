# ============================================================
# TELEGRAM EXPORTER
#
# Session Manager
# PV Manager
# Group Manager
# Channel Manager
# Message Explorer
# Date Jump
# Search
# Media Viewer
# JSON Export
#
# IMPORTANT FIXES
# 1. /start handler is registered BEFORE generic text handler
# 2. Photo download has multiple reliable fallback methods
# 3. Video download uses explicit file path
# 4. Media gets re-fetched when necessary
# 5. message.media is always inspected
# 6. message is not modified is safely ignored
# ============================================================

import asyncio
import html
import json
import logging
import mimetypes
import re
import shutil

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

from google.colab import userdata

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyUnregisteredError,
    AuthKeyDuplicatedError,
    FloodWaitError,
)
from telethon.tl.types import (
    User,
    Chat,
    Channel,
)


# ============================================================
# 1. SECRETS
# ============================================================

try:

    BOT_TOKEN = userdata.get("BOT_TOKEN")
    API_ID_RAW = userdata.get("API_ID")
    API_HASH = userdata.get("API_HASH")
    ADMIN_ID_RAW = userdata.get("ADMIN_ID")

except Exception as exc:

    raise RuntimeError(
        f"❌ Cannot read Colab Secrets: {exc}"
    )


BOT_TOKEN = BOT_TOKEN or ""
API_ID_RAW = API_ID_RAW or ""
API_HASH = API_HASH or ""
ADMIN_ID_RAW = ADMIN_ID_RAW or ""


if not BOT_TOKEN:
    raise RuntimeError(
        "❌ BOT_TOKEN is missing."
    )

if not API_ID_RAW:
    raise RuntimeError(
        "❌ API_ID is missing."
    )

if not API_HASH:
    raise RuntimeError(
        "❌ API_HASH is missing."
    )

if not ADMIN_ID_RAW:
    raise RuntimeError(
        "❌ ADMIN_ID is missing."
    )


try:

    API_ID = int(
        API_ID_RAW
    )

except ValueError:

    raise RuntimeError(
        "❌ API_ID must be numeric."
    )


try:

    ADMIN_ID = int(
        ADMIN_ID_RAW
    )

except ValueError:

    raise RuntimeError(
        "❌ ADMIN_ID must be numeric Telegram UID."
    )


# ============================================================
# 2. PATHS
# ============================================================

PROJECT_ROOT = Path(
    "/content/drive/MyDrive/"
    "TelegramProjects/TelegramExporter"
)

SESSIONS_ACTIVE_DIR = (
    PROJECT_ROOT
    / "01_Sessions"
    / "Active"
)

SESSIONS_DISABLED_DIR = (
    PROJECT_ROOT
    / "01_Sessions"
    / "Disabled"
)

SESSIONS_INVALID_DIR = (
    PROJECT_ROOT
    / "01_Sessions"
    / "Invalid"
)

EXPORTS_RUNNING_DIR = (
    PROJECT_ROOT
    / "03_Exports"
    / "Running"
)

EXPORTS_COMPLETED_DIR = (
    PROJECT_ROOT
    / "03_Exports"
    / "Completed"
)

EXPORTS_FAILED_DIR = (
    PROJECT_ROOT
    / "03_Exports"
    / "Failed"
)

EXPORT_REPORTS_DIR = (
    PROJECT_ROOT
    / "08_Reports"
    / "Exports"
)

LOCAL_VIEWER_DIR = Path(
    "/content/TelegramExporterViewer"
)

DESKTOP_EXPORTS_DIR = PROJECT_ROOT / "09_Desktop_Exports"
SAVED_MESSAGES_PAGE_SIZE = 20
saved_messages_state = {}


for directory in (
    SESSIONS_ACTIVE_DIR,
    SESSIONS_DISABLED_DIR,
    SESSIONS_INVALID_DIR,
    EXPORTS_RUNNING_DIR,
    EXPORTS_COMPLETED_DIR,
    EXPORTS_FAILED_DIR,
    EXPORT_REPORTS_DIR,
    LOCAL_VIEWER_DIR,
    DESKTOP_EXPORTS_DIR,
):

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )


# ============================================================
# 3. SETTINGS
# ============================================================

DIALOG_PAGE_SIZE = 8

DEFAULT_VIEWER_PAGE_SIZE = 20

VIEWER_PAGE_SIZES = (
    5,
    10,
    20,
    50,
    100,
)

MAX_MEDIA_VIEW_SIZE_MB = 49

# Viewer files are temporary and kept locally only
KEEP_VIEWER_FILES = False

# Retry download attempts
MEDIA_DOWNLOAD_RETRIES = 3


# ============================================================
# 4. LOGGING
# ============================================================

LOG_DIR = (
    PROJECT_ROOT
    / "06_Logs"
    / "Bot"
)

LOG_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger(
    "TelegramExporter"
)


# ============================================================
# 5. MODELS
# ============================================================

@dataclass
class DialogInfo:

    id: int
    title: str
    kind: str
    username: Optional[str] = None


# ============================================================
# 6. RUNTIME STATE
# ============================================================

selected_session: Dict[
    int,
    str
] = {}

selected_dialogs: Dict[
    int,
    Dict[str, Set[int]]
] = {}

current_pages: Dict[
    int,
    Dict[str, int]
] = {}

viewer_state: Dict[
    int,
    Dict
] = {}

# Persistent viewer settings per chat.
# Key: (telegram_user_id, kind, dialog_id)
viewer_chat_profiles: Dict[
    tuple,
    Dict
] = {}

# Saved Messages has its own persistent profile.
saved_view_profiles: Dict[
    int,
    Dict
] = {}

viewer_message_ids: Dict[
    int,
    List[int]
] = {}

pending_input: Dict[
    int,
    Dict
] = {}


# ============================================================
# 7. STATE HELPERS
# ============================================================

def get_selections(
    user_id: int,
):

    if user_id not in selected_dialogs:

        selected_dialogs[user_id] = {
            "pv": set(),
            "group": set(),
            "channel": set(),
        }

    return selected_dialogs[user_id]


def get_pages(
    user_id: int,
):

    if user_id not in current_pages:

        current_pages[user_id] = {
            "pv": 0,
            "group": 0,
            "channel": 0,
        }

    return current_pages[user_id]


def get_viewer_state(
    user_id: int,
):

    if user_id not in viewer_state:

        viewer_state[user_id] = {
            "kind": None,
            "dialog_id": None,
            "page": 0,
            "page_size": DEFAULT_VIEWER_PAGE_SIZE,
            "date": None,
            "date_mode": None,
            "filter": "all",
            "search": None,
        }

    return viewer_state[user_id]


def get_chat_view_profile(
    user_id: int,
    kind: str,
    dialog_id: int,
):
    """Persistent viewer settings for each individual chat."""

    key = (user_id, kind, int(dialog_id))

    if key not in viewer_chat_profiles:
        viewer_chat_profiles[key] = {
            "page": 0,
            "page_size": DEFAULT_VIEWER_PAGE_SIZE,
            "date": None,
            "date_mode": None,
            "filter": "all",
            "search": None,
        }

    return viewer_chat_profiles[key]


def sync_viewer_state_from_profile(
    user_id: int,
    kind: str,
    dialog_id: int,
):
    profile = get_chat_view_profile(
        user_id,
        kind,
        dialog_id,
    )

    state = get_viewer_state(user_id)
    state.clear()
    state.update({
        "kind": kind,
        "dialog_id": int(dialog_id),
        **profile,
    })
    return state

def save_viewer_state_to_profile(
    user_id: int,
    kind: str,
    dialog_id: int,
):
    state = get_viewer_state(user_id)
    profile = get_chat_view_profile(
        user_id,
        kind,
        dialog_id,
    )
    for key in (
        "page",
        "page_size",
        "date",
        "date_mode",
        "filter",
        "search",
    ):
        profile[key] = state.get(key)


def get_saved_view_profile(user_id: int):
    if user_id not in saved_view_profiles:
        saved_view_profiles[user_id] = {
            "page": 0,
            "page_size": DEFAULT_VIEWER_PAGE_SIZE,
        }
    return saved_view_profiles[user_id]


# ============================================================
# 8. SECURITY
# ============================================================

def is_admin(
    user_id: int,
) -> bool:

    return (
        user_id == ADMIN_ID
    )


# ============================================================
# 9. SAFE BOT HELPERS
# ============================================================

async def safe_answer(
    callback: CallbackQuery,
    text: Optional[str] = None,
    show_alert: bool = False,
):

    try:

        await callback.answer(
            text=text,
            show_alert=show_alert,
        )

    except Exception:

        pass


async def safe_edit_text(
    message: Message,
    text: str,
    reply_markup=None,
    parse_mode: Optional[str] = None,
):
    """
    Edit a bot message safely.

    Handles:
      - message is not modified
      - message to edit not found
      - message can't be edited

    When the original control/status message disappeared,
    create a replacement message instead of raising a second
    TelegramBadRequest and crashing the callback handler.
    """

    try:

        await message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )

        return True

    except TelegramBadRequest as exc:

        error = str(exc).lower()

        if "message is not modified" in error:
            return False

        if (
            "message to edit not found" in error
            or "message can't be edited" in error
            or "message to edit not found" in error
        ):
            try:
                await bot.send_message(
                    chat_id=message.chat.id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
                return True
            except Exception:
                logger.exception(
                    "Replacement status message failed"
                )
                return False

        raise


async def safe_edit_markup(
    message: Message,
    reply_markup=None,
):

    try:

        await message.edit_reply_markup(
            reply_markup=reply_markup
        )

        return True

    except TelegramBadRequest as exc:

        if (
            "message is not modified"
            in str(exc).lower()
        ):

            return False

        raise


# ============================================================
# 10. MEDIA DETECTION
# ============================================================

def get_media_type(
    message,
) -> Optional[str]:

    # --------------------------------------------------------
    # Explicit properties
    # --------------------------------------------------------

    if getattr(
        message,
        "photo",
        None,
    ):

        return "photo"

    if getattr(
        message,
        "video",
        None,
    ):

        return "video"

    if getattr(
        message,
        "voice",
        None,
    ):

        return "voice"

    if getattr(
        message,
        "audio",
        None,
    ):

        return "audio"

    if getattr(
        message,
        "gif",
        None,
    ):

        return "gif"

    if getattr(
        message,
        "sticker",
        None,
    ):

        return "sticker"

    # --------------------------------------------------------
    # Generic media class
    # --------------------------------------------------------

    media = getattr(
        message,
        "media",
        None,
    )

    if media:

        media_class = (
            type(media).__name__
        )

        if media_class == (
            "MessageMediaPhoto"
        ):

            return "photo"

        if media_class == (
            "MessageMediaWebPage"
        ):

            return "webpage"

        if media_class in (
            "MessageMediaGeo",
            "MessageMediaGeoLive",
        ):

            return "geo"

        if media_class == (
            "MessageMediaContact"
        ):

            return "contact"

        if media_class == (
            "MessageMediaPoll"
        ):

            return "poll"

        if media_class == (
            "MessageMediaVenue"
        ):

            return "venue"

        if media_class == (
            "MessageMediaDice"
        ):

            return "dice"

    # --------------------------------------------------------
    # Document
    # --------------------------------------------------------

    document = getattr(
        message,
        "document",
        None,
    )

    if document:

        mime = (
            getattr(
                document,
                "mime_type",
                None,
            )
            or ""
        ).lower()

        filename = ""

        try:

            filename = (
                getattr(
                    message.file,
                    "name",
                    None,
                )
                or ""
            ).lower()

        except Exception:

            pass

        if mime.startswith("video/"):
            return "video"

        if mime.startswith("audio/"):
            return "audio"

        if mime == "image/gif":
            return "gif"

        if filename.endswith(
            (
                ".mp4",
                ".m4v",
                ".mov",
                ".mkv",
                ".avi",
                ".webm",
            )
        ):

            return "video"

        if filename.endswith(
            (
                ".mp3",
                ".m4a",
                ".wav",
                ".flac",
                ".aac",
                ".ogg",
            )
        ):

            return "audio"

        if filename.endswith(".gif"):
            return "gif"

        if filename.endswith(".tgs"):
            return "sticker"

        return "document"

    return None


def media_matches_filter(
    message,
    filter_name: str,
) -> bool:

    if filter_name == "all":
        return True

    if filter_name == "media":

        return bool(
            getattr(
                message,
                "media",
                None,
            )
        )

    return (
        get_media_type(message)
        == filter_name
    )


# ============================================================
# 11. SESSION MANAGER
# ============================================================

class TelegramSessionManager:

    def __init__(self):

        self.clients = {}
        self.locks = {}

    def get_lock(
        self,
        session_name: str,
    ):

        if session_name not in self.locks:

            self.locks[
                session_name
            ] = asyncio.Lock()

        return self.locks[
            session_name
        ]

    def find_session(
        self,
        session_name: str,
    ) -> Optional[Path]:

        for folder in (
            SESSIONS_ACTIVE_DIR,
            SESSIONS_DISABLED_DIR,
            SESSIONS_INVALID_DIR,
        ):

            candidate = (
                folder
                / session_name
            )

            if candidate.exists():
                return candidate

        return None

    def list_all_sessions(self):

        result = []

        folders = {
            "active": SESSIONS_ACTIVE_DIR,
            "disabled": SESSIONS_DISABLED_DIR,
            "invalid": SESSIONS_INVALID_DIR,
        }

        for status, folder in folders.items():

            for file in folder.glob(
                "*.session"
            ):

                result.append({
                    "name": file.name,
                    "path": file,
                    "status": status,
                })

        return sorted(
            result,
            key=lambda x:
                x["name"].lower(),
        )

    async def connect(
        self,
        session_name: str,
    ):

        session_path = self.find_session(
            session_name
        )

        if not session_path:

            raise FileNotFoundError(
                f"Session not found: {session_name}"
            )

        existing = self.clients.get(
            session_name
        )

        if (
            existing
            and existing.is_connected()
        ):

            return existing

        session_base = (
            session_path.with_suffix("")
        )

        client = TelegramClient(
            str(session_base),
            API_ID,
            API_HASH,
            device_model="Telegram Exporter",
            system_version="Google Colab",
            app_version="1.0",
            sequential_updates=True,
        )

        try:

            await client.connect()

            if not await client.is_user_authorized():

                await client.disconnect()

                raise AuthKeyUnregisteredError(
                    request=None
                )

            self.clients[
                session_name
            ] = client

            return client

        except AuthKeyDuplicatedError:

            # The server rejected this authorization key because
            # the same session is being used concurrently from
            # different IPs. Always close the local connection so
            # Telethon's background send/receive tasks are cleaned up.
            try:

                await client.disconnect()

            except Exception:

                pass

            self.clients.pop(
                session_name,
                None,
            )

            raise

        except Exception:

            # Make sure partially-started Telethon background tasks
            # do not survive the failed connection.
            try:

                await client.disconnect()

            except Exception:

                pass

            self.clients.pop(
                session_name,
                None,
            )

            raise

    async def disconnect(
        self,
        session_name: str,
    ):

        client = self.clients.pop(
            session_name,
            None,
        )

        if client:

            try:
                await client.disconnect()
            except Exception:
                logger.exception(
                    "Disconnect failed"
                )

    async def get_me(
        self,
        session_name: str,
    ):

        async with self.get_lock(
            session_name
        ):

            client = await self.connect(
                session_name
            )

            return await client.get_me()

    # ========================================================
    # DIALOGS
    # ========================================================

    async def get_dialogs(
        self,
        session_name: str,
        kind: str,
    ) -> List[DialogInfo]:

        async with self.get_lock(
            session_name
        ):

            client = await self.connect(
                session_name
            )

            result = []

            async for dialog in (
                client.iter_dialogs()
            ):

                entity = dialog.entity

                # ------------------------------------------------
                # PV
                # ------------------------------------------------

                if kind == "pv":

                    if isinstance(
                        entity,
                        User,
                    ):

                        result.append(
                            DialogInfo(
                                id=dialog.id,
                                title=(
                                    dialog.name
                                    or "Unknown"
                                ),
                                kind="pv",
                                username=(
                                    getattr(
                                        entity,
                                        "username",
                                        None,
                                    )
                                ),
                            )
                        )

                # ------------------------------------------------
                # GROUP
                # ------------------------------------------------

                elif kind == "group":

                    if isinstance(
                        entity,
                        Chat,
                    ):

                        result.append(
                            DialogInfo(
                                id=dialog.id,
                                title=(
                                    dialog.name
                                    or "Group"
                                ),
                                kind="group",
                            )
                        )

                    elif isinstance(
                        entity,
                        Channel,
                    ):

                        if getattr(
                            entity,
                            "megagroup",
                            False,
                        ):

                            result.append(
                                DialogInfo(
                                    id=dialog.id,
                                    title=(
                                        dialog.name
                                        or "Group"
                                    ),
                                    kind="group",
                                    username=(
                                        getattr(
                                            entity,
                                            "username",
                                            None,
                                        )
                                    ),
                                )
                            )

                # ------------------------------------------------
                # CHANNEL
                # ------------------------------------------------

                elif kind == "channel":

                    if isinstance(
                        entity,
                        Channel,
                    ):

                        if not getattr(
                            entity,
                            "megagroup",
                            False,
                        ):

                            result.append(
                                DialogInfo(
                                    id=dialog.id,
                                    title=(
                                        dialog.name
                                        or "Channel"
                                    ),
                                    kind="channel",
                                    username=(
                                        getattr(
                                            entity,
                                            "username",
                                            None,
                                        )
                                    ),
                                )
                            )

            return result

    # ========================================================
    # MESSAGE ENGINE
    # ========================================================

    async def get_messages(
        self,
        session_name: str,
        dialog_id: int,
        page: int,
        page_size: int,
        target_date=None,
        date_mode: str = "before",
        search: Optional[str] = None,
        filter_type: Optional[str] = None,
    ):

        async with self.get_lock(
            session_name
        ):

            client = await self.connect(
                session_name
            )

            entity = await client.get_entity(
                dialog_id
            )

            # ------------------------------------------------
            # SEARCH
            # ------------------------------------------------

            if search:

                messages = []

                async for message in (
                    client.iter_messages(
                        entity,
                        search=search,
                        limit=page_size,
                        add_offset=(
                            page * page_size
                        ),
                    )
                ):

                    if (
                        filter_type
                        and filter_type != "all"
                        and not media_matches_filter(
                            message,
                            filter_type,
                        )
                    ):

                        continue

                    messages.append(
                        message
                    )

                return entity, messages

            # ------------------------------------------------
            # DATE
            # ------------------------------------------------

            if target_date:

                if target_date.tzinfo is None:

                    target_date = (
                        target_date.replace(
                            tzinfo=timezone.utc
                        )
                    )

                # --------------------------------------------
                # BEFORE
                # --------------------------------------------

                if date_mode == "before":

                    messages = []

                    async for message in (
                        client.iter_messages(
                            entity,
                            offset_date=target_date,
                            limit=page_size,
                        )
                    ):

                        if (
                            filter_type
                            and filter_type != "all"
                            and not media_matches_filter(
                                message,
                                filter_type,
                            )
                        ):

                            continue

                        messages.append(
                            message
                        )

                        if len(messages) >= page_size:
                            break

                    return entity, messages

                # --------------------------------------------
                # AFTER
                # --------------------------------------------

                if date_mode == "after":

                    messages = []

                    async for message in (
                        client.iter_messages(
                            entity,
                            offset_date=target_date,
                            reverse=True,
                            limit=page_size,
                        )
                    ):

                        if (
                            filter_type
                            and filter_type != "all"
                            and not media_matches_filter(
                                message,
                                filter_type,
                            )
                        ):

                            continue

                        messages.append(
                            message
                        )

                        if len(messages) >= page_size:
                            break

                    return entity, messages

                # --------------------------------------------
                # AROUND
                # --------------------------------------------

                if date_mode == "around":

                    half = max(
                        1,
                        page_size // 2,
                    )

                    older = []
                    newer = []

                    async for message in (
                        client.iter_messages(
                            entity,
                            offset_date=target_date,
                            limit=half,
                        )
                    ):

                        if (
                            filter_type
                            and filter_type != "all"
                            and not media_matches_filter(
                                message,
                                filter_type,
                            )
                        ):

                            continue

                        older.append(
                            message
                        )

                    async for message in (
                        client.iter_messages(
                            entity,
                            offset_date=target_date,
                            reverse=True,
                            limit=half,
                        )
                    ):

                        if (
                            filter_type
                            and filter_type != "all"
                            and not media_matches_filter(
                                message,
                                filter_type,
                            )
                        ):

                            continue

                        newer.append(
                            message
                        )

                    older.reverse()

                    result = (
                        older
                        + newer
                    )

                    return (
                        entity,
                        result[
                            :page_size
                        ],
                    )

            # ------------------------------------------------
            # NORMAL
            # ------------------------------------------------

            messages = []

            async for message in (
                client.iter_messages(
                    entity,
                    limit=page_size,
                    add_offset=(
                        page * page_size
                    ),
                )
            ):

                if (
                    filter_type
                    and filter_type != "all"
                    and not media_matches_filter(
                        message,
                        filter_type,
                    )
                ):

                    continue

                messages.append(
                    message
                )

            return entity, messages


session_manager = (
    TelegramSessionManager()
)


# ============================================================
# 12. BOT
# ============================================================

bot = Bot(
    token=BOT_TOKEN
)

dp = Dispatcher()


# ============================================================
# 13. MAIN KEYBOARD
# ============================================================

def main_keyboard():

    kb = InlineKeyboardBuilder()

    kb.button(
        text="👤 Session Manager",
        callback_data="manager:sessions",
    )

    kb.button(
        text="💬 PV Manager",
        callback_data="manager:pv",
    )

    kb.button(
        text="👥 Group Manager",
        callback_data="manager:group",
    )

    kb.button(
        text="📢 Channel Manager",
        callback_data="manager:channel",
    )

    kb.button(
        text="📦 Data Export",
        callback_data="export:menu",
    )

    kb.button(
        text="⭐ Saved Messages",
        callback_data="savedlive:open",
    )

    kb.button(
        text="🔄 Refresh",
        callback_data="main:refresh",
    )

    kb.adjust(1)

    return kb.as_markup()


# ============================================================
# 14. SESSION KEYBOARD
# ============================================================

def sessions_keyboard():

    sessions = (
        session_manager.list_all_sessions()
    )

    kb = InlineKeyboardBuilder()

    if not sessions:

        kb.button(
            text="❌ No sessions",
            callback_data="noop",
        )

    else:

        for item in sessions:

            icon = {
                "active": "🟢",
                "disabled": "⚪",
                "invalid": "🔴",
            }.get(
                item["status"],
                "❔",
            )

            title = (
                Path(
                    item["name"]
                ).stem[:35]
            )

            kb.button(
                text=f"{icon} {title}",
                callback_data=(
                    "session:select:"
                    f"{item['name']}"
                ),
            )

    kb.button(
        text="🔄 Refresh",
        callback_data="manager:sessions",
    )

    kb.button(
        text="🏠 Main",
        callback_data="main:refresh",
    )

    kb.adjust(1)

    return kb.as_markup()


# ============================================================
# 15. SELECTED SESSION
# ============================================================

def selected_session_keyboard():

    kb = InlineKeyboardBuilder()

    kb.button(
        text="💬 PV Manager",
        callback_data="manager:pv",
    )

    kb.button(
        text="👥 Group Manager",
        callback_data="manager:group",
    )

    kb.button(
        text="📢 Channel Manager",
        callback_data="manager:channel",
    )

    kb.button(
        text="🗂 All Chats",
        callback_data="allchats:0",
    )

    kb.button(
        text="⭐ Saved Messages",
        callback_data="savedlive:open",
    )

    kb.button(
        text="📂 Sessions",
        callback_data="manager:sessions",
    )

    kb.button(
        text="🏠 Main",
        callback_data="main:refresh",
    )

    kb.adjust(2, 1, 1)

    return kb.as_markup()


# ============================================================
# 16. MANAGER
# ============================================================

def manager_keyboard(
    kind: str,
):

    kb = InlineKeyboardBuilder()

    kb.button(
        text="📋 Load List",
        callback_data=(
            f"list:{kind}:0"
        ),
    )

    kb.button(
        text="👤 Sessions",
        callback_data=(
            "manager:sessions"
        ),
    )

    kb.button(
        text="🏠 Main",
        callback_data=(
            "main:refresh"
        ),
    )

    kb.adjust(1)

    return kb.as_markup()


# ============================================================
# 17. DIALOG KEYBOARD
# ============================================================

def dialog_keyboard(
    kind,
    dialogs,
    page,
    selected,
):

    start = (
        page
        * DIALOG_PAGE_SIZE
    )

    end = (
        start
        + DIALOG_PAGE_SIZE
    )

    visible = dialogs[
        start:end
    ]

    kb = InlineKeyboardBuilder()

    for dialog in visible:

        mark = (
            "✅"
            if dialog.id in selected
            else "☐"
        )

        kb.button(
            text=(
                f"{mark} "
                f"{dialog.title[:32]}"
            ),
            callback_data=(
                f"toggle:"
                f"{kind}:"
                f"{dialog.id}"
            ),
        )

    if page > 0:

        kb.button(
            text="⬅️ Previous",
            callback_data=(
                f"list:{kind}:{page - 1}"
            ),
        )

    if end < len(dialogs):

        kb.button(
            text="Next ➡️",
            callback_data=(
                f"list:{kind}:{page + 1}"
            ),
        )

    kb.button(
        text="✅ Select All",
        callback_data=(
            f"selectall:{kind}"
        ),
    )

    kb.button(
        text="🗑 Clear",
        callback_data=(
            f"clear:{kind}"
        ),
    )

    kb.button(
        text="📨 LOAD MESSAGES",
        callback_data=(
            f"view:selected:{kind}"
        ),
    )

    kb.button(
        text="📦 Export Selected",
        callback_data=(
            f"export:selected:{kind}"
        ),
    )

    kb.button(
        text="⬅️ Manager",
        callback_data=(
            f"manager:{kind}"
        ),
    )

    kb.adjust(1)

    return kb.as_markup()


# ============================================================
# 18. VIEWER KEYBOARD
# ============================================================

def viewer_keyboard(
    kind,
    dialog_id,
    page,
    page_size,
    filter_name,
):

    kb = InlineKeyboardBuilder()

    if page > 0:

        kb.button(
            text="⬅️ Newer",
            callback_data=(
                f"view:"
                f"{kind}:"
                f"{dialog_id}:"
                f"{page - 1}"
            ),
        )

    kb.button(
        text="🔄 Refresh",
        callback_data=(
            f"view:"
            f"{kind}:"
            f"{dialog_id}:"
            f"{page}"
        ),
    )

    kb.button(
        text="Older ➡️",
        callback_data=(
            f"view:"
            f"{kind}:"
            f"{dialog_id}:"
            f"{page + 1}"
        ),
    )

    kb.button(
        text=(
            f"🔢 Messages: "
            f"{page_size}"
        ),
        callback_data=(
            f"viewsettings:"
            f"{kind}:"
            f"{dialog_id}"
        ),
    )

    kb.button(
        text="📄 Jump to Page",
        callback_data=(
            f"viewpagejump:"
            f"{kind}:"
            f"{dialog_id}"
        ),
    )

    kb.button(
        text="📅 Jump to Date",
        callback_data=(
            f"viewdate:"
            f"{kind}:"
            f"{dialog_id}"
        ),
    )

    filter_labels = {
        "all": "All",
        "media": "All Media",
        "photo": "Photos",
        "video": "Videos",
        "sticker": "Stickers",
        "voice": "Voice",
        "audio": "Audio",
        "document": "Documents",
    }

    kb.button(
        text=(
            "🎛 "
            + filter_labels.get(
                filter_name,
                "All",
            )
        ),
        callback_data=(
            f"viewfilters:"
            f"{kind}:"
            f"{dialog_id}"
        ),
    )

    kb.button(
        text="🔎 Search",
        callback_data=(
            f"viewsearch:"
            f"{kind}:"
            f"{dialog_id}"
        ),
    )

    kb.button(
        text="📦 Export Chat",
        callback_data=(
            f"exportone:"
            f"{kind}:"
            f"{dialog_id}"
        ),
    )

    kb.button(
        text="⬅️ Back",
        callback_data=(
            f"manager:{kind}"
        ),
    )

    kb.adjust(
        2,
        1,
        1,
        1,
        1,
        1,
    )

    return kb.as_markup()


# ============================================================
# 19. MESSAGE PREVIEW
# ============================================================

def message_preview_text(
    message,
):

    sender_id = (
        message.sender_id
        if message.sender_id
        else "Unknown"
    )

    date_text = (
        message.date.strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        if message.date
        else "—"
    )

    lines = [
        f"<b>#{message.id}</b>",
        f"👤 <code>{sender_id}</code>",
        f"🕐 {date_text}",
    ]

    media_type = get_media_type(
        message
    )

    labels = {
        "photo": "📷 Photo",
        "video": "🎥 Video",
        "voice": "🎤 Voice",
        "audio": "🎵 Audio",
        "gif": "🎞 GIF",
        "sticker": "🧩 Sticker",
        "document": "📄 Document",
        "webpage": "🌐 Web Page",
        "geo": "📍 Location",
        "contact": "👤 Contact",
        "poll": "📊 Poll",
        "venue": "📍 Venue",
        "dice": "🎲 Dice",
    }

    if media_type:

        lines.append(
            labels.get(
                media_type,
                "📦 Media",
            )
        )

    if message.media:

        lines.append(
            "🔎 "
            f"<code>"
            f"{html.escape(type(message.media).__name__)}"
            f"</code>"
        )

    if message.text:

        lines.append(
            html.escape(
                message.text[:1000]
            )
        )

    if (
        not message.media
        and not message.text
    ):

        action = getattr(
            message,
            "action",
            None,
        )

        if action:

            lines.append(
                "⚙️ "
                f"<code>"
                f"{html.escape(type(action).__name__)}"
                f"</code>"
            )

        else:

            lines.append(
                "ℹ️ No text or media."
            )

    return "\n".join(
        lines
    )


# ============================================================
# 20. VIEWER CLEANUP
# ============================================================

async def clear_viewer_messages(
    user_id: int,
):

    ids = viewer_message_ids.get(
        user_id,
        [],
    )

    for message_id in ids:

        try:

            await bot.delete_message(
                chat_id=user_id,
                message_id=message_id,
            )

        except Exception:

            pass

    viewer_message_ids[
        user_id
    ] = []


async def cleanup_local_viewer(
    user_id: int,
):

    directory = (
        LOCAL_VIEWER_DIR
        / str(user_id)
    )

    if not directory.exists():
        return

    try:

        shutil.rmtree(
            directory
        )

    except Exception:

        logger.exception(
            "Viewer cleanup failed"
        )


# ============================================================
# 21. SAFE MEDIA DOWNLOAD
# ============================================================

async def download_media_robust(
    client: TelegramClient,
    dialog_id: int,
    message,
    output_dir: Path,
    session_name: str,
):
    """
    Multiple download strategies.

    Strategy 1:
        message.download_media()

    Strategy 2:
        client.download_media(message.media)

    Strategy 3:
        re-fetch the message and download it

    Strategy 4 for Photo:
        re-fetch photo object and download photo
    """

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    message_id = int(
        message.id
    )

    media_type = get_media_type(
        message
    )

    logger.info(
        "MEDIA DOWNLOAD START | "
        "session=%s | "
        "dialog=%s | "
        "message=%s | "
        "type=%s | "
        "class=%s",
        session_name,
        dialog_id,
        message_id,
        media_type,
        type(message.media).__name__
        if message.media
        else None,
    )

    # --------------------------------------------------------
    # Determine extension
    # --------------------------------------------------------

    extension = ""

    try:

        if media_type == "photo":

            extension = ".jpg"

        elif (
            media_type == "video"
        ):

            extension = (
                getattr(
                    message.file,
                    "ext",
                    None,
                )
                or ".mp4"
            )

        elif (
            media_type == "voice"
        ):

            extension = (
                getattr(
                    message.file,
                    "ext",
                    None,
                )
                or ".ogg"
            )

        elif (
            media_type == "audio"
        ):

            extension = (
                getattr(
                    message.file,
                    "ext",
                    None,
                )
                or ".mp3"
            )

        elif (
            media_type == "gif"
        ):

            extension = (
                getattr(
                    message.file,
                    "ext",
                    None,
                )
                or ".gif"
            )

        else:

            extension = (
                getattr(
                    message.file,
                    "ext",
                    None,
                )
                or ""
            )

    except Exception:

        extension = ""

    if not extension:

        extension = ""

    target_file = (
        output_dir
        / f"{message_id}{extension}"
    )

    # --------------------------------------------------------
    # Remove stale output
    # --------------------------------------------------------

    try:

        if target_file.exists():
            target_file.unlink()

    except Exception:

        pass

    # ========================================================
    # ATTEMPT 1
    # ========================================================

    try:

        result = (
            await message.download_media(
                file=str(target_file)
            )
        )

        if result:

            result_path = Path(
                result
            )

            if result_path.exists():
                logger.info(
                    "MEDIA SUCCESS A1 | "
                    "message=%s | file=%s",
                    message_id,
                    result_path,
                )
                return result_path

        if target_file.exists():

            logger.info(
                "MEDIA SUCCESS A1 | "
                "message=%s | file=%s",
                message_id,
                target_file,
            )

            return target_file

    except FloodWaitError:

        raise

    except Exception as exc:

        logger.warning(
            "MEDIA A1 failed | "
            "message=%s | %s",
            message_id,
            exc,
        )

    # ========================================================
    # ATTEMPT 2
    # ========================================================

    try:

        media_object = (
            getattr(
                message,
                "photo",
                None,
            )
            or getattr(
                message,
                "document",
                None,
            )
            or getattr(
                message,
                "media",
                None,
            )
        )

        if media_object:

            result = (
                await client.download_media(
                    media_object,
                    file=str(target_file),
                )
            )

            if result:

                result_path = Path(
                    result
                )

                if result_path.exists():

                    logger.info(
                        "MEDIA SUCCESS A2 | "
                        "message=%s | file=%s",
                        message_id,
                        result_path,
                    )

                    return result_path

            if target_file.exists():

                logger.info(
                    "MEDIA SUCCESS A2 | "
                    "message=%s | file=%s",
                    message_id,
                    target_file,
                )

                return target_file

    except FloodWaitError:

        raise

    except Exception as exc:

        logger.warning(
            "MEDIA A2 failed | "
            "message=%s | %s",
            message_id,
            exc,
        )

    # ========================================================
    # ATTEMPT 3 — REFETCH MESSAGE
    # ========================================================

    try:

        fresh = (
            await client.get_messages(
                dialog_id,
                ids=message_id,
            )
        )

        if fresh:

            result = (
                await fresh.download_media(
                    file=str(target_file)
                )
            )

            if result:

                result_path = Path(
                    result
                )

                if result_path.exists():

                    logger.info(
                        "MEDIA SUCCESS A3 | "
                        "message=%s | file=%s",
                        message_id,
                        result_path,
                    )

                    return result_path

            if target_file.exists():

                logger.info(
                    "MEDIA SUCCESS A3 | "
                    "message=%s | file=%s",
                    message_id,
                    target_file,
                )

                return target_file

    except FloodWaitError:

        raise

    except Exception as exc:

        logger.warning(
            "MEDIA A3 failed | "
            "message=%s | %s",
            message_id,
            exc,
        )

    # ========================================================
    # ATTEMPT 4 — PHOTO OBJECT
    # ========================================================

    if media_type == "photo":

        try:

            fresh = (
                await client.get_messages(
                    dialog_id,
                    ids=message_id,
                )
            )

            if fresh and fresh.photo:

                photo_file = (
                    output_dir
                    / f"{message_id}.jpg"
                )

                result = (
                    await client.download_media(
                        fresh.photo,
                        file=str(photo_file),
                    )
                )

                if result:

                    result_path = Path(
                        result
                    )

                    if result_path.exists():

                        logger.info(
                            "MEDIA SUCCESS A4 PHOTO | "
                            "message=%s | file=%s",
                            message_id,
                            result_path,
                        )

                        return result_path

                if photo_file.exists():

                    logger.info(
                        "MEDIA SUCCESS A4 PHOTO | "
                        "message=%s | file=%s",
                        message_id,
                        photo_file,
                    )

                    return photo_file

        except FloodWaitError:

            raise

        except Exception as exc:

            logger.warning(
                "MEDIA A4 PHOTO failed | "
                "message=%s | %s",
                message_id,
                exc,
            )

    # ========================================================
    # FAILURE
    # ========================================================

    logger.error(
        "MEDIA DOWNLOAD FAILED | "
        "session=%s | dialog=%s | message=%s | type=%s | class=%s",
        session_name,
        dialog_id,
        message_id,
        media_type,
        type(message.media).__name__
        if message.media
        else None,
    )

    return None


# ============================================================
# 22. SEND VIEWER MESSAGE
# ============================================================

async def send_viewer_message(
    user_id: int,
    dialog_id: int,
    message,
    temp_root: Path,
):

    caption = message_preview_text(
        message
    )

    media_type = get_media_type(
        message
    )

    session_name = (
        selected_session.get(
            user_id
        )
    )

    if not session_name:

        raise RuntimeError(
            "No selected session."
        )

    # --------------------------------------------------------
    # Text only
    # --------------------------------------------------------

    if not message.media:

        return await bot.send_message(
            user_id,
            caption,
            parse_mode="HTML",
        )

    # --------------------------------------------------------
    # Non-file media
    # --------------------------------------------------------

    if media_type in (
        "webpage",
        "geo",
        "contact",
        "poll",
        "venue",
        "dice",
    ):

        return await bot.send_message(
            user_id,
            caption,
            parse_mode="HTML",
        )

    client = (
        session_manager.clients.get(
            session_name
        )
    )

    if not client:

        client = await session_manager.connect(
            session_name
        )

    output_dir = (
        temp_root
        / str(dialog_id)
        / str(message.id)
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # Download with retries
    # --------------------------------------------------------

    downloaded = None

    for attempt in range(
        1,
        MEDIA_DOWNLOAD_RETRIES + 1,
    ):

        logger.info(
            "Media attempt %d/%d | message=%s",
            attempt,
            MEDIA_DOWNLOAD_RETRIES,
            message.id,
        )

        downloaded = (
            await download_media_robust(
                client=client,
                dialog_id=dialog_id,
                message=message,
                output_dir=output_dir,
                session_name=session_name,
            )
        )

        if downloaded:
            break

        if attempt < MEDIA_DOWNLOAD_RETRIES:

            await asyncio.sleep(
                1.0 * attempt
            )

    # --------------------------------------------------------
    # Download failed
    # --------------------------------------------------------

    if not downloaded:

        return await bot.send_message(
            user_id,
            caption
            + "\n\n"
            + "⚠️ Media از Session قابل دانلود نبود.",
            parse_mode="HTML",
        )

    file_path = Path(
        downloaded
    )

    if not file_path.exists():

        return await bot.send_message(
            user_id,
            caption
            + "\n\n"
            + "⚠️ فایل Media پیدا نشد.",
            parse_mode="HTML",
        )

    size_mb = (
        file_path.stat().st_size
        / (1024 * 1024)
    )

    if size_mb <= 0:

        return await bot.send_message(
            user_id,
            caption
            + "\n\n"
            + "⚠️ فایل Media خالی است.",
            parse_mode="HTML",
        )

    logger.info(
        "MEDIA READY | "
        "message=%s | "
        "type=%s | "
        "size=%.2fMB | "
        "file=%s",
        message.id,
        media_type,
        size_mb,
        file_path,
    )

    # --------------------------------------------------------
    # Size
    # --------------------------------------------------------

    if (
        size_mb
        > MAX_MEDIA_VIEW_SIZE_MB
    ):

        return await bot.send_message(
            user_id,
            caption
            + "\n\n"
            + (
                f"⚠️ Media بزرگ است: "
                f"<b>{size_mb:.2f} MB</b>"
            ),
            parse_mode="HTML",
        )

    # ========================================================
    # PHOTO
    # ========================================================

    if media_type == "photo":

        # Try actual photo
        if size_mb <= 9.5:

            try:

                return await bot.send_photo(
                    user_id,
                    photo=FSInputFile(
                        file_path
                    ),
                    caption=caption[:1024],
                    parse_mode="HTML",
                )

            except Exception as exc:

                logger.warning(
                    "PHOTO send failed | "
                    "message=%s | %s",
                    message.id,
                    exc,
                )

        # Fallback Document
        try:

            return await bot.send_document(
                user_id,
                document=FSInputFile(
                    file_path
                ),
                caption=caption[:1024],
                parse_mode="HTML",
            )

        except Exception as exc:

            logger.exception(
                "PHOTO document fallback failed"
            )

    # ========================================================
    # VIDEO
    # ========================================================

    if media_type == "video":

        ext = (
            file_path.suffix.lower()
        )

        if ext in (
            ".mp4",
            ".m4v",
        ):

            try:

                return await bot.send_video(
                    user_id,
                    video=FSInputFile(
                        file_path
                    ),
                    caption=caption[:1024],
                    parse_mode="HTML",
                    supports_streaming=True,
                )

            except Exception as exc:

                logger.warning(
                    "VIDEO send_video failed | "
                    "message=%s | %s",
                    message.id,
                    exc,
                )

        # Fallback as Document
        try:

            return await bot.send_document(
                user_id,
                document=FSInputFile(
                    file_path
                ),
                caption=caption[:1024],
                parse_mode="HTML",
            )

        except Exception as exc:

            logger.exception(
                "VIDEO document fallback failed"
            )

    # ========================================================
    # GIF
    # ========================================================

    if media_type == "gif":

        try:

            return await bot.send_animation(
                user_id,
                animation=FSInputFile(
                    file_path
                ),
                caption=caption[:1024],
                parse_mode="HTML",
            )

        except Exception as exc:

            logger.warning(
                "GIF send failed: %s",
                exc,
            )

        try:

            return await bot.send_document(
                user_id,
                document=FSInputFile(
                    file_path
                ),
                caption=caption[:1024],
                parse_mode="HTML",
            )

        except Exception:

            logger.exception(
                "GIF fallback failed"
            )

    # ========================================================
    # VOICE
    # ========================================================

    if media_type == "voice":

        try:

            return await bot.send_voice(
                user_id,
                voice=FSInputFile(
                    file_path
                ),
                caption=caption[:1024],
                parse_mode="HTML",
            )

        except Exception as exc:

            logger.warning(
                "VOICE send failed: %s",
                exc,
            )

        try:

            return await bot.send_document(
                user_id,
                document=FSInputFile(
                    file_path
                ),
                caption=caption[:1024],
                parse_mode="HTML",
            )

        except Exception:

            logger.exception(
                "VOICE fallback failed"
            )

    # ========================================================
    # AUDIO
    # ========================================================

    if media_type == "audio":

        try:

            return await bot.send_audio(
                user_id,
                audio=FSInputFile(
                    file_path
                ),
                caption=caption[:1024],
                parse_mode="HTML",
            )

        except Exception as exc:

            logger.warning(
                "AUDIO send failed: %s",
                exc,
            )

        try:

            return await bot.send_document(
                user_id,
                document=FSInputFile(
                    file_path
                ),
                caption=caption[:1024],
                parse_mode="HTML",
            )

        except Exception:

            logger.exception(
                "AUDIO fallback failed"
            )

    # ========================================================
    # STICKER
    # ========================================================

    if media_type == "sticker":

        try:

            sticker_msg = (
                await bot.send_sticker(
                    user_id,
                    sticker=FSInputFile(
                        file_path
                    ),
                )
            )

            info_msg = (
                await bot.send_message(
                    user_id,
                    caption,
                    parse_mode="HTML",
                )
            )

            viewer_message_ids[
                user_id
            ].append(
                info_msg.message_id
            )

            return sticker_msg

        except Exception as exc:

            logger.warning(
                "STICKER send failed: %s",
                exc,
            )

        try:

            return await bot.send_document(
                user_id,
                document=FSInputFile(
                    file_path
                ),
                caption=caption[:1024],
                parse_mode="HTML",
            )

        except Exception:

            logger.exception(
                "STICKER fallback failed"
            )

    # ========================================================
    # GENERIC DOCUMENT
    # ========================================================

    try:

        filename = (
            getattr(
                getattr(
                    message,
                    "file",
                    None,
                ),
                "name",
                None,
            )
            or file_path.name
        )

        return await bot.send_document(
            user_id,
            document=FSInputFile(
                file_path
            ),
            caption=(
                caption
                + "\n📎 "
                + html.escape(
                    str(filename)
                )
            )[:1024],
            parse_mode="HTML",
        )

    except Exception as exc:

        logger.exception(
            "GENERIC MEDIA send failed: %s",
            exc,
        )

    return await bot.send_message(
        user_id,
        caption
        + "\n\n"
        + "⚠️ Media دریافت شد ولی ارسال نشد.",
        parse_mode="HTML",
    )


# ============================================================
# 23. SHOW MESSAGE PAGE
# ============================================================

async def show_message_page(
    callback: CallbackQuery,
    kind: str,
    dialog_id: int,
    page: int,
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    user_id = (
        callback.from_user.id
    )

    session_name = (
        selected_session.get(
            user_id
        )
    )

    if not session_name:

        await safe_answer(
            callback,
            "Session انتخاب نشده.",
            show_alert=True,
        )

        return

    state = get_viewer_state(
        user_id
    )

    state["kind"] = kind
    state["dialog_id"] = dialog_id
    state["page"] = max(
        0,
        page,
    )

    page = state["page"]

    # Persist current page/settings for this specific chat.
    save_viewer_state_to_profile(
        user_id,
        kind,
        dialog_id,
    )

    page_size = state.get(
        "page_size",
        DEFAULT_VIEWER_PAGE_SIZE,
    )

    target_date = state.get(
        "date"
    )

    date_mode = state.get(
        "date_mode",
        "before",
    )

    filter_name = state.get(
        "filter",
        "all",
    )

    search_text = state.get(
        "search"
    )

    try:

        await safe_answer(
            callback,
            "⏳ Loading messages..."
        )

        await clear_viewer_messages(
            user_id
        )

        await cleanup_local_viewer(
            user_id
        )

        entity, messages = (
            await session_manager.get_messages(
                session_name=session_name,
                dialog_id=dialog_id,
                page=page,
                page_size=page_size,
                target_date=target_date,
                date_mode=date_mode,
                search=search_text,
                filter_type=(
                    filter_name
                    if filter_name != "all"
                    else None
                ),
            )
        )

        title = (
            getattr(
                entity,
                "title",
                None,
            )
            or getattr(
                entity,
                "first_name",
                None,
            )
            or getattr(
                entity,
                "username",
                None,
            )
            or str(dialog_id)
        )

        filter_labels = {
            "all": "All",
            "media": "All Media",
            "photo": "Photos",
            "video": "Videos",
            "sticker": "Stickers",
            "voice": "Voice",
            "audio": "Audio",
            "document": "Documents",
        }

        date_info = ""

        if target_date:

            date_info = (
                "\n📅 Date: "
                f"<code>"
                f"{target_date.strftime('%Y-%m-%d')}"
                f"</code>"
                f" ({html.escape(date_mode)})"
            )

        search_info = ""

        if search_text:

            search_info = (
                "\n🔎 Search: "
                f"<code>"
                f"{html.escape(search_text)}"
                f"</code>"
            )

        header = (
            "📨 <b>MESSAGE EXPLORER</b>\n\n"
            f"📱 Session:\n"
            f"<code>{html.escape(session_name)}</code>\n\n"
            f"💬 Chat:\n"
            f"<b>{html.escape(str(title))}</b>\n"
            f"🆔 <code>{dialog_id}</code>\n\n"
            f"📄 Page: <b>{page + 1}</b>\n"
            f"📨 Loaded: <b>{len(messages)}</b>\n"
            f"🔢 Page Size: <b>{page_size}</b>\n"
            f"🎛 Filter: <b>"
            f"{filter_labels.get(filter_name, 'All')}"
            f"</b>"
            f"{date_info}"
            f"{search_info}"
        )

        await safe_edit_text(
            callback.message,
            header,
            reply_markup=viewer_keyboard(
                kind,
                dialog_id,
                page,
                page_size,
                filter_name,
            ),
            parse_mode="HTML",
        )

        viewer_message_ids[
            user_id
        ] = []

        if not messages:

            empty = (
                await bot.send_message(
                    user_id,
                    "📭 هیچ پیامی با این تنظیمات پیدا نشد.",
                )
            )

            viewer_message_ids[
                user_id
            ].append(
                empty.message_id
            )

            return

        temp_root = (
            LOCAL_VIEWER_DIR
            / str(user_id)
        )

        temp_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        # --------------------------------------------
        # Order
        # --------------------------------------------

        if (
            target_date
            and date_mode in (
                "after",
                "around",
            )
            and not search_text
        ):

            iterable = messages

        else:

            iterable = reversed(
                messages
            )

        # --------------------------------------------
        # Send
        # --------------------------------------------

        for message in iterable:

            try:

                sent = (
                    await send_viewer_message(
                        user_id,
                        dialog_id,
                        message,
                        temp_root,
                    )
                )

                if sent:

                    if isinstance(
                        sent,
                        list,
                    ):

                        for item in sent:

                            viewer_message_ids[
                                user_id
                            ].append(
                                item.message_id
                            )

                    else:

                        viewer_message_ids[
                            user_id
                        ].append(
                            sent.message_id
                        )

            except FloodWaitError:

                raise

            except Exception as exc:

                logger.exception(
                    "Viewer message failed | "
                    "message=%s",
                    message.id,
                )

                fallback = (
                    await bot.send_message(
                        user_id,
                        "⚠️ Message "
                        f"<code>{message.id}</code>\n"
                        f"❌ <code>"
                        f"{type(exc).__name__}"
                        f"</code>",
                        parse_mode="HTML",
                    )
                )

                viewer_message_ids[
                    user_id
                ].append(
                    fallback.message_id
                )

        # --------------------------------------------
        # cleanup
        # --------------------------------------------

        if not KEEP_VIEWER_FILES:

            await cleanup_local_viewer(
                user_id
            )

    except FloodWaitError as exc:

        await safe_edit_text(
            callback.message,
            "⏳ Telegram موقتاً درخواست‌ها را محدود کرده.\n\n"
            f"حدود {exc.seconds} ثانیه دیگر دوباره امتحان کن.",
            reply_markup=main_keyboard(),
        )

    except Exception as exc:

        logger.exception(
            "Message Explorer failed"
        )

        await safe_edit_text(
            callback.message,
            "❌ <b>Message Explorer Error</b>\n\n"
            f"<code>{type(exc).__name__}</code>\n"
            f"<code>{html.escape(str(exc)[:500])}</code>",
            reply_markup=main_keyboard(),
            parse_mode="HTML",
        )


# ============================================================
# 24. START
# IMPORTANT:
# THIS MUST BE BEFORE generic F.text HANDLER
# ============================================================

@dp.message(
    CommandStart()
)
async def start_handler(
    message: Message,
):

    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):

        await message.answer(
            "⛔ Access denied."
        )

        return

    # Clear old pending input
    pending_input.pop(
        message.from_user.id,
        None
    )

    await message.answer(
        "🤖 <b>Telegram Export Manager</b>\n\n"
        "Manager موردنظر را انتخاب کن.",
        reply_markup=main_keyboard(),
        parse_mode="HTML",
    )


# ============================================================
# 25. MAIN
# ============================================================

@dp.callback_query(
    F.data == "main:refresh"
)
async def main_refresh(
    callback: CallbackQuery,
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    await safe_edit_text(
        callback.message,
        "🤖 <b>Telegram Export Manager</b>\n\n"
        "Manager موردنظر را انتخاب کن.",
        reply_markup=main_keyboard(),
        parse_mode="HTML",
    )

    await safe_answer(
        callback
    )


# ============================================================
# 26. SESSION MANAGER
# ============================================================

@dp.callback_query(
    F.data == "manager:sessions"
)
async def session_manager_handler(
    callback: CallbackQuery,
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    sessions = (
        session_manager.list_all_sessions()
    )

    active = sum(
        x["status"] == "active"
        for x in sessions
    )

    disabled = sum(
        x["status"] == "disabled"
        for x in sessions
    )

    invalid = sum(
        x["status"] == "invalid"
        for x in sessions
    )

    await safe_edit_text(
        callback.message,
        "👤 <b>Session Manager</b>\n\n"
        f"🟢 Active: <b>{active}</b>\n"
        f"⚪ Disabled: <b>{disabled}</b>\n"
        f"🔴 Invalid: <b>{invalid}</b>\n\n"
        "Session موردنظر را انتخاب کن.",
        reply_markup=sessions_keyboard(),
        parse_mode="HTML",
    )

    await safe_answer(
        callback
    )


# ============================================================
# 27. SELECT SESSION
# ============================================================

@dp.callback_query(
    F.data.startswith(
        "session:select:"
    )
)
async def select_session_handler(
    callback: CallbackQuery,
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    session_name = (
        callback.data.split(
            ":",
            2,
        )[2]
    )

    try:

        me = await session_manager.get_me(
            session_name
        )

        user_id = (
            callback.from_user.id
        )

        selected_session[
            user_id
        ] = session_name

        selected_dialogs[
            user_id
        ] = {
            "pv": set(),
            "group": set(),
            "channel": set(),
        }

        current_pages[
            user_id
        ] = {
            "pv": 0,
            "group": 0,
            "channel": 0,
        }

        viewer_state[
            user_id
        ] = {
            "kind": None,
            "dialog_id": None,
            "page": 0,
            "page_size": DEFAULT_VIEWER_PAGE_SIZE,
            "date": None,
            "date_mode": None,
            "filter": "all",
            "search": None,
        }

        full_name = " ".join(
            x
            for x in (
                me.first_name,
                me.last_name,
            )
            if x
        )

        username = (
            f"@{me.username}"
            if me.username
            else "—"
        )

        await safe_edit_text(
            callback.message,
            (
                "✅ <b>Session Selected</b>\n\n"
                f"📱 <code>"
                f"{html.escape(session_name)}"
                f"</code>\n\n"
                f"🆔 <code>{me.id}</code>\n"
                f"👤 "
                f"{html.escape(full_name or '—')}\n"
                f"🔹 "
                f"{html.escape(username)}"
            ),
            reply_markup=(
                selected_session_keyboard()
            ),
            parse_mode="HTML",
        )

        await safe_answer(
            callback,
            "✅ Session connected"
        )

    except AuthKeyDuplicatedError:

        await safe_answer(
            callback,
            "❌ این Session هم‌زمان از IP دیگری در حال استفاده است و Telegram آن را رد کرده است. این Session را فقط در یک محیط اجرا کن.",
            show_alert=True,
        )

    except AuthKeyUnregisteredError:

        await safe_answer(
            callback,
            "🔴 Session معتبر نیست.",
            show_alert=True,
        )

    except Exception as exc:

        logger.exception(
            "Session selection failed"
        )

        await safe_answer(
            callback,
            f"❌ {type(exc).__name__}",
            show_alert=True,
        )


# ============================================================
# 28. MANAGER ENTRY
# ============================================================

@dp.callback_query(
    F.data.regexp(
        r"^manager:(pv|group|channel)$"
    )
)
async def manager_entry(
    callback: CallbackQuery,
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    kind = (
        callback.data.split(":")[1]
    )

    session_name = selected_session.get(
        callback.from_user.id
    )

    if not session_name:

        await safe_answer(
            callback,
            "اول Session را انتخاب کن.",
            show_alert=True,
        )

        return

    names = {
        "pv": "💬 PV Manager",
        "group": "👥 Group Manager",
        "channel": "📢 Channel Manager",
    }

    await safe_edit_text(
        callback.message,
        (
            f"{names[kind]}\n\n"
            f"📱 <code>"
            f"{html.escape(session_name)}"
            f"</code>\n\n"
            "برای دریافت لیست "
            "<b>Load List</b> را بزن."
        ),
        reply_markup=manager_keyboard(
            kind
        ),
        parse_mode="HTML",
    )

    await safe_answer(
        callback
    )


# ============================================================
# 29. LOAD DIALOGS
# ============================================================

@dp.callback_query(
    F.data.regexp(
        r"^list:(pv|group|channel):\d+$"
    )
)
async def load_dialogs(
    callback: CallbackQuery,
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    _, kind, page_raw = (
        callback.data.split(":")
    )

    page = int(
        page_raw
    )

    session_name = selected_session.get(
        callback.from_user.id
    )

    if not session_name:

        await safe_answer(
            callback,
            "Session انتخاب نشده.",
            show_alert=True,
        )

        return

    try:

        await safe_answer(
            callback,
            "⏳ Loading..."
        )

        dialogs = (
            await session_manager.get_dialogs(
                session_name,
                kind,
            )
        )

        total_pages = max(
            1,
            (
                len(dialogs)
                + DIALOG_PAGE_SIZE
                - 1
            )
            // DIALOG_PAGE_SIZE,
        )

        page = max(
            0,
            min(
                page,
                total_pages - 1,
            ),
        )

        get_pages(
            callback.from_user.id
        )[kind] = page

        selections = get_selections(
            callback.from_user.id
        )

        labels = {
            "pv": "💬 Private Chats",
            "group": "👥 Groups",
            "channel": "📢 Channels",
        }

        await safe_edit_text(
            callback.message,
            (
                f"{labels[kind]}\n\n"
                f"📱 <code>"
                f"{html.escape(session_name)}"
                f"</code>\n"
                f"📊 Total: <b>{len(dialogs)}</b>\n"
                f"📄 Page: "
                f"<b>{page + 1}/{total_pages}</b>\n"
                f"✅ Selected: "
                f"<b>{len(selections[kind])}</b>\n\n"
                "موارد موردنظر را انتخاب کن."
            ),
            reply_markup=dialog_keyboard(
                kind,
                dialogs,
                page,
                selections[kind],
            ),
            parse_mode="HTML",
        )

    except FloodWaitError as exc:

        await safe_edit_text(
            callback.message,
            "⏳ Telegram درخواست‌ها را محدود کرده است.\n"
            f"حدود {exc.seconds} ثانیه دیگر دوباره امتحان کن.",
            reply_markup=main_keyboard(),
        )

    except Exception as exc:

        logger.exception(
            "Dialog loading failed"
        )

        await safe_edit_text(
            callback.message,
            "❌ <b>Dialog loading error</b>\n\n"
            f"<code>{type(exc).__name__}</code>",
            reply_markup=main_keyboard(),
            parse_mode="HTML",
        )


# ============================================================
# 30. TOGGLE
# ============================================================

@dp.callback_query(
    F.data.regexp(
        r"^toggle:(pv|group|channel):-?\d+$"
    )
)
async def toggle_dialog(
    callback: CallbackQuery,
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    _, kind, dialog_id_raw = (
        callback.data.split(":")
    )

    dialog_id = int(
        dialog_id_raw
    )

    session_name = selected_session.get(
        callback.from_user.id
    )

    if not session_name:
        return

    selections = get_selections(
        callback.from_user.id
    )

    if dialog_id in selections[kind]:

        selections[kind].remove(
            dialog_id
        )

    else:

        selections[kind].add(
            dialog_id
        )

    page = get_pages(
        callback.from_user.id
    )[kind]

    try:

        dialogs = (
            await session_manager.get_dialogs(
                session_name,
                kind,
            )
        )

        await safe_edit_markup(
            callback.message,
            dialog_keyboard(
                kind,
                dialogs,
                page,
                selections[kind],
            ),
        )

        await safe_answer(
            callback
        )

    except Exception as exc:

        logger.exception(
            "Toggle failed"
        )

        await safe_answer(
            callback,
            f"❌ {type(exc).__name__}",
            show_alert=True,
        )


# ============================================================
# 31. SELECT ALL
# ============================================================

@dp.callback_query(
    F.data.regexp(
        r"^selectall:(pv|group|channel)$"
    )
)
async def select_all(
    callback: CallbackQuery,
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    kind = (
        callback.data.split(":")[1]
    )

    session_name = selected_session.get(
        callback.from_user.id
    )

    if not session_name:
        return

    try:

        dialogs = (
            await session_manager.get_dialogs(
                session_name,
                kind,
            )
        )

        selections = get_selections(
            callback.from_user.id
        )

        selections[kind] = {
            d.id
            for d in dialogs
        }

        page = get_pages(
            callback.from_user.id
        )[kind]

        await safe_edit_markup(
            callback.message,
            dialog_keyboard(
                kind,
                dialogs,
                page,
                selections[kind],
            ),
        )

        await safe_answer(
            callback,
            f"✅ {len(dialogs)} مورد انتخاب شد."
        )

    except Exception as exc:

        logger.exception(
            "Select all failed"
        )

        await safe_answer(
            callback,
            f"❌ {type(exc).__name__}",
            show_alert=True,
        )


# ============================================================
# 32. CLEAR
# ============================================================

@dp.callback_query(
    F.data.regexp(
        r"^clear:(pv|group|channel)$"
    )
)
async def clear_selection(
    callback: CallbackQuery,
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    kind = (
        callback.data.split(":")[1]
    )

    selections = get_selections(
        callback.from_user.id
    )

    selections[kind].clear()

    session_name = selected_session.get(
        callback.from_user.id
    )

    if not session_name:
        return

    page = get_pages(
        callback.from_user.id
    )[kind]

    try:

        dialogs = (
            await session_manager.get_dialogs(
                session_name,
                kind,
            )
        )

        await safe_edit_markup(
            callback.message,
            dialog_keyboard(
                kind,
                dialogs,
                page,
                selections[kind],
            ),
        )

        await safe_answer(
            callback,
            "🗑 Selection cleared"
        )

    except Exception as exc:

        logger.exception(
            "Clear failed"
        )

        await safe_answer(
            callback,
            f"❌ {type(exc).__name__}",
            show_alert=True,
        )


# ============================================================
# 33. LOAD MESSAGES
# ============================================================

@dp.callback_query(
    F.data.regexp(
        r"^view:selected:(pv|group|channel)$"
    )
)
async def view_selected(
    callback: CallbackQuery,
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    kind = (
        callback.data.split(":")[2]
    )

    selections = get_selections(
        callback.from_user.id
    )

    ids = list(
        selections[kind]
    )

    if len(ids) != 1:

        await safe_answer(
            callback,
            "برای LOAD MESSAGES فقط یک Chat را انتخاب کن.",
            show_alert=True,
        )

        return

    dialog_id = ids[0]

    sync_viewer_state_from_profile(
        callback.from_user.id,
        kind,
        dialog_id,
    )

    await show_message_page(
        callback,
        kind,
        dialog_id,
        0,
    )


# ============================================================
# 34. VIEW PAGE
# ============================================================

@dp.callback_query(
    F.data.regexp(
        r"^view:(pv|group|channel):-?\d+:\d+$"
    )
)
async def view_page_handler(
    callback: CallbackQuery,
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    _, kind, dialog_id_raw, page_raw = (
        callback.data.split(":")
    )

    await show_message_page(
        callback,
        kind,
        int(dialog_id_raw),
        int(page_raw),
    )


# ============================================================
# 35. JUMP TO PAGE
# ============================================================

@dp.callback_query(
    F.data.regexp(
        r"^viewpagejump:(pv|group|channel):-?\d+$"
    )
)
async def view_page_jump_prompt(
    callback: CallbackQuery,
):

    if not is_admin(callback.from_user.id):
        return

    _, kind, dialog_id_raw = callback.data.split(":")
    dialog_id = int(dialog_id_raw)

    pending_input[callback.from_user.id] = {
        "type": "page_jump",
        "kind": kind,
        "dialog_id": dialog_id,
    }

    state = get_chat_view_profile(
        callback.from_user.id,
        kind,
        dialog_id,
    )

    await safe_edit_text(
        callback.message,
        (
            "📄 <b>Jump to Page</b>\n\n"
            f"Page فعلی: <b>{state.get('page', 0) + 1}</b>\n\n"
            "شماره صفحه را وارد کن.\n"
            "مثال: <code>15</code>"
        ),
        parse_mode="HTML",
    )

    await safe_answer(callback)


# ============================================================
# 35. PAGE SIZE
# ============================================================

@dp.callback_query(
    F.data.regexp(
        r"^viewsettings:(pv|group|channel):-?\d+$"
    )
)
async def viewer_settings(
    callback: CallbackQuery,
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    _, kind, dialog_id_raw = (
        callback.data.split(":")
    )

    dialog_id = int(
        dialog_id_raw
    )

    state = get_viewer_state(
        callback.from_user.id
    )

    current = state.get(
        "page_size",
        DEFAULT_VIEWER_PAGE_SIZE,
    )

    kb = InlineKeyboardBuilder()

    for size in VIEWER_PAGE_SIZES:

        mark = (
            "✅"
            if size == current
            else "☐"
        )

        kb.button(
            text=(
                f"{mark} {size} messages"
            ),
            callback_data=(
                f"setviewsize:"
                f"{kind}:"
                f"{dialog_id}:"
                f"{size}"
            ),
        )

    kb.button(
        text="⬅️ Back",
        callback_data=(
            f"view:"
            f"{kind}:"
            f"{dialog_id}:0"
        ),
    )

    kb.adjust(1)

    await safe_edit_text(
        callback.message,
        (
            "🔢 <b>Messages per page</b>\n\n"
            f"Current: <b>{current}</b>"
        ),
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )

    await safe_answer(
        callback
    )


# ============================================================
# 36. SET PAGE SIZE
# ============================================================

@dp.callback_query(
    F.data.regexp(
        r"^setviewsize:(pv|group|channel):-?\d+:\d+$"
    )
)
async def set_view_size(
    callback: CallbackQuery,
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    _, kind, dialog_id_raw, size_raw = (
        callback.data.split(":")
    )

    dialog_id = int(
        dialog_id_raw
    )

    size = int(
        size_raw
    )

    if size not in VIEWER_PAGE_SIZES:

        await safe_answer(
            callback,
            "Invalid page size.",
            show_alert=True,
        )

        return

    state = get_viewer_state(
        callback.from_user.id
    )

    state["page_size"] = size
    state["page"] = 0

    profile = get_chat_view_profile(
        callback.from_user.id,
        kind,
        dialog_id,
    )
    profile.update(state)

    await show_message_page(
        callback,
        kind,
        dialog_id,
        0,
    )


# ============================================================
# 37. DATE PROMPT
# ============================================================

@dp.callback_query(
    F.data.regexp(
        r"^viewdate:(pv|group|channel):-?\d+$"
    )
)
async def view_date_prompt(
    callback: CallbackQuery,
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    _, kind, dialog_id_raw = (
        callback.data.split(":")
    )

    pending_input[
        callback.from_user.id
    ] = {
        "type": "date",
        "kind": kind,
        "dialog_id": int(
            dialog_id_raw
        ),
    }

    await safe_edit_text(
        callback.message,
        "📅 <b>Jump to Date</b>\n\n"
        "تاریخ را وارد کن:\n\n"
        "<code>2026-08-24</code>",
        parse_mode="HTML",
    )

    await safe_answer(
        callback
    )


# ============================================================
# 38. FILTER MENU
# ============================================================

@dp.callback_query(
    F.data.regexp(
        r"^viewfilters:(pv|group|channel):-?\d+$"
    )
)
async def view_filters(
    callback: CallbackQuery,
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    _, kind, dialog_id_raw = (
        callback.data.split(":")
    )

    dialog_id = int(
        dialog_id_raw
    )

    filters = {
        "all": "📝 All",
        "media": "📦 All Media",
        "photo": "📷 Photos",
        "video": "🎥 Videos",
        "sticker": "🧩 Stickers",
        "voice": "🎤 Voice",
        "audio": "🎵 Audio",
        "document": "📄 Documents",
    }

    kb = InlineKeyboardBuilder()

    for key, title in filters.items():

        kb.button(
            text=title,
            callback_data=(
                f"setfilter:"
                f"{kind}:"
                f"{dialog_id}:"
                f"{key}"
            ),
        )

    kb.button(
        text="⬅️ Back",
        callback_data=(
            f"view:"
            f"{kind}:"
            f"{dialog_id}:0"
        ),
    )

    kb.adjust(1)

    await safe_edit_text(
        callback.message,
        "🎛 <b>Message Filter</b>\n\n"
        "نوع پیام را انتخاب کن.",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )

    await safe_answer(
        callback
    )


# ============================================================
# 39. SET FILTER
# ============================================================

@dp.callback_query(
    F.data.regexp(
        r"^setfilter:(pv|group|channel):-?\d+:[a-z]+$"
    )
)
async def set_filter(
    callback: CallbackQuery,
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    _, kind, dialog_id_raw, filter_name = (
        callback.data.split(":")
    )

    dialog_id = int(
        dialog_id_raw
    )

    state = get_viewer_state(
        callback.from_user.id
    )

    state["kind"] = kind
    state["dialog_id"] = dialog_id
    state["page"] = 0
    state["filter"] = filter_name

    await show_message_page(
        callback,
        kind,
        dialog_id,
        0,
    )


# ============================================================
# 40. SEARCH PROMPT
# ============================================================

@dp.callback_query(
    F.data.regexp(
        r"^viewsearch:(pv|group|channel):-?\d+$"
    )
)
async def view_search_prompt(
    callback: CallbackQuery,
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    _, kind, dialog_id_raw = (
        callback.data.split(":")
    )

    pending_input[
        callback.from_user.id
    ] = {
        "type": "search",
        "kind": kind,
        "dialog_id": int(
            dialog_id_raw
        ),
    }

    await safe_edit_text(
        callback.message,
        "🔎 <b>Search Messages</b>\n\n"
        "عبارت موردنظر را ارسال کن.",
        parse_mode="HTML",
    )

    await safe_answer(
        callback
    )


# ============================================================
# 41. DATE MODE
# ============================================================

@dp.callback_query(
    F.data.startswith(
        "date_mode:"
    )
)
async def date_mode_handler(
    callback: CallbackQuery,
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    mode = (
        callback.data.split(":")[1]
    )

    pending = pending_input.get(
        callback.from_user.id
    )

    if not pending:

        await safe_answer(
            callback,
            "درخواست تاریخ منقضی شده.",
            show_alert=True,
        )

        return

    if pending.get(
        "type"
    ) != "date_mode":

        await safe_answer(
            callback,
            "Date state invalid.",
            show_alert=True,
        )

        return

    user_id = (
        callback.from_user.id
    )

    state = get_viewer_state(
        user_id
    )

    state["kind"] = pending[
        "kind"
    ]

    state["dialog_id"] = pending[
        "dialog_id"
    ]

    state["page"] = 0

    state["date"] = pending[
        "date_value"
    ]

    state["date_mode"] = mode

    state["search"] = None

    pending_input.pop(
        user_id,
        None
    )

    await show_message_page(
        callback,
        state["kind"],
        state["dialog_id"],
        0,
    )


# ============================================================
# 42. TEXT INPUT
#
# IMPORTANT:
# This is AFTER CommandStart handler.
# ============================================================

@dp.message(
    F.text
)
async def text_input_handler(
    message: Message,
):

    if not message.from_user:
        return

    user_id = (
        message.from_user.id
    )

    if not is_admin(
        user_id
    ):
        return

    pending = pending_input.get(
        user_id
    )

    # Nothing waiting for text
    if not pending:
        return

    text = (
        message.text or ""
    ).strip()

    # ========================================================
    # SAVED MESSAGE PAGE JUMP
    # ========================================================

    if pending["type"] == "saved_page_jump":

        if not text.isdigit() or int(text) < 1:
            await message.answer(
                "❌ شماره صفحه باید عددی حداقل 1 باشد.",
            )
            return

        requested_page = int(text)
        profile = get_saved_view_profile(user_id)
        profile["page"] = requested_page - 1
        pending_input.pop(user_id, None)

        await message.answer(
            f"✅ رفتن به صفحه <b>{requested_page}</b>",
            parse_mode="HTML",
        )

        # Load directly from the live Saved Messages session.
        session_name = selected_session.get(user_id)
        status = await message.answer("⏳ در حال بارگذاری صفحه...")

        try:
            messages = await fetch_live_saved_messages(
                session_name,
                profile["page"],
                profile["page_size"],
            )

            if not messages and profile["page"] > 0:
                profile["page"] -= 1
                messages = await fetch_live_saved_messages(
                    session_name,
                    profile["page"],
                    profile["page_size"],
                )

            await clear_viewer_messages(user_id)
            await cleanup_local_viewer(user_id)

            has_older = len(messages) == profile["page_size"]

            header = await bot.send_message(
                user_id,
                (
                    "⭐ <b>SAVED MESSAGES</b>\n\n"
                    f"📱 Session: <code>{html.escape(session_name)}</code>\n"
                    f"📄 Page: <b>{profile['page'] + 1}</b>\n"
                    f"📨 Loaded: <b>{len(messages)}</b>"
                ),
                reply_markup=await live_saved_keyboard(
                    user_id,
                    profile["page"],
                    has_older,
                ),
                parse_mode="HTML",
            )

            viewer_message_ids[user_id] = [header.message_id]

            temp_root = LOCAL_VIEWER_DIR / str(user_id)
            temp_root.mkdir(parents=True, exist_ok=True)

            for msg in reversed(messages):
                sent = await send_live_saved_message(
                    user_id,
                    msg,
                    temp_root,
                )
                if sent:
                    viewer_message_ids[user_id].append(sent.message_id)

            await status.delete()

        except Exception as exc:
            logger.exception("Saved page jump failed")
            await status.edit_text(
                "❌ Saved Messages page jump failed\n"
                f"<code>{type(exc).__name__}</code>",
                parse_mode="HTML",
            )

        return

    # ========================================================
    # PAGE JUMP
    # ========================================================

    if pending["type"] == "page_jump":

        if not text.isdigit():
            await message.answer(
                "❌ شماره صفحه باید عدد باشد.\nمثال: <code>15</code>",
                parse_mode="HTML",
            )
            return

        requested_page = int(text)

        if requested_page < 1:
            await message.answer(
                "❌ شماره صفحه باید حداقل 1 باشد.",
            )
            return

        kind = pending["kind"]
        dialog_id = pending["dialog_id"]

        profile = get_chat_view_profile(
            user_id,
            kind,
            dialog_id,
        )

        profile["page"] = requested_page - 1
        profile["date"] = None
        profile["date_mode"] = None
        profile["search"] = None

        sync_viewer_state_from_profile(
            user_id,
            kind,
            dialog_id,
        )

        pending_input.pop(user_id, None)

        # Delete prompt message if possible and show requested page
        await message.answer(
            f"✅ رفتن به صفحه <b>{requested_page}</b>",
            parse_mode="HTML",
        )

        # We don't have a CallbackQuery here, so create a lightweight
        # status message and render the page through a dedicated helper.
        status = await message.answer("⏳ در حال بارگذاری صفحه...")

        try:
            state = get_viewer_state(user_id)
            session_name = selected_session.get(user_id)
            if not session_name:
                await status.edit_text("❌ Session انتخاب نشده.")
                return

            await clear_viewer_messages(user_id)
            await cleanup_local_viewer(user_id)

            entity, messages = await session_manager.get_messages(
                session_name=session_name,
                dialog_id=dialog_id,
                page=state["page"],
                page_size=state["page_size"],
                target_date=state.get("date"),
                date_mode=state.get("date_mode", "before"),
                search=state.get("search"),
                filter_type=(
                    state["filter"] if state["filter"] != "all" else None
                ),
            )

            title = (
                getattr(entity, "title", None)
                or getattr(entity, "first_name", None)
                or getattr(entity, "username", None)
                or str(dialog_id)
            )

            header = (
                "📨 <b>MESSAGE EXPLORER</b>\n\n"
                f"📱 Session: <code>{html.escape(session_name)}</code>\n"
                f"💬 Chat: <b>{html.escape(str(title))}</b>\n"
                f"🆔 <code>{dialog_id}</code>\n\n"
                f"📄 Page: <b>{state['page'] + 1}</b>\n"
                f"📨 Loaded: <b>{len(messages)}</b>\n"
                f"🔢 Page Size: <b>{state['page_size']}</b>"
            )

            header_msg = await bot.send_message(
                user_id,
                header,
                reply_markup=viewer_keyboard(
                    kind,
                    dialog_id,
                    state["page"],
                    state["page_size"],
                    state["filter"],
                ),
                parse_mode="HTML",
            )

            viewer_message_ids[user_id] = [header_msg.message_id]

            temp_root = LOCAL_VIEWER_DIR / str(user_id)
            temp_root.mkdir(parents=True, exist_ok=True)

            iterable = reversed(messages)
            for msg in iterable:
                sent = await send_viewer_message(
                    user_id,
                    dialog_id,
                    msg,
                    temp_root,
                )
                if sent:
                    viewer_message_ids[user_id].append(sent.message_id)

            await status.delete()

        except Exception as exc:
            logger.exception("Page jump failed")
            await status.edit_text(
                "❌ Page jump failed\n"
                f"<code>{type(exc).__name__}</code>\n"
                f"<code>{html.escape(str(exc)[:500])}</code>",
                parse_mode="HTML",
            )

        return

    # ========================================================
    # DATE
    # ========================================================

    if pending["type"] == "date":

        try:

            target_date = (
                datetime.strptime(
                    text,
                    "%Y-%m-%d",
                ).replace(
                    tzinfo=timezone.utc
                )
            )

        except ValueError:

            await message.answer(
                "❌ تاریخ اشتباه است.\n\n"
                "فرمت درست:\n"
                "<code>2026-08-24</code>",
                parse_mode="HTML",
            )

            return

        pending[
            "date_value"
        ] = target_date

        pending[
            "type"
        ] = "date_mode"

        kb = InlineKeyboardBuilder()

        kb.button(
            text="⬅️ Before",
            callback_data=(
                "date_mode:before"
            ),
        )

        kb.button(
            text="🎯 Around",
            callback_data=(
                "date_mode:around"
            ),
        )

        kb.button(
            text="➡️ After",
            callback_data=(
                "date_mode:after"
            ),
        )

        kb.adjust(3)

        await message.answer(
            "📅 تاریخ ثبت شد.\n\n"
            "حالت را انتخاب کن:",
            reply_markup=kb.as_markup(),
        )

        return

    # ========================================================
    # SEARCH
    # ========================================================

    if pending["type"] == "search":

        kind = pending[
            "kind"
        ]

        dialog_id = pending[
            "dialog_id"
        ]

        state = get_viewer_state(
            user_id
        )

        state["kind"] = kind
        state["dialog_id"] = dialog_id
        state["page"] = 0
        state["search"] = text

        state["date"] = None
        state["date_mode"] = None

        pending_input.pop(
            user_id,
            None
        )

        status = await message.answer(
            "⏳ در حال جستجو..."
        )

        try:

            session_name = (
                selected_session.get(
                    user_id
                )
            )

            if not session_name:

                await status.edit_text(
                    "❌ Session انتخاب نشده."
                )

                return

            entity, messages = (
                await session_manager.get_messages(
                    session_name=session_name,
                    dialog_id=dialog_id,
                    page=0,
                    page_size=state[
                        "page_size"
                    ],
                    search=text,
                    filter_type=(
                        state["filter"]
                        if state["filter"] != "all"
                        else None
                    ),
                )
            )

            await status.delete()

            await clear_viewer_messages(
                user_id
            )

            title = (
                getattr(
                    entity,
                    "title",
                    None,
                )
                or getattr(
                    entity,
                    "first_name",
                    None,
                )
                or getattr(
                    entity,
                    "username",
                    None,
                )
                or str(dialog_id)
            )

            header = await bot.send_message(
                user_id,
                (
                    "🔎 <b>SEARCH RESULTS</b>\n\n"
                    f"💬 "
                    f"<b>{html.escape(str(title))}</b>\n"
                    f"🔎 <code>"
                    f"{html.escape(text)}"
                    f"</code>\n"
                    f"📨 Loaded: "
                    f"<b>{len(messages)}</b>"
                ),
                reply_markup=viewer_keyboard(
                    kind,
                    dialog_id,
                    0,
                    state["page_size"],
                    state["filter"],
                ),
                parse_mode="HTML",
            )

            viewer_message_ids[
                user_id
            ] = [
                header.message_id
            ]

            temp_root = (
                LOCAL_VIEWER_DIR
                / str(user_id)
            )

            for msg in reversed(
                messages
            ):

                try:

                    sent = (
                        await send_viewer_message(
                            user_id,
                            dialog_id,
                            msg,
                            temp_root,
                        )
                    )

                    if sent:

                        if isinstance(
                            sent,
                            list,
                        ):

                            for item in sent:

                                viewer_message_ids[
                                    user_id
                                ].append(
                                    item.message_id
                                )

                        else:

                            viewer_message_ids[
                                user_id
                            ].append(
                                sent.message_id
                            )

                except Exception:

                    logger.exception(
                        "Search media send failed"
                    )

        except Exception as exc:

            logger.exception(
                "Search failed"
            )

            try:

                await status.edit_text(
                    "❌ Search failed:\n"
                    f"<code>"
                    f"{type(exc).__name__}"
                    f"</code>",
                    parse_mode="HTML",
                )

            except Exception:
                pass

        return


# ============================================================
# 43. FULL EXPORT ENGINE
# ============================================================

EXPORT_MEDIA_DIR_NAMES = {
    "photo": "photos",
    "video": "videos",
    "audio": "audio",
    "voice": "voice",
    "sticker": "stickers",
    "gif": "gif",
    "document": "documents",
}


def sanitize_filename(name: str, fallback: str = "chat") -> str:
    name = str(name or "").strip()
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name)
    name = name.strip(". ")
    return name[:80] or fallback


async def export_dialog_full(
    session_name: str,
    dialog_id: int,
    output_dir: Path,
    message_limit=None,
):
    """Export a chat with message metadata + downloadable media + HTML viewer."""

    async with session_manager.get_lock(session_name):
        client = await session_manager.connect(session_name)
        entity = await client.get_entity(dialog_id)

        output_dir.mkdir(parents=True, exist_ok=True)

        data_dir = output_dir / "data"
        media_root = output_dir / "media"

        media_dirs = {
            key: media_root / folder
            for key, folder in EXPORT_MEDIA_DIR_NAMES.items()
        }

        for folder in [data_dir, media_root, *media_dirs.values()]:
            folder.mkdir(parents=True, exist_ok=True)

        chat_title = (
            getattr(entity, "title", None)
            or getattr(entity, "first_name", None)
            or getattr(entity, "username", None)
            or str(dialog_id)
        )

        messages = []
        async for message in client.iter_messages(
            entity,
            limit=message_limit,
        ):
            messages.append(message)

        messages.reverse()

        html_parts = [
            "<!DOCTYPE html>",
            "<html lang='en'>",
            "<head>",
            "<meta charset='UTF-8'>",
            "<meta name='viewport' content='width=device-width,initial-scale=1.0'>",
            f"<title>{html.escape(str(chat_title))}</title>",
            """
<style>
body{margin:0;background:#e7ebee;font-family:Arial,Helvetica,sans-serif;color:#222}
.header{position:sticky;top:0;z-index:10;background:#fff;border-bottom:1px solid #ddd;padding:16px 20px;font-weight:700}
.header small{display:block;margin-top:4px;font-weight:400;color:#888}
.chat{max-width:920px;margin:auto;padding:16px 12px 50px}
.message{background:#fff;border-radius:12px;padding:12px 14px;margin:8px 0;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.sender{font-weight:700}.sender-id,.date,.meta{font-size:11px;color:#8b8b8b}.date{margin:3px 0 9px}.text{white-space:pre-wrap;word-break:break-word;line-height:1.45}
.media{margin-top:10px}.media img{display:block;max-width:100%;max-height:700px;border-radius:10px}.media video{display:block;width:100%;max-height:700px;border-radius:10px}.media audio{width:100%}
.file{display:inline-block;padding:10px 12px;background:#f2f3f5;color:#222;text-decoration:none;border-radius:8px}.error{margin-top:8px;padding:9px;background:#fff1f1;color:#a33;border-radius:8px;font-size:12px}
</style>
""",
            "</head><body>",
            "<div class='header'>",
            f"💬 {html.escape(str(chat_title))}",
            "<small>Telegram Export</small>",
            "</div><div class='chat'>",
        ]

        records = []
        media_total = 0
        media_ok = 0
        media_failed = 0

        for index, message in enumerate(messages, start=1):
            sender_name = str(message.sender_id or "Unknown")
            try:
                sender = await message.get_sender()
                if sender:
                    sender_name = (
                        getattr(sender, "first_name", None)
                        or getattr(sender, "title", None)
                        or getattr(sender, "username", None)
                        or str(message.sender_id or "Unknown")
                    )
            except Exception:
                pass

            record = {
                "id": message.id,
                "date": message.date.isoformat() if message.date else None,
                "sender_id": message.sender_id,
                "sender_name": sender_name,
                "text": message.text,
                "media": None,
                "status": "completed",
            }

            html_parts.append("<div class='message'>")
            html_parts.append(
                f"<div class='sender'>👤 {html.escape(str(sender_name))}</div>"
            )
            html_parts.append(
                f"<div class='sender-id'>ID: {html.escape(str(message.sender_id or '—'))}</div>"
            )
            if message.date:
                html_parts.append(
                    f"<div class='date'>🕐 {message.date.strftime('%Y-%m-%d %H:%M:%S')}</div>"
                )
            if message.text:
                html_parts.append(
                    f"<div class='text'>{html.escape(message.text)}</div>"
                )

            if message.media:
                media_total += 1
                media_type = get_media_type(message)
                record["media"] = {
                    "type": media_type,
                    "class": type(message.media).__name__,
                }

                if media_type in ("webpage", "geo", "contact", "poll", "venue", "dice"):
                    record["status"] = "metadata_only"
                    html_parts.append(
                        f"<div class='meta'>📦 {html.escape(str(media_type))}</div>"
                    )
                else:
                    target_dir = media_dirs.get(
                        media_type,
                        media_dirs["document"],
                    )
                    downloaded = None
                    try:
                        downloaded = await download_media_robust(
                            client=client,
                            dialog_id=dialog_id,
                            message=message,
                            output_dir=target_dir,
                            session_name=session_name,
                        )
                    except FloodWaitError:
                        raise
                    except Exception:
                        logger.exception(
                            "EXPORT MEDIA ERROR | message=%s",
                            message.id,
                        )

                    if downloaded:
                        file_path = Path(downloaded)
                        relative = Path("media") / target_dir.name / file_path.name
                        record["media"]["path"] = str(relative).replace("\\", "/")
                        record["status"] = "media_completed"
                        media_ok += 1

                        rel = html.escape(str(relative).replace("\\", "/"))

                        if media_type == "photo":
                            html_parts.append(
                                f"<div class='media'><a href='{rel}' target='_blank'>"
                                f"<img src='{rel}' loading='lazy'></a></div>"
                            )
                        elif media_type == "video":
                            html_parts.append(
                                f"<div class='media'><video controls preload='metadata'>"
                                f"<source src='{rel}'></video></div>"
                            )
                        elif media_type in ("audio", "voice"):
                            html_parts.append(
                                f"<div class='media'><audio controls>"
                                f"<source src='{rel}'></audio></div>"
                            )
                        else:
                            label = {
                                "sticker": "🧩 Open Sticker",
                                "gif": "🎞 Open GIF",
                                "document": "📄 Open File",
                            }.get(media_type, "📦 Open Media")
                            html_parts.append(
                                f"<div class='media'><a class='file' href='{rel}' target='_blank'>"
                                f"{label}</a></div>"
                            )
                    else:
                        record["status"] = "media_failed"
                        media_failed += 1
                        html_parts.append(
                            "<div class='error'>⚠️ Media download failed.</div>"
                        )

            html_parts.append(
                f"<div class='meta'>Message ID: {message.id}</div>"
            )
            html_parts.append("</div>")
            records.append(record)

            if index % 50 == 0:
                logger.info(
                    "EXPORT PROGRESS | %s/%s | dialog=%s",
                    index,
                    len(messages),
                    dialog_id,
                )

        html_parts.extend(["</div></body></html>"])

        html_file = output_dir / "messages.html"
        json_file = data_dir / "messages.json"
        summary_file = output_dir / "export_summary.json"

        html_file.write_text(
            "\n".join(html_parts),
            encoding="utf-8",
        )

        json_file.write_text(
            json.dumps(
                {
                    "export_version": "6.0",
                    "session": session_name,
                    "dialog_id": dialog_id,
                    "chat_title": chat_title,
                    "message_count": len(records),
                    "messages": records,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        summary = {
            "status": "completed" if media_failed == 0 else "partial",
            "session": session_name,
            "dialog_id": dialog_id,
            "chat_title": chat_title,
            "message_count": len(records),
            "media_total": media_total,
            "media_completed": media_ok,
            "media_failed": media_failed,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "html": str(html_file),
        }

        summary_file.write_text(
            json.dumps(
                summary,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        return summary


# ============================================================
# 44. EXPORT ONE CHAT
# ============================================================

@dp.callback_query(
    F.data.regexp(
        r"^exportone:(pv|group|channel):-?\d+$"
    )
)
async def export_one(
    callback: CallbackQuery,
):

    if not is_admin(callback.from_user.id):
        return

    _, kind, dialog_id_raw = callback.data.split(":")
    dialog_id = int(dialog_id_raw)

    session_name = selected_session.get(callback.from_user.id)
    if not session_name:
        await safe_answer(callback, "Session انتخاب نشده.", show_alert=True)
        return

    account_name = sanitize_filename(
        Path(session_name).stem,
        "account",
    )
    export_name = (
        "Export_"
        + datetime.now().strftime("%Y%m%d_%H%M%S")
    )

    running_dir = (
        EXPORTS_RUNNING_DIR
        / account_name
        / export_name
        / kind
        / str(dialog_id)
    )

    await safe_edit_text(
        callback.message,
        "⏳ <b>FULL EXPORT STARTED</b>\n\n"
        f"📱 <code>{html.escape(session_name)}</code>\n"
        f"🆔 <code>{dialog_id}</code>\n\n"
        "📦 Messages + Media در حال Export هستند...",
        parse_mode="HTML",
    )

    try:
        result = await export_dialog_full(
            session_name=session_name,
            dialog_id=dialog_id,
            output_dir=running_dir,
            message_limit=None,
        )

        source_root = (
            running_dir.parent.parent
        )
        destination = (
            EXPORTS_COMPLETED_DIR
            / account_name
            / export_name
        )

        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        if destination.exists():
            shutil.rmtree(destination)

        shutil.move(
            str(source_root),
            str(destination),
        )

        report_dir = EXPORT_REPORTS_DIR / account_name
        report_dir.mkdir(parents=True, exist_ok=True)

        result["path"] = str(destination)
        result["kind"] = kind
        result["export_name"] = export_name

        (report_dir / f"{export_name}_{dialog_id}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        await safe_edit_text(
            callback.message,
            "✅ <b>FULL EXPORT COMPLETE</b>\n\n"
            f"💬 Messages: <b>{result['message_count']}</b>\n"
            f"📦 Media: <b>{result['media_total']}</b>\n"
            f"✅ Media OK: <b>{result['media_completed']}</b>\n"
            f"❌ Media Failed: <b>{result['media_failed']}</b>\n\n"
            f"📁 <code>{html.escape(str(destination))}</code>",
            reply_markup=main_keyboard(),
            parse_mode="HTML",
        )

        await safe_answer(callback, "✅ Export complete")

    except Exception as exc:
        logger.exception("FULL EXPORT FAILED")

        try:
            source_root = running_dir.parent.parent
            destination = (
                EXPORTS_FAILED_DIR
                / account_name
                / export_name
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source_root.exists():
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.move(str(source_root), str(destination))
        except Exception:
            logger.exception("Failed export move failed")

        await safe_edit_text(
            callback.message,
            "❌ <b>FULL EXPORT FAILED</b>\n\n"
            f"<code>{type(exc).__name__}</code>\n\n"
            f"<code>{html.escape(str(exc)[:700])}</code>",
            reply_markup=main_keyboard(),
            parse_mode="HTML",
        )

        await safe_answer(
            callback,
            "❌ Export failed",
            show_alert=True,
        )


# ============================================================
# 45. EXPORT SELECTED CHATS
# ============================================================

@dp.callback_query(
    F.data.regexp(
        r"^export:selected:(pv|group|channel)$"
    )
)
async def export_selected(
    callback: CallbackQuery,
):

    if not is_admin(callback.from_user.id):
        return

    kind = callback.data.split(":")[2]
    session_name = selected_session.get(callback.from_user.id)

    if not session_name:
        await safe_answer(callback, "Session انتخاب نشده.", show_alert=True)
        return

    selections = get_selections(callback.from_user.id)
    ids = sorted(selections[kind])

    if not ids:
        await safe_answer(callback, "هیچ موردی انتخاب نشده.", show_alert=True)
        return

    account_name = sanitize_filename(Path(session_name).stem, "account")
    export_name = "Export_" + datetime.now().strftime("%Y%m%d_%H%M%S")

    running_root = (
        EXPORTS_RUNNING_DIR
        / account_name
        / export_name
    )
    running_root.mkdir(parents=True, exist_ok=True)

    await safe_edit_text(
        callback.message,
        "⏳ <b>BULK EXPORT STARTED</b>\n\n"
        f"📱 <code>{html.escape(session_name)}</code>\n"
        f"📦 Chats: <b>{len(ids)}</b>",
        parse_mode="HTML",
    )

    results = []
    success = []
    failed = []

    for index, dialog_id in enumerate(ids, start=1):
        try:
            logger.info(
                "BULK EXPORT %d/%d | dialog=%s",
                index,
                len(ids),
                dialog_id,
            )

            out = (
                running_root
                / kind
                / str(dialog_id)
            )

            result = await export_dialog_full(
                session_name=session_name,
                dialog_id=dialog_id,
                output_dir=out,
                message_limit=None,
            )

            results.append(result)
            success.append(dialog_id)

        except Exception as exc:
            failed.append(dialog_id)
            logger.exception(
                "BULK EXPORT FAILED | dialog=%s | %s",
                dialog_id,
                exc,
            )

    manifest = {
        "status": "completed" if not failed else "partial",
        "session": session_name,
        "kind": kind,
        "export_name": export_name,
        "selected_count": len(ids),
        "success_count": len(success),
        "failed_count": len(failed),
        "success_ids": success,
        "failed_ids": failed,
        "results": results,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    (running_root / "bulk_export_report.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    destination_base = (
        EXPORTS_COMPLETED_DIR
        if not failed
        else EXPORTS_FAILED_DIR
    )
    destination = (
        destination_base
        / account_name
        / export_name
    )
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists():
        shutil.rmtree(destination)

    shutil.move(
        str(running_root),
        str(destination),
    )

    report_dir = EXPORT_REPORTS_DIR / account_name
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / f"{export_name}_{kind}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    await safe_edit_text(
        callback.message,
        "✅ <b>BULK EXPORT FINISHED</b>\n\n"
        f"📦 Selected: <b>{len(ids)}</b>\n"
        f"✅ Success: <b>{len(success)}</b>\n"
        f"❌ Failed: <b>{len(failed)}</b>\n\n"
        f"📁 <code>{html.escape(str(destination))}</code>",
        reply_markup=main_keyboard(),
        parse_mode="HTML",
    )

    await safe_answer(callback, "✅ Export finished")


# ============================================================
# 46A. LIVE ACCOUNT EXPLORER
# ============================================================

async def get_all_dialogs_live(session_name: str) -> List[DialogInfo]:
    """Return all dialogs from the selected authenticated session.

    Bots are intentionally included as private dialogs.
    """
    async with session_manager.get_lock(session_name):
        client = await session_manager.connect(session_name)
        result = []

        async for dialog in client.iter_dialogs():
            entity = dialog.entity

            if isinstance(entity, User):
                result.append(DialogInfo(
                    id=dialog.id,
                    title=dialog.name or "Unknown",
                    kind="pv",
                    username=getattr(entity, "username", None),
                ))
            elif isinstance(entity, Chat):
                result.append(DialogInfo(
                    id=dialog.id,
                    title=dialog.name or "Group",
                    kind="group",
                ))
            elif isinstance(entity, Channel):
                if getattr(entity, "megagroup", False):
                    result.append(DialogInfo(
                        id=dialog.id,
                        title=dialog.name or "Group",
                        kind="group",
                        username=getattr(entity, "username", None),
                    ))
                else:
                    result.append(DialogInfo(
                        id=dialog.id,
                        title=dialog.name or "Channel",
                        kind="channel",
                        username=getattr(entity, "username", None),
                    ))

        return result


def all_chats_keyboard(dialogs, page: int, page_size: int = 8):
    start = page * page_size
    visible = dialogs[start:start + page_size]
    kb = InlineKeyboardBuilder()

    for dialog in visible:
        icons = {
            "pv": "🤖" if (dialog.username or "").lower().startswith("bot") else "👤",
            "group": "👥",
            "channel": "📢",
        }
        # Try to identify bots by username suffix where available; the
        # actual PV list also includes bots, so label remains generic when unknown.
        icon = icons.get(dialog.kind, "💬")
        kb.button(
            text=f"{icon} {dialog.title[:34]}",
            callback_data=f"allchat:{dialog.kind}:{dialog.id}",
        )

    if page > 0:
        kb.button(
            text="⬅️ Previous",
            callback_data=f"allchats:{page - 1}",
        )
    if start + page_size < len(dialogs):
        kb.button(
            text="Next ➡️",
            callback_data=f"allchats:{page + 1}",
        )

    kb.button(
        text="⭐ Saved Messages",
        callback_data="savedlive:open",
    )
    kb.button(
        text="⬅️ Back",
        callback_data="main:refresh",
    )
    kb.adjust(1)
    return kb.as_markup()


@dp.callback_query(
    F.data.regexp(r"^allchats:\d+$")
)
async def all_chats_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    user_id = callback.from_user.id
    session_name = selected_session.get(user_id)
    if not session_name:
        await safe_answer(callback, "اول یک Session انتخاب کن.", show_alert=True)
        return

    page = int(callback.data.split(":")[1])

    try:
        dialogs = await get_all_dialogs_live(session_name)
        page_size = 8
        total_pages = max(1, (len(dialogs) + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))

        await safe_edit_text(
            callback.message,
            (
                "🗂 <b>ALL CHATS</b>\n\n"
                f"📱 Session: <code>{html.escape(session_name)}</code>\n"
                f"📊 Total: <b>{len(dialogs)}</b>\n"
                f"📄 Page: <b>{page + 1}/{total_pages}</b>\n\n"
                "PV، Bot، Group و Channel همگی اینجا نمایش داده می‌شوند."
            ),
            reply_markup=all_chats_keyboard(dialogs, page, page_size),
            parse_mode="HTML",
        )
        await safe_answer(callback)
    except Exception as exc:
        logger.exception("All chats load failed")
        await safe_edit_text(
            callback.message,
            (
                "❌ <b>All Chats Error</b>\n\n"
                f"<code>{type(exc).__name__}</code>\n"
                f"<code>{html.escape(str(exc)[:500])}</code>"
            ),
            reply_markup=main_keyboard(),
            parse_mode="HTML",
        )
        await safe_answer(callback)


@dp.callback_query(
    F.data.regexp(r"^allchat:(pv|group|channel):-?\d+$")
)
async def all_chat_open_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    _, kind, dialog_id_raw = callback.data.split(":")
    dialog_id = int(dialog_id_raw)

    user_id = callback.from_user.id
    viewer_state[user_id] = {
        "kind": kind,
        "dialog_id": dialog_id,
        "page": 0,
        "page_size": DEFAULT_VIEWER_PAGE_SIZE,
        "date": None,
        "date_mode": None,
        "filter": "all",
        "search": None,
    }

    await show_message_page(
        callback,
        kind,
        dialog_id,
        0,
    )


# ============================================================
# 46B. LIVE SAVED MESSAGES
# ============================================================

async def live_saved_keyboard(user_id: int, page: int, has_older: bool):
    kb = InlineKeyboardBuilder()

    profile = get_saved_view_profile(user_id)
    page_size = profile.get("page_size", DEFAULT_VIEWER_PAGE_SIZE)

    if page > 0:
        kb.button(
            text="⬅️ Newer",
            callback_data=f"savedlive:page:{page - 1}",
        )

    kb.button(
        text="🔄 Refresh",
        callback_data=f"savedlive:page:{page}",
    )

    if has_older:
        kb.button(
            text="Older ➡️",
            callback_data=f"savedlive:page:{page + 1}",
        )

    kb.button(
        text=f"🔢 Messages: {page_size}",
        callback_data="savedlive:size",
    )

    kb.button(
        text="📄 Jump to Page",
        callback_data="savedlive:jump",
    )

    kb.button(
        text="🗂 All Chats",
        callback_data="allchats:0",
    )

    kb.button(
        text="🏠 Main",
        callback_data="main:refresh",
    )

    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


async def fetch_live_saved_messages(session_name: str, page: int, page_size: int):
    async with session_manager.get_lock(session_name):
        client = await session_manager.connect(session_name)
        messages = []

        async for message in client.iter_messages(
            "me",
            limit=page_size,
            add_offset=page * page_size,
        ):
            messages.append(message)

        return messages


async def send_live_saved_message(
    user_id: int,
    message,
    temp_root: Path,
):
    """Send a Saved Messages item from the live Session."""
    session_name = selected_session.get(user_id)
    if not session_name:
        raise RuntimeError("No selected session")

    caption = message_preview_text(message)

    if not message.media:
        return await bot.send_message(
            user_id,
            caption,
            parse_mode="HTML",
        )

    media_type = get_media_type(message)

    if media_type in (
        "webpage",
        "geo",
        "contact",
        "poll",
        "venue",
        "dice",
    ):
        return await bot.send_message(
            user_id,
            caption,
            parse_mode="HTML",
        )

    client = session_manager.clients.get(session_name)
    if not client:
        client = await session_manager.connect(session_name)

    temp_dir = (
        temp_root
        / "saved"
        / str(message.id)
    )
    temp_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    downloaded = None

    for attempt in range(1, MEDIA_DOWNLOAD_RETRIES + 1):
        downloaded = await download_media_robust(
            client=client,
            dialog_id="me",
            message=message,
            output_dir=temp_dir,
            session_name=session_name,
        )
        if downloaded:
            break
        if attempt < MEDIA_DOWNLOAD_RETRIES:
            await asyncio.sleep(attempt)

    if not downloaded:
        return await bot.send_message(
            user_id,
            caption + "\n\n⚠️ Media قابل دانلود نبود.",
            parse_mode="HTML",
        )

    path = Path(downloaded)
    size_mb = path.stat().st_size / (1024 * 1024)

    if size_mb > MAX_VIEW_MEDIA_SIZE_MB:
        return await bot.send_message(
            user_id,
            caption + f"\n\n⚠️ Media بزرگ است: {size_mb:.2f} MB",
            parse_mode="HTML",
        )

    if media_type == "photo":
        try:
            return await bot.send_photo(
                user_id,
                photo=FSInputFile(path),
                caption=caption[:1024],
                parse_mode="HTML",
            )
        except Exception:
            return await bot.send_document(
                user_id,
                document=FSInputFile(path),
                caption=caption[:1024],
                parse_mode="HTML",
            )

    if media_type == "video":
        if path.suffix.lower() in (".mp4", ".m4v"):
            try:
                return await bot.send_video(
                    user_id,
                    video=FSInputFile(path),
                    caption=caption[:1024],
                    supports_streaming=True,
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return await bot.send_document(
            user_id,
            document=FSInputFile(path),
            caption=caption[:1024],
            parse_mode="HTML",
        )

    if media_type == "gif":
        try:
            return await bot.send_animation(
                user_id,
                animation=FSInputFile(path),
                caption=caption[:1024],
                parse_mode="HTML",
            )
        except Exception:
            pass

    if media_type == "voice":
        try:
            return await bot.send_voice(
                user_id,
                voice=FSInputFile(path),
                caption=caption[:1024],
                parse_mode="HTML",
            )
        except Exception:
            pass

    if media_type == "audio":
        try:
            return await bot.send_audio(
                user_id,
                audio=FSInputFile(path),
                caption=caption[:1024],
                parse_mode="HTML",
            )
        except Exception:
            pass

    if media_type == "sticker":
        try:
            sticker = await bot.send_sticker(
                user_id,
                sticker=FSInputFile(path),
            )
            info = await bot.send_message(
                user_id,
                caption,
                parse_mode="HTML",
            )
            viewer_message_ids.setdefault(user_id, []).append(info.message_id)
            return sticker
        except Exception:
            pass

    return await bot.send_document(
        user_id,
        document=FSInputFile(path),
        caption=caption[:1024],
        parse_mode="HTML",
    )


async def show_live_saved_messages(
    callback: CallbackQuery,
    page: int = 0,
):
    if not is_admin(callback.from_user.id):
        return

    user_id = callback.from_user.id
    session_name = selected_session.get(user_id)

    if not session_name:
        await safe_answer(
            callback,
            "اول یک Session انتخاب کن.",
            show_alert=True,
        )
        return

    try:
        profile = get_saved_view_profile(user_id)
        profile["page"] = page
        page_size = profile.get("page_size", DEFAULT_VIEWER_PAGE_SIZE)

        await safe_answer(
            callback,
            "⏳ Loading Saved Messages...",
        )

        await clear_viewer_messages(user_id)
        await cleanup_local_viewer(user_id)

        messages = await fetch_live_saved_messages(
            session_name,
            page,
            page_size,
        )

        # If Telegram has no messages on this page, avoid showing a blank page.
        if not messages and page > 0:
            page -= 1
            messages = await fetch_live_saved_messages(
                session_name,
                page,
                page_size,
            )

        has_older = len(messages) == page_size

        await safe_edit_text(
            callback.message,
            (
                "⭐ <b>SAVED MESSAGES</b>\n\n"
                f"📱 Session: <code>{html.escape(session_name)}</code>\n"
                f"📄 Page: <b>{page + 1}</b>\n"
                f"📨 Loaded: <b>{len(messages)}</b>\n\n"
                "این پیام‌ها مستقیم از Saved Messages همین Session خوانده می‌شوند."
            ),
            reply_markup=await live_saved_keyboard(
                user_id,
                page,
                has_older,
            ),
            parse_mode="HTML",
        )

        viewer_message_ids[user_id] = []

        temp_root = LOCAL_VIEWER_DIR / str(user_id)
        temp_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        for message in reversed(messages):
            try:
                sent = await send_live_saved_message(
                    user_id,
                    message,
                    temp_root,
                )

                if sent:
                    viewer_message_ids[user_id].append(
                        sent.message_id
                    )

            except FloodWaitError:
                raise
            except Exception as exc:
                logger.exception(
                    "Live Saved Message send failed | message=%s",
                    message.id,
                )
                fallback = await bot.send_message(
                    user_id,
                    (
                        f"⚠️ Message <code>{message.id}</code>\n"
                        f"<code>{type(exc).__name__}</code>"
                    ),
                    parse_mode="HTML",
                )
                viewer_message_ids[user_id].append(
                    fallback.message_id
                )

    except FloodWaitError as exc:
        await safe_edit_text(
            callback.message,
            (
                "⏳ Telegram درخواست‌ها را محدود کرده است.\n\n"
                f"حدود {exc.seconds} ثانیه دیگر دوباره امتحان کن."
            ),
            reply_markup=main_keyboard(),
        )

    except AuthKeyDuplicatedError:
        await safe_edit_text(
            callback.message,
            (
                "❌ <b>Session در حال استفاده هم‌زمان است</b>\n\n"
                "این فایل Session هم‌زمان از IP دیگری استفاده شده "
                "و Telegram اتصال جدید را رد کرده است.\n\n"
                "اجرای دیگری از همین Session را متوقف کن و سپس "
                "دوباره امتحان کن."
            ),
            reply_markup=main_keyboard(),
            parse_mode="HTML",
        )

    except Exception as exc:
        logger.exception(
            "Live Saved Messages failed"
        )
        await safe_edit_text(
            callback.message,
            (
                "❌ <b>Saved Messages Error</b>\n\n"
                f"<code>{type(exc).__name__}</code>\n"
                f"<code>{html.escape(str(exc)[:700])}</code>"
            ),
            reply_markup=main_keyboard(),
            parse_mode="HTML",
        )


@dp.callback_query(F.data == "savedlive:size")
async def saved_live_size_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    current = get_saved_view_profile(callback.from_user.id).get(
        "page_size", DEFAULT_VIEWER_PAGE_SIZE
    )

    kb = InlineKeyboardBuilder()
    for size in VIEWER_PAGE_SIZES:
        mark = "✅" if size == current else "☐"
        kb.button(
            text=f"{mark} {size} messages",
            callback_data=f"savedlive:setsize:{size}",
        )
    kb.button(text="⬅️ Back", callback_data="savedlive:page:0")
    kb.adjust(1)

    await safe_edit_text(
        callback.message,
        "🔢 <b>Saved Messages per page</b>\n\n"
        f"Current: <b>{current}</b>",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await safe_answer(callback)


@dp.callback_query(F.data.regexp(r"^savedlive:setsize:\d+$"))
async def saved_live_set_size_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    size = int(callback.data.split(":")[2])
    if size not in VIEWER_PAGE_SIZES:
        await safe_answer(callback, "Invalid size", show_alert=True)
        return

    profile = get_saved_view_profile(callback.from_user.id)
    profile["page_size"] = size
    profile["page"] = 0

    await show_live_saved_messages(callback, page=0)


@dp.callback_query(F.data == "savedlive:jump")
async def saved_live_jump_prompt(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    pending_input[callback.from_user.id] = {
        "type": "saved_page_jump"
    }

    current = get_saved_view_profile(callback.from_user.id).get(
        "page", 0
    )

    await safe_edit_text(
        callback.message,
        "📄 <b>Jump to Saved Messages Page</b>\n\n"
        f"Page فعلی: <b>{current + 1}</b>\n\n"
        "شماره صفحه را بفرست. مثال: <code>10</code>",
        parse_mode="HTML",
    )
    await safe_answer(callback)


@dp.callback_query(
    F.data == "savedlive:open"
)
async def saved_live_open_handler(
    callback: CallbackQuery,
):
    await show_live_saved_messages(
        callback,
        page=0,
    )


@dp.callback_query(
    F.data.regexp(r"^savedlive:page:\d+$")
)
async def saved_live_page_handler(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        return
    page = int(callback.data.split(":")[2])
    get_saved_view_profile(callback.from_user.id)["page"] = page
    await show_live_saved_messages(
        callback,
        page=page,
    )


# ============================================================
# 46. EXPORT MENU
# ============================================================

@dp.callback_query(
    F.data == "export:menu"
)
async def export_menu_handler(
    callback: CallbackQuery,
):

    if not is_admin(callback.from_user.id):
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="💬 PV Manager", callback_data="manager:pv")
    kb.button(text="👥 Group Manager", callback_data="manager:group")
    kb.button(text="📢 Channel Manager", callback_data="manager:channel")
    kb.button(text="⬅️ Back", callback_data="main:refresh")
    kb.adjust(1)

    await safe_edit_text(
        callback.message,
        "📦 <b>Data Export</b>\n\n"
        "از Manager مربوطه Chatها را انتخاب کن و سپس Export را بزن.",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )

    await safe_answer(callback)


# ============================================================
# 43. RUN
# ============================================================

async def run_bot():

    print("=" * 70)
    print("🤖 TELEGRAM EXPORTER")
    print("=" * 70)
    print("✅ Configuration loaded")
    print("✅ Colab Secrets loaded")
    print("✅ Drive storage ready")
    print("✅ Telethon ready")
    print("✅ aiogram ready")
    print()
    print("📁 Project:")
    print(PROJECT_ROOT)
    print()
    print("📱 Session folder:")
    print(SESSIONS_ACTIVE_DIR)
    print()
    print("🧪 Viewer temp:")
    print(LOCAL_VIEWER_DIR)
    print()
    print("🚀 Bot is starting...")
    print("=" * 70)

    try:

        await dp.start_polling(
            bot,
            allowed_updates=(
                dp.resolve_used_update_types()
            ),
        )

    finally:

        for session_name in list(
            session_manager.clients.keys()
        ):

            try:

                await session_manager.disconnect(
                    session_name
                )

            except Exception:

                logger.exception(
                    "Session shutdown failed"
                )

        try:

            await bot.session.close()

        except Exception:

            pass


# ============================================================
# 44. START
# ============================================================

# await run_bot()
