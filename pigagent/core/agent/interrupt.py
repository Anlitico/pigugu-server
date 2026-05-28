# pigagent/core/agent/interrupt.py
"""InterruptManager + @check_interrupt decorator for React agent loops.

Local in-process interrupt system:
  - asyncio.Event per key  -  lightweight, zero-dependency
  - Thread-safe via threading.Lock
  - Auto-cleanup of expired events (background task)
  - @check_interrupt decorator (regular async + async generators)

Redis Pub/Sub for cross-process interrupt is deferred to a future iteration.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
import traceback
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Union, AsyncIterator

from loguru import logger

from .state import AgentState, StateStatus


class InterruptedException(Exception):
    """Legacy exception for interrupt events. Kept for backward compatibility."""

    pass


class InterruptManager:
    """Global interrupt event manager  -  in-process only.

    Key-based design: each interrupt event has a unique key (e.g. "agent:room_abc").
    Events are stored as local asyncio.Event objects.

    Use get_interrupt_manager() to obtain the singleton instance.
    """

    def __init__(
        self,
        auto_cleanup_enabled: bool = True,
        auto_cleanup_interval: int = 300,
        auto_cleanup_max_age: int = 1800,
    ):
        self._events: Dict[str, asyncio.Event] = {}
        self._creation_times: Dict[str, float] = {}
        self._lock = threading.Lock()

        self._auto_cleanup_enabled = auto_cleanup_enabled
        self._auto_cleanup_interval = auto_cleanup_interval
        self._auto_cleanup_max_age = auto_cleanup_max_age
        self._auto_cleanup_task: Optional[asyncio.Task] = None

        logger.info("InterruptManager initialized (local-only mode)")

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start_background_tasks(self) -> None:
        """Start auto-cleanup background task. Call once at application startup."""
        if self._auto_cleanup_enabled:
            if self._auto_cleanup_task and not self._auto_cleanup_task.done():
                logger.warning("Auto cleanup task already running")
            else:
                self._auto_cleanup_task = asyncio.create_task(self._auto_cleanup())
                logger.info(
                    f"InterruptManager auto-cleanup started "
                    f"(interval={self._auto_cleanup_interval}s, max_age={self._auto_cleanup_max_age}s)"
                )

    async def stop(self):
        """Stop auto-cleanup task. Call at application shutdown."""
        if self._auto_cleanup_task and not self._auto_cleanup_task.done():
            self._auto_cleanup_task.cancel()
            try:
                await self._auto_cleanup_task
            except asyncio.CancelledError:
                pass
            self._auto_cleanup_task = None
            logger.info("InterruptManager stopped")

    # ── Auto cleanup ───────────────────────────────────────────────────

    async def _auto_cleanup(self):
        """Background task that periodically cleans expired events."""
        logger.info(
            f"Auto cleanup task started "
            f"(interval={self._auto_cleanup_interval}s, max_age={self._auto_cleanup_max_age}s)"
        )
        try:
            while True:
                try:
                    await asyncio.sleep(self._auto_cleanup_interval)
                    current_time = time.time()
                    keys_to_cleanup = []

                    with self._lock:
                        for key, creation_time in list(self._creation_times.items()):
                            age = current_time - creation_time
                            if age > self._auto_cleanup_max_age:
                                keys_to_cleanup.append((key, age))

                    if keys_to_cleanup:
                        logger.info(f"Auto cleanup: found {len(keys_to_cleanup)} expired events")
                        for key, age in keys_to_cleanup:
                            logger.debug(f"Cleaning up expired event: {key} (age: {age:.1f}s)")
                            self.cleanup(key)
                        logger.info(
                            f"Auto cleanup completed: cleaned {len(keys_to_cleanup)} events, "
                            f"remaining: {len(self._events)}"
                        )

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Error in auto cleanup loop: {e}\n{traceback.format_exc()}")

        except asyncio.CancelledError:
            logger.info("Auto cleanup task cancelled")
        finally:
            logger.info("Auto cleanup task stopped")

    # ── Event lifecycle ─────────────────────────────────────────────────

    def create(self, key: str) -> asyncio.Event:
        """Create (or replace) an interrupt event. Returns the new event.

        If the key already exists, the old event is replaced to avoid reusing
        already-triggered events.
        """
        with self._lock:
            if key in self._events:
                old = self._events[key]
                logger.warning(f"Replacing existing interrupt event: {key} (was_set={old.is_set()})")
                if not old.is_set():
                    old.set()
                del self._events[key]
                self._creation_times.pop(key, None)

            self._events[key] = asyncio.Event()
            self._creation_times[key] = time.time()

            count = len(self._events)
            logger.info(f"Created interrupt event: {key} (total: {count})")

            if count > 100:
                logger.warning(f"InterruptManager has {count} events  -  possible memory leak!")

            return self._events[key]

    def get(self, key: str) -> Optional[asyncio.Event]:
        """Get interrupt event by key, or None if not found."""
        with self._lock:
            event = self._events.get(key)
            if event is None:
                logger.debug(f"Interrupt event not found: {key} (total: {len(self._events)})")
            return event

    async def trigger(self, key: str) -> None:
        """Trigger an interrupt signal. Creates the event if it doesn't exist."""
        with self._lock:
            local_event = self._events.get(key)

        if local_event is not None:
            local_event.set()
            logger.info(f"Triggered interrupt: {key}")
            return

        # Auto-create if doesn't exist (handles race conditions)
        logger.warning(f"Interrupt event not found: {key}, creating and triggering")
        event = self.create(key)
        event.set()
        logger.info(f"Created and triggered interrupt: {key}")

    def is_set(self, key: str) -> bool:
        """Check if an interrupt event has been triggered."""
        event = self._events.get(key)
        return event.is_set() if event else False

    def cleanup(self, key: str, trigger_event: bool = False) -> None:
        """Remove an interrupt event.

        Args:
            key: Interrupt event key.
            trigger_event: If True, set() before deleting (wakes waiters).
                           Default False (normal cleanup after task completion).
        """
        with self._lock:
            if key in self._events:
                event = self._events[key]
                was_set = event.is_set()

                if trigger_event and not was_set:
                    event.set()
                    logger.debug(f"Triggered event during cleanup: {key}")

                del self._events[key]
                self._creation_times.pop(key, None)

                logger.info(f"Cleaned up interrupt event: {key} (was_set={was_set}, remaining: {len(self._events)})")
            else:
                logger.debug(f"Attempted to cleanup non-existent event: {key}")

    # ── Queries ────────────────────────────────────────────────────────

    def get_all_keys(self) -> List[str]:
        """Get all active interrupt event keys."""
        with self._lock:
            return list(self._events.keys())

    def get_interrupted_keys(self) -> List[str]:
        """Get all keys whose events have been triggered."""
        with self._lock:
            return [key for key, event in self._events.items() if event.is_set()]

    def get_stats(self) -> Dict[str, Any]:
        """Return current state for monitoring."""
        current_time = time.time()
        set_count = 0
        not_set_count = 0
        events_detail = []

        with self._lock:
            for key, event in self._events.items():
                is_set = event.is_set()
                creation_time = self._creation_times.get(key, 0)
                age_seconds = current_time - creation_time if creation_time > 0 else 0

                if is_set:
                    set_count += 1
                else:
                    not_set_count += 1

                events_detail.append({
                    "key": key,
                    "is_set": is_set,
                    "age_seconds": round(age_seconds, 2),
                })

        result = {
            "total_events": len(self._events),
            "set_events": set_count,
            "not_set_events": not_set_count,
            "auto_cleanup_enabled": self._auto_cleanup_enabled,
            "auto_cleanup_running": (
                self._auto_cleanup_task is not None
                and not self._auto_cleanup_task.done()
            ),
            "events_detail": events_detail,
        }

        if self._auto_cleanup_enabled:
            result["auto_cleanup_config"] = {
                "check_interval_seconds": self._auto_cleanup_interval,
                "max_age_seconds": self._auto_cleanup_max_age,
            }

        return result


