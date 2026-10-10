"""Contract v1 end to end: services in, sensors and Invite Calendar calls
out."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.ev_trip_planner.const import DOMAIN

from .conftest import CAL, DELFT, HAARLEM, OWN, at, make_entry, wall


def state(hass: HomeAssistant, key: str):
    return hass.states.get(f"sensor.ev_trip_planner_{key}")


async def call(hass: HomeAssistant, service: str, user: str | None = None, **data):
    await hass.services.async_call(
        DOMAIN, service, data, blocking=True, context=Context(user_id=user)
    )
    await hass.async_block_till_done()


def created(ic) -> list[dict[str, Any]]:
    return [d for a, d in ic.calls if a == "create_event"]


async def test_entities_and_ids(hass: HomeAssistant, entry) -> None:
    for key in ("trips", "search", "status", "plan"):
        assert state(hass, key) is not None, key
    assert state(hass, "status").state == "ok"
    assert state(hass, "search").state == "idle"
    plan = state(hass, "plan")
    assert float(plan.state) == 0
    assert plan.attributes["kind"] == "idle"
    assert plan.attributes["version"] == 1


async def test_not_ready_without_calendar(hass: HomeAssistant, world) -> None:
    e = make_entry()
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    assert e.state is ConfigEntryState.SETUP_RETRY


async def test_schedule_departure(hass: HomeAssistant, ic, world, entry) -> None:
    start = at(days=3, hour=8)
    await call(
        hass,
        "schedule",
        user="u_bart",
        start=wall(start),
        place="Markt 87, Delft",
        location="Markt 87, 2611 Delft",
        geo="52.011,4.358",
        one_way=True,
    )
    data = created(ic)[-1]
    assert data["summary"] == "Trip to Markt 87, Delft"
    assert data["attendees"] == ["bart@example.com"]
    assert data["start_date_time"] == start
    assert data["end_date_time"] - data["start_date_time"] == dt.timedelta(hours=1)
    assert data["description"].split("\n") == [
        "TRIP_TYPE=ONE_WAY",
        "TIME_IS=DEPARTURE",
        "GEO=52.011000,4.358000",
    ]
    status = state(hass, "status")
    assert status.state == "success"
    assert "leaving" in status.attributes["message"]
    trips = state(hass, "trips").attributes["trips"]
    assert len(trips) == 1
    assert trips[0]["one_way"] is True
    assert trips[0]["time_is"] == "departure"
    assert set(trips[0]) == {"uid", "start", "place", "location", "one_way", "time_is"}
    plan = state(hass, "plan")
    assert plan.attributes["kind"] == "trip"
    assert plan.attributes["place"] == "Markt 87, Delft"
    deadline = dt.datetime.fromisoformat(plan.attributes["deadline"])
    assert deadline == start - dt.timedelta(minutes=15)
    assert plan.attributes["notify_service"] == "notify.mobile_app_bart"
    # Predicted traffic: Waze asked for the departure, minutes from now.
    out_at = world.waze_calls[-1][2]
    assert abs(out_at - (start - dt_util.now()).total_seconds() / 60) < 2


async def test_schedule_defaults_user_to_caller(hass, ic, world, entry) -> None:
    await call(
        hass,
        "schedule",
        user="u_mar",
        start=wall(at(days=2, hour=9)),
        place="Delft",
        location="Delft",
        geo="52.01,4.36",
    )
    assert created(ic)[-1]["attendees"] == ["Marjolijn@example.com"]
    # Alerts and plan notifications go to her (case blind match).
    assert state(hass, "plan").attributes["notify_service"] == "notify.mobile_app_mar"


async def test_schedule_refusals(hass, ic, world, entry) -> None:
    base = {"place": "Delft", "location": "Delft", "geo": "52.01,4.36"}
    await call(hass, "schedule", user="u_bart", start=wall(at(days=-1)), **base)
    assert state(hass, "status").state == "error"
    await call(hass, "schedule", user="u_nobody", start=wall(at(days=1)), **base)
    assert "household member" in state(hass, "status").attributes["message"]
    await call(
        hass,
        "schedule",
        user="u_bart",
        start=wall(at(days=1)),
        place="X",
        location="Nowhereville 99",
        geo="garbage",
    )
    assert "on the map" in state(hass, "status").attributes["message"]
    assert created(ic) == []


async def test_geocoded_without_pin(hass, ic, world, entry) -> None:
    await call(
        hass,
        "schedule",
        user="u_bart",
        start=wall(at(days=2)),
        place="Delft",
        location="Delft",
    )
    assert "GEO=" not in created(ic)[-1]["description"]


async def test_pending_invite(hass, ic, world, entry) -> None:
    ic.smtp_down = True
    await call(
        hass,
        "schedule",
        user="u_bart",
        start=wall(at(days=2)),
        place="Delft",
        location="Delft",
        geo="52.01,4.36",
    )
    status = state(hass, "status")
    assert status.state == "warning"
    assert "next mailbox check" in status.attributes["message"]


async def test_arrive_by_and_reachability(hass, ic, world, entry) -> None:
    haarlem = {"place": "Haarlem", "location": "Haarlem", "geo": "52.38,4.64"}
    world.waze_calls.clear()
    # Too late even at 120 km/h: refused without Waze.
    await call(
        hass,
        "schedule",
        user="u_bart",
        start=wall(at(minutes=10)),
        arrive_by=True,
        **haarlem,
    )
    assert "at least" in state(hass, "status").attributes["message"]
    assert world.waze_calls == []
    # In the doubtful band, Waze says 60 min: refused.
    await call(
        hass,
        "schedule",
        user="u_bart",
        start=wall(at(minutes=35)),
        arrive_by=True,
        **haarlem,
    )
    assert "about 60 min" in state(hass, "status").attributes["message"]
    assert created(ic) == []
    # Waze down in the band: accepted rather than refused on a guess.
    world.waze_min = None
    await call(
        hass,
        "schedule",
        user="u_bart",
        start=wall(at(minutes=70)),
        arrive_by=True,
        **haarlem,
    )
    assert len(created(ic)) == 1
    world.waze_min = 60.0
    # Far ahead: accepted, costed as arrival (drive subtracted).
    start = at(days=1, hour=16)
    await call(
        hass, "schedule", user="u_bart", start=wall(start), arrive_by=True, **haarlem
    )
    assert "TIME_IS=ARRIVAL" in created(ic)[-1]["description"]
    trips = state(hass, "trips").attributes["trips"]
    assert all(t["time_is"] == "arrival" for t in trips)


async def test_arrival_deadline(hass, ic, world, entry) -> None:
    start = at(days=1, hour=16)
    await call(
        hass,
        "schedule",
        user="u_bart",
        start=wall(start),
        arrive_by=True,
        place="Haarlem",
        location="Haarlem",
        geo="52.38,4.64",
    )
    deadline = dt.datetime.fromisoformat(state(hass, "plan").attributes["deadline"])
    assert deadline == start - dt.timedelta(minutes=60 + 15)


async def test_move_and_cancel(hass, ic, world, entry) -> None:
    await call(
        hass,
        "schedule",
        user="u_bart",
        start=wall(at(days=2, hour=9)),
        place="Delft",
        location="Delft",
        geo="52.01,4.36",
    )
    uid = state(hass, "trips").attributes["trips"][0]["uid"]
    new = at(days=3, hour=11)
    await call(hass, "move", uid=uid, start=wall(new))
    assert [d for a, d in ic.calls if a == "update_event"][-1] == {
        "uid": uid,
        "start_date_time": new,
    }
    assert state(hass, "status").state == "success"
    assert (
        state(hass, "trips")
        .attributes["trips"][0]["start"]
        .startswith(new.strftime("%Y-%m-%dT%H:%M"))
    )
    await call(hass, "move", uid=uid, start=wall(at(days=-1)))
    assert "Nothing was changed" in state(hass, "status").attributes["message"]

    ic.smtp_down = True
    await call(hass, "cancel", uid=uid)
    status = state(hass, "status")
    assert status.state == "error"
    assert "NOT cancelled" in status.attributes["message"]
    assert "Delft on" in status.attributes["message"]
    assert len(state(hass, "trips").attributes["trips"]) == 1
    ic.smtp_down = False
    await call(hass, "cancel", uid=uid)
    assert state(hass, "status").state == "success"
    assert state(hass, "trips").attributes["trips"] == []
    assert state(hass, "plan").attributes["kind"] == "idle"

    await call(hass, "cancel", uid="nope")
    assert "No event with UID" in state(hass, "status").attributes["message"]


async def test_move_arrival_reachability(hass, ic, world, entry) -> None:
    await call(
        hass,
        "schedule",
        user="u_bart",
        start=wall(at(days=1, hour=16)),
        arrive_by=True,
        place="Haarlem",
        location="Haarlem",
        geo="52.38,4.64",
    )
    uid = state(hass, "trips").attributes["trips"][0]["uid"]
    await call(hass, "move", uid=uid, start=wall(at(minutes=40)))
    assert "Nothing was changed" in state(hass, "status").attributes["message"]
    assert not [a for a, _ in ic.calls if a == "update_event"]


async def test_search(hass, ic, world, entry) -> None:
    await call(hass, "search", query="lidl delft")
    search = state(hass, "search")
    assert search.state == "results"
    assert search.attributes["results"][0]["place"] == "Papsouwselaan 119, Delft"
    assert search.attributes["results"][0]["geo"] == "52.001200,4.371200"
    assert len(search.attributes["results"]) == 2
    await call(hass, "search", query="markt 14 delft")
    assert state(hass, "search").attributes["results"][0]["warning"] == (
        "number 12, not 14"
    )
    await call(hass, "search", query="zzz nowhere")
    assert state(hass, "search").state == "empty"
    assert state(hass, "status").state == "warning"
    world.nominatim_down = True
    await call(hass, "search", query="lidl delft")
    assert state(hass, "search").state == "failed"
    assert state(hass, "status").state == "error"
    await call(hass, "clear_search")
    assert state(hass, "search").state == "idle"
    assert state(hass, "status").state == "ok"


async def test_status_services(hass, entry) -> None:
    await call(hass, "set_status", level="warning", message="hello")
    status = state(hass, "status")
    assert status.state == "warning"
    assert status.attributes["source"] == "form"
    await call(hass, "clear_status")
    assert state(hass, "status").state == "ok"
    with pytest.raises(Exception):  # noqa: B017 - schema rejects the level
        await call(hass, "set_status", level="bogus", message="x")


async def test_inbound_invite_accepted_as_arrival(hass, ic, world, entry) -> None:
    start = at(days=1, hour=14)
    ic.add(
        uid="in-1",
        summary="Meeting",
        location="Delft",
        organizer="bart@example.com",
        start=start,
        end=start + dt.timedelta(hours=1),
    )
    ic.add(
        uid="in-bad",
        summary="Bad",
        location="Nowhereville",
        organizer="bart@example.com",
        start=at(days=4),
        end=at(days=4) + dt.timedelta(hours=1),
    )
    ic.add(
        uid="in-old",
        summary="Old",
        location="Delft",
        organizer="bart@example.com",
        managed=False,
        start=at(days=3),
        end=at(days=3) + dt.timedelta(hours=1),
    )
    await call(hass, "refresh")
    accepted = [d["uid"] for a, d in ic.calls if a == "accept_event"]
    assert accepted == ["in-1"]
    plan = state(hass, "plan")
    assert plan.attributes["place"] == "Meeting"
    deadline = dt.datetime.fromisoformat(plan.attributes["deadline"])
    assert deadline == start - dt.timedelta(minutes=60 + 15)
    # Inbound invites are not in the card's trip list.
    assert state(hass, "trips").attributes["trips"] == []
    # The bad address alerted its organizer once.
    alerts = [d for s, d in world.notified if d["title"] == "Trip: location not found"]
    assert len(alerts) == 1
    await call(hass, "refresh")
    alerts = [d for s, d in world.notified if d["title"] == "Trip: location not found"]
    assert len(alerts) == 1
    assert not [d for a, d in ic.calls if a == "accept_event"][1:]


async def test_waze_down_estimates_and_alerts(hass, ic, world, entry) -> None:
    world.waze_min = None
    await call(
        hass,
        "schedule",
        user="u_bart",
        start=wall(at(days=1, hour=9)),
        place="Delft",
        location="Delft",
        geo="52.01,4.36",
    )
    plan = state(hass, "plan")
    assert plan.attributes["kind"] == "trip"
    assert plan.attributes["km"] > 0
    assert any(d["title"] == "Trip: route estimated" for _, d in world.notified)


async def test_charging_stop_alert(hass, ic, world, entry) -> None:
    world.waze_km = 600.0
    await call(
        hass,
        "schedule",
        user="u_mar",
        start=wall(at(days=1, hour=9)),
        place="Paris",
        location="Paris",
        geo="48.85,2.35",
    )
    assert float(state(hass, "plan").state) == 100
    stops = [s for s, d in world.notified if d["title"] == "Trip: charging stop needed"]
    assert stops == ["mobile_app_mar"]
    # Delivered by push: nothing in the sidebar for the rest of the household.
    assert not _sidebar(hass)


async def test_alert_falls_back_to_sidebar_when_push_fails(
    hass, ic, world, entry
) -> None:
    async def broken(call) -> None:
        raise RuntimeError("phone gone")

    hass.services.async_register("notify", "mobile_app_mar", broken)
    world.waze_km = 600.0
    await call(
        hass,
        "schedule",
        user="u_mar",
        start=wall(at(days=1, hour=9)),
        place="Paris",
        location="Paris",
        geo="48.85,2.35",
    )
    notes = _sidebar(hass)
    assert len(notes) == 1
    note = next(iter(notes.values()))
    assert note["title"] == "Trip: charging stop needed"
    assert "notify.mobile_app_mar failed" in note["message"]


def _sidebar(hass) -> dict:
    from homeassistant.components import persistent_notification

    return {
        k: v
        for k, v in persistent_notification._async_get_or_create_notifications(
            hass
        ).items()
        if k.startswith("ev_trip_planner_")
    }


async def test_list_failure_keeps_plan(hass, ic, world, entry) -> None:
    await call(
        hass,
        "schedule",
        user="u_bart",
        start=wall(at(days=1, hour=9)),
        place="Delft",
        location="Delft",
        geo="52.01,4.36",
    )
    before = state(hass, "plan").attributes["deadline"]
    ic.fail_list = True
    await call(hass, "refresh")
    assert state(hass, "plan").attributes["deadline"] == before
    assert len(state(hass, "trips").attributes["trips"]) == 1


async def test_floor(hass: HomeAssistant, ic, world) -> None:
    e = make_entry(floor_soc=50)
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    plan = state(hass, "plan")
    assert float(plan.state) == 50
    assert plan.attributes["kind"] == "floor"
    # Big trip days out, car below the floor: floor first.
    hass.states.async_set("sensor.car_battery", "30")
    world.waze_km = 300.0
    await call(
        hass,
        "schedule",
        user="u_bart",
        start=wall(at(days=3, hour=9)),
        place="Delft",
        location="Delft",
        geo="52.01,4.36",
    )
    assert state(hass, "plan").attributes["kind"] == "floor"
    hass.states.async_set("sensor.car_battery", "60")
    await call(hass, "refresh")
    assert state(hass, "plan").attributes["kind"] == "trip"


async def test_removed_event_forgets_alerts(hass, ic, world, entry) -> None:
    ic.add(
        uid="in-bad",
        summary="Bad",
        location="Nowhereville",
        organizer=OWN.upper(),
        attendees=["bart@example.com"],
        start=at(days=1),
        end=at(days=1, minutes=60),
    )
    await call(hass, "refresh")
    coordinator = entry.runtime_data
    assert any(k.startswith("in-bad|") for k in coordinator._cache["alerted"])
    ic.events.clear()
    ic._fire(removed=["in-bad"])
    await hass.async_block_till_done()
    assert not any(k.startswith("in-bad|") for k in coordinator._cache["alerted"])


async def test_which_entry(hass: HomeAssistant, ic, world, entry) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "clear_status", {"config_entry_id": "nope"}, blocking=True
        )


async def test_calendar_entity_id_used(hass, ic, entry) -> None:
    assert entry.runtime_data.settings.calendar == CAL
    assert HAARLEM and DELFT


async def test_first_refresh_failure_retries(hass: HomeAssistant, ic, world) -> None:
    ic.fail_list = True
    e = make_entry()
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    assert e.state is ConfigEntryState.SETUP_RETRY
