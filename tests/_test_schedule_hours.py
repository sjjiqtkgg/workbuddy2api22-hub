"""Configurable background schedule hours (#456), defaults unchanged."""
import calendar
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_scheduler
import wb_settings


class StubPool(object):
    def __init__(self, directory):
        self.dir = directory
        self.accounts = []


class ScheduleSettingsTests(unittest.TestCase):
    def test_defaults_when_the_key_is_absent(self):
        with tempfile.TemporaryDirectory(prefix="sched-") as directory:
            self.assertEqual(wb_settings.schedule_hours(directory), {
                "checkin": [9, 21], "travel": [9, 21],
                "keepalive": [22], "cat": [1],
            })

    def test_list_and_comma_string_are_accepted(self):
        with tempfile.TemporaryDirectory(prefix="sched-") as directory:
            wb_settings.set_schedule_hours(directory, {
                "checkin": "20, 10,10", "travel": [6, 18],
                "keepalive": "12", "cat": [1],
            })
            hours = wb_settings.schedule_hours(directory)
            self.assertEqual(hours["checkin"], [10, 20])
            self.assertEqual(hours["travel"], [6, 18])
            self.assertEqual(hours["keepalive"], [12])

    def test_invalid_empty_and_out_of_range_fall_back_per_key(self):
        with tempfile.TemporaryDirectory(prefix="sched-") as directory:
            data = wb_settings.load(directory)
            data["schedule"] = {
                "checkin": [24, -1, "x"],   # nothing valid -> default
                "travel": [],                 # empty -> default
                "keepalive": "99",            # out of range -> default
                "cat": [3, 3],                # valid -> kept
            }
            wb_settings.save(directory, data)
            hours = wb_settings.schedule_hours(directory)
            self.assertEqual(hours["checkin"], [9, 21])
            self.assertEqual(hours["travel"], [9, 21])
            self.assertEqual(hours["keepalive"], [22])
            self.assertEqual(hours["cat"], [3])

    def test_partial_update_keeps_other_keys(self):
        with tempfile.TemporaryDirectory(prefix="sched-") as directory:
            wb_settings.set_schedule_hours(directory, {"checkin": [10, 20]})
            wb_settings.set_schedule_hours(directory, {"keepalive": [12]})
            hours = wb_settings.schedule_hours(directory)
            self.assertEqual(hours["checkin"], [10, 20])
            self.assertEqual(hours["keepalive"], [12])
            self.assertEqual(hours["travel"], [9, 21])

    def test_invalid_write_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="sched-") as directory:
            with self.assertRaises(ValueError):
                wb_settings.set_schedule_hours(directory, {"checkin": []})
            with self.assertRaises(ValueError):
                wb_settings.set_schedule_hours(directory, {"cat": "26"})
            with self.assertRaises(ValueError):
                wb_settings.set_schedule_hours(directory, "9,21")


class SchedulerScheduleTests(unittest.TestCase):
    def test_scheduler_picks_up_changes_without_restart(self):
        with tempfile.TemporaryDirectory(prefix="sched-run-") as directory:
            wb_settings.set_schedule_hours(directory, {
                "checkin": [10, 20], "travel": [10, 20],
                "keepalive": [12], "cat": [1],
            })
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            self.assertEqual(scheduler.checkin_hours, [10, 20])
            self.assertEqual(scheduler.keepalive_hours, [12])
            self.assertEqual(scheduler.all_hours, [1, 10, 12, 20])
            self.assertIn("10:00/20:00", scheduler.status()["mode_cn"])
            self.assertIn("12:00", scheduler.status()["mode_intl"])

            wb_settings.set_schedule_hours(directory, {"checkin": [8]})
            self.assertTrue(scheduler._reload_schedule())
            self.assertEqual(scheduler.checkin_hours, [8])
            self.assertIn("08:00", scheduler.status()["mode_cn"])


class FakeAccount(object):
    def __init__(self, realm="cn"):
        self.uid = "fake-uid-1234"
        self.realm = realm
        self.tasks_enabled = True
        self.account_banned = False
        self.expires_at = 0.0
        self.last_error = ""
        self.checkins = 0
        self.reports = 0
        self.daily = 0
        self.refreshes = 0

    def task_active(self):
        return self.tasks_enabled

    def can_checkin(self):
        return True

    def checkin(self, trigger=""):
        self.checkins += 1
        return {"ok": True, "msg": "OK"}

    def can_report_activity(self):
        return True

    def report_activity(self):
        self.reports += 1
        return {"ok": True, "msg": "OK"}

    def can_daily_chat(self):
        return True

    def daily_chat(self, trigger=""):
        self.daily += 1
        return {"ok": True, "msg": "OK"}

    def refresh(self):
        self.refreshes += 1
        return True


