import sys
import inspect
import functools
import contextvars

_active_bot_var = contextvars.ContextVar("active_bot", default=None)


def get_bot_module():
    """
    Returns the currently active bot module (from execution context or sys.modules).
    Ensures that when tests patch or re-import bot instances, handlers and services
    resolve attributes from the exact caller instance.
    """
    active = _active_bot_var.get()
    if active is not None:
        return active
    mod = sys.modules.get("bot") or sys.modules.get("Telegram.bot")
    if mod is not None:
        return mod
    main_mod = sys.modules.get("__main__")
    if main_mod and (hasattr(main_mod, "account_pool") or hasattr(main_mod, "dp")):
        return main_mod
    return None


def bind_active_bot(fn, module):
    """
    Binds a function to a specific bot module instance in contextvars so that
    any nested calls made by handlers/services resolve attributes from that instance.
    """
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            token = _active_bot_var.set(module)
            try:
                return await fn(*args, **kwargs)
            finally:
                _active_bot_var.reset(token)
        return async_wrapper
    elif callable(fn):
        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            token = _active_bot_var.set(module)
            try:
                return fn(*args, **kwargs)
            finally:
                _active_bot_var.reset(token)
        return sync_wrapper
    return fn
