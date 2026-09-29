import { createRootRouteWithContext, Link, Outlet } from '@tanstack/react-router';
import type { RouterContext } from '../context';

export const Route = createRootRouteWithContext<RouterContext>()({
  component: () => <Outlet />,
  notFoundComponent: NotFound,
});

function NotFound() {
  return (
    <main className="page">
      <h1>Page not found</h1>
      <p>
        <Link to="/">Back to apps</Link>
      </p>
    </main>
  );
}
