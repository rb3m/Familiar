# 0019: Desk weather from pluggable providers (amends 0016)

Date: 2026-11-21
Status: Accepted

## Context

The Commander wants the desk companion to know the weather: current temperature, cloud cover,
precipitation, today's low and high and how the day will go, fetched about hourly rather than
every minute like the clock. Sources must be pluggable so a provider can be swapped, and moving
house must only mean changing the location. The crew should not keep mentioning the
temperature, with a config setting for how often they do. Until now the desk feed had one source
(the clock), hardcoded in `commands.py`.

## Options

1. A fixed Open-Meteo client inside the clock source.
2. A `weather` desk source that asks pluggable providers, with the desk sources themselves
   chosen by name.
3. METAR only (the Querétaro airport, `MMQT`): reliable but current conditions only, no forecast.

## Decision

Option 2.

- **Desk sources are plugins.** Entry-point group `familiar_desk.sources` (`clock`, `weather`);
  `[desk] sources = ["clock"]` by default, `["clock", "weather"]` to add weather. Each source is
  built from the whole `[desk]` table; unknown names are config errors.
- **Weather providers are plugins.** Group `familiar_desk.weather_providers`, protocol
  `WeatherProvider` (`name`, `fetch(place, client) -> WeatherReport`) in
  `familiar_desk/weather/`. The report is a normalised dataclass in SI units: `current`
  (observed time, condition word, temperature, feels-like, humidity, cloud cover, recent
  precipitation, wind) and `today` (low, high, rain chance, total, condition, and morning /
  afternoon / evening windows still to come), each `None` when the provider lacks it.
  Built-ins: `open-meteo` (free, keyless, forecast) and `metar` (aviationweather.gov, current
  only). Endpoints, the WMO and METAR code vocabularies and every threshold live in
  `familiar_desk/data/weather.yaml`, overridable per user.
- **Fallback fills gaps.** `providers = ["open-meteo", "metar"]` are tried in order, and later
  providers fill whatever earlier ones did not give (an outage leaves METAR's current conditions
  with "no forecast available"). An observation older than `max_age_s` does not count.
- **Location.** The place is `[desk]` latitude, longitude, timezone and location, which
  `[desk.weather]` may override. METAR needs `station`; a station farther than
  `max_station_km` from the coordinates is refused, so moving without changing it fails loudly
  instead of reporting another city's weather.
- **Talking about it.** The prompt always carries a compact weather line marked "background
  only: don't bring it up unless asked or it matters", so questions get answers. Unprompted
  remarks are ambient events, within `active_hours` (all day by default: if Familiar is on at
  night, the Commander is awake): one morning outlook from
  `outlook_hour`, a remark when rain (or a storm) starts or stops, and a temperature report every
  `remark_every_h` hours (default 6; 0 = never). The first sample never reports a change or a
  temperature. Event ids carry the date and slot, so a remark is never repeated.
- **Freshness.** The prompt line judges age by the observation's own time and today's date, not
  by the feed entry, which keeps old data after a failed fetch.

## Consequences

- Existing setups are unchanged; weather is opt-in through `[desk] sources`.
- One hourly request to Open-Meteo, plus METAR only when Open-Meteo is short. httpx with a short
  timeout and a `Familiar/<version>` User-Agent; no personal details are sent beyond coordinates.
- A forecast hour with a wet WMO code but a rain chance under `wet_min_chance_pct` reads as
  cloudy; otherwise a 14% drizzle hour would make the whole day "drizzle".
- Review fix (Codex): the prompt's forecast-date check uses the weather place's timezone, so
  a `[desk.weather]` place in another zone keeps its valid forecast.
- Hourly sampling can miss a short shower; lower `interval_s` if that matters.
- Prompt wording cannot strictly stop a persona volunteering the temperature; the cadence knob
  controls our own reports.

## Who agreed

- Commander: hourly weather, pluggable sources, easy relocation, a setting for how often the
  temperature comes up; METAR from Querétaro's airport as the fallback.
- Codex (gpt-6.1-sol, workhorse): agreed on plugin sources and providers; asked for nullable
  fields, explicit missing forecasts instead of "first success wins", stale-report rejection,
  station/location consistency, no change events on startup, unique event ids, and freshness
  from the observation time. All adopted. Not adopted: putting the provider protocol in
  `familiar.interfaces`; it is a `familiar-desk` extension point, and core never sees weather.
- agy: agreed on all five points; asked for an identifiable User-Agent (Open-Meteo's terms),
  dated event ids and graceful METAR-only prompts. Adopted.
- Claude (coordinator): merged; gap-filling fallback, station distance guard, low-chance wet
  hours read as dry.