# -------------------------------------------------------------------------------
# Singleton
# -------------------------------------------------------------------------------

_interrupt_manager: InterruptManager | None = None


def get_interrupt_manager() -> InterruptManager:
    """Get or create the global InterruptManager singleton."""
    global _interrupt_manager
    if _interrupt_manager is None:
        _interrupt_manager = InterruptManager()
    return _interrupt_manager


# -------------------------------------------------------------------------------
# @check_interrupt decorator
# -------------------------------------------------------------------------------


def _validate_and_bind_args(func: Callable, args: tuple, kwargs: dict):
    """Validate function signature and bind arguments."""
    sig = inspect.signature(func)
    if "state" not in sig.parameters:
        raise TypeError(
            f"Function '{func.__name__}' decorated with @check_interrupt "
            f"must have a 'state' parameter of type AgentState or its subclass"
        )
    bound_args = sig.bind(*args, **kwargs)
    bound_args.apply_defaults()
    state_value = bound_args.arguments.get("state")
    if state_value is None:
        raise TypeError(f"Function '{func.__name__}' requires a 'state' parameter but got None")
    if not isinstance(state_value, AgentState):
        raise TypeError(
            f"Function '{func.__name__}' requires 'state' parameter to be "
            f"AgentState or its subclass, but got {type(state_value).__name__}"
        )
    return bound_args, state_value


