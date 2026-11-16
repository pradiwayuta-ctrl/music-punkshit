import asyncio
from collections import deque
from dataclasses import dataclass, field
from config import OWNER_ID

@dataclass
class TrackItem:
    title: str
    stream_url: str
    webpage_url: str
    duration: str
    requester_id: int
    requester_name: str
    thread_id: int | None = None
    source_label: str = "Music Vault"

@dataclass
class GroupState:
    chat_id: int
    queue: deque[TrackItem] = field(default_factory=deque)
    current_track: TrackItem | None = None
    is_paused: bool = False
    idle_since: float | None = None
    play_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    enqueue_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    privileged_cache: set[int] = field(default_factory=lambda: {OWNER_ID})
    privileged_cache_ts: float = 0.0
    call_id: int | None = None
    now_playing_msg_id: int | None = None

_groups: dict[int, GroupState] = {}
_call_id_to_chat: dict[int, int] = {}

def get_group_state(chat_id: int) -> GroupState:
    st = _groups.get(chat_id)
    if st is None:
        st = GroupState(chat_id=chat_id)
        _groups[chat_id] = st
    return st

def is_group_allowed(chat_id: int) -> bool:
    if not ALLOWED_GROUPS:
        return True # Ubah jadi True jika ingin bebas grup mana saja
    return chat_id in ALLOWED_GROUPS
