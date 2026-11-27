"""Тесты ядра (без Telegram): время, БД, VK-клиент на поддельном транспорте, планировщик."""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import Config, ConfigError
from app.db import Database
from app.service import Service
from app.timeutil import (
    UTC, from_iso, latest_daily_boundary, next_occurrence, parse_time, to_iso, window_for_boundary,
)
from app.vk import TransportError, VKAmbiguousError, VKClient, VKError

MSK = ZoneInfo("Europe/Moscow")


def run(coro):
    return asyncio.run(coro)


# ---------- время ----------
def test_parse_time():
    assert parse_time("9:05") == (9, 5)
    assert parse_time("09:05") == (9, 5)
    assert parse_time(" 21.30 ") == (21, 30)
    assert parse_time("24:00") is None
    assert parse_time("12:60") is None
    assert parse_time("abc") is None
    assert parse_time(None) is None


def test_next_occurrence():
    now = datetime(2026, 11, 27, 9, 0, tzinfo=MSK)  # пятница
    assert next_occurrence(4, 12, 0, MSK, now) == datetime(2026, 11, 27, 12, 0, tzinfo=MSK).astimezone(UTC)
    assert next_occurrence(4, 8, 0, MSK, now) == datetime(2026, 12, 4, 8, 0, tzinfo=MSK).astimezone(UTC)
    assert next_occurrence(4, 9, 0, MSK, now) == datetime(2026, 12, 4, 9, 0, tzinfo=MSK).astimezone(UTC)
    assert next_occurrence(0, 10, 0, MSK, now) == datetime(2026, 11, 30, 10, 0, tzinfo=MSK).astimezone(UTC)


def test_report_window():
    now = datetime(2026, 11, 27, 11, 0, 5, tzinfo=MSK)
    b = latest_daily_boundary(now, 11, 0, MSK)
    assert b == datetime(2026, 11, 27, 11, 0, tzinfo=MSK).astimezone(UTC)
    s, e = window_for_boundary(b, MSK)
    assert s == datetime(2026, 11, 26, 11, 0, tzinfo=MSK).astimezone(UTC) and e == b
    early = datetime(2026, 11, 27, 10, 59, tzinfo=MSK)
    assert latest_daily_boundary(early, 11, 0, MSK) == datetime(2026, 11, 26, 11, 0, tzinfo=MSK).astimezone(UTC)


# ---------- конфиг ----------
def test_config():
    env = {"BOT_TOKEN": "x", "OWNER_ID": "1", "VK_USER_TOKEN": "t", "VK_GROUP_ID": "-123"}
    cfg = Config.from_env(env, dotenv=None)
    assert cfg.vk_group_id == 123 and cfg.timezone == "Europe/Moscow" and cfg.report_hour == 11
    with pytest.raises(ConfigError):
        Config.from_env({}, dotenv=None)
    with pytest.raises(ConfigError):
        Config.from_env({**env, "TIMEZONE": "Mars/Base"}, dotenv=None)


def make_cfg(tmp, **over):
    env = {"BOT_TOKEN": "bot-token-123456", "OWNER_ID": "1", "VK_USER_TOKEN": "user-token-123456",
           "VK_GROUP_ID": "777", "DATA_DIR": tmp, **over}
    return Config.from_env(env, dotenv=None)


# ---------- БД ----------
async def new_db(tmp):
    db = Database(os.path.join(tmp, "t.sqlite3"))
    await db.open()
    return db


def mk(db, when_iso, repeat=False, text="Привет", photo=None, weekday=4, send_time="12:00"):
    return db.create_post(topic=None, text=text, photo_data=photo, photo_file_id=None, weekday=weekday,
                          send_time=send_time, repeat_weekly=repeat, next_run_at=when_iso)


def test_claim_is_exclusive_and_no_double_send():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db = await new_db(tmp)
            pid = await mk(db, "2026-01-01T00:00:00Z")
            future = await mk(db, "2999-01-01T00:00:00Z")
            a, b = await asyncio.gather(db.claim_due("2026-06-01T00:00:00Z"),
                                        db.claim_due("2026-06-01T00:00:00Z"))
            assert len(a) + len(b) == 1
            claimed = (a or b)[0][0]
            assert claimed.id == pid
            assert (await db.get_post(pid)).status == "sending"
            assert (await db.get_post(future)).status == "pending"
            assert await db.claim_due("2026-06-01T00:00:00Z") == []
            await db.close()
    run(go())


