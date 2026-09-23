"""原始（有缺陷）的事件排序实现，仅用于复现问题与对照测试。

缺陷：直接用本机墙上时间戳排序。
- 时钟回拨（NTP 校正/手动改时）后，后发生的事件拿到更小的时间戳；
- 同一毫秒内多个事件时间戳相同，先后关系丢失；
- 进程重启后墙上时间可能倒退，新事件排到旧事件之前；
- 跨来源合并时各机器时钟有偏差，因果关系被打乱。
"""

import time


class Event:
    def __init__(self, payload, source, timestamp=None):
        self.payload = payload
        self.source = source
        self.timestamp = time.time() if timestamp is None else timestamp

    def __repr__(self):
        return "Event(source=%r, timestamp=%r, payload=%r)" % (
            self.source, self.timestamp, self.payload)


def stamp(payload, source, clock=time.time):
    return Event(payload, source, timestamp=clock())


def sort_events(events):
    return sorted(events, key=lambda e: e.timestamp)


def merge_streams(streams):
    events = [e for stream in streams for e in stream]
    events.sort(key=lambda e: e.timestamp)
    return iter(events)