class TaskGatingTests(unittest.TestCase):
    """A configured hour means that task, not "run the whole routine".

    #515: the 01:00 cat slot used to run check-in, activity report and cat
    travel as well, so a schedule of 10:00/20:00 still checked in at 01:00.
    """

    def _run_at(self, scheduler, account, hour, force=False):
        stamp = time.struct_time((2026, 10, 20, hour, 0, 0, 0, 0, -1))
        seen = {"travel": 0, "bonus": 0, "cat": 0}

        def travel(_acc):
            seen["travel"] += 1
            return {}

        def bonus(_acc):
            seen["bonus"] += 1
            return {}

        def night(_acc):
            seen["cat"] += 1
            return {"logs": []}

        with mock.patch.object(wb_scheduler.time, "localtime", lambda *a: stamp), \
                mock.patch.object(wb_scheduler.time, "sleep", lambda *_: None), \
                mock.patch.object(wb_scheduler, "do_cat_travel", travel), \
                mock.patch.object(wb_scheduler.wb_tasks, "run_streak_bonus", bonus), \
                mock.patch.object(wb_scheduler.wb_tasks, "run_night_growth", night):
            scheduler._run_cycle("test", force=force)
        return seen

    def test_task_flag_off_skips_tasks_but_keeps_keepalive(self):
        with tempfile.TemporaryDirectory(prefix="sched-task-") as directory:
            wb_settings.set_schedule_hours(directory, {
                "checkin": [10, 20], "travel": [10, 20],
                "keepalive": [10], "cat": [1],
            })
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            account = FakeAccount()
            account.tasks_enabled = False
            account.expires_at = time.time() + 60
            scheduler.pool.accounts = [account]

            seen = self._run_at(scheduler, account, 10)
            self.assertEqual((account.checkins, account.reports), (0, 0))
            self.assertEqual((seen["travel"], seen["bonus"], seen["cat"]),
                             (0, 0, 0))
            self.assertEqual(account.refreshes, 1)

    def test_task_flag_on_still_runs_checkin(self):
        with tempfile.TemporaryDirectory(prefix="sched-task-") as directory:
            wb_settings.set_schedule_hours(directory, {
                "checkin": [10, 20], "travel": [10, 20],
                "keepalive": [12], "cat": [1],
            })
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            account = FakeAccount()
            scheduler.pool.accounts = [account]

            self._run_at(scheduler, account, 10)
            self.assertEqual((account.checkins, account.reports), (1, 1))

    def test_cat_hour_does_not_run_checkin_or_travel(self):
        with tempfile.TemporaryDirectory(prefix="sched-gate-") as directory:
            wb_settings.set_schedule_hours(directory, {
                "checkin": [10, 20], "travel": [10, 20],
                "keepalive": [12], "cat": [1],
            })
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            account = FakeAccount()
            scheduler.pool.accounts = [account]

            seen = self._run_at(scheduler, account, 1)
            self.assertEqual((account.checkins, account.reports), (0, 0))
            self.assertEqual((seen["travel"], seen["bonus"], seen["cat"]), (0, 0, 1))

    def test_checkin_hour_runs_checkin_and_report_but_not_travel(self):
        with tempfile.TemporaryDirectory(prefix="sched-gate-") as directory:
            wb_settings.set_schedule_hours(directory, {
                "checkin": [10, 20], "travel": [9, 21],
                "keepalive": [12], "cat": [1],
            })
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            account = FakeAccount()
            scheduler.pool.accounts = [account]

            seen = self._run_at(scheduler, account, 10)
            self.assertEqual((account.checkins, account.reports), (1, 1))
            self.assertEqual((seen["travel"], seen["bonus"], seen["cat"]), (0, 0, 0))

    def test_manual_trigger_forces_every_task(self):
        with tempfile.TemporaryDirectory(prefix="sched-gate-") as directory:
            wb_settings.set_schedule_hours(directory, {
                "checkin": [10, 20], "travel": [10, 20],
                "keepalive": [12], "cat": [1],
            })
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            account = FakeAccount()
            scheduler.pool.accounts = [account]

            seen = self._run_at(scheduler, account, 3, force=True)
            self.assertEqual((account.checkins, account.reports), (1, 1))
            self.assertEqual((seen["travel"], seen["bonus"], seen["cat"]), (1, 1, 1))


