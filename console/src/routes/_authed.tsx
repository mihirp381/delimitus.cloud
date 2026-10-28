import { useInfiniteQuery } from '@tanstack/react-query';
import { createFileRoute, Link, Outlet, redirect, useLocation, useNavigate, useRouter } from '@tanstack/react-router';
import { type ReactNode, useEffect, useId, useRef, useState } from 'react';
import { INBOX_KEY, inboxPage, waitingCount } from '../api/approvals';
import { APP_TAB_TITLE, appTab } from '../app-detail/tabs';
import type { Whoami } from '../auth/admin';
import { Brand, BrandRule } from '../components/Brand';
import { Id } from '../components/Copy';
import { Icon } from '../components/Icon';
import { PageActionsSlot } from '../components/PageHeader';
import { Segmented } from '../components/Segmented';
import { ToastRegion } from '../components/Toast';
import { chooseTheme, readTheme, THEME_CHOICES, THEME_LABEL, type ThemeChoice } from '../theme';

export const Route = createFileRoute('/_authed')({
  beforeLoad: ({ context, location }) => {
    if (!context.session.token()) {
      throw redirect({ to: '/login', search: { next: location.href } });
    }
  },
  component: AuthedLayout,
});

/**
 * How often the shell asks the API who is signed in while the tab is on show. A person who was
 * deactivated gets a 401 here, which signs the console out (router.tsx): under 5 seconds, with the
 * request's own time counted.
 */
export const WHOAMI_EVERY_MS = 4000;

/** How often the shell counts the requests waiting for the signed-in person. */
export const WAITING_EVERY_MS = 60_000;

/** Below this width the sidebar is a drawer. The same figure is in styles.css. */
const DRAWER_BELOW = '(max-width: 899px)';

const ROLE_TEXT: Readonly<Record<NonNullable<Whoami['role']>, string>> = {
  admin: 'Org admin',
  member: 'Member',
};