def test_recovery_marks_unknown_without_resend():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db = await new_db(tmp)
            one = await mk(db, "2026-01-01T00:00:00Z")
            rep = await mk(db, "2026-01-01T00:00:00Z", repeat=True)
            await db.claim_due("2026-06-01T00:00:00Z")
            recovered = await db.recover_interrupted(lambda p: "2026-06-08T09:00:00Z")
            assert {p.id for p in recovered} == {one, rep}
            assert (await db.get_post(one)).status == "unknown"
            r = await db.get_post(rep)
            assert r.status == "pending" and r.next_run_at == "2026-06-08T09:00:00Z"
            assert await db.claim_due("2026-06-01T00:00:00Z") == []
            logs = await db.list_logs(10, 0)
            assert {l.result for l in logs} == {"unknown"}
            await db.close()
    run(go())


def test_update_blocked_while_sending_and_soft_delete():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db = await new_db(tmp)
            pid = await mk(db, "2026-01-01T00:00:00Z")
            await db.claim_due("2026-06-01T00:00:00Z")
            assert await db.update_post(pid, {"text": "новый"}) is False
            assert await db.delete_post(pid) is True
            assert await db.get_post(pid) is None
            assert await db.count_posts() == 0
            assert await db.count_logs() == 1  # история сохраняется
            await db.close()
    run(go())


def test_backup():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db = await new_db(tmp)
            await mk(db, "2026-01-01T00:00:00Z")
            path = await db.backup(os.path.join(tmp, "bk"))
            assert os.path.getsize(path) > 0
            await db.close()
    run(go())


# ---------- VK-клиент ----------
class FakeTransport:
    """Скриптуемый транспорт: handlers[method] -> dict | list[dict] | Exception."""

    def __init__(self, handlers):
        self.handlers, self.calls = handlers, []

    async def post(self, url, data):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, dict(data)))
        h = self.handlers[method]
        if isinstance(h, list):
            h = h.pop(0) if len(h) > 1 else h[0]
        if isinstance(h, Exception):
            raise h
        return h(data) if callable(h) else h

    async def upload(self, url, field, data):
        self.calls.append(("UPLOAD", {"url": url, "size": len(data)}))
        return {"server": 1, "photo": "[{...}]", "hash": "h"}

    async def close(self):
        pass


def group(**kw):
    g = {"id": 777, "name": "GRAY", "is_admin": 0, "wall": 2}
    g.update(kw)
    return {"response": {"groups": [g]}}


async def _nosleep(_):
    return None


def vkc(handlers, **kw):
    t = FakeTransport(handlers)
    return VKClient(user_token="user-token-123456", group_id=777, transport=t, min_interval=0,
                    sleep=_nosleep, **kw), t


def test_suggest_confirmed_and_never_sends_from_group():
    async def go():
        vk, t = vkc({
            "groups.getById": group(),
            "wall.post": {"response": {"post_id": 55}},
            "wall.getById": {"response": {"items": [{"id": 55, "post_type": "suggest"}]}},
        })
        res = await vk.suggest_post("текст")
        assert (res.post_id, res.state) == (55, "confirmed")
        post_params = [d for m, d in t.calls if m == "wall.post"][0]
        assert post_params["owner_id"] == -777
        assert "from_group" not in post_params and "signed" not in post_params
        assert "publish_date" not in post_params
    run(go())


def test_manager_account_is_refused_before_posting():
    async def go():
        vk, t = vkc({"groups.getById": group(is_admin=1, admin_level=2)})
        with pytest.raises(VKError, match="руководител"):
            await vk.suggest_post("текст")
        assert all(m != "wall.post" for m, _ in t.calls)
    run(go())


def test_open_wall_refused_unless_allowed():
    async def go():
        vk, t = vkc({"groups.getById": group(wall=1)})
        with pytest.raises(VKError, match="открытая"):
            await vk.suggest_post("текст")
        vk2, _ = vkc({"groups.getById": group(wall=1), "wall.post": {"response": {"post_id": 1}},
                      "wall.getById": {"response": {"items": [{"post_type": "suggest"}]}}},
                     allow_open_wall=True)
        assert (await vk2.suggest_post("x")).state == "confirmed"
    run(go())


