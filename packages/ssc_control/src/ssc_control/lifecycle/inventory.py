"""Every app in the org, for its admins (SSC-025): owner, environments, sharing, last use.

One query, keyset-paged by slug. Each environment carries its live release, its latest
deployment and how widely it is shared (every grant counts, builder or user). ``last_used_at``
is the newest ``app_opened`` metrics event; it stays null until the gateway records them
(SSC-018).
"""

from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

MAX_PAGE: Final = 100

_PAGE = text(
    """
select a.id as app_id, a.slug, a.status, a.created_at,
       jsonb_build_object('user_id', u.id, 'display_name', u.display_name) as owner,
       used.last_used_at, envs.environments
from ssc.app a
join ssc.user_account u on u.org_id = a.org_id and u.id = a.owner_user_id
cross join lateral (
  select max(m.at) as last_used_at from ssc.metrics_event m
  where m.org_id = a.org_id and m.app_id = a.id and m.kind = 'app_opened'
) used
cross join lateral (
  select coalesce(jsonb_agg(jsonb_build_object(
    'environment_id', e.id,
    'name', e.name,
    'current_release', case when r.id is null then null
      else jsonb_build_object('release_id', r.id, 'number', r.number) end,
    'last_deploy', case when d.id is null then null
      else jsonb_build_object('operation_id', d.id, 'kind', d.kind, 'state', d.state,
                              'at', coalesce(d.finished_at, d.started_at)) end,
    'sharing', jsonb_build_object('org_wide', g.org_wide, 'users', g.users, 'groups', g.groups)
  ) order by e.name), '[]'::jsonb) as environments
  from ssc.environment e
  left join ssc.deployment cd on cd.org_id = e.org_id and cd.id = e.current_deployment_id
  left join ssc.release r on r.org_id = cd.org_id and r.app_id = cd.app_id and r.id = cd.release_id
  left join lateral (
    select x.id, x.kind, x.state, x.started_at, x.finished_at from ssc.deployment x
    where x.org_id = e.org_id and x.environment_id = e.id
    order by x.started_at desc, x.id desc limit 1
  ) d on true
  cross join lateral (
    select coalesce(bool_or(ag.subject_kind = 'org'), false) as org_wide,
           count(*) filter (where ag.subject_kind = 'user') as users,
           count(*) filter (where ag.subject_kind = 'group') as groups
    from ssc.app_grant ag where ag.org_id = e.org_id and ag.environment_id = e.id
  ) g
  where e.org_id = a.org_id and e.app_id = a.id
) envs
where a.org_id = :org and (cast(:cursor as text) is null or a.slug > cast(:cursor as text))
order by a.slug
limit :limit
"""
)


async def page(
    conn: AsyncConnection, org_id: str, *, limit: int = MAX_PAGE, cursor: str | None = None
) -> tuple[list[dict[str, Any]], str | None]:
    """Up to ``limit`` apps after slug ``cursor``, and the cursor of the next page if any."""
    params = {"org": org_id, "cursor": cursor, "limit": limit + 1}
    rows = [dict(r) for r in (await conn.execute(_PAGE, params)).mappings()]
    if len(rows) <= limit:
        return rows, None
    rows = rows[:limit]
    return rows, str(rows[-1]["slug"])
