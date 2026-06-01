"""Unit tests for RoastEventBus."""

import asyncio

import pytest


class TestRoastEventBus:
    def test_singleton_is_shared(self):
        from roast.event_bus import event_bus, RoastEventBus
        assert isinstance(event_bus, RoastEventBus)

    def test_publish_to_no_subscribers_does_not_crash(self):
        from roast.event_bus import RoastEventBus
        bus = RoastEventBus()
        # Should not raise — no subscribers
        asyncio.run(bus.publish("u1", {"type": "state_change", "state": "listening"}))

    def test_subscribe_and_receive(self):
        from roast.event_bus import RoastEventBus
        bus = RoastEventBus()

        async def _run():
            q = await bus.subscribe("u1")
            await bus.publish("u1", {"type": "state_change", "state": "listening"})
            event = await asyncio.wait_for(q.get(), timeout=1.0)
            assert event == {"type": "state_change", "state": "listening"}

        asyncio.run(_run())

    def test_multiple_subscribers_same_user(self):
        from roast.event_bus import RoastEventBus
        bus = RoastEventBus()

        async def _run():
            q1 = await bus.subscribe("u1")
            q2 = await bus.subscribe("u1")
            await bus.publish("u1", {"type": "user_transcript", "text": "hello"})
            e1 = await asyncio.wait_for(q1.get(), timeout=1.0)
            e2 = await asyncio.wait_for(q2.get(), timeout=1.0)
            assert e1["text"] == "hello"
            assert e2["text"] == "hello"

        asyncio.run(_run())

    def test_different_users_isolated(self):
        from roast.event_bus import RoastEventBus
        bus = RoastEventBus()

        async def _run():
            q1 = await bus.subscribe("u1")
            q2 = await bus.subscribe("u2")
            await bus.publish("u1", {"type": "user_transcript", "text": "u1 msg"})
            await bus.publish("u2", {"type": "user_transcript", "text": "u2 msg"})
            e1 = await asyncio.wait_for(q1.get(), timeout=1.0)
            e2 = await asyncio.wait_for(q2.get(), timeout=1.0)
            assert e1["text"] == "u1 msg"
            assert e2["text"] == "u2 msg"

        asyncio.run(_run())

    def test_unsubscribe_removes_subscriber(self):
        from roast.event_bus import RoastEventBus
        bus = RoastEventBus()

        async def _run():
            q = await bus.subscribe("u1")
            await bus.unsubscribe("u1", q)
            await bus.publish("u1", {"type": "pong"})
            # Queue should be empty — no subscriber to receive
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.1)

        asyncio.run(_run())

    def test_queue_full_drops_event(self):
        from roast.event_bus import RoastEventBus
        bus = RoastEventBus()

        async def _run():
            # Create a bus with tiny queue for testing
            q = await bus.subscribe("u1")
            assert q.maxsize == 256  # default

            # Fill the queue artificially
            for i in range(256):
                q.put_nowait({"type": "ping", "n": i})

            # This publish should be dropped (queue full)
            await bus.publish("u1", {"type": "state_change", "state": "dropped"})

            # Drain queue — the dropped event should not be there
            found = False
            while not q.empty():
                event = q.get_nowait()
                if event.get("state") == "dropped":
                    found = True
            assert not found, "Event was queued despite full queue"

            # Clean up
            await bus.unsubscribe("u1", q)

        asyncio.run(_run())

    def test_unsubscribe_last_subscriber_removes_key(self):
        from roast.event_bus import RoastEventBus
        bus = RoastEventBus()

        async def _run():
            q = await bus.subscribe("u1")
            await bus.unsubscribe("u1", q)
            # Internal state should be clean
            assert "u1" not in bus._subscribers

        asyncio.run(_run())

    def test_event_bus_import_is_same_object(self):
        """Multiple imports should return the same global singleton."""
        import roast.event_bus as m1
        import roast.event_bus as m2
        assert m1.event_bus is m2.event_bus
