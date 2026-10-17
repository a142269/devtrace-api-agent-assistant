"""Per-request public timeline events; never emits model reasoning."""
from contextvars import ContextVar

event_sink = ContextVar('developer_event_sink', default=None)


class EventList(list):
    def append(self, event):
        super().append(event)
        sink = event_sink.get()
        if sink is not None:
            sink(event)


def publish_existing(events):
    sink = event_sink.get()
    if sink is not None:
        for event in events:
            sink(event)
    return EventList(events)
