"""Логика сценариев бота (хендлеры вызываются напрямую; aiogram заменён заглушкой)."""
import asyncio
import os
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(__file__))
import aiogram_stub  # noqa: E402

aiogram_stub.install()

from app.bot import keyboards as kb  # noqa: E402
from app.bot.handlers import active, create, menu, reports  # noqa: E402
from app.db import Database  # noqa: E402
from app.service import Service  # noqa: E402
from app.timeutil import UTC  # noqa: E402
from test_core import FakeVK, make_cfg  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")


class FakeState:
    def __init__(self):
        self.state, self.data = None, {}

    async def get_state(self):
        return self.state

    async def set_state(self, s):
        self.state = s

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def clear(self):
        self.state, self.data = None, {}


class FakeMessage:
    def __init__(self, text=None, caption=None, photo=None):
        self.text, self.caption, self.photo = text, caption, photo
        self.sent = []

    async def answer(self, text, reply_markup=None, **kw):
        self.sent.append(("text", text, reply_markup))

    async def answer_photo(self, photo, caption=None, reply_markup=None, **kw):
        self.sent.append(("photo", caption, reply_markup))

    async def edit_text(self, text, reply_markup=None):
        self.sent.append(("edit", text, reply_markup))


class FakeCB:
    def __init__(self, data, message=None):
        self.data, self.message, self.alerts = data, message or FakeMessage(), []

    async def answer(self, text=None, show_alert=False):
        self.alerts.append(text)


class FakeBot:
    async def download(self, photo, destination):
        with open(destination, "wb") as fh:
            fh.write(b"\xff\xd8fake-jpeg")


def last_text(msg):
    return msg.sent[-1][1]


async def env(tmp):
    db = Database(os.path.join(tmp, "b.sqlite3"))
    await db.open()
    cfg = make_cfg(tmp)
    clock = lambda: datetime(2026, 11, 27, 9, 0, tzinfo=MSK).astimezone(UTC)  # пятница 09:00

    async def notify(_):
        return True

    svc = Service(db, FakeVK(), cfg, notify, now=clock)
    return db, cfg, svc


def run(coro):
    return asyncio.run(coro)


def test_full_create_flow_text_only():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db, cfg, svc = await env(tmp)
            st, msg = FakeState(), FakeMessage()
            await create.start_flow(msg, st, cfg, svc)
            assert st.state is create.Flow.material

            await create.material_text(FakeMessage(text="Обновление сервера"), st, cfg, svc)
            assert st.state is create.Flow.topic
            m = FakeMessage()
            await create.topic_text(FakeMessage(text="Апдейт"), st, cfg, svc)
            assert st.state is create.Flow.weekday

            m = FakeMessage()
            await create.pick_day(FakeCB("day:4", m), st, cfg, svc)
            assert st.state is create.Flow.repeat  # шага «время» нет: отправка всегда в 11:00 МСК
            assert st.data["draft"]["send_time"] == "11:00"

            m = FakeMessage()
            await create.pick_repeat(FakeCB("rp:1", m), st, cfg, svc)
            assert st.state is create.Flow.preview
            assert "Предпросмотр" in last_text(m) and "⏰ 11:00 (МСК)" in last_text(m)
            assert m.sent[-1][2] is not None  # кнопки подтвердить/изменить/отменить

            m = FakeMessage()
            await create.confirm(FakeCB("pv:ok", m), st, cfg, svc)
            assert st.state is None
            posts = await db.list_posts(10, 0)
            assert len(posts) == 1
            p = posts[0]
            assert (p.text, p.topic, p.weekday, p.send_time, p.repeat_weekly, p.status) == \
                   ("Обновление сервера", "Апдейт", 4, "11:00", True, "pending")
            assert "сохранён" in last_text(m) and "27.11 11:00" in last_text(m)

            # двойное нажатие «Подтвердить» не создаёт второй пост
            await create.confirm(FakeCB("pv:ok", FakeMessage()), st, cfg, svc) if st.state else None
            assert await db.count_posts() == 1
            await db.close()
    run(go())


def test_photo_requires_caption_and_is_saved():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db, cfg, svc = await env(tmp)
            st = FakeState()
            await create.start_flow(FakeMessage(), st, cfg, svc)
            photo = [SimpleNamespace(file_id="small"), SimpleNamespace(file_id="BIG")]

            nocap = FakeMessage(photo=photo, caption=None)
            await create.material_photo(nocap, st, FakeBot(), cfg, svc)
            assert st.state is create.Flow.material and "подпись" in last_text(nocap)

            ok = FakeMessage(photo=photo, caption="Текст с фото")
            await create.material_photo(ok, st, FakeBot(), cfg, svc)
            assert st.state is create.Flow.topic
            d = st.data["draft"]
            assert d["photo_file_id"] == "BIG" and os.path.exists(d["photo_path"])
            path = d["photo_path"]

            # отмена удаляет скачанный файл
            await create.cancel(FakeCB("fl:cancel"), st)
            assert not os.path.exists(path) and st.state is None
            await db.close()
    run(go())


def _flow_with_photo(st, cfg, svc):
    async def go():
        await create.start_flow(FakeMessage(), st, cfg, svc)
        photo = [SimpleNamespace(file_id="BIG")]
        await create.material_photo(FakeMessage(photo=photo, caption="Текст с фото"), st, FakeBot(), cfg, svc)
        await create.topic_text(FakeMessage(text="Тема"), st, cfg, svc)
        await create.pick_day(FakeCB("day:4"), st, cfg, svc)
        await create.pick_repeat(FakeCB("rp:0"), st, cfg, svc)
    return go()