class LogRoutingTests(unittest.TestCase):
    """Scheduler logs must land in the wb_proxy that is actually running.

    Started as `python wb_proxy.py`, the main module is `__main__`; a plain
    `import wb_proxy` loads a second copy whose LOG_STORE is None, so every
    scheduler line was dropped. Measured on a real install: 64k log rows and
    zero `[调度器]` entries, while the startup banner had 8.
    """

    def test_log_goes_to_the_running_main_module(self):
        recorded = []

        class FakeMain(object):
            LOG_BUFFER = []

            @staticmethod
            def add_log_entry(text, tag=None):
                recorded.append((text, tag))

        fake_main = FakeMain()
        with mock.patch.dict(sys.modules, {"__main__": fake_main}):
            self.assertIs(wb_scheduler._proxy_module(), fake_main)
            with tempfile.TemporaryDirectory(prefix="sched-log-") as directory:
                scheduler = wb_scheduler.Scheduler(StubPool(directory))
                scheduler.log("巡检开始")

        self.assertEqual(recorded, [("[调度器] 巡检开始", "scheduler")])

    def test_log_falls_back_when_main_module_is_not_the_proxy(self):
        # Under `python -m pytest` or an import from another host, `__main__`
        # is the test runner: it must not be mistaken for the gateway.
        cached = sys.modules.get("wb_proxy") or wb_scheduler._proxy_module()
        with mock.patch.dict(sys.modules, {"__main__": object()}):
            self.assertIs(wb_scheduler._proxy_module(), cached)