def _should_skip(
    state_value: AgentState,
    skip_on_interrupted: bool,
    skip_on_fail: bool,
    skip_on_success: bool,
) -> bool:
    if state_value.status == StateStatus.INTERRUPTED.value and skip_on_interrupted:
        return True
    if state_value.status == StateStatus.FAIL.value and skip_on_fail:
        return True
    if state_value.status == StateStatus.SUCCESS.value and skip_on_success:
        return True
    return False


def _resolve_callback(callback, method_name: str, self_value):
    if callback is None and self_value:
        return getattr(self_value, method_name, None)
    elif isinstance(callback, str) and self_value:
        return getattr(self_value, callback, None)
    elif callable(callback):
        return callback
    return None


async def _call_callback(
    callback_func: Optional[Callable], state: AgentState, **extra_kwargs
) -> Union[None, AgentState, AsyncIterator[Any]]:
    if callback_func is None:
        return None
    try:
        if inspect.isasyncgenfunction(callback_func):
            return callback_func(state, **extra_kwargs)
        result = callback_func(state, **extra_kwargs)
        if inspect.iscoroutine(result):
            return await result
        return result
    except Exception as e:
        logger.warning(f"Error calling callback {callback_func.__name__}: {e}")
        logger.debug(f"Callback error traceback: {traceback.format_exc()}")
        return None


def _get_interrupt_key(key, bound_args) -> str | None:
    if callable(key):
        try:
            key_sig = inspect.signature(key)
            key_kwargs = {}
            for param_name in key_sig.parameters.keys():
                if param_name in bound_args.arguments:
                    key_kwargs[param_name] = bound_args.arguments[param_name]
            return str(key(**key_kwargs))
        except Exception as e:
            logger.error(f"Error generating key from function: {e}")
            return None
    else:
        return key


