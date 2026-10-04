# Pilot success sheet

Agreed in writing before any pilot user logs in (SSC-060). The numbers are measured by `ssc_control.metrics.report` (SSC-028). The SQL in the appendix gives the same figures by hand. Days are UTC. Pilot day 1 is the first day of the window.

Customer: ____________________  Cell: ____________________  Day 1: ____-__-__  Invited builders: ____

## What the customer agrees to

Apps sleep when unused. The first load after a quiet spell takes several seconds: static site [T7: ___ s], Python API [T7: ___ s], Streamlit app [T7: ___ s], measured in SSC-086 T7 (not yet measured). A session app's connection ends at 60 minutes, and the app reloads and loses its session state when it does.

## The six numbers

| # | id | Number | Target | Due day |
| --- | --- | --- | --- | --- |
| 1 | active_builders_week6 | Share of invited builders active weekly in pilot week 6 (days 36-42) | 0.40 | 42 |
| 2 | apps_per_builder_day60 | Apps per active builder, days 1-60 | 2.0 | 60 |
| 3 | apps_using_data_day60 | Share of apps using company data or their own database, days 1-60 | 0.30 | 60 |
| 4 | tools_side_by_side | Builder tools with an app running side by side | 2 | 60 |
| 5 | approved_path | A written "approved path" designation from the customer | yes | 60 |
| 6 | paid_conversion | Payment | yes | 60 |

A builder deployed or shared. Every builder active in week 6 counts as invited. An app counts as using data when it recorded `data_query` or `database_use`.

## Fill the sheet

```
SSC_DATABASE_DSN=<app role dsn> uv run python -m ssc_control.metrics.report \
  --org <org_id> --since <day 1> --as-of <date> --invited-builders <N> \
  --approved-path yes|no --paid yes|no
```

Add `--json` for the machine form. Numbers 5 and 6 are the two written yes/no items, passed as flags.

## Stop rule

Two misses by day 60 means stop and rethink. The report prints `stop` at two misses, `undecided` while misses plus open items could still reach two, and `continue` otherwise.

## Ten places

The base database tier holds ten places (`PLACES_TOTAL`). One place is one environment with a database, so an app with a stateful preview and a stateful prod takes two. Only `database_use` takes places. `data_query` goes through the data gateway and takes none.

Check number 3 against the places: apps expected at day 60, times 0.30, rounded up, times environments per app, must be 10 or fewer. With prod only that holds up to 33 apps; with a stateful preview too, up to 16.

Places check, filled when the sheet is filled:

- Apps deployed: ___
- Worst-case apps taking places (0.30 x apps deployed, rounded up): ___
- Places each (1 if prod only, 2 with a stateful preview): ___
- Places needed: ___ of 10
- Measured places used (appendix, `places_used`): ___

If places needed is over 10, agree the bigger database tier (SSC-040) before day 60.

## Signed

| Role | Name | Date | Signature |
| --- | --- | --- | --- |
| Customer | | | |
| Delimitus | | | |

## Filled with dogfood numbers

Not filled. Filled live from the dogfood org after SSC-030, never from seeded or local data.

| id | Observed | n |
| --- | --- | --- |
| active_builders_week6 | | |
| apps_per_builder_day60 | | |
| apps_using_data_day60 | | |
| tools_side_by_side | | |
| approved_path | | |
| paid_conversion | | |

Places used: ____ of 10.

## Appendix: the SQL

Run as the app role in one read-only transaction, bound to the org, with the session in UTC. In `psql`:

```
\set org org_xxxxxxxxxxxxxxxxxxxx
\set day1 2026-06-01
\set asof 2026-07-30
begin read only;
select set_config('ssc.org', :'org', true);
```

`asof` is the last day counted. Each query below is the one in `report.py` that fills the number.

Number 1, divide by the invited builders:

```sql active_builders_week6
with w as (select
  (cast(:'day1' as date) + 35)::timestamp at time zone 'UTC' as lo,
  least(cast(:'day1' as date) + 42, cast(:'asof' as date) + 1)::timestamp at time zone 'UTC' as hi)
select count(distinct pseudonym) as active_builders
from ssc.metrics_event, w
where org_id = :'org' and kind in ('deploy', 'share') and pseudonym is not null
  and at >= w.lo and at < w.hi;
```

Number 2:

```sql apps_per_builder_day60
with w as (select
  cast(:'day1' as date)::timestamp at time zone 'UTC' as lo,
  least(cast(:'day1' as date) + 60, cast(:'asof' as date) + 1)::timestamp at time zone 'UTC' as hi)
select avg(apps)::float8 as apps_per_builder, count(*) as builders from (
  select count(distinct app_id) as apps from ssc.metrics_event, w
  where org_id = :'org' and kind = 'deploy' and pseudonym is not null and app_id is not null
    and at >= w.lo and at < w.hi
  group by pseudonym) per_builder;
```

Number 3, divide the first column by the second:

```sql apps_using_data_day60
with w as (select
  cast(:'day1' as date)::timestamp at time zone 'UTC' as lo,
  least(cast(:'day1' as date) + 60, cast(:'asof' as date) + 1)::timestamp at time zone 'UTC' as hi),
deployed as (
  select distinct app_id from ssc.metrics_event, w
  where org_id = :'org' and kind = 'deploy' and app_id is not null and at >= w.lo and at < w.hi)
select count(*) filter (where exists (
    select 1 from ssc.metrics_event u, w
    where u.org_id = :'org' and u.app_id = deployed.app_id
      and u.kind in ('data_query', 'database_use') and u.at >= w.lo and u.at < w.hi))
  as apps_using_data, count(*) as apps_deployed
from deployed;
```

Number 4, from each running app's latest deploy. A tool named `other` or none does not count:

```sql tools_side_by_side
with w as (select
  least(cast(:'day1' as date) + 60, cast(:'asof' as date) + 1)::timestamp at time zone 'UTC' as hi)
select count(*) filter (where source_tool is not null and source_tool <> 'other') as tools,
  sum(apps)::int as running_apps
from (
  select source_tool, count(*) as apps from (
    select distinct on (m.app_id) m.source_tool
    from ssc.metrics_event m join ssc.app a on a.org_id = m.org_id and a.id = m.app_id, w
    where m.org_id = :'org' and m.kind = 'deploy' and m.at < w.hi and a.status = 'active'
    order by m.app_id, m.at desc, m.id desc) latest
  group by source_tool) per_tool;
```

Places used now, then places by app:

```sql places_used
select count(*) as places_used from ssc.app_database where org_id = :'org';
```

```sql places_by_app
select e.app_id, count(*) as places
from ssc.app_database d join ssc.environment e on e.org_id = d.org_id and e.id = d.environment_id
where d.org_id = :'org'
group by e.app_id order by places desc, e.app_id;
```

Apps that used their own database in the window:

```sql apps_with_database
with w as (select
  cast(:'day1' as date)::timestamp at time zone 'UTC' as lo,
  least(cast(:'day1' as date) + 60, cast(:'asof' as date) + 1)::timestamp at time zone 'UTC' as hi)
select count(distinct app_id) as apps_with_database from ssc.metrics_event, w
where org_id = :'org' and kind = 'database_use' and app_id is not null
  and at >= w.lo and at < w.hi;
```