def test_photo_is_stored_in_db_and_temp_file_removed():
    """На бесплатном Render диск не постоянный: фото обязано жить в БД, а не в файле."""
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db, cfg, svc = await env(tmp)
            st = FakeState()
            await _flow_with_photo(st, cfg, svc)
            path = st.data["draft"]["photo_path"]
            assert os.path.exists(path)
            await create.confirm(FakeCB("pv:ok"), st, cfg, svc)
            post = (await db.list_posts(10, 0))[0]
            assert post.has_photo and post.photo_file_id == "BIG"
            assert await db.get_photo(post.id) == b"\xff\xd8fake-jpeg"
            assert not os.path.exists(path)  # временный файл удалён
            await db.close()
    run(go())


def test_edit_keeps_or_removes_photo():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db, cfg, svc = await env(tmp)
            st = FakeState()
            await _flow_with_photo(st, cfg, svc)
            await create.confirm(FakeCB("pv:ok"), st, cfg, svc)
            pid = (await db.list_posts(10, 0))[0].id

            # правка только дня: фото остаётся
            st, m = FakeState(), FakeMessage()
            await active.edit_post(FakeCB(f"ep:{pid}", m), st, db, cfg, svc)
            await create.edit_field(FakeCB("ed:day", m), st, cfg, svc)
            await create.pick_day(FakeCB("day:0", m), st, cfg, svc)
            await create.confirm(FakeCB("pv:ok", m), st, cfg, svc)
            p = await db.get_post(pid)
            assert p.weekday == 0 and p.has_photo
            assert await db.get_photo(pid) == b"\xff\xd8fake-jpeg"

            # заменили материал на текст без фото: фото удаляется
            st, m = FakeState(), FakeMessage()
            await active.edit_post(FakeCB(f"ep:{pid}", m), st, db, cfg, svc)
            await create.edit_field(FakeCB("ed:text", m), st, cfg, svc)
            await create.material_text(FakeMessage(text="Только текст"), st, cfg, svc)
            await create.confirm(FakeCB("pv:ok", m), st, cfg, svc)
            p = await db.get_post(pid)
            assert p.text == "Только текст" and not p.has_photo and await db.get_photo(pid) is None
            await db.close()
    run(go())


def test_edit_existing_post_via_preview():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db, cfg, svc = await env(tmp)
            post = await svc.create_post({"text": "Старый", "topic": None, "weekday": 4,
                                          "send_time": "12:00", "repeat": False})
            st, m = FakeState(), FakeMessage()
            await active.edit_post(FakeCB(f"ep:{post.id}", m), st, db, cfg, svc)
            assert st.state is create.Flow.preview and st.data["edit_id"] == post.id

            await create.edit_field(FakeCB("ed:day", m), st, cfg, svc)
            assert st.state is create.Flow.weekday and st.data["from_preview"] is True
            await create.pick_day(FakeCB("day:0", m), st, cfg, svc)
            assert st.state is create.Flow.preview  # сразу вернулись в предпросмотр

            await create.edit_field(FakeCB("ed:text", m), st, cfg, svc)
            await create.material_text(FakeMessage(text="Новый текст"), st, cfg, svc)
            assert st.state is create.Flow.preview

            await create.confirm(FakeCB("pv:ok", m), st, cfg, svc)
            p = await db.get_post(post.id)
            assert (p.text, p.weekday, p.send_time, p.status) == ("Новый текст", 0, "11:00", "pending")
            assert await db.count_posts() == 1
            await db.close()
    run(go())


def test_list_view_delete_and_pagination():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db, cfg, svc = await env(tmp)
            for i in range(7):
                await svc.create_post({"text": f"Пост номер {i}", "weekday": i % 7, "send_time": "10:00"})
            text, markup, page = await active.build_list(db, 0)
            assert "всего 7" in text and page == 0
            assert text.count("<b>#") == kb.PAGE_SIZE
            text2, _, page2 = await active.build_list(db, 1)
            assert text2.count("<b>#") == 2 and page2 == 1
            _, _, clamp = await active.build_list(db, 99)
            assert clamp == 1

            m = FakeMessage()
            await active.view_post(FakeCB("vw:1", m), db, cfg)
            assert "Пост #1" in m.sent[-1][1] and "Пост номер 0" in m.sent[-1][1]

            await active.delete_do(FakeCB("dly:1", m), db, svc)
            assert await db.count_posts() == 6
            await db.close()
    run(go())


def test_list_empty_and_reports_screen():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db, cfg, svc = await env(tmp)
            text, markup, _ = await active.build_list(db, 0)
            assert "Пока нет" in text and markup is None
            text, markup = await reports.build_reports(db, cfg, 0)
            assert "Отправок пока не было" in text
            await db.close()
    run(go())


def test_reports_screen_after_send_and_retry_flow():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db, cfg, svc = await env(tmp)
            post = await svc.create_post({"text": "Привет", "weekday": 4, "send_time": "09:00"})
            svc._now = lambda: datetime(2026, 11, 27, 11, 0, 10, tzinfo=MSK).astimezone(UTC)
            await svc.tick()
            text, markup = await reports.build_reports(db, cfg, 0)
            assert "✅" in text and "Привет" in text
            assert (await db.get_post(post.id)).status == "sent"
            m = FakeMessage()
            await active.retry_ask(FakeCB(f"rt:{post.id}", m), db)
            assert "проверьте" in last_text(m).lower()
            kicked = []
            svc.kick = lambda: kicked.append(1)  # проверка запускается сразу, а не ждёт расписания
            await active.retry_do(FakeCB(f"rty:{post.id}", m), svc, cfg)
            assert kicked and (await db.get_post(post.id)).status == "pending"
            await db.close()
    run(go())


def test_welcome_mentions_editor():
    cfg = make_cfg(tempfile.gettempdir())
    t = menu.welcome(cfg)
    assert "GRAY Editor" in t and "Winston_Goodman" in t