def test_direct_publication_detected_and_blocks_further_sends():
    async def go():
        vk, t = vkc({
            "groups.getById": group(),
            "wall.post": {"response": {"post_id": 9}},
            "wall.getById": {"response": {"items": [{"id": 9, "post_type": "post"}]}},
        })
        res = await vk.suggest_post("текст")
        assert res.state == "direct"
        with pytest.raises(VKError, match="остановлена"):
            await vk.suggest_post("ещё")
    run(go())


def test_unconfirmed_when_verification_fails():
    async def go():
        vk, _ = vkc({
            "groups.getById": group(),
            "wall.post": {"response": {"post_id": 3}},
            "wall.getById": {"error": {"error_code": 15, "error_msg": "denied"}},
        })
        assert (await vk.suggest_post("т")).state == "unconfirmed"
    run(go())


def test_admin_token_verification_uses_suggests_filter():
    async def go():
        vk, t = vkc({
            "groups.getById": group(),
            "wall.post": {"response": {"post_id": 5}},
            "wall.get": {"response": {"items": [{"id": 5}]}},
        }, admin_token="admin-token-123456")
        assert (await vk.suggest_post("т")).state == "confirmed"
        m, d = [c for c in t.calls if c[0] == "wall.get"][0]
        assert d["filter"] == "suggests" and d["access_token"] == "admin-token-123456"
    run(go())


def test_network_error_on_wall_post_is_ambiguous_and_not_retried():
    async def go():
        vk, t = vkc({"groups.getById": group(), "wall.post": TransportError("timeout")})
        with pytest.raises(VKAmbiguousError):
            await vk.suggest_post("т")
        assert sum(1 for m, _ in t.calls if m == "wall.post") == 1
    run(go())


def test_network_error_on_idempotent_call_is_retried():
    async def go():
        vk, t = vkc({"groups.getById": [TransportError("x"), group()],
                     "wall.post": {"response": {"post_id": 1}},
                     "wall.getById": {"response": {"items": [{"post_type": "suggest"}]}}})
        assert (await vk.suggest_post("т")).state == "confirmed"
    run(go())


def test_rate_limit_retry():
    async def go():
        vk, t = vkc({"groups.getById": group(),
                     "wall.post": [{"error": {"error_code": 6, "error_msg": "many"}},
                                   {"response": {"post_id": 2}}],
                     "wall.getById": {"response": {"items": [{"post_type": "suggest"}]}}})
        res = await vk.suggest_post("т")
        assert res.post_id == 2
        assert sum(1 for m, _ in t.calls if m == "wall.post") == 2
    run(go())


def test_vk_error_is_translated_and_has_no_token():
    async def go():
        vk, _ = vkc({"groups.getById": group(),
                     "wall.post": {"error": {"error_code": 214, "error_msg": "Access to adding post denied"}}})
        with pytest.raises(VKError) as ei:
            await vk.suggest_post("т")
        assert ei.value.code == 214 and "user-token" not in str(ei.value)
    run(go())


def test_photo_upload_flow():
    async def go():
        vk, t = vkc({
            "groups.getById": group(),
            "photos.getWallUploadServer": {"response": {"upload_url": "https://up.example/x"}},
            "photos.saveWallPhoto": {"response": [{"id": 10, "owner_id": 4242}]},
            "wall.post": {"response": {"post_id": 8}},
            "wall.getById": {"response": {"items": [{"post_type": "suggest"}]}},
        })
        await vk.suggest_post("т", photo=b"\xff\xd8jpeg-bytes")
        post = [d for m, d in t.calls if m == "wall.post"][0]
        assert post["attachments"] == "photo4242_10"
        assert [d for m, d in t.calls if m == "UPLOAD"][0]["size"] == len(b"\xff\xd8jpeg-bytes")
    run(go())


# ---------- планировщик ----------
class Clock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt


class FakeVK:
    can_verify_with_admin = False

    def __init__(self, behaviour="ok"):
        self.behaviour, self.sent = behaviour, []

    async def suggest_post(self, text, photo=None):
        from app.vk import SendResult
        self.sent.append(text)
        self.photos = getattr(self, "photos", []) + [photo]
        if self.behaviour == "error":
            raise VKError("VK: нет доступа (код 15)")
        if self.behaviour == "unknown":
            raise VKAmbiguousError("нет ответа")
        return SendResult(100 + len(self.sent), "confirmed", "ок")


