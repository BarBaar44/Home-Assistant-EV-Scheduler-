"""The planner: reads the trip calendar (an Invite Calendar entity), costs
the next cluster of trips, publishes one plan, and serves the card's
services (contract v1).

Runs every UPDATE_MINUTES (time passing moves deadlines and the floor) and
right after invite_calendar_updated for its calendar."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import planner
from .const import (
    ALERT_KEEP_PAST_SECONDS,
    CONTRACT_VERSION,
    DOMAIN,
    IC_DOMAIN,
    IC_UPDATED_EVENT,
    LOGGER,
    MARK_ARRIVAL,
    MARK_DEPARTURE,
    MARK_ONE_WAY,
    MARK_ROUND_TRIP,
    STATUS_LEVELS,
    STATUS_SOURCES,
    TRIP_LIST_DAYS,
    UPDATE_MINUTES,
)
from .geocode import Geocoder, LookupFailed
from .options import Settings, settings
from .planner import Coords
from .routing import Router

STORAGE_VERSION = 1


@dataclass
class Plan:
    """The one plan published for the charger."""

    soc: float = 0.0
    kind: str = "idle"  # trip | floor | idle
    deadline: dt.datetime | None = None
    uid: str | None = None
    place: str | None = None
    km: float | None = None
    notify_service: str | None = None


@dataclass
class Status:
    level: str = "ok"
    message: str = ""
    source: str = ""
    updated: dt.datetime = field(default_factory=dt_util.now)


@dataclass
class Search:
    state: str = "idle"  # idle | results | empty | failed
    query: str = ""
    results: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PlannerData:
    trips: list[dict[str, Any]] = field(default_factory=list)
    plan: Plan = field(default_factory=Plan)


class TripPlannerCoordinator(DataUpdateCoordinator[PlannerData]):
    """One trip calendar, one car."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=dt.timedelta(minutes=UPDATE_MINUTES),
        )
        self.settings: Settings = settings(entry)
        self.status = Status()
        self.search = Search()
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._cache: dict[str, Any] = {"geocode": {}, "routes": {}, "alerted": {}}
        self.geocoder = Geocoder(hass, self.settings.contact, self._cache["geocode"])
        self.router = Router(hass, self._cache["routes"])
        self._lock = asyncio.Lock()
        # uid -> info about upcoming own trips, for banners and moves.
        self._trip_info: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ setup

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        for key in self._cache:
            if isinstance(stored.get(key), dict):
                self._cache[key].update(stored[key])

    def _save(self) -> None:
        self._store.async_delay_save(lambda: self._cache, 10)

    @callback
    def async_subscribe(self) -> None:
        """Recalculate when the calendar changed; forget alerts of events
        that left it."""

        @callback
        def _changed(event: Event) -> None:
            if event.data.get("entity_id") != self.settings.calendar:
                return
            removed = event.data.get("removed") or []
            if removed:
                alerted = self._cache["alerted"]
                for key in [k for k in alerted if k.split("|occ=")[0] in removed]:
                    del alerted[key]
                self._save()
            self.hass.async_create_task(self.async_request_refresh())

        self.config_entry.async_on_unload(
            self.hass.bus.async_listen(IC_UPDATED_EVENT, _changed)
        )

    # ------------------------------------------------- Invite Calendar

    async def _ic(self, action: str, **data: Any) -> dict[str, Any]:
        """Call an Invite Calendar action on our calendar. Raises
        HomeAssistantError with the integration's own (translated) text."""
        resp = await self.hass.services.async_call(
            IC_DOMAIN,
            action,
            data,
            target={"entity_id": self.settings.calendar},
            blocking=True,
            return_response=True,
        )
        if isinstance(resp, dict) and self.settings.calendar in resp:
            resp = resp[self.settings.calendar]
        return resp if isinstance(resp, dict) else {}

    async def _list_events(self, days: int) -> list[dict[str, Any]]:
        resp = await self._ic(
            "list_events", start=dt_util.now(), duration=dt.timedelta(days=days)
        )
        return list(resp.get("events") or [])

    # ------------------------------------------------------------ update

    async def _async_update_data(self) -> PlannerData:
        async with self._lock:
            try:
                events = await self._list_events(TRIP_LIST_DAYS)
            except Exception as err:  # noqa: BLE001 - keep the last plan
                if self.data is not None:
                    LOGGER.warning("list_events failed, keeping the plan: %s", err)
                    return self.data
                # First refresh: setup is retried later.
                raise UpdateFailed(
                    f"Could not read {self.settings.calendar}: {err}"
                ) from err
            trips = self._trips(events)
            plan = await self._plan(events)
            if plan is None:
                plan = self.data.plan if self.data else Plan()
            self._save()
            return PlannerData(trips=trips, plan=plan)

    def _start(self, event: dict[str, Any]) -> dt.datetime | None:
        return planner.event_start(
            event, dt_util.get_default_time_zone(), self.settings.all_day_hour
        )

    def _trips(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Upcoming trips this calendar organizes, soonest first. Do not
        use recurrence_id to tell series from single events: up to Invite
        Calendar 1.2.1 every event carried one."""
        now = dt_util.now()
        rows = []
        for ev in events:
            if not ev.get("own") or planner.is_cancelled(ev) or not ev.get("uid"):
                continue
            if ev.get("all_day"):
                continue
            start = self._start(ev)
            if start is None or start < now:
                continue
            rows.append((start, ev))
        rows.sort(key=lambda r: r[0])
        trips = []
        self._trip_info.clear()
        for start, ev in rows:
            place = planner.event_place(ev)
            description = ev.get("description") or ""
            arrival = MARK_ARRIVAL in description
            trips.append(
                {
                    "uid": ev["uid"],
                    "start": start.isoformat(timespec="seconds"),
                    "place": place,
                    "location": str(ev.get("location") or ""),
                    "one_way": planner.is_one_way(description),
                    "time_is": "arrival" if arrival else "departure",
                }
            )
            self._trip_info[ev["uid"]] = {
                "name": f"{place} on {start.strftime('%a %d %b, %H:%M')}",
                "place": place,
                "arrival": arrival,
                "geo": planner.pinned_coords(description),
            }
        return trips

    # -------------------------------------------------------------- plan

    def _home(self) -> Coords | None:
        home = self.hass.states.get("zone.home")
        if home and "latitude" in home.attributes:
            return (home.attributes["latitude"], home.attributes["longitude"])
        return None

    def _float_state(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        try:
            return float(state.state) if state else None
        except TypeError, ValueError:
            return None

    async def _plan(self, events: list[dict[str, Any]]) -> Plan | None:
        """The plan for the next cluster of located events, or None to keep
        the previous one (the first event could not be located)."""
        s = self.settings
        now = dt_util.now()
        home = self._home()
        if home is None:
            LOGGER.warning("zone.home is unavailable, cannot cost trips")
            return None

        horizon = now + dt.timedelta(days=s.lookahead_days)
        located = []
        for ev in events:
            if not ev.get("location") or planner.is_cancelled(ev):
                continue
            start = self._start(ev)
            if start is None or start > horizon:
                continue
            # Occurrences already under way (started, not ended) still
            # count until their deadline passes, as with list_events.
            located.append((start, ev))
        located.sort(key=lambda r: r[0])

        self._prune_alerts(time.time())
        self.geocoder.prune()
        self.router.prune()

        if not located:
            soc, deadline, kind = planner.choose_plan(
                None, None, None, now, s.floor_soc, s.floor_ready_hour
            )
            return Plan(
                soc=soc, kind=kind, deadline=deadline, notify_service=s.default_notify
            )

        first_start = located[0][0]
        cluster_end = first_start + dt.timedelta(hours=s.cluster_hours)
        total_km = 0.0
        first_out = 0.0
        accepted_now: set[str] = set()
        clean_keys = []

        for index, (start, ev) in enumerate(located):
            in_cluster = start < cluster_end
            uid = ev.get("uid") or f"nouid:{ev.get('summary', '')}"
            key = f"{uid}|occ={int(start.timestamp())}"
            summary = ev.get("summary") or "an event"
            description = ev.get("description") or ""
            notify, _ = s.notify_for(planner.person_email(ev))

            dest = planner.pinned_coords(description)
            if dest is None:
                dest = await self.geocoder.geocode(ev["location"], home)
            if dest is None:
                LOGGER.warning("Could not geocode %r for %r", ev["location"], summary)
                await self._alert(
                    key,
                    "geocode_failed",
                    "Trip: location not found",
                    f'Could not identify the location "{ev["location"]}" for '
                    f'"{summary}". Please check the address.',
                    notify,
                )
                if index == 0:
                    return None
                continue

            await self._accept_if_due(ev, accepted_now)
            if not in_cluster:
                continue

            departure = planner.is_departure(ev)
            one_way = planner.is_one_way(description)
            route = await self.router.route(home, dest, start, departure, one_way, now)
            if route is None:
                route = planner.estimate_route(home, dest, one_way)
                await self._alert(
                    key,
                    "route_estimated",
                    "Trip: route estimated",
                    f'Waze could not calculate a route to "{ev["location"]}" for '
                    f'"{summary}". Charging is planned on an estimate of '
                    f"{route.km:.0f} km instead; it switches back to the real "
                    f"route as soon as Waze answers again.",
                    notify,
                )
            else:
                clean_keys.append(key)
            total_km += route.km
            if index == 0:
                first_out = route.out_min

        first_ev = located[0][1]
        first_key = f"{first_ev.get('uid')}|occ={int(first_start.timestamp())}"
        first_notify, reason = s.notify_for(planner.person_email(first_ev))
        if reason:
            LOGGER.warning("Alerts for this trip go to %s: %s", first_notify, reason)

        wh_km = self._float_state(s.efficiency_sensor) or s.fallback_wh_km
        kwh, raw_soc = planner.required_soc(
            total_km, wh_km, s.battery_kwh, s.safety_buffer
        )
        if raw_soc > 100:
            await self._alert(
                first_key,
                "insufficient_range",
                "Trip: charging stop needed",
                f'"{first_ev.get("summary") or "The trip"}" needs about '
                f"{kwh:.0f} kWh ({total_km:.0f} km), more than a full charge. "
                f"Plan a charging stop along the way.",
                first_notify,
            )
        trip_soc = min(100.0, raw_soc)
        departure = planner.is_departure(first_ev)
        trip_deadline = planner.deadline(
            first_start, departure, first_out, s.prep_buffer, now
        )
        soc, deadline, kind = planner.choose_plan(
            trip_soc,
            trip_deadline,
            self._float_state(s.soc_sensor),
            now,
            s.floor_soc,
            s.floor_ready_hour,
        )
        # A real route clears an earlier "route estimated" alert, so the next
        # Waze failure is reported again. Only that one: a charging stop
        # alert shares the key and must not repeat every run.
        alerted = self._cache["alerted"]
        for key in clean_keys:
            if alerted.get(key) == "route_estimated":
                del alerted[key]

        LOGGER.info(
            "Next trip cluster (first %r): %.1f km, %.1f kWh at %.0f Wh/km, "
            "trip SOC %.1f%% by %s (start %s as %s, %.0f min drive + %.0f min "
            "buffer); published %s %.1f%%",
            first_ev.get("summary"),
            total_km,
            kwh,
            wh_km,
            trip_soc,
            trip_deadline.strftime("%Y-%m-%d %H:%M"),
            first_start.strftime("%H:%M"),
            "departure" if departure else "arrival",
            0 if departure else first_out,
            s.prep_buffer,
            kind,
            soc,
        )
        if kind != "trip":
            return Plan(
                soc=soc, kind=kind, deadline=deadline, notify_service=s.default_notify
            )
        return Plan(
            soc=round(soc, 1),
            kind="trip",
            deadline=deadline,
            uid=first_ev.get("uid"),
            place=planner.event_place(first_ev),
            km=round(total_km, 1),
            notify_service=first_notify,
        )

    async def _accept_if_due(self, ev: dict[str, Any], done: set[str]) -> None:
        """RSVP ACCEPTED for an inbound invite whose location was found:
        managed, not own, not yet accepted at this SEQUENCE. Once per uid
        per run, so a series is answered once."""
        uid = ev.get("uid")
        if (
            not self.settings.accept_invites
            or not uid
            or uid in done
            or ev.get("own")
            or not ev.get("managed")
            or ev.get("accepted")
        ):
            return
        done.add(uid)
        try:
            resp = await self._ic("accept_event", uid=uid)
        except Exception as err:  # noqa: BLE001 - never fatal for the plan
            LOGGER.warning("Could not accept %r (%s): %s", ev.get("summary"), uid, err)
            return
        if resp.get("sent"):
            LOGGER.info(
                "Accepted %r (%s), its location was found", ev.get("summary"), uid
            )

    # ------------------------------------------------------------ alerts

    def _prune_alerts(self, now_ts: float) -> None:
        alerted = self._cache["alerted"]
        stale = []
        for key in alerted:
            _, sep, ts = key.partition("|occ=")
            try:
                if sep and now_ts - int(ts) > ALERT_KEEP_PAST_SECONDS:
                    stale.append(key)
            except ValueError:
                stale.append(key)
        for key in stale:
            del alerted[key]

    async def _alert(
        self, key: str, kind: str, title: str, message: str, notify: str
    ) -> None:
        """Once per (occurrence, failure type): a push to the trip's person.

        The trip's person is the only one who needs to see it, so the
        sidebar (a persistent notification every HA user sees) is used only
        when the push cannot be delivered. An alert never disappears
        silently."""
        if self._cache["alerted"].get(key) == kind:
            return
        self._cache["alerted"][key] = kind
        domain, _, service = notify.partition(".")
        try:
            if not self.hass.services.has_service(domain, service):
                raise ServiceNotFound(domain, service)
            await self.hass.services.async_call(
                domain, service, {"title": title, "message": message}, blocking=True
            )
        except Exception as err:  # noqa: BLE001 - the sidebar is the fallback
            LOGGER.warning("Push via %s failed: %s", notify, err)
            slug = "".join(ch if ch.isalnum() else "_" for ch in key)[-40:]
            persistent_notification.async_create(
                self.hass,
                f"{message}\n\nThe push via {notify} failed: {err}",
                title,
                f"{DOMAIN}_{slug}",
            )

    # ------------------------------------------------------------ status

    def set_status(self, level: str, message: str, source: str) -> None:
        self.status = Status(level=level, message=message, source=source)
        self.async_update_listeners()

    def clear_status(self, force: bool = False) -> None:
        if self.status.level == "ok":
            return
        if not force and self.status.source not in STATUS_SOURCES:
            return
        self.set_status("ok", "", "")

    def _reject(self, message: str, source: str = "schedule") -> None:
        LOGGER.warning("Refused: %s", message)
        self.set_status("error", message, source)

    def _pending(self, to: str, done: str) -> None:
        self.set_status(
            "warning",
            f"{done}. The email to {to} couldn't be sent yet; it goes out "
            f"automatically on the next mailbox check.",
            "email",
        )

    def set_card_status(self, level: str, message: str) -> None:
        """Contract set_status: the card's own message, source form."""
        self.set_status(level if level in STATUS_LEVELS else "warning", message, "form")

    # ------------------------------------------------------------ search

    def _set_search(self, state: str, query: str = "", results=None) -> None:
        self.search = Search(state=state, query=query, results=results or [])
        self.async_update_listeners()

    async def async_search(self, query: str) -> None:
        query = (query or "").strip()
        if len(query) < 3:
            self._set_search("empty", query)
            self.set_status(
                "warning",
                "Type at least three characters of an address or place name, "
                "then search.",
                "search",
            )
            return
        try:
            candidates = await self.geocoder.search(query, self._home())
        except LookupFailed as err:
            LOGGER.warning("Nominatim search failed for %r: %s", query, err)
            self._set_search("failed", query)
            self.set_status(
                "error",
                "The address lookup service didn't answer. Try again in a "
                "moment; nothing has been scheduled.",
                "search",
            )
            return
        if not candidates:
            self._set_search("empty", query)
            self.set_status(
                "warning",
                f'No places found for "{query}". Try a street and town, or a '
                f"business name with the town after it.",
                "search",
            )
            return
        results = planner.search_results(query, candidates)
        self._set_search("results", query, results)
        self.set_status(
            "success",
            "Found one match. Check it, then schedule."
            if len(results) == 1
            else f"Found {len(results)} matches, best first. Pick the right "
            f"one before scheduling.",
            "search",
        )

    def clear_search(self) -> None:
        self._set_search("idle")
        self.clear_status(force=True)

    # ------------------------------------------------------ reachability

    async def _unreachable(
        self, dest: Coords | None, arrive_at: dt.datetime, place: str
    ) -> str | None:
        """Refusal text for an arrive by trip that can't be made leaving
        now, else None. Waze only in the doubtful band; if Waze fails
        there, accept rather than refuse on a guess."""
        home = self._home()
        if home is None or dest is None:
            return None
        now = dt_util.now()
        left = (arrive_at - now).total_seconds() / 60
        reach = planner.reach_bounds(home, dest, left)
        if reach.verdict == "ok":
            return None
        if reach.verdict == "late":
            drive = f"at least {reach.fastest_min:.0f} min"
        else:
            minutes = await self.router.drive_now(home, dest)
            if minutes is None or minutes <= left:
                return None
            drive = f"about {minutes:.0f} min"
        return planner.unreachable_text(place, arrive_at, now, drive)

    # ---------------------------------------------------------- schedule

    async def async_schedule(
        self,
        start: dt.datetime,
        place: str,
        location: str,
        geo: str | None,
        user_id: str | None,
        one_way: bool,
        arrive_by: bool,
    ) -> None:
        """Contract schedule. Checks cheapest first, every refusal on the
        status; the search is cleared only on success."""
        s = self.settings
        start = dt_util.as_local(_local(start))
        now = dt_util.now()
        if start <= now:
            self._reject(
                f"The trip time {start.strftime('%Y-%m-%d %H:%M')} is in the "
                f"past. A past trip would be invisible to the planner and "
                f"impossible to cancel."
            )
            return
        location = (location or "").strip()
        if not location:
            self._reject(
                "Pick a destination before scheduling. The planner needs one "
                "to work out how much charge the trip needs."
            )
            return
        place = (place or "").strip() or location
        member = s.member_by_user(user_id)
        if member is None:
            self._reject(
                "You're not set up as a household member of the EV trip "
                "planner, so there's nobody to send the invite to. Add "
                "yourself under Settings > Devices & services > EV Trip Planner."
            )
            return

        dest = planner.parse_geo(geo)
        pinned = dest is not None
        if dest is None:
            if geo:
                LOGGER.warning("Ignoring unusable geo %r, looking up %r", geo, location)
            dest = await self.geocoder.geocode(location, self._home())
            if dest is None:
                self.geocoder.forget(location)
                self._save()
                self._reject(
                    f'Couldn\'t find "{location}" on the map, so the trip '
                    f"wasn't scheduled. Search again and pick a result."
                )
                return

        if arrive_by:
            refusal = await self._unreachable(dest, start, place)
            if refusal:
                self._reject(refusal)
                return

        lines = [
            MARK_ONE_WAY if one_way else MARK_ROUND_TRIP,
            MARK_ARRIVAL if arrive_by else MARK_DEPARTURE,
        ]
        if pinned:
            lines.append(f"GEO={dest[0]:.6f},{dest[1]:.6f}")

        self.set_status("info", "Scheduling the trip…", "schedule")
        try:
            resp = await self._ic(
                "create_event",
                summary=f"Trip to {place}",
                start_date_time=start,
                end_date_time=start + dt.timedelta(minutes=s.trip_duration),
                location=location,
                description="\n".join(lines),
                attendees=[member.email],
            )
        except Exception as err:  # noqa: BLE001 - shown to the person
            self._reject(f"The trip wasn't scheduled: {err}")
            return

        self._set_search("idle")
        text = (
            f"Trip to {place}, {'arriving by' if arrive_by else 'leaving'} "
            f"{start.strftime('%a %d %b, %H:%M')}{' (one-way)' if one_way else ''}"
        )
        if resp.get("pending"):
            self._pending(member.email, f"{text}, scheduled")
        else:
            self.set_status("success", f"{text}, scheduled.", "schedule")
        LOGGER.info(
            "Scheduled trip %s to %r at %s (%s, %s) for %s",
            resp.get("uid"),
            location,
            start,
            "arrival" if arrive_by else "departure",
            "one way" if one_way else "round trip",
            member.email,
        )
        await self.async_refresh()

    # ------------------------------------------------------- move/cancel

    async def async_move(self, uid: str, start: dt.datetime) -> None:
        info = self._trip_info.get(uid) or {}
        name = info.get("name") or "the trip"
        start = dt_util.as_local(_local(start))
        if start <= dt_util.now():
            self._reject(
                f"The new trip time {start.strftime('%Y-%m-%d %H:%M')} is in "
                f"the past. Nothing was changed.",
                "reschedule",
            )
            return
        if info.get("arrival"):
            refusal = await self._unreachable(
                info.get("geo"), start, info.get("place") or name
            )
            if refusal:
                self._reject(f"{refusal} Nothing was changed.", "reschedule")
                return
        self.set_status("info", f'Moving "{name}"…', "reschedule")
        try:
            resp = await self._ic("update_event", uid=uid, start_date_time=start)
        except Exception as err:  # noqa: BLE001 - shown to the person
            self.set_status("error", f'"{name}" was not moved: {err}', "reschedule")
            await self.async_refresh()
            return
        moved = f"Moved to {start.strftime('%a %d %b, %H:%M')}"
        invited = resp.get("invited") or []
        if resp.get("pending"):
            self._pending(", ".join(invited) or "the member", moved)
        elif not invited:
            self.set_status(
                "warning",
                f"{moved} here, but nobody is invited to this trip, so no "
                f"calendar was updated.",
                "reschedule",
            )
        else:
            self.set_status("success", f"{moved}.", "reschedule")
        await self.async_refresh()

    async def async_cancel(self, uid: str) -> None:
        """Invite Calendar sends the CANCEL before it changes the calendar:
        when the mail can't go out nothing is cancelled, so the member's
        calendar never keeps a trip that no longer exists here."""
        name = (self._trip_info.get(uid) or {}).get("name") or "the trip"
        self.set_status("info", f'Cancelling "{name}"…', "cancel")
        try:
            resp = await self._ic("cancel_event", uid=uid)
        except Exception as err:  # noqa: BLE001 - shown to the person
            self.set_status(
                "error",
                f'"{name}" was NOT cancelled: {err} The trip is still planned '
                f"and charged for; try again later.",
                "cancel",
            )
            await self.async_refresh()
            return
        if resp.get("notified"):
            self.set_status("success", f'Cancelled "{name}".', "cancel")
        else:
            self.set_status(
                "warning",
                f'Cancelled "{name}" here, but it had nobody invited, so no '
                f"cancellation notice was sent.",
                "cancel",
            )
        await self.async_refresh()

    # ------------------------------------------------------- diagnostics

    def plan_dict(self) -> dict[str, Any]:
        plan = asdict(self.data.plan) if self.data else asdict(Plan())
        if plan["deadline"] is not None:
            plan["deadline"] = plan["deadline"].isoformat(timespec="seconds")
        plan["version"] = CONTRACT_VERSION
        return plan


def _local(value: dt.datetime) -> dt.datetime:
    """A naive time from a service call is local wall time, never UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt_util.get_default_time_zone())
    return value