def check_interrupt(
    key: Union[str, Callable[[AgentState], str]],
    on_start: Optional[Union[str, Callable]] = None,
    on_success: Optional[Union[str, Callable]] = None,
    on_error: Optional[Union[str, Callable]] = None,
    on_interrupt: Optional[Union[str, Callable]] = None,
    on_finally: Optional[Union[str, Callable]] = None,
    skip_on_interrupted: bool = True,
    skip_on_fail: bool = True,
    skip_on_success: bool = False,
) -> Callable:
    """Decorator: monitor an interrupt event alongside an async function.

    Uses asyncio.wait([func_task, event.wait()])  -  whichever completes first wins.
    Supports both regular async functions and async generators.

    The decorated function MUST have a 'state' parameter of type AgentState.
    """

    def decorator(func: Callable) -> Callable:
        is_async_gen = inspect.isasyncgenfunction(func)

        if is_async_gen:
            @wraps(func)
            async def async_gen_wrapper(*args, **kwargs):
                bound_args, state_value = _validate_and_bind_args(func, args, kwargs)

                if _should_skip(state_value, skip_on_interrupted, skip_on_fail, skip_on_success):
                    return

                self_value = bound_args.arguments.get("self")
                resolved_on_start = _resolve_callback(on_start, "_on_start", self_value)
                resolved_on_success = _resolve_callback(on_success, "_on_success", self_value)
                resolved_on_error = _resolve_callback(on_error, "_on_error", self_value)
                resolved_on_interrupt = _resolve_callback(on_interrupt, "_on_interrupt", self_value)
                resolved_on_finally = _resolve_callback(on_finally, "_on_finally", self_value)

                final_key: str | None = _get_interrupt_key(key, bound_args)

                if resolved_on_start:
                    callback_result = await _call_callback(resolved_on_start, state_value)
                    if inspect.isasyncgen(callback_result):
                        async for item in callback_result:
                            yield item

                if not final_key:
                    try:
                        async for item in func(*args, **kwargs):
                            yield item
                        if resolved_on_success:
                            callback_result = await _call_callback(resolved_on_success, state_value, result=None)
                            if inspect.isasyncgen(callback_result):
                                async for item in callback_result:
                                    yield item
                    except InterruptedException:
                        if state_value and hasattr(state_value, "status"):
                            state_value.status = StateStatus.INTERRUPTED.value
                        if resolved_on_interrupt:
                            callback_result = await _call_callback(
                                resolved_on_interrupt, state_value,
                                interrupt_key=final_key or "unknown",
                            )
                            if inspect.isasyncgen(callback_result):
                                async for item in callback_result:
                                    yield item
                    except Exception as e:
                        if resolved_on_error:
                            callback_result = await _call_callback(resolved_on_error, state_value, exception=e)
                            if inspect.isasyncgen(callback_result):
                                async for item in callback_result:
                                    yield item
                    finally:
                        if resolved_on_finally:
                            callback_result = await _call_callback(resolved_on_finally, state_value)
                            if inspect.isasyncgen(callback_result):
                                async for item in callback_result:
                                    yield item
                    return

                manager = get_interrupt_manager()
                event = manager.get(final_key)
                if not event:
                    logger.warning(f"Interrupt event not found: {final_key}, creating automatically")
                    event = manager.create(final_key)

                generator = func(*args, **kwargs)
                interrupt_task = asyncio.create_task(event.wait())

                try:
                    while True:
                        next_item_task = asyncio.create_task(generator.__anext__())

                        done, pending = await asyncio.wait(
                            [next_item_task, interrupt_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )

                        if interrupt_task in done:
                            next_item_task.cancel()
                            try:
                                await next_item_task
                            except asyncio.CancelledError:
                                pass

                            if state_value and hasattr(state_value, "status"):
                                state_value.status = StateStatus.INTERRUPTED.value

                            if resolved_on_interrupt:
                                callback_result = await _call_callback(
                                    resolved_on_interrupt, state_value, interrupt_key=final_key,
                                )
                                if inspect.isasyncgen(callback_result):
                                    async for item in callback_result:
                                        yield item

                            logger.info(f"Generator {func.__name__} interrupted: {final_key}")
                            break

                        try:
                            item = next_item_task.result()
                            yield item
                        except StopAsyncIteration:
                            if resolved_on_success:
                                callback_result = await _call_callback(
                                    resolved_on_success, state_value, result=None,
                                )
                                if inspect.isasyncgen(callback_result):
                                    async for item in callback_result:
                                        yield item
                            break
                        except InterruptedException:
                            if state_value and hasattr(state_value, "status"):
                                state_value.status = StateStatus.INTERRUPTED.value
                            if resolved_on_interrupt:
                                callback_result = await _call_callback(
                                    resolved_on_interrupt, state_value, interrupt_key=final_key,
                                )
                                if inspect.isasyncgen(callback_result):
                                    async for item in callback_result:
                                        yield item
                            logger.info(f"Generator {func.__name__} interrupted by inner: {final_key}")
                            break

                except InterruptedException:
                    pass
                except Exception as e:
                    if state_value and hasattr(state_value, "status"):
                        state_value.status = StateStatus.ERROR.value
                    if resolved_on_error:
                        callback_result = await _call_callback(resolved_on_error, state_value, exception=e)
                        if inspect.isasyncgen(callback_result):
                            async for item in callback_result:
                                yield item
                finally:
                    if not interrupt_task.done():
                        interrupt_task.cancel()
                        try:
                            await interrupt_task
                        except asyncio.CancelledError:
                            pass
                    try:
                        await generator.aclose()
                    except Exception:
                        pass
                    if resolved_on_finally:
                        callback_result = await _call_callback(resolved_on_finally, state_value)
                        if inspect.isasyncgen(callback_result):
                            async for item in callback_result:
                                yield item
                    manager.cleanup(final_key)

            return async_gen_wrapper
        else:
            @wraps(func)
            async def wrapper(*args, **kwargs):
                bound_args, state_value = _validate_and_bind_args(func, args, kwargs)

                if _should_skip(state_value, skip_on_interrupted, skip_on_fail, skip_on_success):
                    return None

                self_value = bound_args.arguments.get("self")
                resolved_on_start = _resolve_callback(on_start, "_on_start", self_value)
                resolved_on_success = _resolve_callback(on_success, "_on_success", self_value)
                resolved_on_error = _resolve_callback(on_error, "_on_error", self_value)
                resolved_on_interrupt = _resolve_callback(on_interrupt, "_on_interrupt", self_value)
                resolved_on_finally = _resolve_callback(on_finally, "_on_finally", self_value)

                final_key: str | None = _get_interrupt_key(key, bound_args)

                if resolved_on_start:
                    await _call_callback(resolved_on_start, state_value)

                if not final_key:
                    try:
                        result = await func(*args, **kwargs)
                        if resolved_on_success:
                            await _call_callback(resolved_on_success, state_value, result=result)
                        return result
                    except InterruptedException:
                        if state_value and hasattr(state_value, "status"):
                            state_value.status = StateStatus.INTERRUPTED.value
                        if resolved_on_interrupt:
                            await _call_callback(
                                resolved_on_interrupt, state_value,
                                interrupt_key=final_key or "unknown",
                            )
                        return None
                    except Exception as e:
                        if state_value and hasattr(state_value, "status"):
                            state_value.status = StateStatus.ERROR.value
                        if resolved_on_error:
                            await _call_callback(resolved_on_error, state_value, exception=e)
                        return None
                    finally:
                        if resolved_on_finally:
                            await _call_callback(resolved_on_finally, state_value)

                manager = get_interrupt_manager()
                event = manager.get(final_key)
                if not event:
                    logger.warning(f"Interrupt event not found: {final_key}, creating automatically")
                    event = manager.create(final_key)

                func_task = asyncio.create_task(func(*args, **kwargs))
                interrupt_task = asyncio.create_task(event.wait())

                try:
                    done, pending = await asyncio.wait(
                        [func_task, interrupt_task], return_when=asyncio.FIRST_COMPLETED,
                    )

                    if func_task in done:
                        interrupt_task.cancel()
                        try:
                            await interrupt_task
                        except asyncio.CancelledError:
                            pass

                        if func_task.exception() is None:
                            result = func_task.result()
                            if resolved_on_success:
                                await _call_callback(resolved_on_success, state_value, result=result)
                            return result
                        else:
                            exc = func_task.exception()
                            if isinstance(exc, InterruptedException):
                                if state_value and hasattr(state_value, "status"):
                                    state_value.status = StateStatus.INTERRUPTED.value
                                if resolved_on_interrupt:
                                    await _call_callback(
                                        resolved_on_interrupt, state_value, interrupt_key=final_key,
                                    )
                                logger.info(f"Function {func.__name__} interrupted by inner: {final_key}")
                                return None
                            if state_value and hasattr(state_value, "status"):
                                state_value.status = StateStatus.ERROR.value
                            if resolved_on_error:
                                await _call_callback(resolved_on_error, state_value, exception=exc)
                            return None
                    else:
                        func_task.cancel()
                        try:
                            await func_task
                        except asyncio.CancelledError:
                            pass

                        if state_value and hasattr(state_value, "status"):
                            state_value.status = StateStatus.INTERRUPTED.value
                        if resolved_on_interrupt:
                            await _call_callback(resolved_on_interrupt, state_value, interrupt_key=final_key)
                        logger.info(f"Function {func.__name__} interrupted: {final_key}")
                        return None

                except Exception as e:
                    if not func_task.done():
                        func_task.cancel()
                    if not interrupt_task.done():
                        interrupt_task.cancel()
                    if isinstance(e, asyncio.CancelledError):
                        raise
                    raise
                finally:
                    if resolved_on_finally:
                        await _call_callback(resolved_on_finally, state_value)
                    manager.cleanup(final_key)

            return wrapper

    return decorator
