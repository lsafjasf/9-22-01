"""事件排序库（修复版）。

排序键改用混合逻辑时钟（HLC）：(physical_ms, logical, node)。
- physical_ms：单调不减的物理毫秒，时钟回拨时冻结在上次观测值；
- logical：同一物理毫秒内的递增计数，区分同刻事件；
- node：来源唯一标识，为并发事件提供确定且可重现的全序。

原始墙上时间戳（Event.wall_time）仅用于展示与审计，不参与排序。
"""

from __future__ import annotations

import heapq
import json
import os
import threading
import time
import uuid
import warnings
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, List, Optional


@dataclass(frozen=True)
class HLCTimestamp:
    physical: int  # 单调物理毫秒（>= 本进程观测到的墙上时钟）
    logical: int   # 同一物理毫秒内的递增计数
    node: str      # 节点/来源唯一标识，并发事件的确定性仲裁者

    def key(self):
        return (self.physical, self.logical, self.node)


class Event:
    """事件。__slots__ 控制海量事件时的单条内存开销。"""

    __slots__ = ("payload", "source", "wall_time", "hlc")

    def __init__(self, payload, source, wall_time=None, hlc=None):
        self.payload = payload
        self.source = source
        self.wall_time = wall_time  # 原始墙上时间戳，仅展示/审计，允许为 None
        self.hlc = hlc              # 因果排序键

    # ---- 兼容旧接口 ----
    @property
    def timestamp(self):
        return self.wall_time

    @property
    def display_time(self):
        """展示用时间：优先原始墙上时间，缺失时退化为 HLC 物理分量。"""
        if self.wall_time is not None:
            return self.wall_time
        return self.hlc.physical / 1000.0 if self.hlc is not None else None

    def sort_key(self):
        if self.hlc is None:
            raise ValueError("事件缺少 HLC，请先打戳或用 sort_events 自动补戳")
        return self.hlc.key()

    def __repr__(self):
        return "Event(source=%r, hlc=%r, wall_time=%r, payload=%r)" % (
            self.source, self.hlc, self.wall_time, self.payload)


class EventClock:
    """每个来源（节点）持有一个，生成单调的 HLC 时间戳。

    - clock：墙上时钟（秒），可注入假时钟便于测试；
    - state_path：持久化文件，重启后即使墙上时间倒退也保持单调；
    - persist_every：每多少次打戳落盘一次（吞吐与崩溃恢复的权衡）。
    """

    def __init__(self, node_id: Optional[str] = None,
                 clock: Optional[Callable[[], float]] = None,
                 state_path: Optional[str] = None,
                 persist_every: int = 1):
        self.node_id = node_id or "node-%s" % uuid.uuid4().hex[:12]
        self._clock = clock or time.time
        self._state_path = state_path
        self._persist_every = max(1, persist_every)
        self._since_persist = 0
        self._lock = threading.Lock()
        self._physical = 0
        self._logical = 0
        if state_path and os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
            self._physical = int(state.get("physical", 0))
            self._logical = int(state.get("logical", 0))

    def _wall_ms(self) -> int:
        return int(self._clock() * 1000)

    def now(self) -> HLCTimestamp:
        """本地事件打戳：时钟回拨时 physical 冻结、logical 递增。"""
        with self._lock:
            wall = self._wall_ms()
            if wall > self._physical:
                self._physical, self._logical = wall, 0
            else:
                self._logical += 1
            self._maybe_persist()
            return HLCTimestamp(self._physical, self._logical, self.node_id)

    def observe(self, other: Optional[HLCTimestamp]) -> HLCTimestamp:
        """观察到外部事件后打戳：保证本地新事件因果上排在它之后。"""
        if other is None:
            return self.now()
        with self._lock:
            wall = self._wall_ms()
            if wall > self._physical and wall > other.physical:
                self._physical, self._logical = wall, 0
            elif self._physical == other.physical:
                self._logical = max(self._logical, other.logical) + 1
            elif self._physical > other.physical:
                self._logical += 1
            else:
                self._physical, self._logical = other.physical, other.logical + 1
            self._maybe_persist()
            return HLCTimestamp(self._physical, self._logical, self.node_id)

    def stamp(self, payload, source=None, wall_time=None, after=None) -> Event:
        """生成事件。after=某事件/某 HLC 表示本事件因果上发生在它之后。"""
        if after is not None:
            hlc = self.observe(after.hlc if isinstance(after, Event) else after)
        else:
            hlc = self.now()
        if wall_time is None:
            wall_time = self._clock()
        return Event(payload, source or self.node_id,
                     wall_time=wall_time, hlc=hlc)

    def _maybe_persist(self):
        if not self._state_path:
            return
        self._since_persist += 1
        if self._since_persist >= self._persist_every:
            self._since_persist = 0
            self._persist()

    def _persist(self):
        tmp = self._state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"physical": self._physical,
                       "logical": self._logical,
                       "node": self.node_id}, fh)
        os.replace(tmp, self._state_path)

    def close(self):
        with self._lock:
            if self._state_path:
                self._persist()


def _ensure_hlc(event: Event, clock: EventClock) -> Event:
    if event.hlc is None:
        event.hlc = clock.now()
    return event


def sort_events(events: Iterable[Event],
                clock: Optional[EventClock] = None) -> List[Event]:
    """对任意事件集合给出确定的全序列表。

    缺少 HLC 的旧事件按"被观察到的顺序"补戳，因此先被看到的事件
    一定排在前面；排序稳定，同键事件保持输入顺序，结果可重现。
    """
    clock = clock or EventClock(node_id="sorter")
    stamped = [_ensure_hlc(e, clock) for e in events]
    stamped.sort(key=lambda e: e.hlc.key())
    return stamped


def merge_streams(streams: Iterable[Iterable[Event]]) -> Iterator[Event]:
    """把多个各自已按 HLC 升序的事件流合并为一个因果全序的惰性迭代器。

    - 内存开销 O(来源数)，与事件总数无关，可处理无限/海量流；
    - 缺少 HLC 的事件在拉取时按观察顺序补戳；
    - 来源标识重复时按注册顺序消歧（并发出警告），结果确定可重现。
    """
    merge_clock = EventClock(node_id="merge")
    heap = []
    seen_sources = {}
    for idx, stream in enumerate(streams):
        it = iter(stream)
        try:
            first = next(it)
        except StopIteration:
            continue
        _ensure_hlc(first, merge_clock)
        if first.source in seen_sources:
            warnings.warn("来源标识重复：%r（流 #%d 与 #%d），已按注册顺序消歧"
                          % (first.source, seen_sources[first.source], idx))
        else:
            seen_sources[first.source] = idx
        heap.append((first.hlc.key(), idx, first, it))
    heapq.heapify(heap)
    while heap:
        _, idx, event, it = heapq.heappop(heap)
        yield event
        try:
            nxt = _ensure_hlc(next(it), merge_clock)
        except StopIteration:
            continue
        heapq.heappush(heap, (nxt.hlc.key(), idx, nxt, it))


# ---- 兼容旧接口的模块级函数 ----
_default_clock = EventClock()


def stamp(payload, source=None, wall_time=None, after=None) -> Event:
    return _default_clock.stamp(payload, source, wall_time, after)