function AuthedLayout() {
  const { api, queries, session, queryClient, isAdmin } = Route.useRouteContext();
  const navigate = useNavigate();
  const me = queries.useQuery('get', '/v1/whoami', {}, { refetchInterval: WHOAMI_EVERY_MS });
  const router = useRouter();
  // An app's own page belongs to "Apps": the link reads as current there too. Looks only; it goes where it did.
  // The shell reads no more of the location than this: drawn again on every move, it would ask who
  // is signed in once more on the way out to the login page.
  const inApp = useLocation({ select: (location) => location.pathname.startsWith('/apps/') });
  const admin = isAdmin(me.data);
  const [drawer, setDrawer] = useState(false);
  const [actions, setActions] = useState<HTMLElement | null>(null);
  const menuButton = useRef<HTMLButtonElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const countId = useId();
  // The inbox the Approvals page shows, so the page and this count are one read.
  const waiting = useInfiniteQuery({
    queryKey: INBOX_KEY,
    queryFn: ({ pageParam, signal }) => inboxPage(api, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_before,
    refetchInterval: WAITING_EVERY_MS,
  });
  const { count, more } = waitingCount(waiting.data?.pages);
  // "3+" when older requests were not read, so not counted.
  const waitingText = `${count}${more ? '+' : ''}`;

  function closeDrawer(returnFocus: boolean) {
    setDrawer(false);
    if (returnFocus) menuButton.current?.focus();
  }

  // Going somewhere closes the drawer.
  useEffect(() => router.subscribe('onResolved', () => setDrawer(false)), [router]);

  // While the drawer is open, Escape closes it; a window grown wide enough for the sidebar does too.
  useEffect(() => {
    if (!drawer) return undefined;
    closeButton.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeDrawer(true);
    };
    const narrow = typeof window.matchMedia === 'function' ? window.matchMedia(DRAWER_BELOW) : null;
    const onWidth = () => {
      if (narrow && !narrow.matches) setDrawer(false);
    };
    document.addEventListener('keydown', onKey);
    narrow?.addEventListener('change', onWidth);
    return () => {
      document.removeEventListener('keydown', onKey);
      narrow?.removeEventListener('change', onWidth);
    };
  }, [drawer]);

  function signOut() {
    // Forgets the tokens at once; the auth host's /revoke ends the session behind them.
    void session.signOut();
    queryClient.clear();
    void navigate({ to: '/login' });
  }

  return (
    <ToastRegion>
      <div className="shell">
        <a className="skip" href="#main">
          Skip to the page
        </a>
        <aside id="sidebar" className={drawer ? 'sidebar open' : 'sidebar'}>
          {/* The column runs the height of the page; what is in it stays in view as the page scrolls. */}
          <div className="sidebar-in">
            <BrandRule />
            <div className="sidebar-head">
              <Link to="/" className="brand">
                <Brand />
              </Link>
              <button
                ref={closeButton}
                type="button"
                className="icon-btn drawer-only"
                aria-label="Close menu"
                onClick={() => closeDrawer(true)}
              >
                <Icon name="close" />
              </button>
            </div>
            {me.data ? (
              <p className="sidebar-org">
                <span className="visually-hidden">Organisation </span>
                {me.data.org_name ?? 'Your organisation'}
              </p>
            ) : null}
            <nav aria-label="Main" className="sidenav">
              <div className="nav-group">
                <Link
                  to="/"
                  activeOptions={{ exact: true }}
                  className={inApp ? 'active' : undefined}
                  aria-current={inApp ? 'true' : undefined}
                >
                  <Icon name="apps" />
                  <span>Apps</span>
                </Link>
                <div className="nav-item">
                  <Link to="/approvals" aria-describedby={count > 0 ? countId : undefined}>
                    <Icon name="approvals" />
                    <span>Approvals</span>
                  </Link>
                  {count > 0 ? (
                    <span id={countId} className="nav-count">
                      {waitingText}{' '}
                      <span className="visually-hidden">waiting for you</span>
                    </span>
                  ) : null}
                </div>
                <Link to="/connections">
                  <Icon name="connections" />
                  <span>Connections</span>
                </Link>
                <Link to="/egress">
                  <Icon name="globe" />
                  <span>Internet access</span>
                </Link>
                {admin ? (
                  <Link to="/domain">
                    <Icon name="domain" />
                    <span>Your own address</span>
                  </Link>
                ) : null}
              </div>
              <hr className="nav-divider" />
              <div className="nav-group">
                {admin ? (
                  <div className="nav-group" role="group" aria-label="Admin">
                    <Link to="/people">
                      <Icon name="people" />
                      <span>People</span>
                    </Link>
                    <Link to="/audit">
                      <Icon name="audit" />
                      <span>Audit log</span>
                    </Link>
                    <Link to="/environment">
                      <Icon name="server" />
                      <span>Usage</span>
                    </Link>
                  </div>
                ) : null}
                <Link to="/help">
                  <Icon name="help" />
                  <span>Help</span>
                </Link>
              </div>
            </nav>
            <UserMenu me={me.data} onSignOut={signOut} />
          </div>
        </aside>
        {drawer ? <div className="scrim" onClick={() => closeDrawer(false)} /> : null}
        <div className="main" inert={drawer}>
          <header className="pagebar">
            <button
              ref={menuButton}
              type="button"
              className="icon-btn drawer-only"
              aria-label="Open menu"
              aria-expanded={drawer}
              aria-controls="sidebar"
              onClick={() => setDrawer(true)}
            >
              <Icon name="menu" />
            </button>
            <Breadcrumbs />
            <div className="pagebar-actions" ref={setActions} />
          </header>
          <PageActionsSlot.Provider value={actions}>
            <main id="main" className="page" tabIndex={-1}>
              <Outlet />
            </main>
          </PageActionsSlot.Provider>
        </div>
      </div>
    </ToastRegion>
  );
}

const SECTION_TITLE: Readonly<Record<string, string>> = {
  approvals: 'Approvals',
  connections: 'Connections',
  egress: 'Internet access',
  environment: 'Usage',
  people: 'People',
  audit: 'Audit log',
  help: 'Help',
};

/** A step back up is not where you are: only the last crumb is marked as the current page. */
const EXACT = { exact: true } as const;

interface Crumb {
  readonly text: string;
  readonly link?: ReactNode;
}

/**
 * Where this page sits: "Apps / expenses / Secrets". Every step but the last is a link. An app's
 * slug comes from the same read its page makes, so the bar adds no request.
 */