class CnRewardsRandomSlotTests(unittest.TestCase):
    @staticmethod
    def at(hour, minute=0, day=9):
        return calendar.timegm((2026, 11, day, hour, minute, 0)) - 8 * 3600

    def test_default_and_independent_hour_overrides(self):
        for fixed, random in (("checkin", "travel"), ("travel", "checkin")):
            with self.subTest(fixed=fixed), tempfile.TemporaryDirectory() as directory:
                scheduler = wb_scheduler.Scheduler(StubPool(directory))
                self.assertFalse(wb_settings.cn_rewards_random_enabled(directory))
                self.assertEqual((scheduler.checkin_hours, scheduler.travel_hours),
                                 ([9, 21], [9, 21]))
                self.assertNotIn(wb_settings.CN_REWARDS_RANDOM_KEY, wb_settings.load(directory))

                wb_settings.set_schedule_hours(directory, {fixed: [12]})
                wb_settings.set_schedule_hours(directory, {"keepalive": [20]})
                wb_settings.set_cn_rewards_random_enabled(directory, True)
                scheduler._reload_schedule()
                self.assertEqual(scheduler.cn_rewards_tasks, (random,))
                self.assertEqual(getattr(scheduler, fixed + "_hours"), [12])
                self.assertEqual(getattr(scheduler, random + "_hours"), [])
                self.assertEqual(scheduler.codearts_checkin_hours,
                                 [12] if fixed == "checkin" else [9, 21])

                wb_settings.set_cn_rewards_random_enabled(directory, False)
                scheduler._reload_schedule()
                self.assertEqual(getattr(scheduler, random + "_hours"), [9, 21])
                self.assertFalse(scheduler.cn_rewards_tasks)

    def test_draw_and_completion_are_persisted_on_the_beijing_day(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(wb_settings, "_draw_cn_rewards_time",
                                  side_effect=["08:37", "09:42"]) as draw:
            wb_settings.set_cn_rewards_random_enabled(directory, True)
            tasks, state = wb_settings.cn_rewards_schedule(directory, self.at(0))
            self.assertEqual((tasks, state["date"], state["time"]),
                             (("checkin", "travel"), "2026-11-09", "08:37"))
            wb_settings.complete_cn_rewards(directory, state["date"], tasks, {"trip"})
            _, restarted = wb_settings.cn_rewards_schedule(directory, self.at(20))
            self.assertEqual(restarted["time"], "08:37")
            self.assertEqual(restarted["done"], ["checkin", "travel"])
            self.assertEqual(restarted["pending"], ["trip"])
            _, next_day = wb_settings.cn_rewards_schedule(directory, self.at(0, day=10))
            self.assertEqual(next_day, {"date": "2026-11-10", "time": "09:42"})
            self.assertEqual(draw.call_count, 2)

    def test_due_slot_is_not_consumed_when_busy_or_when_cycle_raises(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(wb_scheduler.time, "time", return_value=self.at(8, 37)), \
                mock.patch.object(wb_settings, "_draw_cn_rewards_time", return_value="08:37"):
            wb_settings.set_cn_rewards_random_enabled(directory, True)
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            with mock.patch.object(scheduler, "_run_cycle", return_value=True) as run:
                self.assertFalse(scheduler._execute_cn_rewards(self.at(8, 36)))
                with scheduler._run_lock:
                    self.assertFalse(scheduler._execute_cn_rewards(self.at(8, 37)))
                run.assert_not_called()
                run.side_effect = [RuntimeError("test interruption"), True]
                with self.assertRaises(RuntimeError):
                    scheduler._execute_cn_rewards(self.at(8, 38))
                self.assertFalse(scheduler._execute_cn_rewards(self.at(11, 38)))
                self.assertTrue(scheduler._execute_cn_rewards(self.at(8, 39)))
                self.assertFalse(scheduler._execute_cn_rewards(self.at(8, 40)))

    def test_evening_only_checks_pending_travel_after_restart(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(wb_scheduler.time, "time", return_value=self.at(8, 37)), \
                mock.patch.object(wb_scheduler.time, "sleep"), \
                mock.patch.object(wb_settings, "_draw_cn_rewards_time", return_value="08:37"):
            wb_settings.set_cn_rewards_random_enabled(directory, True)
            pool = StubPool(directory)
            trip = FakeAccount()
            idle = FakeAccount()
            idle.uid = "idle"
            intl = FakeAccount("intl")
            intl.uid = "intl"
            pool.accounts = [trip, idle, intl]
            scheduler = wb_scheduler.Scheduler(pool)

            def travel(account, *, claim_only=False):
                if claim_only:
                    return {"ok": True, "action": "claim", "msg": "done"}
                return {"ok": True, "action": "depart" if account is trip else "idle"}

            with mock.patch.object(wb_scheduler, "do_cat_travel", side_effect=travel) as trips, \
                    mock.patch.object(wb_scheduler.wb_tasks, "do_buddy_open", return_value={}) as boxes, \
                    mock.patch.object(wb_scheduler.wb_tasks, "run_streak_bonus", return_value={}) as bonus, \
                    mock.patch.object(wb_scheduler.wb_tasks, "run_night_growth") as night:
                self.assertTrue(scheduler._execute_cn_rewards(self.at(8, 37)))
                self.assertEqual((trip.checkins, trip.reports, idle.checkins), (1, 1, 1))
                self.assertEqual((bonus.call_count, boxes.call_count), (2, 1))
                scheduler = wb_scheduler.Scheduler(pool)
                self.assertFalse(scheduler._execute_cn_rewards(self.at(9)))
                self.assertTrue(scheduler._execute_cn_rewards(self.at(21)))
                self.assertFalse(scheduler._execute_cn_rewards(self.at(21, 1)))
                self.assertEqual(trips.call_args_list,
                                 [mock.call(trip), mock.call(idle), mock.call(trip, claim_only=True)])
                self.assertEqual((trip.checkins, idle.checkins, intl.daily, trip.refreshes), (1, 1, 0, 0))
                self.assertEqual((bonus.call_count, boxes.call_count), (2, 1))
                self.assertTrue(scheduler._catchup_pending)
                night.assert_not_called()

    def test_claim_only_never_departs_an_idle_cat(self):
        tasks = wb_scheduler.wb_tasks
        account = mock.Mock()
        account.is_enterprise.return_value = False
        for state in ("idle", "traveling"):
            with mock.patch.object(tasks, "_travel_status", return_value={"state": state}), \
                    mock.patch.object(tasks._accounts, "urlopen") as request:
                self.assertEqual(tasks.do_cat_travel(account, claim_only=True)["action"], state)
                request.assert_not_called()

    def test_claim_only_uses_claim_endpoint_and_validates_result(self):
        tasks = wb_scheduler.wb_tasks
        account = mock.Mock(proxy=None)
        account.is_enterprise.return_value = False
        account.headers.return_value = {}
        with mock.patch.object(tasks, "_travel_status", return_value={"state": "arrived"}), \
                mock.patch.object(tasks._accounts, "urlopen") as request:
            response = request.return_value.__enter__.return_value
            response.read.side_effect = [b'{"code": 500}', b'{"code": 0, "data": {"reward_credit": 10}}']
            self.assertFalse(tasks.do_cat_travel(account, claim_only=True)["ok"])
            account.fetch_credits.assert_not_called()
            self.assertEqual(tasks.do_cat_travel(account, claim_only=True)["credit"], 10)
            account.fetch_credits.assert_called_once()
            self.assertEqual(request.call_count, 2)
            for call in request.call_args_list:
                self.assertTrue(call.args[0].full_url.endswith("/travel/claim"))
                self.assertEqual(call.args[0].method, "POST")


if __name__ == "__main__":
    unittest.main()
