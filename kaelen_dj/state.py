import os
import glob
import asyncio
from collections import deque
from dataclasses import dataclass, field
from kaelen_dj.config import OWNER_ID, ALLOWED_GROUPS, CACHE_DIR


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


groups: dict[int, GroupState] = {}
call_id_to_chat: dict[int, int] = {}
bg_tasks: set[asyncio.Task] = set()


def is_group_allowed(chat_id: int) -> bool:
    if not ALLOWED_GROUPS:
        return False
    return chat_id in ALLOWED_GROUPS


def get_group_state(chat_id: int) -> GroupState:
    st = groups.get(chat_id)
    if st is None:
        st = GroupState(chat_id=chat_id)
        groups[chat_id] = st
    return st


def spawn_task(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)
    return task


def cleanup_old_cache(keep_path: str | None = None):
    try:
        active_paths = {
            os.path.abspath(st.current_track.stream_url)
            for st in groups.values()
            if st.current_track and os.path.exists(st.current_track.stream_url)
        }
        for st in groups.values():
            for t in st.queue:
                if os.path.exists(t.stream_url):
                    active_paths.add(os.path.abspath(t.stream_url))

        files = sorted(glob.glob(os.path.join(CACHE_DIR, "*")), key=os.path.getmtime)
        for f in files[:-5]:
            abs_f = os.path.abspath(f)
            if (keep_path and abs_f == os.path.abspath(keep_path)) or abs_f in active_paths:
                continue
            try:
                os.remove(f)
            except Exception:
                pass
    except Exception:
        pass