def make_service(tmp, db, vk, clock):
    notes = []

    async def notify(text):
        notes.append(text)
        return True

    return Service(db, vk, make_cfg(tmp), notify, now=clock), notes


def test_tick_sends_once_and_not_again_after_restart():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db = await new_db(tmp)
            clock = Clock(datetime(2026, 11, 27, 12, 0, 30, tzinfo=MSK).astimezone(UTC))
            vk = FakeVK()
            svc, notes = make_service(tmp, db, vk, clock)
            pid = await mk(db, to_iso(datetime(2026, 11, 27, 12, 0, tzinfo=MSK)))
            await asyncio.gather(svc.tick(), svc.tick())
            await svc.tick()
            assert vk.sent == ["Привет"]
            assert (await db.get_post(pid)).status == "sent"
            # «рестарт»: новый сервис над той же БД
            svc2, _ = make_service(tmp, db, vk, clock)
            await svc2.startup()
            await svc2.tick()
            assert vk.sent == ["Привет"]
            assert "Отправлено" in notes[0]
            await db.close()
    run(go())


def test_repeat_post_moves_to_next_week():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db = await new_db(tmp)
            clock = Clock(datetime(2026, 11, 27, 12, 0, 30, tzinfo=MSK).astimezone(UTC))
            svc, _ = make_service(tmp, db, FakeVK(), clock)
            pid = await mk(db, to_iso(datetime(2026, 11, 27, 12, 0, tzinfo=MSK)), repeat=True)
            await svc.tick()
            p = await db.get_post(pid)
            assert p.status == "pending"
            assert from_iso(p.next_run_at) == datetime(2026, 12, 4, 12, 0, tzinfo=MSK).astimezone(UTC)
            await db.close()
    run(go())


def test_error_is_saved_and_reported():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db = await new_db(tmp)
            clock = Clock(datetime(2026, 11, 27, 12, 0, 30, tzinfo=MSK).astimezone(UTC))
            svc, notes = make_service(tmp, db, FakeVK("error"), clock)
            pid = await mk(db, to_iso(datetime(2026, 11, 27, 12, 0, tzinfo=MSK)))
            await svc.tick()
            p = await db.get_post(pid)
            assert p.status == "error" and "нет доступа" in p.last_error
            assert "Не удалось отправить" in notes[0] and "нет доступа" in notes[0]
            await svc.tick()  # ошибка не приводит к автоповтору
            assert (await db.list_logs(10, 0))[0].result == "error" and await db.count_logs() == 1
            assert await svc.retry_now(pid) is True
            await db.close()
    run(go())


def test_unknown_result_status():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db = await new_db(tmp)
            clock = Clock(datetime(2026, 11, 27, 12, 0, 30, tzinfo=MSK).astimezone(UTC))
            svc, notes = make_service(tmp, db, FakeVK("unknown"), clock)
            pid = await mk(db, to_iso(datetime(2026, 11, 27, 12, 0, tzinfo=MSK)))
            await svc.tick()
            assert (await db.get_post(pid)).status == "unknown"
            assert "неизвестен" in notes[0]
            await db.close()
    run(go())


def test_missed_post_not_sent_late():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db = await new_db(tmp)
            clock = Clock(datetime(2026, 11, 27, 18, 0, tzinfo=MSK).astimezone(UTC))
            vk = FakeVK()
            svc, notes = make_service(tmp, db, vk, clock)
            pid = await mk(db, to_iso(datetime(2026, 11, 27, 12, 0, tzinfo=MSK)))
            await svc.tick()
            assert vk.sent == []
            assert (await db.get_post(pid)).status == "error"
            assert (await db.list_logs(1, 0))[0].result == "missed"
            await db.close()
    run(go())


def test_daily_report_sent_once_and_empty_report():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db = await new_db(tmp)
            clock = Clock(datetime(2026, 11, 27, 11, 0, 2, tzinfo=MSK).astimezone(UTC))
            svc, notes = make_service(tmp, db, FakeVK(), clock)
            await db.set_meta("report_last", "2026-11-26T08:00:00Z")
            assert await svc.daily_report() is True
            assert "Отправок не было" in notes[-1]
            assert await svc.daily_report() is False  # повторно не отправляется
            assert len(notes) == 1
            await db.close()
    run(go())


