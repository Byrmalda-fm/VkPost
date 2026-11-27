"""Минимальная заглушка aiogram — только чтобы прогнать ЛОГИКУ хендлеров без установки aiogram.

Это не замена интеграционной проверки с настоящим Telegram: фильтры/FSM aiogram здесь не работают.
"""
import sys
import types


class _Magic:
    def __getattr__(self, name):
        return _Magic()

    def __call__(self, *a, **k):
        return _Magic()

    def __eq__(self, other):
        return _Magic()

    def __hash__(self):
        return 1


class _Deco:
    def __call__(self, *a, **k):
        return lambda f: f


class Router:
    def __init__(self, name=None):
        self.message = self.callback_query = _Deco()


class _Plain:
    def __init__(self, *a, **k):
        self.args, self.kwargs = a, k


class State:
    def __init__(self):
        self.name = None

    def __set_name__(self, owner, name):
        self.name = f"{owner.__name__}:{name}"


class StatesGroup:
    pass


class TelegramBadRequest(Exception):
    pass


def install():
    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    mod("aiogram", F=_Magic(), Router=Router, Bot=_Plain, BaseMiddleware=object, Dispatcher=_Plain)
    mod("aiogram.exceptions", TelegramBadRequest=TelegramBadRequest)
    mod("aiogram.fsm")
    mod("aiogram.fsm.context", FSMContext=_Plain)
    mod("aiogram.fsm.state", State=State, StatesGroup=StatesGroup)
    mod("aiogram.filters", Command=lambda *a, **k: _Magic(), CommandStart=lambda *a, **k: _Magic())
    mod("aiogram.types", Message=_Plain, CallbackQuery=_Plain, InlineKeyboardMarkup=_Plain,
        InlineKeyboardButton=_Plain, KeyboardButton=_Plain, ReplyKeyboardMarkup=_Plain,
        FSInputFile=_Plain, TelegramObject=_Plain, BotCommand=_Plain)