function Breadcrumbs() {
  const { queries } = Route.useRouteContext();
  const location = useLocation();
  const parts = location.pathname.split('/').filter(Boolean);
  const [section, id, sub] = parts;
  const appId = section === 'apps' && id ? id : null;
  const app = queries.useQuery(
    'get',
    '/v1/apps/{app_id}',
    { params: { path: { app_id: appId ?? '' } } },
    { enabled: appId !== null },
  );
  const connectionId = section === 'connections' && id ? id : null;
  const connections = queries.useQuery('get', '/v1/connections', undefined, { enabled: connectionId !== null });
  const crumbs: Crumb[] = [];
  if (connectionId) {
    crumbs.push({ text: 'Connections', link: <Link to="/connections">Connections</Link> });
    crumbs.push({ text: connections.data?.connections.find((c) => c.id === connectionId)?.name ?? 'Connection' });
  } else if (appId) {
    const slug = app.data?.slug ?? 'App';
    const tab = appTab((location.search as Record<string, unknown>).tab);
    const run = sub === 'kill-switch' ? 'Kill switch run' : sub === 'delete' ? 'Delete run' : null;
    crumbs.push({ text: 'Apps', link: <Link to="/" activeOptions={EXACT}>Apps</Link> });
    crumbs.push({
      text: slug,
      // A deleted app has no page left to link to: only its removal can still be read.
      link:
        (run || tab !== 'overview') && !app.isError ? (
          <Link to="/apps/$appId" params={{ appId }} activeOptions={EXACT}>
            {slug}
          </Link>
        ) : undefined,
    });
    if (run) crumbs.push({ text: run });
    else if (tab !== 'overview') crumbs.push({ text: APP_TAB_TITLE[tab] });
  } else if (section === 'approvals' && id) {
    crumbs.push({ text: 'Approvals', link: <Link to="/approvals" activeOptions={EXACT}>Approvals</Link> });
    crumbs.push({ text: 'Request' });
  } else {
    crumbs.push({ text: section ? (SECTION_TITLE[section] ?? 'Not found') : 'Apps' });
  }
  return (
    <nav aria-label="Breadcrumb" className="crumbs">
      <ol>
        {crumbs.map((c, i) => (
          <li key={`${i}:${c.text}`} aria-current={i === crumbs.length - 1 ? 'page' : undefined}>
            {c.link ?? c.text}
          </li>
        ))}
      </ol>
    </nav>
  );
}

/**
 * Who is signed in, at the foot of the sidebar. It opens the person's own choices: the theme, and
 * signing out. Escape, a click elsewhere and leaving it by keyboard all close it.
 */
function UserMenu({ me, onSignOut }: { readonly me: Whoami | undefined; readonly onSignOut: () => void }) {
  const [open, setOpen] = useState(false);
  const [theme, setTheme] = useState<ThemeChoice>(readTheme);
  const box = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const panelId = useId();
  const role = me ? (me.role ? ROLE_TEXT[me.role] : me.is_agent ? 'Agent' : 'No role') : null;
  const who = me?.name ?? 'Your account';

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      // The drawer listens for Escape too; this one is for the menu alone.
      event.stopPropagation();
      setOpen(false);
      button.current?.focus();
    };
    const onPointer = (event: PointerEvent) => {
      if (event.target instanceof Node && !box.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener('keydown', onKey, true);
    document.addEventListener('pointerdown', onPointer);
    return () => {
      document.removeEventListener('keydown', onKey, true);
      document.removeEventListener('pointerdown', onPointer);
    };
  }, [open]);

  return (
    <div
      ref={box}
      className="user"
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}
    >
      {open ? (
        <div id={panelId} className="user-menu" role="group" aria-label="Your account">
          {me ? (
            <div className="user-menu-who">
              <span className="user-menu-name">{who}</span>
              {me.email ? <span className="user-menu-email">{me.email}</span> : null}
              <span className="user-menu-org">
                Organisation id <Id value={me.org_id} label="organisation id" />
              </span>
            </div>
          ) : null}
          <div className="user-menu-row">
            <span className="menu-label" aria-hidden="true">
              Theme
            </span>
            <Segmented
              label="Theme"
              options={THEME_CHOICES.map((value) => ({ value, label: THEME_LABEL[value] }))}
              value={theme}
              onChange={(next) => {
                chooseTheme(next);
                setTheme(next);
              }}
            />
          </div>
          <button type="button" className="menu-item" onClick={onSignOut}>
            <Icon name="signOut" />
            Sign out
          </button>
        </div>
      ) : null}
      <button
        ref={button}
        type="button"
        className="user-button"
        aria-label={me ? `Signed in as ${who}, ${role}` : 'Your account'}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={() => setOpen((was) => !was)}
      >
        <span className="avatar" aria-hidden="true">
          <Icon name="user" />
        </span>
        {me ? (
          <span className="user-text">
            <span className="user-name">{who}</span>
            <span className="user-role">{role}</span>
          </span>
        ) : (
          // Who is signed in is not known yet, or the API would not say: signing out still works.
          <span className="user-text">
            <span className="user-name">Your account</span>
          </span>
        )}
        <Icon name="chevron" className="user-chevron" />
      </button>
    </div>
  );
}