def test_daily_report_lists_results_and_errors():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db = await new_db(tmp)
            t = datetime(2026, 11, 26, 15, 0, 30, tzinfo=MSK).astimezone(UTC)
            clock = Clock(t)
            svc, notes = make_service(tmp, db, FakeVK("error"), clock)
            await mk(db, to_iso(datetime(2026, 11, 26, 15, 0, tzinfo=MSK)), text="Обновление <b>сервера</b>")
            await svc.tick()
            clock.dt = datetime(2026, 11, 27, 11, 0, 1, tzinfo=MSK).astimezone(UTC)
            await db.set_meta("report_last", "2026-11-26T08:00:00Z")
            await svc.daily_report()
            assert "Обновление &lt;b&gt;сервера&lt;/b&gt;" in notes[-1]
            assert "Причина" in notes[-1] and "❌" in notes[-1]
            await db.close()
    run(go())


def test_first_start_does_not_send_empty_report():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db = await new_db(tmp)
            clock = Clock(datetime(2026, 11, 27, 15, 0, tzinfo=MSK).astimezone(UTC))
            svc, notes = make_service(tmp, db, FakeVK(), clock)
            await svc.startup()
            assert notes == []
            assert await db.get_meta("report_last") is not None
            await db.close()
    run(go())


def test_catch_up_report_after_downtime():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db = await new_db(tmp)
            clock = Clock(datetime(2026, 11, 27, 13, 0, tzinfo=MSK).astimezone(UTC))
            svc, notes = make_service(tmp, db, FakeVK(), clock)
            await db.set_meta("report_last", "2026-11-26T08:00:00Z")
            await svc.startup()
            assert len(notes) == 1 and "Отчёт за сутки" in notes[0]
            await db.close()
    run(go())


# ---------- Render: единое время отправки, фото в БД, Postgres-хелперы ----------
def test_config_send_time_and_render_options():
    env = {"BOT_TOKEN": "x", "OWNER_ID": "1", "VK_USER_TOKEN": "t", "VK_GROUP_ID": "5"}
    cfg = Config.from_env(env, dotenv=None)
    assert (cfg.send_time, cfg.send_hour, cfg.send_minute) == ("11:00", 11, 0)
    assert cfg.timezone == "Europe/Moscow" and cfg.database_url is None and cfg.port is None

    cfg = Config.from_env({**env, "SEND_TIME": "9:05", "PORT": "10000", "DATABASE_URL": " postgres://u:p@h/d ",
                           "RENDER_EXTERNAL_URL": "https://x.onrender.com/", "STARTUP_NOTIFY": "false"},
                          dotenv=None)
    assert cfg.send_time == "09:05" and cfg.port == 10000 and cfg.database_url == "postgres://u:p@h/d"
    assert cfg.self_ping_url == "https://x.onrender.com" and cfg.startup_notify is False

    for bad in ({"SEND_TIME": "25:00"}, {"PORT": "abc"}, {"TICK_SECONDS": "7"}, {"TICK_SECONDS": "120"}):
        with pytest.raises(ConfigError):
            Config.from_env({**env, **bad}, dotenv=None)


def test_all_posts_go_out_at_1100_moscow_regardless_of_input():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db = await new_db(tmp)
            cfg = make_cfg(tmp)
            clock = Clock(datetime(2026, 11, 27, 9, 0, tzinfo=MSK).astimezone(UTC))  # пятница 09:00 МСК
            svc, _ = make_service(tmp, db, FakeVK(), clock)
            # даже если кто-то передаст другое время — используется единое SEND_TIME
            p = await svc.create_post({"text": "A", "weekday": 4, "send_time": "03:17"})
            assert p.send_time == "11:00"
            assert from_iso(p.next_run_at).astimezone(MSK) == datetime(2026, 11, 27, 11, 0, tzinfo=MSK)
            q = await svc.create_post({"text": "B", "weekday": 0, "repeat": True})  # понедельник
            assert from_iso(q.next_run_at).astimezone(MSK) == datetime(2026, 11, 30, 11, 0, tzinfo=MSK)

            clock.dt = datetime(2026, 11, 27, 10, 59, 59, tzinfo=MSK).astimezone(UTC)
            await svc.tick()
            assert svc.vk.sent == []                      # в 10:59:59 ещё рано
            clock.dt = datetime(2026, 11, 27, 11, 0, 0, tzinfo=MSK).astimezone(UTC)
            await svc.tick()
            assert svc.vk.sent == ["A"]                   # ровно в 11:00 — уходит
            await db.close()
    run(go())


def test_late_wakeup_still_sends_within_grace_but_not_after():
    """Сервис на бесплатном Render мог спать: пост, пропущенный меньше чем на 2 часа, уходит при пробуждении."""
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db = await new_db(tmp)
            clock = Clock(datetime(2026, 11, 27, 9, 0, tzinfo=MSK).astimezone(UTC))
            svc, _ = make_service(tmp, db, FakeVK(), clock)
            late = await svc.create_post({"text": "late", "weekday": 4})
            clock.dt = datetime(2026, 11, 27, 11, 40, tzinfo=MSK).astimezone(UTC)   # проснулись в 11:40
            await svc.tick()
            assert svc.vk.sent == ["late"]
            assert (await db.get_post(late.id)).status == "sent"

            old = await svc.create_post({"text": "old", "weekday": 5})              # суббота 11:00
            clock.dt = datetime(2026, 11, 28, 14, 30, tzinfo=MSK).astimezone(UTC)   # проснулись в 14:30
            await svc.tick()
            assert svc.vk.sent == ["late"]                                           # задним числом не шлём
            assert (await db.get_post(old.id)).status == "error"
            await db.close()
    run(go())


def test_photo_bytes_live_in_db_and_reach_vk():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            db = await new_db(tmp)
            clock = Clock(datetime(2026, 11, 27, 9, 0, tzinfo=MSK).astimezone(UTC))
            svc, _ = make_service(tmp, db, FakeVK(), clock)
            jpg = os.path.join(tmp, "draft.jpg")
            with open(jpg, "wb") as fh:
                fh.write(b"\xff\xd8photo")
            post = await svc.create_post({"text": "с фото", "weekday": 4, "photo_path": jpg,
                                          "photo_file_id": "FID", "photo_new": True})
            os.unlink(jpg)  # как на Render после рестарта: файла нет, фото осталось в БД
            assert post.has_photo and (await db.list_posts(10, 0))[0].has_photo
            clock.dt = datetime(2026, 11, 27, 11, 0, 1, tzinfo=MSK).astimezone(UTC)
            await svc.tick()
            assert svc.vk.photos == [b"\xff\xd8photo"]
            # удаление поста освобождает место под фото
            assert await svc.delete_post(post.id)
            assert await db.get_photo(post.id) is None
            await db.close()
    run(go())


def test_sqlite_migration_from_old_schema_with_photo_files():
    import sqlite3
    with tempfile.TemporaryDirectory() as tmp:
        old_photo = os.path.join(tmp, "old.jpg")
        with open(old_photo, "wb") as fh:
            fh.write(b"\xff\xd8old")
        path = os.path.join(tmp, "old.sqlite3")
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT, text TEXT NOT NULL,
              photo_path TEXT, photo_file_id TEXT, weekday INTEGER NOT NULL, send_time TEXT NOT NULL,
              repeat_weekly INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
              next_run_at TEXT NOT NULL, last_error TEXT, deleted INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        """)
        c.execute("INSERT INTO posts (text, photo_path, weekday, send_time, next_run_at, created_at, updated_at)"
                  " VALUES ('x', ?, 4, '12:00', '2026-12-04T09:00:00Z', 'n', 'n')", (old_photo,))
        c.commit()
        c.close()

        async def go():
            db = Database(path)
            await db.open()
            assert await db.get_photo(1) == b"\xff\xd8old"
            assert (await db.get_post(1)).has_photo
            await db.close()
        run(go())


def test_postgres_helpers():
    from app.db import clean_dsn, to_pg_sql
    assert to_pg_sql("UPDATE t SET a=?, b=? WHERE id=?") == "UPDATE t SET a=$1, b=$2 WHERE id=$3"
    assert to_pg_sql("SELECT 1") == "SELECT 1"
    assert clean_dsn("postgres://u:p@ep-x.neon.tech/db?sslmode=require&channel_binding=require") == \
        "postgresql://u:p@ep-x.neon.tech/db?sslmode=require"
    assert clean_dsn("postgresql://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"


def test_database_picks_backend_by_url():
    assert Database(path="x.sqlite3").kind == "sqlite"
    assert Database(path="x.sqlite3", url="postgresql://u:p@h/d").kind == "postgres"
